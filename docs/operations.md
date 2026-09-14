# Running Osprey in production

The controls in [SECURITY.md](../SECURITY.md) describe what the software does.
This describes what *you* have to do. Everything here is a procedure someone has
to be able to execute at 3am, so each section says what to run, what "it worked"
looks like, and what to check when it does not.

---

## 1. Before the first deploy

Osprey **refuses to start** in production with insecure configuration. This is
deliberate: a deploy carrying the default signing key is not degraded, it is
unprotected, and failing at rollout is far cheaper than discovering it later. The
boot aborts on any of:

| Setting | Requirement |
|---|---|
| `OSPREY_SECRET_KEY` | not the default, at least 32 characters |
| `OSPREY_ENCRYPTION_KEY` | not the default |
| `OSPREY_WEBHOOK_HMAC_SECRET` | not the default |
| `OSPREY_DATABASE_URL` | Postgres, not SQLite |
| `OSPREY_DEBUG` | off |
| `OSPREY_CORS_ALLOW_ORIGINS` | non-empty, no `*` |
| `OSPREY_PASSWORD_HASH_ITERATIONS` | at least 390,000 |

Generate the three secrets independently — never reuse one value for two of them:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Advisories that log a warning but do **not** block a boot: RLS disabled, rate
limiting disabled, no `OSPREY_PUBLIC_BASE_URL`, no retention window, unauthenticated
`/metrics`. Read the startup log after every deploy — that is where they appear.

### Tenant isolation

Row-level security is off by default because it only works if you connect as an
ordinary role. Turn it on properly:

```bash
psql -c "CREATE ROLE osprey_app LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS;"
alembic upgrade head          # applies 0002 (policies) and 0003
```

then set `OSPREY_RLS_ENABLED=true` and point `OSPREY_DATABASE_URL` at `osprey_app`.
Confirm it actually took effect — this is the one check worth doing by hand,
because a superuser connection makes RLS silently inert:

```bash
curl -s -H "Authorization: Bearer $TOKEN" https://osprey.example.com/admin/security/tenant-isolation
```

`{"enforced": true}` is the only acceptable answer. `false` with RLS enabled means
the app is connected as a role that bypasses policies.

---

## 2. Backup and restore

**What must be backed up:** the Postgres database, and `OSPREY_ENCRYPTION_KEY`.

A database backup without that key is useless for connector tokens — they are
sealed with AES-256-GCM and nothing else can open them. Store the key in your
secret manager, not alongside the dumps, and make sure it is covered by its own
backup and rotation policy. Redis holds only queue state and rate-limit counters;
losing it costs one poll cycle, not data.

### Taking a backup

```bash
pg_dump --format=custom --no-owner --no-privileges "$DATABASE_URL" > osprey-$(date +%F).dump
```

Run it as the schema owner, not `osprey_app`. **Row-level security applies to
`pg_dump` too**: dumping as the application role, with no `osprey.current_org`
set, silently produces an empty dump of every tenant table. This is the single
most likely way to end up holding a backup that restores nothing, so verify the
row counts rather than the exit status:

```bash
pg_restore --list osprey-$(date +%F).dump | grep -c "TABLE DATA"
```

### Testing a restore

An untested backup is not a backup. Quarterly, at minimum:

```bash
createdb osprey_restore_test
pg_restore --dbname=osprey_restore_test --no-owner osprey-YYYY-MM-DD.dump
OSPREY_DATABASE_URL=postgresql+asyncpg://.../osprey_restore_test \
  python -c "
import asyncio
from osprey.db import session_scope
from osprey.security.audit import verify_chain_detail
from sqlalchemy import select
from osprey.models import Org

async def main():
    async with session_scope() as s:
        orgs = (await s.execute(select(Org))).scalars().all()
        print(f'{len(orgs)} orgs restored')
        for org in orgs:
            print(org.name, await verify_chain_detail(s, org.id))
asyncio.run(main())
"
```

Every org's audit chain must come back `valid: true`. If `anchored_at_genesis`
is `false`, the chain was truncated by a retention purge — expected if you have
`OSPREY_RETENTION_AUDIT_DAYS` set, and worth investigating if you do not.

### Recovery targets

