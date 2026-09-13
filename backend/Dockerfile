# Osprey backend — API + worker share one image.
#
# Multi-stage: the compiler toolchain builds wheels in `builder` and is left
# behind. A single-stage build ships build-essential (~200 MB, and a large part
# of the image's CVE surface) into production for no runtime benefit.
FROM python:3.14-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml constraints.txt constraints-prod.txt ./
COPY osprey ./osprey

# Which optional extras go in the image. `otel` is out of the default because it
# drags in grpcio -- tens of megabytes and its own CVE stream -- for a feature
# that stays off unless OSPREY_OTEL_ENABLED is set. Turn it on for a tracing
# deployment:  docker build --build-arg EXTRAS=prod,ai,push,otel .
ARG EXTRAS=prod,ai,push

# Installed into a self-contained venv the runtime stage copies wholesale, and
# pinned by BOTH constraint files: constraints.txt covers the set the test suite
# is green on, constraints-prod.txt the runtime extras it never installs. Without
# the second file the deployed image would be the one artefact with unpinned
# dependencies, which is the opposite of where pinning matters most.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install \
        -c constraints.txt -c constraints-prod.txt ".[${EXTRAS}]"


FROM python:3.14-slim AS runtime

# Populated by the release workflow; `docker inspect` then tells you exactly
# which commit an image came from, which matters when a rollback is urgent.
ARG VERSION=0.0.0
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="Osprey" \
      org.opencontainers.image.description="The foreman that never sleeps — backend brain." \
      org.opencontainers.image.source="https://github.com/ibuilder/osprey" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    OSPREY_ENV=prod

WORKDIR /app

# curl is the only runtime package added, and only because the HEALTHCHECK needs
# a client. Everything else stays out.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin osprey

COPY --from=builder /opt/venv /opt/venv
COPY --chown=osprey:osprey osprey ./osprey
COPY --chown=osprey:osprey alembic.ini ./
COPY --chown=osprey:osprey alembic ./alembic
COPY --chown=osprey:osprey pyproject.toml ./

USER osprey

EXPOSE 8000

# /live, not /health: liveness must not depend on the database. A DB outage
# should drain traffic (via /ready) without also restarting every container.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/live || exit 1

# --proxy-headers so the app sees the real scheme behind an ingress; it still
# only *trusts* them when OSPREY_TRUST_PROXY_HEADERS is set.
CMD ["uvicorn", "osprey.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--timeout-graceful-shutdown", "30"]
