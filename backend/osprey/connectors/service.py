"""Bridge between persisted Connections and the connector plugin layer."""

from __future__ import annotations

from ..models import Connection as ConnectionRow
from ..security import crypto
from .base import Connection as ConnView
from .base import Connector, RawEvent, registry


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
