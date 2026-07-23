# Osprey

> **The foreman that never sleeps.**
> Open-source, self-hostable background agent that watches every source on a construction / real-estate project and surfaces the one thing to act on now.

[![CI](https://github.com/ospreyhq/osprey/actions/workflows/ci.yml/badge.svg)](https://github.com/ospreyhq/osprey/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-0E1A2B.svg)](LICENSE)

Osprey connects to the email accounts and platforms a construction/RE team already
uses (Outlook, Gmail, Procore, calendars, file drops), continuously ingests everything,
and produces a single **prioritized hotlist** of what the project needs to handle right
now — with the *why*, the source link, the dollar/schedule exposure, and a recommended
next action. Exportable to Excel and PDF.

This repository is the **backend brain** — the always-on service described in
[`SPEC.md`](SPEC.md) §2. The clients (Tauri desktop/mobile) are tracked separately.

---

## What's built here

| Area | Status |
|---|---|
| Monorepo skeleton, config, DB layer, migrations baseline | ✅ |
| Data model (Org → User → Project → Connection → Signal → Item → Score → Action) | ✅ |
| Connector framework (ABC + registry) | ✅ |
| Universal **File-Drop / IMAP / Forward-To** fallback connector | ✅ |
| Connectors: **Outlook · Gmail · Google Calendar · Procore** (OAuth2 + delta/webhook) | ✅ |
| **Desktop-app OAuth** — user authorizes each source in their own browser (loopback + PKCE), tokens sealed server-side, never via any AI/MCP layer | ✅ |
| Engine: cluster → extract → **explainable score** → rank → hotlist | ✅ |
| AI layer: pluggable (deterministic offline default · Claude · Ollama) | ✅ |
| **Bring-your-own AI** connection + natural-language **sift → hotlist** (cited findings) | ✅ |
| **User Python background scripts** (sandboxed) that emit signals into the hotlist | ✅ |
| Exports: styled Excel + branded PDF from one `HotlistSnapshot` | ✅ |
| Security: token vault (AES-GCM), RBAC, JWT auth, append-only audit log | ✅ |
| REST + webhook + **WebSocket (live hotlist)** API (FastAPI) | ✅ |
| Background workers (ARQ: poll · ingest · score · run-scripts · notify) | ✅ |
| **Push**: device registration + APNs/FCM/Web-Push sender abstraction | ✅ |
| Admin console (connection health · audit verify · stats · feature flags) | ✅ |
| **Tauri 2.0 desktop client** (tray · live hotlist · connect · AI · scripts) + **mobile viewer** scaffold | ✅ |
| Test suite (pytest, 49 tests, runs on SQLite, no external services) | ✅ |
| docker-compose + **Helm chart** (api · worker · migrations · ingress) | ✅ |
| CI (ruff · mypy · pytest · SBOM · Trivy) | ✅ |

## Quick start (local, no Docker)

```bash
cd backend
python -m venv .venv && . .venv/Scripts/activate   # (.venv/bin/activate on *nix)
pip install -e ".[dev]"
cp ../.env.example .env
pytest -q                                            # full suite, offline
uvicorn osprey.main:app --reload                     # http://localhost:8000/docs
```

The app boots against **SQLite by default** so it runs with zero infrastructure.
Point `OSPREY_DATABASE_URL` at Postgres (with pgvector) for production.

## Self-host (Docker)

```bash
cp .env.example .env      # set OSPREY_SECRET_KEY + OSPREY_ENCRYPTION_KEY
docker compose up         # api :8000, worker, postgres+pgvector, redis
```

## Design principles (the golden rules)

- **Least-privilege, read-only OAuth.** Never store a source-account password.
- **Encrypt tokens at rest** (AES-256-GCM envelope; OS keychain in local mode).
- **Idempotent ingestion** — dedupe on `external_id`; pollers use rate-limit + backoff.
- **Explainable scoring** — every hotlist item shows its factor breakdown and cites
  source text. No black-box ranking until there's feedback data.
- **Contractual notice deadlines are weighted highest** — missing one can waive a claim.
- **Excel and PDF exports derive from the same `HotlistSnapshot`.**

See [`CLAUDE.md`](CLAUDE.md) for the full agent operating guide and [`SPEC.md`](SPEC.md)
for the complete build specification.

## License

Core is **AGPL-3.0** (see [`LICENSE`](LICENSE)). Connector SDK and client libs are
Apache-2.0/MIT. See [`SECURITY.md`](SECURITY.md) for the security posture and
responsible-disclosure policy.
