"""OIDC single sign-on — against a locally-generated provider, never a live IdP.

The provider is a real RSA key plus a real JWKS document served through respx, so
the signature path, the ``kid`` lookup, and every claim check run for real. Only
the network is faked.
"""

from __future__ import annotations

import json
import time

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa

from osprey.config import settings
from osprey.security import oidc

ISSUER = "https://idp.example.com"
CLIENT_ID = "osprey-test-client"
KID = "test-key-1"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks() -> dict:
    """The issuer's public key set, in the shape a provider publishes."""
    from jwt.algorithms import RSAAlgorithm

    jwk = json.loads(RSAAlgorithm.to_jwk(_KEY.public_key()))
    jwk.update(kid=KID, use="sig", alg="RS256")
    return {"keys": [jwk]}


def _id_token(**overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": "idp-subject-0001",
        "aud": CLIENT_ID,
        "iat": now,
        "exp": now + 300,
        "email": "sso.user@example.com",
        "email_verified": True,
        "name": "Sso User",
    }
    claims.update(overrides)
    return jwt.encode(claims, _KEY, algorithm="RS256", headers={"kid": KID})


@pytest.fixture
def sso_enabled(monkeypatch):
    monkeypatch.setattr(settings, "oidc_enabled", True)
    monkeypatch.setattr(settings, "oidc_issuer", ISSUER)
    monkeypatch.setattr(settings, "oidc_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "oidc_client_secret", "shh")
    monkeypatch.setattr(settings, "oidc_redirect_url", "https://osprey.test/auth/sso/callback")
    monkeypatch.setattr(settings, "oidc_allowed_email_domains", [])
    monkeypatch.setattr(settings, "oidc_auto_provision", False)
    oidc.clear_discovery_cache()
    yield
    oidc.clear_discovery_cache()


def _mock_provider(
    mock,
    state_box: dict,
    *,
    claims: dict | None = None,
    id_token: str | None = None,
    token_status: int = 200,
):
    """Stand up a provider that behaves like a real one.

    In particular it *echoes the nonce* we sent on /authorize back in the ID
    token, which is what the flow requires. The token endpoint therefore has to
    mint its response lazily, after /auth/sso/start has produced a state.
    """
    mock.get(f"{ISSUER}/.well-known/openid-configuration").mock(
        return_value=httpx.Response(
            200,
            json={
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": f"{ISSUER}/token",
                "jwks_uri": f"{ISSUER}/jwks",
            },
        )
    )
    mock.get(f"{ISSUER}/jwks").mock(return_value=httpx.Response(200, json=_jwks()))

    def _token_response(request):
        body = (
            id_token
            if id_token is not None
            else _id_token(nonce=state_box.get("nonce"), **(claims or {}))
        )
        return httpx.Response(token_status, json={"id_token": body, "token_type": "Bearer"})

    mock.post(f"{ISSUER}/token").mock(side_effect=_token_response)


async def _run_flow(client, **provider_kwargs) -> httpx.Response:
    """Drive start -> callback against the mock provider."""
    state_box: dict = {}
    with respx.mock(assert_all_called=False) as mock:
        _mock_provider(mock, state_box, **provider_kwargs)
        start = await client.post("/auth/sso/start")
        if start.status_code != 200:
            return start
        state = start.json()["state"]
        # The nonce rides inside our own signed state; read it back the way the
        # provider would have received it on the authorize URL.
        state_box["nonce"] = jwt.decode(state, options={"verify_signature": False})["nonce"]
        return await client.post("/auth/sso/callback", json={"code": "auth-code", "state": state})


# --------------------------------------------------------------------------- #
# Config discovery
# --------------------------------------------------------------------------- #
async def test_sso_config_reports_disabled_by_default(client):
    body = (await client.get("/auth/sso/config")).json()
    assert body["enabled"] is False


async def test_sso_config_reports_enabled(client, sso_enabled):
    body = (await client.get("/auth/sso/config")).json()
    assert body["enabled"] is True
    assert body["issuer"] == ISSUER


async def test_start_is_503_when_sso_is_off(client):
    assert (await client.post("/auth/sso/start")).status_code == 503


# --------------------------------------------------------------------------- #
# The flow
# --------------------------------------------------------------------------- #
async def test_start_returns_a_provider_url_with_pkce(client, sso_enabled):
    with respx.mock(assert_all_called=False) as mock:
        _mock_provider(mock, {})
        body = (await client.post("/auth/sso/start")).json()

    assert body["authorize_url"].startswith(f"{ISSUER}/authorize?")
    assert "code_challenge_method=S256" in body["authorize_url"]
    assert "response_type=code" in body["authorize_url"]
    assert body["state"]


async def test_an_invited_user_can_sign_in_through_sso(client, sso_enabled, auth_client):
    """Provisioning is off, so the account must already exist -- via invite."""
    owner_client, owner = auth_client
    invite = (
        await owner_client.post(
            "/orgs/current/invites", json={"email": "sso.user@example.com", "role": "pm"}
        )
    ).json()
    await client.post(
        "/invites/accept",
        json={"token": invite["token"], "password": "Sup3rSecret!pass", "full_name": "Sso"},
    )

    resp = await _run_flow(client)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["org_id"] == owner["org_id"]
    assert body["role"] == "pm"
    assert body["refresh_token"]


async def test_an_unknown_user_is_refused_without_auto_provision(client, sso_enabled):
    resp = await _run_flow(client)
    assert resp.status_code == 403
    assert "invite" in resp.json()["detail"]


async def test_auto_provision_creates_the_account(client, sso_enabled, auth_client, monkeypatch):
    _owner_client, owner = auth_client
    monkeypatch.setattr(settings, "oidc_auto_provision", True)
    monkeypatch.setattr(settings, "oidc_default_org_id", owner["org_id"])
    monkeypatch.setattr(settings, "oidc_default_role", "viewer")

    resp = await _run_flow(client)

    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "viewer"


async def test_auto_provisioned_users_have_no_local_password(
    client, sso_enabled, auth_client, monkeypatch, session
):
    from osprey.models import User

    _owner_client, owner = auth_client
    monkeypatch.setattr(settings, "oidc_auto_provision", True)
    monkeypatch.setattr(settings, "oidc_default_org_id", owner["org_id"])

    body = (await _run_flow(client)).json()

    user = await session.get(User, body["user_id"])
    assert user.password_hash == ""
    assert user.sso_subject == f"{ISSUER}|idp-subject-0001"

    # ...and changing a password they do not have is a clean 409, not a crash.
    resp = await client.post(
        "/auth/password",
        json={"current_password": "x" * 12, "new_password": "An0ther!GoodPass"},
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert resp.status_code == 409


# --------------------------------------------------------------------------- #
# Claim verification
# --------------------------------------------------------------------------- #
async def test_an_unverified_email_is_refused(client, sso_enabled):
    resp = await _run_flow(client, claims={"email_verified": False})
    assert resp.status_code == 401
    assert "not verified" in resp.json()["detail"]


async def test_a_missing_email_verified_claim_is_refused(client, sso_enabled):
    """Absent is not the same as verified."""
    token = jwt.encode(
        {
            "iss": ISSUER,
            "sub": "s",
            "aud": CLIENT_ID,
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            "email": "x@example.com",
        },
        _KEY,
        algorithm="RS256",
        headers={"kid": KID},
    )
    resp = await _run_flow(client, id_token=token)
    assert resp.status_code == 401


async def test_a_wrong_audience_is_refused(client, sso_enabled):
    resp = await _run_flow(client, claims={"aud": "some-other-app"})
    assert resp.status_code == 401


async def test_an_expired_id_token_is_refused(client, sso_enabled):
    past = int(time.time()) - 3600
    resp = await _run_flow(client, claims={"iat": past, "exp": past + 60})
    assert resp.status_code == 401


async def test_a_mismatched_nonce_is_refused(client, sso_enabled):
    """A replayed ID token obtained elsewhere must not open a session here."""
    resp = await _run_flow(client, claims={"nonce": "not-our-nonce"})
    assert resp.status_code == 401
    assert "nonce" in resp.json()["detail"]


async def test_a_disallowed_email_domain_is_refused(client, sso_enabled, monkeypatch):
    monkeypatch.setattr(settings, "oidc_allowed_email_domains", ["corp.example.com"])
    resp = await _run_flow(client)
    assert resp.status_code == 401
    assert "domain" in resp.json()["detail"]


async def test_a_token_signed_with_the_client_secret_is_refused(client, sso_enabled):
    """HS256 would verify against the shared secret, making any holder an issuer."""
    forged = jwt.encode(
        {
            "iss": ISSUER,
            "sub": "attacker",
            "aud": CLIENT_ID,
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            "email": "attacker@example.com",
            "email_verified": True,
        },
        "shh",
        algorithm="HS256",
        headers={"kid": KID},
    )
    resp = await _run_flow(client, id_token=forged)
    assert resp.status_code == 401


async def test_a_forged_state_is_refused(client, sso_enabled):
    resp = await client.post("/auth/sso/callback", json={"code": "c", "state": "not.a.real.jwt"})
    assert resp.status_code == 400


async def test_a_connector_state_cannot_be_redeemed_for_a_login(client, sso_enabled):
    """The two OAuth flows share a signing key; only `purpose` keeps them apart."""
    from osprey.security.oauth import sign_state

    state = sign_state({"project_id": "p", "source_type": "outlook", "verifier": "v"})
    resp = await client.post("/auth/sso/callback", json={"code": "c", "state": state})
    assert resp.status_code == 400


async def test_a_rejected_token_exchange_surfaces_cleanly(client, sso_enabled):
    resp = await _run_flow(client, token_status=400)
    assert resp.status_code == 401
    # The provider's body can echo the code back; it must not reach the client.
    assert "auth-code" not in resp.text


async def test_a_discovery_issuer_mismatch_is_refused(client, sso_enabled):
    """A hijacked well-known URL must not be able to point us at other keys."""
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{ISSUER}/.well-known/openid-configuration").mock(
            return_value=httpx.Response(
                200,
                json={
                    "issuer": "https://evil.example.com",
                    "authorization_endpoint": "https://evil.example.com/authorize",
                    "token_endpoint": "https://evil.example.com/token",
                    "jwks_uri": "https://evil.example.com/jwks",
                },
            )
        )
        resp = await client.post("/auth/sso/start")
    assert resp.status_code == 503


# --------------------------------------------------------------------------- #
# Redirect URI validation (RFC 8252 loopback for native clients)
# --------------------------------------------------------------------------- #
async def test_a_native_client_may_supply_a_loopback_redirect(client, sso_enabled):
    with respx.mock(assert_all_called=False) as mock:
        _mock_provider(mock, {})
        resp = await client.post(
            "/auth/sso/start", json={"redirect_uri": "http://127.0.0.1:51234/callback"}
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["redirect_uri"] == "http://127.0.0.1:51234/callback"
    assert (
        "127.0.0.1%3A51234" in body["authorize_url"] or "127.0.0.1:51234" in body["authorize_url"]
    )


async def test_the_loopback_redirect_is_carried_into_the_token_exchange(client, sso_enabled):
    """The provider compares it against /authorize; a mismatch fails the exchange."""
    seen: dict = {}
    state_box: dict = {}

    with respx.mock(assert_all_called=False) as mock:
        _mock_provider(mock, state_box)

        def _capture(request):
            from urllib.parse import parse_qs

            seen.update(parse_qs(request.content.decode()))
            body = _id_token(nonce=state_box.get("nonce"))
            return httpx.Response(200, json={"id_token": body, "token_type": "Bearer"})

        mock.post(f"{ISSUER}/token").mock(side_effect=_capture)

        start = (
            await client.post(
                "/auth/sso/start", json={"redirect_uri": "http://127.0.0.1:44444/callback"}
            )
        ).json()
        state_box["nonce"] = jwt.decode(start["state"], options={"verify_signature": False})[
            "nonce"
        ]
        await client.post("/auth/sso/callback", json={"code": "c", "state": start["state"]})

    assert seen["redirect_uri"] == ["http://127.0.0.1:44444/callback"]


@pytest.mark.parametrize(
    "redirect",
    [
        "https://evil.example.com/callback",
        "http://localhost:51234/callback",  # name resolution can be influenced
        "http://10.0.0.5:51234/callback",
        "http://user:pw@127.0.0.1:51234/callback",
        "http://127.0.0.1:51234/callback#frag",
        "ftp://127.0.0.1/callback",
    ],
)
async def test_a_non_loopback_redirect_is_refused(client, sso_enabled, redirect):
    """Echoing an unchecked caller-supplied URL is how codes reach the wrong party."""
    resp = await client.post("/auth/sso/start", json={"redirect_uri": redirect})
    assert resp.status_code == 400, resp.text


async def test_omitting_the_redirect_uses_the_configured_one(client, sso_enabled):
    with respx.mock(assert_all_called=False) as mock:
        _mock_provider(mock, {})
        body = (await client.post("/auth/sso/start", json={})).json()
    assert body["redirect_uri"] == "https://osprey.test/auth/sso/callback"


def test_allowed_redirect_uri_accepts_ipv6_loopback(monkeypatch):
    monkeypatch.setattr(settings, "oidc_redirect_url", "https://osprey.test/cb")
    assert oidc.allowed_redirect_uri("http://[::1]:9000/callback") == "http://[::1]:9000/callback"
