"""OIDC single sign-on — discovery, authorization-code + PKCE, ID-token verification.

This is the *app login* path (who may use Osprey), distinct from
``security/oauth.py``, which authorises *data sources* (which mailbox Osprey may
read). They share the PKCE and signed-state helpers and nothing else.

Verification is done here rather than delegated to a library so the trust
decisions are visible and reviewable:

* the ID token's signature is checked against the issuer's published JWKS, keyed
  by ``kid``, with the key set cached and re-fetched on an unknown ``kid``
  (which is how a routine provider key rotation is meant to be handled);
* ``iss``, ``aud``, ``exp``, and ``iat`` are all verified;
* ``nonce`` is bound to our signed ``state``, so an ID token obtained elsewhere
  cannot be replayed into a session here;
* ``email_verified`` is required, because an IdP that lets a user self-assert an
  unverified address would otherwise let them claim a colleague's account.

Only asymmetric algorithms are accepted. An ``HS256`` ID token would be verified
with the client secret, which turns any party holding that secret into an issuer.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
import jwt
from pydantic import BaseModel

from ..config import settings

log = logging.getLogger("osprey.oidc")

#: Asymmetric only, on purpose. See the module docstring.
_ALLOWED_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS512"]

_DISCOVERY_TTL_SECONDS = 3600
_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class OIDCError(Exception):
    """SSO could not be completed. The message is safe to show a user."""


class OIDCClaims(BaseModel):
    subject: str  # "{issuer}|{sub}" — globally unique, stable across renames
    email: str
    full_name: str = ""
    groups: list[str] = []


class _Discovery(BaseModel):
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    issuer: str
    end_session_endpoint: str = ""


_discovery_cache: dict[str, tuple[float, _Discovery]] = {}
#: jwks_uri -> (expires_at, {kid: PyJWK})
_jwks_cache: dict[str, tuple[float, dict[str, jwt.PyJWK]]] = {}


def allowed_redirect_uri(candidate: str | None) -> str:
    """Resolve and validate the redirect URI for one sign-in attempt.

    A native app cannot use a fixed redirect: RFC 8252 has it bind an ephemeral
    loopback port and register ``http://127.0.0.1`` with a wildcard port. So the
    client supplies its own, and this decides whether to honour it.

    Exactly two shapes are accepted:

    * the deployment's configured ``OSPREY_OIDC_REDIRECT_URL`` (a browser client
      served from a known origin), and
    * a loopback address -- ``127.0.0.1`` or ``[::1]``, any port, over plain HTTP,
      which is correct here precisely because the traffic never leaves the machine.

    Anything else is refused. Echoing a caller-supplied URL into the authorize
    request unchecked is how authorization codes get delivered to somebody else;
    that most providers also enforce a registered redirect is a second lock, not a
    reason to leave this one open. ``localhost`` is deliberately *not* accepted --
    it resolves through the host's name resolution, which another process can
    influence, whereas the literal loopback addresses cannot be redirected.
    """
    from .oauth import RedirectUriError, assert_loopback_redirect

    configured = settings.oidc_redirect_url
    if not candidate:
        if not configured:
            raise OIDCError("this server has no configured SSO redirect URL")
        return configured
    if candidate == configured:
        return candidate
    try:
        return assert_loopback_redirect(candidate)
    except RedirectUriError as exc:
        raise OIDCError(f"{exc}, or the configured SSO redirect URL") from exc


def is_configured() -> bool:
    return bool(
        settings.oidc_enabled
        and settings.oidc_issuer
        and settings.oidc_client_id
        and settings.oidc_redirect_url
    )


def _assert_configured() -> None:
    if not is_configured():
        raise OIDCError("single sign-on is not configured on this server")


async def discover(client: httpx.AsyncClient | None = None) -> _Discovery:
    """Fetch (and cache) the provider's ``.well-known`` document."""
    _assert_configured()
    issuer = settings.oidc_issuer.rstrip("/")
    cached = _discovery_cache.get(issuer)
    if cached and cached[0] > time.time():
        return cached[1]

    url = f"{issuer}/.well-known/openid-configuration"
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
    try:
        response = await client.get(url)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        raise OIDCError(f"could not reach the identity provider: {exc}") from exc
    finally:
        if owns_client:
            await client.aclose()

    try:
        doc = _Discovery(**{k: v for k, v in data.items() if k in _Discovery.model_fields})
    except Exception as exc:  # noqa: BLE001
        raise OIDCError("identity provider returned an unusable discovery document") from exc
    # The document must agree with the issuer we were configured to trust;
    # otherwise a hijacked well-known URL could point us at someone else's keys.
    if doc.issuer.rstrip("/") != issuer:
        raise OIDCError("identity provider issuer does not match OSPREY_OIDC_ISSUER")

    _discovery_cache[issuer] = (time.time() + _DISCOVERY_TTL_SECONDS, doc)
    return doc


def clear_discovery_cache() -> None:
    _discovery_cache.clear()
    _jwks_cache.clear()


