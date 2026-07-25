# Security Policy

Osprey handles email and platform data for construction/real-estate projects. We
treat that data as sensitive by default. This document is the honest version:
no "100% secure" or "bank-level" marketing absolutes — **enterprise-grade,
defense-in-depth, and self-hostable so data never has to leave your org.**

## Reporting a vulnerability

Email **security@ospreyhq.dev** (or open a private GitHub security advisory).
Please include reproduction steps and impact. We aim to acknowledge within 3
business days and to ship a fix or mitigation before public disclosure. We
practice coordinated disclosure and will credit reporters who wish it.

Do **not** open a public issue for a security report.

## Control set (what backs "enterprise-grade")

### Identity & access
- OAuth2 **only** for source accounts — Osprey never stores a user's source-account
  password. Requests least-privilege, read-only scopes (e.g. `Mail.Read`).
- App login via JWT (OIDC/SSO-ready); **RBAC** with roles owner/admin/pm/viewer.
- SCIM provisioning is on the enterprise roadmap.

### Multi-tenant isolation
- Every query is scoped by `org_id` in application code, and Postgres **row-level
  security** (migration `0002_rls`) enforces the same boundary in the database, so a
  query that forgets its filter still cannot cross tenants.
- **The app must connect as an ordinary database role.** Postgres superusers — and any
  role with `BYPASSRLS` — skip row-level security entirely, and `FORCE ROW LEVEL
  SECURITY` does *not* override that. Most Postgres images hand you a superuser by
  default, which would leave the policies applied but inert. `docker compose` therefore
  provisions a dedicated `osprey_app` role (`deploy/postgres/init/`), runs migrations as
  the schema owner, and serves the app as the ordinary role.
- Osprey checks this itself: it logs an error at startup if RLS is enabled while the
  connection could bypass it, and `GET /admin/security/tenant-isolation` reports whether
  isolation is **enforced**, not merely configured.

### Data protection
- **In transit:** TLS 1.3 at the edge; HSTS. (Terminate at your reverse proxy /
  ingress; the app assumes an HTTPS front.)
- **At rest:** connector tokens are sealed with **AES-256-GCM** envelope encryption
  (`osprey.security.crypto`). The master key comes from `OSPREY_ENCRYPTION_KEY`
  (KMS/Vault in server mode; OS keychain — DPAPI/Keychain/libsecret — in local mode).
- **Privacy mode:** fully local/self-hosted; local LLM via Ollama; data never
  transits a third party. First-class, documented deployment.

### Application hardening
- Idempotent, signature-verified webhooks (HMAC); per-connector rate-limit +
  exponential backoff; Pydantic input validation at every boundary.
- Append-only, tamper-evident **audit log** (hash-chained) for data access and
  config changes.
- Secrets never in code or logs; `.env` + Vault; secret scanning in CI.

### Supply chain
- SBOM (Syft) per release; vuln scanning (Trivy + Dependabot); signed releases
  (Sigstore/cosign); pinned dependencies; reproducible Docker builds.

### Operational
- Encrypted backups + tested restore/DR; data-retention + right-to-delete controls
  (GDPR/CCPA posture); OpenTelemetry tracing with PII scrubbing.
- Controls above map to **SOC 2 Type II**; formal audit is a roadmap item.

## Supported versions

Security fixes land on `main` and the latest tagged minor. Older tags are
best-effort.
