"""Concrete push senders: APNs (iOS) and FCM (Android), plus a routing composite.

Payload/message construction is pure and unit-tested; the network calls are thin
and guarded (a failure returns ``False`` and is logged, never raised). Selection is
config-driven via :func:`build_sender`; the offline default remains the logging sender.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
import jwt

from ..config import settings
from ..models import Device
from .notify import LoggingPushSender, PushMessage, PushSender

log = logging.getLogger("osprey.push")

APNS_PROD = "https://api.push.apple.com"
APNS_SANDBOX = "https://api.sandbox.push.apple.com"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


# --------------------------------------------------------------------------- #
# Pure payload builders (unit-tested, no I/O)
# --------------------------------------------------------------------------- #
def apns_payload(message: PushMessage) -> dict:
    return {
        "aps": {
            "alert": {"title": message.title, "body": message.body},
            "sound": "default",
            "mutable-content": 1,
        },
        **{k: v for k, v in message.data.items() if v is not None},
    }


def fcm_message(token: str, message: PushMessage) -> dict:
    return {
        "message": {
            "token": token,
            "notification": {"title": message.title, "body": message.body},
            "data": {k: str(v) for k, v in message.data.items() if v is not None},
        }
    }


# --------------------------------------------------------------------------- #
# APNs — token-based auth (ES256 JWT), HTTP/2
# --------------------------------------------------------------------------- #
class ApnsSender(PushSender):
    def __init__(self) -> None:
        self._jwt: tuple[str, float] | None = None  # (token, issued_at)

    def _provider_token(self) -> str:
        # APNs tokens are valid up to 60 min; refresh at ~50 min.
        if self._jwt and (time.time() - self._jwt[1]) < 3000:
            return self._jwt[0]
        token = jwt.encode(
            {"iss": settings.apns_team_id, "iat": int(time.time())},
            settings.apns_private_key,
            algorithm="ES256",
            headers={"kid": settings.apns_key_id},
        )
        self._jwt = (token, time.time())
        return token

    async def send(self, device: Device, message: PushMessage) -> bool:
        base = APNS_SANDBOX if settings.apns_use_sandbox else APNS_PROD
        headers = {
            "authorization": f"bearer {self._provider_token()}",
            "apns-topic": settings.apns_bundle_id,
            "apns-push-type": "alert",
        }
        try:
            async with httpx.AsyncClient(http2=True, timeout=20) as client:
                resp = await client.post(
                    f"{base}/3/device/{device.token}", headers=headers, json=apns_payload(message)
                )
            if resp.status_code == 200:
                return True
            log.warning("APNs push failed (%s): %s", resp.status_code, resp.text[:200])
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("APNs push error: %s", exc)
            return False


# --------------------------------------------------------------------------- #
# FCM — HTTP v1 with a service account
# --------------------------------------------------------------------------- #
class FcmSender(PushSender):
    def __init__(self) -> None:
        self._sa = json.loads(settings.fcm_service_account_json) if settings.fcm_service_account_json else {}
        self._access: tuple[str, float] | None = None

    async def _access_token(self) -> str:
        if self._access and (time.time() - self._access[1]) < 3300:
            return self._access[0]
        now = int(time.time())
        assertion = jwt.encode(
            {
                "iss": self._sa["client_email"],
                "scope": FCM_SCOPE,
                "aud": self._sa.get("token_uri", GOOGLE_TOKEN_URL),
                "iat": now,
                "exp": now + 3600,
            },
            self._sa["private_key"],
            algorithm="RS256",
        )
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                self._sa.get("token_uri", GOOGLE_TOKEN_URL),
                data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
            )
            resp.raise_for_status()
            token = resp.json()["access_token"]
        self._access = (token, time.time())
        return token

    async def send(self, device: Device, message: PushMessage) -> bool:
        project_id = settings.fcm_project_id or self._sa.get("project_id", "")
        try:
            access = await self._access_token()
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send",
                    headers={"Authorization": f"Bearer {access}"},
                    json=fcm_message(device.token, message),
                )
            if resp.status_code == 200:
                return True
            log.warning("FCM push failed (%s): %s", resp.status_code, resp.text[:200])
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("FCM push error: %s", exc)
            return False


# --------------------------------------------------------------------------- #
# Web Push (VAPID + aes128gcm)
# --------------------------------------------------------------------------- #
def parse_subscription(token: str) -> dict:
    """A web-push device token is the JSON PushSubscription from the browser."""
    sub = json.loads(token)
    if "endpoint" not in sub or "keys" not in sub:
        raise ValueError("web-push token must be a PushSubscription JSON")
    return sub


def webpush_notification(message: PushMessage) -> dict:
    return {"title": message.title, "body": message.body, "data": message.data}


class WebPushSender(PushSender):
    """Encrypted Web Push via VAPID. Uses ``pywebpush`` (payload encryption is
    non-trivial); if the library isn't installed the send is skipped, not raised."""

    def _vapid_claims(self) -> dict:
        return {"sub": settings.vapid_subject}

    async def send(self, device: Device, message: PushMessage) -> bool:
        try:
            from pywebpush import webpush  # lazy: optional dependency
        except Exception:  # noqa: BLE001
            log.warning("web push requested but 'pywebpush' is not installed")
            return False
        try:
            subscription = parse_subscription(device.token)
            payload = json.dumps(webpush_notification(message))

            def _send() -> bool:
                webpush(
                    subscription_info=subscription,
                    data=payload,
                    vapid_private_key=settings.vapid_private_key,
                    vapid_claims=dict(self._vapid_claims()),
                )
                return True

            # pywebpush is synchronous; run it off the event loop.
            return await asyncio.to_thread(_send)
        except Exception as exc:  # noqa: BLE001
            log.warning("web push error: %s", exc)
            return False


# --------------------------------------------------------------------------- #
# Routing composite
# --------------------------------------------------------------------------- #
class CompositePushSender(PushSender):
    """Route each device to the sender for its platform; fall back to logging."""

    def __init__(
        self,
        ios: PushSender | None = None,
        android: PushSender | None = None,
        web: PushSender | None = None,
    ) -> None:
        self._ios = ios
        self._android = android
        self._web = web
        self._fallback = LoggingPushSender()

    async def send(self, device: Device, message: PushMessage) -> bool:
        if device.platform == "ios" and self._ios:
            return await self._ios.send(device, message)
        if device.platform == "android" and self._android:
            return await self._android.send(device, message)
        if device.platform == "web" and self._web:
            return await self._web.send(device, message)
        return await self._fallback.send(device, message)


def build_sender() -> PushSender:
    """Construct the push sender from config. Defaults to the logging sender."""
    provider = settings.push_provider
    apns = ApnsSender() if settings.apns_private_key else None
    fcm = FcmSender() if settings.fcm_service_account_json else None
    web = WebPushSender() if settings.vapid_private_key else None
    if provider == "apns" and apns:
        return apns
    if provider == "fcm" and fcm:
        return fcm
    if provider == "webpush" and web:
        return web
    if provider == "auto" and (apns or fcm or web):
        return CompositePushSender(ios=apns, android=fcm, web=web)
    return LoggingPushSender()
