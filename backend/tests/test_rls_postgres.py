"""Row-level security actually isolates tenants (Postgres only).

The 0002 migration creates the policies and CI proves it applies, but applying is
not the same as enforcing. These tests assert the database itself refuses to hand
back another org's rows when ``osprey.current_org`` is set — i.e. that RLS would
still contain a query that forgot its ``org_id`` filter.

Skipped on SQLite, which has no row-level security; the Postgres CI job runs them.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from osprey.config import settings
from osprey.models import Item, Org, Project

pytestmark = pytest.mark.skipif(
    settings.is_sqlite, reason="row-level security is a Postgres feature"
)

# Mirrors the policies in alembic/versions/0002_rls.py. FORCE is what makes them
# apply to the table owner too — without it the CI role would bypass RLS entirely.
# One statement per entry: asyncpg prepares statements, so it rejects multi-command
# strings.
_ENABLE_PROJECT_RLS = [
    "ALTER TABLE project ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE project FORCE ROW LEVEL SECURITY",
    "CREATE POLICY osprey_tenant_isolation ON project USING "
    "(org_id = current_setting('osprey.current_org', true))",
]

_ENABLE_ITEM_RLS = [
    "ALTER TABLE item ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE item FORCE ROW LEVEL SECURITY",
    "CREATE POLICY osprey_tenant_isolation ON item USING "
    "(project_id IN (SELECT id FROM project WHERE org_id = "
    "current_setting('osprey.current_org', true)))",
]


# RLS is bypassed entirely by superusers and by roles with BYPASSRLS — FORCE does
# not change that. Production must therefore connect as an ordinary role, and these
# tests assume that role via SET LOCAL ROLE (the CI container user is a superuser).
_APP_ROLE = "osprey_app_test"
_CREATE_APP_ROLE = [
    f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{_APP_ROLE}') "
    f"THEN CREATE ROLE {_APP_ROLE} NOLOGIN; END IF; END $$",
    f"GRANT USAGE ON SCHEMA public TO {_APP_ROLE}",
    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {_APP_ROLE}",
]


async def _apply(session, statements: list[str]) -> None:
    for stmt in statements:
        await session.execute(text(stmt))


async def _become_app_role(session) -> None:
    """Run the rest of this transaction as a non-superuser so RLS actually applies."""
    await _apply(session, _CREATE_APP_ROLE)
    await session.execute(text(f"SET LOCAL ROLE {_APP_ROLE}"))


async def _two_orgs(session):
    """Two orgs, each with a project and an item. Created before RLS is enabled."""
    a, b = Org(name="Org A"), Org(name="Org B")
    session.add_all([a, b])
    await session.flush()
    pa = Project(org_id=a.id, name="A project")
    pb = Project(org_id=b.id, name="B project")
    session.add_all([pa, pb])
    await session.flush()
    session.add_all(
        [Item(project_id=pa.id, title="A item"), Item(project_id=pb.id, title="B item")]
    )
    await session.flush()
    return a, b, pa, pb


async def test_rls_hides_other_orgs_projects(session):
    a, b, _, _ = await _two_orgs(session)
    await _apply(session, _ENABLE_PROJECT_RLS)
    await _become_app_role(session)

    # Scoped to org A: only A's project is visible, even though the query has no
    # org_id predicate of its own — the database applies the filter.
    await session.execute(text("SELECT set_config('osprey.current_org', :o, true)"), {"o": a.id})
    names = (await session.execute(text("SELECT name FROM project"))).scalars().all()
    assert names == ["A project"]

    # Switching tenants switches the visible rows.
    await session.execute(text("SELECT set_config('osprey.current_org', :o, true)"), {"o": b.id})
    names = (await session.execute(text("SELECT name FROM project"))).scalars().all()
    assert names == ["B project"]


async def test_rls_hides_everything_when_no_tenant_is_set(session):
    """An unscoped connection sees nothing — fail closed, not open."""
    await _two_orgs(session)
    await _apply(session, _ENABLE_PROJECT_RLS)
    await _become_app_role(session)

    await session.execute(text("SELECT set_config('osprey.current_org', '', true)"))
    rows = (await session.execute(text("SELECT name FROM project"))).scalars().all()
    assert rows == []


async def test_rls_isolates_items_through_their_project(session):
    """Tables without org_id are still scoped, via the project hop."""
    a, _, _, _ = await _two_orgs(session)
    await _apply(session, _ENABLE_PROJECT_RLS)
    await _apply(session, _ENABLE_ITEM_RLS)
    await _become_app_role(session)

    await session.execute(text("SELECT set_config('osprey.current_org', :o, true)"), {"o": a.id})
    titles = (await session.execute(text("SELECT title FROM item"))).scalars().all()
    assert titles == ["A item"]


async def test_set_current_org_helper_scopes_the_session(session):
    """The app helper used by the request path sets the same GUC."""
    from osprey.security.rls import set_current_org

    a, _, _, _ = await _two_orgs(session)
    await _apply(session, _ENABLE_PROJECT_RLS)
    await _become_app_role(session)

    # The helper is a no-op unless RLS is switched on in config.
    original = settings.rls_enabled
    settings.rls_enabled = True
    try:
        await set_current_org(session, a.id)
        names = (await session.execute(text("SELECT name FROM project"))).scalars().all()
        assert names == ["A project"]
    finally:
        settings.rls_enabled = original


async def test_can_bypass_rls_detects_superuser_vs_ordinary_role(session):
    """The startup guard must tell a bypassing role from an enforcing one."""
    from osprey.security.rls import can_bypass_rls

    # The CI container connects as a superuser — it *would* bypass RLS.
    assert await can_bypass_rls(session) is True

    # After assuming the ordinary app role, it would not.
    await _become_app_role(session)
    assert await can_bypass_rls(session) is False


async def test_verify_enforcement_reports_false_for_bypassing_role(session):
    """Configured-but-bypassed must report False, not a cheerful True."""
    from osprey.security.rls import verify_enforcement

    original = settings.rls_enabled
    settings.rls_enabled = True
    try:
        assert await verify_enforcement(session) is False  # superuser
        await _become_app_role(session)
        assert await verify_enforcement(session) is True  # ordinary role
    finally:
        settings.rls_enabled = original


async def test_registration_works_with_rls_enforced(client):
    """Signup is the one flow with no tenant yet — it must still work under RLS.

    The policies' USING clause also governs INSERT, so without binding the new org
    up front every row written during registration (org, membership, audit) is
    rejected. This is a regression guard for exactly that.
    """
    original = settings.rls_enabled
    settings.rls_enabled = True
    try:
        resp = await client.post(
            "/auth/register",
            json={"email": "rls@example.com", "password": "password123", "org_name": "RLS Co"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["access_token"]
    finally:
        settings.rls_enabled = original
