"""Refresh-session issue / rotate / revoke.

Rotation is mandatory: a refresh token is single-use, and presenting one that has
already been rotated means either a replay or a stolen copy racing the legitimate
client. Neither is recoverable by guessing which is which, so the whole *family*
descended from the original login is revoked and both parties must sign in again.
That is the standard reuse-detection response (OAuth 2.1 BCP) and it is the reason
``RefreshToken`` carries ``family_id`` and ``rotated_to``.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from ..models import Membership, RefreshToken, User, utcnow
from .auth import Principal, hash_secret, new_refresh_secret, refresh_expiry

log = logging.getLogger("osprey.sessions")


class SessionError(Exception):
    """The presented refresh token cannot be exchanged."""


async def issue(
    session: AsyncSession,
    *,
    user: User,
    org_id: str,
    user_agent: str = "",
    ip: str = "",
    family_id: str | None = None,
) -> tuple[str, RefreshToken]:
    """Mint a refresh token. Returns ``(plaintext, row)``; the plaintext is
    the only copy the server will ever have."""
    secret = new_refresh_secret()
    row = RefreshToken(
        org_id=org_id,
        user_id=user.id,
        token_hash=hash_secret(secret),
        expires_at=refresh_expiry(),
        user_agent=user_agent[:400],
        ip=ip[:64],
    )
    if family_id:
        row.family_id = family_id
    session.add(row)
    await session.flush()
    return secret, row


async def rotate(
    session: AsyncSession,
    *,
    secret: str,
    user_agent: str = "",
    ip: str = "",
) -> tuple[str, Principal]:
    """Exchange a refresh token for a successor plus a fresh Principal."""
    row = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == hash_secret(secret))
        )
    ).scalar_one_or_none()
    if row is None:
        raise SessionError("unknown refresh token")

    if row.rotated_to is not None:
        # Reuse detected. Someone is holding a copy we already retired.
        await revoke_family(session, row.family_id)
        log.warning(
            "refresh token reuse detected; revoked session family %s for user %s",
            row.family_id,
            row.user_id,
        )
        raise SessionError("refresh token already used")
    if row.revoked_at is not None:
        raise SessionError("refresh token revoked")
    if _expired(row):
        raise SessionError("refresh token expired")

    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        raise SessionError("user is not active")

    membership = (
        await session.execute(
            select(Membership).where(Membership.user_id == user.id, Membership.org_id == row.org_id)
        )
    ).scalar_one_or_none()
    if membership is None:
        # Membership was removed since the session started; the refresh must not
        # mint a token for an org this user has been taken out of.
        await revoke_family(session, row.family_id)
        raise SessionError("no membership for this org")

    successor_secret, successor = await issue(
        session,
        user=user,
        org_id=row.org_id,
        user_agent=user_agent,
        ip=ip,
        family_id=row.family_id,
    )
    row.rotated_to = successor.id
    row.revoked_at = utcnow()
    session.add(row)

    principal = Principal(
        user_id=user.id,
        org_id=row.org_id,
        role=membership.role,
        email=user.email,
        token_version=user.token_version,
    )
    return successor_secret, principal


async def revoke(session: AsyncSession, *, secret: str) -> bool:
    """Revoke one session by its token. Returns False if it was unknown."""
    row = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == hash_secret(secret))
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        session.add(row)
    return True


async def revoke_family(session: AsyncSession, family_id: str) -> int:
    """Revoke every live token descended from one login."""
    result = await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, col(RefreshToken.revoked_at).is_(None))
        .values(revoked_at=utcnow())
    )
    return int(result.rowcount or 0)


async def revoke_all_for_user(session: AsyncSession, user_id: str) -> int:
    """Sign a user out everywhere. Pair with a ``token_version`` bump, which is
    what invalidates the access tokens already in flight."""
    result = await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, col(RefreshToken.revoked_at).is_(None))
        .values(revoked_at=utcnow())
    )
    return int(result.rowcount or 0)


async def list_active(session: AsyncSession, user_id: str) -> list[RefreshToken]:
    rows = (
        (
            await session.execute(
                select(RefreshToken).where(
                    RefreshToken.user_id == user_id, col(RefreshToken.revoked_at).is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    return [r for r in rows if not _expired(r)]


async def purge_expired(session: AsyncSession) -> int:
    """Delete rows that can no longer authenticate anything. Housekeeping only."""
    from sqlalchemy import delete

    rows = (await session.execute(select(RefreshToken))).scalars().all()
    dead = [r.id for r in rows if _expired(r) or r.revoked_at is not None]
    if not dead:
        return 0
    await session.execute(delete(RefreshToken).where(RefreshToken.id.in_(dead)))
    return len(dead)


def _expired(row: RefreshToken) -> bool:
    expires = row.expires_at
    if expires is None:
        return True
    # SQLite hands back naive datetimes; compare in a single convention.
    if expires.tzinfo is None:
        return expires < utcnow().replace(tzinfo=None)
    return expires < utcnow()