Set these deliberately rather than inheriting them:

- **RPO** — governed by your Postgres backup interval and WAL archiving. Osprey
  re-polls on a five-minute cycle, so signals lost inside the last poll window
  are usually re-ingested (webhook deliveries are not; those are gone).
- **RTO** — restore time plus `alembic upgrade head`. There is no warm standby in
  the chart; add one at the database layer if your target is minutes.

---

## 3. Key rotation

### `OSPREY_SECRET_KEY` (JWT signing)

Rotating it invalidates every access token immediately. Refresh tokens are opaque
and stored hashed, so they survive — clients recover on their next `/auth/refresh`
without anybody signing in again. Rotate during low traffic and expect a burst of
401s for up to `OSPREY_ACCESS_TOKEN_TTL_MINUTES`.

### `OSPREY_ENCRYPTION_KEY` (token vault) — needs a migration

**Do not just change this value.** Every stored connector token is sealed under
the old key; changing it without re-sealing breaks every connection at once, and
the plaintext is unrecoverable. Use the envelope's rotation support:

```python
import asyncio, os
from sqlalchemy import select
from osprey.db import session_scope
from osprey.models import Connection
from osprey.security.crypto import rotate

OLD, NEW = os.environ["OSPREY_OLD_KEY"], os.environ["OSPREY_NEW_KEY"]

async def main():
    async with session_scope() as session:
        for conn in (await session.execute(select(Connection))).scalars().all():
            if conn.encrypted_tokens:
                conn.encrypted_tokens = rotate(
                    conn.encrypted_tokens, old_key=OLD, new_key=NEW
                )
                session.add(conn)

asyncio.run(main())
```

Run it with the application **stopped**, against a fresh backup, then start with
`OSPREY_ENCRYPTION_KEY` set to the new value. Verify before declaring victory:

```bash
curl -s -H "Authorization: Bearer $TOKEN" https://osprey.example.com/admin/connections/health
```

Every connection should report `active`. Any that report `error` did not re-seal;
restore the backup rather than trying to repair them individually.

### `OSPREY_WEBHOOK_HMAC_SECRET`

Providers sign callbacks with the secret they were registered with, so rotating
this invalidates in-flight subscriptions. Rotate, then let the hourly
`renew_subscriptions` job re-register — or force it:

```bash
python -c "
import asyncio
from osprey.db import session_scope
from osprey.config import settings
from osprey.workers.tasks import renew_subscriptions

async def main():
    async with session_scope() as s:
        print(await renew_subscriptions(s, notify_base=settings.public_base_url))
asyncio.run(main())
"
```

Expect webhook deliveries to fail signature verification in the gap. Polling
covers it; nothing is lost, it just arrives on the next cycle instead of instantly.

### SCIM and metrics tokens

Both are revocable without a restart. Mint the replacement, update the consumer
(your IdP connector, or Prometheus), then revoke the old one:

```bash
curl -X DELETE -H "Authorization: Bearer $TOKEN" \
  https://osprey.example.com/orgs/current/scim-tokens/$ID
```

---

## 4. Data retention and erasure

Retention is **off by default** (`0` = keep forever). Nothing is deleted until an
operator chooses a window, which is the right default for a product whose whole
job is not losing track of a deadline.

