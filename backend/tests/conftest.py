"""Test harness: isolated SQLite DB, offline AI, per-test schema reset.

The whole suite runs with zero infrastructure — deterministic AI provider and
hashing embedder, SQLite storage — so it is fully reproducible in CI.
"""

from __future__ import annotations

import os
import pathlib
import tempfile

# Configure the environment BEFORE importing any osprey module (settings read env).
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="osprey-test-")).as_posix()
os.environ.update(
    OSPREY_ENV="test",
    OSPREY_DEBUG="true",
    OSPREY_DATABASE_URL=f"sqlite+aiosqlite:///{_TMP}/test.db",
    OSPREY_SECRET_KEY="test-secret-key-at-least-32-bytes-long-000",
    OSPREY_ENCRYPTION_KEY="test-encryption-key",
    OSPREY_WEBHOOK_HMAC_SECRET="test-hmac-secret",
    OSPREY_AI_PROVIDER="deterministic",
    OSPREY_MSGRAPH_CLIENT_ID="test-msgraph-client",
    OSPREY_MSGRAPH_CLIENT_SECRET="test-msgraph-secret",
    OSPREY_GOOGLE_CLIENT_ID="test-google-client",
    OSPREY_GOOGLE_CLIENT_SECRET="test-google-secret",
)

import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

import osprey.connectors  # noqa: E402,F401  (register connectors)
from osprey.db import create_all, drop_all, get_sessionmaker  # noqa: E402
from osprey.main import app  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def _schema():
    await drop_all()
    await create_all()
    yield


@pytest_asyncio.fixture
async def session():
    maker = get_sessionmaker()
    async with maker() as s:
        yield s
        await s.commit()


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture
async def auth_client(client):
    """A registered owner client + its token/context."""
    resp = await client.post(
        "/auth/register",
        json={
            "email": "owner@example.com",
            "password": "supersecret1",
            "full_name": "Owner",
            "org_name": "Tower B GC",
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    client.headers["Authorization"] = f"Bearer {data['access_token']}"
    return client, data
