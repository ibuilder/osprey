"""FastAPI dependencies: DB session, auth principal, RBAC guards, org scoping."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Membership, Org, Project, Role, User
from ..security import rbac
from ..security.auth import Principal, decode_token
from ..security.rls import set_current_org


async def db_session(authorization: str = Header(default="")) -> AsyncIterator[AsyncSession]:
    async for s in get_session():
        # Bind the tenant for Postgres row-level security (no-op on SQLite/disabled).
        if authorization.lower().startswith("bearer "):
            try:
                principal = decode_token(authorization.split(" ", 1)[1].strip())
                await set_current_org(s, principal.org_id)
            except Exception:  # noqa: BLE001 - auth errors surface in the real guard
                pass
        yield s


def _unauthorized(detail: str) -> HTTPException:
    # WWW-Authenticate is what tells a client to refresh rather than re-prompt.
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail,
        headers={"WWW-Authenticate": 'Bearer realm="osprey"'},
    )


async def current_principal(
    authorization: str = Header(default=""),
    session: AsyncSession = Depends(db_session),
) -> Principal:
    """Verify the bearer token and confirm it has not been revoked.

    Signature validity is not sufficient. A token stays cryptographically sound
    until it expires, so we also check that the user still exists, is still
    active, and that the token's ``ver`` still matches the user's
    ``token_version`` -- which is what makes deactivation, role changes, and
    "sign me out everywhere" take effect immediately rather than up to
    ``access_token_ttl_minutes`` later.
    """
    if not authorization.lower().startswith("bearer "):
        raise _unauthorized("missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        principal = decode_token(token)
    except Exception as exc:  # noqa: BLE001
        raise _unauthorized("invalid or expired token") from exc

    user = await session.get(User, principal.user_id)
    if user is None or not user.is_active:
        raise _unauthorized("account is disabled")
    if user.token_version != principal.token_version:
        raise _unauthorized("token has been revoked; sign in again")

    org = await session.get(Org, principal.org_id)
    if org is not None and org.deletion_requested_at is not None:
        raise HTTPException(status.HTTP_423_LOCKED, "this organization is scheduled for deletion")
    return principal


def require_role(minimum: Role):
    async def _guard(principal: Principal = Depends(current_principal)) -> Principal:
        if not rbac.satisfies(principal.role, minimum):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"requires role >= {minimum.value} (you are {principal.role.value})",
            )
        return principal

    return _guard


async def project_in_org(
    project_id: str,
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> Project:
    project = await session.get(Project, project_id)
    if project is None or project.org_id != principal.org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    return project


async def assert_membership(session: AsyncSession, principal: Principal) -> None:
    row = (
        await session.execute(
            select(Membership).where(
                Membership.org_id == principal.org_id, Membership.user_id == principal.user_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not a member of this org")


def client_address(request: Request) -> str:
    """The caller's IP, honouring proxy headers only when configured to trust them."""
    from ..middleware import client_ip

    return client_ip(request)
