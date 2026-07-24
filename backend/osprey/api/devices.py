"""Device registration for push notifications (APNs / FCM / Web Push)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Device
from ..security.auth import Principal
from .deps import current_principal, db_session

router = APIRouter(prefix="/devices", tags=["devices"])


class DeviceRegister(BaseModel):
    platform: str = "web"  # ios | android | web
    token: str


class DeviceOut(BaseModel):
    id: str
    platform: str


@router.post("", response_model=DeviceOut, status_code=201)
async def register_device(
    body: DeviceRegister,
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> DeviceOut:
    if not body.token:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "token required")
    existing = (
        await session.execute(
            select(Device).where(Device.user_id == principal.user_id, Device.token == body.token)
        )
    ).scalar_one_or_none()
    if existing:
        return DeviceOut(id=existing.id, platform=existing.platform)
    row = Device(
        org_id=principal.org_id, user_id=principal.user_id, platform=body.platform, token=body.token
    )
    session.add(row)
    await session.flush()
    return DeviceOut(id=row.id, platform=row.platform)


@router.get("", response_model=list[DeviceOut])
async def list_devices(
    session: AsyncSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> list[DeviceOut]:
    rows = (
        (await session.execute(select(Device).where(Device.user_id == principal.user_id)))
        .scalars()
        .all()
    )
    return [DeviceOut(id=r.id, platform=r.platform) for r in rows]
