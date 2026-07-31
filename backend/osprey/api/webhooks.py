"""Provider webhooks — authenticated, idempotent ingestion (SPEC §6, §9)."""

from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..connectors.base import Connector, registry
from ..connectors.service import (
    collect_webhook,
    get_connector,
    handle_lifecycle,
    to_view,
)
from ..engine.ingest import ingest_events
from ..models import Connection
from .deps import db_session

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _valid_signature(raw: bytes, signature: str) -> bool:
    expected = hmac.new(settings.webhook_hmac_secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, (signature or "").removeprefix("sha256="))


def _authenticate(
    connector: Connector, connection: Connection, raw: bytes, payload: dict, signature: str
) -> bool:
    """Verify a callback using whichever scheme the source actually supports.

    Providers that sign nothing (Microsoft Graph) authenticate with the
    clientState secret Osprey handed them at subscribe time; everything else
    uses Osprey's own HMAC.
    """
    if connector.webhook_auth != "client_state":
        return _valid_signature(raw, signature)

    expected = to_view(connection).tokens.get("client_state", "")
    presented = connector.webhook_client_state(payload)
    if not expected or not presented:
        return False
    return hmac.compare_digest(expected, presented)


async def _load(source_type: str, connection_id: str | None, session: AsyncSession) -> Connection:
    if source_type not in registry.types():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown source_type: {source_type}")
    if not connection_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "connection_id is required")
    connection = await session.get(Connection, connection_id)
    if connection is None or connection.source_type != source_type:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "connection not found for source_type")
    return connection


@router.post("/{source_type}")
async def receive_webhook(
    source_type: str,
    request: Request,
    connection_id: str | None = None,
    validationToken: str | None = None,  # MS Graph subscription handshake
    session: AsyncSession = Depends(db_session),
) -> Response:
    # 1) Subscription-validation handshake (Graph): echo the token as plain text.
    if validationToken is not None:
        return Response(content=validationToken, media_type="text/plain")

    connection = await _load(source_type, connection_id, session)
    raw = await request.body()
    payload = json.loads(raw or b"{}")
    connector = get_connector(source_type)

    if not _authenticate(
        connector, connection, raw, payload, request.headers.get("X-Osprey-Signature", "")
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "webhook failed authentication")

    # A provider may post lifecycle events to the notification URL too, so check
    # here as well as on the dedicated endpoint.
    events = connector.lifecycle_events(payload)
    if events:
        result = await handle_lifecycle(
            session, connection, events, notify_base=settings.public_base_url
        )
        return Response(
            content=json.dumps(result),
            media_type="application/json",
            status_code=status.HTTP_202_ACCEPTED,
        )

    raw_events = await collect_webhook(connector, payload)
    created = await ingest_events(session, connector, connection, raw_events)
    return Response(
        content=json.dumps({"parsed": len(raw_events), "created": len(created)}),
        media_type="application/json",
        status_code=status.HTTP_202_ACCEPTED,
    )


@router.post("/{source_type}/lifecycle")
async def receive_lifecycle(
    source_type: str,
    request: Request,
    connection_id: str | None = None,
    validationToken: str | None = None,
    session: AsyncSession = Depends(db_session),
) -> Response:
    """Subscription-lifecycle callbacks (Microsoft Graph).

    Graph sends ``reauthorizationRequired``, ``subscriptionRemoved`` and
    ``missed`` here rather than to the notification URL. Without this endpoint a
    subscription can lapse — or drop notifications — and the connection just goes
    quiet, still reporting itself healthy.
    """
    if validationToken is not None:
        return Response(content=validationToken, media_type="text/plain")

    connection = await _load(source_type, connection_id, session)
    raw = await request.body()
    payload = json.loads(raw or b"{}")
    connector = get_connector(source_type)

    if not _authenticate(
        connector, connection, raw, payload, request.headers.get("X-Osprey-Signature", "")
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "webhook failed authentication")

    events = connector.lifecycle_events(payload)
    if not events:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no lifecycle events in payload")

    result = await handle_lifecycle(
        session, connection, events, notify_base=settings.public_base_url
    )
    return Response(
        content=json.dumps(result),
        media_type="application/json",
        status_code=status.HTTP_202_ACCEPTED,
    )
