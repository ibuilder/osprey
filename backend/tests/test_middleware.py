"""HTTP hardening: security headers, request ids, body caps, rate limiting, metrics."""

from __future__ import annotations

import pytest

from osprey.config import settings
from osprey.middleware import REQUEST_ID_HEADER


async def test_security_headers_on_every_response(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'none'" in resp.headers["Content-Security-Policy"]
    assert "camera=()" in resp.headers["Permissions-Policy"]


async def test_security_headers_present_on_errors_too(client):
    """A 401 is exactly where a missing frame-ancestors policy would matter."""
    resp = await client.get("/projects")
    assert resp.status_code == 401
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


async def test_hsts_absent_without_tls(client):
    """HSTS over plain HTTP would poison localhost for two years."""
    assert "Strict-Transport-Security" not in (await client.get("/health")).headers


async def test_request_id_is_issued_and_echoed(client):
    resp = await client.get("/health")
    assert len(resp.headers[REQUEST_ID_HEADER]) == 32  # uuid4 hex


async def test_upstream_request_id_is_adopted(client):
    resp = await client.get("/health", headers={REQUEST_ID_HEADER: "trace-abc-123"})
    assert resp.headers[REQUEST_ID_HEADER] == "trace-abc-123"


async def test_hostile_request_id_is_replaced(client):
    """The id is echoed into headers and logs, so it must not carry injection."""
    resp = await client.get("/health", headers={REQUEST_ID_HEADER: "abc\r\nX-Evil: 1"})
    assert resp.headers[REQUEST_ID_HEADER] != "abc\r\nX-Evil: 1"
    assert "X-Evil" not in resp.headers


async def test_error_bodies_carry_the_request_id(client):
    body = (await client.get("/projects")).json()
    assert body["request_id"]


async def test_oversized_body_is_refused(client, monkeypatch):
    monkeypatch.setattr(settings, "max_request_body_bytes", 1024)
    resp = await client.post(
        "/auth/register",
        json={
            "email": "big@example.com",
            "password": "Sup3rSecret!pass",
            "org_name": "x" * 4096,
        },
    )
    assert resp.status_code == 413


async def test_validation_errors_never_echo_the_password(client):
    """Pydantic's default handler returns the rejected input, passwords included."""
    resp = await client.post(
        "/auth/register",
        json={"email": "not-an-email", "password": "hunter2-in-the-clear", "org_name": "X"},
    )
    assert resp.status_code == 422
    assert "hunter2-in-the-clear" not in resp.text


async def test_rate_limit_headers_are_reported(client):
    resp = await client.get("/projects")
    assert int(resp.headers["X-RateLimit-Limit"]) > 0
    assert "X-RateLimit-Remaining" in resp.headers


async def test_anonymous_rate_limit_trips(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_anonymous_per_minute", 3)
    codes = [(await client.get("/projects")).status_code for _ in range(6)]
    assert 429 in codes
    # The refusal must tell the client when to come back.
    last = await client.get("/projects")
    if last.status_code == 429:
        assert int(last.headers["Retry-After"]) >= 1


async def test_probes_are_never_rate_limited(client, monkeypatch):
    """A 1s Prometheus scrape must not consume the anonymous budget."""
    monkeypatch.setattr(settings, "rate_limit_anonymous_per_minute", 2)
    codes = [(await client.get("/health")).status_code for _ in range(10)]
    assert codes == [200] * 10


async def test_metrics_endpoint_exposes_series(client):
    await client.get("/health")
    body = (await client.get("/metrics")).text
    assert "osprey_http_requests_total" in body
    assert "osprey_build_info" in body


async def test_metrics_label_by_route_template_not_path(client, auth_client):
    """Labelling by resolved path would mint a series per project id."""
    owner_client, _ = auth_client
    project_id = (await owner_client.post("/projects", json={"name": "Metrics Co"})).json()["id"]
    await owner_client.get(f"/projects/{project_id}/items")
    body = (await client.get("/metrics")).text
    assert "/projects/{project_id}/items" in body
    assert project_id not in body


async def test_metrics_token_is_enforced(client, monkeypatch):
    monkeypatch.setattr(settings, "metrics_token", "scrape-me")
    assert (await client.get("/metrics")).status_code == 401
    ok = await client.get("/metrics", headers={"Authorization": "Bearer scrape-me"})
    assert ok.status_code == 200


@pytest.mark.parametrize("path", ["/live", "/health"])
async def test_liveness_never_touches_the_database(client, path):
    assert (await client.get(path)).status_code == 200


async def test_ready_returns_503_when_the_database_is_gone(client, monkeypatch):
    """Returning 200 with {"ready": false} leaves a broken pod in the LB pool."""
    from sqlalchemy.ext.asyncio import AsyncSession

    async def boom(self, *args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(AsyncSession, "execute", boom)
    resp = await client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["ready"] is False
