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
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if all(k in data for k in ("secret_key", "encryption_key", "webhook_hmac_secret")):
            return data

    data = {
        "secret_key": secrets.token_urlsafe(48),
        "encryption_key": secrets.token_urlsafe(48),
        "webhook_hmac_secret": secrets.token_urlsafe(32),
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    with contextlib.suppress(OSError):  # not supported on every platform
        path.chmod(0o600)
    return data


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
