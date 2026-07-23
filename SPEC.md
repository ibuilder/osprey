# Osprey — Build Specification

> **Name:** Osprey — the raptor that hovers over the whole area, watching everything at once, then dives on the single target that matters. Exactly the right metaphor for a tool that watches your entire project and hands you the one thing to act on now.
>
> **GitHub org / packages:** `ospreyhq` — the bare `osprey` handle is taken, but `ospreyhq` is clean across GitHub, npm, and PyPI. Packages ship scoped: `@ospreyhq/*` (npm), `osprey-core` / `osprey-connectors` (PyPI). The product is branded simply **Osprey**; the qualified handle is only the namespace.
>
> **Brand kit:** logos, app icon/favicon, color + type tokens, and bundled OFL fonts live in the `osprey-brand/` package (see `BRAND_KIT.md`). Colors: Ink `#0E1A2B`, Ember `#FF6A2B`. Fonts: Space Grotesk (display), Inter (UI), JetBrains Mono (data). Drop `BRAND_KIT.md` in the repo root as `BRAND.md` so the client theming pulls from it.

> **What this document is:** a complete, agent-ready build spec. Drop it into your repo as `SPEC.md`, generate the `CLAUDE.md` in §17, and drive Claude Code phase by phase using the task breakdowns in §16. Every phase has concrete deliverables and acceptance criteria so an agent can self-check.

---

## 1. Product vision

Osprey is a **free, open-source, self-hostable background agent** that connects to the email accounts and platforms a construction or real-estate team already uses, continuously watches everything flowing through them, and produces a single **prioritized hotlist** of what the project needs to handle *right now* — with the reason why, the source link, the dollar/schedule exposure, and a recommended next action. Exportable to Excel and PDF.

**One-line pitch:** *The foreman that never sleeps — it reads every email, RFI, submittal, change order, and invoice across all your systems and hands you the five things that actually matter today.*

### The wedge (why this exists)
The problem is structural, not a missing feature. A live construction or RE project
generates a relentless stream across many disconnected systems — email, project-
management platforms, ERP/accounting, calendars, and document stores. The signal
that matters *today* (a notice deadline, a stalled RFI, an unpaid pay application,
a schedule slip) is buried in that noise, split across tools that don't talk to each
other, and only surfaced when someone happens to look. Nobody watches the whole
field at once.

Osprey's answer rests on four choices, each a structural advantage rather than a
feature that can simply be copied:

1. **Free + open source (AGPL-3.0 core).** No seat licenses. A general contractor or
   small development shop can run it forever at zero cost.
2. **Self-hostable / privacy-first.** Data never has to leave the org — decisive for
   owners, lenders, and firms with NDA or data-residency constraints.
3. **Vendor-neutral aggregation.** One hotlist across *all* sources (email + project
   platforms + ERP + calendar + docs) — no single vendor's walled garden.
4. **Pluggable connector SDK.** The community adds sources a single team would never
   staff, so coverage compounds over time instead of being gated by a roadmap.

---

## 2. The one architecture decision that governs everything

**"Runs in the background 24/7" cannot live on a phone.** iOS and Android forcibly kill long-running background processes; there is no supported way to run a persistent monitor on a mobile OS. Any plan that promises continuous mobile background monitoring is wrong on the platform physics.

**Therefore the always-on brain is a service, and the phones are viewers:**

