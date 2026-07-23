"""Push notifications for critical (Act today) hotlist items (SPEC Phase 5).

A pluggable ``PushSender`` abstracts APNs (iOS), FCM (Android), and Web Push. The
default logs (safe offline default); production wires real senders behind the same
interface. ``notify_critical`` fans a snapshot's 🔴 items out to the org's devices.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Device

log = logging.getLogger("osprey.notify")


class PushMessage:
    def __init__(self, title: str, body: str, data: dict) -> None:
        self.title = title
        self.body = body
        self.data = data


class PushSender(ABC):
    @abstractmethod
    async def send(self, device: Device, message: PushMessage) -> bool: ...


class LoggingPushSender(PushSender):
    """Offline default — records intent without contacting a push service."""

    async def send(self, device: Device, message: PushMessage) -> bool:
        log.info("PUSH[%s] -> %s: %s", device.platform, device.token[:8], message.title)
        return True


_sender: PushSender = LoggingPushSender()


def set_sender(sender: PushSender) -> None:
    global _sender
    _sender = sender


def critical_items(payload: dict) -> list[dict]:
    """The 🔴 Act-today items from a hotlist snapshot payload."""
    return [it for it in payload.get("items", []) if it.get("bucket") == "act_today"]


async def notify_critical(session: AsyncSession, *, org_id: str, payload: dict, limit: int = 5) -> int:
    """Push each critical item to every device in the org. Returns pushes sent."""
    items = critical_items(payload)[:limit]
    if not items:
        return 0
    devices = (
        await session.execute(select(Device).where(Device.org_id == org_id))
    ).scalars().all()
    sent = 0
    for item in items:
        msg = PushMessage(
            title=f"🔴 {item.get('what', 'Act today')}",
            body=item.get("recommended_action") or item.get("why", ""),
            data={"item_id": item.get("item_id"), "score": item.get("score")},
        )
        for device in devices:
            if await _sender.send(device, msg):
                sent += 1
    return sent
