"""Choices about this machine, kept outside the database: which database, which log folder.

Not in the database because one of them *is* the database. Set from the front page
(or by `pnt service install --db`), and read by everything that starts later -- the
background task at login, `pnt serve`, and the CLI's defaults -- so a database or
folder picked once is the one every part of the tracker uses.

Lives in ~/.pnt beside the server log, for the reason given there (service.py): not
under %LOCALAPPDATA%, which Microsoft Store Python silently redirects. $PNT_CONFIG
points it elsewhere; the tests use that to keep away from the real one.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

PATH = Path(os.environ.get("PNT_CONFIG") or Path.home() / ".pnt" / "config.json")

# The server saves from its request threads: without this, two saves at once both
# read the old file, and the second write drops the first one's key.
_lock = threading.Lock()


def load() -> dict:
    """Everything set so far; empty when nothing is, or the file cannot be read."""
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get(key: str) -> str | None:
    value = load().get(key)
    return value if isinstance(value, str) and value else None


def put(key: str, value: str | None) -> None:
    """Set one choice (None removes it), keeping the rest. Replaced whole, never
    half-written: a crash mid-write must not lose the database's location."""
    with _lock:
        data = load()
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
        PATH.parent.mkdir(parents=True, exist_ok=True)
        # Unique per writer, as in log_folder.write_log: the CLI can be saving too.
        tmp = PATH.with_name(f"{PATH.name}.{os.getpid()}.{threading.get_ident()}.part")
        try:
            tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, PATH)
        finally:
            tmp.unlink(missing_ok=True)


def database(fallback: Path) -> Path:
    """The chosen database, or `fallback` when none is chosen or it has gone missing.

    Missing falls back rather than fails: a database moved by hand should still let
    the server start on the one it was installed with, not refuse to start at all.
    """
    chosen = get("db")
    return Path(chosen) if chosen and Path(chosen).is_file() else fallback
