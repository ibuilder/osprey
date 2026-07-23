"""Sandboxed runner for user-authored Python scripts.

A user's script runs in a **separate, isolated subprocess** (``python -I``) with:
  * a hard wall-clock timeout,
  * a scrubbed environment (no Osprey secrets, DB URLs, or provider keys),
  * a tiny ``osprey`` API — ``osprey.emit_signal(...)`` / ``osprey.log(...)`` —
    that streams records back over stdout using line markers.

Emitted signals flow through the normal ingest/cluster/score pipeline via the
``pyscript`` connector, so a script's output becomes a first-class hotlist item.

Defense-in-depth note: this is process isolation + timeout + env scrubbing, not a
full kernel sandbox. For untrusted multi-tenant scripts, wrap the interpreter with
an OS sandbox by setting ``OSPREY_SCRIPT_SANDBOX_CMD`` (e.g. ``nsjail -Mo --`` /
``firejail --quiet``); the runner prepends it to the command.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime

from ..config import settings
from ..connectors.base import RawEvent
from ..models import SourceKind

_EMIT = "OSPREY_EMIT::"
_LOG = "OSPREY_LOG::"
_ERR = "OSPREY_ERR::"

# The harness runs inside the subprocess. It provides the `osprey` object to the
# user's script, execs the script, and streams emitted records over stdout.
_HARNESS = r'''
import json, sys, hashlib

class _Osprey:
    def __init__(self, ctx):
        self.ctx = ctx
        self._n = 0
    def _emit(self, kind, payload):
        sys.stdout.write(kind + json.dumps(payload, default=str) + "\n")
        sys.stdout.flush()
    def log(self, message):
        self._emit("OSPREY_LOG::", {"message": str(message)})
    def emit_signal(self, title, body="", *, external_id=None, source_kind="general",
                    due_at=None, amount=None, url=None, thread_key=None, participants=None, raw=None):
        self._n += 1
        if not external_id:
            external_id = "script:" + hashlib.sha256(
                (str(title) + str(body) + str(self._n)).encode()).hexdigest()[:16]
        self._emit("OSPREY_EMIT::", {
            "external_id": str(external_id), "title": str(title), "body": str(body),
            "source_kind": source_kind, "due_at": due_at, "amount": amount, "url": url,
            "thread_key": thread_key, "participants": participants or [], "raw": raw or {},
        })
    # Alias — a "finding" is just a signal Osprey will score and rank.
    emit_finding = emit_signal

def _main():
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        source = fh.read()
    ctx = json.loads(sys.argv[2])
    osprey = _Osprey(ctx)
    g = {"__name__": "__osprey_script__", "osprey": osprey, "ctx": ctx}
    try:
        exec(compile(source, "<user_script>", "exec"), g, g)
    except Exception as exc:  # noqa
        import traceback
        sys.stdout.write("OSPREY_ERR::" + json.dumps({
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc()[-1500:],
        }) + "\n")
        sys.stdout.flush()

_main()
'''


@dataclass
class RunOutput:
    status: str                       # "ok" | "error" | "timeout"
    events: list[RawEvent] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    error: str | None = None


def _scrubbed_env() -> dict[str, str]:
    """Minimal environment — no OSPREY_* secrets, keys, or DB URLs leak into scripts."""
    keep = {}
    for var in ("PATH", "SYSTEMROOT", "SystemRoot", "WINDIR", "TEMP", "TMP", "LANG", "TZ"):
        if var in os.environ:
            keep[var] = os.environ[var]
    keep["OSPREY_SCRIPT_SANDBOX"] = "1"
    keep["PYTHONIOENCODING"] = "utf-8"
    return keep


def run_source(
    source_code: str, *, ctx: dict, timeout_seconds: int = 30
) -> RunOutput:
    timeout = max(1, min(timeout_seconds, settings.scripts_max_timeout_seconds))
    with tempfile.TemporaryDirectory(prefix="osprey-script-") as tmp:
        user_path = os.path.join(tmp, "user_script.py")
        harness_path = os.path.join(tmp, "_harness.py")
        with open(user_path, "w", encoding="utf-8") as fh:
            fh.write(source_code)
        with open(harness_path, "w", encoding="utf-8") as fh:
            fh.write(_HARNESS)

        cmd = [sys.executable, "-I", harness_path, user_path, json.dumps(ctx, default=str)]
        sandbox = settings.__dict__.get("script_sandbox_cmd") or os.environ.get("OSPREY_SCRIPT_SANDBOX_CMD")
        if sandbox:
            cmd = sandbox.split() + cmd

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                env=_scrubbed_env(), cwd=tmp,
            )
        except subprocess.TimeoutExpired:
            return RunOutput(status="timeout", error=f"script exceeded {timeout}s timeout")

        return _parse_output(proc.stdout, proc.stderr, proc.returncode)


def _parse_output(stdout: str, stderr: str, returncode: int) -> RunOutput:
    events: list[RawEvent] = []
    logs: list[str] = []
    error: str | None = None
    for line in stdout.splitlines():
        if line.startswith(_EMIT):
            try:
                data = json.loads(line[len(_EMIT):])
                events.append(_to_event(data))
            except Exception as exc:  # noqa: BLE001
                logs.append(f"bad emit: {exc}")
        elif line.startswith(_LOG):
            with contextlib.suppress(Exception):
                logs.append(json.loads(line[len(_LOG):]).get("message", ""))
        elif line.startswith(_ERR):
            try:
                payload = json.loads(line[len(_ERR):])
                error = payload.get("error")
                logs.append(payload.get("trace", ""))
            except Exception:  # noqa: BLE001
                error = "script error"
    if error is None and returncode != 0:
        error = (stderr or "").strip()[-500:] or f"exit code {returncode}"
    return RunOutput(status="error" if error else "ok", events=events, logs=logs, error=error)


def _to_event(data: dict) -> RawEvent:
    try:
        kind = SourceKind(data.get("source_kind", "general"))
    except ValueError:
        kind = SourceKind.general
    due = data.get("due_at")
    due_at = None
    if due:
        try:
            due_at = datetime.fromisoformat(str(due).replace("Z", "+00:00"))
        except ValueError:
            due_at = None
    return RawEvent(
        external_id=data["external_id"],
        source_kind=kind,
        thread_key=data.get("thread_key"),
        title=data.get("title", ""),
        body=data.get("body", ""),
        participants=data.get("participants", []),
        due_at=due_at,
        amount=data.get("amount"),
        url=data.get("url"),
        raw=data.get("raw", {}),
    )