```
                       ┌───────────────────────────────────────────┐
                       │          HARRIER BACKEND ("the brain")     │
                       │   Always-on. Self-hosted or cloud.         │
                       │                                            │
  Outlook/O365 ──┐     │  ┌──────────┐  ┌───────────┐  ┌─────────┐ │
  Gmail ─────────┤     │  │Connector │→ │ Normalizer│→ │ Scoring │ │
  Procore ───────┼─────┼─→│  workers │  │ (Signals) │  │ engine  │ │
  Sage ──────────┤ web │  └──────────┘  └───────────┘  └────┬────┘ │
  Argus (file) ──┤ hook│         ↑ poll + webhooks           │      │
  Calendar ──────┘  +  │  ┌──────────────┐            ┌──────▼────┐ │
                  poll │  │ Postgres +    │◀───────────│  Hotlist  │ │
                       │  │ pgvector +    │            │  builder  │ │
                       │  │ Redis queue   │            └──────┬────┘ │
                       │  └──────────────┘                   │      │
                       │        REST + WebSocket API  ◀───────┘      │
                       └───────────┬──────────────────┬─────────────┘
                                   │                  │ APNs / FCM push
              ┌────────────────────┼──────────┐       │
        ┌─────▼─────┐        ┌─────▼─────┐  ┌──▼────────▼──┐
        │  Desktop   │       │  Desktop   │  │   Mobile     │
        │  Win/Mac   │       │  (privacy  │  │ iOS/Android  │
        │  Tauri +   │       │   mode:    │  │  Tauri       │
        │  tray svc  │       │   local    │  │  viewer +    │
        └────────────┘       │   agent)   │  │  push        │
                             └────────────┘  └──────────────┘
```

### Verdict: it's not Rust *or* Python Flask — it's both, each where it wins

| Layer | Choice | Why |
|---|---|---|
| **Backend brain** | **Python 3.12 + FastAPI** (not Flask) | The workload is I/O-bound: hundreds of concurrent connector calls, webhooks, LLM calls. FastAPI is async-native; Flask is synchronous and would need Celery gymnastics to keep up. Python also has the richest ecosystem for Graph/Gmail/Procore SDKs, email parsing, and the AI layer. You already ship Flask (FieldForge), so the ramp is near-zero. *If you insist on Flask, use it with Celery workers — but FastAPI is the right call here.* |
| **Clients (all 4 OSes)** | **Tauri 2.0** (Rust core + web frontend) | One codebase → Windows, macOS, iOS, Android. ~3–5 MB binaries vs Electron's 150 MB, ~45 MB RAM vs ~280 MB, granular permission model, native webviews. This is where **Rust earns its place**: the secure, tiny, cross-platform shell. |
| **Desktop "privacy mode" agent** (optional) | **Rust sidecar inside Tauri** | Lets a desktop user run the *entire* monitor locally with no server — data never leaves the machine. The killer feature for privacy-sensitive firms. |
| **AI/analysis** | Pluggable: **Anthropic Claude API** (default) or **local via Ollama** (privacy mode) | Extraction, classification, summarization, "why this matters + next action." Local option keeps offline/on-prem promise real. |

**Net:** Python FastAPI backend + Tauri 2 clients + optional Rust local agent. This satisfies "Rust or Python," is faster to build than pure Rust, and is more secure and lighter than an Electron/Python-desktop combo.

---

## 3. Tech stack (pin these)

**Backend**
- Python 3.12, FastAPI, Uvicorn/Gunicorn
- Pydantic v2 (schemas/validation), SQLModel or SQLAlchemy 2.0 + Alembic (migrations)
- Postgres 16 + **pgvector** (embeddings/RAG), Redis 7 (queue + cache)
- **ARQ** or Celery for scheduled polling + background jobs (ARQ is async-native, lighter)
- httpx (async HTTP), tenacity (retry/backoff), APScheduler for cron-style polls
- Auth: Authlib (OAuth2 clients), OIDC for app login, `python-jose` for JWTs

**AI layer**
- `anthropic` SDK (default), `ollama` client (local mode)
- Structured extraction via tool-use / JSON mode; RAG over docs with pgvector

**Exports**
- Excel: `openpyxl` (styled workbook) — see §11
- PDF: `WeasyPrint` (HTML→PDF, easiest to style) or `ReportLab` (programmatic)

**Clients**
- Tauri 2.0, React 18 + TypeScript + Vite, TanStack Query, Tailwind
- Desktop background presence: system tray / menubar + OS service (launchd / Windows Service / systemd)
- Push: APNs (iOS), FCM (Android), Web Push (desktop)

