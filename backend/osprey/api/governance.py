"""Data governance: retention policy, subject-access export, right-to-delete.

These are the endpoints behind the GDPR/CCPA posture SPEC §9 claims. All three
are owner-only, and erasure additionally requires the caller to type the org's
name -- there is no undo and no soft-delete tier behind it.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..engine import retention
from ..models import (
    Action,
    AuditLog,
    Connection,
    Item,
    Membership,
    Org,
    Project,
    Role,
    Score,
    Signal,
    User,
    utcnow,
)
from ..schemas import (
    DeletionRequest,
    DeletionStatus,
    PurgePreview,
    RetentionOut,
    RetentionPolicy,
)
from ..security import audit
from ..security.auth import Principal
from .deps import db_session, principal_during_deletion, require_role

log = logging.getLogger("osprey.governance")

router = APIRouter(prefix="/orgs/current", tags=["governance"])


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #
@router.get("/retention", response_model=RetentionOut)
async def get_retention(
    principal: Principal = Depends(require_role(Role.admin)),
    session: AsyncSession = Depends(db_session),
) -> RetentionOut:
    org = await session.get(Org, principal.org_id)
    signal_days, item_days = retention.effective_windows(org)
    return RetentionOut(
        signal_days=org.retention_signal_days if org else None,
        item_days=org.retention_item_days if org else None,
        effective_signal_days=signal_days,
        effective_item_days=item_days,
    )


@router.put("/retention", response_model=RetentionOut)
async def set_retention(
    body: RetentionPolicy,
    principal: Principal = Depends(require_role(Role.owner)),
    session: AsyncSession = Depends(db_session),
) -> RetentionOut:
    org = await session.get(Org, principal.org_id)
    if org is None:  # pragma: no cover - the principal proves the org exists
        raise HTTPException(status.HTTP_404_NOT_FOUND, "org not found")
    org.retention_signal_days = body.signal_days
    org.retention_item_days = body.item_days
    session.add(org)
    await audit.record(
        session,
        org_id=org.id,
        actor=principal.email,
        action="retention.updated",
        target=org.id,
        meta={"signal_days": body.signal_days, "item_days": body.item_days},
    )
    signal_days, item_days = retention.effective_windows(org)
    return RetentionOut(
        signal_days=org.retention_signal_days,
        item_days=org.retention_item_days,
        effective_signal_days=signal_days,
        effective_item_days=item_days,
    )


@router.get("/retention/preview", response_model=PurgePreview)
async def preview_purge(
    principal: Principal = Depends(require_role(Role.admin)),
    session: AsyncSession = Depends(db_session),
) -> PurgePreview:
    """What the next retention run would delete. Always available before the fact."""
    counts = await retention.preview_org(session, principal.org_id)
    return PurgePreview(**counts)


@router.post("/retention/run", response_model=dict)
async def run_purge(
    principal: Principal = Depends(require_role(Role.owner)),
    session: AsyncSession = Depends(db_session),
) -> dict[str, int]:
    """Apply the retention policy now instead of waiting for the scheduled run."""
    removed = await retention.purge_org(session, principal.org_id)
    await audit.record(
        session,
        org_id=principal.org_id,
        actor=principal.email,
        action="retention.purged",
        target=principal.org_id,
        meta=dict(removed),
    )
    return removed


# --------------------------------------------------------------------------- #
# Subject-access export
# --------------------------------------------------------------------------- #
@router.get("/export")
async def export_org(
    principal: Principal = Depends(require_role(Role.owner)),
    session: AsyncSession = Depends(db_session),
) -> Response:
    """Machine-readable export of everything this tenant holds (GDPR art. 20).

    Connector tokens are excluded by design. They are sealed OAuth credentials
    for third-party accounts, not personal data about the subject, and putting
    them in a file that leaves the server would undo the whole point of the
    vault. Their *existence* is reported; their contents are not.
    """
    org = await session.get(Org, principal.org_id)
    if org is None:  # pragma: no cover
        raise HTTPException(status.HTTP_404_NOT_FOUND, "org not found")

    project_ids = list(
        (await session.execute(select(Project.id).where(Project.org_id == org.id))).scalars().all()
    )
    item_ids = (
        list(
            (await session.execute(select(Item.id).where(Item.project_id.in_(project_ids))))
            .scalars()
            .all()
        )
        if project_ids
        else []
    )

    payload: dict[str, Any] = {
        "exported_at": utcnow().isoformat(),
        "format_version": 1,
        "org": _row(org),
        "members": await _members(session, org.id),
        "projects": await _rows(session, select(Project).where(Project.org_id == org.id)),
        "connections": await _connections(session, org.id),
        "signals": await _rows(session, select(Signal).where(Signal.project_id.in_(project_ids)))
        if project_ids
        else [],
        "items": await _rows(session, select(Item).where(Item.project_id.in_(project_ids)))
        if project_ids
        else [],
        "scores": await _rows(session, select(Score).where(Score.item_id.in_(item_ids)))
        if item_ids
        else [],
        "actions": await _rows(session, select(Action).where(Action.item_id.in_(item_ids)))
        if item_ids
        else [],
        "audit_log": await _rows(session, select(AuditLog).where(AuditLog.org_id == org.id)),
    }

    await audit.record(
        session,
        org_id=org.id,
        actor=principal.email,
        action="org.exported",
        target=org.id,
        meta={"projects": len(project_ids), "items": len(item_ids)},
    )
    body = json.dumps(payload, indent=2, default=str)
    filename = f"osprey-export-{org.id}.json"
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _members(session: AsyncSession, org_id: str) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(Membership.org_id == org_id)
        )
    ).all()
    return [
        {
            "user_id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "role": membership.role.value,
            "is_active": user.is_active,
            "scim_managed": user.scim_managed,
            "created_at": str(user.created_at),
            "last_login_at": str(user.last_login_at) if user.last_login_at else None,
        }
        for membership, user in rows
    ]


async def _connections(session: AsyncSession, org_id: str) -> list[dict[str, Any]]:
    rows = (
        (await session.execute(select(Connection).where(Connection.org_id == org_id)))
        .scalars()
        .all()
    )
    out = []
    for row in rows:
        record = _row(row)
        # Never export the vault. Report only that a credential is held.
        sealed = record.pop("encrypted_tokens", "")
        record["has_credentials"] = bool(sealed)
        out.append(record)
    return out


def _row(model) -> dict[str, Any]:
    return dict(model.model_dump())


async def _rows(session: AsyncSession, statement) -> list[dict[str, Any]]:
    return [_row(r) for r in (await session.execute(statement)).scalars().all()]


# --------------------------------------------------------------------------- #
# Right-to-delete
# --------------------------------------------------------------------------- #
@router.post("/delete", response_model=DeletionStatus)
async def delete_org(
    body: DeletionRequest,
    response: Response,
    principal: Principal = Depends(require_role(Role.owner)),
    session: AsyncSession = Depends(db_session),
) -> DeletionStatus:
    """Erase this tenant and everything in it. There is no undo.

    By default deletion runs inline rather than as a queued job: it is one
    transaction, so a failure rolls back cleanly, and a self-hosted deployment
    cannot be assumed to have a worker running at all.

    A tenant larger than ``erasure_inline_max_rows`` is instead queued: the
    ``deletion_requested_at`` flag is committed, the call returns 202, and the
    worker drains the tenant in bounded batches. While the flag is set every
    authenticated request is refused with 423, and pollers and webhooks ignore
    the tenant, so nothing new arrives while it is being removed.
    """
    org = await session.get(Org, principal.org_id)
    if org is None:  # pragma: no cover
        raise HTTPException(status.HTTP_404_NOT_FOUND, "org not found")
    if body.confirm_org_name.strip() != org.name:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "confirm_org_name does not match this organization's name",
        )

    org_id, org_name = org.id, org.name
    requested_at = utcnow()
    org.deletion_requested_at = requested_at
    session.add(org)
    await session.flush()

    limit = settings.erasure_inline_max_rows
    if limit > 0:
        rows = await retention.count_org_rows(session, org_id)
        if rows > limit:
            log.warning(
                "erasure queued for org %s (%s) by %s: %d rows exceeds the inline limit of %d",
                org_id,
                org_name,
                principal.email,
                rows,
                limit,
            )
            response.status_code = status.HTTP_202_ACCEPTED
            return DeletionStatus(
                org_id=org_id, requested_at=requested_at.isoformat(), completed=False
            )

    log.warning("erasure requested for org %s (%s) by %s", org_id, org_name, principal.email)
    removed = await retention.erase_org(session, org_id)
    return DeletionStatus(
        org_id=org_id,
        requested_at=utcnow().isoformat(),
        completed=True,
        deleted_rows=removed,
    )


@router.get("/deletion-status", response_model=DeletionStatus)
async def deletion_status(
    principal: Principal = Depends(principal_during_deletion),
    session: AsyncSession = Depends(db_session),
) -> DeletionStatus:
    org = await session.get(Org, principal.org_id)
    if org is None:
        return DeletionStatus(org_id=principal.org_id, completed=True)
    return DeletionStatus(
        org_id=org.id,
        requested_at=org.deletion_requested_at.isoformat() if org.deletion_requested_at else None,
        completed=False,
    )


@router.get("/settings", response_model=dict)
async def org_settings(
    principal: Principal = Depends(require_role(Role.admin)),
    session: AsyncSession = Depends(db_session),
) -> dict[str, Any]:
    """Effective governance posture, for the admin console."""
    org = await session.get(Org, principal.org_id)
    signal_days, item_days = retention.effective_windows(org)
    return {
        "org_id": principal.org_id,
        "org_name": org.name if org else "",
        "retention": {"signal_days": signal_days, "item_days": item_days},
        "sso_enabled": settings.oidc_enabled,
        "scim_enabled": settings.scim_enabled,
        "rls_enabled": settings.rls_enabled,
        "audit": await audit.verify_chain_detail(session, principal.org_id),
    }
