"""SCIM 2.0 provisioning — token auth, lifecycle, role ceiling, error shape."""

from __future__ import annotations

import pytest

from osprey.config import settings

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


@pytest.fixture(autouse=True)
def _scim_on(monkeypatch):
    monkeypatch.setattr(settings, "scim_enabled", True)


async def _token(owner_client, max_role: str = "pm") -> dict:
    resp = await owner_client.post(
        "/orgs/current/scim-tokens", json={"name": "Okta", "max_role": max_role}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create(client, token: str, email: str, **extra) -> dict:
    body = {"schemas": [USER_SCHEMA], "userName": email, "active": True, **extra}
    resp = await client.post("/scim/v2/Users", json=body, headers=_hdr(token))
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# Token authentication
# --------------------------------------------------------------------------- #
async def test_scim_is_404_when_disabled(client, auth_client, monkeypatch):
    owner_client, _ = auth_client
    token = (await _token(owner_client))["token"]
    monkeypatch.setattr(settings, "scim_enabled", False)
    resp = await client.get("/scim/v2/Users", headers=_hdr(token))
    assert resp.status_code == 404


async def test_unauthenticated_scim_is_rejected_in_scim_error_shape(client):
    resp = await client.get("/scim/v2/Users")
    assert resp.status_code == 401
    body = resp.json()
    # A connector parses this shape specifically; the generic {"detail": ...}
    # wrapper would not be understood.
    assert body["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
    assert body["status"] == "401"


async def test_a_revoked_token_stops_working(client, auth_client):
    owner_client, _ = auth_client
    token = await _token(owner_client)
    assert (await client.get("/scim/v2/Users", headers=_hdr(token["token"]))).status_code == 200

    await owner_client.delete(f"/orgs/current/scim-tokens/{token['id']}")
    assert (await client.get("/scim/v2/Users", headers=_hdr(token["token"]))).status_code == 401


async def test_a_scim_token_may_not_be_allowed_to_create_owners(auth_client):
    owner_client, _ = auth_client
    resp = await owner_client.post(
        "/orgs/current/scim-tokens", json={"name": "bad", "max_role": "owner"}
    )
    assert resp.status_code == 422


async def test_only_an_owner_can_mint_a_scim_token(auth_client, client):
    owner_client, _ = auth_client
    invite = (
        await owner_client.post(
            "/orgs/current/invites", json={"email": "adm@example.com", "role": "admin"}
        )
    ).json()
    admin = (
        await client.post(
            "/invites/accept", json={"token": invite["token"], "password": "Sup3rSecret!pass"}
        )
    ).json()
    resp = await client.post(
        "/orgs/current/scim-tokens",
        json={"name": "x", "max_role": "pm"},
        headers=_hdr(admin["access_token"]),
    )
    assert resp.status_code == 403


async def test_the_token_secret_is_shown_once(auth_client):
    owner_client, _ = auth_client
    created = await _token(owner_client)
    assert created["token"].startswith("osp_scim_")

    listed = (await owner_client.get("/orgs/current/scim-tokens")).json()
    assert all(t["token"] is None for t in listed)


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
async def test_create_list_and_get_a_user(client, auth_client):
    owner_client, _ = auth_client
    token = (await _token(owner_client))["token"]

    created = await _create(client, token, "provisioned@example.com", externalId="okta-1")
    assert created["userName"] == "provisioned@example.com"
    assert created["active"] is True
    assert created["externalId"] == "okta-1"
    assert created["meta"]["resourceType"] == "User"

    fetched = await client.get(f"/scim/v2/Users/{created['id']}", headers=_hdr(token))
    assert fetched.json()["id"] == created["id"]

    listing = (await client.get("/scim/v2/Users", headers=_hdr(token))).json()
    assert listing["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:ListResponse"]
    assert listing["totalResults"] == 2  # the owner plus the provisioned user


async def test_username_filter_is_supported(client, auth_client):
    """The one filter every IdP actually sends."""
    owner_client, _ = auth_client
    token = (await _token(owner_client))["token"]
    await _create(client, token, "findme@example.com")

    resp = await client.get(
        '/scim/v2/Users?filter=userName eq "findme@example.com"', headers=_hdr(token)
    )
    body = resp.json()
    assert body["totalResults"] == 1
    assert body["Resources"][0]["userName"] == "findme@example.com"


async def test_duplicate_creation_is_409_with_uniqueness(client, auth_client):
    owner_client, _ = auth_client
    token = (await _token(owner_client))["token"]
    await _create(client, token, "dupe@example.com")

    resp = await client.post(
        "/scim/v2/Users",
        json={"schemas": [USER_SCHEMA], "userName": "dupe@example.com"},
        headers=_hdr(token),
    )
    assert resp.status_code == 409
    assert resp.json()["scimType"] == "uniqueness"


async def test_patch_deactivates_a_user_and_cuts_their_sessions(client, auth_client):
    """Entra deactivates only through PATCH, with no path on the operation."""
    owner_client, _ = auth_client
    token = (await _token(owner_client))["token"]
    created = await _create(client, token, "deact@example.com")

    resp = await client.patch(
        f"/scim/v2/Users/{created['id']}",
        json={
            "schemas": [PATCH_SCHEMA],
            "Operations": [{"op": "replace", "value": {"active": False}}],
        },
        headers=_hdr(token),
    )
    assert resp.status_code == 200
    assert resp.json()["active"] is False


async def test_patch_accepts_the_pathed_form_too(client, auth_client):
    owner_client, _ = auth_client
    token = (await _token(owner_client))["token"]
    created = await _create(client, token, "pathed@example.com")

    resp = await client.patch(
        f"/scim/v2/Users/{created['id']}",
        json={
            "schemas": [PATCH_SCHEMA],
            "Operations": [{"op": "replace", "path": "active", "value": False}],
        },
        headers=_hdr(token),
    )
    assert resp.json()["active"] is False


async def test_patch_treats_the_string_false_as_false(client, auth_client):
    """Some connectors send "False"; the truthy string would enable the account."""
    owner_client, _ = auth_client
    token = (await _token(owner_client))["token"]
    created = await _create(client, token, "strfalse@example.com")

    resp = await client.patch(
        f"/scim/v2/Users/{created['id']}",
        json={
            "schemas": [PATCH_SCHEMA],
            "Operations": [{"op": "replace", "path": "active", "value": "False"}],
        },
        headers=_hdr(token),
    )
    assert resp.json()["active"] is False


async def test_patch_rejects_a_non_patchop_document(client, auth_client):
    owner_client, _ = auth_client
    token = (await _token(owner_client))["token"]
    created = await _create(client, token, "notpatch@example.com")

    resp = await client.patch(
        f"/scim/v2/Users/{created['id']}", json={"active": False}, headers=_hdr(token)
    )
    assert resp.status_code == 400
    assert resp.json()["scimType"] == "invalidSyntax"


async def test_delete_is_a_soft_deprovision(client, auth_client, session):
    """The audit trail must still resolve who did what after a deprovision."""
    from osprey.models import User

    owner_client, _ = auth_client
    token = (await _token(owner_client))["token"]
    created = await _create(client, token, "softdel@example.com")

    assert (
        await client.delete(f"/scim/v2/Users/{created['id']}", headers=_hdr(token))
    ).status_code == 204

    user = await session.get(User, created["id"])
    assert user is not None
    await session.refresh(user)
    assert user.is_active is False


# --------------------------------------------------------------------------- #
# Role ceiling
# --------------------------------------------------------------------------- #
async def test_a_token_cannot_grant_above_its_ceiling(client, auth_client):
    owner_client, _ = auth_client
    token = (await _token(owner_client, max_role="viewer"))["token"]

    resp = await client.post(
        "/scim/v2/Users",
        json={
            "schemas": [USER_SCHEMA],
            "userName": "climber@example.com",
            "roles": [{"value": "admin", "primary": True}],
        },
        headers=_hdr(token),
    )
    assert resp.status_code == 403
    assert resp.json()["scimType"] == "mutability"


async def test_an_unmappable_idp_role_falls_back_to_the_ceiling(client, auth_client):
    owner_client, _ = auth_client
    token = (await _token(owner_client, max_role="viewer"))["token"]
    created = await _create(
        client, token, "weird@example.com", roles=[{"value": "Regional Sales Lead"}]
    )
    assert created["roles"][0]["value"] == "viewer"


async def test_role_defaults_to_the_ceiling_when_absent(client, auth_client):
    owner_client, _ = auth_client
    token = (await _token(owner_client, max_role="pm"))["token"]
    created = await _create(client, token, "norole@example.com")
    assert created["roles"][0]["value"] == "pm"


# --------------------------------------------------------------------------- #
# Discovery / unsupported
# --------------------------------------------------------------------------- #
async def test_service_provider_config_is_served(client, auth_client):
    owner_client, _ = auth_client
    token = (await _token(owner_client))["token"]
    body = (await client.get("/scim/v2/ServiceProviderConfig", headers=_hdr(token))).json()
    assert body["patch"]["supported"] is True
    assert body["bulk"]["supported"] is False


async def test_groups_answers_501_rather_than_404(client, auth_client):
    """404 would read as "wrong URL"; 501 says the endpoint exists and is not built."""
    owner_client, _ = auth_client
    token = (await _token(owner_client))["token"]
    resp = await client.get("/scim/v2/Groups", headers=_hdr(token))
    assert resp.status_code == 501


async def test_scim_cannot_reach_another_tenant(client, auth_client):
    """A token is scoped to one org; another org's user must be invisible."""
    owner_client, _ = auth_client
    token = (await _token(owner_client))["token"]

    other = await client.post(
        "/auth/register",
        json={
            "email": "other-org@example.com",
            "password": "Sup3rSecret!pass",
            "org_name": "Other Co",
        },
    )
    other_user_id = other.json()["user_id"]

    resp = await client.get(f"/scim/v2/Users/{other_user_id}", headers=_hdr(token))
    assert resp.status_code == 404
