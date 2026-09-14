"""The connector contract, as checks a connector author can run against their own code.

SPEC §12 asks for "contract tests for the Connector interface so community plugins
can self-verify". This module is that: the rules every connector must honour,
expressed as checks rather than prose, so a plugin author finds out in their own
test suite -- not in a production incident -- that a webhook handler is missing or
an ``external_id`` is not stable.

Use it from a pytest suite::

    from osprey.connectors.contract import assert_connector_contract

    async def test_my_connector_honours_the_contract():
        await assert_connector_contract(
            MyConnector,
            webhook_payloads=[load_fixture("webhook.json")],
        )

Every built-in connector is held to the same checks (tests/test_connector_contract.py),
so the rules are known to be satisfiable, not aspirational.

What it deliberately does not check: anything that needs a live provider
(``poll`` against a real API, ``healthcheck`` against a real token). Test ``poll``
against recorded responses with respx; see the SDK template.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from .base import Connector, NormalizedSignal, RawEvent, registry

#: Lowercase, starts with a letter, URL-safe: it is also the ``/webhooks/{source_type}`` path.
SOURCE_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,39}$")

#: How a webhook may be authenticated. See ``Connector.webhook_auth``.
WEBHOOK_AUTH_MODES = frozenset({"hmac", "client_state", "signature"})

# Osprey reads; it never writes back to a source. A scope that grants writes is a
# standing liability if the token leaks, for no benefit to anything Osprey does.
_WRITE_SCOPE = re.compile(r"write|manage|delete|admin|modify|full[_.]?access", re.IGNORECASE)

# Provider scopes that grant full access without saying so in their name.
_FULL_ACCESS_SCOPES = frozenset(
    {
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/drive",
    }
)


@dataclass
class ContractReport:
    """Everything that was checked, and every rule that was broken."""

    connector: str
    checked: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def fail(self, rule: str) -> None:
        self.violations.append(rule)


class ContractViolation(AssertionError):
    """Raised by :func:`assert_connector_contract` with every broken rule listed."""


def _overrides(cls: type[Connector], name: str) -> bool:
    return getattr(cls, name) is not getattr(Connector, name)


def _check_identity(cls: type[Connector], report: ContractReport, require_registered: bool) -> None:
    source_type = getattr(cls, "source_type", None)
    report.checked.append("source_type")
    if not isinstance(source_type, str) or not SOURCE_TYPE_PATTERN.match(source_type):
        report.fail(
            f"source_type {source_type!r} must match {SOURCE_TYPE_PATTERN.pattern}: it is "
            "the webhook URL path and the registry key"
        )
        return
    if require_registered:
        report.checked.append("registered")
        try:
            registered = registry.get(source_type)
        except KeyError:
            report.fail(
                f"{source_type!r} is not registered: decorate the class with "
                "@registry.register (and, for a plugin, declare the entry point)"
            )
        else:
            if type(registered) is not cls:
                report.fail(
                    f"{source_type!r} is registered to {type(registered).__name__}, "
                    f"not {cls.__name__}"
                )


def _check_scopes(cls: type[Connector], report: ContractReport) -> None:
    report.checked.append("scopes are read-only")
    scopes = cls.scopes
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        report.fail("scopes must be a list of strings")
        return
    for scope in scopes:
        if scope in _FULL_ACCESS_SCOPES or _WRITE_SCOPE.search(scope):
            report.fail(
                f"scope {scope!r} grants more than read access; request the narrowest "
                "read-only scope the provider offers, or make it an optional scope an "
                "admin opts into, with the reason"
            )


def _check_optional_scopes(cls: type[Connector], report: ContractReport) -> None:
    """Anything beyond read access must be opt-in, and must say why."""
    report.checked.append("optional scopes are opt-in and explained")
    optional = cls.optional_scopes
    if not isinstance(optional, dict):
        report.fail("optional_scopes must be a dict of scope -> reason")
        return
    for scope, reason in optional.items():
        if not isinstance(scope, str) or not scope:
            report.fail("optional_scopes keys must be non-empty scope strings")
        elif not isinstance(reason, str) or len(reason.strip()) < 20:
            report.fail(
                f"optional scope {scope!r} needs a plain-language reason (shown to the "
                "admin before they grant it)"
            )
        if isinstance(cls.scopes, list) and scope in cls.scopes:
            report.fail(
                f"{scope!r} is in both scopes and optional_scopes; an optional scope must "
                "not be requested unless opted into"
            )


def _check_methods(cls: type[Connector], report: ContractReport) -> None:
    report.checked.append("method shapes")
    if not inspect.isasyncgenfunction(cls.poll):
        report.fail("poll() must be an async generator (use `yield`, even if it yields nothing)")
    if not inspect.iscoroutinefunction(cls.normalize):
        report.fail("normalize() must be `async def`")
    if cls.supports_webhooks and not _overrides(cls, "handle_webhook"):
        report.fail(
            "supports_webhooks is True but handle_webhook() is not implemented: every "
            "webhook for this source would fail with NotImplementedError"
        )
    if _overrides(cls, "handle_webhook") and not inspect.isasyncgenfunction(cls.handle_webhook):
        report.fail("handle_webhook() must be an async generator")


def _check_webhook_auth(cls: type[Connector], report: ContractReport) -> None:
    report.checked.append("webhook authentication")
    if cls.webhook_auth not in WEBHOOK_AUTH_MODES:
        report.fail(
            f"webhook_auth {cls.webhook_auth!r} must be one of {sorted(WEBHOOK_AUTH_MODES)}; "
            "an unknown mode would fall through to HMAC and reject genuine callbacks"
        )
    elif cls.webhook_auth == "client_state" and not _overrides(cls, "webhook_client_state"):
        report.fail(
            "webhook_auth is 'client_state' but webhook_client_state() is not implemented, "
            "so every callback would fail authentication"
        )
    elif cls.webhook_auth == "signature" and not _overrides(cls, "verify_webhook_signature"):
        report.fail(
            "webhook_auth is 'signature' but verify_webhook_signature() is not implemented, "
            "so every callback would fail authentication"
        )


def _check_oauth(connector: Connector, report: ContractReport) -> None:
    report.checked.append("oauth endpoints")
    spec = connector.oauth_spec()
    if spec is None:
        return
    for name in ("authorize_endpoint", "token_endpoint"):
        url = str(getattr(spec, name, ""))
        if not url.startswith("https://"):
            report.fail(f"oauth {name} {url!r} must be https")
    if not getattr(spec, "use_pkce", False):
        report.fail(
            "oauth must use PKCE: the desktop app is a public client and cannot keep a "
            "client secret"
        )


async def _collect(connector: Connector, payload: dict) -> list[RawEvent]:
    return [event async for event in connector.handle_webhook(payload)]


async def _check_webhooks(
    connector: Connector, payloads: list[dict], report: ContractReport
) -> list[RawEvent]:
    cls = type(connector)
    if not cls.supports_webhooks:
        return []
    report.checked.append("webhook payloads parse deterministically")
    if not payloads:
        report.fail(
            "supports_webhooks is True but no sample webhook payload was supplied; pass a "
            "recorded payload so parsing is actually exercised"
        )
        return []

    events: list[RawEvent] = []
    for index, payload in enumerate(payloads):
        try:
            first = await _collect(connector, payload)
            second = await _collect(connector, payload)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            report.fail(f"webhook payload #{index} raised {type(exc).__name__}: {exc}")
            continue
        for event in first:
            if not isinstance(event, RawEvent):
                report.fail(
                    f"webhook payload #{index} yielded {type(event).__name__}, not RawEvent"
                )
        # Osprey dedupes on (connection, external_id). A redelivered webhook -- and
        # providers do redeliver -- must map to the same ids, or it ingests twice.
        if [e.external_id for e in first] != [e.external_id for e in second]:
            report.fail(
                f"webhook payload #{index} produced different external_ids on a second "
                "parse; ids must derive from the payload, not from time or randomness"
            )
        events.extend(e for e in first if isinstance(e, RawEvent))

    if not events and not report.violations:
        report.fail("no sample webhook payload produced any events; supply one that should")
    return events


async def _check_events(
    connector: Connector, events: list[RawEvent], report: ContractReport
) -> None:
    report.checked.append("normalize preserves identity and is stable")
    for event in events:
        if not isinstance(event.external_id, str) or not event.external_id.strip():
            report.fail(f"event {event.title!r} has an empty external_id; it cannot be deduped")
            continue
        try:
            first = await connector.normalize(event)
            second = await connector.normalize(event)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            report.fail(f"normalize() raised {type(exc).__name__} for {event.external_id!r}: {exc}")
            continue
        if not isinstance(first, NormalizedSignal):
            report.fail(f"normalize() returned {type(first).__name__}, not NormalizedSignal")
            continue
        if first.external_id != event.external_id:
            report.fail(
                f"normalize() changed external_id {event.external_id!r} -> "
                f"{first.external_id!r}; dedupe keys on the raw event's id"
            )
        if first.model_dump() != second.model_dump():
            report.fail(f"normalize() is not deterministic for {event.external_id!r}")


async def check_connector(
    connector_cls: type[Connector],
    *,
    webhook_payloads: Iterable[dict] = (),
    raw_events: Iterable[RawEvent] = (),
    require_registered: bool = True,
) -> ContractReport:
    """Run every contract check and return a report. Never raises for a violation."""
    report = ContractReport(connector=getattr(connector_cls, "__name__", repr(connector_cls)))
    if not (inspect.isclass(connector_cls) and issubclass(connector_cls, Connector)):
        report.fail("connector must be a subclass of osprey.connectors.base.Connector")
        return report
    try:
        connector = connector_cls()
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        report.fail(f"connector must be constructible with no arguments ({exc})")
        return report

    _check_identity(connector_cls, report, require_registered)
    _check_scopes(connector_cls, report)
    _check_optional_scopes(connector_cls, report)
    _check_methods(connector_cls, report)
    _check_webhook_auth(connector_cls, report)
    _check_oauth(connector, report)
    events = await _check_webhooks(connector, list(webhook_payloads), report)
    await _check_events(connector, [*events, *raw_events], report)
    return report


async def assert_connector_contract(
    connector_cls: type[Connector],
    *,
    webhook_payloads: Iterable[dict] = (),
    raw_events: Iterable[RawEvent] = (),
    require_registered: bool = True,
) -> ContractReport:
    """Run the contract and raise :class:`ContractViolation` listing every broken rule."""
    report = await check_connector(
        connector_cls,
        webhook_payloads=webhook_payloads,
        raw_events=raw_events,
        require_registered=require_registered,
    )
    if not report.ok:
        rules = "\n".join(f"  - {rule}" for rule in report.violations)
        raise ContractViolation(f"{report.connector} breaks the connector contract:\n{rules}")
    return report
