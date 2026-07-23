"""Connector framework — one interface every source implements (SPEC §6).

A connector turns an external source into a stream of ``RawEvent`` (via polling
and/or webhooks) and normalizes each into a ``NormalizedSignal``. New sources are
plugins registered on the :data:`registry`; the core never changes to add one.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime

from pydantic import BaseModel, Field

from ..models import SourceKind, utcnow


class RawEvent(BaseModel):
    """Provider-native payload emitted by poll()/handle_webhook()."""

    external_id: str
    source_kind: SourceKind = SourceKind.general
    thread_key: str | None = None
    title: str = ""
    body: str = ""
    participants: list[str] = Field(default_factory=list)
    due_at: datetime | None = None
    amount: float | None = None
    url: str | None = None
    raw: dict = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=utcnow)


class NormalizedSignal(BaseModel):
    """Cleaned, source-agnostic signal. Ingest attaches ids + embedding + persists."""

    external_id: str
    source_kind: SourceKind
    thread_key: str | None = None
    title: str = ""
    body: str = ""
    participants: list[str] = Field(default_factory=list)
    due_at: datetime | None = None
    amount: float | None = None
    url: str | None = None
    raw: dict = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=utcnow)


class Health(BaseModel):
    ok: bool
    detail: str = ""


class Connection(BaseModel):
    """Lightweight connection view passed to connectors (no ORM coupling)."""

    id: str
    source_type: str
    account_ref: str = ""
    cursor: str | None = None
    tokens: dict = Field(default_factory=dict)   # decrypted at call time by ingest
    scopes: list[str] = Field(default_factory=list)


class Connector(ABC):
    source_type: str
    scopes: list[str] = []
    supports_webhooks: bool = False

    # -- OAuth (desktop-app driven) ------------------------------------------ #
    def oauth_spec(self):
        """Return an ``OAuthSpec`` for OAuth2 sources, or ``None`` (e.g. filedrop)."""
        return None

    def client_credentials(self) -> tuple[str, str]:
        """(client_id, client_secret) for this source, from settings. Override as needed."""
        return "", ""

    async def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None) -> dict:
        """Exchange an authorization code for sealed-able tokens (generic OAuth2)."""
        import httpx

        spec = self.oauth_spec()
        if spec is None:
            raise NotImplementedError(f"{self.source_type} is not an OAuth source")
        client_id, client_secret = self.client_credentials()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
        }
        if client_secret:
            data["client_secret"] = client_secret
        if code_verifier:
            data["code_verifier"] = code_verifier
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(spec.token_endpoint, data=data)
            resp.raise_for_status()
            return resp.json()

    async def account_ref_from_tokens(self, tokens: dict) -> str:
        """Best-effort display label for the connected account (override per source)."""
        return ""

    @abstractmethod
    async def poll(self, conn: Connection, since: datetime | None) -> AsyncIterator[RawEvent]:
        """Yield events since the connection cursor / ``since`` (incremental)."""
        raise NotImplementedError
        yield  # pragma: no cover - marks this an async generator

    async def handle_webhook(self, payload: dict) -> AsyncIterator[RawEvent]:
        """Yield events from a verified webhook payload. Idempotent downstream."""
        raise NotImplementedError
        yield  # pragma: no cover

    @abstractmethod
    async def normalize(self, raw: RawEvent) -> NormalizedSignal:
        """Map a RawEvent to a NormalizedSignal."""
        raise NotImplementedError

    async def healthcheck(self, conn: Connection) -> Health:
        return Health(ok=True)

    # -- Webhook subscription lifecycle -------------------------------------- #
    supports_subscriptions: bool = False

    async def ensure_subscription(self, conn: Connection, notify_url: str) -> str | None:
        """Create or renew a provider webhook subscription; return its id/expiry.

        Subscriptions (e.g. MS Graph) expire and must be renewed before lapse. The
        default is a no-op for sources without subscriptions (filedrop, procore-poll).
        """
        return None


class _Registry:
    def __init__(self) -> None:
        self._by_type: dict[str, type[Connector]] = {}

    def register(self, cls: type[Connector]) -> type[Connector]:
        self._by_type[cls.source_type] = cls
        return cls

    def get(self, source_type: str) -> Connector:
        try:
            return self._by_type[source_type]()
        except KeyError as exc:
            raise KeyError(f"no connector registered for source_type={source_type!r}") from exc

    def types(self) -> list[str]:
        return sorted(self._by_type)


registry = _Registry()
