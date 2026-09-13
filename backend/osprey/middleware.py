"""ASGI middleware: request identity, security headers, body caps, rate limiting.

Ordering matters and is set in :func:`osprey.main.create_app`. Starlette runs
middleware in reverse registration order, so the last one added is outermost.
What we need is:

    RequestContext (outermost)  -> every response, including a 429 or a 500,
                                   carries a request id and gets logged
      SecurityHeaders           -> headers on every response the app produces
        BodySizeLimit           -> reject oversized uploads before routing
          RateLimit             -> cheapest rejection last, once we know the path
            CORS -> router

The rate limiter sits inside the body cap deliberately: a 200 MB body should be
refused on its ``Content-Length`` without spending a limiter round trip.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from .config import settings
from .security import ratelimit

log = logging.getLogger("osprey.http")

#: Correlation id for the in-flight request, readable from anywhere (log filter,
#: error handlers, audit records) without threading it through call signatures.
request_id_var: ContextVar[str] = ContextVar("osprey_request_id", default="")

REQUEST_ID_HEADER = "X-Request-ID"

#: Paths a load balancer or scrape job hits constantly. Excluded from the access
#: log and from rate limiting, or a 1-second Prometheus interval would consume
#: the anonymous budget by itself.
_UNMETERED_PATHS = frozenset({"/health", "/ready", "/live", "/metrics"})


def current_request_id() -> str:
    return request_id_var.get()


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, time the request, emit one structured access line."""

    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        # Accept an upstream id so a trace survives the proxy hop, but only if it
        # looks like one -- it is echoed into responses and logs, so an arbitrary
        # client string is a header-injection and log-forging vector.
        request_id = incoming if _is_safe_request_id(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed = (time.perf_counter() - started) * 1000
            log.exception(
                "%s %s -> 500 in %.1fms (request_id=%s)",
                request.method,
                request.url.path,
                elapsed,
                request_id,
            )
            request_id_var.reset(token)
            # Never leak a traceback to the caller; the id is how support ties the
            # user's report back to the logged exception.
            return JSONResponse(
                {"detail": "internal server error", "request_id": request_id},
                status_code=500,
                headers={REQUEST_ID_HEADER: request_id},
            )
        elapsed = (time.perf_counter() - started) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers["Server-Timing"] = f"app;dur={elapsed:.1f}"
        if request.url.path not in _UNMETERED_PATHS:
            log.info(
                "%s %s -> %s in %.1fms (request_id=%s)",
                request.method,
                request.url.path,
                response.status_code,
                elapsed,
                request_id,
            )
        request_id_var.reset(token)
        return response


def _is_safe_request_id(value: str) -> bool:
    return bool(value) and len(value) <= 128 and all(c.isalnum() or c in "-_." for c in value)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline browser hardening headers.

    The API serves JSON and two HTML pages (``/docs``, ``/redoc``). The CSP is
    therefore restrictive by default and relaxed only for those two paths, which
    load Swagger/ReDoc from a CDN -- the alternative is vendoring both bundles,
    which is the right call for an air-gapped deployment and overkill otherwise.
    """

    _BASE = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-site",
        # No feature this API serves needs any of these.
        "Permissions-Policy": (
            "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
            "magnetometer=(), microphone=(), payment=(), usb=()"
        ),
    }

    _API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    _DOCS_CSP = (
        "default-src 'self'; "
        "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
        "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
        "img-src 'self' data: https://fastapi.tiangolo.com; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
    )
    _DOCS_PATHS = frozenset({"/docs", "/redoc", "/docs/oauth2-redirect"})

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        if not settings.security_headers_enabled:
            return response
        for header, value in self._BASE.items():
            response.headers.setdefault(header, value)
        if request.url.path in self._DOCS_PATHS:
            # Swagger renders in an iframe-free page but needs its own scripts.
            response.headers["Content-Security-Policy"] = self._DOCS_CSP
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
        else:
            response.headers.setdefault("Content-Security-Policy", self._API_CSP)
        # HSTS is meaningless (and harmful on localhost) without TLS, so it is
        # sent only when the request actually arrived over https.
        if settings.is_prod and _is_secure(request):
            response.headers.setdefault(
                "Strict-Transport-Security",
                f"max-age={settings.hsts_max_age_seconds}; includeSubDomains",
            )
        return response


def _is_secure(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    if settings.trust_proxy_headers:
        return request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
    return False


class BodySizeLimitMiddleware:
    """Refuse oversized request bodies with 413.

    Written as raw ASGI rather than ``BaseHTTPMiddleware`` so a streamed body with
    no ``Content-Length`` can be cut off mid-flight: the chunk counter below is
    what stops a chunked upload that never declares its size.
    """

    def __init__(self, app: ASGIApp, max_bytes: int | None = None) -> None:
        self.app = app
        # None => read the live setting on each request. Capturing it at
        # construction would freeze the value at import time, which makes it
        # unchangeable without a restart and untestable without one.
        self._max_bytes = max_bytes

    @property
    def max_bytes(self) -> int:
        if self._max_bytes is not None:
            return self._max_bytes
        return settings.max_request_body_bytes

    async def __call__(self, scope, receive, send) -> None:
        limit = self.max_bytes
        if scope["type"] != "http" or limit <= 0:
            await self.app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > limit:
            await _send_json(send, 413, {"detail": "request body too large"})
            return

        seen = 0
        exceeded = False

        async def counting_receive():
            nonlocal seen, exceeded
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    exceeded = True
                    # Hand the app an empty terminal chunk; it will fail to parse
                    # the truncated body and we have already flagged the reason.
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        started = False

        async def guarded_send(message) -> None:
            nonlocal started
            if exceeded and not started:
                started = True
                await _send_json(send, 413, {"detail": "request body too large"})
                return
            if exceeded:
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        await self.app(scope, counting_receive, guarded_send)


def _content_length(scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _send_json(send, status: int, payload: dict) -> None:
    import json

    body = json.dumps(payload).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Coarse per-caller quota across the whole API.

    Authenticated callers are bucketed by their token subject so one user cannot
    spend a colleague's budget from behind shared NAT; anonymous callers by IP.
    Credential endpoints get their own, much tighter treatment in
    ``api/auth.py`` -- this is the blunt backstop, not the brute-force control.
    """

    async def dispatch(self, request: Request, call_next):
        if not settings.rate_limit_enabled or request.url.path in _UNMETERED_PATHS:
            return await call_next(request)

        subject = _token_subject(request)
        if subject:
            key, limit = f"user:{subject}", settings.rate_limit_authenticated_per_minute
        else:
            key, limit = f"ip:{client_ip(request)}", settings.rate_limit_anonymous_per_minute

        decision = await ratelimit.check(key, limit=limit, window_seconds=60)
        if not decision.allowed:
            log.warning("rate limit exceeded for %s on %s", key, request.url.path)
            return JSONResponse(
                {"detail": "rate limit exceeded"},
                status_code=429,
                headers=_limit_headers(decision) | {"Retry-After": str(decision.retry_after)},
            )
        response = await call_next(request)
        for header, value in _limit_headers(decision).items():
            response.headers.setdefault(header, value)
        return response


class MetricsMiddleware(BaseHTTPMiddleware):
    """Record request count, latency, and concurrency.

    The route *template* is used as the label, not the resolved path, so the
    series count stays proportional to the number of endpoints rather than to
    the number of projects. Requests that match no route collapse to a single
    ``<unmatched>`` series, which stops a 404 scanner from minting series at will.
    """

    async def dispatch(self, request: Request, call_next):
        if not settings.metrics_enabled:
            return await call_next(request)
        from . import metrics

        metrics.http_requests_in_flight.inc()
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            elapsed = time.perf_counter() - started
            metrics.http_requests_in_flight.dec()
            route = _route_template(request)
            metrics.http_requests_total.inc(
                method=request.method, route=route, status=str(status_code)
            )
            metrics.http_request_duration_seconds.observe(
                elapsed, method=request.method, route=route
            )
            if status_code == 429:
                metrics.rate_limit_rejections_total.inc(route=route)
            elif status_code in (401, 403):
                metrics.auth_failures_total.inc(route=route, status=str(status_code))


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return str(path)
    return "<unmatched>"


def _limit_headers(decision: ratelimit.Decision) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset": str(decision.reset_after),
    }


def _token_subject(request: Request) -> str:
    """Best-effort subject from the bearer token, without verifying it.

    Verification happens in the auth dependency. Here we only need a stable
    bucket key, and an unverified one is fine: a forged token still has to pass
    the real check a moment later, and until then it is rate limited like any
    other caller.
    """
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return ""
    token = header.split(" ", 1)[1].strip()
    try:
        import jwt

        claims = jwt.decode(token, options={"verify_signature": False})
    except Exception:  # noqa: BLE001
        return ""
    sub = claims.get("sub", "")
    return str(sub) if isinstance(sub, str | int) else ""


def client_ip(request: Request) -> str:
    """The caller's address, honouring proxy headers only when configured to.

    Taking ``X-Forwarded-For`` on trust lets any client pick its own rate-limit
    bucket by sending a fresh value each request, so this is opt-in and reads the
    *left-most* entry only when we know a proxy set it.
    """
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real = request.headers.get("x-real-ip", "")
        if real:
            return real.strip()
    return request.client.host if request.client else "unknown"
