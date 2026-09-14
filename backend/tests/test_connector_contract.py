"""The connector contract: every built-in honours it, and each rule catches its violation.

If a built-in connector cannot pass the contract, the contract is wrong or the
connector is -- either way plugin authors would be held to a rule the core itself
breaks. The second half proves every check fires, so a green contract run means
something.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from types import SimpleNamespace

import pytest

import osprey.connectors as connectors_pkg
from osprey.connectors.base import Connector, NormalizedSignal, RawEvent, registry
from osprey.connectors.contract import (
    ContractViolation,
    assert_connector_contract,
    check_connector,
)
from osprey.connectors.filedrop import FileDropConnector, parse_email, stable_id
from osprey.security.oauth import OAuthSpec

RAW_EMAIL = """\
From: Jane PM <jane@gc.com>
To: super@sub.com
Subject: RFI-0412 beam penetration at grid C-4
Date: Wed, 22 Jul 2026 09:15:00 -0500
Message-ID: <rfi-0412@gc.com>

Please clarify the beam penetration detail at grid C-4.
"""

GRAPH_MESSAGE = {
    "id": "AAMk-123",
    "subject": "Submittal 03 30 00 concrete mix",
    "conversationId": "conv-9",
    "from": {"emailAddress": {"address": "arch@ae.com"}},
    "toRecipients": [{"emailAddress": {"address": "pm@gc.com"}}],
    "ccRecipients": [],
    "body": {"contentType": "text", "content": "Please review the concrete mix submittal."},
    "receivedDateTime": "2026-07-22T14:00:00Z",
}

#: Recorded-shape webhook payloads for every built-in that accepts webhooks.
WEBHOOK_SAMPLES: dict[str, list[dict]] = {
    "filedrop": [
        {"kind": "email", "raw": RAW_EMAIL},
        {
            "kind": "csv",
            "raw": "id,title,amount\nCO-1,Slab thickening,45000\n",
            "source_kind": "change_order",
        },
        {"kind": "note", "title": "Site walk", "body": "Crack at grid B-2"},
    ],
    "argus": [
        {
            "kind": "csv",
            "raw": (
                "Tenant,Suite,Lease Expiration,Option Type,Notice Period (Months),Annual Rent\n"
                "Acme,210,2027-12-31,Renewal,9,412500\n"
            ),
        }
    ],
    "outlook": [{"value": [{"clientState": "secret", "resourceData": GRAPH_MESSAGE}]}],
    "procore": [
        {
            "resource_name": "rfis",
            "resource": {"id": 42, "number": "12", "subject": "RFI 12", "due_date": "2026-08-01"},
        }
    ],
}


@pytest.mark.parametrize(
    "source_type",
    [
        "acc",
        "argus",
        "filedrop",
        "outlook",
        "procore",
        "gmail",
        "gcal",
        "sage-intacct",
        "pyscript",
        "ai",
    ],
)
async def test_builtin_connector_honours_the_contract(source_type):
    connector_cls = type(registry.get(source_type))
    await assert_connector_contract(
        connector_cls, webhook_payloads=WEBHOOK_SAMPLES.get(source_type, [])
    )


def test_every_registered_builtin_is_covered_above():
    # A new built-in must be added to the parametrize list, with a sample payload
    # if it takes webhooks; this fails until it is.
    covered = {
        "acc",
        "argus",
        "filedrop",
        "outlook",
        "procore",
        "gmail",
        "gcal",
        "sage-intacct",
        "pyscript",
        "ai",
    }
    builtin = {
        t
        for t in registry.types()
        if type(registry.get(t)).__module__.startswith("osprey.connectors.")
    }
    assert builtin <= covered, f"not held to the contract: {sorted(builtin - covered)}"


# --------------------------------------------------------------------------- #
# Every rule fires
# --------------------------------------------------------------------------- #
class _Good(Connector):
    source_type = "contract-good"
    supports_webhooks = True

    async def poll(self, conn, since):
        return
        yield  # pragma: no cover

    async def handle_webhook(self, payload):
        for item in payload["items"]:
            yield RawEvent(external_id=f"good:{item}", title=str(item))

    async def normalize(self, raw):
        return NormalizedSignal(**raw.model_dump())


SAMPLE = [{"items": [1, 2]}]


async def _violations(cls, **kwargs) -> list[str]:
    kwargs.setdefault("webhook_payloads", SAMPLE)
    kwargs.setdefault("require_registered", False)
    return (await check_connector(cls, **kwargs)).violations


async def test_a_well_behaved_connector_passes():
    assert await _violations(_Good) == []


async def test_source_type_must_be_a_url_safe_slug():
    class BadSlug(_Good):
        source_type = "My Source!"

    assert any("source_type" in v for v in await _violations(BadSlug))


async def test_scopes_must_be_read_only():
    class Greedy(_Good):
        scopes = ["Mail.Read", "Mail.ReadWrite", "https://mail.google.com/"]

    violations = await _violations(Greedy)
    assert any("Mail.ReadWrite" in v for v in violations)
    assert any("https://mail.google.com/" in v for v in violations)
    assert not any("'Mail.Read'" in v for v in violations)


async def test_declaring_webhooks_requires_a_handler():
    class NoHandler(Connector):
        source_type = "no-handler"
        supports_webhooks = True

        async def poll(self, conn, since):
            return
            yield  # pragma: no cover

        async def normalize(self, raw):
            return NormalizedSignal(**raw.model_dump())

    assert any("handle_webhook() is not implemented" in v for v in await _violations(NoHandler))


async def test_webhooks_must_be_exercised_with_a_sample():
    assert any(
        "no sample webhook payload" in v for v in await _violations(_Good, webhook_payloads=[])
    )


async def test_external_ids_must_be_stable_across_redelivery():
    class Random(_Good):
        async def handle_webhook(self, payload):
            yield RawEvent(external_id=str(uuid.uuid4()), title="x")

    assert any("different external_ids" in v for v in await _violations(Random))


async def test_a_raising_webhook_handler_is_reported_not_raised():
    class Crashes(_Good):
        async def handle_webhook(self, payload):
            raise KeyError("items")
            yield  # pragma: no cover

    assert any("raised KeyError" in v for v in await _violations(Crashes))


async def test_normalize_must_keep_the_external_id():
    class Renames(_Good):
        async def normalize(self, raw):
            return NormalizedSignal(**{**raw.model_dump(), "external_id": "other"})

    assert any("changed external_id" in v for v in await _violations(Renames))


async def test_normalize_must_be_deterministic():
    class Drifts(_Good):
        async def normalize(self, raw):
            return NormalizedSignal(**{**raw.model_dump(), "body": str(uuid.uuid4())})

    assert any("not deterministic" in v for v in await _violations(Drifts))


async def test_client_state_auth_needs_an_extractor():
    class Unverifiable(_Good):
        webhook_auth = "client_state"

    assert any("webhook_client_state" in v for v in await _violations(Unverifiable))


async def test_unknown_webhook_auth_mode_is_rejected():
    class Typo(_Good):
        webhook_auth = "hmac-sha256"

    assert any("webhook_auth" in v for v in await _violations(Typo))


async def test_oauth_must_be_https_with_pkce():
    class Insecure(_Good):
        def oauth_spec(self):
            return OAuthSpec(
                authorize_endpoint="http://idp.example.com/authorize",
                token_endpoint="https://idp.example.com/token",
                scopes=[],
                use_pkce=False,
            )

    violations = await _violations(Insecure)
    assert any("authorize_endpoint" in v and "https" in v for v in violations)
    assert any("PKCE" in v for v in violations)


async def test_signature_auth_needs_a_verifier():
    class Unverified(_Good):
        webhook_auth = "signature"

    assert any("verify_webhook_signature" in v for v in await _violations(Unverified))


async def test_a_write_scope_is_allowed_only_as_an_explained_opt_in():
    class OptIn(_Good):
        scopes = ["data:read"]
        optional_scopes = {"data:write": "Registers webhooks so changes arrive in seconds."}

    class Unexplained(_Good):
        optional_scopes = {"data:write": "webhooks"}

    class Both(_Good):
        scopes = ["data:read", "data:write"]
        optional_scopes = {"data:write": "Registers webhooks so changes arrive in seconds."}

    assert await _violations(OptIn) == []
    assert any("plain-language reason" in v for v in await _violations(Unexplained))
    violations = await _violations(Both)
    assert any("both scopes and optional_scopes" in v for v in violations)
    assert any("grants more than read access" in v for v in violations)


async def test_an_unregistered_connector_is_reported():
    assert any("not registered" in v for v in await _violations(_Good, require_registered=True))


async def test_poll_must_be_an_async_generator():
    class Eager(_Good):
        async def poll(self, conn, since):  # returns a list, never yields
            return []

    assert any("poll() must be an async generator" in v for v in await _violations(Eager))


async def test_non_connectors_are_rejected():
    assert (await check_connector(dict, require_registered=False)).violations  # type: ignore[arg-type]


async def test_assert_form_lists_every_broken_rule():
    class TwiceBad(_Good):
        source_type = "Nope"
        scopes = ["files.write"]

    with pytest.raises(ContractViolation) as exc:
        await assert_connector_contract(TwiceBad, webhook_payloads=SAMPLE, require_registered=False)
    assert "source_type" in str(exc.value)
    assert "files.write" in str(exc.value)


# --------------------------------------------------------------------------- #
# Registry and plugin loading
# --------------------------------------------------------------------------- #
def test_registry_refuses_a_second_connector_for_a_taken_source_type():
    class Impostor(_Good):
        source_type = "filedrop"

    with pytest.raises(ValueError, match="already registered"):
        registry.register(Impostor)
    assert type(registry.get("filedrop")) is FileDropConnector


def test_registering_the_same_class_twice_is_harmless():
    assert registry.register(FileDropConnector) is FileDropConnector


def test_plugins_load_and_a_broken_one_is_isolated(monkeypatch):
    class Plugin(_Good):
        source_type = "contract-plugin"

    class Impostor(_Good):
        source_type = "outlook"

    def boom():
        raise ImportError("missing dependency")

    fake_entries = [
        SimpleNamespace(name="good", value="good_pkg", load=lambda: registry.register(Plugin)),
        SimpleNamespace(name="broken", value="broken_pkg", load=boom),
        SimpleNamespace(name="impostor", value="bad_pkg", load=lambda: registry.register(Impostor)),
    ]
    monkeypatch.setattr(connectors_pkg, "entry_points", lambda group: fake_entries)
    try:
        assert connectors_pkg.load_plugins() == ["good"]
        assert type(registry.get("contract-plugin")) is Plugin
        assert type(registry.get("outlook")).__name__ == "OutlookConnector"
    finally:
        registry.unregister("contract-plugin")


# --------------------------------------------------------------------------- #
# File-drop fallback ids survive a restart
# --------------------------------------------------------------------------- #
def test_filedrop_fallback_id_is_the_same_in_another_process():
    """Regression: the fallback id used hash(), which is salted per interpreter.

    The same forwarded email, redelivered after a restart, got a new external_id
    and was ingested twice. A same-process test could never show it.
    """
    raw = "Subject: no id\n\nbody text"
    here = parse_email(raw).external_id
    code = (
        "from osprey.connectors.filedrop import parse_email;"
        f"print(parse_email({raw!r}).external_id)"
    )
    there = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        # A different hash seed from this process, which is what a restart gives you.
        env={**os.environ, "PYTHONHASHSEED": "12345"},
    ).stdout.strip()
    assert here == there == stable_id("filedrop", "no id", "body text")
