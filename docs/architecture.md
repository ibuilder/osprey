# Architecture

Osprey's always-on brain is a service; clients are viewers. The backend in this
repo implements the pipeline end to end.

## The pipeline

```
 Sources ──> Connectors ──> Ingest ──> Cluster ──> Extract ──> Score ──> Hotlist ──> Exports
 (email,     (poll +        (dedupe,   (thread +   (AI, with   (explain-  (rank,      (xlsx,
  Procore,    webhook)       embed)     cosine)     citations)  able)      bucket)     pdf)
  files…)
                                                                              │
                                                              Actions ────────┘ (learning loop)
```

Each stage is a module under `backend/osprey/engine/` (plus `connectors/`, `ai/`,
`exports/`). The stages are pure/async functions so the whole flow is unit-testable
offline — see `tests/test_engine.py`.

## Key decisions

- **Async everywhere (FastAPI + SQLAlchemy async).** The workload is I/O-bound
  (hundreds of connector calls, webhooks, LLM calls).
- **Portable storage.** UUID-hex PKs, JSON columns, tz-aware UTC datetimes → the
  identical schema runs on SQLite (dev/test) and Postgres (prod). Embeddings are
  stored as JSON and compared in-process; pgvector is a drop-in prod optimization
  behind `engine/cluster.py` with unchanged clustering semantics.
- **Pluggable AI, offline default.** `ai/provider.py` selects deterministic (offline,
  no keys), Claude, or Ollama. A `ResilientProvider` always degrades to the rule-based
  extractor, so the pipeline never fails for lack of a model.
- **Explainable scoring.** `engine/score.py` is a transparent weighted rubric
  (urgency × impact × confidence); every item carries its factor breakdown and
  source citations. Contractual notice deadlines are weighted highest.
- **One snapshot, two exports.** Excel and PDF both derive from the same immutable
  `HotlistSnapshot.payload`, so they can never disagree.

## Security posture

Token vault (AES-256-GCM), JWT auth, RBAC (owner/admin/pm/viewer), hash-chained
append-only audit log, HMAC-verified webhooks, PII-scrubbing logs. See
[`SECURITY.md`](../SECURITY.md).

## What runs where

| Component | Command | Notes |
|---|---|---|
| API | `uvicorn osprey.main:app` | REST + webhooks + OpenAPI at `/docs` |
| Worker | `arq osprey.workers.main.WorkerSettings` | 5-min poll cron + on-demand jobs |
| DB / queue | `docker compose up` | Postgres+pgvector + Redis |

Clients (Tauri desktop/mobile) are separate and consume this API; the desktop
"privacy mode" local agent (SPEC §2) reuses `engine/` + `connectors/` in-process.
