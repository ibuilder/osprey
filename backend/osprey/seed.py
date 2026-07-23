"""One-command demo:  python -m osprey.seed

Loads a realistic multi-source construction project, runs the full pipeline
(ingest -> cluster -> extract -> score -> hotlist), and writes an Excel + PDF
hotlist to ./demo. Runs fully offline (deterministic AI + SQLite), so anyone can
see Osprey produce real output in one command.
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from pathlib import Path

from .connectors.base import RawEvent
from .models import SourceKind, utcnow


# A realistic slice of a project's week, spread across the sources a GC actually uses.
def _events() -> dict[str, list[RawEvent]]:
    now = utcnow()

    def days(n: int):
        return now + timedelta(days=n)

    return {
        "outlook": [
            RawEvent(
                external_id="mail-notice-1",
                source_kind=SourceKind.email,
                thread_key="notice-delay-towerb",
                title="NOTICE OF DELAY — differing site conditions at Tower B",
                body=(
                    "Pursuant to Section 8.3, this is formal notice of delay due to differing "
                    "site conditions discovered at the east foundation. A written response is "
                    "required within 7 days or the claim may be deemed waived. Estimated "
                    "schedule and cost exposure $180,000."
                ),
                participants=["pm@gc.com", "owner@developer.com"],
                url="https://outlook.office.com/mail/id/mail-notice-1",
                occurred_at=now - timedelta(days=1),
            ),
            RawEvent(
                external_id="mail-payapp-7",
                source_kind=SourceKind.email,
                title="Pay Application 07 — retention release requested",
                body="Attached is pay application 07 for $54,000 including a retention release. Please review against the schedule of values.",
                participants=["ap@subcontractor.com", "pm@gc.com"],
                amount=54000,
                occurred_at=now - timedelta(days=2),
            ),
            RawEvent(
                external_id="mail-sched-1",
                source_kind=SourceKind.email,
                title="Two-week look-ahead — steel delivery slipping",
                body="Heads up: steel delivery is slipping ~5 days which may affect the critical path for level 3. Look-ahead attached.",
                participants=["super@gc.com", "pm@gc.com"],
                occurred_at=now - timedelta(days=1),
            ),
        ],
        "procore": [
            RawEvent(
                external_id="procore:rfis:5001",
                source_kind=SourceKind.rfi,
                thread_key="procore:rfis:RFI-0500",
                title="RFI-0500 — curtain wall anchor spacing at grid C-4",
                body="Please confirm anchor spacing for the curtain wall at grid C-4. This blocks fabrication release.",
                due_at=days(4),
                url="https://app.procore.com/rfis/5001",
                occurred_at=now - timedelta(days=3),
            ),
            RawEvent(
                external_id="procore:change_orders:88",
                source_kind=SourceKind.change_order,
                title="PCO-088 — slab thickening at loading dock",
                body="Owner-requested slab thickening at the loading dock. Extra work; pricing attached.",
                amount=45000,
                due_at=days(9),
                url="https://app.procore.com/change_orders/88",
                occurred_at=now - timedelta(days=2),
            ),
            RawEvent(
                external_id="procore:observations:12",
                source_kind=SourceKind.observation,
                title="Safety observation — missing fall protection at level 3 leading edge",
                body="Observed workers at the level 3 leading edge without fall protection. Immediate correction required.",
                due_at=days(1),
                url="https://app.procore.com/observations/12",
                occurred_at=now,
            ),
        ],
        "gmail": [
            RawEvent(
                external_id="gmail-submittal-1",
                source_kind=SourceKind.submittal,
                title="Submittal 03 30 00 — concrete mix design for review",
                body="Please review the concrete mix design submittal for the podium slab. Resubmittal if strength assumptions change.",
                participants=["architect@ae.com", "pm@gc.com"],
                due_at=days(6),
                occurred_at=now - timedelta(days=1),
            ),
        ],
        "gcal": [
            RawEvent(
                external_id="gcal:owner-mtg-1",
                source_kind=SourceKind.event,
                title="Owner-Architect-Contractor meeting — Tower B",
                body="Weekly OAC coordination meeting. Agenda: progress review and open items.",
                due_at=days(2),
                participants=["pm@gc.com", "owner@developer.com", "architect@ae.com"],
                occurred_at=now,
            ),
        ],
    }


async def run_seed(*, db_path: str = "./osprey-demo.db", out_dir: str = "./demo") -> dict:
    os.environ.setdefault("OSPREY_ENCRYPTION_KEY", "demo-encryption-key")
    os.environ.setdefault("OSPREY_SECRET_KEY", "demo-secret-key-at-least-32-bytes-long-000")
    os.environ["OSPREY_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"

    # Import after env is set so settings pick up the demo DB.
    from .connectors.service import get_connector
    from .db import create_all, get_sessionmaker
    from .engine.hotlist import refresh_project
    from .engine.ingest import ingest_events
    from .exports import hotlist_to_pdf, hotlist_to_xlsx
    from .models import Connection, ConnectionStatus, Org, Project

    Path(db_path).unlink(missing_ok=True)
    await create_all()

    maker = get_sessionmaker()
    async with maker() as session:
        org = Org(name="Summit Builders (demo)")
        session.add(org)
        await session.flush()
        project = Project(org_id=org.id, name="Tower B")
        session.add(project)
        await session.flush()

        total = 0
        for source_type, events in _events().items():
            connector = get_connector(source_type)
            conn = Connection(
                org_id=org.id, project_id=project.id, source_type=source_type,
                account_ref=f"{source_type}@demo", status=ConnectionStatus.active,
            )
            session.add(conn)
            await session.flush()
            created = await ingest_events(session, connector, conn, events)
            total += len(created)

        snapshot = await refresh_project(session, project.id, generated_by="seed")
        await session.commit()
        payload = snapshot.payload

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    xlsx_path = out / "hotlist.xlsx"
    pdf_path = out / "hotlist.pdf"
    xlsx_path.write_bytes(hotlist_to_xlsx(payload, project_name="Tower B"))
    pdf_path.write_bytes(hotlist_to_pdf(payload, project_name="Tower B"))

    return {
        "signals": total,
        "items": payload["item_count"],
        "total_exposure": payload["total_exposure"],
        "xlsx": str(xlsx_path),
        "pdf": str(pdf_path),
        "payload": payload,
    }


def main() -> None:
    import contextlib
    import sys

    # Windows consoles default to cp1252; make emoji/em-dash output non-fatal.
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    result = asyncio.run(run_seed())
    payload = result["payload"]
    label = {"act_today": "[ACT TODAY]", "this_week": "[THIS WEEK]", "watch": "[WATCH]", "done": "[DONE]"}
    print("\n  Osprey demo - Tower B")
    print("  " + "-" * 60)
    print(f"  Ingested {result['signals']} signals across 4 sources into {result['items']} items.")
    print(f"  Total exposure: ${result['total_exposure']:,.0f}\n")
    for i, item in enumerate(payload["items"][:8], start=1):
        bucket = label.get(item.get("bucket", ""), "")
        exp = f" - ${item['dollar_exposure']:,.0f}" if item.get("dollar_exposure") else ""
        print(f"  {i}. {bucket} [{item['score']:.0f}] {item['what']}{exp}")
    print(f"\n  Exports: {result['xlsx']} - {result['pdf']}\n")


if __name__ == "__main__":
    main()
