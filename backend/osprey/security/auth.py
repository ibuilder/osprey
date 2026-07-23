"""JWT issuing/verification and the request Principal."""

from __future__ import annotations

from datetime import timedelta

import jwt
from pydantic import BaseModel

from ..config import settings
from ..models import Role, utcnow


class Principal(BaseModel):
    user_id: str
    org_id: str
    role: Role
    email: str = ""


def create_access_token(principal: Principal, *, ttl_minutes: int | None = None) -> str:
    ttl = ttl_minutes if ttl_minutes is not None else settings.access_token_ttl_minutes
    now = utcnow()
    payload = {
        "sub": principal.user_id,
        "org": principal.org_id,
        "role": principal.role.value,
        "email": principal.email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ttl)).timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> Principal:
    data = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    return Principal(
        user_id=data["sub"],
        org_id=data["org"],
        role=Role(data["role"]),
        email=data.get("email", ""),
    )
