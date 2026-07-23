"""Async database engine and session management.

SQLite (aiosqlite) by default so the app runs with zero infrastructure; point
``OSPREY_DATABASE_URL`` at ``postgresql+asyncpg://`` for production. Embeddings
are stored portably (JSON) so clustering works identically on both; pgvector is a
production optimization documented in ``engine/cluster.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import SQLModel

from .config import settings

_ENGINE: AsyncEngine | None = None
_SESSIONMAKER: async_sessionmaker[AsyncSession] | None = None


def _connect_args() -> dict:
    if settings.is_sqlite:
        return {"check_same_thread": False}
    return {}


def get_engine() -> AsyncEngine:
    global _ENGINE, _SESSIONMAKER
    if _ENGINE is None:
        _ENGINE = create_async_engine(
            settings.database_url,
            echo=False,
            future=True,
            pool_pre_ping=not settings.is_sqlite,
            connect_args=_connect_args(),
        )
        _SESSIONMAKER = async_sessionmaker(_ENGINE, expire_on_commit=False, class_=AsyncSession)
    return _ENGINE


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _SESSIONMAKER is None:
        get_engine()
    assert _SESSIONMAKER is not None
    return _SESSIONMAKER


async def create_all() -> None:
    """Create tables from model metadata (dev/test; prod uses Alembic)."""
    # Import models so they register on SQLModel.metadata.
    from . import models  # noqa: F401

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


async def drop_all() -> None:
    from . import models  # noqa: F401

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)


async def dispose() -> None:
    global _ENGINE, _SESSIONMAKER
    if _ENGINE is not None:
        await _ENGINE.dispose()
    _ENGINE = None
    _SESSIONMAKER = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional session context for workers / scripts."""
    maker = get_sessionmaker()
    async with maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yields a session, commits on success."""
    maker = get_sessionmaker()
    async with maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
