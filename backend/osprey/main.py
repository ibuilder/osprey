"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, metrics
from .api import (
    admin,
    ai,
    auth,
    connections,
    devices,
    governance,
    health,
    hotlist,
    items,
    members,
    projects,
    scim,
    scripts,
    sso,
    webhooks,
    ws,
)
from .config import settings
from .connectors.base import registry
from .db import create_all, dispose
from .logging_setup import configure_logging
from .middleware import (
    REQUEST_ID_HEADER,
    BodySizeLimitMiddleware,
    MetricsMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    current_request_id,
)

log = logging.getLogger("osprey")


class ConfigurationError(RuntimeError):
    """A production boot was attempted with insecure or unusable settings."""


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_level)

    # A production deploy carrying a default secret is not "degraded", it is
    # unprotected. Refuse the boot so it fails loudly at deploy time instead of
    # quietly serving traffic with a publicly known signing key.
    problems = settings.assert_prod_secrets()
    if problems:
        for problem in problems:
            log.error("CONFIG: %s", problem)
        raise ConfigurationError(
            "refusing to start in production with insecure configuration: " + "; ".join(problems)
        )
    for warning in settings.prod_warnings():
        log.warning("CONFIG: %s", warning)

    # Import connectors so the registry is populated.
    import osprey.connectors  # noqa: F401

    # Select the configured push sender (logging by default).
    from .engine.notify import set_sender
    from .engine.push_senders import build_sender

    set_sender(build_sender())

    # Pick the rate-limit backend once, at startup, rather than on the first
    # request -- a Redis handshake in the middle of a user's login is not where
    # the fallback decision should be made.
    from .security.ratelimit import build_limiter, close_limiter, set_limiter

    set_limiter(await build_limiter())

    # Dev/test create the schema on boot; servers migrate with Alembic. The desktop
    # bundle is production-ish but ships no migration step, so it opts in explicitly.
    if not settings.is_prod or settings.create_schema_on_start:
        await create_all()

    # If tenant isolation is switched on, confirm the connection can't bypass it.
    if settings.rls_enabled and not settings.is_sqlite:
        from .db import session_scope
        from .security.rls import verify_enforcement

        async with session_scope() as session:
            await verify_enforcement(session)

    metrics.set_build_info(__version__, settings.env)
    log.info(
        "Osprey %s starting (env=%s, connectors=%s)", __version__, settings.env, registry.types()
    )
    yield
    await close_limiter()
    await dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Osprey API",
        version=__version__,
        summary="The foreman that never sleeps.",
        description="Open-source construction/RE hotlist agent — backend brain.",
        lifespan=lifespan,
        # Interactive docs are a fingerprinting surface and a CSP exception; a
        # production deployment that wants them can turn them back on.
        docs_url=None if settings.is_prod and not settings.debug else "/docs",
        redoc_url=None if settings.is_prod and not settings.debug else "/redoc",
    )

    _install_middleware(app)
    _install_exception_handlers(app)

    for module in (
        health,
        auth,
        sso,
        members,
        projects,
        connections,
        hotlist,
        items,
        webhooks,
        ai,
        scripts,
        admin,
        devices,
        governance,
        scim,
        ws,
    ):
        app.include_router(module.router)
    # Routers that live outside their module's main prefix.
    app.include_router(members.accept_router)
    app.include_router(scim.admin_router)

    # Optional OpenTelemetry instrumentation (no-op unless enabled + installed).
    from .observability import setup_observability

    setup_observability(app)
    return app


def _install_middleware(app: FastAPI) -> None:
    """Register middleware. Starlette runs these in reverse: last added is outermost.

    The order below therefore executes as
    RequestContext -> SecurityHeaders -> Metrics -> BodySizeLimit -> RateLimit -> CORS.
    """
    origins = ["*"] if not settings.is_prod else settings.cors_allow_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        # A wildcard origin cannot carry credentials -- browsers reject the
        # combination outright -- so the two settings are kept consistent rather
        # than sent as a pair no user agent will honour.
        allow_credentials="*" not in origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", REQUEST_ID_HEADER],
        expose_headers=[REQUEST_ID_HEADER, "X-RateLimit-Remaining", "X-RateLimit-Reset"],
        max_age=600,
    )
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)


def _install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        # SCIM errors carry a fully-formed RFC 7644 body as their detail; pass it
        # through unwrapped, because IdP connectors parse that shape specifically.
        if isinstance(exc.detail, dict) and "schemas" in exc.detail:
            return JSONResponse(
                exc.detail,
                status_code=exc.status_code,
                media_type="application/scim+json",
                headers=exc.headers,
            )
        return JSONResponse(
            {"detail": exc.detail, "request_id": current_request_id()},
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        # Pydantic's default body echoes the rejected input back, which lands
        # submitted passwords in client logs and error trackers. Report the
        # location and the rule, never the value.
        errors = [
            {
                "loc": list(error.get("loc", [])),
                "msg": error.get("msg", ""),
                "type": error.get("type", ""),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            {"detail": errors, "request_id": current_request_id()},
            status_code=422,
        )


app = create_app()