**Infra / DevEx**
- Docker + docker-compose (self-host one-liner), Helm chart (k8s)
- GitHub Actions CI, `pytest` + `ruff` + `mypy`, `vitest`/Playwright for clients
- Supply chain: Syft (SBOM), Trivy + Dependabot (scanning), Sigstore/cosign (signed releases)
- Observability: OpenTelemetry → Prometheus/Grafana + structured logs

---

## 4. Monorepo layout

```
osprey/
├── CLAUDE.md                      # agent operating guide (see §17)
├── SPEC.md                        # this document
├── LICENSE                        # AGPL-3.0 (core)
├── docker-compose.yml             # self-host: db + redis + api + worker
├── deploy/helm/                   # k8s chart
├── backend/
│   ├── pyproject.toml
│   ├── alembic/
│   ├── osprey/
│   │   ├── main.py                # FastAPI app factory
│   │   ├── config.py              # settings (pydantic-settings)
│   │   ├── db.py                  # engine/session
│   │   ├── models/                # SQLModel tables (§8)
│   │   ├── schemas/               # Pydantic DTOs
│   │   ├── api/                   # routers: auth, connections, hotlist, items, exports, webhooks
│   │   ├── connectors/            # plugin framework (§9)
│   │   │   ├── base.py            # Connector ABC + registry
│   │   │   ├── outlook/           # MS Graph
│   │   │   ├── gmail/
│   │   │   ├── procore/
│   │   │   ├── calendar/
│   │   │   ├── filedrop/          # universal CSV/IMAP/forward-to fallback
│   │   │   └── ...                # sage/, argus/, acc/ (Tier 2/3)
│   │   ├── normalize/             # raw payload → Signal
│   │   ├── engine/                # scoring, clustering, hotlist builder (§10)
│   │   ├── ai/                    # llm client abstraction + prompts
│   │   ├── exports/               # excel.py, pdf.py (§11)
│   │   ├── security/              # crypto, token vault, rbac, audit
│   │   └── workers/               # ARQ tasks: poll, ingest, score, notify
│   └── tests/
├── clients/
│   ├── shared/                    # TS types (generated from OpenAPI), UI kit
│   ├── desktop/                   # Tauri (win/mac) + tray + optional local agent
│   │   └── src-tauri/agent/       # Rust local polling engine (privacy mode)
│   └── mobile/                    # Tauri iOS/Android viewer + push
├── connectors-sdk/                # docs + template for community connectors
└── docs/                          # architecture, connector-authoring, security
```

---

## 5. Data model (core tables)

```
Org ──< User ──< Membership (role: owner/admin/pm/viewer)
Org ──< Project
Org ──< Connection   (source_type, account_ref, oauth_tokens[encrypted], scopes, status, last_sync)
Project ──< Signal   (raw normalized event)
Signal  ──> Item     (clustered/deduped unit of work)
Item    ──< Score    (versioned; urgency, impact, confidence, total, explanation)
Item    ──< Action   (user feedback: done/snooze/dismiss/escalate/assign) → tunes weights
Project ──< HotlistSnapshot (immutable, for export + history)
Org     ──< AuditLog (who/what/when, tamper-evident)
```

**Signal** (the normalization target every connector emits):
```python
class Signal(SQLModel, table=True):
    id: UUID
    project_id: UUID
    connection_id: UUID
    source_type: str          # "outlook" | "procore" | "gmail" | ...
    source_kind: str          # "email" | "rfi" | "submittal" | "change_order" | "invoice" | "task" | "event" | "doc"
    external_id: str          # dedupe key within source
    thread_key: str | None    # links related signals (email thread / RFI number)
    title: str
    body: str                 # cleaned text
    participants: list[str]    # emails/users
    due_at: datetime | None
    amount: Decimal | None     # $ exposure if present
    url: str | None            # deep link back to source
    raw: dict                  # original payload (jsonb)
    occurred_at: datetime
    ingested_at: datetime
    embedding: Vector | None   # pgvector, for clustering + RAG
```

