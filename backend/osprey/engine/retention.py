"""Data retention and tenant erasure (SPEC §9, "data-retention + right-to-delete").

Two operations:

``purge_expired``
    Age-out. Runs on a schedule, honours a per-tenant override, and deletes in
    dependency order so no foreign key is ever left dangling.

``erase_org``
    Right-to-delete. Removes a whole tenant.

The audit log is treated differently from everything else and is *not* aged out
by default. It is hash-chained: deleting a middle record breaks verification for
every record after it, so `verify_chain` would start reporting tamper for a
routine cleanup. Purging audit history is therefore opt-in
(``OSPREY_RETENTION_AUDIT_DAYS``) and truncates a *prefix* only -- the oldest
records, in order -- which leaves the remaining chain verifiable from its new
root. Tenant erasure drops the chain outright, which is the point of erasure.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from ..config import settings
from ..models import (
    Action,
    AIConnection,
    AuditLog,
    Connection,
    Device,
    HotlistSnapshot,
    Invite,
    Item,
    Membership,
    Org,
    Project,
    RefreshToken,
    ScimToken,
    Score,
    ScriptTask,
    Signal,
    User,
    utcnow,
)

log = logging.getLogger("osprey.retention")


def _cutoff(days: int) -> datetime | None:
    """The instant before which rows are expired. None means keep forever."""
    if days <= 0:
        return None
    return utcnow() - timedelta(days=days)


def effective_windows(org: Org | None) -> tuple[int, int]:
    """(signal_days, item_days) for a tenant, applying its override if set."""
    signal_days = settings.retention_signal_days
    item_days = settings.retention_item_days
    if org is not None:
        if org.retention_signal_days is not None:
            signal_days = org.retention_signal_days
        if org.retention_item_days is not None:
            item_days = org.retention_item_days
    return signal_days, item_days


def _naive_if_needed(cutoff: datetime) -> datetime:
    """SQLite stores naive datetimes; compare in one convention."""
    return cutoff.replace(tzinfo=None) if settings.is_sqlite else cutoff


async def preview_org(session: AsyncSession, org_id: str) -> dict[str, int | str | None]:
    """Count what a purge would remove for one tenant, without removing it."""
    org = await session.get(Org, org_id)
    signal_days, item_days = effective_windows(org)
    project_ids = await _project_ids(session, org_id)
    out: dict[str, int | str | None] = {
        "signals": 0,
        "items": 0,
        "scores": 0,
        "snapshots": 0,
        "cutoff_signal": None,
        "cutoff_item": None,
    }
    if not project_ids:
        return out

    signal_cutoff = _cutoff(signal_days)
    if signal_cutoff is not None:
        out["cutoff_signal"] = signal_cutoff.isoformat()
        out["signals"] = await _count(
            session,
            select(func.count())
            .select_from(Signal)
            .where(
                Signal.project_id.in_(project_ids),
                Signal.ingested_at < _naive_if_needed(signal_cutoff),
            ),
        )

    item_cutoff = _cutoff(item_days)
    if item_cutoff is not None:
        out["cutoff_item"] = item_cutoff.isoformat()
        stale = await _stale_item_ids(session, project_ids, item_cutoff)
        out["items"] = len(stale)
        if stale:
            out["scores"] = await _count(
                session,
                select(func.count()).select_from(Score).where(Score.item_id.in_(stale)),
            )
        out["snapshots"] = await _count(
            session,
            select(func.count())
            .select_from(HotlistSnapshot)
            .where(
                HotlistSnapshot.project_id.in_(project_ids),
                HotlistSnapshot.created_at < _naive_if_needed(item_cutoff),
            ),
        )
    return out


async def purge_org(session: AsyncSession, org_id: str) -> dict[str, int]:
    """Delete this tenant's expired rows. Returns per-table counts."""
    org = await session.get(Org, org_id)
    signal_days, item_days = effective_windows(org)
    project_ids = await _project_ids(session, org_id)
    removed = {"signals": 0, "items": 0, "scores": 0, "actions": 0, "snapshots": 0, "audit": 0}
    if not project_ids:
        return removed

    item_cutoff = _cutoff(item_days)
    if item_cutoff is not None:
        stale = await _stale_item_ids(session, project_ids, item_cutoff)
        if stale:
            # Children first: Score and Action both reference Item.
            removed["scores"] = await _delete(
                session, delete(Score).where(Score.item_id.in_(stale))
            )
            removed["actions"] = await _delete(
                session, delete(Action).where(Action.item_id.in_(stale))
            )
            # Signals point at the item they were clustered into; detach rather
            # than delete, so a signal inside its own retention window survives
            # the disappearance of the item it fed.
            await session.execute(
                Signal.__table__.update().where(col(Signal.item_id).in_(stale)).values(item_id=None)
            )
            removed["items"] = await _delete(session, delete(Item).where(Item.id.in_(stale)))
        removed["snapshots"] = await _delete(
            session,
            delete(HotlistSnapshot).where(
                HotlistSnapshot.project_id.in_(project_ids),
                HotlistSnapshot.created_at < _naive_if_needed(item_cutoff),
            ),
        )

    signal_cutoff = _cutoff(signal_days)
    if signal_cutoff is not None:
        removed["signals"] = await _delete(
            session,
            delete(Signal).where(
                Signal.project_id.in_(project_ids),
                Signal.ingested_at < _naive_if_needed(signal_cutoff),
            ),
        )

    removed["audit"] = await _purge_audit_prefix(session, org_id)
    if any(removed.values()):
        log.info("retention purge for org %s removed %s", org_id, removed)
    return removed


