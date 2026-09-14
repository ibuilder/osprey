# Osprey Connector SDK

A connector turns a source (an API, a mailbox, a file export) into a stream of
`RawEvent`s that Osprey normalizes, clusters, scores and ranks. Writing one is meant
to be a weekend project, and it never touches Osprey's core: you ship a Python
package, and installing it next to Osprey is the whole integration.

> The connector interface is Apache-2.0/MIT-friendly to build against, so you can
> license your plugin however you like, including closed integrations. The core
> engine is AGPL-3.0.

## Quick start

```bash
cp -r connectors-sdk/template my-connector
cd my-connector
# Rename osprey_connector_example/ and update pyproject.toml ([project] name and
# the entry point), then:
pip install -e ".[dev]"      # into the same environment as osprey-core
pytest
```

The template is a complete, tested connector for a made-up issue tracker. It shows
every part: the pure mapping function, a paginated poll against a rate-limited
client, a webhook handler, recorded fixtures, mocked HTTP, and the contract test.
Replace the tracker specifics with your source and keep the tests green.

Osprey's own CI installs the template exactly this way and runs its tests, so the
template cannot drift out of step with the core.

## Registering: the entry point

```toml
[project.entry-points."osprey.connectors"]
mysource = "osprey_connector_mysource"
```

At startup Osprey imports every module in the `osprey.connectors` group. Your
module's `@registry.register` decorator adds the connector. It then appears in
`GET /connections/sources`, and webhooks for it arrive at
`POST /webhooks/{source_type}`.

Two rules the loader enforces:

- **A plugin cannot replace a built-in.** Registering a `source_type` that is
  already taken is refused, and logged. Otherwise any installed package could
  claim `outlook` and be handed that source's decrypted tokens.
- **A broken plugin does not stop Osprey.** An import error is logged and that
  plugin is skipped; every other source keeps working.

A plugin runs inside Osprey with the same privileges as Osprey itself, including
decrypted tokens for its own source. Install plugins you trust, as you would any
dependency.

## The interface

Subclass `osprey.connectors.base.Connector`:

```python
from collections.abc import AsyncIterator
from datetime import datetime

from osprey.connectors.base import Connection, Connector, Health, NormalizedSignal, RawEvent, registry
from osprey.connectors.http import connector_client
from osprey.models import SourceKind


def normalize_item(item: dict) -> RawEvent:          # pure: test it with fixtures
    return RawEvent(
        external_id=f"mysource:{item['id']}",        # stable dedupe key
        source_kind=SourceKind.rfi,
        title=item["subject"],
        body=item.get("text", ""),
        due_at=...,                                   # deadlines and amounts drive the score
        amount=...,
    )


@registry.register
class MySourceConnector(Connector):
    source_type = "mysource"         # unique slug; also the webhook URL path
    scopes = ["items:read"]           # read-only
    supports_webhooks = True          # only if handle_webhook() is implemented

    async def poll(self, conn: Connection, since: datetime | None) -> AsyncIterator[RawEvent]:
        async with connector_client(self.source_type, base_url=API) as client:
            resp = await client.get("/items", params={"since": since.isoformat() if since else None})
            resp.raise_for_status()
            for item in resp.json()["items"]:
                yield normalize_item(item)

    async def handle_webhook(self, payload: dict) -> AsyncIterator[RawEvent]:
        yield normalize_item(payload["item"])

    async def normalize(self, raw: RawEvent) -> NormalizedSignal:
        return NormalizedSignal(**raw.model_dump())

    async def healthcheck(self, conn: Connection) -> Health:
        return Health(ok=bool(conn.tokens.get("api_token")))
```

Optional hooks, when your source needs them:

| Override | When |
|---|---|
| `oauth_spec()`, `client_credentials()` | OAuth2 sources. The desktop app runs the browser flow; PKCE is required. |
| `webhook_auth = "client_state"` + `webhook_client_state()` | Providers that echo a shared secret instead of signing (Microsoft Graph). The default, `"hmac"`, verifies Osprey's `X-Osprey-Signature`. |
| `supports_subscriptions`, `ensure_subscription()`, `lifecycle_events()` | Providers whose webhook subscriptions expire and must be renewed. |

## The contract

These are the rules every connector honours. They are checked, not just stated:
`osprey.connectors.contract.assert_connector_contract` runs them against your
connector, and every built-in connector passes the same checks.

1. **Stable identity.** The same real-world item always produces the same
   `external_id`: derived from the provider's id, never from time, randomness, or
   Python's `hash()` (which changes on every restart). Osprey dedupes on
   `(connection, external_id)`, and providers redeliver webhooks.
2. **`normalize()` keeps that id** and is deterministic.
3. **Read-only, least privilege.** Request the narrowest read scope. Never store a
   source-account password.
4. **Webhooks you declare, you handle.** `supports_webhooks = True` requires a
   working `handle_webhook()` and at least one recorded sample payload.
5. **Authenticated callbacks.** `webhook_auth` is `"hmac"` or `"client_state"`,
   and the latter needs `webhook_client_state()`.
6. **OAuth over HTTPS, with PKCE.**
7. **Rate limits are honoured.** Make HTTP calls through `connector_client(...)`.
   It paces requests per source and retries `429`/`5xx`, obeying `Retry-After`.
   Raise on other errors (`resp.raise_for_status()`): a `401` must surface, never
   look like "no new data".
8. **No core edits.** A new source is only ever a new package.

```python
from osprey.connectors.contract import assert_connector_contract

async def test_honours_the_contract():
    await assert_connector_contract(
        MySourceConnector,
        webhook_payloads=[load("webhook.json")],
        raw_events=[normalize_item(load("item.json"))],
    )
```

A failure lists every broken rule at once, with the reason it matters.

## Testing

Test against **recorded fixtures**, never live production data:

- Put the mapping in a pure function (`normalize_item`) and test it directly with a
  saved API response. This is where most of your tests should live.
- Test `poll()` with [respx](https://lundberg.github.io/respx/) mocking the
  provider's HTTP API: pagination, the auth header, and that a `401` raises.
- Run the contract test.

The template's `tests/test_example.py` does all three.

## Reference connectors

- `backend/osprey/connectors/filedrop/`: the universal forward-to / CSV fallback.
  Pure parse functions; a good first read.
- `backend/osprey/connectors/procore/`: OAuth2, paginated REST, webhooks, and
  per-module 403/404 handling.
- `backend/osprey/connectors/outlook/`: OAuth2 with delta queries, Graph
  subscriptions, and `client_state` webhook authentication.