**Item** = one or more clustered Signals (e.g., an RFI that appears in email *and* Procore = one Item). **Score** is versioned so you can show trend and audit *why* something ranked where it did.

---

## 6. Connectors — framework, tiering, and the fallback that guarantees coverage

Every connector implements one interface so the community can add sources without touching the core:

```python
class Connector(ABC):
    source_type: str
    scopes: list[str]
    supports_webhooks: bool

    async def authorize(self, ...) -> Connection: ...      # OAuth2 flow
    async def poll(self, conn: Connection, since: datetime) -> AsyncIterator[RawEvent]: ...
    async def handle_webhook(self, payload: dict) -> AsyncIterator[RawEvent]: ...
    async def normalize(self, raw: RawEvent) -> Signal: ...
    async def healthcheck(self, conn: Connection) -> Health: ...
```

**Ingestion pattern:** webhooks for near-real-time where supported + delta/incremental polling as the reliable backbone. All webhook processing is **idempotent** (dedupe on `external_id`) and all pollers use per-connector **rate-limit + exponential backoff** (tenacity).

### Tier 1 — launch connectors
| Source | API | Real-time mechanism | Notes |
|---|---|---|---|
| **Microsoft 365 / Outlook** | Microsoft Graph | Change notifications (webhooks) + delta queries | OAuth2 (Entra). Least-privilege: `Mail.Read`, `Calendars.Read`. Renew subscriptions before expiry. |
| **Gmail / Google Workspace** | Gmail API | Pub/Sub push (`watch`) + History API | OAuth2. Scope `gmail.readonly`. Handle history-ID gaps with full re-sync. |
| **Procore** | REST v1.1 + Webhooks | Webhooks (create/update/delete per `resource_name`) | OAuth2, free dev account + **sandbox**. Subscribe only to RFIs, submittals, change orders, observations, invoices to cut noise. Header `Procore-Company-Id`. |
| **Calendar (M365 + Google)** | Graph / Google Calendar | Webhooks + delta | Deadlines and meetings are high-signal for urgency scoring. |

### Tier 2 — fast-follow
- **Sage** — Intacct (Web Services/REST) and 300 CRE / 100 Contractor (often reached via the Procore↔Sage connector or a direct integration). Financial signals: unpaid invoices, commitments, cost overruns.
- **Autodesk Construction Cloud / BIM 360** — Autodesk Platform Services (APS). RFIs, submittals, issues, docs.
- **Docs**: SharePoint / Google Drive / Egnyte / Box — surface new/changed contract + spec docs into the RAG index.

### Tier 3 — hard sources (be honest about these)
- **Argus Enterprise** (Altus) — desktop valuation software with **limited/gated API access**; most shops export to Excel. **Ship the file-drop/CSV ingestion path for Argus first**, add direct API only if/when access is granted. Don't promise a clean Argus API you can't guarantee.
- **Yardi / MRI** — enterprise, gated; treat like Argus.

### The universal fallback (build this in Phase 1 — it guarantees coverage of the long tail)
A **File-Drop / IMAP / Forward-To** connector:
- A monitored **forward-to address** (`project-x@in.yourhost`) — users/rules forward anything; Osprey parses it into Signals.
- **IMAP** polling for any mailbox without a modern API.
- **Folder/CSV watcher** — drop an Argus/Sage export in a watched folder (or S3 prefix) and it ingests.

This means Osprey has *something* for every source on day one, even before a bespoke connector exists.

---

## 7. The engine — how raw noise becomes a ranked hotlist (the heart of the product)

Pipeline: **Ingest → Normalize → Cluster → Extract → Score → Rank → Explain → Learn.**

### 7.1 Cluster & dedupe
Group Signals into Items by `thread_key`, `external_id` cross-refs, and embedding similarity (pgvector cosine). One real-world thing = one Item, even across sources.

