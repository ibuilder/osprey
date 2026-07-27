"""Desktop (sidecar) mode: per-user storage and first-run secret generation.

This is what the frozen binary inside the Tauri bundle runs, so the important
properties are that it never invents new keys for an existing install (that would
orphan every sealed connector token) and never falls back to the insecure defaults.
"""

from __future__ import annotations

import json

from osprey.local import (
    DB_FILE,
    SECRETS_FILE,
    app_data_dir,
    configure_environment,
    load_or_create_secrets,
)


def test_secrets_are_generated_on_first_run(tmp_path):
    creds = load_or_create_secrets(tmp_path)

    assert set(creds) == {"secret_key", "encryption_key", "webhook_hmac_secret"}
    assert all(len(v) >= 32 for v in creds.values())
    # Never the placeholder values that ship in .env.example.
    assert all("dev-only-insecure" not in v for v in creds.values())
    assert (tmp_path / SECRETS_FILE).exists()


def test_secrets_are_stable_across_restarts(tmp_path):
    """Regenerating keys would make previously sealed tokens undecryptable."""
    first = load_or_create_secrets(tmp_path)
    second = load_or_create_secrets(tmp_path)
    assert first == second


def test_incomplete_secrets_file_is_regenerated(tmp_path):
    (tmp_path / SECRETS_FILE).write_text(json.dumps({"secret_key": "only-one"}), encoding="utf-8")

    creds = load_or_create_secrets(tmp_path)

    assert "encryption_key" in creds and "webhook_hmac_secret" in creds


def test_configure_environment_points_at_per_user_storage(tmp_path, monkeypatch):
    for var in (
        "OSPREY_DATABASE_URL",
        "OSPREY_SECRET_KEY",
        "OSPREY_ENCRYPTION_KEY",
        "OSPREY_WEBHOOK_HMAC_SECRET",
        "OSPREY_CREATE_SCHEMA_ON_START",
        "OSPREY_AI_PROVIDER",
    ):
        monkeypatch.delenv(var, raising=False)

    directory = configure_environment(tmp_path / "data")
    import os

    assert directory.exists()
    assert os.environ["OSPREY_DATABASE_URL"].startswith("sqlite+aiosqlite:///")
    assert DB_FILE in os.environ["OSPREY_DATABASE_URL"]
    # The desktop bundle has no Alembic step, so it creates its schema itself.
    assert os.environ["OSPREY_CREATE_SCHEMA_ON_START"] == "true"
    # Offline by default — no key required to be useful.
    assert os.environ["OSPREY_AI_PROVIDER"] == "deterministic"


def test_existing_environment_wins(tmp_path, monkeypatch):
    """A power user can still point the desktop build at their own database."""
    monkeypatch.setenv("OSPREY_DATABASE_URL", "postgresql+asyncpg://u:p@host/db")

    configure_environment(tmp_path / "data")

    import os

    assert os.environ["OSPREY_DATABASE_URL"] == "postgresql+asyncpg://u:p@host/db"


def test_app_data_dir_is_created_and_per_user():
    directory = app_data_dir()
    assert directory.exists()
    assert directory.name == "Osprey"
