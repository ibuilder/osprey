"""Auth: register, login, refresh, logout, password change, session management.

Registration bootstraps an org and its owner. Every later member arrives through
an invite (``api/members.py``) or SCIM (``api/scim.py``), never through this
endpoint -- which is why registering a second user does not silently join them to
an existing tenant.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Membership, Org, RefreshToken, Role, User, utcnow
from ..schemas import (
    LoginRequest,
    PasswordChangeRequest,
    RefreshRequest,
    RegisterRequest,
    SessionOut,
    TokenResponse,
)
from ..security import audit, ratelimit, sessions
from ..security.auth import Principal, create_access_token
from ..security.passwords import (
    PasswordPolicyError,
    check_policy,
    hash_password,
    needs_rehash,
    verify_password,
)
from ..security.rls import set_current_org
from .deps import client_address, current_principal, db_session

log = logging.getLogger("osprey.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _guard_credentials(request: Request, email: str) -> None:
    """Meter credential attempts by IP and by account, in two windows.

    Per-IP alone lets a botnet spray one password across many accounts from many
    addresses; per-account alone lets one address walk a dictionary against a
    thousand accounts. Both keys are checked, in a per-minute and a per-hour
    window, so neither shape gets through.
    """
    ip = client_address(request)
    account = email.strip().lower()
    decision = await ratelimit.check_all(
        (f"login:ip:{ip}", settings.rate_limit_login_per_minute, 60),
        (f"login:ip:{ip}:h", settings.rate_limit_login_per_hour, 3600),
        (f"login:acct:{account}", settings.rate_limit_login_per_minute, 60),
        (f"login:acct:{account}:h", settings.rate_limit_login_per_hour, 3600),
    )
    if not decision.allowed:
        log.warning("credential rate limit hit from %s", ip)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many attempts; try again shortly",
            headers={"Retry-After": str(decision.retry_after)},
        )


def _locked(user: User) -> bool:
    until = user.locked_until
    if until is None:
        return False
    now = utcnow() if until.tzinfo else utcnow().replace(tzinfo=None)
    return until > now


async def _record_failure(session: AsyncSession, user: User) -> None:
    """Count a failed sign-in, locking the account past the threshold.

    Commits before returning. The caller raises 401 immediately afterwards, and
    ``deps.db_session`` rolls the session back on any exception -- so without an
    explicit commit here the counter would be discarded on every failed attempt
    and the lockout could never trigger.
    """
    user.failed_login_count += 1
    if user.failed_login_count >= settings.login_max_failures:
        user.locked_until = utcnow() + timedelta(minutes=settings.login_lockout_minutes)
        user.failed_login_count = 0
        log.warning("account locked after repeated failures: user_id=%s", user.id)
    session.add(user)
    await session.commit()


def _issue(principal: Principal, refresh: str | None = None) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(principal),
        refresh_token=refresh,
        expires_in=settings.access_token_ttl_minutes * 60,
        role=principal.role,
        org_id=principal.org_id,
        user_id=principal.user_id,
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    request: Request,
    session: AsyncSession = Depends(db_session),
) -> TokenResponse:
    await _guard_credentials(request, body.email)
    try:
        check_policy(body.password, email=body.email)
    except PasswordPolicyError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    existing = (
        await session.execute(select(User).where(func.lower(User.email) == body.email.lower()))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered")

    org = Org(name=body.org_name)
    # Bind the new tenant BEFORE inserting anything. Registration is the one flow
    # that runs without an existing tenant context, and with row-level security on,
    # the policies (whose USING clause also governs INSERT) would reject every row
    # written here — org, membership, audit entry — because no tenant is bound.
    # The id is generated client-side, so it can be bound up front.
    await set_current_org(session, org.id)
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

    await audit.record(
        session, org_id=org.id, actor=user.email, action="org.created", target=org.id
    )
    await session.flush()

    principal = Principal(
        user_id=user.id,
        org_id=org.id,
        role=Role.owner,
        email=user.email,
        token_version=user.token_version,
    )
    refresh, _ = await sessions.issue(
        session,
        user=user,
        org_id=org.id,
        user_agent=request.headers.get("user-agent", ""),
        ip=client_address(request),
    )
    return _issue(principal, refresh)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(db_session),
) -> TokenResponse:
    await _guard_credentials(request, body.email)

    user = (
        await session.execute(select(User).where(func.lower(User.email) == body.email.lower()))
    ).scalar_one_or_none()

    # Uniform failure for "no such user" and "wrong password": distinguishing them
    # turns login into an account-enumeration oracle.
    invalid = HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    if user is None:
        # Spend comparable time so response latency does not leak existence. The
        # cost parameter is read from config rather than hardcoded, or this
        # decoy would take a different amount of work than a real verification
        # and reintroduce exactly the timing signal it exists to suppress.
        verify_password(
            body.password,
            f"pbkdf2_sha256${settings.password_hash_iterations}$AAAAAAAAAAAAAAAA$AAAAAAAA",
        )
        raise invalid
    if _locked(user):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "account temporarily locked after repeated failed sign-ins",
            headers={"Retry-After": str(settings.login_lockout_minutes * 60)},
        )
    if not verify_password(body.password, user.password_hash):
        await _record_failure(session, user)
        raise invalid
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "user disabled")

    membership = (
        (await session.execute(select(Membership).where(Membership.user_id == user.id)))
        .scalars()
        .first()
    )
    if membership is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no org membership")

    # Transparent upgrade if the stored hash predates the current KDF parameters.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    session.add(user)

    await set_current_org(session, membership.org_id)
    principal = Principal(
        user_id=user.id,
        org_id=membership.org_id,
        role=membership.role,
        email=user.email,
        token_version=user.token_version,
    )
    refresh, _ = await sessions.issue(
        session,
        user=user,
        org_id=membership.org_id,
        user_agent=request.headers.get("user-agent", ""),
        ip=client_address(request),
    )
    await audit.record(
        session,
        org_id=membership.org_id,
        actor=user.email,
        action="auth.login",
        target=user.id,
        meta={"ip": client_address(request)},
    )
    return _issue(principal, refresh)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    body: RefreshRequest,
    request: Request,
    session: AsyncSession = Depends(db_session),
) -> TokenResponse:
    """Exchange a refresh token for a new access token and a *new* refresh token."""
    try:
        successor, principal = await sessions.rotate(
            session,
            secret=body.refresh_token,
            user_agent=request.headers.get("user-agent", ""),
            ip=client_address(request),
        )
    except sessions.SessionError as exc:
        # Reuse detection revokes the whole session family on the way out, and
        # that revocation must survive the 401 -- db_session rolls back on any
        # exception, which would silently undo it and leave the stolen token live.
        await session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    await set_current_org(session, principal.org_id)
    return _issue(principal, successor)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    body: RefreshRequest,
    session: AsyncSession = Depends(db_session),
) -> None:
    """End one session. Idempotent -- an unknown token is not an error, because
    reporting one would let a caller probe which tokens exist."""
    await sessions.revoke(session, secret=body.refresh_token)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> None:
    """Sign out of every device, invalidating access tokens already in flight."""
    user = await session.get(User, principal.user_id)
    if user is None:  # pragma: no cover - current_principal already proved this
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    await sessions.revoke_all_for_user(session, user.id)
    user.token_version += 1
    session.add(user)
    await audit.record(
        session,
        org_id=principal.org_id,
        actor=principal.email,
        action="auth.logout_all",
        target=user.id,
    )


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> list[SessionOut]:
    rows = await sessions.list_active(session, principal.user_id)
    return [
        SessionOut(
            id=r.id,
            created_at=r.created_at.isoformat() if r.created_at else "",
            expires_at=r.expires_at.isoformat() if r.expires_at else "",
            user_agent=r.user_agent,
            ip=r.ip,
        )
        for r in rows
    ]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: str,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> None:
    row = await session.get(RefreshToken, session_id)
    # 404 rather than 403 for someone else's session: the caller has no business
    # learning that the id exists.
    if row is None or row.user_id != principal.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        session.add(row)


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: PasswordChangeRequest,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> None:
    """Change the caller's password and sign every other session out."""
    user = await session.get(User, principal.user_id)
    if user is None:  # pragma: no cover
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    if not user.password_hash:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "this account signs in through your identity provider"
        )
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "current password is incorrect")
    try:
        check_policy(body.new_password, email=user.email)
    except PasswordPolicyError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    user.password_hash = hash_password(body.new_password)
    # A password change must not leave a stolen session alive.
    user.token_version += 1
    user.updated_at = utcnow()
    session.add(user)
    await sessions.revoke_all_for_user(session, user.id)
    await audit.record(
        session,
        org_id=principal.org_id,
        actor=user.email,
        action="auth.password_changed",
        target=user.id,
    )
