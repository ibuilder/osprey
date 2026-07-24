# Contributing to Osprey

Thanks for helping build the foreman that never sleeps. Osprey is AGPL-3.0 core with an
Apache-2.0/MIT connector SDK — see [`LICENSE`](LICENSE).

## Ground rules (from `CLAUDE.md`)

These are architectural, not stylistic. Changes that violate them will be asked to change:

- **Least-privilege, read-only OAuth scopes.** Never store a source-account password.
- **Encrypt tokens at rest.** Provider tokens never reach the client or any AI layer.
- **Ingestion is idempotent** — dedupe on `external_id`; pollers use rate-limit + backoff.
- **Scoring stays explainable** — every hotlist item shows its factor breakdown and cites
  source text. No black-box ranking until there is feedback data.
- **Contractual NOTICE deadlines are weighted highest.**
- **Excel and PDF exports derive from the same `HotlistSnapshot`.**
- **A new source is a new connector plugin**, never a core edit.

## Getting set up

```bash
cd backend
python -m venv .venv && . .venv/Scripts/activate    # .venv/bin/activate on *nix
pip install -c constraints.txt -e ".[dev]"
pytest -q
```

The whole suite runs **offline and deterministically** — no API keys, no database, no
network. If a change requires network to test, it belongs behind a mock (see
`tests/test_integration_connectors.py` for the `respx` pattern).

Optional but recommended:

```bash
pip install pre-commit && pre-commit install
```

## Before you open a PR

```bash
cd backend
ruff check osprey tests          # lint
ruff format osprey tests         # format
mypy osprey                      # types
pytest -q                        # tests
```

For desktop changes:

```bash
cd clients/desktop
npm ci && npx tsc --noEmit && npx vite build
```

CI runs all of the above. Keep PRs focused and reviewable — prefer several small PRs
over one large one.

## Tests

- New behavior needs a test. Bug fixes need a regression test.
- Connectors are tested against **recorded fixtures**, never live production data.
- Coverage is reported in CI; don't regress it meaningfully without saying why.

## Adding a connector

Read [`connectors-sdk/README.md`](connectors-sdk/README.md). Implement the `Connector`
ABC, register it, keep `normalize` pure, and test it against a fixture. You should not
need to modify anything in `engine/` or `api/`.

## Commit style

Short imperative subject, prefixed by area where useful (`engine:`, `desktop:`, `ci:`,
`docs:`). Explain *why* in the body when it isn't obvious.

## Security

Do not open a public issue for a vulnerability — see [`SECURITY.md`](SECURITY.md) for
the disclosure process.
