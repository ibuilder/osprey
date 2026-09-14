"""Argus Enterprise, through its exports (SPEC §6, Tier 3).

Argus Enterprise (Altus) has no generally available API: access is gated, and most
shops work from Excel/CSV exports. So this ships the path SPEC asks for first.
Export a tenancy schedule or lease-expiry report from Argus as CSV and send it to
an ``argus`` connection (``POST /connections/{id}/forward`` with ``kind: "csv"``).
Each lease becomes dated, rankable signals:

* an **option notice deadline** for each renewal, termination, extension or
  expansion option. That is a contractual notice, which Osprey weights highest,
  because missing it can forfeit the option. When the export gives a notice
  period instead of a date, the deadline is computed back from the lease end;
* the **lease expiration** itself.

Both carry the same thread key, so a lease is one hotlist item whose deadline is
its earliest date. Annual rent is the dollar exposure.

Argus report layouts differ by template and version, so columns are matched by the
aliases in :data:`COLUMN_ALIASES`, ignoring case, spacing and punctuation. Rows with
no tenant (vacancies) or no usable date are skipped and counted, never guessed at.
A direct API connector can follow if access is ever granted; this module does not
pretend to be one.
"""

from __future__ import annotations

import calendar
import csv
import io
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ...models import SourceKind
from ...normalize import clean_text
from ..base import Connection as ConnView
from ..base import Connector, Health, NormalizedSignal, RawEvent, registry

log = logging.getLogger(__name__)

SOURCE_TYPE = "argus"


def _key(header: str) -> str:
    """Compare headers on letters and digits only: "Notice Period (Months)" -> "notice period months"."""
    return re.sub(r"[^a-z0-9]+", " ", header.lower()).strip()


#: Canonical field -> header spellings seen across Argus report templates.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "property": ("property", "property name", "asset", "asset name", "building"),
    "tenant": ("tenant", "tenant name", "lessee", "occupant"),
    "suite": ("suite", "suite id", "unit", "unit id", "space"),
    "lease_start": (
        "lease start",
        "lease start date",
        "start date",
        "commencement",
        "commencement date",
        "lease commencement",
    ),
    "lease_end": (
        "lease end",
        "lease end date",
        "lease expiration",
        "lease expiration date",
        "lease expiry",
        "expiration",
        "expiration date",
        "expiry date",
        "end date",
    ),
    "option_type": ("option type", "option", "option kind"),
    "notice_date": (
        "notice date",
        "option notice date",
        "notice deadline",
        "exercise by",
        "notice by",
    ),
    "notice_period": (
        "notice period months",
        "notice period",
        "notice months",
        "notice period mos",
        "notice period days",
        "notice days",
    ),
    "rent": (
        "annual rent",
        "annual base rent",
        "base rent",
        "current rent",
        "contract rent",
        "rent",
    ),
    "area": ("area", "leased area", "sf", "rsf", "nra", "square feet"),
}

_ALIAS_TO_FIELD = {
    _key(alias): name for name, aliases in COLUMN_ALIASES.items() for alias in aliases
}

_VACANT = frozenset({"vacant", "vacancy", "available", "unleased"})

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%d-%b-%Y",
    "%d-%b-%y",
    "%d %b %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
)


@dataclass
class ArgusParse:
    """The events from one export, plus what could not be used and why."""

    events: list[RawEvent] = field(default_factory=list)
    skipped_rows: int = 0
    unresolved_options: int = 0
    #: Required fields no column mapped to. Non-empty means this is not a usable export.
    missing: list[str] = field(default_factory=list)
    #: Headers that matched no alias; kept on each event's raw row, never dropped.
    unmapped_columns: list[str] = field(default_factory=list)


