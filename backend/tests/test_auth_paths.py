"""Auth + authorization edge cases.

These are the branches that decide who gets in, so they are worth covering
explicitly rather than only via the happy path in ``test_api``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
from sqlalchemy import select

from osprey.config import settings
from osprey.models import Membership, Org, Role, User
from osprey.security.auth import Principal, create_access_token, decode_token
from osprey.security.passwords import hash_password


async def test_register_rejects_duplicate_email(client):
    body = {"email": "dupe@example.com", "password": "password123", "org_name": "A"}
    first = await client.post("/auth/register", json=body)
    assert first.status_code == 201
    second = await client.post("/auth/register", json=body)
    assert second.status_code == 409


async def test_register_is_case_insensitive_on_email(client):
    await client.post(
        "/auth/register",
        json={"email": "Mixed@Example.com", "password": "password123", "org_name": "A"},
    )
    dupe = await client.post(
        "/auth/register",
        json={"email": "mixed@example.com", "password": "password123", "org_name": "B"},
    )
    assert dupe.status_code == 409
    # ...and login works regardless of the case supplied.
    login = await client.post(
        "/auth/login", json={"email": "MIXED@example.com", "password": "password123"}
    )
    assert login.status_code == 200


async def test_register_rejects_short_password(client):
    resp = await client.post(
        "/auth/register", json={"email": "x@y.com", "password": "short", "org_name": "A"}
    )
    assert resp.status_code == 422  # schema enforces min_length


async def test_login_unknown_email_is_401(client):
    resp = await client.post("/auth/login", json={"email": "nobody@x.com", "password": "whatever1"})
    assert resp.status_code == 401


async def test_login_disabled_user_is_403(client, session):
    await client.post(
        "/auth/register",
        json={"email": "off@example.com", "password": "password123", "org_name": "A"},
    )
    user = (await session.execute(select(User).where(User.email == "off@example.com"))).scalar_one()
    user.is_active = False
    session.add(user)
    await session.commit()

    resp = await client.post(
        "/auth/login", json={"email": "off@example.com", "password": "password123"}
    )
    assert resp.status_code == 403


async def test_login_without_membership_is_403(client, session):
    """A user with no org membership cannot obtain a token."""
    user = User(email="orphan@example.com", password_hash=hash_password("password123"))
    session.add(user)
    await session.commit()

    resp = await client.post(
        "/auth/login", json={"email": "orphan@example.com", "password": "password123"}
    )
    assert resp.status_code == 403


async def test_login_returns_the_membership_role(client, session):
    """The token reflects the user's actual role, not a default."""
    org = Org(name="RoleCo")
    session.add(org)
    await session.flush()
    user = User(email="pm@example.com", password_hash=hash_password("password123"))
    session.add(user)
    await session.flush()
    session.add(Membership(org_id=org.id, user_id=user.id, role=Role.pm))
    await session.commit()

    resp = await client.post(
        "/auth/login", json={"email": "pm@example.com", "password": "password123"}
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "pm"
    assert decode_token(resp.json()["access_token"]).role is Role.pm


async def test_malformed_and_expired_tokens_are_rejected(client):
    # Not a bearer scheme.
    assert (
        await client.get("/projects", headers={"Authorization": "Basic abc"})
    ).status_code == 401
    # Garbage token.
    assert (
        await client.get("/projects", headers={"Authorization": "Bearer not-a-jwt"})
    ).status_code == 401
    # Expired token.
    expired = create_access_token(
        Principal(user_id="u", org_id="o", role=Role.owner, email="e@x.com"), ttl_minutes=-1
    )
    assert (
        await client.get("/projects", headers={"Authorization": f"Bearer {expired}"})
    ).status_code == 401


async def test_token_signed_with_another_key_is_rejected(client):
    """A token forged with a different secret must not authenticate."""
    forged = jwt.encode(
        {
            "sub": "u",
            "org": "o",
            "role": "owner",
            "email": "e@x.com",
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
        "a-completely-different-signing-key",
        algorithm=settings.jwt_algorithm,
    )
    resp = await client.get("/projects", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401


async def test_cross_org_project_access_is_404(client):
    """Org A's token must not reach Org B's project."""
    a = (
        await client.post(
            "/auth/register",
            json={"email": "a@a.com", "password": "password123", "org_name": "A"},
        )
    ).json()
    b = (
        await client.post(
            "/auth/register",
            json={"email": "b@b.com", "password": "password123", "org_name": "B"},
        )
    ).json()

    b_project = (
        await client.post(
            "/projects",
            json={"name": "B secret"},
            headers={"Authorization": f"Bearer {b['access_token']}"},
        )
    ).json()["id"]

    leaked = await client.get(
        f"/projects/{b_project}", headers={"Authorization": f"Bearer {a['access_token']}"}
    )
    assert leaked.status_code == 404  # not 403 — don't confirm existence
