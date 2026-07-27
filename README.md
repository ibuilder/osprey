# Osprey

> **The foreman that never sleeps.**

[![CI](https://github.com/ibuilder/osprey/actions/workflows/ci.yml/badge.svg)](https://github.com/ibuilder/osprey/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-0E1A2B.svg)](LICENSE)
&nbsp;·&nbsp; [**Website**](https://ibuilder.github.io/osprey/)

Every project buries the thing that matters under a thousand emails, RFIs, submittals,
change orders, invoices, and calendar invites — spread across systems that don't talk
to each other. **Osprey watches all of them at once and hands you the five things that
actually need you today** — with the reason why, the dollars and schedule at stake, the
deadline, the source, and the recommended next move.

It's **free, open source, and runs on your own server**, so your project data never
leaves your control. No seat licenses. No vendor lock-in.

### What you get

- **One prioritized hotlist** across all your sources — Outlook, Gmail, Procore,
  calendars, and a catch-all *forward-an-email / drop-a-CSV* fallback for everything else.
- **Every item explains itself** — why it ranked where it did, the $ exposure, the
  deadline, links back to the source, and a concrete next action. No black box.
- **Contract notice deadlines weighted highest** — miss one and you can waive a claim
  worth more than the whole fee. Osprey is built to never let that happen quietly.
- **🔴 Act today / 🟠 This week / 🟡 Watch** buckets, and one-click **Excel + PDF export**
  for your OAC meeting.
- **Ask your own AI** to sift the project ("flag anything about liquidated damages") and
  push what it finds straight onto the hotlist — using *your* Claude/OpenAI key.
- **Private by design** — self-hosted; least-privilege, read-only access to your accounts;
  tokens encrypted; nothing routed through a third party.

### See it work in 30 seconds

```bash
cd backend && python -m osprey.seed
```

```
  Tower B — 8 items, $279,000 exposure

  1. [ACT TODAY] [83] NOTICE OF DELAY — differing site conditions   - $180,000
  2. [ACT TODAY] [74] Safety observation — missing fall protection at level 3
  3. [THIS WEEK] [64] PCO-088 — slab thickening at loading dock     - $45,000
  4. [THIS WEEK] [54] RFI-0500 — curtain wall anchor spacing at grid C-4
  5. [THIS WEEK] [51] Pay Application 07 — retention release         - $54,000
  ...                                            → demo/hotlist.xlsx · demo/hotlist.pdf
```

### The two pieces

- **The brain** (this repo, `backend/`) — the always-on service that does the watching,
  ranking, and exporting. Runs on a small server or a spare machine, or fully local.
- **The apps** (`clients/`) — a desktop app (system-tray, live hotlist, connect your
  accounts) and a mobile viewer. You read and act; the brain never sleeps.

> **Heads up:** Osprey is self-hosted, so first-time setup is a one-time IT step (or a
> tech-savvy PM following the [setup guide](docs/connecting.md)). After that, connecting
> an account is point-and-click in the desktop app.

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
| Test suite (97 tests: connector poll-loops, Postgres **RLS isolation proven**, ~79% coverage) | ✅ |
| docker-compose + **Helm chart** (api · worker · migrations · ingress) | ✅ |
| CI (9 blocking jobs): Python **3.11/3.12/3.13** · ruff lint+format · mypy · coverage gate · **Postgres+pgvector** (migrations, drift, asyncpg suite) · frontend · **Rust** (fmt/clippy/build) · **Helm lint+render** · **live kind deploy smoke (RLS enforced end-to-end)** · SBOM · pip-audit / npm-audit / Trivy | ✅ |

## Quick start (local, no Docker)

```bash
cd backend
python -m venv .venv && . .venv/Scripts/activate   # (.venv/bin/activate on *nix)
pip install -e ".[dev]"
cp ../.env.example .env
pytest -q                                            # full suite, offline
uvicorn osprey.main:app --reload                     # http://localhost:8000/docs
```

The app boots against **SQLite by default** so it runs with zero infrastructure
(try `python -m osprey.seed` — see the demo above). Point `OSPREY_DATABASE_URL` at
Postgres (with pgvector) for production.

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

Known deferrals and their rationale live in [`docs/backlog.md`](docs/backlog.md).

See [`CLAUDE.md`](CLAUDE.md) for the full agent operating guide and [`SPEC.md`](SPEC.md)
for the complete build specification.

## License

Core is **AGPL-3.0** (see [`LICENSE`](LICENSE)). Connector SDK and client libs are
Apache-2.0/MIT. See [`SECURITY.md`](SECURITY.md) for the security posture and
responsible-disclosure policy.