async def purge_expired(session: AsyncSession) -> dict[str, int]:
    """Purge every tenant. The scheduled entry point."""
    totals: dict[str, int] = {}
    org_ids = (await session.execute(select(Org.id))).scalars().all()
    for org_id in org_ids:
        for table, count in (await purge_org(session, org_id)).items():
            totals[table] = totals.get(table, 0) + count
    # Retired refresh tokens are pure housekeeping, not tenant data.
    from ..security.sessions import purge_expired as purge_sessions

    totals["sessions"] = await purge_sessions(session)
    return totals


async def _purge_audit_prefix(session: AsyncSession, org_id: str) -> int:
    """Drop the oldest audit records, keeping the surviving chain verifiable.

    Deleting from the middle would break every subsequent hash. Deleting a
    contiguous prefix only means the remaining records still chain correctly to
    each other; ``verify_chain`` is written to accept a chain whose first record
    has a non-empty ``prev_hash`` for exactly this reason.
    """
    cutoff = _cutoff(settings.retention_audit_days)
    if cutoff is None:
        return 0
    rows = (
        (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.org_id == org_id)
                .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
            )
        )
        .scalars()
        .all()
    )
    limit = _naive_if_needed(cutoff)
    doomed: list[str] = []
    for row in rows:
        created = row.created_at
        if created is None or created >= limit:
            break  # prefix only: stop at the first record we are keeping
        doomed.append(row.id)
    if not doomed:
        return 0
    await session.execute(delete(AuditLog).where(AuditLog.id.in_(doomed)))
    return len(doomed)


async def erase_org(session: AsyncSession, org_id: str) -> dict[str, int]:
    """Delete a tenant and everything belonging to it. Irreversible.

    Users are deleted only when this was their *last* membership; someone who
    also belongs to another tenant on the same instance keeps their account.
    """
    project_ids = await _project_ids(session, org_id)
    item_ids: list[str] = []
    if project_ids:
        item_ids = list(
            (await session.execute(select(Item.id).where(Item.project_id.in_(project_ids))))
            .scalars()
            .all()
        )

    removed: dict[str, int] = {}
    if item_ids:
        removed["scores"] = await _delete(session, delete(Score).where(Score.item_id.in_(item_ids)))
        removed["actions"] = await _delete(
            session, delete(Action).where(Action.item_id.in_(item_ids))
        )
    if project_ids:
        removed["signals"] = await _delete(
            session, delete(Signal).where(Signal.project_id.in_(project_ids))
        )
        removed["items"] = await _delete(
            session, delete(Item).where(Item.project_id.in_(project_ids))
        )
        removed["snapshots"] = await _delete(
            session, delete(HotlistSnapshot).where(HotlistSnapshot.project_id.in_(project_ids))
        )
        removed["scripts"] = await _delete(
            session, delete(ScriptTask).where(ScriptTask.project_id.in_(project_ids))
        )

    removed["connections"] = await _delete(
        session, delete(Connection).where(Connection.org_id == org_id)
    )
    removed["ai_connections"] = await _delete(
        session, delete(AIConnection).where(AIConnection.org_id == org_id)
    )
    removed["devices"] = await _delete(session, delete(Device).where(Device.org_id == org_id))
    removed["projects"] = await _delete(session, delete(Project).where(Project.org_id == org_id))
    removed["refresh_tokens"] = await _delete(
        session, delete(RefreshToken).where(RefreshToken.org_id == org_id)
    )
    removed["scim_tokens"] = await _delete(
        session, delete(ScimToken).where(ScimToken.org_id == org_id)
    )
    removed["invites"] = await _delete(session, delete(Invite).where(Invite.org_id == org_id))
    removed["audit"] = await _delete(session, delete(AuditLog).where(AuditLog.org_id == org_id))

    member_ids = list(
        (await session.execute(select(Membership.user_id).where(Membership.org_id == org_id)))
        .scalars()
        .all()
    )
    removed["memberships"] = await _delete(
        session, delete(Membership).where(Membership.org_id == org_id)
    )
    orphans: list[str] = []
    for user_id in member_ids:
        remaining = await _count(
            session,
            select(func.count()).select_from(Membership).where(Membership.user_id == user_id),
        )
        if remaining == 0:
            orphans.append(user_id)
    if orphans:
        removed["users"] = await _delete(session, delete(User).where(User.id.in_(orphans)))

    removed["orgs"] = await _delete(session, delete(Org).where(Org.id == org_id))
    log.warning("erased org %s: %s", org_id, removed)
    return {k: v for k, v in removed.items() if v}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _project_ids(session: AsyncSession, org_id: str) -> list[str]:
    return list(
        (await session.execute(select(Project.id).where(Project.org_id == org_id))).scalars().all()
    )


async def _stale_item_ids(
    session: AsyncSession, project_ids: list[str], cutoff: datetime
) -> list[str]:
    """Items past the window that are no longer live work.

    Two rules, both deliberate:

    * Age is measured from ``updated_at``, not ``created_at``. An item opened a
      year ago and actioned last week is not stale.
    * An open item is never purged however old it is: an unanswered notice
      deadline is exactly the thing this product exists to keep in front of
      somebody, and quietly deleting it would be the worst possible failure.
    """
    from ..models import ItemStatus

    return list(
        (
            await session.execute(
                select(Item.id).where(
                    Item.project_id.in_(project_ids),
                    Item.updated_at < _naive_if_needed(cutoff),
                    Item.status != ItemStatus.open,
                )
            )
        )
        .scalars()
        .all()
    )


async def _count(session: AsyncSession, statement) -> int:
    return int((await session.execute(statement)).scalar_one() or 0)


async def _delete(session: AsyncSession, statement) -> int:
    return int((await session.execute(statement)).rowcount or 0)