### 7.2 Extract (LLM, structured)
For each Item, the AI layer returns **structured JSON** (tool-use / JSON mode), with **citations back to source text** so every conclusion is verifiable — the trust feature that lets a user check *why* an item was flagged rather than taking a score on faith:
```json
{
  "category": "rfi | change_order | submittal | invoice | safety | schedule | contractual_notice | general",
  "summary": "one sentence",
  "deadline": "ISO8601 | null",
  "dollar_exposure": "number | null",
  "notice_deadline": true,
  "blocking": ["what it blocks"],
  "recommended_action": "concrete next step + owner",
  "citations": [{"signal_id": "...", "quote_span": "..."}]
}
```

### 7.3 Score (transparent first, ML later)
Start with an **explainable weighted rubric** — users must be able to see *why* something is #1. Total = weighted blend of:

- **Urgency** — time to deadline, aging since last activity, response-SLA breaches, meeting proximity.
- **Impact** — dollar exposure, critical-path/schedule impact, safety, and **contractual notice deadlines** (weight these *highest* — a missed notice deadline can waive a claim worth more than the whole project fee; this domain nuance is what makes Osprey feel built by a builder).
- **Confidence** — extraction certainty; low-confidence items get flagged, not buried.

```
score = w_u·urgency + w_i·impact + w_c·confidence_penalty
```
Weights live in config, are **per-project tunable**, and are adjusted by the learning loop. Every Item shows its factor breakdown. **Only after** you have feedback data should you consider an ML ranker — explainable rubric first, always.

### 7.4 Rank & present — the Hotlist
Top-N items, each row:
> **What** · **Why it matters** · **Source link(s)** · **Owner** · **Due** · **$ exposure** · **Recommended action** · **Score + factor breakdown**

Buckets: 🔴 Act today / 🟠 This week / 🟡 Watch. Pareto view (the 20% driving 80% of exposure).

### 7.5 Learn
User actions (done/snooze/dismiss/escalate/reassign) feed back to nudge weights per project and per user. Dismissed categories decay; escalated ones amplify. Keep it transparent and reversible.

---

## 8. Exports — the hotlist as Excel & PDF

- **Excel** (`openpyxl`): styled workbook — Summary sheet (buckets, totals, $ exposure rollup), Hotlist sheet (one row per Item with clickable source links + score breakdown), Raw sheet (audit). Conditional formatting by bucket. Frozen header, autofilter.
- **PDF** (`WeasyPrint`): branded one-pager — title block (project, date, prepared-by), bucketed hotlist, per-item why/action/exposure, footer with generation timestamp + item count. Deterministic, print-ready.
- Both generated from the **same `HotlistSnapshot`** so Excel and PDF always agree. Expose `GET /projects/{id}/hotlist/export?format=xlsx|pdf`.

---

## 9. Security — turning "bank-level / 100%" into controls you can actually stand behind

> Straight talk: "100% secure" and "bank-level" are marketing phrases, not achievable absolutes — no system is 100% secure. What you *can* credibly claim is **enterprise-grade, defense-in-depth, and self-hostable so data never leaves the customer.** Here's the concrete control set that backs that claim:

**Identity & access**
- OAuth2 **only** — never store a user's source-account password. Request **least-privilege, read-only scopes**.
- App login via OIDC/SSO; **RBAC** (owner/admin/pm/viewer); optional **SCIM** for enterprise provisioning.

**Data protection**
- **In transit:** TLS 1.3 everywhere; HSTS.
- **At rest:** AES-256-GCM. Connector tokens encrypted with envelope encryption; keys in **KMS/HashiCorp Vault** (server) or **OS keychain** — DPAPI (Windows), Keychain (macOS), libsecret (Linux) — in desktop/local mode.
- **Privacy mode:** fully local/self-hosted; local LLM via Ollama; data never transits a third party. Make this a first-class, documented deployment.

