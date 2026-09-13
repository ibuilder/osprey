"""Revocable sessions: refresh rotation, reuse detection, logout, lockout, policy."""

from __future__ import annotations

from sqlalchemy import delete, select

from osprey.config import settings
from osprey.models import Membership, RefreshToken, User
from osprey.security.passwords import PasswordPolicyError, check_policy, needs_rehash

GOOD = "Sup3rSecret!pass"


async def _register(client, email: str = "sess@example.com") -> dict:
    resp = await client.post(
        "/auth/register",
        json={"email": email, "password": GOOD, "full_name": "S", "org_name": "SessCo"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# Issue / refresh / rotate
# --------------------------------------------------------------------------- #
async def test_register_and_login_return_a_refresh_token(client):
    body = await _register(client)
    assert body["refresh_token"]
    assert body["expires_in"] == settings.access_token_ttl_minutes * 60

    login = await client.post("/auth/login", json={"email": "sess@example.com", "password": GOOD})
    assert login.json()["refresh_token"] != body["refresh_token"]


async def test_refresh_rotates_the_token(client):
    body = await _register(client)
    first = body["refresh_token"]

    refreshed = await client.post("/auth/refresh", json={"refresh_token": first})
    assert refreshed.status_code == 200
    second = refreshed.json()["refresh_token"]
    assert second != first

    # The successor works...
    assert (await client.post("/auth/refresh", json={"refresh_token": second})).status_code == 200


async def test_replaying_a_rotated_token_revokes_the_whole_family(client, session):
    """Reuse means a copy is loose; neither party can be trusted from here."""
    body = await _register(client)
    first = body["refresh_token"]
    second = (await client.post("/auth/refresh", json={"refresh_token": first})).json()[
        "refresh_token"
    ]

    replay = await client.post("/auth/refresh", json={"refresh_token": first})
    assert replay.status_code == 401
    assert "already used" in replay.json()["detail"]

    # ...and the legitimate successor is dead too.
    assert (await client.post("/auth/refresh", json={"refresh_token": second})).status_code == 401


async def test_unknown_refresh_token_is_rejected(client):
    resp = await client.post("/auth/refresh", json={"refresh_token": "x" * 40})
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Revocation
# --------------------------------------------------------------------------- #
async def test_logout_revokes_only_that_session(client):
    body = await _register(client)
    other = (
        await client.post("/auth/login", json={"email": "sess@example.com", "password": GOOD})
    ).json()["refresh_token"]

    assert (
        await client.post("/auth/logout", json={"refresh_token": body["refresh_token"]})
    ).status_code == 204
    assert (
        await client.post("/auth/refresh", json={"refresh_token": body["refresh_token"]})
    ).status_code == 401
    # The other session is untouched.
    assert (await client.post("/auth/refresh", json={"refresh_token": other})).status_code == 200


async def test_logout_all_invalidates_access_tokens_already_issued(client):
    """The whole point of token_version: revocation cannot wait for expiry."""
    body = await _register(client)
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    assert (await client.get("/projects", headers=headers)).status_code == 200

    assert (await client.post("/auth/logout-all", headers=headers)).status_code == 204

    stale = await client.get("/projects", headers=headers)
    assert stale.status_code == 401
    assert "revoked" in stale.json()["detail"]


async def test_deactivating_a_user_kills_their_live_token(client, session, auth_client):
    owner_client, owner = auth_client
    invite = (
        await owner_client.post(
            "/orgs/current/invites", json={"email": "temp@example.com", "role": "pm"}
        )
    ).json()
    joined = (
        await client.post(
            "/invites/accept", json={"token": invite["token"], "password": GOOD, "full_name": "T"}
        )
    ).json()
    headers = {"Authorization": f"Bearer {joined['access_token']}"}
    assert (await client.get("/projects", headers=headers)).status_code == 200

    await owner_client.post(f"/orgs/current/members/{joined['user_id']}/deactivate")
    assert (await client.get("/projects", headers=headers)).status_code == 401


async def test_sessions_can_be_listed_and_revoked_individually(client):
    body = await _register(client)
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    await client.post("/auth/login", json={"email": "sess@example.com", "password": GOOD})

    listed = (await client.get("/auth/sessions", headers=headers)).json()
    assert len(listed) >= 2

    assert (
        await client.delete(f"/auth/sessions/{listed[0]['id']}", headers=headers)
    ).status_code == 204
    remaining = (await client.get("/auth/sessions", headers=headers)).json()
    assert len(remaining) == len(listed) - 1


async def test_cannot_revoke_someone_elses_session(client):
    a = await _register(client, "a-sess@example.com")
    b = await _register(client, "b-sess@example.com")
    b_session = (
        await client.get("/auth/sessions", headers={"Authorization": f"Bearer {b['access_token']}"})
    ).json()[0]

    resp = await client.delete(
        f"/auth/sessions/{b_session['id']}",
        headers={"Authorization": f"Bearer {a['access_token']}"},
    )
    # 404, not 403 -- the caller has no business learning the id exists.
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Password change
# --------------------------------------------------------------------------- #
async def test_password_change_revokes_every_other_session(client):
    body = await _register(client)
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    resp = await client.post(
        "/auth/password",
        json={"current_password": GOOD, "new_password": "An0ther!GoodPass"},
        headers=headers,
    )
    assert resp.status_code == 204
    assert (
        await client.post("/auth/refresh", json={"refresh_token": body["refresh_token"]})
    ).status_code == 401
    assert (
        await client.post(
            "/auth/login", json={"email": "sess@example.com", "password": "An0ther!GoodPass"}
        )
    ).status_code == 200


async def test_password_change_requires_the_current_one(client):
    body = await _register(client)
    resp = await client.post(
        "/auth/password",
        json={"current_password": "Wr0ng!password", "new_password": "An0ther!GoodPass"},
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Brute force
# --------------------------------------------------------------------------- #
async def test_repeated_failures_lock_the_account(client, monkeypatch, session):
    monkeypatch.setattr(settings, "login_max_failures", 3)
    monkeypatch.setattr(settings, "rate_limit_login_per_minute", 1000)
    monkeypatch.setattr(settings, "rate_limit_login_per_hour", 1000)
    await _register(client, "lock@example.com")

    for _ in range(3):
        bad = await client.post(
            "/auth/login", json={"email": "lock@example.com", "password": "Wr0ng!password"}
        )
        assert bad.status_code == 401

    # Even the *correct* password is refused while the lock holds.
    locked = await client.post("/auth/login", json={"email": "lock@example.com", "password": GOOD})
    assert locked.status_code == 429
    assert "Retry-After" in locked.headers


async def test_credential_endpoint_is_rate_limited(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_login_per_minute", 3)
    codes = [
        (
            await client.post(
                "/auth/login", json={"email": "nobody@example.com", "password": "Wr0ng!password"}
            )
        ).status_code
        for _ in range(6)
    ]
    assert 429 in codes


async def test_login_does_not_reveal_whether_an_account_exists(client):
    await _register(client, "known@example.com")
    unknown = await client.post(
        "/auth/login", json={"email": "unknown@example.com", "password": "Wr0ng!password"}
    )
    wrong = await client.post(
        "/auth/login", json={"email": "known@example.com", "password": "Wr0ng!password"}
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
async def test_weak_passwords_are_refused_at_registration(client):
    resp = await client.post(
        "/auth/register",
        json={"email": "weak@example.com", "password": "alllowercaseletters", "org_name": "W"},
    )
    assert resp.status_code == 422
    assert "mix at least" in resp.json()["detail"]


def test_policy_rejects_a_password_containing_the_email():
    try:
        check_policy("Marcus!Marcus99", email="marcus@example.com")
    except PasswordPolicyError as exc:
        assert "email address" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected the policy to reject it")


def test_policy_caps_length_to_bound_kdf_cost():
    try:
        check_policy("A1!" + "x" * 2000)
    except PasswordPolicyError as exc:
        assert "at most" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected a length cap")


def test_needs_rehash_detects_weaker_parameters():
    assert needs_rehash("pbkdf2_sha256$1$c2FsdA$aGFzaA")
    assert needs_rehash("bcrypt$12$whatever")
    assert not needs_rehash(f"pbkdf2_sha256${settings.password_hash_iterations}$c2FsdA$aGFzaA")


async def test_refresh_token_is_never_stored_in_plaintext(client, session):
    body = await _register(client, "hashed@example.com")
    rows = (await session.execute(select(RefreshToken))).scalars().all()
    assert rows
    assert all(row.token_hash != body["refresh_token"] for row in rows)
    assert all(len(row.token_hash) == 64 for row in rows)  # sha256 hex


async def test_token_for_a_deleted_user_is_rejected(client, session):
    """A signature-valid token is not sufficient; the subject must still exist."""
    body = await _register(client, "ghost@example.com")
    # Dependents first. Postgres enforces these foreign keys (SQLite, by default,
    # does not), so deleting the user alone fails there with a violation on
    # membership -- the test has to remove the user the way a real deletion must.
    await session.execute(delete(RefreshToken).where(RefreshToken.user_id == body["user_id"]))
    await session.execute(delete(Membership).where(Membership.user_id == body["user_id"]))
    user = await session.get(User, body["user_id"])
    await session.delete(user)
    await session.commit()

    resp = await client.get(
        "/projects", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert resp.status_code == 401
