"""User Python background scripts that emit signals into the hotlist."""

from __future__ import annotations

GOOD_SCRIPT = '''
# A user script: emit a couple of signals Osprey will score and rank.
osprey.log(f"running for project {ctx['project_name']}")
osprey.emit_signal(
    "Building permit expires soon",
    "The Tower B building permit expires and must be renewed. Exposure $25,000.",
    due_at="2026-08-15",
    amount=25000,
    source_kind="general",
    external_id="permit-check-1",
)
osprey.emit_signal("Second finding", "A lower-priority note from the script.")
'''

BAD_SCRIPT = '''
osprey.emit_signal("before the boom", "this one still lands")
raise ValueError("intentional failure")
'''

SECRET_PROBE = '''
import os
osprey.log("SECRET=" + os.environ.get("OSPREY_ENCRYPTION_KEY", "<absent>"))
osprey.log("DB=" + os.environ.get("OSPREY_DATABASE_URL", "<absent>"))
'''


async def _project(client) -> str:
    return (await client.post("/projects", json={"name": "Tower B"})).json()["id"]


async def _create(client, project_id, source, **kw):
    payload = {"name": kw.get("name", "s"), "source_code": source, **kw}
    return await client.post(f"/projects/{project_id}/scripts", json=payload)


async def test_script_emits_signals_to_hotlist(auth_client):
    client, _ = auth_client
    project_id = await _project(client)

    created = await _create(client, project_id, GOOD_SCRIPT, name="permit-watch")
    assert created.status_code == 201, created.text
    script_id = created.json()["id"]

    run = await client.post(f"/scripts/{script_id}/run")
    assert run.status_code == 200, run.text
    result = run.json()
    assert result["status"] == "ok"
    assert result["emitted"] == 2
    assert result["created"] == 2
    assert any("running for project Tower B" in line for line in result["logs"])

    # The script's output is now a real, scored hotlist item.
    hot = (await client.get(f"/projects/{project_id}/hotlist")).json()
    assert any("permit expires" in i["what"].lower() for i in hot["items"])


async def test_script_error_is_captured_not_crashed(auth_client):
    client, _ = auth_client
    project_id = await _project(client)
    created = await _create(client, project_id, BAD_SCRIPT, name="boom")
    script_id = created.json()["id"]

    run = await client.post(f"/scripts/{script_id}/run")
    assert run.status_code == 200
    result = run.json()
    assert result["status"] == "error"
    assert "ValueError" in (result["error"] or "")
    # The signal emitted before the exception still landed (streamed, not buffered).
    assert result["emitted"] == 1


async def test_script_cannot_see_server_secrets(auth_client):
    client, _ = auth_client
    project_id = await _project(client)
    created = await _create(client, project_id, SECRET_PROBE, name="probe")
    script_id = created.json()["id"]

    result = (await client.post(f"/scripts/{script_id}/run")).json()
    joined = " ".join(result["logs"])
    assert "SECRET=<absent>" in joined       # encryption key scrubbed from the sandbox
    assert "DB=<absent>" in joined           # database URL scrubbed


async def test_script_toggle_and_list(auth_client):
    client, _ = auth_client
    project_id = await _project(client)
    script_id = (await _create(client, project_id, GOOD_SCRIPT, name="watch")).json()["id"]

    off = await client.post(f"/scripts/{script_id}/toggle", params={"enabled": "false"})
    assert off.status_code == 200
    assert off.json()["enabled"] is False

    listing = (await client.get(f"/projects/{project_id}/scripts")).json()
    assert len(listing) == 1
