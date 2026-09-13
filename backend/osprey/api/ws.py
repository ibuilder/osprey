"""Live hotlist over WebSocket.

An in-process pub/sub hub broadcasts a project's hotlist to subscribed clients
whenever a new snapshot is built. For multi-process/HA deployments, swap the hub
for a Redis pub/sub backend behind the same ``publish`` interface.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..security.auth import decode_token

log = logging.getLogger("osprey.ws")
router = APIRouter()


class _Hub:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = {}

    def subscribe(self, project_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        self._subs.setdefault(project_id, set()).add(q)
        return q

    def unsubscribe(self, project_id: str, q: asyncio.Queue) -> None:
        subs = self._subs.get(project_id)
        if subs:
            subs.discard(q)
            if not subs:
                self._subs.pop(project_id, None)

    def publish(self, project_id: str, payload: dict) -> None:
        for q in list(self._subs.get(project_id, ())):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(payload)


hub = _Hub()


async def _authorize(token: str, project_id: str):
    """Verify the token and that this project belongs to the caller's tenant.

    A valid signature is not authorization. Two things are checked here that a
    bare ``decode_token`` cannot see:

    * the token has not been revoked -- the user still exists, is still active,
      and their ``token_version`` still matches, so deactivating somebody closes
      this door as well as the REST one;
    * the project is in the principal's org. Without this, any authenticated user
      of any tenant could subscribe to any project's live hotlist by knowing its
      id, which is precisely the boundary row-level security exists to hold.

    Returns the Principal, or None if the connection must be refused.
    """
    try:
        principal = decode_token(token)
    except Exception:  # noqa: BLE001
        return None

    from ..db import session_scope
    from ..models import Project, User
    from ..security.rls import set_current_org

    try:
        async with session_scope() as session:
            await set_current_org(session, principal.org_id)
            user = await session.get(User, principal.user_id)
            if user is None or not user.is_active:
                return None
            if user.token_version != principal.token_version:
                return None
            project = await session.get(Project, project_id)
            if project is None or project.org_id != principal.org_id:
                return None
    except Exception as exc:  # noqa: BLE001
        # Fail closed: if we cannot confirm authorization, we do not grant it.
        log.warning("websocket authorization check failed: %s", exc)
        return None
    return principal


@router.websocket("/ws/projects/{project_id}/hotlist")
async def hotlist_ws(websocket: WebSocket, project_id: str, token: str = "") -> None:
    # Authenticate via ?token= (WebSocket clients can't set Authorization easily).
    principal = await _authorize(token, project_id)
    if principal is None:
        # One close code for every refusal: distinguishing "bad token" from
        # "not your project" would let a caller enumerate project ids.
        await websocket.close(code=4401)
        return

    await websocket.accept()
    queue = hub.subscribe(project_id)
    await websocket.send_json(
        {"type": "connected", "project_id": project_id, "org_id": principal.org_id}
    )
    try:
        while True:
            payload = await queue.get()
            await websocket.send_json({"type": "hotlist", "payload": payload})
    except WebSocketDisconnect:
        pass
    finally:
        hub.unsubscribe(project_id, queue)