**App hardening**
- Idempotent, signature-verified webhooks; per-connector rate-limit + backoff; input validation (Pydantic) at every boundary.
- Tamper-evident **audit log** (append-only) for every data access and config change.
- Secrets never in code/logs; `.env` + Vault; secret scanning in CI.

**Supply chain (this is where open-source projects earn or lose trust)**
- **SBOM** (Syft) published per release; vuln scanning (Trivy + Dependabot); **signed releases** (Sigstore/cosign); pinned dependencies; reproducible Docker builds.

**Operational**
- Encrypted backups + tested restore/DR; data-retention + right-to-delete controls (GDPR/CCPA posture); OpenTelemetry tracing with PII scrubbing.
- Document a path toward **SOC 2 Type II** alignment even before formal audit — controls above map to it. Publish a `SECURITY.md` + threat model + responsible-disclosure policy.

---

## 10. Enterprise-grade layer
Multi-tenancy (org isolation, row-level security) · RBAC + SSO/SCIM · full audit trail · self-host + data residency · connector **plugin architecture** (community-extensible — the open-source force multiplier) · feature flags · i18n · horizontal-scaling workers · graceful connector degradation (one source down ≠ system down) · admin console for connection health + rate-limit dashboards.

---

## 11. Licensing & open-source strategy
- **Core: AGPL-3.0.** Ensures anyone who runs a modified, hosted version contributes their changes back — the right shield to keep the project open as it grows.
- **Connector SDK + client libs: Apache-2.0/MIT**, so integrators and firms can build freely against it.
- Public roadmap, `CONTRIBUTING.md`, connector-authoring guide, "good first connector" issues, and a connector template repo. **Adoption strategy = make writing a connector a weekend project.**
- Optional future sustainability without going closed: paid **managed hosting** and **priority-support** tiers (open-core-adjacent), never gating the core.

---

## 12. CI/CD, testing, observability
- CI: `ruff` + `mypy` + `pytest` (backend), `vitest` + Playwright (clients), Trivy + Syft on every PR.
- Test connectors against **recorded fixtures** (VCR-style) + provider sandboxes (Procore sandbox, Graph/Gmail test tenants) — never live prod data in CI.
- Contract tests for the `Connector` interface so community plugins can self-verify.
- Release: signed multi-platform Tauri bundles (win/mac/ios/android) + backend Docker images + Helm chart, versioned together.

---

## 13. Phased roadmap — drive Claude Code one phase at a time

Each phase = a self-contained Claude Code work order with acceptance criteria. Don't start a phase until the prior one's criteria pass.

### Phase 0 — Skeleton (foundation)
**Build:** monorepo (§4), docker-compose (Postgres+pgvector, Redis, API, worker), FastAPI app factory, config, DB + Alembic baseline, health endpoint, CI (ruff/mypy/pytest), `CLAUDE.md`, `SECURITY.md`, AGPL license.
**Accept:** `docker compose up` boots API + DB + worker; `/health` green; CI passes on a trivial test.

### Phase 1 — Ingestion spine + first connector + fallback
**Build:** `Connector` ABC + registry; **Outlook (MS Graph)** connector (OAuth2 + delta poll + webhook); the **universal File-Drop/IMAP/Forward-To** fallback; `Signal` model + normalizer; encrypted token vault; idempotent webhook handler; ARQ poll worker.
**Accept:** connect a test O365 mailbox → emails land as normalized Signals; forward an email to the drop address → Signal created; tokens encrypted at rest; re-ingesting the same event creates no duplicate.

### Phase 2 — The engine (make it useful)
**Build:** clustering/dedupe (thread_key + pgvector); AI extraction layer (Claude default, JSON/tool-use, citations) with Ollama fallback; explainable scoring rubric (urgency×impact×confidence, notice-deadline weighting); Item + versioned Score; hotlist builder + `GET /projects/{id}/hotlist`.
**Accept:** a mailbox of real-ish emails produces a ranked hotlist; each item shows why + factor breakdown + source citation; tweaking a weight reorders predictably.

