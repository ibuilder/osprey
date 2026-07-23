"""Provider webhooks — signature-verified, idempotent ingestion (SPEC §6, §9)."""

from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..connectors.base import registry
from ..connectors.service import collect_webhook, get_connector
from ..engine.ingest import ingest_events
from ..models import Connection
from .deps import db_session

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _valid_signature(raw: bytes, signature: str) -> bool:
    expected = hmac.new(settings.webhook_hmac_secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, (signature or "").removeprefix("sha256="))


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

    if source_type not in registry.types():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown source_type: {source_type}")

    raw = await request.body()
    signature = request.headers.get("X-Osprey-Signature", "")
    if not _valid_signature(raw, signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid webhook signature")

    if not connection_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "connection_id is required")
    connection = await session.get(Connection, connection_id)
    if connection is None or connection.source_type != source_type:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "connection not found for source_type")

    payload = json.loads(raw or b"{}")
    connector = get_connector(source_type)
    events = await collect_webhook(connector, payload)
    created = await ingest_events(session, connector, connection, events)
    return Response(
        content=json.dumps({"parsed": len(events), "created": len(created)}),
        media_type="application/json",
        status_code=status.HTTP_202_ACCEPTED,
    )
