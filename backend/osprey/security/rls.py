"""Row-level-security helper — Postgres tenant isolation (defense in depth).

Osprey already scopes every query by ``org_id`` in application code. RLS adds a
second, database-enforced layer so a query bug can't leak across tenants: policies
compare each row's ``org_id`` to a per-connection setting, ``osprey.current_org``,
which we set from the authenticated principal.

No-op on SQLite (dev/test). Enable with ``OSPREY_RLS_ENABLED=true`` after running the
``0002_rls`` migration.

**The application must connect as an ordinary role.** Postgres lets superusers — and
any role with ``BYPASSRLS`` — skip row-level security entirely, and ``FORCE ROW LEVEL
SECURITY`` does *not* override that. Pointing ``OSPREY_DATABASE_URL`` at a superuser
(the default in most container images) silently disables this whole control. Create a
dedicated role instead::

    CREATE ROLE osprey_app LOGIN PASSWORD '...';
    GRANT USAGE ON SCHEMA public TO osprey_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO osprey_app;

``tests/test_rls_postgres.py`` asserts the isolation actually holds for such a role.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings


async def set_current_org(session: AsyncSession, org_id: str) -> None:
    """Bind the tenant for this connection so RLS policies apply. Postgres-only."""
    if settings.is_sqlite or not settings.rls_enabled:
        return
    # set_config(name, value, is_local=true) => scoped to the current transaction.
    await session.execute(
        text("SELECT set_config('osprey.current_org', :org, true)"), {"org": org_id}
    )