Set a deployment-wide window with `OSPREY_RETENTION_SIGNAL_DAYS` /
`OSPREY_RETENTION_ITEM_DAYS`, or per tenant via `PUT /orgs/current/retention`.
Always look before you cut:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  https://osprey.example.com/orgs/current/retention/preview
```

Two things the purge deliberately will not do:

- **Open items are never deleted, at any age.** An unanswered notice deadline is
  precisely what must not disappear quietly. Only `done`/`dismissed`/`snoozed`
  items age out.
- **Audit history is not purged** unless `OSPREY_RETENTION_AUDIT_DAYS` is set, and
  then only as a contiguous oldest-first prefix, so the surviving hash chain still
  verifies.

The worker runs the purge nightly at 03:17. `POST /orgs/current/retention/run`
forces it.

### Right to delete

`POST /orgs/current/delete` with `{"confirm_org_name": "<exact name>"}` erases a
tenant: projects, signals, items, scores, actions, snapshots, connections,
devices, scripts, invites, sessions, and audit log. Users are removed only if
this was their last membership. **There is no undo and no soft-delete tier.**
Take a backup first; the API will not do it for you.

By default the erasure runs inline, in one transaction, and needs no worker. For a
very large tenant that transaction can hold locks on the busiest tables for a long
time, so set `OSPREY_ERASURE_INLINE_MAX_ROWS` (signals + items + scores + audit
records) to queue anything bigger:

- the call returns **202** instead of 200, and the tenant is locked at once: every
  authenticated request gets 423, and polling, subscription renewal, scheduled
  scripts and webhooks all ignore it, so nothing new arrives;
- the worker removes up to `OSPREY_ERASURE_BATCH_ROWS` (default 5000) rows per
  tenant per minute, each batch its own transaction, then erases the remainder;
- `GET /orgs/current/deletion-status` keeps answering the owner while it drains.

Queued erasure needs the worker running. Without one a queued tenant stays locked
and undeleted, so leave the setting at `0` on a deployment with no worker.

### Subject access

`GET /orgs/current/export` returns the tenant's full contents as JSON. Connector
tokens are excluded by design — they are third-party OAuth credentials, not
subject data, and exporting them would defeat the vault. Their existence is
reported; their contents are not.

---

## 5. Monitoring

`/metrics` serves Prometheus text format. Set `OSPREY_METRICS_TOKEN` or keep the
port off the internet — the series leak tenant counts, error rates, and the route
table.

Alerts worth having, in rough order of how much they will save you:

| Condition | Why it matters |
|---|---|
| `osprey_http_requests_total{status=~"5.."}` rising | the API is failing, not just slow |
| `/ready` failing on a replica | database unreachable from that pod |
| `osprey_auth_failures_total` spiking | credential stuffing in progress |
| `osprey_rate_limit_rejections_total` sustained | either an attack or a limit set too low |
| `osprey_connections{status="error"}` > 0 | a source stopped ingesting — silent data loss |
| worker liveness probe failing | Redis is gone; nothing is being polled |
| `histogram_quantile(0.95, osprey_http_request_duration_seconds)` | latency regression |

The connector one is the least obvious and the most valuable: a broken connection
does not make the API unhealthy. It just quietly stops producing hotlist items,
and nobody notices until someone asks why a deadline was missed.

### Probes

Three endpoints, three questions — do not mix them up:

- `/live` — is the process running? No dependencies. Use for liveness.
- `/ready` — should traffic route here? Returns **503** when the database is
  unreachable. Use for readiness.
- `/health` — human summary. Always 200 if the process is up.

Pointing liveness at `/ready` means a database blip restarts every pod in the
fleet on top of the outage.

---

## 6. Scaling

- **API** — stateless; scale horizontally. Enable `autoscaling.enabled` for an
  HPA on CPU. With more than one replica, set `rateLimitBackend: redis`; the
  in-process fallback gives each replica its own counters, so N replicas means
  N times the intended limit.
- **Worker** — deployed `Recreate`, single replica, on purpose. ARQ cron jobs are
  not leader-elected, so a second replica fires every scheduled poll twice.
  Scale by sharding connections across separate deployments, not by raising
  `replicaCount.worker`.
- **Postgres** — the hot path is signal insert and item ranking. Sizing follows
  signal volume, which follows mailbox traffic, not user count.

---

## 7. Incident response

1. **Suspected credential compromise.** `POST /auth/logout-all` as the affected
   user, or bump `token_version` directly for several. Both cut access tokens
   already in flight — the check is on every request.
2. **Suspected tenant data exposure.** `GET /admin/audit/verify` first: if the
   chain is broken, the database was written to outside the application. Preserve
   it before doing anything else.
3. **A connector leaking or misbehaving.** Revoke it at the provider *and*
   `DELETE` the connection. Revoking only at the provider leaves a sealed token
   in the vault.
4. **Rate limiting a specific attacker.** There is no per-IP block list; use your
   ingress or WAF. Osprey's limiter is a fairness control, not a defence against
   a determined adversary.

Report vulnerabilities per [SECURITY.md](../SECURITY.md).
