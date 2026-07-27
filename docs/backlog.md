# Known deferrals

Deliberate decisions, not forgotten work. Each records why it is deferred and what
should trigger revisiting it, so nobody re-derives the analysis later.

## httpx 1.x → httpx2 (`StarletteDeprecationWarning`)

**Status:** blocked upstream.

Starlette warns that using `httpx` with `starlette.testclient` is deprecated and wants
`httpx2`. We cannot migrate yet: `respx` — the library the connector integration tests
use to mock provider HTTP (`tests/test_integration_connectors.py`) — declares
`httpx>=0.25.0`, i.e. the 1.x line, and has no httpx2 support. Installing httpx2 would
leave those tests unable to intercept requests.

The warning is cosmetic; `TestClient` works. It is deliberately *not* silenced, so it
stays visible.

**Revisit when:** `respx` ships httpx2 support (Dependabot will raise the bump), or we
replace respx with a different HTTP mocking approach.

## Alembic `0001_baseline` uses `create_all`

**Status:** risk mitigated; rewrite not currently justified.

The baseline revision calls `SQLModel.metadata.create_all` rather than explicit
`op.create_table(...)` operations. The original concern was drift between models and
migrations going unnoticed.

That concern is now continuously gated in CI (`postgres` job):

- `alembic upgrade head` against real Postgres,
- a `downgrade base` → `upgrade head` round-trip,
- `alembic check`, which fails if the models and migrations have diverged.

Rewriting the baseline into explicit operations is a large mechanical change to the
one revision every deployment has already applied, in exchange for reviewability we
currently get from the drift check instead. Not worth the risk today.

**Revisit when:** we need a migration the autogenerate diff cannot express, or we want
to support a database where `create_all` and the migration path diverge.

## React 18 → 19 (and the Vite majors that follow it)

**Status:** deliberately deferred.

A piecemeal bump breaks peer resolution — `@types/react-dom@18` requires
`@types/react@^18` — so it must be done as one coordinated change with a manual pass
over the desktop UI. Dependabot is configured to ignore React majors
(`.github/dependabot.yml`) so it stops proposing the broken partial upgrade.

Vite is already on 8.x; that was taken early because it carried a security advisory.

**Revisit when:** someone can smoke-test the desktop app's screens after the upgrade.
The `frontend` and `desktop-rust` CI jobs will catch build/type breakage, but not
behavioural regressions in the UI.

## What is *not* covered by CI

Worth stating plainly, since the pipeline is otherwise thorough:

- **kind is not production.** `deploy-smoke` validates wiring, hook ordering,
  migrations, and that RLS enforces. It does not exercise ingress/TLS, storage
  classes, resource limits under load, or a managed database's specifics.
- **Live provider APIs.** Connector poll loops are tested against recorded/mocked
  HTTP, never a real Microsoft/Google/Procore tenant. See `docs/connecting.md` for the
  manual sandbox checklist.
- **Real push delivery.** APNs/FCM/Web Push senders are unit-tested; nothing verifies a
  notification actually lands on a device.
