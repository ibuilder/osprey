"""row-level security policies for tenant isolation (Postgres only)

Revision ID: 0002_rls
Revises: 0001_baseline
Create Date: 2026-07-23

Enables RLS on org-scoped tables. Each policy restricts rows to those whose tenant
key equals ``current_setting('osprey.current_org')`` (set per request by the app).
No-op on SQLite. Superusers bypass RLS — connect the app as a normal role.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_rls"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables that carry the tenant key directly ("org" keys on its own id).
_DIRECT_ORG = [
    "org",
    "membership",
    "project",
    "connection",
    "ai_connection",
    "script_task",
    "device",
    "audit_log",
]
# Tables reached through their project.
_VIA_PROJECT = ["signal", "item", "action", "hotlist_snapshot"]
# "score" has no project_id — it hangs off an item, so it needs one more hop.
_VIA_ITEM = ["score"]


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return
    for table in _DIRECT_ORG:
        col = "id" if table == "org" else "org_id"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY osprey_tenant_isolation ON {table} USING "
            f"({col} = current_setting('osprey.current_org', true))"
        )
    for table in _VIA_PROJECT:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY osprey_tenant_isolation ON {table} USING "
            f"(project_id IN (SELECT id FROM project WHERE org_id = "
            f"current_setting('osprey.current_org', true)))"
        )
    for table in _VIA_ITEM:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY osprey_tenant_isolation ON {table} USING "
            f"(item_id IN (SELECT id FROM item WHERE project_id IN "
            f"(SELECT id FROM project WHERE org_id = "
            f"current_setting('osprey.current_org', true))))"
        )


def downgrade() -> None:
    if not _is_postgres():
        return
    for table in _DIRECT_ORG + _VIA_PROJECT + _VIA_ITEM:
        op.execute(f"DROP POLICY IF EXISTS osprey_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
