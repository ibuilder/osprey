"""SCIM 2.0 user provisioning (RFC 7643 / 7644).

Scope is deliberate: ``/Users`` is implemented, ``/Groups`` is not. Okta, Entra
ID, and JumpCloud can all drive user lifecycle -- create, update, deactivate,
delete -- against ``/Users`` alone, and mapping IdP groups onto Osprey's four
fixed roles would be a guess. Role comes from the ``roles`` attribute when the
IdP sends one and from the token's default otherwise. ``/Groups`` returns 501
rather than 404, so a connector probing for it gets an honest answer.

Authentication is a per-org bearer token (``ScimToken``), not a user JWT: an IdP
connector has no interactive session to refresh. Each token carries a ``max_role``
ceiling, because a leaked provisioning credential that could mint owners would be
a full tenant takeover.

Deactivation (``active: false``) is a soft delete and ``DELETE`` is mapped to the
same thing. An IdP deprovision must not erase the audit trail of what that user
did, and SCIM's own guidance permits either reading.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Membership, Role, ScimToken, User, utcnow
from ..schemas import ScimTokenCreate, ScimTokenOut
from ..security import audit, rbac, sessions
from ..security.auth import Principal, hash_secret
from ..security.rls import set_current_org
from .deps import db_session, require_role

log = logging.getLogger("osprey.scim")

router = APIRouter(prefix="/scim/v2", tags=["scim"])

_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
_PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

#: SCIM mandates this content type; several connectors reject anything else.
_SCIM_MEDIA_TYPE = "application/scim+json"


class ScimError(HTTPException):
    """An error shaped the way a SCIM connector expects to parse it."""

    def __init__(self, status_code: int, detail: str, scim_type: str = "") -> None:
        body: dict[str, Any] = {
            "schemas": [_ERROR_SCHEMA],
            "detail": detail,
            "status": str(status_code),
        }
        if scim_type:
            body["scimType"] = scim_type
        super().__init__(status_code, body)


# --------------------------------------------------------------------------- #
# Token authentication
# --------------------------------------------------------------------------- #
async def scim_context(
    authorization: str = Header(default=""),
    session: AsyncSession = Depends(db_session),
) -> ScimToken:
    if not settings.scim_enabled:
        raise ScimError(status.HTTP_404_NOT_FOUND, "SCIM provisioning is not enabled")
    if not authorization.lower().startswith("bearer "):
        raise ScimError(status.HTTP_401_UNAUTHORIZED, "missing bearer token")

    presented = authorization.split(" ", 1)[1].strip()
    token = (
        await session.execute(
            select(ScimToken).where(ScimToken.token_hash == hash_secret(presented))
        )
    ).scalar_one_or_none()
    if token is None or token.revoked_at is not None:
        raise ScimError(status.HTTP_401_UNAUTHORIZED, "invalid provisioning token")

    token.last_used_at = utcnow()
    session.add(token)
    # RLS is bound from the token's org, since there is no JWT for db_session to read.
    await set_current_org(session, token.org_id)
    return token


# --------------------------------------------------------------------------- #
# Representation
# --------------------------------------------------------------------------- #
def _to_scim(user: User, membership: Membership, request: Request) -> dict[str, Any]:
    location = str(request.url_for("scim_get_user", user_id=user.id))
    given, _, family = (user.full_name or "").partition(" ")
    return {
        "schemas": [_USER_SCHEMA],
        "id": user.id,
        "externalId": user.external_id or None,
        "userName": user.email,
        "name": {
            "formatted": user.full_name,
            "givenName": given,
            "familyName": family,
        },
        "displayName": user.full_name or user.email,
        "emails": [{"value": user.email, "primary": True, "type": "work"}],
        "roles": [{"value": membership.role.value, "primary": True}],
        "active": user.is_active,
        "meta": {
            "resourceType": "User",
            "created": user.created_at.isoformat() if user.created_at else None,
            "lastModified": user.updated_at.isoformat() if user.updated_at else None,
            "location": location,
        },
    }


def _requested_role(payload: dict[str, Any], token: ScimToken) -> Role:
    """Role from the IdP's ``roles`` attribute, clamped to the token's ceiling."""
    raw = payload.get("roles") or []
    value = ""
    if isinstance(raw, list) and raw:
        first = raw[0]
        value = str(first.get("value", "") if isinstance(first, dict) else first)
    try:
        requested = Role(value.strip().lower()) if value else token.max_role
    except ValueError:
        # An unmappable IdP role must not silently become an owner; fall back to
        # the ceiling, which an administrator chose deliberately.
        log.info("SCIM sent unmappable role %r; using the token default", value)
        requested = token.max_role
    if not rbac.satisfies(token.max_role, requested):
        raise ScimError(
            status.HTTP_403_FORBIDDEN,
            f"this provisioning token cannot grant '{requested.value}'",
            scim_type="mutability",
        )
    return requested


def _email_of(payload: dict[str, Any]) -> str:
    user_name = str(payload.get("userName") or "").strip().lower()
    if "@" in user_name:
        return user_name
    entries = [e for e in (payload.get("emails") or []) if isinstance(e, dict) and e.get("value")]
    # Prefer the address the IdP marked primary; fall back to the first it sent.
    for entry in sorted(entries, key=lambda e: not e.get("primary", False)):
        return str(entry["value"]).strip().lower()
    raise ScimError(
        status.HTTP_400_BAD_REQUEST, "userName must be an email address", scim_type="invalidValue"
    )


def _full_name(payload: dict[str, Any]) -> str:
    name = payload.get("name") or {}
    if isinstance(name, dict):
        formatted = str(name.get("formatted") or "").strip()
        if formatted:
            return formatted
        parts = [str(name.get("givenName") or ""), str(name.get("familyName") or "")]
        joined = " ".join(p for p in parts if p).strip()
        if joined:
            return joined
    return str(payload.get("displayName") or "").strip()


def _scim_response(body: dict[str, Any], status_code: int = 200) -> Response:
    import json

    return Response(
        content=json.dumps(body),
        status_code=status_code,
        media_type=_SCIM_MEDIA_TYPE,
    )


# --------------------------------------------------------------------------- #
# /Users
# --------------------------------------------------------------------------- #
@router.get("/Users")
async def scim_list_users(
    request: Request,
    filter: str = "",  # noqa: A002 - the SCIM parameter is literally named "filter"
    startIndex: int = 1,  # noqa: N803 - SCIM uses camelCase query parameters
    count: int = 100,
    token: ScimToken = Depends(scim_context),
    session: AsyncSession = Depends(db_session),
) -> Response:
    """List members. Supports the one filter every IdP actually sends:
    ``userName eq "someone@example.com"``."""
    count = max(1, min(count, 200))
    start = max(1, startIndex)

    query = (
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.org_id == token.org_id)
    )
    email = _parse_username_filter(filter)
    if email:
        query = query.where(func.lower(User.email) == email)

    rows = (await session.execute(query)).all()
    page = rows[start - 1 : start - 1 + count]
    return _scim_response(
        {
            "schemas": [_LIST_SCHEMA],
            "totalResults": len(rows),
            "startIndex": start,
            "itemsPerPage": len(page),
            "Resources": [_to_scim(user, m, request) for m, user in page],
        }
    )


def _parse_username_filter(expression: str) -> str:
    """Extract the value from ``userName eq "x"``. Anything else is ignored.

    Returning everything for an unsupported filter is the lenient reading, and
    the safe one here: the alternative -- erroring -- breaks an IdP's reconcile
    sweep entirely, whereas an over-broad list is filtered by the connector.
    """
    if not expression:
        return ""
    parts = expression.strip().split(None, 2)
    if len(parts) == 3 and parts[0].lower() == "username" and parts[1].lower() == "eq":
        return parts[2].strip().strip('"').lower()
    return ""


@router.get("/Users/{user_id}", name="scim_get_user")
async def scim_get_user(
    user_id: str,
    request: Request,
    token: ScimToken = Depends(scim_context),
    session: AsyncSession = Depends(db_session),
) -> Response:
    membership, user = await _scim_member(session, token.org_id, user_id)
    return _scim_response(_to_scim(user, membership, request))


@router.post("/Users", status_code=status.HTTP_201_CREATED)
async def scim_create_user(
    payload: dict[str, Any],
    request: Request,
    token: ScimToken = Depends(scim_context),
    session: AsyncSession = Depends(db_session),
) -> Response:
    email = _email_of(payload)
    role = _requested_role(payload, token)

    user = (
        await session.execute(select(User).where(func.lower(User.email) == email))
    ).scalar_one_or_none()
    if user is not None:
        existing = (
            await session.execute(
                select(Membership).where(
                    Membership.org_id == token.org_id, Membership.user_id == user.id
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            # RFC 7644 §3.3: a duplicate is 409 with scimType "uniqueness".
            raise ScimError(status.HTTP_409_CONFLICT, "user already exists", scim_type="uniqueness")
    else:
        user = User(
            email=email,
            full_name=_full_name(payload),
            password_hash="",  # provisioned users sign in through the IdP
        )
        session.add(user)
        await session.flush()

    user.scim_managed = True
    user.external_id = str(payload.get("externalId") or "")
    user.is_active = bool(payload.get("active", True))
    user.updated_at = utcnow()
    session.add(user)

    membership = Membership(org_id=token.org_id, user_id=user.id, role=role)
    session.add(membership)
    await audit.record(
        session,
        org_id=token.org_id,
        actor=f"scim:{token.name or token.id}",
        action="scim.user_created",
        target=user.id,
        meta={"email": email, "role": role.value},
    )
    await session.flush()
    return _scim_response(_to_scim(user, membership, request), status.HTTP_201_CREATED)


@router.put("/Users/{user_id}")
async def scim_replace_user(
    user_id: str,
    payload: dict[str, Any],
    request: Request,
    token: ScimToken = Depends(scim_context),
    session: AsyncSession = Depends(db_session),
) -> Response:
    membership, user = await _scim_member(session, token.org_id, user_id)
    role = _requested_role(payload, token)
    was_active = user.is_active

    user.full_name = _full_name(payload) or user.full_name
    user.external_id = str(payload.get("externalId") or user.external_id)
    user.is_active = bool(payload.get("active", True))
    user.updated_at = utcnow()
    session.add(user)
    membership.role = role
    session.add(membership)

    await _apply_deactivation(session, user, was_active=was_active, org_id=token.org_id)
    # A role or status change must reach tokens already issued.
    user.token_version += 1
    session.add(user)
    return _scim_response(_to_scim(user, membership, request))


@router.patch("/Users/{user_id}")
async def scim_patch_user(
    user_id: str,
    payload: dict[str, Any],
    request: Request,
    token: ScimToken = Depends(scim_context),
    session: AsyncSession = Depends(db_session),
) -> Response:
    """Apply a PatchOp. Entra ID deactivates exclusively through this verb."""
    if _PATCH_SCHEMA not in (payload.get("schemas") or []):
        raise ScimError(
            status.HTTP_400_BAD_REQUEST, "expected a PatchOp document", scim_type="invalidSyntax"
        )
    membership, user = await _scim_member(session, token.org_id, user_id)
    was_active = user.is_active

    for operation in payload.get("Operations") or []:
        if not isinstance(operation, dict):
            continue
        op = str(operation.get("op", "")).lower()
        if op == "remove":
            # The only removable attribute Osprey models is membership itself,
            # which IdPs express as active=false; ignore anything else.
            continue
        path = str(operation.get("path") or "").strip()
        value = operation.get("value")
        # Entra sends {"op":"replace","value":{"active":false}} with no path.
        updates = value if isinstance(value, dict) and not path else {path: value}
        for key, raw in updates.items():
            _apply_patch_field(user, membership, key, raw, token)

    user.updated_at = utcnow()
    user.token_version += 1
    session.add(user)
    session.add(membership)
    await _apply_deactivation(session, user, was_active=was_active, org_id=token.org_id)
    return _scim_response(_to_scim(user, membership, request))


def _apply_patch_field(
    user: User, membership: Membership, key: str, raw: Any, token: ScimToken
) -> None:
    field = key.split(".")[-1].lower()
    if field == "active":
        # A JSON string "False" is truthy in Python; IdPs send both forms.
        user.is_active = raw if isinstance(raw, bool) else str(raw).lower() == "true"
    elif field in {"displayname", "formatted"}:
        user.full_name = str(raw)
    elif field == "username" and isinstance(raw, str) and "@" in raw:
        user.email = raw.strip().lower()
    elif field == "externalid":
        user.external_id = str(raw)
    elif field == "roles":
        membership.role = _requested_role({"roles": raw}, token)


@router.delete("/Users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def scim_delete_user(
    user_id: str,
    token: ScimToken = Depends(scim_context),
    session: AsyncSession = Depends(db_session),
) -> Response:
    """Deprovision. Soft: the account is disabled and its sessions cut, but the
    row survives so the audit trail still resolves who did what."""
    _membership, user = await _scim_member(session, token.org_id, user_id)
    was_active = user.is_active
    user.is_active = False
    user.updated_at = utcnow()
    user.token_version += 1
    session.add(user)
    await _apply_deactivation(session, user, was_active=was_active, org_id=token.org_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _apply_deactivation(
    session: AsyncSession, user: User, *, was_active: bool, org_id: str
) -> None:
    if was_active and not user.is_active:
        await sessions.revoke_all_for_user(session, user.id)
        await audit.record(
            session,
            org_id=org_id,
            actor="scim",
            action="scim.user_deactivated",
            target=user.id,
        )


async def _scim_member(session: AsyncSession, org_id: str, user_id: str) -> tuple[Membership, User]:
    row = (
        await session.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(Membership.org_id == org_id, Membership.user_id == user_id)
        )
    ).first()
    if row is None:
        raise ScimError(status.HTTP_404_NOT_FOUND, "user not found")
    return row[0], row[1]


# --------------------------------------------------------------------------- #
# Discovery + unimplemented endpoints
# --------------------------------------------------------------------------- #
@router.get("/ServiceProviderConfig")
async def service_provider_config(token: ScimToken = Depends(scim_context)) -> Response:
    return _scim_response(
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": 200},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [
                {
                    "type": "oauthbearertoken",
                    "name": "OAuth Bearer Token",
                    "description": "An Osprey SCIM provisioning token.",
                }
            ],
        }
    )


@router.api_route("/Groups", methods=["GET", "POST"])
async def groups_unsupported(token: ScimToken = Depends(scim_context)) -> Response:
    raise ScimError(
        status.HTTP_501_NOT_IMPLEMENTED,
        "Osprey does not model SCIM groups; roles come from the User 'roles' attribute",
    )


# --------------------------------------------------------------------------- #
# Token administration (normal JWT auth, owner only)
# --------------------------------------------------------------------------- #
admin_router = APIRouter(prefix="/orgs/current/scim-tokens", tags=["scim"])


@admin_router.post("", response_model=ScimTokenOut, status_code=status.HTTP_201_CREATED)
async def create_scim_token(
    body: ScimTokenCreate,
    principal: Principal = Depends(require_role(Role.owner)),
    session: AsyncSession = Depends(db_session),
) -> ScimTokenOut:
    if body.max_role == Role.owner:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "a provisioning token may not be allowed to create owners",
        )
    secret = f"osp_scim_{secrets.token_urlsafe(32)}"
    token = ScimToken(
        org_id=principal.org_id,
        name=body.name,
        token_hash=hash_secret(secret),
        max_role=body.max_role,
        created_by=principal.email,
    )
    session.add(token)
    await audit.record(
        session,
        org_id=principal.org_id,
        actor=principal.email,
        action="scim.token_created",
        target=token.id,
        meta={"max_role": body.max_role.value},
    )
    await session.flush()
    return ScimTokenOut(
        id=token.id,
        name=token.name,
        max_role=token.max_role,
        created_at=token.created_at.isoformat() if token.created_at else "",
        token=secret,
    )


@admin_router.get("", response_model=list[ScimTokenOut])
async def list_scim_tokens(
    principal: Principal = Depends(require_role(Role.owner)),
    session: AsyncSession = Depends(db_session),
) -> list[ScimTokenOut]:
    rows = (
        (await session.execute(select(ScimToken).where(ScimToken.org_id == principal.org_id)))
        .scalars()
        .all()
    )
    return [
        ScimTokenOut(
            id=t.id,
            name=t.name,
            max_role=t.max_role,
            created_at=t.created_at.isoformat() if t.created_at else "",
            last_used_at=t.last_used_at.isoformat() if t.last_used_at else None,
            revoked=t.revoked_at is not None,
        )
        for t in rows
    ]


@admin_router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_scim_token(
    token_id: str,
    principal: Principal = Depends(require_role(Role.owner)),
    session: AsyncSession = Depends(db_session),
) -> None:
    token = await session.get(ScimToken, token_id)
    if token is None or token.org_id != principal.org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "token not found")
    token.revoked_at = utcnow()
    session.add(token)
    await audit.record(
        session,
        org_id=principal.org_id,
        actor=principal.email,
        action="scim.token_revoked",
        target=token_id,
    )
