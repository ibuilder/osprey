"""Health & readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import __version__
from ..config import settings
from ..connectors.base import registry
from .deps import db_session

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "app": settings.app_name, "version": __version__, "env": settings.env}


@router.get("/ready")
async def ready(session: AsyncSession = Depends(db_session)) -> dict:
    db_ok = True
    detail = "ok"
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        db_ok, detail = False, str(exc)
    return {
        "ready": db_ok,
        "database": detail,
        "connectors": registry.types(),
        "ai_provider": settings.ai_provider,
    }
