#!/usr/bin/env python
"""Regenerate ``constraints-prod.txt`` from the current pyproject + constraints.txt.

Osprey pins in two files:

``constraints.txt``
    the set the test suite installs (``.[dev]``). Dependabot edits this one, and
    it is the authority for anything the two files share.

``constraints-prod.txt``
    the runtime extras the test suite never installs but the Docker image does --
    asyncpg, arq, alembic, the AI SDKs, push, OTel. Dependabot cannot maintain
    this: it resolves each manifest in isolation, so it neither knows the two are
    applied together nor that this one is *generated* rather than hand-edited.

Run this after any dependency change that CI's "Production constraints are
current" step rejects:

    python backend/tools/regen_constraints.py

It resolves with ``pip install --dry-run``, so nothing is installed into the
current environment and it is safe to run against your working venv.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
DEV_CONSTRAINTS = BACKEND / "constraints.txt"
PROD_CONSTRAINTS = BACKEND / "constraints-prod.txt"

#: Every extra the Docker image can install. `otel` is not in the image's default
#: build, but it is pinned here so `--build-arg EXTRAS=...,otel` resolves against
#: reviewed versions rather than whatever is current that day.
EXTRAS = "prod,ai,push,otel"

HEADER = f"""# Osprey backend PRODUCTION dependency constraints.
#
# constraints.txt pins the set the test suite installs (`.[dev]`). It does not
# cover the runtime extras -- asyncpg, arq, alembic, the AI SDKs, push, OTel --
# which the Docker image installs. Unpinned, two builds of the same commit could
# ship different dependency versions, and "pinned dependencies" in SECURITY.md
# would not be true of the artefact anyone actually deploys.
#
# Used together, never alone:
#   pip install -c constraints.txt -c constraints-prod.txt ".[{EXTRAS}]"
#
# This file lists only what constraints.txt does not already pin, so the two can
# never disagree about a shared package.
#
# GENERATED -- do not hand-edit. Regenerate after a dependency change with:
#   python backend/tools/regen_constraints.py
"""


def parse_pins(path: Path) -> dict[str, str]:
    """``name -> version`` for every ``name==version`` line in a pin file."""
    pins: dict[str, str] = {}
    if not path.exists():
        return pins
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, _, version = line.partition("==")
        pins[name.strip()] = version.strip()
    return pins


def resolve(spec: str, constraints: list[Path]) -> dict[str, str]:
    """Resolve ``spec`` without installing anything, via pip's --report.

    ``constraints`` matters more than it looks. Resolving unconstrained picks
    whatever is newest on PyPI that day, so a ``--check`` built that way fails
    every time any production dependency publishes a release -- which is how
    this check first went red in CI, on nothing but upstream patch bumps.
    """
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--dry-run",
                "--quiet",
                "--report",
                str(report),
                *[arg for c in constraints for arg in ("-c", str(c))],
                spec,
            ],
            cwd=BACKEND,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            sys.exit(
                "pip could not resolve the production extras:\n"
                + (result.stderr or result.stdout)[-3000:]
            )
        data = json.loads(report.read_text(encoding="utf-8"))

    pins: dict[str, str] = {}
    for item in data["install"]:
        meta = item["metadata"]
        if meta["name"] == "osprey-core":  # the project itself
            continue
        pins[meta["name"]] = meta["version"]
    return pins


def render(pins: dict[str, str]) -> str:
    body = "\n".join(
        f"{name}=={version}" for name, version in sorted(pins.items(), key=lambda kv: kv[0].lower())
    )
    return f"{HEADER}\n{body}\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the file is out of date instead of rewriting it",
    )
    args = parser.parse_args()

    dev = {name.lower() for name in parse_pins(DEV_CONSTRAINTS)}
    # --check holds the committed prod pins fixed, so it fails only when the set
    # of packages changes or the two files stop resolving together -- not when
    # something upstream ships a release. Regenerating is the deliberate upgrade,
    # so it resolves against the dev pins alone and takes whatever is current.
    constraints = [DEV_CONSTRAINTS]
    if args.check and PROD_CONSTRAINTS.exists():
        constraints.append(PROD_CONSTRAINTS)
    resolved = resolve(f".[{EXTRAS}]", constraints)
    # Only what constraints.txt does not already pin, so the two files can never
    # disagree about a shared package and Dependabot's edits to the dev file stay
    # authoritative.
    prod = {name: version for name, version in resolved.items() if name.lower() not in dev}

    rendered = render(prod)
    current = PROD_CONSTRAINTS.read_text(encoding="utf-8") if PROD_CONSTRAINTS.exists() else ""

    if args.check:
        if rendered != current:
            import difflib

            # Say what changed. "Out of date" alone sent the last reader off to
            # regenerate locally just to find out.
            diff = difflib.unified_diff(
                current.splitlines(),
                rendered.splitlines(),
                "constraints-prod.txt (committed)",
                "constraints-prod.txt (resolved)",
                lineterm="",
            )
            print("\n".join(diff), file=sys.stderr)
            print(
                "\nconstraints-prod.txt is out of date.\n"
                "Run: python backend/tools/regen_constraints.py",
                file=sys.stderr,
            )
            return 1
        print(f"constraints-prod.txt is current ({len(prod)} pins).")
        return 0

    if rendered == current:
        print(f"constraints-prod.txt already current ({len(prod)} pins).")
        return 0
    PROD_CONSTRAINTS.write_text(rendered, encoding="utf-8")
    print(f"Wrote {PROD_CONSTRAINTS.name}: {len(prod)} pins.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
