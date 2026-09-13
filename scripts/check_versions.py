#!/usr/bin/env python
"""Fail if Osprey's version strings disagree, or disagree with a release tag.

The version lives in several files that nothing ties together. They drifted for
real: tag v0.2.0 shipped Osprey_0.1.0_* installers because tauri.conf.json was
not bumped, and the backend reported 0.1.0 from /health inside images tagged
v0.2.x. verify_release.py catches the first case, but only after a full build;
this catches both before anything is built.

    python scripts/check_versions.py              # every file agrees
    python scripts/check_versions.py --tag v0.3.0 # ...and matches the tag

Standard library only, and no tomllib, so it runs on whatever Python a CI runner
ships (ubuntu-22.04 still has 3.10).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _match(rel: str, pattern: str) -> str:
    found = re.search(pattern, _read(rel), flags=re.MULTILINE)
    if not found:
        raise SystemExit(f"FAILED: no version found in {rel} (pattern {pattern!r})")
    return found.group(1)


def _cargo_lock_version() -> str:
    # The lock records the crate's own version too, and a stale one is rewritten
    # by the first cargo build -- in CI, not in the commit.
    text = _read("clients/desktop/src-tauri/Cargo.lock")
    block = re.search(r'\[\[package\]\]\nname = "osprey-desktop"\nversion = "([^"]+)"', text)
    if not block:
        raise SystemExit("FAILED: osprey-desktop is not in clients/desktop/src-tauri/Cargo.lock")
    return block.group(1)


def collect() -> dict[str, str]:
    lock = json.loads(_read("clients/desktop/package-lock.json"))
    return {
        "clients/desktop/src-tauri/tauri.conf.json": json.loads(
            _read("clients/desktop/src-tauri/tauri.conf.json")
        )["version"],
        "clients/desktop/package.json": json.loads(_read("clients/desktop/package.json"))["version"],
        "clients/desktop/package-lock.json": lock["version"],
        'clients/desktop/package-lock.json packages[""]': lock["packages"][""]["version"],
        "clients/desktop/src-tauri/Cargo.toml": _match(
            "clients/desktop/src-tauri/Cargo.toml", r'^version\s*=\s*"([^"]+)"'
        ),
        "clients/desktop/src-tauri/Cargo.lock": _cargo_lock_version(),
        "backend/pyproject.toml": _match("backend/pyproject.toml", r'^version\s*=\s*"([^"]+)"'),
        "backend/osprey/__init__.py": _match(
            "backend/osprey/__init__.py", r'^__version__\s*=\s*"([^"]+)"'
        ),
        "deploy/helm/Chart.yaml appVersion": _match(
            "deploy/helm/Chart.yaml", r'^appVersion:\s*"?([^"\s]+)"?'
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tag", help="release tag the versions must match, e.g. v0.3.0")
    args = parser.parse_args()

    versions = collect()
    expected = args.tag[1:] if args.tag and args.tag.startswith("v") else args.tag
    if expected is None:
        # No tag: the most common value is the intended one, so the report names
        # the odd files out rather than blaming whichever file was read first.
        values = list(versions.values())
        expected = max(set(values), key=values.count)

    wrong = {path: v for path, v in versions.items() if v != expected}
    width = max(len(p) for p in versions)
    for path, v in versions.items():
        print(f"  {'OK ' if v == expected else 'BAD'}  {path:<{width}}  {v}")
    if wrong:
        origin = f"the tag {args.tag}" if args.tag else "the other files"
        print(f"\nFAILED: {len(wrong)} version string(s) disagree with {origin} ({expected}).", file=sys.stderr)
        return 1
    print(f"\nEvery version string is {expected}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
