"""JWT issuing/verification, the request Principal, and revocable refresh sessions.

Access tokens are short-lived JWTs. They are *not* looked up in the database on
every request -- that would put a query in front of every call -- so revocation
works through two cheap mechanisms instead:

* ``ver`` carries the user's ``token_version``. Bumping the column invalidates
  every token that user holds, which is what disabling, demoting, or forcing a
  password reset does.
* Refresh tokens are opaque, stored hashed, and rotated on every use, so a
  session can be ended individually without touching anyone else's.

The ``ver`` check costs one indexed primary-key read per request, which is
already in the session's identity map for most handlers.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

import jwt
from pydantic import BaseModel

from ..config import settings
from ..models import Role, utcnow

#: Length of the opaque refresh secret, in bytes, before base64url encoding.
_REFRESH_BYTES = 32


class Principal(BaseModel):
    user_id: str
    org_id: str
    role: Role
    email: str = ""
    #: JWT id -- distinct per token, so an audit record can name the credential
    #: that performed an action without storing the token itself.
    jti: str = ""
    #: The user's ``token_version`` at issue time.
    token_version: int = 0


def create_access_token(principal: Principal, *, ttl_minutes: int | None = None) -> str:
    ttl = ttl_minutes if ttl_minutes is not None else settings.access_token_ttl_minutes
    now = utcnow()
    payload = {
        "sub": principal.user_id,
        "org": principal.org_id,
        "role": principal.role.value,
        "email": principal.email,
        "ver": principal.token_version,
        "jti": principal.jti or secrets.token_urlsafe(16),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ttl)).timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> Principal:
    """Verify and unpack an access token. Raises on any failure.

    ``iss``/``aud`` are verified so a token minted by a *different* Osprey
    deployment that happens to share a secret (a copied ``.env``, a restored
    backup) cannot be replayed here.
    """
    data = jwt.decode(
        token,
        settings.secret_key,
        algorithms=[settings.jwt_algorithm],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
        options={"require": ["exp", "sub", "org"]},
    )
    return Principal(
        user_id=data["sub"],
        org_id=data["org"],
        role=Role(data["role"]),
        email=data.get("email", ""),
        jti=data.get("jti", ""),
        token_version=int(data.get("ver", 0)),
    )


# --------------------------------------------------------------------------- #
# Refresh tokens
# --------------------------------------------------------------------------- #
def new_refresh_secret() -> str:
    """A fresh opaque refresh token. Returned to the client exactly once."""
    return secrets.token_urlsafe(_REFRESH_BYTES)


def hash_secret(secret: str) -> str:
    """Storage form for an opaque credential.

    Plain SHA-256, deliberately: unlike a password these are 256 bits of CSPRNG
    output, so there is nothing to brute-force and a slow KDF would only add
    latency to every refresh.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def refresh_expiry(now: datetime | None = None) -> datetime:
    return (now or utcnow()) + timedelta(days=settings.refresh_token_ttl_days)
