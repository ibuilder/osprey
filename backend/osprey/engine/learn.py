"""Learning loop (SPEC §7.5).

User actions nudge per-project scoring weights: escalations amplify, dismissals
decay. Transparent and reversible — every nudge is driven by an ``Action`` row and
bounded, so weights can be recomputed or reset from history.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Action, ActionType, Item, ItemStatus, Project, utcnow
from .hotlist import project_weights
from .score import DEFAULT_WEIGHTS

_STEP = 0.02
_MIN, _MAX = 0.05, 0.80


def _clamp(v: float) -> float:
    return max(_MIN, min(_MAX, v))


def _nudge(weights: dict[str, float], direction: float) -> dict[str, float]:
    """Shift emphasis toward (direction>0) or away from urgency+impact, renormalized."""
    w = dict(weights)
    w["urgency"] = _clamp(w["urgency"] + direction * _STEP)
    w["impact"] = _clamp(w["impact"] + direction * _STEP)
    total = sum(w.values())
    return {k: round(v / total, 4) for k, v in w.items()}


_DIRECTION = {
    ActionType.escalate: +1.0,
    ActionType.dismiss: -1.0,
    ActionType.done: 0.0,
    ActionType.snooze: 0.0,
    ActionType.assign: 0.0,
    ActionType.reopen: 0.0,
}

_STATUS = {
    ActionType.done: ItemStatus.done,
    ActionType.dismiss: ItemStatus.dismissed,
    ActionType.snooze: ItemStatus.snoozed,
    ActionType.reopen: ItemStatus.open,
    ActionType.escalate: ItemStatus.open,
}


async def record_action(
    session: AsyncSession,
    *,
    item: Item,
    action_type: ActionType,
    user_id: str | None = None,
    meta: dict | None = None,
) -> Action:
    meta = meta or {}
    action = Action(
        item_id=item.id, project_id=item.project_id, user_id=user_id, type=action_type, meta=meta
    )
    session.add(action)

    # Apply item state transition.
    new_status = _STATUS.get(action_type)
    if new_status is not None:
        item.status = new_status
    if action_type == ActionType.snooze and meta.get("snooze_until"):
        item.snooze_until = meta["snooze_until"]
    if action_type == ActionType.assign and meta.get("owner"):
        item.owner = meta["owner"]
    item.updated_at = utcnow()
    session.add(item)

    # Nudge per-project weights.
    direction = _DIRECTION.get(action_type, 0.0)
    if direction != 0.0:
        project = await session.get(Project, item.project_id)
        if project is not None:
            current = project_weights(project)
            project.weights = {**{k: current.get(k, v) for k, v in DEFAULT_WEIGHTS.items()}}
            project.weights = _nudge(project.weights, direction)
            session.add(project)

    await session.flush()
    return action
