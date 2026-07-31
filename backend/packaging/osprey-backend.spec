# PyInstaller spec: freeze the Osprey backend into a folder for the desktop bundle.
#
#   cd backend && pyinstaller packaging/osprey-backend.spec --noconfirm
#
# The Tauri shell ships the result as a bundled resource and spawns it on a free port.
# Only the offline path is bundled — SQLite, the deterministic AI provider — so the
# desktop app needs no Python, no Docker and no API keys. Postgres/Redis/AI extras stay
# excluded to keep it small; a user who wants them runs the server build instead.
#
# This produces a DIRECTORY (COLLECT), not a single file, and that is deliberate:
#
#   * --onefile unpacks the whole interpreter to a temp dir on every launch. That
#     self-extracting behaviour is exactly what antivirus heuristics flag, and
#     unsigned open-source builds get quarantined for it.
#   * The same unpacking is paid on every cold start, which is what forced the
#     "Starting your private copy…" wait in the UI.
#
# A directory build starts faster and looks like an ordinary application on disk.
# UPX stays off for the same reason — packed sections read as obfuscation.
import os

from PyInstaller.utils.hooks import collect_submodules

# Paths in a .spec resolve relative to the spec file, which lives in packaging/.
BACKEND_DIR = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 - PyInstaller global

hiddenimports = [
    # Imported dynamically by name, so static analysis cannot see them.
    "aiosqlite",
    "sqlalchemy.dialects.sqlite.aiosqlite",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    # Connectors register themselves via import side effects.
    *collect_submodules("osprey.connectors"),
]

a = Analysis(
    [os.path.join(BACKEND_DIR, "entry.py")],
    pathex=[BACKEND_DIR],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Optional server/AI extras: excluded so the bundle stays small.
    excludes=[
        "asyncpg", "pgvector", "arq", "redis", "alembic",
        "anthropic", "openai", "ollama", "pywebpush",
        "opentelemetry", "pytest", "respx", "mypy", "ruff",
        # NB: PIL stays IN — reportlab imports it for the PDF export.
        "tkinter", "matplotlib",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    # Keep the libraries out of the executable; COLLECT lays them out beside it.
    exclude_binaries=True,
    name="osprey-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821 - PyInstaller global
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="osprey-backend",
)
