"""Local (desktop) entrypoint — the backend as a sidecar inside the Tauri app.

The desktop bundle ships this frozen as a single binary. The Rust shell picks a free
port, spawns it, and shuts it down on exit; everything else about running privately on
one machine is decided here rather than in Rust, so it stays testable in Python:

  * data lives under the OS app-data directory, not the working directory,
  * secrets are generated once on first run and reused (nothing hard-coded),
  * SQLite, offline AI provider — no Docker, no Postgres, no keys required.

Run directly for development:  python -m osprey.local --port 8000
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import sys
from pathlib import Path

APP_DIR_NAME = "Osprey"
SECRETS_FILE = "secrets.json"
DB_FILE = "osprey.db"

# Origins the Tauri webview presents. Windows serves the bundled frontend from
# http(s)://tauri.localhost; macOS and Linux use the tauri:// scheme.
TAURI_ORIGINS = [
    "http://tauri.localhost",
    "https://tauri.localhost",
    "tauri://localhost",
]


def app_data_dir() -> Path:
    """Per-user data directory, following each platform's convention."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    path = base / APP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_or_create_secrets(directory: Path) -> dict[str, str]:
    """Generate signing/encryption keys on first run; reuse them afterwards.

    Losing these makes existing sealed connector tokens undecryptable, so they are
    written once and never regenerated. The file is created 0600 where the platform
    supports it — on a single-user desktop this is the pragmatic equivalent of the
    server deployment's KMS/Vault.
    """
    path = directory / SECRETS_FILE
    existing: dict[str, str] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = {k: v for k, v in loaded.items() if isinstance(v, str) and v}
        except (json.JSONDecodeError, OSError):
            # A truncated or corrupt file must not brick every future launch. Keep a
            # copy for forensics and continue with fresh keys — connector tokens
            # sealed with the lost key are unrecoverable either way.
            with contextlib.suppress(OSError):
                path.replace(path.with_suffix(".json.corrupt"))

    generators = {
        "secret_key": lambda: secrets.token_urlsafe(48),
        "encryption_key": lambda: secrets.token_urlsafe(48),
        "webhook_hmac_secret": lambda: secrets.token_urlsafe(32),
    }
    # Fill in only what is missing. Regenerating a key that already exists would
    # orphan every connector token sealed with it, so keys are never replaced.
    data = {name: existing.get(name) or make() for name, make in generators.items()}
    if data != existing:
        _write_private_json(path, data)
    return data


def _write_private_json(path: Path, data: dict[str, str]) -> None:
    """Write owner-only and atomically, so the key file is never world-readable
    and never left half-written if the process dies mid-write."""
    tmp = path.with_suffix(".json.tmp")
    # Create with 0600 from the start rather than chmod-ing afterwards, which would
    # leave a window at the default umask (and is a no-op on Windows).
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)  # atomic on POSIX and Windows
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def configure_environment(directory: Path | None = None) -> Path:
    """Point the app at per-user storage and its own secrets. Returns the data dir.

    Anything already set in the environment wins, so a power user can still aim the
    desktop build at Postgres or a hosted backend.
    """
    if directory is None:
        directory = app_data_dir()
    else:
        # An explicit --data-dir may not exist yet; app_data_dir() creates its own.
        directory.mkdir(parents=True, exist_ok=True)
    creds = load_or_create_secrets(directory)

    db_path = (directory / DB_FILE).as_posix()
    defaults = {
        "OSPREY_ENV": "prod",
        "OSPREY_DEBUG": "false",
        "OSPREY_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "OSPREY_SECRET_KEY": creds["secret_key"],
        "OSPREY_ENCRYPTION_KEY": creds["encryption_key"],
        "OSPREY_WEBHOOK_HMAC_SECRET": creds["webhook_hmac_secret"],
        # Local mode is offline by default; the user can attach their own AI key
        # from the app afterwards.
        "OSPREY_AI_PROVIDER": "deterministic",
        # There is no Alembic step in the desktop bundle, so the app creates its own
        # schema on start. (Server deployments keep this off and migrate explicitly.)
        "OSPREY_CREATE_SCHEMA_ON_START": "true",
        # This runs as env=prod, where CORS is deny-by-default. The webview is a
        # different origin from the loopback API, so name the Tauri origins
        # explicitly — Windows uses http(s)://tauri.localhost, macOS/Linux tauri://.
        "OSPREY_CORS_ALLOW_ORIGINS": json.dumps(TAURI_ORIGINS),
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)
    return directory


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="osprey-backend", description="Osprey local backend")
    parser.add_argument("--port", type=int, default=8000, help="port to listen on")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (loopback by default)")
    parser.add_argument("--data-dir", default=None, help="override the per-user data directory")
    args = parser.parse_args(argv)

    directory = configure_environment(Path(args.data_dir) if args.data_dir else None)

    # Imported only after the environment is configured — settings read it at import.
    import uvicorn

    from .main import app

    print(f"osprey: data directory {directory}", flush=True)
    print(f"osprey: listening on http://{args.host}:{args.port}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
