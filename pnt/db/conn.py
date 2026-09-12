"""SQLite connection handling."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DB = Path("pokernow.sqlite")


def connect(path: str | Path = DEFAULT_DB, *, init: bool = True) -> sqlite3.Connection:
    """Open the tracker database, creating the schema if needed."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets the HUD read while an import writes -- the two consumers of this
    # file are meant to run at the same time.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    # Stated rather than inherited from sqlite3.connect's default, because it is
    # load-bearing: `writing()` relies on a blocked writer waiting here instead of
    # failing. A rebuild holds the lock for a fraction of a second, so this is slack.
    conn.execute("PRAGMA busy_timeout = 15000")
    if init:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        _migrate(conn)
        conn.commit()
    return conn


#: Columns added to layer-2 tables after the first release. `CREATE TABLE IF NOT
#: EXISTS` is a no-op on a database that already has the table, so a new column
#: reaches existing files only through an explicit ALTER. Layer-2 rows are
#: disposable -- `pnt rebuild` repopulates the column from the stored raw entries.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("hand_players", "bounty", "INTEGER NOT NULL DEFAULT 0"),
    # The parser has always worked this out -- a log that stops mid-hand leaves one
    # with half its chips recorded -- but it was never written down, so nothing
    # downstream could act on it and those hands were booked as a total loss for
    # everyone still in. Existing rows default to complete; `pnt rebuild` corrects
    # the handful that are not.
    ("hands", "complete", "INTEGER NOT NULL DEFAULT 1"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any missing columns. Idempotent, and safe on a fresh database."""
    for table, column, decl in _ADDED_COLUMNS:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if existing and column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


@contextmanager
def writing(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a write transaction, taking the write lock *before* reading anything.

    SQLite's default `BEGIN` is deferred: the transaction opens as a reader and
    asks for the write lock only at its first write. If another connection has
    committed in between, that upgrade can never be granted -- the snapshot it
    already read from is stale -- so SQLite returns `SQLITE_BUSY` **without
    calling the busy handler**, since no amount of waiting would help. That is why
    the failure arrives in 40 ms and `busy_timeout` looks like it does nothing.

    Reachable in ordinary use: two PokerNow tabs on the same table, or `pnt import`
    run while the background server is up. `BEGIN IMMEDIATE` takes the write lock
    up front, where `busy_timeout` does apply, and concurrent writers queue.

    Not reentrant: it owns the transaction it opens.
    """
    if conn.in_transaction:
        raise RuntimeError("writing() must own its transaction; it is already in one")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


#: Key in `meta` holding the derivation generation.
_GENERATION = "derivation"


def generation(conn: sqlite3.Connection) -> int | None:
    """How many times the derived view of this database has been invalidated.

    Zero on a database that has never been rebuilt. Cheap enough to read on every
    request -- it is a primary-key lookup in a one-row table.

    None when the answer is unknowable: a connection opened with `init=False`
    against a database predating the `meta` table has no counter to read, and
    guessing zero there would let two genuinely different states agree. Callers
    treat None as "do not cache", which is slow but never wrong.
    """
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (_GENERATION,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else 0


def bump_generation(conn: sqlite3.Connection) -> None:
    """Declare every previously derived fact stale. Call inside the writing() block
    that made it so, never after: a crash between the two would leave caches holding
    results for a database that had already moved on."""
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, 1)"
        " ON CONFLICT(key) DO UPDATE SET value = value + 1",
        (_GENERATION,),
    )


def path_of(conn: sqlite3.Connection) -> str:
    """The file this connection has open, or "" for an in-memory database.

    Caches are keyed on it because a process may hold connections to several
    databases -- the test suite does exactly that.
    """
    for _, name, file in conn.execute("PRAGMA database_list"):
        if name == "main":
            return file or ""
    return ""
