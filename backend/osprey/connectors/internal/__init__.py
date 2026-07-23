"""Internal source connectors: ``pyscript`` and ``ai``.

These have no external API — they are the ingestion path for signals produced
*inside* Osprey by user-authored Python background scripts and by AI sift jobs.
Both reuse the normal ingest/cluster/score/hotlist pipeline (with dedupe), so a
script or AI finding becomes a first-class, explainable hotlist item.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

from ..base import Connection as ConnView
from ..base import Connector, Health, NormalizedSignal, RawEvent, registry


class _PassthroughConnector(Connector):
    supports_webhooks = False

    async def poll(self, conn: ConnView, since: datetime | None) -> AsyncIterator[RawEvent]:
        return
        yield  # pragma: no cover

    async def normalize(self, raw: RawEvent) -> NormalizedSignal:
        return NormalizedSignal(**raw.model_dump())

    async def healthcheck(self, conn: ConnView) -> Health:
        return Health(ok=True, detail=f"{self.source_type} passthrough source")


@registry.register
class PyScriptConnector(_PassthroughConnector):
    source_type = "pyscript"


@registry.register
class AIConnectorSource(_PassthroughConnector):
    source_type = "ai"
