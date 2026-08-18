"""Single source of truth for writable runtime data.

Source files stay read-only. Databases, uploads, imports, stickers, diagnostics and
memory logs are derived from ``JTYHOME_DATA_DIR`` so tests, containers and backups
can isolate the complete private state with one setting.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _probe_writable_dir(path: Path) -> bool:
    """Return True only when a tiny file can actually be created and removed."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".jtyhome-write-test-{os.getpid()}"
        probe.write_bytes(b"ok")
        probe.unlink()
        return True
    except OSError:
        return False


def _resolve_data_dir() -> Path:
    explicit = str(os.getenv("JTYHOME_DATA_DIR") or "").strip()
    if explicit:
        # An explicit location is authoritative: do not silently redirect private
        # data elsewhere if the operator configured a bad path.
        return Path(explicit).expanduser().resolve()

    portable = (BASE_DIR / ".jtyhome-data").expanduser().resolve()
    if _probe_writable_dir(portable):
        return portable

    # Read-only app bundles / mounted install directories need a writable user
    # location.  Keep this independent from legacy migration: no old data is
    # scanned or copied here.
    if os.name == "nt" and os.getenv("LOCALAPPDATA"):
        fallback = Path(os.environ["LOCALAPPDATA"]) / "JTYHome"
    elif sys.platform == "darwin":
        fallback = Path.home() / "Library" / "Application Support" / "JTYHome"
    elif os.getenv("XDG_DATA_HOME"):
        fallback = Path(os.environ["XDG_DATA_HOME"]) / "jtyhome"
    else:
        fallback = Path.home() / ".local" / "share" / "jtyhome"
    return fallback.expanduser().resolve()


DATA_DIR = _resolve_data_dir()

UPLOAD_DIR = DATA_DIR / "uploads"
IMPORT_DIR = DATA_DIR / "conversation-imports"
STICKER_DATA_DIR = DATA_DIR / "stickers"
STICKER_DIR = STICKER_DATA_DIR / "files"
DIAGNOSTICS_DIR = DATA_DIR / "diagnostics"
MEMORY_DIR = DATA_DIR / "memory"


def ensure_runtime_dirs() -> None:
    for path in (
        DATA_DIR,
        UPLOAD_DIR,
        IMPORT_DIR,
        STICKER_DATA_DIR,
        STICKER_DIR,
        DIAGNOSTICS_DIR,
        MEMORY_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)



# Runtime directories are always fresh-location only. Historical conversations
# enter the app exclusively through the explicit conversation import feature;
# startup never scans or copies private state from older install layouts.


ensure_runtime_dirs()
