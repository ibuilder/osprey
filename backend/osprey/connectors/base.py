"""Connector framework — one interface every source implements (SPEC §6).

A connector turns an external source into a stream of ``RawEvent`` (via polling
and/or webhooks) and normalizes each into a ``NormalizedSignal``. New sources are
plugins registered on the :data:`registry`; the core never changes to add one.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from datetime import datetime

from pydantic import BaseModel, Field

from ..models import SourceKind, utcnow


class RawEvent(BaseModel):
    """Provider-native payload emitted by poll()/handle_webhook()."""

    external_id: str
    source_kind: SourceKind = SourceKind.general
    thread_key: str | None = None
    title: str = ""
    body: str = ""
    participants: list[str] = Field(default_factory=list)
    due_at: datetime | None = None
    amount: float | None = None
    url: str | None = None
    raw: dict = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=utcnow)


class NormalizedSignal(BaseModel):
    """Cleaned, source-agnostic signal. Ingest attaches ids + embedding + persists."""

    external_id: str
    source_kind: SourceKind
    thread_key: str | None = None
    title: str = ""
    body: str = ""
    participants: list[str] = Field(default_factory=list)
    due_at: datetime | None = None
    amount: float | None = None
    url: str | None = None
    raw: dict = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=utcnow)


class Health(BaseModel):
    ok: bool
    detail: str = ""


class SubscriptionState(BaseModel):
    """What a provider subscription leaves behind for the next renewal.

    Persisted into the connection's sealed tokens: ``subscription_id`` so renewal
    extends the existing subscription instead of creating another, and
    ``client_state`` so incoming notifications can be authenticated.
    """

    subscription_id: str
    client_state: str = ""
    expires_at: datetime | None = None


class Connection(BaseModel):
    """Lightweight connection view passed to connectors (no ORM coupling)."""

    id: str
    source_type: str
    account_ref: str = ""
    cursor: str | None = None
    tokens: dict = Field(default_factory=dict)  # decrypted at call time by ingest
    scopes: list[str] = Field(default_factory=list)


class Connector(ABC):
    source_type: str
    scopes: list[str] = []
    #: Scopes an admin may choose to grant on top of ``scopes``, each mapped to the
    #: plain-language reason shown before they opt in. Requested only when opted into
    #: at authorize time, and recorded on the connection. This is the one place a
    #: scope beyond read access can appear, and only with its reason.
    optional_scopes: dict[str, str] = {}
    supports_webhooks: bool = False

    # -- OAuth (desktop-app driven) ------------------------------------------ #
    def oauth_spec(self):
        """Return an ``OAuthSpec`` for OAuth2 sources, or ``None`` (e.g. filedrop)."""
        return None

    def client_credentials(self) -> tuple[str, str]:
        """(client_id, client_secret) for this source, from settings. Override as needed."""
        return "", ""

    async def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None) -> dict:
        """Exchange an authorization code for sealed-able tokens (generic OAuth2)."""
        import httpx

        spec = self.oauth_spec()
        if spec is None:
            raise NotImplementedError(f"{self.source_type} is not an OAuth source")
        client_id, client_secret = self.client_credentials()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
        }
        if client_secret:
            data["client_secret"] = client_secret
        if code_verifier:
            data["code_verifier"] = code_verifier
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(spec.token_endpoint, data=data)
            resp.raise_for_status()
            return resp.json()

    async def account_ref_from_tokens(self, tokens: dict) -> str:
        """Best-effort display label for the connected account (override per source)."""
        return ""

    @abstractmethod
    async def poll(self, conn: Connection, since: datetime | None) -> AsyncIterator[RawEvent]:
        """Yield events since the connection cursor / ``since`` (incremental)."""
        raise NotImplementedError
        yield  # pragma: no cover - marks this an async generator

    async def handle_webhook(self, payload: dict) -> AsyncIterator[RawEvent]:
        """Yield events from a verified webhook payload. Idempotent downstream."""
        raise NotImplementedError
        yield  # pragma: no cover

    @abstractmethod
    async def normalize(self, raw: RawEvent) -> NormalizedSignal:
        """Map a RawEvent to a NormalizedSignal."""
        raise NotImplementedError

    async def healthcheck(self, conn: Connection) -> Health:
        return Health(ok=True)

    # -- Webhook authentication ---------------------------------------------- #
    #: How incoming webhooks for this source are authenticated.
    #:
    #: ``"hmac"`` — Osprey's own ``X-Osprey-Signature``, for sources that post
    #: through a relay we control. ``"client_state"`` — the shared secret Osprey
    #: gave the provider when subscribing, echoed back in the payload. Providers
    #: like Microsoft Graph sign nothing and offer only the latter, so demanding
    #: an HMAC from them would reject every genuine notification. ``"signature"``
    #: — the provider signs the raw body with a secret Osprey registered with it
    #: (stored on the connection as ``webhook_secret``); the connector verifies.
    webhook_auth: str = "hmac"

    def webhook_client_state(self, payload: dict) -> str | None:
        """Extract the echoed shared secret from a payload, if it carries one."""
        return None

    def verify_webhook_signature(self, raw: bytes, headers: Mapping[str, str], secret: str) -> bool:
        """For ``webhook_auth = "signature"``: is this raw body signed with ``secret``?"""
        return False

    # -- Webhook subscription lifecycle -------------------------------------- #
    supports_subscriptions: bool = False

    async def ensure_subscription(
        self, conn: Connection, notify_url: str, lifecycle_url: str = ""
    ) -> SubscriptionState | None:
        """Create or renew a provider webhook subscription.

        Returns the state the caller must persist onto the connection — without
        that round trip the next renewal cannot find the existing subscription
        and silently creates a duplicate. Default is a no-op for sources with no
        subscriptions (filedrop, procore-poll).
        """
        return None

    def lifecycle_events(self, payload: dict) -> list[str]:
        """Classify a payload as provider subscription-lifecycle events.

        Returns the event names (e.g. ``reauthorizationRequired``) if this is a
        lifecycle callback rather than a data notification, else an empty list.
        Parsing lives here; deciding what to do about it lives in the service
        layer, which owns the database session.
        """
        return []


class _Registry:
    def __init__(self) -> None:
        self._by_type: dict[str, type[Connector]] = {}

    def register(self, cls: type[Connector]) -> type[Connector]:
        existing = self._by_type.get(cls.source_type)
        if existing is not None and existing is not cls:
            # Refuse rather than overwrite. With plugins loaded from installed
            # packages, a silent overwrite would let any package replace a built-in
            # connector -- and receive that source's sealed tokens -- by reusing its
            # name, with nothing in the logs to say it happened.
            raise ValueError(
                f"source_type {cls.source_type!r} is already registered to "
                f"{existing.__module__}.{existing.__qualname__}"
            )
        self._by_type[cls.source_type] = cls
        return cls

    def unregister(self, source_type: str) -> None:
        """Remove a connector. For tests that register throwaway connectors."""
        self._by_type.pop(source_type, None)

    def get(self, source_type: str) -> Connector:
        try:
            return self._by_type[source_type]()
        except KeyError as exc:
            raise KeyError(f"no connector registered for source_type={source_type!r}") from exc

    def types(self) -> list[str]:
        return sorted(self._by_type)


registry = _Registry()
