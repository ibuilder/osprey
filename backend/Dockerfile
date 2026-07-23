# Osprey backend — API + worker share one image.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps kept minimal (reportlab needs no native libs; WeasyPrint is not used).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY osprey ./osprey
COPY alembic.ini ./
COPY alembic ./alembic

RUN pip install --upgrade pip && pip install -e ".[prod,ai]"

# Non-root runtime user.
RUN useradd -m -u 10001 osprey && chown -R osprey:osprey /app
USER osprey

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "osprey.main:app", "--host", "0.0.0.0", "--port", "8000"]
