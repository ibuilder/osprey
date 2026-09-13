"""OAuth2 authorization-code flow (with PKCE) — driven by the desktop app.

The user authenticates in their own system browser; the desktop app runs a
loopback redirect (``http://127.0.0.1:<port>/callback``), captures the ``code``,
and relays it to the backend, which performs the confidential token exchange and
seals the tokens. Provider tokens never touch the AI/MCP layer and are never
persisted in plaintext.

State integrity: the ``state`` is a short-lived signed JWT that also carries the
PKCE ``code_verifier``, so no server-side pending-auth storage is required and a
forged/way-late callback is rejected.
"""

from __future__ import annotations

import base64
import hashlib
import os
from datetime import timedelta

import jwt
from pydantic import BaseModel

from ..config import settings
from ..models import utcnow

_STATE_TTL = timedelta(minutes=10)


class OAuthSpec(BaseModel):
    """Per-connector OAuth endpoints + parameters."""

    authorize_endpoint: str
    token_endpoint: str
    scopes: list[str]
    use_pkce: bool = True
    extra_authorize_params: dict[str, str] = {}


class AuthorizeRequest(BaseModel):
    project_id: str
    source_type: str
    redirect_uri: str  # desktop loopback, e.g. http://127.0.0.1:53682/callback
    account_ref: str = ""


class AuthorizeChallenge(BaseModel):
    authorize_url: str
    state: str


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for S256 PKCE."""
    verifier = _b64url(os.urandom(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


class RedirectUriError(ValueError):
    """A client-supplied redirect URI is not acceptable."""


def is_loopback_redirect(uri: str) -> bool:
    """True for the redirect shape a native app is allowed to ask for.

    RFC 8252: a desktop app cannot use a fixed redirect, so it binds an ephemeral
    loopback port and registers ``http://127.0.0.1`` with a wildcard port. Plain
    HTTP is correct here precisely because the traffic never leaves the machine.

    ``localhost`` is deliberately rejected. It resolves through the host's name
    resolution, which another process can influence; the literal loopback
    addresses cannot be redirected. Credentials and fragments are rejected too --
    neither has any business in a redirect target, and both are places to smuggle
    something past a naive comparison.
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(uri)
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "::1"}
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
    )


def assert_loopback_redirect(uri: str) -> str:
    """Return ``uri`` if a native client may use it; raise otherwise.

    Echoing a caller-supplied URL into a provider's authorize request unchecked is
    how authorization codes get delivered to somebody else. That providers also
    enforce a registered redirect is a second lock, not a reason to leave this one
    open.
    """
    if not is_loopback_redirect(uri):
        raise RedirectUriError(
            "redirect_uri must be a loopback address (http://127.0.0.1:<port>/...)"
        )
    return uri


def sign_state(payload: dict) -> str:
    now = utcnow()
    body = {**payload, "iat": int(now.timestamp()), "exp": int((now + _STATE_TTL).timestamp())}
    return jwt.encode(body, settings.secret_key, algorithm=settings.jwt_algorithm)


def verify_state(state: str) -> dict:
    return jwt.decode(state, settings.secret_key, algorithms=[settings.jwt_algorithm])


def build_authorize_url(
    spec: OAuthSpec,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str | None,
) -> str:
    from urllib.parse import urlencode

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(spec.scopes),
        "state": state,
        **spec.extra_authorize_params,
    }
    if spec.use_pkce and code_challenge:
        params |= {"code_challenge": code_challenge, "code_challenge_method": "S256"}
    return f"{spec.authorize_endpoint}?{urlencode(params)}"
