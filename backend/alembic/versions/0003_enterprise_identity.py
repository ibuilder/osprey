"""enterprise identity, revocable sessions, retention

Revision ID: 0003_enterprise_identity
Revises: 0002_rls
Create Date: 2026-08-27

Adds:
  * ``user`` columns for token revocation, SSO subject binding, SCIM ownership,
    and failed-login lockout
  * ``org`` columns for per-tenant retention and pending erasure
  * ``refresh_token``  — revocable sessions with reuse detection
  * ``scim_token``     — per-org provisioning credentials
  * ``invite``         — pending memberships

The ``role`` ENUM already exists (created by 0001 for ``membership``). Postgres
would try to create it again for ``scim_token.max_role`` and ``invite.role``, so
both reference it with ``create_type=False``. Getting this wrong is the same
class of bug the baseline hit in the other direction — see docs/backlog.md.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel

from alembic import op

revision: str = "0003_enterprise_identity"
down_revision: str | None = "0002_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _role_enum() -> sa.Enum:
    # postgresql.ENUM(create_type=False) is the Postgres-specific spelling; the
    # generic Enum with a matching name resolves to the existing type there and
    # to a CHECK constraint on SQLite, which is what both backends need.
    return sa.Enum("owner", "admin", "pm", "viewer", name="role", create_type=False)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # ---- user ------------------------------------------------------------- #
    with op.batch_alter_table("user") as batch:
        batch.add_column(
            sa.Column("token_version", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column(
                "sso_subject",
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default="",
            )
        )
        batch.add_column(
            sa.Column("scim_managed", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(
            sa.Column(
                "external_id",
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default="",
            )
        )
        batch.add_column(
            sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_user_sso_subject", "user", ["sso_subject"])

    # ---- org -------------------------------------------------------------- #
    with op.batch_alter_table("org") as batch:
        batch.add_column(sa.Column("retention_signal_days", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("retention_item_days", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("deletion_requested_at", sa.DateTime(timezone=True), nullable=True)
        )

    # ---- refresh_token ----------------------------------------------------- #
    op.create_table(
        "refresh_token",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("org_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("user_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("token_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("family_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("rotated_to", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("ip", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_refresh_token_org_id", "refresh_token", ["org_id"])
    op.create_index("ix_refresh_token_user_id", "refresh_token", ["user_id"])
    op.create_index("ix_refresh_token_family_id", "refresh_token", ["family_id"])
    # Unique: the lookup key, and two sessions must never collide on it.
    op.create_index("ix_refresh_token_token_hash", "refresh_token", ["token_hash"], unique=True)

    # ---- scim_token -------------------------------------------------------- #
    op.create_table(
        "scim_token",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("org_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("token_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("max_role", _role_enum(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scim_token_org_id", "scim_token", ["org_id"])
    op.create_index("ix_scim_token_token_hash", "scim_token", ["token_hash"], unique=True)

    # ---- invite ------------------------------------------------------------ #
    op.create_table(
        "invite",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("org_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("email", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("role", _role_enum(), nullable=False),
        sa.Column("token_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("invited_by", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_invite_org_id", "invite", ["org_id"])
    op.create_index("ix_invite_email", "invite", ["email"])
    op.create_index("ix_invite_token_hash", "invite", ["token_hash"], unique=True)

    # ---- tenant isolation for the new tables ------------------------------- #
    # 0002 enabled RLS per table; these three carry org_id directly, so they get
    # the same treatment. Omitting this would leave three tables outside the
    # policy set that the deploy-smoke job asserts is complete.
    if _is_postgres():
        for table in ("refresh_token", "scim_token", "invite"):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"CREATE POLICY {table}_tenant_isolation ON {table} "
                f"USING (org_id = current_setting('osprey.current_org', true)) "
                f"WITH CHECK (org_id = current_setting('osprey.current_org', true))"
            )


def downgrade() -> None:
    if _is_postgres():
        for table in ("refresh_token", "scim_token", "invite"):
            op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")

    op.drop_index("ix_invite_token_hash", table_name="invite")
    op.drop_index("ix_invite_email", table_name="invite")
    op.drop_index("ix_invite_org_id", table_name="invite")
    op.drop_table("invite")

    op.drop_index("ix_scim_token_token_hash", table_name="scim_token")
    op.drop_index("ix_scim_token_org_id", table_name="scim_token")
    op.drop_table("scim_token")

    op.drop_index("ix_refresh_token_token_hash", table_name="refresh_token")
    op.drop_index("ix_refresh_token_family_id", table_name="refresh_token")
    op.drop_index("ix_refresh_token_user_id", table_name="refresh_token")
    op.drop_index("ix_refresh_token_org_id", table_name="refresh_token")
    op.drop_table("refresh_token")

    with op.batch_alter_table("org") as batch:
        batch.drop_column("deletion_requested_at")
        batch.drop_column("retention_item_days")
        batch.drop_column("retention_signal_days")

    op.drop_index("ix_user_sso_subject", table_name="user")
    with op.batch_alter_table("user") as batch:
        batch.drop_column("updated_at")
        batch.drop_column("last_login_at")
        batch.drop_column("locked_until")
        batch.drop_column("failed_login_count")
        batch.drop_column("external_id")
        batch.drop_column("scim_managed")
        batch.drop_column("sso_subject")
        batch.drop_column("token_version")
    # The "role" ENUM is left in place: it was created by 0001 for membership
    # and is still in use after this downgrade.
