#!/usr/bin/env python
"""Make `latest.json` carry the signatures that are actually published.

The updater manifest embeds a copy of each installer's `.sig`. tauri-action writes
both at build time, so they normally agree. They stop agreeing when an installer
is changed after the build -- which Authenticode signing does on purpose: SignPath
returns new installer bytes, the release workflow recomputes each `.sig` over them
and re-uploads it, and the manifest still holds the signatures of the unsigned
build. An installed copy would then download the signed installer, check it
against the stale signature from the manifest, and reject the update.

This runs after every desktop build has finished, so no matrix leg can overwrite
the manifest afterwards. For each platform in `latest.json` it takes the published
`<installer>.sig` and writes it into the manifest, uploading the manifest only if
something changed. It is idempotent and harmless when nothing was re-signed.

    python scripts/sync_manifest_signatures.py v0.3.0
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import tempfile


class Failure(Exception):
    """The manifest cannot be brought in line. The message is the report."""


def gh(*args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=False,  # the non-zero case is reported, not raised
    )
    if result.returncode != 0:
        raise Failure(f"gh {' '.join(args[:2])} failed:\n{result.stderr.strip()}")
    return result


def sync(tag: str, repo: str, *, dry_run: bool) -> list[str]:
    changes: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        work = pathlib.Path(tmp)
        gh(
            "release", "download", tag, "--repo", repo, "--dir", str(work),
            "--pattern", "latest.json", "--pattern", "*.sig", "--clobber",
        )  # fmt: skip
        manifest_path = work / "latest.json"
        if not manifest_path.exists():
            raise Failure(f"{tag} has no latest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        for platform, entry in (manifest.get("platforms") or {}).items():
            name = str(entry.get("url", "")).rsplit("/", 1)[-1]
            sig_path = work / f"{name}.sig"
            if not name or not sig_path.exists():
                raise Failure(f"{platform}: no published signature for {name!r}")
            published = sig_path.read_text(encoding="utf-8").strip()
            if entry.get("signature", "").strip() != published:
                entry["signature"] = published
                changes.append(f"{platform}: took the signature from {name}.sig")

        if changes and not dry_run:
            manifest_path.write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
            )
            gh(
                "release",
                "upload",
                tag,
                str(manifest_path),
                "--repo",
                repo,
                "--clobber",
            )
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="release tag, e.g. v0.3.0")
    parser.add_argument("--repo", default="ibuilder/osprey")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without uploading",
    )
    args = parser.parse_args()

    try:
        changes = sync(args.tag, args.repo, dry_run=args.dry_run)
    except Failure as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    if not changes:
        print(f"{args.tag}: latest.json already matches every published signature.")
        return 0
    for line in changes:
        print(f"  {line}")
    verb = "would update" if args.dry_run else "updated"
    print(f"{args.tag}: {verb} latest.json ({len(changes)} platform(s)).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
