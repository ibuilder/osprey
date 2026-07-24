"""OpenTelemetry tracing/metrics — optional, guarded.

Enabled only when ``OSPREY_OTEL_ENABLED`` is set and the ``otel`` extra is installed,
so the default (and test) path pulls in nothing and does nothing. Instruments
FastAPI, SQLAlchemy, and httpx, exporting via OTLP. PII scrubbing is handled at the
logging layer (see ``logging_setup``).
"""

from __future__ import annotations

import logging

from .config import settings

log = logging.getLogger("osprey.otel")


def setup_observability(app) -> bool:
    """Instrument the app if OTel is enabled + available. Returns True if wired."""
    if not settings.otel_enabled:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except Exception as exc:  # noqa: BLE001
        log.warning("OTel enabled but instrumentation libs missing (%s); skipping", exc)
        return False

    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name})
    )
    exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint or None)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()
    try:
        from .db import get_engine

        SQLAlchemyInstrumentor().instrument(engine=get_engine().sync_engine)
    except Exception as exc:  # noqa: BLE001
        log.warning("SQLAlchemy instrumentation skipped: %s", exc)

    log.info("OpenTelemetry instrumentation enabled (service=%s)", settings.otel_service_name)
    return True
