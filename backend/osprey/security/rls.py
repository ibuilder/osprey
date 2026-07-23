"""Row-level-security helper — Postgres tenant isolation (defense in depth).

Osprey already scopes every query by ``org_id`` in application code. RLS adds a
second, database-enforced layer so a query bug can't leak across tenants: policies
compare each row's ``org_id`` to a per-connection setting, ``osprey.current_org``,
which we set from the authenticated principal.

No-op on SQLite (dev/test). Enable with ``OSPREY_RLS_ENABLED=true`` after running the
``0002_rls`` migration and connecting as a non-superuser role (superusers bypass RLS).
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
