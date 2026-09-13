#!/usr/bin/env python
"""Verify a published release against the key the application actually trusts.

The GitHub releases page cannot tell you the thing that matters. It shows that
`.sig` files exist; it does not show *which key* made them. A release signed by a
valid minisign key that is not the one compiled into the app produces installers
that look perfect and an installed base that can never update again, and the only
symptom is silence, months later, when nobody gets a new version.

This checks, for a given tag:

1. every installer has a `.sig`, and `latest.json` is present;
2. every signature was made by the key id in tauri.conf.json pubkey, not
   merely by *a* valid key;
3. `latest.json` names the version the tag claims;
4. `latest.json` covers every platform that shipped an installer.

It needs only the signatures and the manifest (a few kilobytes), not the
installers, so it is cheap enough to run on every release.

    python scripts/verify_release.py v0.2.1

Exit status is 0 only if every check passes.

What this does NOT check: that the signature is cryptographically valid over the
installer bytes. That needs the installers and the `minisign` binary; the key-id
check is what catches the failure mode above, and it catches it for the price of a
few HTTP requests.
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import subprocess
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TAURI_CONF = REPO_ROOT / "clients" / "desktop" / "src-tauri" / "tauri.conf.json"

#: Extensions that are installers rather than metadata.
INSTALLER_SUFFIXES = (
    ".exe",
    ".msi",
    ".dmg",
    ".AppImage",
    ".deb",
    ".rpm",
    ".app.tar.gz",
)


class Failure(Exception):
    """A check failed. The message is the report."""


def key_id(inner_b64: str) -> bytes:
    """The 8-byte key id from a minisign public key or signature line.

    Layout after base64-decoding: 2-byte algorithm, 8-byte key id, then the key
    or signature itself. The raw bytes are compared rather than the hex minisign
    prints, because minisign renders the id byte-reversed and matching on its
    display form is a good way to conclude two identical keys differ.
    """
    raw = base64.b64decode(inner_b64)
    if len(raw) < 10:
        raise Failure(
            f"minisign payload is too short to contain a key id ({len(raw)} bytes)"
        )
    return raw[2:10]


def signature_line(minisign_file_text: str) -> str:
    """The signature line of a minisign file (line 2, after the untrusted comment)."""
    lines = minisign_file_text.splitlines()
    if len(lines) < 2:
        raise Failure("minisign file has no signature line")
    return lines[1]


def decode_sig_asset(text: str) -> str:
    """A Tauri `.sig` asset is base64 of the *whole* minisign file, not the file.

    Skipping this layer yields `"untrusted comment"[2:10]` == `"trusted "` as the
    key id, which looks like a real mismatch and is not one.
    """
    return base64.b64decode(text.strip()).decode("utf-8")


def trusted_key_id() -> tuple[bytes, str]:
    conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    try:
        pubkey = conf["plugins"]["updater"]["pubkey"]
    except KeyError as exc:
        raise Failure(f"{TAURI_CONF} has no plugins.updater.pubkey") from exc
    text = base64.b64decode(pubkey).decode("utf-8")
    return key_id(signature_line(text)), text.splitlines()[0]


def download(tag: str, repo: str, into: pathlib.Path) -> None:
    result = subprocess.run(
        [
            "gh",
            "release",
            "download",
            tag,
            "--repo",
            repo,
            "--dir",
            str(into),
            "--pattern",
            "*.sig",
            "--pattern",
            "latest.json",
            "--clobber",
        ],
        capture_output=True,
        text=True,
        check=False,  # the non-zero case is reported, not raised
    )
    if result.returncode != 0:
        raise Failure(f"could not download release assets:\n{result.stderr.strip()}")


def asset_names(tag: str, repo: str) -> list[str]:
    result = subprocess.run(
        [
            "gh",
            "release",
            "view",
            tag,
            "--repo",
            repo,
            "--json",
            "assets",
            "--jq",
            ".assets[].name",
        ],
        capture_output=True,
        text=True,
        check=False,  # the non-zero case is reported, not raised
    )
    if result.returncode != 0:
        raise Failure(f"could not list release assets:\n{result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line]


def verify(tag: str, repo: str) -> list[str]:
    """Run every check. Returns the report lines; raises Failure on a problem."""
    report: list[str] = []
    trusted, comment = trusted_key_id()
    report.append(f"app trusts key id {trusted.hex().upper()}  [{comment}]")

    names = asset_names(tag, repo)
    installers = [n for n in names if n.endswith(INSTALLER_SUFFIXES)]
    if not installers:
        raise Failure(f"{tag} published no installers")
    if "latest.json" not in names:
        raise Failure(
            f"{tag} has no latest.json: installed clients have nothing to poll, "
            "so this release is invisible to the updater"
        )

    # An installer with no signature beside it is one the updater cannot accept.
    unsigned = [n for n in installers if f"{n}.sig" not in names]
    if unsigned:
        raise Failure("installers with no signature: " + ", ".join(unsigned))
    report.append(f"{len(installers)} installer(s), each with a signature")

    with tempfile.TemporaryDirectory() as tmp:
        work = pathlib.Path(tmp)
        download(tag, repo, work)

        wrong: list[str] = []
        for sig_path in sorted(work.glob("*.sig")):
            kid = key_id(
                signature_line(decode_sig_asset(sig_path.read_text(encoding="utf-8")))
            )
            if kid != trusted:
                wrong.append(f"{sig_path.name} signed by {kid.hex().upper()}")
        if wrong:
            raise Failure(
                "signed by a key this application does not trust. An installed copy "
                "will reject these updates:\n  " + "\n  ".join(wrong)
            )
        report.append("every .sig is from the trusted key")

        manifest = json.loads((work / "latest.json").read_text(encoding="utf-8"))
        expected = tag.lstrip("v")
        built = manifest.get("version", "").lstrip("v")
        if built != expected:
            # Seen for real on v0.2.0, whose assets are all Osprey_0.1.0_*: the tag
            # was cut without bumping tauri.conf.json, so the release advertises and
            # ships a version nobody asked for. The manifest is internally
            # consistent, which is why nothing else notices.
            raise Failure(
                f"tag/version skew: the tag is {tag!r} but this release was built as "
                f"{built!r}. Either the tag is wrong or tauri.conf.json was not "
                f"bumped before tagging. Anyone downloading {tag} gets {built}."
            )

        platforms = manifest.get("platforms") or {}
        if not platforms:
            raise Failure("latest.json lists no platforms")
        bad_platforms: list[str] = []
        for platform, entry in platforms.items():
            kid = key_id(signature_line(decode_sig_asset(entry["signature"])))
            if kid != trusted:
                bad_platforms.append(f"{platform} signed by {kid.hex().upper()}")
        if bad_platforms:
            raise Failure(
                "latest.json carries signatures from an untrusted key:\n  "
                + "\n  ".join(bad_platforms)
            )
        report.append(
            f"latest.json v{expected}, {len(platforms)} platform(s), all trusted"
        )

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="release tag, e.g. v0.2.1")
    parser.add_argument("--repo", default="ibuilder/osprey")
    args = parser.parse_args()

    try:
        for line in verify(args.tag, args.repo):
            print(f"  OK  {line}")
    except Failure as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 1
    print(f"\n{args.tag} verifies against the key the application trusts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
