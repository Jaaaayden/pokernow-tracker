"""The log folder: one `poker_now_log_<id>.csv` per game, the running record.

Manual exports, `pnt backfill` and live capture all write here, in PokerNow's own
export layout, so the folder alone can rebuild a database with `pnt import`.

Where it is: $PNT_LOG_DIR, else ~/Downloads/pokernow-logs. The environment variable
is read once, at import -- so the CLI also takes `--log-dir`, and the server has to
be restarted to move it.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

from .csv_source import RawEntry, read_csv

DEFAULT_LOG_DIR = Path.home() / "Downloads" / "pokernow-logs"
LOG_DIR = Path(os.environ.get("PNT_LOG_DIR") or DEFAULT_LOG_DIR)

#: Whether live capture writes each game's CSV here. On unless PNT_SAVE_LOGS=0.
SAVE_LOGS = os.environ.get("PNT_SAVE_LOGS", "1").strip().lower() not in ("0", "false", "off", "")

# Two tabs on one table post the same game from two server threads. Each file gets a
# lock, so neither merges from a copy the other is halfway through replacing.
_locks: dict[Path, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(path.resolve(), threading.Lock())


def log_path(folder: Path, game_id: str) -> Path:
    return folder / f"poker_now_log_{game_id}.csv"


def _quote(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def write_log(entries: list[RawEntry], path: Path) -> int:
    """Write entries as a PokerNow export, merged with whatever `path` already holds.

    Merged, not overwritten: re-fetching with a cookie adds your `Your hand is` lines
    to a file first fetched without one, and a manual export already in the folder
    loses nothing. `order` is the dedupe key, as everywhere else. Returns how many
    lines were new to the file.

    The layout copies PokerNow's own: `entry` always quoted, newest first, LF.
    """
    with _lock_for(path):
        merged = {e.ord: e for e in read_csv(path)} if path.exists() else {}
        before = len(merged)
        for e in entries:
            merged.setdefault(e.ord, e)
        if len(merged) == before and path.exists():
            return 0  # nothing new: leave the file, and its timestamp, alone
        path.parent.mkdir(parents=True, exist_ok=True)
        # Unique per writer: `pnt backfill` in a terminal and the server can both be
        # writing this game, and a shared temp name would interleave them.
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.part")
        try:
            with tmp.open("w", encoding="utf-8", newline="") as fh:
                fh.write("entry,at,order\n")
                for e in sorted(merged.values(), key=lambda e: e.ord, reverse=True):
                    fh.write(f"{_quote(e.entry)},{e.at},{e.ord}\n")
            os.replace(tmp, path)  # never leave a half-written log where `pnt import` looks
        finally:
            tmp.unlink(missing_ok=True)
        return len(merged) - before


def save_game(conn: sqlite3.Connection, game_id: str, folder: Path | None = None) -> int:
    """Write a stored game's raw lines to its CSV in the log folder. Returns lines added."""
    rows = conn.execute(
        "SELECT ord, at, entry FROM raw_entries WHERE game_id = ?", (game_id,)
    ).fetchall()
    if not rows:
        return 0
    entries = [RawEntry(ord=r[0], at=r[1], entry=r[2]) for r in rows]
    return write_log(entries, log_path(folder or LOG_DIR, game_id))