def _date(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return datetime(parsed.year, parsed.month, parsed.day)
    return None


def _amount(value: str) -> float | None:
    cleaned = value.replace("$", "").replace(",", "").strip()
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    if not cleaned:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return -number if negative else number


def _notice_period(value: str, header: str) -> timedelta | int | None:
    """A notice period as whole months (int) or days (timedelta).

    Argus exports state it in months unless the value or the column says days.
    """
    match = re.search(r"\d+", value)
    if not match:
        return None
    number = int(match.group())
    if "day" in value.lower() or "day" in header:
        return timedelta(days=number)
    return number


def minus_months(when: datetime, months: int) -> datetime:
    """``when`` moved back ``months`` calendar months, clamped to the month's last day.

    31 Aug minus 6 months is 28 Feb (or 29 in a leap year), not an error.
    """
    years, month_index = divmod(when.month - 1 - months, 12)
    year = when.year + years
    month = month_index + 1
    day = min(when.day, calendar.monthrange(year, month)[1])
    return when.replace(year=year, month=month, day=day)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "na"


def _money(value: float) -> str:
    return f"${value:,.0f}"


def parse_argus_csv(text: str) -> ArgusParse:
    """Turn an Argus tenancy / lease-expiry CSV export into events. Pure."""
    result = ArgusParse()
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    headers = reader.fieldnames or []

    column_for: dict[str, str] = {}
    for header in headers:
        name = _ALIAS_TO_FIELD.get(_key(header or ""))
        if name is None:
            result.unmapped_columns.append(header)
        elif name not in column_for:
            column_for[name] = header

    if "tenant" not in column_for:
        result.missing.append("tenant")
    if not ({"lease_end", "notice_date"} & column_for.keys()):
        result.missing.append("lease_end or notice_date")
    if result.missing:
        return result

    for row in reader:

        def get(name: str, row: dict = row) -> str:
            column = column_for.get(name)
            return (row.get(column) or "").strip() if column else ""

        tenant = get("tenant")
        if not tenant or tenant.lower() in _VACANT:
            result.skipped_rows += 1
            continue

        prop, suite = get("property"), get("suite")
        lease_end = _date(get("lease_end"))
        rent = _amount(get("rent"))
        option = get("option_type")
        notice_raw = get("notice_period")
        where = ", ".join(p for p in (prop, f"Suite {suite}" if suite else "") if p)
        label = f"{tenant} ({where})" if where else tenant
        identity = ":".join(_slug(p) for p in (prop, suite, tenant))
        thread_key = f"{SOURCE_TYPE}:{identity}"

        details = [
            line
            for line in (
                f"Property: {prop}" if prop else "",
                f"Suite: {suite}" if suite else "",
                f"Tenant: {tenant}",
                f"Lease start: {get('lease_start')}" if get("lease_start") else "",
                f"Lease end: {lease_end:%Y-%m-%d}" if lease_end else "",
                f"Annual rent: {_money(rent)}" if rent is not None else "",
                f"Area: {get('area')}" if get("area") else "",
                f"Option: {option}" if option else "",
            )
            if line
        ]
        raw = {"row": dict(row), "unmapped_columns": list(result.unmapped_columns)}
        produced = False

        # -- the option notice, if the lease has an option --------------------- #
        has_option = bool(option or get("notice_date") or notice_raw)
        notice = _date(get("notice_date"))
        computed_from = ""
        if notice is None and lease_end is not None and notice_raw:
            period = _notice_period(notice_raw, _key(column_for.get("notice_period", "")))
            if isinstance(period, int):
                notice = minus_months(lease_end, period)
                computed_from = f" ({period} months before the {lease_end:%Y-%m-%d} lease end)"
            elif isinstance(period, timedelta):
                notice = lease_end - period
                computed_from = f" ({period.days} days before the {lease_end:%Y-%m-%d} lease end)"

        if has_option and notice is not None:
            kind = option.strip().title() if option else "Lease"
            body = (
                f"Notice to exercise the {kind.lower()} option must be given by "
                f"{notice:%Y-%m-%d}{computed_from}. Missing the notice deadline can "
                "forfeit the option.\n" + "\n".join(details)
            )
            result.events.append(
                RawEvent(
                    external_id=(
                        f"{SOURCE_TYPE}:{identity}:option:{_slug(kind)}:{notice:%Y-%m-%d}"
                    ),
                    source_kind=SourceKind.task,
                    thread_key=thread_key,
                    title=f"{kind} option notice deadline: {label}",
                    body=clean_text(body, drop_quoted=False),
                    due_at=notice,
                    amount=rent,
                    raw=raw,
                )
            )
            produced = True
        elif has_option:
            result.unresolved_options += 1

        # -- the expiration ------------------------------------------------------ #
        if lease_end is not None:
            body_lines = [f"Lease expires {lease_end:%Y-%m-%d}."]
            if has_option and notice is None:
                body_lines.append(
                    "The lease has an option, but the export gives no notice date or "
                    "notice period to compute one from; check the lease for the deadline."
                )
            result.events.append(
                RawEvent(
                    external_id=f"{SOURCE_TYPE}:{identity}:expiry:{lease_end:%Y-%m-%d}",
                    source_kind=SourceKind.task,
                    thread_key=thread_key,
                    title=f"Lease expiration: {label}",
                    body=clean_text("\n".join(body_lines + details), drop_quoted=False),
                    due_at=lease_end,
                    amount=rent,
                    raw=raw,
                )
            )
            produced = True

        if not produced:
            result.skipped_rows += 1

    return result


@registry.register
class ArgusConnector(Connector):
    source_type = SOURCE_TYPE
    scopes: list[str] = []
    # The forward-to path: exports are sent in, there is nothing to poll.
    supports_webhooks = True

    async def poll(self, conn: ConnView, since: datetime | None) -> AsyncIterator[RawEvent]:
        return
        yield  # pragma: no cover

    async def handle_webhook(self, payload: dict) -> AsyncIterator[RawEvent]:
        result = parse_argus_csv(str(payload.get("raw") or ""))
        if result.missing:
            log.warning(
                "argus export not recognized: no column for %s (headers matched no known "
                "Argus field: %s)",
                ", ".join(result.missing),
                ", ".join(result.unmapped_columns) or "none",
            )
        elif result.skipped_rows or result.unresolved_options:
            log.info(
                "argus export: %d event(s), %d row(s) skipped, %d option(s) without a "
                "determinable notice date",
                len(result.events),
                result.skipped_rows,
                result.unresolved_options,
            )
        for event in result.events:
            yield event

    async def normalize(self, raw: RawEvent) -> NormalizedSignal:
        return NormalizedSignal(**raw.model_dump())

    async def healthcheck(self, conn: ConnView) -> Health:
        return Health(ok=True, detail="accepts Argus CSV exports through forward-to")
