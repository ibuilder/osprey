"""Test harness: offline AI, per-test schema reset, isolated database.

The suite runs with zero infrastructure by default — deterministic AI provider,
hashing embedder, SQLite storage — so it is reproducible anywhere.

``OSPREY_DATABASE_URL`` is honoured if it is already set, which lets CI point the
same suite at a real Postgres (see the ``postgres`` job) to exercise the asyncpg
path. Everything else is forced, so tests can never reach a real provider.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import tempfile

# Configure the environment BEFORE importing any osprey module (settings read env).
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="osprey-test-")).as_posix()
os.environ.setdefault("OSPREY_DATABASE_URL", f"sqlite+aiosqlite:///{_TMP}/test.db")
os.environ.update(
    OSPREY_ENV="test",
    OSPREY_DEBUG="true",
    OSPREY_SECRET_KEY="test-secret-key-at-least-32-bytes-long-000",
    OSPREY_ENCRYPTION_KEY="test-encryption-key",
    OSPREY_WEBHOOK_HMAC_SECRET="test-hmac-secret",
    OSPREY_AI_PROVIDER="deterministic",
    OSPREY_MSGRAPH_CLIENT_ID="test-msgraph-client",
    OSPREY_MSGRAPH_CLIENT_SECRET="test-msgraph-secret",
    OSPREY_GOOGLE_CLIENT_ID="test-google-client",
    OSPREY_GOOGLE_CLIENT_SECRET="test-google-secret",
    # Connector pacing is process-wide and would otherwise make the mocked poll
    # tests wait on a real token bucket. The retry/backoff logic itself is tested
    # directly in test_connector_http.py with an injected clock.
    OSPREY_CONNECTOR_RATE_PER_SEC="10000",
    OSPREY_CONNECTOR_RATE_BURST="10000",
    # PBKDF2 at the production 390k rounds costs ~0.2s per hash; the suite hashes
    # hundreds, which added ~7 minutes to a run. The KDF itself is verified in
    # test_security.py, and config.assert_prod_secrets refuses a production boot
    # below the OWASP floor, so lowering it here cannot leak into a deployment.
    OSPREY_PASSWORD_HASH_ITERATIONS="1000",
    # The limiter is in-process and shared, so without this a test that logs in
    # repeatedly would spend the budget of every test after it. Kept enabled (not
    # disabled outright) so the limiter code stays on the tested path.
    OSPREY_RATE_LIMIT_BACKEND="memory",
)

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

import osprey.connectors  # noqa: E402,F401  (register connectors)
from osprey.db import create_all, dispose, drop_all, get_sessionmaker  # noqa: E402
from osprey.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_rate_limiter():
    """Give every test its own counters.

    The limiter is deliberately process-global in production -- that is what
    makes it a limiter -- so tests have to reset it rather than work around it.
    """
    from osprey.security.ratelimit import MemoryLimiter, set_limiter

    set_limiter(MemoryLimiter())
    yield
    set_limiter(None)


@pytest_asyncio.fixture(autouse=True)
async def _schema():
    """Fresh schema per test, and a fresh engine bound to this test's event loop.

    pytest-asyncio runs each test in its own loop. The engine is process-global, and
    asyncpg pins pooled connections to the loop that opened them, so a cached engine
    leaks across loops and raises "attached to a different loop" on Postgres.
    (aiosqlite happens to tolerate it, which is why SQLite runs never caught this.)
    Disposing on teardown keeps the suite backend-agnostic.
    """
    await drop_all()
    await create_all()
    yield
    # Sync tests that use starlette's TestClient run the app on their own loop via a
    # blocking portal, and the app's lifespan disposes the engine there. Disposing
    # again from the pytest loop can then hit an already-closed / foreign-loop pool.
    # Teardown cleanup must not fail the test that just passed.
    with contextlib.suppress(Exception):
        await dispose()


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
            "password": "Sup3rSecret!pass",
            "full_name": "Owner",
            "org_name": "Tower B GC",
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    client.headers["Authorization"] = f"Bearer {data['access_token']}"
    return client, data


async def _make_member_token(session, org_id: str, role, *, email: str) -> str:
    """Create a real user with `role` in `org_id` and return an access token.

    Forging a Principal alone stopped working once the auth dependency began
    verifying the subject against the database (token revocation). Creating the
    user is also a better test: it exercises the same path a real caller takes.
    """
    from osprey.models import Membership, User
    from osprey.security.auth import Principal, create_access_token
    from osprey.security.passwords import hash_password

    user = User(email=email, password_hash=hash_password("Sup3rSecret!pass"))
    session.add(user)
    await session.flush()
    session.add(Membership(org_id=org_id, user_id=user.id, role=role))
    await session.commit()
    return create_access_token(
        Principal(
            user_id=user.id,
            org_id=org_id,
            role=role,
            email=email,
            token_version=user.token_version,
        )
    )


@pytest.fixture
def member_token(session):
    """Factory: ``await member_token(org_id, Role.viewer, email="...")``."""

    async def _factory(org_id: str, role, *, email: str) -> str:
        return await _make_member_token(session, org_id, role, email=email)

    return _factory
