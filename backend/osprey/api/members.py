"""Org membership: invite, list, change role, deactivate, remove.

Osprey sends no email. An invite returns its single-use token once, at creation,
and the caller delivers it however their organisation already delivers things --
which keeps SMTP credentials, bounce handling, and deliverability out of a
self-hosted product that would otherwise have to own all three.

Two invariants are enforced throughout and are the reason several handlers look
more defensive than a CRUD router usually would:

1. **No privilege escalation.** Nobody may grant a role above their own, so an
   admin cannot mint an owner and then act through it.
2. **An org always has a live owner.** Removing, demoting, or deactivating the
   last one would leave a tenant that nobody can administer and no endpoint can
   repair.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from ..models import Invite, Membership, Role, User, utcnow
from ..schemas import (
    InviteAccept,
    InviteCreate,
    InviteOut,
    MemberOut,
    MemberRoleUpdate,
    TokenResponse,
)
from ..security import audit, rbac, sessions
from ..security.auth import Principal, create_access_token, hash_secret
from ..security.passwords import PasswordPolicyError, check_policy, hash_password
from ..security.rls import set_current_org
from .deps import current_principal, db_session, require_role

log = logging.getLogger("osprey.members")

router = APIRouter(prefix="/orgs/current", tags=["members"])


def _iso(value) -> str:
    return value.isoformat() if value else ""


async def _live_owner_count(session: AsyncSession, org_id: str, *, excluding: str = "") -> int:
    """Owners of this org whose account is still enabled."""
    rows = (
        await session.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(Membership.org_id == org_id, Membership.role == Role.owner)
        )
    ).all()
    return sum(1 for m, u in rows if u.is_active and m.user_id != excluding)


def _assert_can_grant(actor: Principal, target_role: Role) -> None:
    if not rbac.satisfies(actor.role, target_role):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"you cannot grant a role above your own ({actor.role.value})",
        )


async def _assert_owner_remains(session: AsyncSession, org_id: str, user_id: str) -> None:
    if await _live_owner_count(session, org_id, excluding=user_id) == 0:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this is the organization's last active owner; promote someone else first",
        )


# --------------------------------------------------------------------------- #
# Members
# --------------------------------------------------------------------------- #
@router.get("/members", response_model=list[MemberOut])
async def list_members(
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> list[MemberOut]:
    rows = (
        await session.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(Membership.org_id == principal.org_id)
        )
    ).all()
    return [
        MemberOut(
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=membership.role,
            is_active=user.is_active,
            scim_managed=user.scim_managed,
            last_login_at=_iso(user.last_login_at) or None,
        )
        for membership, user in rows
    ]


@router.put("/members/{user_id}/role", response_model=MemberOut)
async def set_member_role(
    user_id: str,
    body: MemberRoleUpdate,
    principal: Principal = Depends(require_role(Role.admin)),
    session: AsyncSession = Depends(db_session),
) -> MemberOut:
    _assert_can_grant(principal, body.role)
    membership, user = await _member(session, principal.org_id, user_id)
    if user.scim_managed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this user is provisioned by your identity provider; change the role there",
        )
    # Demoting a peer you could not have promoted is escalation in reverse.
    _assert_can_grant(principal, membership.role)
    if membership.role == Role.owner and body.role != Role.owner:
        await _assert_owner_remains(session, principal.org_id, user_id)

    previous = membership.role
    membership.role = body.role
    session.add(membership)
    # The role is a token claim, so an in-flight token would keep the old one
    # until it expired. Bumping the version forces a refresh.
    user.token_version += 1
    session.add(user)
    await audit.record(
        session,
        org_id=principal.org_id,
        actor=principal.email,
        action="member.role_changed",
        target=user_id,
        meta={"from": previous.value, "to": body.role.value},
    )
    return MemberOut(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=body.role,
        is_active=user.is_active,
        scim_managed=user.scim_managed,
        last_login_at=_iso(user.last_login_at) or None,
    )


@router.post("/members/{user_id}/deactivate", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_member(
    user_id: str,
    principal: Principal = Depends(require_role(Role.admin)),
    session: AsyncSession = Depends(db_session),
) -> None:
    """Disable an account and cut every live session immediately."""
    membership, user = await _member(session, principal.org_id, user_id)
    _assert_can_grant(principal, membership.role)
    if membership.role == Role.owner:
        await _assert_owner_remains(session, principal.org_id, user_id)

    user.is_active = False
    user.token_version += 1  # invalidate access tokens already issued
    user.updated_at = utcnow()
    session.add(user)
    await sessions.revoke_all_for_user(session, user_id)
    await audit.record(
        session,
        org_id=principal.org_id,
        actor=principal.email,
        action="member.deactivated",
        target=user_id,
    )


@router.post("/members/{user_id}/reactivate", status_code=status.HTTP_204_NO_CONTENT)
async def reactivate_member(
    user_id: str,
    principal: Principal = Depends(require_role(Role.admin)),
    session: AsyncSession = Depends(db_session),
) -> None:
    _membership, user = await _member(session, principal.org_id, user_id)
    user.is_active = True
    user.failed_login_count = 0
    user.locked_until = None
    user.updated_at = utcnow()
    session.add(user)
    await audit.record(
        session,
        org_id=principal.org_id,
        actor=principal.email,
        action="member.reactivated",
        target=user_id,
    )


@router.delete("/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    user_id: str,
    principal: Principal = Depends(require_role(Role.admin)),
    session: AsyncSession = Depends(db_session),
) -> None:
    """Remove someone from this org.

    The ``User`` row survives -- it may hold memberships in other tenants, and
    audit records reference it by id. Only the membership goes.
    """
    membership, user = await _member(session, principal.org_id, user_id)
    _assert_can_grant(principal, membership.role)
    if membership.role == Role.owner:
        await _assert_owner_remains(session, principal.org_id, user_id)
    if user_id == principal.user_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "you cannot remove yourself")

    await session.delete(membership)
    user.token_version += 1
    session.add(user)
    await sessions.revoke_all_for_user(session, user_id)
    await audit.record(
        session,
        org_id=principal.org_id,
        actor=principal.email,
        action="member.removed",
        target=user_id,
    )


async def _member(session: AsyncSession, org_id: str, user_id: str) -> tuple[Membership, User]:
    row = (
        await session.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(Membership.org_id == org_id, Membership.user_id == user_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
    return row[0], row[1]


# --------------------------------------------------------------------------- #
# Invites
# --------------------------------------------------------------------------- #
@router.post("/invites", response_model=InviteOut, status_code=status.HTTP_201_CREATED)
async def create_invite(
    body: InviteCreate,
    principal: Principal = Depends(require_role(Role.admin)),
    session: AsyncSession = Depends(db_session),
) -> InviteOut:
    _assert_can_grant(principal, body.role)
    email = body.email.lower()

    existing_user = (
        await session.execute(select(User).where(func.lower(User.email) == email))
    ).scalar_one_or_none()
    if existing_user is not None:
        already = (
            await session.execute(
                select(Membership).where(
                    Membership.org_id == principal.org_id,
                    Membership.user_id == existing_user.id,
                )
            )
        ).scalar_one_or_none()
        if already is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "already a member of this org")

    secret = secrets.token_urlsafe(32)
    invite = Invite(
        org_id=principal.org_id,
        email=email,
        role=body.role,
        token_hash=hash_secret(secret),
        invited_by=principal.email,
        expires_at=utcnow() + timedelta(days=body.expires_days),
    )
    session.add(invite)
    await audit.record(
        session,
        org_id=principal.org_id,
        actor=principal.email,
        action="invite.created",
        target=email,
        meta={"role": body.role.value},
    )
    await session.flush()
    return InviteOut(
        id=invite.id,
        email=invite.email,
        role=invite.role,
        invited_by=invite.invited_by,
        expires_at=_iso(invite.expires_at),
        accepted=False,
        token=secret,
    )


@router.get("/invites", response_model=list[InviteOut])
async def list_invites(
    principal: Principal = Depends(require_role(Role.admin)),
    session: AsyncSession = Depends(db_session),
) -> list[InviteOut]:
    rows = (
        (
            await session.execute(
                select(Invite).where(
                    Invite.org_id == principal.org_id, col(Invite.revoked_at).is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    # `token` stays None here: it exists in plaintext only in the creation response.
    return [
        InviteOut(
            id=i.id,
            email=i.email,
            role=i.role,
            invited_by=i.invited_by,
            expires_at=_iso(i.expires_at),
            accepted=i.accepted_at is not None,
        )
        for i in rows
    ]


@router.delete("/invites/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite(
    invite_id: str,
    principal: Principal = Depends(require_role(Role.admin)),
    session: AsyncSession = Depends(db_session),
) -> None:
    invite = await session.get(Invite, invite_id)
    if invite is None or invite.org_id != principal.org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invite not found")
    invite.revoked_at = utcnow()
    session.add(invite)


# Accepting is unauthenticated by definition -- the invitee has no account yet --
# so it lives on its own router outside the /orgs/current prefix.
accept_router = APIRouter(prefix="/invites", tags=["members"])


@accept_router.post("/accept", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def accept_invite(
    body: InviteAccept,
    session: AsyncSession = Depends(db_session),
) -> TokenResponse:
    """Redeem an invite token: create or attach the user, and sign them in."""
    invite = (
        await session.execute(select(Invite).where(Invite.token_hash == hash_secret(body.token)))
    ).scalar_one_or_none()
    # One message for every failure mode. Distinguishing "expired" from "unknown"
    # tells a token-guesser when they have found a real one.
    invalid = HTTPException(status.HTTP_400_BAD_REQUEST, "invite is invalid or has expired")
    if invite is None or invite.accepted_at is not None or invite.revoked_at is not None:
        raise invalid
    expires = invite.expires_at
    if expires is None:
        raise invalid
    now = utcnow() if expires.tzinfo else utcnow().replace(tzinfo=None)
    if expires < now:
        raise invalid

    try:
        check_policy(body.password, email=invite.email)
    except PasswordPolicyError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    await set_current_org(session, invite.org_id)
    user = (
        await session.execute(select(User).where(func.lower(User.email) == invite.email))
    ).scalar_one_or_none()
    if user is None:
        user = User(
            email=invite.email,
            full_name=body.full_name,
            password_hash=hash_password(body.password),
        )
        session.add(user)
        await session.flush()
    elif not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this account is disabled")

    existing = (
        await session.execute(
            select(Membership).where(
                Membership.org_id == invite.org_id, Membership.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(Membership(org_id=invite.org_id, user_id=user.id, role=invite.role))
    else:
        existing.role = invite.role
        session.add(existing)

    invite.accepted_at = utcnow()
    session.add(invite)
    await audit.record(
        session,
        org_id=invite.org_id,
        actor=user.email,
        action="invite.accepted",
        target=user.id,
        meta={"role": invite.role.value},
    )
    await session.flush()

    principal = Principal(
        user_id=user.id,
        org_id=invite.org_id,
        role=invite.role,
        email=user.email,
        token_version=user.token_version,
    )
    refresh, _ = await sessions.issue(session, user=user, org_id=invite.org_id)
    return TokenResponse(
        access_token=create_access_token(principal),
        refresh_token=refresh,
        role=invite.role,
        org_id=invite.org_id,
        user_id=user.id,
    )
