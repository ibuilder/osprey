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


@router.websocket("/ws/projects/{project_id}/hotlist")
async def hotlist_ws(websocket: WebSocket, project_id: str, token: str = "") -> None:
    # Authenticate via ?token= (WebSocket clients can't set Authorization easily).
    try:
        principal = decode_token(token)
    except Exception:  # noqa: BLE001
        await websocket.close(code=4401)
        return

    await websocket.accept()
    queue = hub.subscribe(project_id)
    await websocket.send_json({"type": "connected", "project_id": project_id, "org_id": principal.org_id})
    try:
        while True:
            payload = await queue.get()
            await websocket.send_json({"type": "hotlist", "payload": payload})
    except WebSocketDisconnect:
        pass
    finally:
        hub.unsubscribe(project_id, queue)
