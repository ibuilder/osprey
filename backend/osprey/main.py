"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .api import (
    admin,
    ai,
    auth,
    connections,
    devices,
    health,
    hotlist,
    items,
    projects,
    scripts,
    webhooks,
    ws,
)
from .config import settings
from .connectors.base import registry
from .db import create_all, dispose
from .logging_setup import configure_logging

log = logging.getLogger("osprey")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_level)
    for problem in settings.assert_prod_secrets():
        log.warning("CONFIG: %s", problem)
    # Import connectors so the registry is populated.
    import osprey.connectors  # noqa: F401

    # Select the configured push sender (logging by default).
    from .engine.notify import set_sender
    from .engine.push_senders import build_sender

    set_sender(build_sender())

    # In dev/test the schema is created on boot; production uses Alembic migrations.
    if not settings.is_prod:
        await create_all()
    log.info("Osprey %s starting (env=%s, connectors=%s)", __version__, settings.env, registry.types())
    yield
    await dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Osprey API",
        version=__version__,
        summary="The foreman that never sleeps.",
        description="Open-source construction/RE hotlist agent — backend brain.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if not settings.is_prod else [],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for module in (
        health, auth, projects, connections, hotlist, items, webhooks,
        ai, scripts, admin, devices, ws,
    ):
        app.include_router(module.router)

    # Optional OpenTelemetry instrumentation (no-op unless enabled + installed).
    from .observability import setup_observability

    setup_observability(app)
    return app


app = create_app()
