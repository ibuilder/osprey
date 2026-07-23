"""FastAPI dependencies: DB session, auth principal, RBAC guards, org scoping."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Membership, Project, Role
from ..security import rbac
from ..security.auth import Principal, decode_token


async def db_session() -> AsyncIterator[AsyncSession]:
    async for s in get_session():
        yield s


async def current_principal(authorization: str = Header(default="")) -> Principal:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        return decode_token(token)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token") from exc


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
