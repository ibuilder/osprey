"""Org membership: invites, roles, the last-owner rule, and privilege escalation."""

from __future__ import annotations

GOOD = "Sup3rSecret!pass"


async def _invite(owner_client, email: str, role: str = "pm") -> dict:
    resp = await owner_client.post("/orgs/current/invites", json={"email": email, "role": role})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _join(client, token: str, name: str = "New") -> dict:
    resp = await client.post(
        "/invites/accept", json={"token": token, "password": GOOD, "full_name": name}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# The core flow that did not exist before
# --------------------------------------------------------------------------- #
async def test_a_second_user_can_join_an_existing_org(auth_client, client):
    owner_client, owner = auth_client
    invite = await _invite(owner_client, "colleague@example.com", "pm")
    joined = await _join(client, invite["token"])

    assert joined["org_id"] == owner["org_id"]
    assert joined["role"] == "pm"

    members = (await owner_client.get("/orgs/current/members")).json()
    assert {m["email"] for m in members} == {"owner@example.com", "colleague@example.com"}


async def test_the_invite_token_is_returned_once_and_never_again(auth_client):
    owner_client, _ = auth_client
    invite = await _invite(owner_client, "once@example.com")
    assert invite["token"]

    listed = (await owner_client.get("/orgs/current/invites")).json()
    assert all(i["token"] is None for i in listed)


async def test_an_invite_cannot_be_redeemed_twice(auth_client, client):
    owner_client, _ = auth_client
    invite = await _invite(owner_client, "twice@example.com")
    await _join(client, invite["token"])

    again = await client.post("/invites/accept", json={"token": invite["token"], "password": GOOD})
    assert again.status_code == 400


async def test_a_revoked_invite_cannot_be_redeemed(auth_client, client):
    owner_client, _ = auth_client
    invite = await _invite(owner_client, "revoked@example.com")
    assert (await owner_client.delete(f"/orgs/current/invites/{invite['id']}")).status_code == 204

    resp = await client.post("/invites/accept", json={"token": invite["token"], "password": GOOD})
    assert resp.status_code == 400


async def test_expired_and_unknown_invites_are_indistinguishable(auth_client, client, session):
    """Different messages would tell a token-guesser when they found a real one."""
    from datetime import timedelta

    from sqlalchemy import select

    from osprey.models import Invite, utcnow

    owner_client, _ = auth_client
    invite = await _invite(owner_client, "stale@example.com")
    row = (
        await session.execute(select(Invite).where(Invite.email == "stale@example.com"))
    ).scalar_one()
    row.expires_at = utcnow() - timedelta(days=1)
    session.add(row)
    await session.commit()

    expired = await client.post(
        "/invites/accept", json={"token": invite["token"], "password": GOOD}
    )
    unknown = await client.post("/invites/accept", json={"token": "z" * 40, "password": GOOD})
    assert expired.status_code == unknown.status_code == 400
    assert expired.json()["detail"] == unknown.json()["detail"]


async def test_invite_enforces_the_password_policy(auth_client, client):
    owner_client, _ = auth_client
    invite = await _invite(owner_client, "weakjoin@example.com")
    resp = await client.post(
        "/invites/accept", json={"token": invite["token"], "password": "lowercaseonlyhere"}
    )
    assert resp.status_code == 422


async def test_inviting_an_existing_member_conflicts(auth_client):
    owner_client, _ = auth_client
    resp = await owner_client.post(
        "/orgs/current/invites", json={"email": "owner@example.com", "role": "pm"}
    )
    assert resp.status_code == 409


# --------------------------------------------------------------------------- #
# Privilege escalation
# --------------------------------------------------------------------------- #
async def test_an_admin_cannot_mint_an_owner(auth_client, client):
    owner_client, _ = auth_client
    invite = await _invite(owner_client, "admin@example.com", "admin")
    admin = await _join(client, invite["token"])
    admin_headers = {"Authorization": f"Bearer {admin['access_token']}"}

    resp = await client.post(
        "/orgs/current/invites",
        json={"email": "escalated@example.com", "role": "owner"},
        headers=admin_headers,
    )
    assert resp.status_code == 403
    assert "above your own" in resp.json()["detail"]


async def test_an_admin_cannot_promote_someone_to_owner(auth_client, client):
    owner_client, _ = auth_client
    admin = await _join(client, (await _invite(owner_client, "adm2@example.com", "admin"))["token"])
    victim = await _join(client, (await _invite(owner_client, "pm2@example.com", "pm"))["token"])

    resp = await client.put(
        f"/orgs/current/members/{victim['user_id']}/role",
        json={"role": "owner"},
        headers={"Authorization": f"Bearer {admin['access_token']}"},
    )
    assert resp.status_code == 403


async def test_a_pm_cannot_manage_members(auth_client, client):
    owner_client, _ = auth_client
    pm = await _join(client, (await _invite(owner_client, "pm3@example.com", "pm"))["token"])
    resp = await client.post(
        "/orgs/current/invites",
        json={"email": "nope@example.com", "role": "viewer"},
        headers={"Authorization": f"Bearer {pm['access_token']}"},
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# The last-owner invariant
# --------------------------------------------------------------------------- #
async def test_the_last_owner_cannot_be_demoted(auth_client):
    owner_client, owner = auth_client
    resp = await owner_client.put(
        f"/orgs/current/members/{owner['user_id']}/role", json={"role": "viewer"}
    )
    assert resp.status_code == 409
    assert "last active owner" in resp.json()["detail"]


async def test_the_last_owner_cannot_be_deactivated(auth_client):
    owner_client, owner = auth_client
    resp = await owner_client.post(f"/orgs/current/members/{owner['user_id']}/deactivate")
    assert resp.status_code == 409


async def test_an_owner_can_be_demoted_once_another_exists(auth_client, client):
    owner_client, owner = auth_client
    second = await _join(
        client, (await _invite(owner_client, "owner2@example.com", "owner"))["token"]
    )

    resp = await owner_client.put(
        f"/orgs/current/members/{owner['user_id']}/role", json={"role": "admin"}
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"

    members = (
        await client.get(
            "/orgs/current/members",
            headers={"Authorization": f"Bearer {second['access_token']}"},
        )
    ).json()
    assert sum(1 for m in members if m["role"] == "owner") == 1


async def test_a_role_change_takes_effect_on_the_next_request(auth_client, client):
    """A demotion that only applies when the token expires is not a demotion."""
    owner_client, _ = auth_client
    member = await _join(
        client, (await _invite(owner_client, "demote@example.com", "admin"))["token"]
    )
    headers = {"Authorization": f"Bearer {member['access_token']}"}
    assert (await client.get("/admin/stats", headers=headers)).status_code == 200

    await owner_client.put(
        f"/orgs/current/members/{member['user_id']}/role", json={"role": "viewer"}
    )
    # The old token now fails the token-version check, forcing a refresh.
    assert (await client.get("/admin/stats", headers=headers)).status_code == 401


async def test_removing_a_member_deletes_only_the_membership(auth_client, client, session):
    from osprey.models import User

    owner_client, _ = auth_client
    member = await _join(client, (await _invite(owner_client, "gone@example.com"))["token"])

    assert (
        await owner_client.delete(f"/orgs/current/members/{member['user_id']}")
    ).status_code == 204
    assert await session.get(User, member["user_id"]) is not None

    members = (await owner_client.get("/orgs/current/members")).json()
    assert "gone@example.com" not in {m["email"] for m in members}


async def test_you_cannot_remove_yourself(auth_client, client):
    owner_client, _ = auth_client
    second = await _join(
        client, (await _invite(owner_client, "self@example.com", "owner"))["token"]
    )
    resp = await client.delete(
        f"/orgs/current/members/{second['user_id']}",
        headers={"Authorization": f"Bearer {second['access_token']}"},
    )
    assert resp.status_code == 409


async def test_reactivation_clears_the_lockout(auth_client, client, session):
    from osprey.models import User

    owner_client, _ = auth_client
    member = await _join(client, (await _invite(owner_client, "back@example.com"))["token"])
    await owner_client.post(f"/orgs/current/members/{member['user_id']}/deactivate")
    assert (
        await owner_client.post(f"/orgs/current/members/{member['user_id']}/reactivate")
    ).status_code == 204

    user = await session.get(User, member["user_id"])
    await session.refresh(user)
    assert user.is_active
    assert user.locked_until is None
