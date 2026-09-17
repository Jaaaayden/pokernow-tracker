"""Keep the database in step with the log folder, in both directions.

A file new to the folder (or changed since it was last read) is imported; a game
whose every file on record is gone is deleted. `log_files` is the record: a game
only becomes subject to deletion once a file for it has been seen in the folder,
so games imported from elsewhere -- a Downloads copy, the bundled sample corpus --
never vanish because some other folder lacks them.

Deletion is whole files only. Ingest is append-only, so lines cut out of a CSV
that stays in the folder stay in the database too. To drop lines, move the file
out (the game goes with it at the next sync), then put the edited copy back.

Two guards against deleting what is merely out of reach: a file whose *folder* is
missing counts as unavailable, not deleted (an unplugged drive, a renamed folder),
and nothing here ever touches a game that has no `log_files` row.
"""

from __future__ import annotations

import csv
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ..db.conn import writing
from .csv_source import game_id_from_filename, read_csv
from .importer import delete_game, ingest_entries, rebuild_game

LOG_GLOB = "poker_now_log_*.csv"

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    imported: list[dict] = field(default_factory=list)  # import summaries, one per file read
    removed: dict[str, int] = field(default_factory=dict)  # game_id -> hands removed
    failed: dict[str, str] = field(default_factory=dict)  # path -> why it could not be read


def _key(path: Path) -> str:
    return str(path.resolve())


def record_file(conn: sqlite3.Connection, path: Path, game_id: str) -> None:
    """Note that `path` holds `game_id`, as it is on disk right now."""
    st = path.stat()
    with writing(conn):
        conn.execute(
            "INSERT INTO log_files (path, game_id, mtime_ns, size) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(path) DO UPDATE SET game_id = excluded.game_id,"
            " mtime_ns = excluded.mtime_ns, size = excluded.size",
            (_key(path), game_id, st.st_mtime_ns, st.st_size),
        )


def _gone(path: str) -> bool:
    """Deleted, as opposed to unreachable: the file is missing but its folder is not."""
    p = Path(path)
    return not p.exists() and p.parent.is_dir()


def prune(conn: sqlite3.Connection, game_ids: list[str] | None = None) -> dict[str, int]:
    """Delete every game (of `game_ids`, or all) whose log files have all been deleted.

    Rows for deleted copies of a game that still has another file are dropped, so a
    duplicate export removed from the folder leaves the game alone.
    """
    sql = "SELECT path, game_id FROM log_files"
    args: list[str] = []
    if game_ids is not None:
        if not game_ids:
            return {}
        sql += f" WHERE game_id IN ({','.join('?' * len(game_ids))})"
        args = game_ids
    by_game: dict[str, list[str]] = {}
    for r in conn.execute(sql, args).fetchall():
        by_game.setdefault(r[1], []).append(r[0])

    removed: dict[str, int] = {}
    for gid, paths in by_game.items():
        gone = [p for p in paths if _gone(p)]
        if not gone:
            continue
        if len(gone) == len(paths):
            removed[gid] = delete_game(conn, gid)
            log.info("log file deleted: removed game %s (%d hands)", gid, removed[gid])
        else:
            with writing(conn):
                conn.executemany("DELETE FROM log_files WHERE path = ?", [(p,) for p in gone])
    return removed


def import_file(conn: sqlite3.Connection, path: Path) -> dict:
    """Import one log-folder file and record it. Rebuilds only when something changed.

    `import_csv` always rebuilds; a sync reads every file once on a database that
    predates `log_files`, and nearly all of those are already in it.
    """
    gid = game_id_from_filename(path) or path.stem
    offered, n_new = ingest_entries(conn, gid, read_csv(path), source=f"csv:{path.name}")
    known = conn.execute("SELECT 1 FROM games WHERE game_id = ?", (gid,)).fetchone()
    summary = {"game_id": gid, "file": path.name, "entries_offered": offered, "entries_new": n_new}
    if n_new or not known:
        summary |= rebuild_game(conn, gid)
    record_file(conn, path, gid)
    return summary


def sync_folder(
    conn: sqlite3.Connection, folder: Path, skip: dict[str, tuple[int, int]] | None = None
) -> SyncResult:
    """Delete games whose files are gone, then import files that are new or changed.

    `skip` maps a path to the (mtime_ns, size) it failed to read at; that exact file
    is not retried, so a poller does not log the same broken CSV every few seconds.
    Failures are added to it.
    """
    out = SyncResult(removed=prune(conn))
    if not folder.is_dir():
        return out
    seen = {
        r[0]: (r[1], r[2]) for r in conn.execute("SELECT path, mtime_ns, size FROM log_files")
    }
    for path in sorted(folder.glob(LOG_GLOB)):
        try:
            st = path.stat()
        except OSError:
            continue  # deleted between the listing and now; the next sync prunes it
        key, stamp = _key(path), (st.st_mtime_ns, st.st_size)
        if seen.get(key) == stamp or (skip is not None and skip.get(key) == stamp):
            continue
        try:
            out.imported.append(import_file(conn, path))
        except (OSError, ValueError, csv.Error) as exc:  # locked, half-written, not a log
            out.failed[str(path)] = str(exc)
            if skip is not None:
                skip[key] = stamp
    return out


def untracked_games(conn: sqlite3.Connection) -> list[str]:
    """Games no log-folder file is on record for, so no deletion can reach them."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT game_id FROM raw_entries GROUP BY game_id"
            " HAVING game_id NOT IN (SELECT game_id FROM log_files) ORDER BY game_id"
        )
    ]
