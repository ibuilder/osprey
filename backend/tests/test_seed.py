"""The demo seed produces a realistic, ranked hotlist + valid exports."""

from __future__ import annotations

from pathlib import Path

from osprey.seed import run_seed


async def test_seed_produces_ranked_hotlist_and_exports(tmp_path):
    result = await run_seed(out_dir=str(tmp_path))

    assert result["signals"] >= 8
    assert result["items"] >= 6
    assert result["total_exposure"] >= 180000

    payload = result["payload"]
    # The contractual notice of delay should top the list as Act today.
    top = payload["items"][0]
    assert "NOTICE OF DELAY" in top["what"].upper()
    assert top["bucket"] == "act_today"

    # Multiple sources represented.
    source_types = {s["source_type"] for it in payload["items"] for s in it["sources"]}
    assert {"outlook", "procore"} <= source_types

    # Exports written and valid.
    xlsx = Path(result["xlsx"])
    pdf = Path(result["pdf"])
    assert xlsx.read_bytes()[:2] == b"PK"
    assert pdf.read_bytes()[:5] == b"%PDF-"
