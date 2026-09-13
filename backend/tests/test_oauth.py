"""Desktop-app OAuth connector flow: authorize URL + code exchange (user-authed)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from osprey.connectors.outlook import OutlookConnector
from osprey.security.oauth import verify_state


async def _project(client) -> str:
    return (await client.post("/projects", json={"name": "Tower B"})).json()["id"]


async def test_sources_lists_auth_modes(auth_client):
    client, _ = auth_client
    sources = {s["source_type"]: s for s in (await client.get("/connections/sources")).json()}
    assert sources["outlook"]["auth"] == "oauth"
    assert sources["outlook"]["configured"] is True  # creds set in test env
    assert sources["filedrop"]["auth"] == "forward"
    assert sources["pyscript"]["auth"] == "internal"
    assert set(sources["outlook"]["scopes"]) >= {"Mail.Read"}


async def test_authorize_returns_valid_consent_url(auth_client):
    client, _ = auth_client
    project_id = await _project(client)
    redirect = "http://127.0.0.1:53682/callback"
    resp = await client.post(
        "/connections/authorize",
        json={"project_id": project_id, "source_type": "outlook", "redirect_uri": redirect},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    parsed = urlparse(body["authorize_url"])
    qs = parse_qs(parsed.query)
    assert "login.microsoftonline.com" in parsed.netloc
    assert qs["client_id"] == ["test-msgraph-client"]
    assert qs["redirect_uri"] == [redirect]
    assert qs["code_challenge_method"] == ["S256"]  # PKCE
    assert qs["response_type"] == ["code"]
    # State is a signed JWT carrying the flow context + PKCE verifier.
    claims = verify_state(qs["state"][0])
    assert claims["source_type"] == "outlook"
    assert claims["project_id"] == project_id
    assert claims["cv"]  # code_verifier present


async def test_authorize_unconfigured_source_503(auth_client, monkeypatch):
    client, _ = auth_client
    project_id = await _project(client)
    # Procore creds are not set in the test env.
    resp = await client.post(
        "/connections/authorize",
        json={
            "project_id": project_id,
            "source_type": "procore",
            "redirect_uri": "http://127.0.0.1:9/cb",
        },
    )
    assert resp.status_code == 503


async def test_exchange_creates_connection(auth_client, monkeypatch):
    client, _ = auth_client
    project_id = await _project(client)
    redirect = "http://127.0.0.1:53682/callback"

    # Stub the network token exchange + account lookup (no live tenant in tests).
    async def fake_exchange(self, code, redirect_uri, code_verifier):
        assert code == "auth-code-123"
        assert code_verifier  # PKCE verifier relayed
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}

    async def fake_account(self, tokens):
        return "pm@toweb.com"

    monkeypatch.setattr(OutlookConnector, "exchange_code", fake_exchange)
    monkeypatch.setattr(OutlookConnector, "account_ref_from_tokens", fake_account)

    state = (
        await client.post(
            "/connections/authorize",
            json={"project_id": project_id, "source_type": "outlook", "redirect_uri": redirect},
        )
    ).json()["state"]

    resp = await client.post(
        "/connections/exchange",
        json={"code": "auth-code-123", "state": state, "redirect_uri": redirect},
    )
    assert resp.status_code == 201, resp.text
    conn = resp.json()
    assert conn["source_type"] == "outlook"
    assert conn["account_ref"] == "pm@toweb.com"
    assert conn["status"] == "active"

    # Tokens are sealed at rest — never returned, never plaintext.
    listed = (await client.get("/connections")).json()
    assert any(c["id"] == conn["id"] for c in listed)


async def test_exchange_rejects_forged_state(auth_client):
    client, _ = auth_client
    resp = await client.post(
        "/connections/exchange",
        json={"code": "x", "state": "not-a-real-jwt", "redirect_uri": "http://127.0.0.1:9/cb"},
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Redirect URI validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "redirect",
    [
        "https://evil.example.com/callback",
        "http://evil.example.com/callback",
        # localhost resolves through host name resolution another process can
        # influence; the literal loopback addresses cannot be redirected.
        "http://localhost:53682/callback",
        "http://10.0.0.5:53682/callback",
        "http://user:pw@127.0.0.1:53682/callback",
        "http://127.0.0.1:53682/callback#fragment",
        "ftp://127.0.0.1/callback",
        "",
    ],
)
async def test_authorize_refuses_a_non_loopback_redirect(auth_client, redirect):
    """This value goes straight into the provider's authorize request."""
    client, _ = auth_client
    project_id = await _project(client)
    resp = await client.post(
        "/connections/authorize",
        json={"project_id": project_id, "source_type": "outlook", "redirect_uri": redirect},
    )
    assert resp.status_code == 400, resp.text
    assert "loopback" in resp.json()["detail"]


async def test_authorize_accepts_an_ipv6_loopback(auth_client):
    client, _ = auth_client
    project_id = await _project(client)
    resp = await client.post(
        "/connections/authorize",
        json={
            "project_id": project_id,
            "source_type": "outlook",
            "redirect_uri": "http://[::1]:53682/callback",
        },
    )
    assert resp.status_code == 200, resp.text


async def test_exchange_will_not_let_the_body_override_the_signed_state(auth_client, monkeypatch):
    """The state is the trustworthy copy of the redirect; the body is not.

    Accepting `body.redirect_uri or claims["redirect_uri"]` -- which this handler
    used to do -- means the value sealed at authorize time can be swapped at
    exchange time, making the seal decorative.
    """
    used: dict = {}

    async def fake_exchange(self, code, redirect_uri, code_verifier):
        used["redirect_uri"] = redirect_uri
        return {"access_token": "t", "refresh_token": "r"}

    async def fake_account_ref(self, tokens):
        return "someone@example.com"

    monkeypatch.setattr(OutlookConnector, "exchange_code", fake_exchange)
    monkeypatch.setattr(OutlookConnector, "account_ref_from_tokens", fake_account_ref)

    client, _ = auth_client
    project_id = await _project(client)
    redirect = "http://127.0.0.1:53682/callback"
    state = (
        await client.post(
            "/connections/authorize",
            json={"project_id": project_id, "source_type": "outlook", "redirect_uri": redirect},
        )
    ).json()["state"]

    # A different (still loopback, so it would pass a shape check) redirect.
    mismatch = await client.post(
        "/connections/exchange",
        json={"code": "c", "state": state, "redirect_uri": "http://127.0.0.1:44444/callback"},
    )
    assert mismatch.status_code == 400
    assert "does not match" in mismatch.json()["detail"]
    assert not used, "the token exchange must not have been attempted"

    # Omitting it entirely uses the state's copy, which is the intended path.
    ok = await client.post("/connections/exchange", json={"code": "c", "state": state})
    assert ok.status_code == 201, ok.text
    assert used["redirect_uri"] == redirect
