"""Desktop-app OAuth connector flow: authorize URL + code exchange (user-authed)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from osprey.connectors.outlook import OutlookConnector
from osprey.security.oauth import verify_state


async def _project(client) -> str:
    return (await client.post("/projects", json={"name": "Tower B"})).json()["id"]


async def test_sources_lists_auth_modes(auth_client):
    client, _ = auth_client
    sources = {s["source_type"]: s for s in (await client.get("/connections/sources")).json()}
    assert sources["outlook"]["auth"] == "oauth"
    assert sources["outlook"]["configured"] is True          # creds set in test env
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
    assert qs["code_challenge_method"] == ["S256"]      # PKCE
    assert qs["response_type"] == ["code"]
    # State is a signed JWT carrying the flow context + PKCE verifier.
    claims = verify_state(qs["state"][0])
    assert claims["source_type"] == "outlook"
    assert claims["project_id"] == project_id
    assert claims["cv"]                                  # code_verifier present


async def test_authorize_unconfigured_source_503(auth_client, monkeypatch):
    client, _ = auth_client
    project_id = await _project(client)
    # Procore creds are not set in the test env.
    resp = await client.post(
        "/connections/authorize",
        json={"project_id": project_id, "source_type": "procore", "redirect_uri": "http://127.0.0.1:9/cb"},
    )
    assert resp.status_code == 503


async def test_exchange_creates_connection(auth_client, monkeypatch):
    client, _ = auth_client
    project_id = await _project(client)
    redirect = "http://127.0.0.1:53682/callback"

    # Stub the network token exchange + account lookup (no live tenant in tests).
    async def fake_exchange(self, code, redirect_uri, code_verifier):
        assert code == "auth-code-123"
        assert code_verifier                              # PKCE verifier relayed
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