async def _fetch_jwks(jwks_uri: str) -> dict[str, jwt.PyJWK]:
    """Download and parse the issuer's key set, keyed by ``kid``."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        try:
            response = await client.get(jwks_uri)
            response.raise_for_status()
            document = response.json()
        except Exception as exc:  # noqa: BLE001
            raise OIDCError(f"could not fetch the provider's signing keys: {exc}") from exc

    keys: dict[str, jwt.PyJWK] = {}
    for entry in document.get("keys", []):
        kid = entry.get("kid")
        if not kid:
            continue
        try:
            keys[kid] = jwt.PyJWK(entry)
        except Exception as exc:  # noqa: BLE001
            # One unusable key (an unsupported curve, say) must not discard the
            # rest of the set.
            log.warning("skipping unusable JWK %s: %s", kid, exc)
    if not keys:
        raise OIDCError("the provider published no usable signing keys")
    _jwks_cache[jwks_uri] = (time.time() + _DISCOVERY_TTL_SECONDS, keys)
    return keys


async def _signing_key(jwks_uri: str, id_token: str) -> jwt.PyJWK:
    """The key that signed this token, refetching once on an unknown ``kid``.

    PyJWT ships ``PyJWKClient`` for this, and it is deliberately not used: it
    fetches over a blocking ``urllib.request.urlopen``, which inside an async
    request handler stalls the whole event loop for the duration of the call --
    every other request on that worker waits on the IdP.
    """
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise OIDCError(f"the ID token header is unreadable: {exc}") from exc
    kid = header.get("kid")
    if not kid:
        raise OIDCError("the ID token carries no key id")

    cached = _jwks_cache.get(jwks_uri)
    if cached and cached[0] > time.time() and kid in cached[1]:
        return cached[1][kid]

    # Unknown kid, or a stale cache: refetch. This is the normal path for a
    # routine provider key rotation, not an error.
    keys = await _fetch_jwks(jwks_uri)
    if kid not in keys:
        raise OIDCError("the ID token was signed with a key the provider does not publish")
    return keys[kid]


async def build_authorize_url(
    *, state: str, nonce: str, code_challenge: str, redirect_uri: str | None = None
) -> str:
    """The URL to send the user's browser to."""
    from urllib.parse import urlencode

    doc = await discover()
    params = {
        "client_id": settings.oidc_client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri or settings.oidc_redirect_url,
        "scope": " ".join(settings.oidc_scopes),
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{doc.authorization_endpoint}?{urlencode(params)}"


async def exchange_code(
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Trade the authorization code for tokens at the provider's token endpoint.

    ``redirect_uri`` must be byte-identical to the one sent on /authorize -- the
    provider compares them and rejects the exchange otherwise. It comes from the
    signed state rather than the request, so a caller cannot swap it here.
    """
    doc = await discover(client)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri or settings.oidc_redirect_url,
        "client_id": settings.oidc_client_id,
        "code_verifier": code_verifier,
    }
    if settings.oidc_client_secret:
        data["client_secret"] = settings.oidc_client_secret

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
    try:
        response = await client.post(doc.token_endpoint, data=data)
    except Exception as exc:  # noqa: BLE001
        raise OIDCError(f"token exchange failed: {exc}") from exc
    finally:
        if owns_client:
            await client.aclose()

    if response.status_code >= 400:
        # The provider's body can contain the code itself; log the status only.
        log.warning("OIDC token exchange rejected with %s", response.status_code)
        raise OIDCError("the identity provider rejected the sign-in")
    payload = response.json()
    if "id_token" not in payload:
        raise OIDCError("the identity provider returned no ID token")
    return payload


async def verify_id_token(id_token: str, *, nonce: str) -> OIDCClaims:
    """Verify signature, issuer, audience, expiry, nonce; return the claims."""
    doc = await discover()
    signing_key = await _signing_key(doc.jwks_uri, id_token)

    try:
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=_ALLOWED_ALGORITHMS,
            audience=settings.oidc_client_id,
            issuer=doc.issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise OIDCError(f"the ID token is not valid: {exc}") from exc

    if claims.get("nonce") != nonce:
        # Either a replay or a crossed session. Both are fatal.
        raise OIDCError("ID token nonce mismatch")

    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise OIDCError("the identity provider did not return an email address")
    # Absent means "not asserted", which is not the same as verified.
    if claims.get("email_verified") is not True:
        raise OIDCError("the identity provider has not verified this email address")

    allowed = [d.lower().lstrip("@") for d in settings.oidc_allowed_email_domains]
    if allowed and email.rpartition("@")[2] not in allowed:
        raise OIDCError("this email domain is not permitted to sign in")

    groups = claims.get("groups") or claims.get("roles") or []
    if isinstance(groups, str):
        groups = [groups]

    return OIDCClaims(
        subject=f"{doc.issuer}|{claims['sub']}",
        email=email,
        full_name=str(claims.get("name") or claims.get("preferred_username") or ""),
        groups=[str(g) for g in groups],
    )
