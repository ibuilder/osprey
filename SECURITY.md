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
- App login via short-lived JWT access tokens plus opaque, hashed **refresh
  tokens**. Refresh is single-use and rotates; presenting an already-rotated token
  is treated as theft and revokes the entire session family (OAuth 2.1 BCP).
- **Revocation is immediate, not on expiry.** Every access token carries the user's
  `token_version`, checked on each request, so deactivating, demoting, removing, or
  signing a user out everywhere invalidates tokens already in flight.
- **OIDC SSO** (authorization code + PKCE): the ID token's signature is verified
  against the issuer's published JWKS by `kid`, and `iss`/`aud`/`exp`/`nonce` and
  `email_verified` are all checked. Only asymmetric algorithms are accepted -- an
  `HS256` ID token would verify against the client secret, making any holder of it
  an issuer.
- **SCIM 2.0** provisioning for user lifecycle, authenticated by per-org tokens
  with a `max_role` ceiling, so a leaked provisioning credential cannot mint owners.
- **RBAC** with roles owner/admin/pm/viewer. Nobody may grant a role above their
  own, and an org can never be left without an active owner.
- Credential endpoints are metered per IP *and* per account, in a per-minute and a
  per-hour window, with account lockout after repeated failures. Sign-in returns an
  identical response for an unknown account and a wrong password.
- Passwords: PBKDF2-HMAC-SHA256 at the OWASP 390,000-round floor (a production boot
  below it is refused), with a configurable length/composition policy and
  transparent rehash when parameters are raised.

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
- The live-hotlist **WebSocket** authorizes on the same boundary: it confirms the
  token is unrevoked *and* that the project belongs to the caller's org before
  accepting the socket, and fails closed if it cannot confirm either.

### Data protection
- **In transit:** TLS 1.3 at the edge; HSTS, sent only over requests that actually
  arrived on HTTPS. (Terminate at your reverse proxy / ingress.)
- **At rest:** connector tokens are sealed with **AES-256-GCM** envelope encryption
  (`osprey.security.crypto`). The master key comes from `OSPREY_ENCRYPTION_KEY`
  (KMS/Vault in server mode; OS keychain — DPAPI/Keychain/libsecret — in local mode).
- **Privacy mode:** fully local/self-hosted; local LLM via Ollama; data never
  transits a third party. First-class, documented deployment.

### Application hardening
- **Fail-fast configuration.** The API refuses to start with `OSPREY_ENV=prod` and
  any default secret, a SQLite database, debug on, or an empty/wildcard CORS origin
  list. A deploy that would be unprotected fails at rollout.
- Browser hardening headers on every response, errors included: CSP
  (`default-src 'none'` for the API), `X-Frame-Options: DENY`, `nosniff`,
  `Referrer-Policy: no-referrer`, a restrictive `Permissions-Policy`, and HSTS.
- Per-caller API rate limiting (Redis-backed across replicas), request body size
  caps, and a correlation id on every response and log line.
- Validation errors report the field and the rule, never the submitted value, so a
  rejected password does not land in a client log or error tracker.
- Idempotent, signature-verified webhooks (HMAC); per-connector rate-limit +
  exponential backoff; Pydantic input validation at every boundary.
- **OAuth redirect targets are validated, not just relayed.** Both the SSO and the
  connector flows accept a redirect URI from a native client (RFC 8252 loopback),
  and both refuse anything that is not the configured URL or a literal loopback
  address — `localhost` included, since it resolves through name resolution
  another process can influence. The value is sealed into the signed state at
  authorize time and the token exchange uses that copy, so the two cannot be
  decoupled by a later request.
- Append-only, tamper-evident **audit log** (hash-chained) for data access and
  config changes. Verification reports whether the chain is anchored at genesis, so
  a truncated history cannot pass as a complete one.
- Secrets never in code or logs; `.env` + Vault; secret scanning in CI. Structured
  logs are scrubbed of emails and bearer/token/password patterns before emission.

### Supply chain
- SBOM (Syft) per release; vuln scanning (Trivy + Dependabot).
- **Release integrity**: every release publishes `SHA256SUMS.txt` and signed build
  provenance (`actions/attest-build-provenance`, recorded in a public transparency
  log and checkable with `gh attestation verify`). Update payloads are signed with
  a minisign key whose public half is compiled into the app, so an installed copy
  discards any update it cannot verify. **Binaries are not yet code-signed with an
  organisation certificate** — SmartScreen and Gatekeeper still warn on first run;
  see [docs/code-signing.md](docs/code-signing.md).
- **The audit covers what ships, not just what is tested.** `pip-audit` runs
  against the extras the Docker image installs (`prod,ai,push,otel`) under both
  constraint files, so the dependencies that exist only in production are scanned
  too. Dependabot watches the Python, npm, Cargo, GitHub Actions *and* container
  base-image manifests.
- **Pinned dependencies, including the ones only production installs.**
  `constraints.txt` pins the set the test suite is green on; `constraints-prod.txt`
  pins the runtime extras (asyncpg, arq, alembic, AI SDKs, push, OTel) that the
  test suite never installs. The Docker image applies both, so the artefact you
  deploy is resolved from the same versions that were reviewed.
- Multi-stage image: the compiler toolchain stays in the build stage, so
  `build-essential` and its CVE surface never reach production. The runtime adds
  exactly one package (`curl`, for the healthcheck) and runs as an unprivileged
  user on a read-only root filesystem with all capabilities dropped.

### Operational
- **Data retention** per deployment and per tenant, with a preview endpoint that
  reports exactly what a purge would delete before it runs. Open items are never
  purged at any age -- an unanswered notice deadline is the one thing that must not
  disappear quietly.
- **Right to delete** (`POST /orgs/current/delete`) erases a tenant across every
  table; **subject-access export** (`GET /orgs/current/export`) returns its full
  contents as JSON, deliberately excluding the connector token vault.
- Prometheus metrics at `/metrics`; separate `/live`, `/ready`, and `/health`
  probes, where `/ready` returns 503 so a broken replica is actually drained.
- OpenTelemetry tracing (optional `otel` extra) with PII scrubbing at the log layer.
- Backup, tested restore, and key-rotation procedures are documented in
  [docs/operations.md](docs/operations.md) -- including the failure mode where
  `pg_dump` run as the application role silently produces an empty dump once RLS is
  enabled. Osprey ships the procedures; **running them is the operator's job.**
- Controls above map to **SOC 2 Type II**; formal audit is a roadmap item.

## Supported versions

Security fixes land on `main` and the latest tagged minor. Older tags are
best-effort.
