"""Health, readiness, liveness, and the Prometheus scrape endpoint.

Three probes, because Kubernetes asks three different questions:

``/live``   is the process wedged? Never touches a dependency -- a liveness probe
            that fails on a database blip restarts every pod in the fleet during
            an outage that the pods had nothing to do with.
``/health`` a human/load-balancer summary. Always 200 if the process is up.
``/ready``  should traffic be routed here? Checks the database and returns **503**
            when it should not. Returning 200 with ``{"ready": false}`` -- which
            this endpoint used to do -- means the load balancer keeps sending
            requests to a replica that cannot serve them.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import __version__
from ..config import settings
from ..connectors.base import registry
from .deps import db_session

log = logging.getLogger("osprey.health")

router = APIRouter(tags=["health"])


@router.get("/live")
async def live() -> dict:
    """Liveness: the event loop is turning. Deliberately dependency-free."""
    return {"status": "alive", "version": __version__}


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "app": settings.app_name, "version": __version__, "env": settings.env}


@router.get("/ready")
async def ready(response: Response, session: AsyncSession = Depends(db_session)) -> dict:
    db_ok = True
    detail = "ok"
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        db_ok, detail = False, str(exc)
        log.warning("readiness probe failed: %s", exc)
    if not db_ok:
        # The status code is the part orchestrators act on; the body is for humans.
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "ready": db_ok,
        "database": detail,
        "connectors": registry.types(),
        "ai_provider": settings.ai_provider,
    }


@router.get("/metrics", include_in_schema=False)
async def metrics_endpoint(authorization: str = Header(default="")) -> Response:
    """Prometheus scrape target.

    Guarded by a shared token when ``OSPREY_METRICS_TOKEN`` is set. Metrics leak
    real operational detail -- tenant counts, error rates, which routes exist --
    so a public deployment should either set the token or keep the port off the
    internet.
    """
    if not settings.metrics_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "metrics are disabled")
    if settings.metrics_token:
        import hmac

        presented = (
            authorization.split(" ", 1)[1].strip()
            if authorization.lower().startswith("bearer ")
            else ""
        )
        if not hmac.compare_digest(presented, settings.metrics_token):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid metrics token")

    from .. import metrics

    return Response(content=metrics.render(), media_type=metrics.CONTENT_TYPE)
