# Osprey Connector SDK

Writing a connector is meant to be a **weekend project**. A connector turns any
source (an API, a mailbox, a file export) into a stream of `RawEvent`s that Osprey
normalizes, clusters, scores, and ranks. You never touch the core — you register a
plugin.

> The SDK and client libraries are licensed **Apache-2.0/MIT** so you can build
> against Osprey freely, even in closed integrations. (The core engine is AGPL-3.0.)

## The interface

Every connector subclasses `osprey.connectors.base.Connector`:

```python
from collections.abc import AsyncIterator
from datetime import datetime

from osprey.connectors.base import (
    Connector, Connection, RawEvent, NormalizedSignal, Health, registry,
)
from osprey.models import SourceKind


@registry.register
class MySourceConnector(Connector):
    source_type = "mysource"          # unique key; also the /webhooks/{source_type} path
    scopes = ["read:items"]            # least-privilege, read-only
    supports_webhooks = True

    async def poll(self, conn: Connection, since: datetime | None) -> AsyncIterator[RawEvent]:
        # Incremental pull. Use conn.cursor for a delta token; yield RawEvents.
        async for item in _fetch(conn, since):
            yield RawEvent(
                external_id=item["id"],           # dedupe key within the source
                source_kind=SourceKind.rfi,
                thread_key=item.get("thread"),
                title=item["subject"],
                body=item["text"],
                due_at=item.get("due"),
                amount=item.get("amount"),
                url=item.get("link"),
            )

    async def handle_webhook(self, payload: dict) -> AsyncIterator[RawEvent]:
        for ev in payload.get("events", []):
            yield RawEvent(external_id=ev["id"], title=ev["subject"], body=ev["body"])

    async def normalize(self, raw: RawEvent) -> NormalizedSignal:
        return NormalizedSignal(**raw.model_dump())   # add source-specific cleaning here

    async def healthcheck(self, conn: Connection) -> Health:
        return Health(ok=True)
```

## Contract you must honor

1. **Idempotent** — the same real-world event must always produce the same
   `external_id`. Osprey dedupes on `(connection_id, external_id)`.
2. **Read-only, least-privilege** — request the narrowest scopes; never store a
   source-account password.
3. **Rate-limit + backoff** — wrap network calls with `tenacity` retry/backoff.
4. **No core edits** — a new source is only ever a new plugin.

## Testing your connector

Test against **recorded fixtures**, never live production data. `normalize`/parse
functions should be pure so they run offline:

```python
def test_my_normalize():
    ev = MySourceConnector().normalize_fixture(load("fixtures/item.json"))
    assert ev.external_id == "123"
```

See `backend/tests/test_connectors.py` for the pattern used by the built-in
`filedrop` and `outlook` connectors.

## Reference connectors

- `backend/osprey/connectors/filedrop/` — the universal Forward-To / IMAP / CSV
  fallback. Pure parse functions; a good first read.
- `backend/osprey/connectors/outlook/` — a real OAuth2 + delta + webhook connector
  with a pure `normalize_graph_message` you can copy.
