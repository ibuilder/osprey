"""Coverage for the Ollama provider, script scheduling, and push-sender fallbacks."""

from __future__ import annotations

import httpx
import respx

from osprey.ai.base import ExtractionInput, SiftInput
from osprey.ai.ollama import OllamaProvider
from osprey.models import Category, Org, Project, ScriptStatus, ScriptTask, utcnow
from osprey.scripts.service import run_due_scripts

# --------------------------------------------------------------------------- #
# Ollama provider (was 0% covered)
# --------------------------------------------------------------------------- #
_EXTRACTION_JSON = (
    '{"category": "rfi", "summary": "Anchor spacing RFI", "deadline": null, '
    '"dollar_exposure": 1200, "notice_deadline": false, "blocking": [], '
    '"recommended_action": "Respond to the RFI", "confidence": 0.7, "citations": []}'
)


@respx.mock
async def test_ollama_extract_parses_json_response():
    respx.post("http://localhost:11434/api/generate").mock(
        return_value=httpx.Response(200, json={"response": _EXTRACTION_JSON})
    )
    result = await OllamaProvider().extract(
        ExtractionInput(
            item_title="RFI-1", signals=[{"id": "s1", "title": "RFI-1", "body": "spacing?"}]
        )
    )
    assert result.category is Category.rfi
    assert result.dollar_exposure == 1200
    assert result.confidence == 0.7


@respx.mock
async def test_ollama_extract_raises_on_http_error():
    """Errors must propagate so ResilientProvider can fall back deterministically."""
    respx.post("http://localhost:11434/api/generate").mock(return_value=httpx.Response(500))
    try:
        await OllamaProvider().extract(ExtractionInput(item_title="x", signals=[]))
    except Exception:
        return
    raise AssertionError("expected the HTTP error to propagate")


async def test_ollama_inherits_deterministic_sift():
    """OllamaProvider doesn't override sift, so it uses the offline keyword sift."""
    findings = await OllamaProvider().sift(
        SiftInput(
            instruction="anchor spacing",
            signals=[{"id": "1", "title": "anchor spacing detail", "body": "grid C-4"}],
        )
    )
    assert findings and "1" in findings[0].matched_signal_ids


# --------------------------------------------------------------------------- #
# Script scheduling (run_due_scripts was uncovered)
# --------------------------------------------------------------------------- #
_EMIT_ONE = 'osprey.emit_signal("From schedule", "body", external_id="sched-1")'


async def _project(session) -> Project:
    org = Org(name="Sched Co")
    session.add(org)
    await session.flush()
    project = Project(org_id=org.id, name="Tower B")
    session.add(project)
    await session.flush()
    return project


async def test_run_due_scripts_runs_never_run_task(session):
    project = await _project(session)
    session.add(
        ScriptTask(
            org_id=project.org_id,
            project_id=project.id,
            name="due-now",
            source_code=_EMIT_ONE,
            enabled=True,
            schedule_minutes=5,
        )
    )
    await session.flush()

    result = await run_due_scripts(session)
    assert result["ran"] == 1


async def test_run_due_scripts_skips_recently_run_and_disabled(session):
    project = await _project(session)
    session.add(
        ScriptTask(
            org_id=project.org_id,
            project_id=project.id,
            name="just-ran",
            source_code=_EMIT_ONE,
            enabled=True,
            schedule_minutes=60,
            last_run=utcnow(),
            status=ScriptStatus.ok,
        )
    )
    session.add(
        ScriptTask(
            org_id=project.org_id,
            project_id=project.id,
            name="disabled",
            source_code=_EMIT_ONE,
            enabled=False,
            schedule_minutes=1,
        )
    )
    session.add(
        ScriptTask(
            org_id=project.org_id,
            project_id=project.id,
            name="on-demand-only",
            source_code=_EMIT_ONE,
            enabled=True,
            schedule_minutes=0,
        )
    )
    await session.flush()

    assert (await run_due_scripts(session))["ran"] == 0


async def test_run_due_scripts_respects_feature_flag(session, monkeypatch):
    from osprey.config import settings

    project = await _project(session)
    session.add(
        ScriptTask(
            org_id=project.org_id,
            project_id=project.id,
            name="flagged-off",
            source_code=_EMIT_ONE,
            enabled=True,
            schedule_minutes=1,
        )
    )
    await session.flush()

    monkeypatch.setattr(settings, "feature_scripts", False)
    assert (await run_due_scripts(session))["ran"] == 0
