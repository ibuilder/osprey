"""Admin console: connection health, audit verification, stats, feature flags."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import (
    AIConnection,
    Connection,
    Item,
    Project,
    Role,
    ScriptTask,
    Signal,
)
from ..security import audit
from ..security.auth import Principal
from .deps import db_session, require_role

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/connections/health")
async def connections_health(
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(require_role(Role.admin)),
) -> list[dict]:
    rows = (
        (await session.execute(select(Connection).where(Connection.org_id == principal.org_id)))
        .scalars()
        .all()
    )
    return [
        {
            "id": r.id,
            "source_type": r.source_type,
            "account_ref": r.account_ref,
            "status": r.status.value,
            "last_sync": r.last_sync.isoformat() if r.last_sync else None,
            "last_error": r.last_error,
            "project_id": r.project_id,
        }
        for r in rows
    ]


@router.get("/audit/verify")
async def verify_audit(
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(require_role(Role.admin)),
) -> dict:
    ok = await audit.verify_chain(session, principal.org_id)
    return {"org_id": principal.org_id, "audit_chain_intact": ok}


@router.get("/stats")
async def stats(
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(require_role(Role.admin)),
) -> dict:
    org_id = principal.org_id

    async def _count(model, *conds) -> int:
        return (
            await session.execute(select(func.count()).select_from(model).where(*conds))
        ).scalar_one()

    project_ids = (
        (await session.execute(select(Project.id).where(Project.org_id == org_id))).scalars().all()
    )
    return {
        "org_id": org_id,
        "projects": len(project_ids),
        "connections": await _count(Connection, Connection.org_id == org_id),
        "ai_connections": await _count(AIConnection, AIConnection.org_id == org_id),
        "scripts": await _count(ScriptTask, ScriptTask.org_id == org_id),
        "items": await _count(Item, Item.project_id.in_(project_ids)) if project_ids else 0,
        "signals": await _count(Signal, Signal.project_id.in_(project_ids)) if project_ids else 0,
    }


@router.get("/features")
async def features(_: Principal = Depends(require_role(Role.admin))) -> dict:
    return {
        "ai_sift": settings.feature_ai_sift,
        "scripts": settings.feature_scripts,
        "ai_provider_default": settings.ai_provider,
    }


@router.get("/security/tenant-isolation")
async def tenant_isolation(
    session: AsyncSession = Depends(db_session),
    _: Principal = Depends(require_role(Role.admin)),
) -> dict:
    """Is row-level security switched on *and* actually enforced for this connection?

    Configured-but-bypassed is the dangerous state: policies exist, so it looks set
    up, while a superuser connection quietly ignores them.
    """
    from ..security.rls import can_bypass_rls

    if settings.is_sqlite:
        return {
            "enabled": settings.rls_enabled,
            "enforced": False,
            "detail": "SQLite has no row-level security; use Postgres in production",
        }
    if not settings.rls_enabled:
        return {
            "enabled": False,
            "enforced": False,
            "detail": "OSPREY_RLS_ENABLED is off; isolation relies on application scoping alone",
        }
    bypass = await can_bypass_rls(session)
    return {
        "enabled": True,
        "enforced": not bypass,
        "detail": (
            "the database role can bypass RLS (superuser or BYPASSRLS) — tenant "
            "isolation is NOT enforced; connect as an ordinary role"
            if bypass
            else "row-level security is enforced for this connection"
        ),
    }