### Phase 3 — Exports + more connectors
**Build:** Excel + PDF export from `HotlistSnapshot`; **Gmail** + **Procore** (sandbox) + **Calendar** connectors.
**Accept:** `?format=xlsx` and `?format=pdf` produce matching, styled hotlists; Procore RFI created in sandbox appears in the hotlist within one poll/webhook cycle.

### Phase 4 — Desktop client + background presence
**Build:** Tauri desktop app (Win/Mac), tray/menubar, live hotlist via WebSocket, OS background service, desktop notifications; **optional Rust local-agent (privacy mode)** for serverless local polling.
**Accept:** app runs in tray, updates the hotlist live, notifies on new 🔴 items, survives reboot as a service; privacy mode runs with no backend server.

### Phase 5 — Mobile viewer + hardening
**Build:** Tauri iOS/Android viewer; APNs/FCM push for 🔴 items; RBAC + audit log + admin health console; the learning loop (feedback → weights); SBOM + signed releases.
**Accept:** phone shows the hotlist and receives a push when a critical item appears; dismissing an item nudges future ranking; signed release artifacts verify.

### Phase 6 — Enterprise + community
**Build:** multi-tenancy + SSO/SCIM, Helm chart, connector SDK docs + template + 2 community-contributed connectors, Sage/ACC (Tier 2), Argus file-drop path.
**Accept:** two orgs isolated on one instance; an outside dev ships a connector using only the SDK docs.

---

## 14. `CLAUDE.md` starter (drop into repo root)

```markdown
# Osprey — Agent Operating Guide

## What this is
Free, open-source, self-hostable background agent that ingests construction/RE
data sources and produces a prioritized, exportable hotlist. Read SPEC.md first.

## Architecture (do not drift)
- Backend brain: Python 3.12 + FastAPI + Postgres/pgvector + Redis + ARQ.
- Clients: Tauri 2.0 (Rust core + React/TS) — one codebase for win/mac/ios/android.
- Mobile is a VIEWER + push. The always-on monitor is the backend or the desktop
  Rust local-agent. Never try to run continuous background work on iOS/Android.
- Every connector implements the Connector ABC. New source = new plugin, never a
  core edit.

## Golden rules
- Least-privilege, read-only OAuth scopes. Never store source-account passwords.
- Encrypt tokens at rest (Vault/KMS on server; OS keychain in local mode).
- All webhook processing idempotent (dedupe on external_id). All pollers use
  rate-limit + exponential backoff.
- Scoring must stay EXPLAINABLE: every hotlist item shows its factor breakdown
  and cites source text. No black-box ranking until there's feedback data.
- Weight contractual NOTICE deadlines highest — missing one can waive a claim.
- Excel and PDF exports derive from the SAME HotlistSnapshot.

## Workflow
Work phase-by-phase per SPEC.md §13. Meet a phase's acceptance criteria (with
tests) before starting the next. Run ruff + mypy + pytest before every commit.
Connectors are tested against recorded fixtures and provider sandboxes — never
live production data.

## Commands
- Boot: `docker compose up`
- Test: `pytest` / `ruff check` / `mypy osprey`
- Migrate: `alembic upgrade head`
```

---

## 15. Decisions to lock before Phase 1
1. **Name** — ✓ **locked: Osprey** (GitHub org `ospreyhq`, scoped packages).
2. **License** — AGPL-3.0 core (recommended) vs Apache-2.0 (max adoption, less protection)?
3. **Deployment priority** — lead with self-host/privacy mode, or a hosted demo first? (Recommend self-host first; it's your whole differentiation.)
4. **AI provider default** — Claude API out of the box, Ollama-local as the privacy option (recommended), or local-first by default?
5. **First vertical** — GC project team vs owner's rep vs developer? Tune the scoring rubric's default weights to that buyer for the demo.

---

*Built to be handed to Claude Code. Start at Phase 0, keep the golden rules in §14, and let each phase's acceptance criteria gate the next.*
