"""Auth: register (bootstraps org + owner) and login."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Membership, Org, Role, User
from ..schemas import LoginRequest, RegisterRequest, TokenResponse
from ..security import audit
from ..security.auth import Principal, create_access_token
from ..security.passwords import hash_password, verify_password
from .deps import db_session

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, session: AsyncSession = Depends(db_session)) -> TokenResponse:
    existing = (
        await session.execute(select(User).where(func.lower(User.email) == body.email.lower()))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered")

    org = Org(name=body.org_name)
    session.add(org)
    await session.flush()

    user = User(
        email=body.email.lower(),
        full_name=body.full_name,
        password_hash=hash_password(body.password),
    )
    session.add(user)
    await session.flush()

    membership = Membership(org_id=org.id, user_id=user.id, role=Role.owner)
    session.add(membership)

    await audit.record(session, org_id=org.id, actor=user.email, action="org.created", target=org.id)
    await session.flush()

    principal = Principal(user_id=user.id, org_id=org.id, role=Role.owner, email=user.email)
    return TokenResponse(
        access_token=create_access_token(principal),
        role=Role.owner,
        org_id=org.id,
        user_id=user.id,
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, session: AsyncSession = Depends(db_session)) -> TokenResponse:
    user = (
        await session.execute(select(User).where(func.lower(User.email) == body.email.lower()))
    ).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "user disabled")

    membership = (
        await session.execute(select(Membership).where(Membership.user_id == user.id))
    ).scalars().first()
    if membership is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no org membership")

    principal = Principal(
        user_id=user.id, org_id=membership.org_id, role=membership.role, email=user.email
    )
    return TokenResponse(
        access_token=create_access_token(principal),
        role=membership.role,
        org_id=membership.org_id,
        user_id=user.id,
    )
