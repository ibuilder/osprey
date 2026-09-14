"""Argus exports: leases become option-notice deadlines and expirations.

An Argus tenancy export is the only practical way into Argus Enterprise, and the
rows that matter most are the ones whose deadlines are easiest to miss: the notice
to exercise a renewal or termination option, often months before the lease ends.
"""

from __future__ import annotations

from datetime import datetime

from osprey.ai.base import ExtractionInput
from osprey.ai.deterministic import DeterministicProvider
from osprey.connectors.argus import ArgusConnector, minus_months, parse_argus_csv
from osprey.models import Category

# A tenancy schedule with a notice *period*, as many Argus templates export it.
TENANCY = (
    "Property Name,Suite,Tenant Name,Lease Start,Lease Expiration,Option Type,"
    "Notice Period (Months),Annual Base Rent,RSF\n"
    'Harbor Point,210,Acme Engineering,2021-01-01,2027-12-31,Renewal,9,"$412,500",15000\n'
    'Harbor Point,305,Blue Fern Cafe,03/01/2022,02/28/2026,,,"$96,000",2400\n'
    "Harbor Point,400,VACANT,,,,,,5200\n"
    'Harbor Point,500,Nova Legal,2020-06-01,2026-05-31,Termination,,"$250,000",8000\n'
)

# A different template: explicit notice dates, other header spellings.
EXPIRY_REPORT = (
    "Tenant,Unit,Expiry Date,Option,Notice Deadline,Rent,Asset\n"
    "Kestrel Labs,12B,31-Dec-2028,Extension,30-Jun-2028,180000,Westgate\n"
)


def _by_title(events, prefix):
    return [e for e in events if e.title.startswith(prefix)]


def test_option_notice_is_computed_back_from_the_lease_end():
    result = parse_argus_csv(TENANCY)
    [notice] = _by_title(result.events, "Renewal option notice deadline")

    assert notice.due_at == datetime(2027, 3, 31)  # 31 Dec 2027 less 9 months
    assert "Acme Engineering (Harbor Point, Suite 210)" in notice.title
    assert notice.amount == 412500.0
    assert "9 months before the 2027-12-31 lease end" in notice.body


def test_every_lease_gets_its_expiration_and_shares_a_thread_with_its_option():
    events = parse_argus_csv(TENANCY).events
    acme = [e for e in events if "Acme" in e.title]

    assert {e.title.split(":")[0] for e in acme} == {
        "Renewal option notice deadline",
        "Lease expiration",
    }
    assert len({e.thread_key for e in acme}) == 1  # one hotlist item per lease
    [cafe] = _by_title(events, "Lease expiration: Blue Fern Cafe")
    assert cafe.due_at == datetime(2026, 2, 28)
    assert cafe.amount == 96000.0


def test_vacancies_are_skipped_and_counted():
    result = parse_argus_csv(TENANCY)

    assert not any("VACANT" in e.title for e in result.events)
    assert result.skipped_rows == 1


def test_an_option_with_no_notice_date_is_flagged_not_invented():
    result = parse_argus_csv(TENANCY)

    assert not _by_title(result.events, "Termination option")
    [nova] = _by_title(result.events, "Lease expiration: Nova Legal")
    assert "check the lease for the deadline" in nova.body
    assert result.unresolved_options == 1


def test_explicit_notice_dates_and_other_header_spellings():
    events = parse_argus_csv(EXPIRY_REPORT).events
    [notice] = _by_title(events, "Extension option notice deadline")

    assert notice.due_at == datetime(2028, 6, 30)
    assert "Westgate, Suite 12B" in notice.title
    assert notice.amount == 180000.0


def test_ids_are_stable_and_a_renewed_lease_is_a_new_event():
    first = [e.external_id for e in parse_argus_csv(TENANCY).events]
    again = [e.external_id for e in parse_argus_csv(TENANCY).events]
    renewed = [
        e.external_id for e in parse_argus_csv(TENANCY.replace("2027-12-31", "2032-12-31")).events
    ]

    assert first == again  # re-forwarding the same export ingests nothing twice
    assert "argus:harbor-point:210:acme-engineering:expiry:2027-12-31" in first
    assert "argus:harbor-point:210:acme-engineering:expiry:2032-12-31" in renewed


def test_an_unrecognized_file_yields_nothing_and_says_why():
    result = parse_argus_csv("Name,Amount\nWidget,10\n")

    assert result.events == []
    assert "tenant" in result.missing
    assert result.unmapped_columns == ["Name", "Amount"]


def test_notice_periods_in_days_are_honoured():
    export = "Tenant,Lease End,Option Type,Notice Period (Days)\nAcme,2027-12-31,Renewal,180\n"
    [notice] = _by_title(parse_argus_csv(export).events, "Renewal option")

    assert notice.due_at == datetime(2027, 7, 4)  # 180 days before 31 Dec


def test_month_arithmetic_clamps_to_the_end_of_the_month():
    assert minus_months(datetime(2026, 8, 31), 6) == datetime(2026, 2, 28)
    assert minus_months(datetime(2028, 8, 31), 6) == datetime(2028, 2, 29)
    assert minus_months(datetime(2027, 1, 15), 13) == datetime(2025, 12, 15)


async def test_a_lease_option_is_scored_as_a_contractual_notice():
    """The point of the whole path: the option deadline must rank as a notice."""
    [notice] = _by_title(parse_argus_csv(TENANCY).events, "Renewal option")
    extraction = await DeterministicProvider().extract(
        ExtractionInput(
            item_title=notice.title,
            signals=[
                {
                    "id": "s1",
                    "title": notice.title,
                    "body": notice.body,
                    "due_at": notice.due_at.date().isoformat(),
                    "amount": notice.amount,
                }
            ],
        )
    )

    assert extraction.notice_deadline is True
    assert extraction.category == Category.contractual_notice
    assert extraction.deadline == "2027-03-31"
    assert extraction.dollar_exposure == 412500.0


async def test_the_forward_to_path_parses_an_export():
    connector = ArgusConnector()
    events = [e async for e in connector.handle_webhook({"kind": "csv", "raw": TENANCY})]

    assert len(events) == 4  # Acme notice + expiry, cafe expiry, Nova expiry
