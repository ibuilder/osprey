"""Bridge between persisted Connections and the connector plugin layer."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Connection as ConnectionRow
from ..security import crypto
from .base import Connection as ConnView
from .base import Connector, RawEvent, SubscriptionState, registry

log = logging.getLogger(__name__)


def get_connector(source_type: str) -> Connector:
    return registry.get(source_type)


def to_view(row: ConnectionRow) -> ConnView:
    """Decrypt tokens (only in-memory, at call time) into a connector-facing view."""
    tokens: dict = {}
    if row.encrypted_tokens:
        try:
            tokens = crypto.open_sealed(row.encrypted_tokens)
        except Exception:  # noqa: BLE001 - treat undecryptable tokens as absent
            tokens = {}
    return ConnView(
        id=row.id,
        source_type=row.source_type,
        account_ref=row.account_ref,
        cursor=row.cursor,
        tokens=tokens,
        scopes=list(row.scopes or []),
    )


async def collect_webhook(connector: Connector, payload: dict) -> list[RawEvent]:
    return [ev async for ev in connector.handle_webhook(payload)]


# -- Webhook subscriptions ---------------------------------------------------- #


def subscription_urls(notify_base: str, row: ConnectionRow) -> tuple[str, str]:
    """The (notification, lifecycle) callback URLs for a connection."""
    base = notify_base.rstrip("/")
    return (
        f"{base}/webhooks/{row.source_type}?connection_id={row.id}",
        f"{base}/webhooks/{row.source_type}/lifecycle?connection_id={row.id}",
    )


def merge_tokens(row: ConnectionRow, updates: dict) -> None:
    """Re-seal the connection's tokens with ``updates`` applied.

    Reads through the same decrypt path as ``to_view`` so a connection whose
    tokens cannot be opened is not silently replaced with only the new keys —
    that would discard the user's refresh token.
    """
    tokens = crypto.open_sealed(row.encrypted_tokens) if row.encrypted_tokens else {}
    row.encrypted_tokens = crypto.seal(tokens | updates)


async def sync_subscription(
    session: AsyncSession, row: ConnectionRow, *, notify_base: str
) -> SubscriptionState | None:
    """Create/renew this connection's provider subscription and persist the result.

    Persisting is the point: ``ensure_subscription`` can only extend an existing
    subscription if the previous run's id was written back.
    """
    connector = get_connector(row.source_type)
    if not connector.supports_subscriptions:
        return None
    notify_url, lifecycle_url = subscription_urls(notify_base, row)
    state = await connector.ensure_subscription(to_view(row), notify_url, lifecycle_url)
    if state is None:
        return None
    merge_tokens(
        row,
        {"subscription_id": state.subscription_id, "client_state": state.client_state},
    )
    session.add(row)
    return state


async def handle_lifecycle(
    session: AsyncSession, row: ConnectionRow, events: list[str], *, notify_base: str
) -> dict:
    """React to provider subscription-lifecycle events.

    ``reauthorizationRequired`` and ``subscriptionRemoved`` mean the subscription
    is gone or about to be: re-subscribe. ``missed`` means the provider could not
    deliver some notifications, so the only way to recover those changes is to
    poll — which is safe to do because ingestion dedupes on ``external_id``.
    """
    from ..workers.tasks import poll_connection  # local: workers import this module

    actions: list[str] = []
    if {"reauthorizationRequired", "subscriptionRemoved"} & set(events):
        if "subscriptionRemoved" in events:
            # The subscription no longer exists, so renewing it would 404 —
            # clear the id to force a fresh subscribe.
            merge_tokens(row, {"subscription_id": ""})
        try:
            if await sync_subscription(session, row, notify_base=notify_base):
                actions.append("resubscribed")
        except Exception as exc:  # noqa: BLE001 - report, don't 500 at the provider
            log.warning("re-subscribe failed for connection %s: %s", row.id, exc)

    if "missed" in events:
        await session.flush()
        result = await poll_connection(session, row.id)
        actions.append(f"polled:{result.get('created', 0)}")

    return {"events": events, "actions": actions}
