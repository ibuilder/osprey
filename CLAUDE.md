# Osprey — Agent Operating Guide

## What this is
Free, open-source, self-hostable background agent that ingests construction/RE
data sources and produces a prioritized, exportable hotlist. Read `SPEC.md` first.

## Architecture (do not drift)
- Backend brain: Python 3.11+ + FastAPI + Postgres/pgvector + Redis + ARQ.
- Clients: Tauri 2.0 (Rust core + React/TS) — one codebase for win/mac/ios/android.
- Mobile is a VIEWER + push. The always-on monitor is the backend or the desktop
  Rust local-agent. Never try to run continuous background work on iOS/Android.
- Every connector implements the `Connector` ABC. New source = new plugin, never a
  core edit.

## Golden rules
- Least-privilege, read-only OAuth scopes. Never store source-account passwords.
- Encrypt tokens at rest (Vault/KMS on server; OS keychain in local mode).
- All webhook processing idempotent (dedupe on `external_id`). All pollers use
  rate-limit + exponential backoff.
- Scoring must stay EXPLAINABLE: every hotlist item shows its factor breakdown
  and cites source text. No black-box ranking until there's feedback data.
- Weight contractual NOTICE deadlines highest — missing one can waive a claim.
- Excel and PDF exports derive from the SAME `HotlistSnapshot`.

## Where things live (backend/osprey)
- `config.py`        settings (pydantic-settings, `OSPREY_` env prefix)
- `db.py`            async engine/session; SQLite (dev/test) or Postgres (prod)
- `models/`          SQLModel tables (§5 of SPEC)
- `schemas/`         Pydantic DTOs
- `security/`        crypto (AES-GCM token vault), rbac, auth (JWT), audit log
- `connectors/`      base ABC + registry; `filedrop/` universal fallback; `outlook/`
- `normalize/`       raw payload -> Signal
- `ai/`              LLM provider abstraction: deterministic (default) / claude / ollama
- `engine/`          cluster, extract, score (explainable), hotlist builder
- `exports/`         excel.py (openpyxl), pdf.py (reportlab)
- `api/`             routers: health, auth, connections, hotlist, items, exports, webhooks
- `workers/`         ARQ tasks: poll, ingest, score, notify

## Testability contract
The whole pipeline runs OFFLINE and DETERMINISTICALLY:
- Default AI provider is rule-based (`DeterministicProvider`) — no network, no keys.
- Embeddings default to a deterministic `HashingEmbedder` — no model download.
- Tests run on SQLite; embeddings clustered with in-process cosine similarity.
This is why `pytest` passes with zero infrastructure. Postgres+pgvector and Claude
are production opt-ins, selected purely by config.

## Workflow
Work phase-by-phase per `SPEC.md` §13. Meet a phase's acceptance criteria (with
tests) before starting the next. Run `ruff` + `mypy` + `pytest` before every commit.
Connectors are tested against recorded fixtures — never live production data.

## Commands
- Boot (infra):  `docker compose up`
- Boot (local):  `uvicorn osprey.main:app --reload`
- Test:          `pytest` / `ruff check` / `mypy osprey`
- Migrate:       `alembic upgrade head`
