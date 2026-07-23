"""Connections: register a source account and ingest via the Forward-To fallback."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..connectors.base import registry
from ..connectors.service import collect_webhook, get_connector, to_view
from ..engine.ingest import ingest_events
from ..models import Connection, ConnectionStatus, Project, Role
from ..schemas import (
    ConnectionCreate,
    ConnectionOut,
    ExchangeRequest,
    ForwardEmail,
    SourceInfo,
)
from ..security import audit, crypto
from ..security.auth import Principal
from ..security.oauth import (
    AuthorizeChallenge,
    AuthorizeRequest,
    build_authorize_url,
    make_pkce,
    sign_state,
    verify_state,
)
from .deps import current_principal, db_session, require_role

router = APIRouter(prefix="/connections", tags=["connections"])


def _out(row: Connection) -> ConnectionOut:
    return ConnectionOut(
        id=row.id,
        project_id=row.project_id,
        source_type=row.source_type,
        account_ref=row.account_ref,
        status=row.status.value,
        scopes=list(row.scopes or []),
        last_sync=row.last_sync.isoformat() if row.last_sync else None,
    )


async def _project_or_404(session: AsyncSession, project_id: str, org_id: str) -> Project:
    project = await session.get(Project, project_id)
    if project is None or project.org_id != org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    return project


@router.get("/sources", response_model=list[SourceInfo])
async def list_sources(_: Principal = Depends(current_principal)) -> list[SourceInfo]:
    """Connectable sources + how each is authorized (drives the desktop UI)."""
    out: list[SourceInfo] = []
    for source_type in registry.types():
        connector = get_connector(source_type)
        spec = connector.oauth_spec()
        client_id = connector.client_credentials()[0]
        out.append(
            SourceInfo(
                source_type=source_type,
                auth=("oauth" if spec else ("forward" if connector.supports_webhooks else "internal")),
                scopes=list(spec.scopes) if spec else [],
                configured=bool(client_id) if spec else True,
            )
        )
    return out


@router.post("/authorize", response_model=AuthorizeChallenge)
async def authorize(
    body: AuthorizeRequest,
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(require_role(Role.admin)),
) -> AuthorizeChallenge:
    """Step 1 of the desktop OAuth flow: return the provider consent URL + signed state.

    The desktop app opens ``authorize_url`` in the user's system browser and listens
    on its loopback ``redirect_uri`` for the ``code``. No provider token ever passes
    through the AI/MCP layer.
    """
    if body.source_type not in registry.types():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown source_type: {body.source_type}")
    await _project_or_404(session, body.project_id, principal.org_id)

    connector = get_connector(body.source_type)
    spec = connector.oauth_spec()
    if spec is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{body.source_type} is not an OAuth source")
    client_id, _ = connector.client_credentials()
    if not client_id:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{body.source_type} OAuth app is not configured on the server",
        )

    verifier = challenge = None
    if spec.use_pkce:
        verifier, challenge = make_pkce()
    state = sign_state(
        {
            "org_id": principal.org_id,
            "project_id": body.project_id,
            "source_type": body.source_type,
            "redirect_uri": body.redirect_uri,
            "account_ref": body.account_ref,
            "cv": verifier,
        }
    )
    url = build_authorize_url(
        spec, client_id=client_id, redirect_uri=body.redirect_uri, state=state, code_challenge=challenge
    )
    return AuthorizeChallenge(authorize_url=url, state=state)


@router.post("/exchange", response_model=ConnectionOut, status_code=201)
async def exchange(
    body: ExchangeRequest,
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(require_role(Role.admin)),
) -> ConnectionOut:
    """Step 2: the desktop app relays the ``code``; the backend exchanges + seals tokens."""
    try:
        claims = verify_state(body.state)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid or expired state") from exc
    if claims.get("org_id") != principal.org_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "state does not match caller org")

    source_type = claims["source_type"]
    connector = get_connector(source_type)
    redirect_uri = body.redirect_uri or claims["redirect_uri"]
    tokens = await connector.exchange_code(body.code, redirect_uri, claims.get("cv"))
    account_ref = claims.get("account_ref") or await connector.account_ref_from_tokens(tokens)

    spec = connector.oauth_spec()
    row = Connection(
        org_id=principal.org_id,
        project_id=claims["project_id"],
        source_type=source_type,
        account_ref=account_ref,
        scopes=list(spec.scopes) if spec else [],
        encrypted_tokens=crypto.seal(tokens),
        status=ConnectionStatus.active,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session, org_id=principal.org_id, actor=principal.email,
        action="connection.oauth_connected", target=row.id, meta={"source_type": source_type},
    )
    return _out(row)


@router.post("", response_model=ConnectionOut, status_code=201)
async def create_connection(
    body: ConnectionCreate,
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(require_role(Role.admin)),
) -> ConnectionOut:
    if body.source_type not in registry.types():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown source_type: {body.source_type}")
    await _project_or_404(session, body.project_id, principal.org_id)

    row = Connection(
        org_id=principal.org_id,
        project_id=body.project_id,
        source_type=body.source_type,
        account_ref=body.account_ref,
        scopes=body.scopes,
        encrypted_tokens=crypto.seal(body.tokens) if body.tokens else "",
        status=ConnectionStatus.active,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session, org_id=principal.org_id, actor=principal.email,
        action="connection.created", target=row.id, meta={"source_type": row.source_type},
    )
    return _out(row)


@router.get("", response_model=list[ConnectionOut])
async def list_connections(
    project_id: str | None = None,
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> list[ConnectionOut]:
    stmt = select(Connection).where(Connection.org_id == principal.org_id)
    if project_id:
        stmt = stmt.where(Connection.project_id == project_id)
    rows = (await session.execute(stmt)).scalars().all()
    return [_out(r) for r in rows]


async def _load_connection(session: AsyncSession, connection_id: str, org_id: str) -> Connection:
    row = await session.get(Connection, connection_id)
    if row is None or row.org_id != org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "connection not found")
    return row


@router.post("/{connection_id}/forward", status_code=202)
async def forward_to_ingest(
    connection_id: str,
    body: ForwardEmail,
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(require_role(Role.pm)),
) -> dict:
    """Forward-To / File-Drop: parse an email or CSV into deduped Signals."""
    row = await _load_connection(session, connection_id, principal.org_id)
    connector = get_connector(row.source_type)
    if not connector.supports_webhooks:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{row.source_type} has no forward-to path")

    payload = {
        "kind": body.kind,
        "raw": body.raw,
        "external_id": body.external_id,
        "source_kind": body.source_kind,
    }
    events = await collect_webhook(connector, payload)
    created = await ingest_events(session, connector, row, events)
    await audit.record(
        session, org_id=principal.org_id, actor=principal.email,
        action="ingest.forward", target=row.id, meta={"parsed": len(events), "created": len(created)},
    )
    return {"parsed": len(events), "created": len(created), "signal_ids": [s.id for s in created]}


@router.get("/{connection_id}/health")
async def connection_health(
    connection_id: str,
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> dict:
    row = await _load_connection(session, connection_id, principal.org_id)
    connector = get_connector(row.source_type)
    health = await connector.healthcheck(to_view(row))
    return {"source_type": row.source_type, "ok": health.ok, "detail": health.detail}
