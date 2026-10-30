"""Keep the database in step with the log folder, in both directions.

A file new to the folder (or changed since it was last read) is imported; a game
whose every file on record is gone is deleted. `log_files` is the record: a game
only becomes subject to deletion once a file for it has been seen in the folder,
so games imported from elsewhere -- a Downloads copy, the bundled sample corpus --
never vanish because some other folder lacks them.

Deletion is whole files only. Ingest is append-only, so lines cut out of a CSV
that stays in the folder stay in the database too. To drop lines, move the file
out (the game goes with it at the next sync), then put the edited copy back.

A log with no hand in it -- a table joined and left before the first deal -- is not
imported: it would be a game of nothing. `remove_empty` clears out the ones already
in the folder.

Two guards against deleting what is merely out of reach: a file whose *folder* is
missing counts as unavailable, not deleted (an unplugged drive, a renamed folder),
and nothing here ever touches a game that has no `log_files` row. A database switched
to while the server runs has its missing files forgotten first (`forget_missing`).
"""

from __future__ import annotations

import csv
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ..db.conn import writing
from .csv_source import RawEntry, game_id_from_filename, has_hands, read_csv
from .importer import delete_game, ingest_entries, rebuild_game

LOG_GLOB = "poker_now_log_*.csv"

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    imported: list[dict] = field(default_factory=list)  # import summaries, one per file read
    removed: dict[str, int] = field(default_factory=dict)  # game_id -> hands removed
    failed: dict[str, str] = field(default_factory=dict)  # path -> why it could not be read
    empty: list[str] = field(default_factory=list)  # paths skipped for holding no hand


def _key(path: Path) -> str:
    return str(path.resolve())


def _stamp(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_mtime_ns, st.st_size


def record_file(
    conn: sqlite3.Connection, path: Path, game_id: str, stamp: tuple[int, int] | None = None
) -> None:
    """Note that `path` holds `game_id`, as it is on disk right now -- or as it was
    at `stamp` (mtime_ns, size), taken before the file was read. A caller that read
    it passes that: a write landing between the read and now then shows as a change,
    and the next sync reads the file again instead of taking it as imported."""
    mtime_ns, size = stamp or _stamp(path)
    with writing(conn):
        conn.execute(
            "INSERT INTO log_files (path, game_id, mtime_ns, size) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(path) DO UPDATE SET game_id = excluded.game_id,"
            " mtime_ns = excluded.mtime_ns, size = excluded.size",
            (_key(path), game_id, mtime_ns, size),
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


def forget_missing(conn: sqlite3.Connection) -> int:
    """Drop the record of every file `prune` would count as deleted, keeping the games.

    For a database arriving from elsewhere -- switched to on the front page, perhaps a
    copy from another PC. Its files were on that machine; if this one has the same
    folder without them, the next sync would read every game it lacks as deleted. The
    games stay, as if imported from elsewhere; a file that turns up later is recorded
    again. Returns how many records were dropped.
    """
    gone = [(r[0],) for r in conn.execute("SELECT path FROM log_files").fetchall() if _gone(r[0])]
    if gone:
        with writing(conn):
            conn.executemany("DELETE FROM log_files WHERE path = ?", gone)
    return len(gone)


def import_file(
    conn: sqlite3.Connection,
    path: Path,
    entries: list[RawEntry] | None = None,
    stamp: tuple[int, int] | None = None,
) -> dict:
    """Import one log-folder file and record it. Rebuilds only when something changed.

    `import_csv` always rebuilds; a sync reads every file once on a database that
    predates `log_files`, and nearly all of those are already in it. `entries` is
    the file already read, when the caller has it, and `stamp` its (mtime_ns, size)
    from before that read.

    The game is the one the file's name says, a browser's ` (1)` aside, so a second
    download of an export adds nothing. A name that holds no game ID is refused
    (ValueError): sync's glob lets `poker_now_log_anything.csv` through.
    """
    gid = game_id_from_filename(path)
    if gid is None:
        raise ValueError(f"{path.name}: no PokerNow game ID in the file name")
    if entries is None:
        stamp = _stamp(path)
        entries = read_csv(path)
    offered, n_new = ingest_entries(conn, gid, entries, source=f"csv:{path.name}")
    known = conn.execute("SELECT 1 FROM games WHERE game_id = ?", (gid,)).fetchone()
    summary = {"game_id": gid, "file": path.name, "entries_offered": offered, "entries_new": n_new}
    if n_new or not known:
        summary |= rebuild_game(conn, gid)
    record_file(conn, path, gid, stamp)
    return summary


def sync_folder(
    conn: sqlite3.Connection, folder: Path, skip: dict[str, tuple[int, int]] | None = None
) -> SyncResult:
    """Delete games whose files are gone, then import files that are new or changed.

    `skip` maps a path to the (mtime_ns, size) it failed to read at; that exact file
    is not retried, so a poller does not log the same broken CSV every few seconds.
    Failures are added to it, and so are logs with no hand in them: read again only
    once they change.
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
            entries = read_csv(path)
            if not has_hands(entries):
                out.empty.append(str(path))
                if skip is not None:
                    skip[key] = stamp
                continue
            out.imported.append(import_file(conn, path, entries, stamp))
        except sqlite3.Error:
            raise  # the database's trouble, not this file's: the next pass tries again
        except Exception as exc:  # locked, half-written, not a log, a line the parser trips on
            if not isinstance(exc, OSError | ValueError | csv.Error):
                log.exception("could not import %s", path)
            # One bad file must not hold back the ones after it.
            out.failed[str(path)] = str(exc) or type(exc).__name__
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


def empty_logs(folder: Path) -> list[Path]:
    """Logs in `folder` with no hand in them. Unreadable files are left out."""
    out = []
    for path in sorted(folder.glob(LOG_GLOB)):
        try:
            if not has_hands(read_csv(path)):
                out.append(path)
        except (OSError, ValueError, csv.Error):
            continue
    return out


def remove_empty(conn: sqlite3.Connection, folder: Path) -> list[Path]:
    """Delete the logs in `folder` with no hand in them, and their games if handless.

    Only a game with no hands goes with its file. One that has hands from somewhere
    else -- a fuller copy imported from another folder -- is left alone, with just
    the empty file's record dropped. Returns the files deleted.
    """
    removed = []
    for path in empty_logs(folder):
        gid = game_id_from_filename(path) or path.stem
        try:
            path.unlink()
        except OSError as exc:  # open elsewhere, read-only
            log.warning("could not delete empty log %s: %s", path, exc)
            continue
        removed.append(path)
        hands = conn.execute("SELECT COUNT(*) FROM hands WHERE game_id = ?", (gid,)).fetchone()[0]
        if hands == 0:
            delete_game(conn, gid)
        else:
            with writing(conn):
                conn.execute("DELETE FROM log_files WHERE path = ?", (_key(path),))
    return removed
