"""The row-level-security helpers' decisions, without a Postgres server.

`test_rls_postgres.py` proves the policies isolate tenants on a real database,
but it only runs where Postgres is available. The helpers' own branching -- when
the tenant GUC is set, and when the startup guard reports enforcement as off --
is what decides whether that isolation is active at all, so it is tested here
on every run with a fake session.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from osprey.config import settings
from osprey.security import rls

POSTGRES_URL = "postgresql+asyncpg://osprey_app@db/osprey"


class FakeSession:
    def __init__(self, *, scalar: object = False, error: Exception | None = None) -> None:
        self.statements: list[tuple[str, dict | None]] = []
        self._scalar = scalar
        self._error = error

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        if self._error is not None:
            raise self._error
        return SimpleNamespace(scalar_one=lambda: self._scalar)


@pytest.fixture
def postgres_with_rls(monkeypatch):
    monkeypatch.setattr(settings, "database_url", POSTGRES_URL)
    monkeypatch.setattr(settings, "rls_enabled", True)


@pytest.fixture
def log(monkeypatch):
    fake = Mock()
    monkeypatch.setattr(rls, "log", fake)
    return fake


async def test_set_current_org_binds_the_tenant_for_this_transaction(postgres_with_rls):
    session = FakeSession()

    await rls.set_current_org(session, "org-42")

    [(sql, params)] = session.statements
    assert "set_config('osprey.current_org'" in sql
    # is_local=true scopes it to the transaction, so a pooled connection handed to
    # the next request does not arrive still bound to this tenant.
    assert "true" in sql
    assert params == {"org": "org-42"}


@pytest.mark.parametrize(
    ("url", "enabled"),
    [("sqlite+aiosqlite:///./osprey.db", True), (POSTGRES_URL, False)],
)
async def test_set_current_org_is_a_no_op_without_postgres_rls(monkeypatch, url, enabled):
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(settings, "rls_enabled", enabled)
    session = FakeSession()

    await rls.set_current_org(session, "org-42")

    assert session.statements == []


async def test_can_bypass_rls_reports_the_roles_privilege(postgres_with_rls):
    assert await rls.can_bypass_rls(FakeSession(scalar=True)) is True
    assert await rls.can_bypass_rls(FakeSession(scalar=False)) is False


async def test_verify_enforcement_confirms_an_ordinary_role(postgres_with_rls, log):
    assert await rls.verify_enforcement(FakeSession(scalar=False)) is True
    log.error.assert_not_called()


async def test_verify_enforcement_shouts_when_the_role_bypasses_rls(postgres_with_rls, log):
    # A superuser connection makes every policy inert; this must not pass quietly.
    assert await rls.verify_enforcement(FakeSession(scalar=True)) is False
    log.error.assert_called_once()
    assert "BYPASS" in log.error.call_args.args[0]


async def test_verify_enforcement_never_blocks_startup_on_its_own_failure(postgres_with_rls, log):
    session = FakeSession(error=RuntimeError("permission denied for pg_roles"))

    assert await rls.verify_enforcement(session) is False
    log.warning.assert_called_once()


async def test_verify_enforcement_is_off_on_sqlite(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///./osprey.db")
    monkeypatch.setattr(settings, "rls_enabled", True)
    session = FakeSession(scalar=False)

    assert await rls.verify_enforcement(session) is False
    assert session.statements == []
