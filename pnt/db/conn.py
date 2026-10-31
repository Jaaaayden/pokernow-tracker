"""SQLite connection handling."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DB = Path("pokernow.sqlite")


def connect(path: str | Path = DEFAULT_DB, *, init: bool = True) -> sqlite3.Connection:
    """Open the tracker database, creating the schema if needed.

    A file that will not open as one (not a database, corrupt) raises
    sqlite3.DatabaseError with the connection closed, so the file can be deleted:
    Windows refuses while any handle is left open on it."""
    conn = sqlite3.connect(str(path))
    try:
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
            _merge_copy_games(conn)
            conn.commit()
    except BaseException:
        conn.close()
        raise
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
    # Per-game derivation stamp, so caches re-derive only the game a rebuild touched.
    # Zero on existing games until their next rebuild, which is safe: the caches it
    # keys live in one process, and every rebuild made from here on stamps.
    ("games", "derived_gen", "INTEGER NOT NULL DEFAULT 0"),
    # The exception to "disposable": a player's note is typed by you, not parsed,
    # and nothing rebuilds it. The column is still safe to add this way -- it
    # starts empty everywhere, which is exactly what an older database held, since
    # `players.notes` existed from the start but nothing wrote to it.
    ("players", "notes_at", "TEXT"),
    # Timing tells (SPEC.md, "Timing tells"): when each action and each board line
    # was logged, and the table's decision clock. NULL on every row imported before
    # the columns existed -- not timed, never zero seconds -- until `pnt rebuild`
    # re-parses the stored raw entries, which carry every line's time.
    ("actions", "ord", "INTEGER"),
    ("hands", "board_ords", "TEXT"),  # JSON {street: ord} of the run-one board lines
    ("hands", "clock_ms", "INTEGER"),  # decision limit plus a full time bank, in ms
    # Signed Nit Game penalty. Zero on existing rows until `pnt rebuild` re-parses.
    ("hand_players", "nit", "INTEGER NOT NULL DEFAULT 0"),
    # The parser version that stored the game (parser.PARSE_VERSION). Zero on
    # existing games, so `pnt rebuild --if-stale` re-parses each once.
    ("games", "parsed_with", "INTEGER NOT NULL DEFAULT 0"),
    # The game per hand: a table can switch to PLO mid-session, and only Hold'em
    # hands are analysed. NULL until `pnt rebuild`, read as Hold'em.
    ("hands", "variant", "TEXT"),
    # Double Board, told apart from run-it-twice. 0 until `pnt rebuild`.
    ("hands", "double_board", "INTEGER NOT NULL DEFAULT 0"),
)


def _missing_columns(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    out = []
    for table, column, decl in _ADDED_COLUMNS:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if existing and column not in existing:
            out.append((table, column, decl))
    return out


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any missing columns, and bring indexes and `imports` up to date.
    Idempotent, and safe on a fresh database.

    Two connections can migrate at once -- the server's sync and warm-up threads
    both open one at start, as can `pnt import` beside the server -- so the check
    and the ALTER run under the write lock: the second waits, then finds the
    columns there. The usual call has nothing missing and takes no lock.
    """
    if _missing_columns(conn):
        with writing(conn):
            for table, column, decl in _missing_columns(conn):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    _tidy_indexes(conn)


#: One `imports` row per (game, source), which `ingest_entries` updates in place.
IMPORTS_INDEX = "idx_imports_game_source"

#: An index schema.sql no longer makes: the UNIQUE (game_id, hand_number) index
#: starts with game_id and serves every lookup by game, so this one only cost
#: each rebuild a second index to write.
DROPPED_INDEX = "idx_hands_game"


def _index_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
    ).fetchone() is not None


def _indexes_tidy(conn: sqlite3.Connection) -> bool:
    return _index_exists(conn, IMPORTS_INDEX) and not _index_exists(conn, DROPPED_INDEX)


def _tidy_indexes(conn: sqlite3.Connection) -> None:
    """Fold `imports` to one row per (game, source) and index the pair, and drop
    DROPPED_INDEX. Once per database, and only reads when there is nothing to do.

    A read-only file is left as it is rather than refused: neither step changes
    what a reader sees, and the next connection that can write makes them.
    """
    if _indexes_tidy(conn):
        return
    try:
        with writing(conn):
            if _indexes_tidy(conn):
                return  # another connection got here first
            _one_import_row_per_source(conn)
            conn.execute(f"DROP INDEX IF EXISTS {DROPPED_INDEX}")
    except sqlite3.OperationalError as exc:
        if not (exc.sqlite_errorname or "").startswith("SQLITE_READONLY"):
            raise


def _one_import_row_per_source(conn: sqlite3.Connection) -> None:
    """Fold `imports` down to one row per (game, source), then index that pair.
    Inside the caller's write transaction.

    It used to gain a row on every extension poll, empty ones included: 79k rows
    on a few months' play, which nothing reads back. Each source's latest row
    stays, holding the latest call's time and offer and the new lines the source
    has added in all. The unique index cannot be built while two rows share a pair.
    """
    if _index_exists(conn, IMPORTS_INDEX):
        return
    shared = conn.execute(
        "SELECT 1 FROM imports GROUP BY game_id, source HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()
    if shared:
        conn.execute(
            "CREATE TEMP TABLE _imports_kept (import_id INTEGER PRIMARY KEY, n_new INTEGER)"
        )
        conn.execute(
            "INSERT INTO _imports_kept SELECT MAX(import_id), SUM(n_new)"
            " FROM imports GROUP BY game_id, source"
        )
        conn.execute("DELETE FROM imports WHERE import_id NOT IN (SELECT import_id FROM _imports_kept)")
        conn.execute(
            "UPDATE imports SET n_new ="
            " (SELECT k.n_new FROM _imports_kept k WHERE k.import_id = imports.import_id)"
        )
        conn.execute("DROP TABLE _imports_kept")
    conn.execute(f"CREATE UNIQUE INDEX {IMPORTS_INDEX} ON imports(game_id, source)")


#: Key in `meta` set once a database's browser-copy games are merged
#: (`_merge_copy_games`), so the scan runs once per file, not on every connect.
COPY_GAMES_KEY = "copy_games_merged"

#: A game stored under a browser's copy name: `X (1)` (Chrome, Edge) or `X(1)`
#: (Firefox), from `poker_now_log_X (1).csv` read before csv_source dropped the
#: copy number. No real game ID has a space or a bracket in it, so none matches.
_COPY_GAME_ID = re.compile(r"(?P<base>.+?) ?\(\d+\)")

#: The tables that name a game, apart from raw_entries, games and hands, which
#: `_merge_copy_game` moves by hand. The user tables (layer-2 judgement) are kept
#: on the base game; the rest are bookkeeping that follows it.
_HAND_KEYED_USER_TABLES = ("hand_reviews", "hand_flags", "hand_tags")


def _copy_games(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(copy ID, base ID) of every game stored under a browser's copy name."""
    from ..ingest.csv_source import GAME_ID  # lazy: ingest imports this module

    ids: set[str] = set()
    for table in ("raw_entries", "games", "log_files", "imports", "parse_misses",
                  "hand_notes", *_HAND_KEYED_USER_TABLES):
        ids.update(r[0] for r in conn.execute(f"SELECT DISTINCT game_id FROM {table}"))
    out = []
    for gid in sorted(ids):
        m = _COPY_GAME_ID.fullmatch(gid)
        if m and GAME_ID.fullmatch(m.group("base")):
            out.append((gid, m.group("base")))
    return out


def _merge_copy_game(conn: sqlite3.Connection, copy: str, base: str) -> None:
    """Fold the game stored as `copy` into `base`, inside the caller's transaction.

    Raw lines are keyed on (game_id, ord), so lines both hold collapse into one.
    The base game's hands stay as they are until `pnt rebuild --if-stale` re-derives
    them from the merged lines: it is marked stale, never rebuilt here, since a
    parse is far too slow to run inside `connect`.
    """
    conn.execute(
        "INSERT OR IGNORE INTO raw_entries (game_id, ord, at, entry)"
        " SELECT ?, ord, at, entry FROM raw_entries WHERE game_id = ?",
        (base, copy),
    )
    conn.execute("DELETE FROM raw_entries WHERE game_id = ?", (copy,))
    if conn.execute("SELECT 1 FROM games WHERE game_id = ?", (base,)).fetchone():
        # Hands first: hand_players, actions and voluntary_shows cascade from them.
        conn.execute("DELETE FROM hands WHERE game_id = ?", (copy,))
        conn.execute("DELETE FROM games WHERE game_id = ?", (copy,))
    elif conn.execute("SELECT 1 FROM games WHERE game_id = ?", (copy,)).fetchone():
        # Only the copy was ever derived: it becomes the base game. hands reference
        # games(game_id) with no ON UPDATE, so the new row goes in before they move.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(games)") if r[1] != "game_id"]
        conn.execute(
            f"INSERT INTO games (game_id, {', '.join(cols)})"
            f" SELECT ?, {', '.join(cols)} FROM games WHERE game_id = ?",
            (base, copy),
        )
        conn.execute("UPDATE hands SET game_id = ? WHERE game_id = ?", (base, copy))
        conn.execute("DELETE FROM games WHERE game_id = ?", (copy,))
    conn.execute(
        "UPDATE OR IGNORE parse_misses SET game_id = ? WHERE game_id = ?", (base, copy)
    )
    conn.execute("DELETE FROM parse_misses WHERE game_id = ?", (copy,))
    # The copy's file is a file of the base game: a sync that finds it unchanged
    # skips it, and deleting it counts toward deleting the base game.
    conn.execute("UPDATE log_files SET game_id = ? WHERE game_id = ?", (base, copy))
    # One row per (game, source): where the base already has the source, it stands.
    conn.execute("UPDATE OR IGNORE imports SET game_id = ? WHERE game_id = ?", (base, copy))
    conn.execute("DELETE FROM imports WHERE game_id = ?", (copy,))
    # What you marked, flagged and tagged on the copy's hands: the same hands, by
    # the log's own numbers. Where the base has the same mark, it stands.
    for table in _HAND_KEYED_USER_TABLES:
        conn.execute(f"UPDATE OR IGNORE {table} SET game_id = ? WHERE game_id = ?", (base, copy))
        conn.execute(f"DELETE FROM {table} WHERE game_id = ?", (copy,))
    # A note is typed text: where both hold a different one, keep both.
    conn.execute(
        "UPDATE hand_notes SET"
        " note = note || char(10) || char(10) || (SELECT c.note FROM hand_notes c"
        "   WHERE c.game_id = ? AND c.hand_number = hand_notes.hand_number),"
        " noted_at = MAX(noted_at, (SELECT c.noted_at FROM hand_notes c"
        "   WHERE c.game_id = ? AND c.hand_number = hand_notes.hand_number))"
        " WHERE game_id = ? AND hand_number IN (SELECT c.hand_number FROM hand_notes c"
        "   WHERE c.game_id = ? AND c.note <> hand_notes.note)",
        (copy, copy, base, copy),
    )
    conn.execute("UPDATE OR IGNORE hand_notes SET game_id = ? WHERE game_id = ?", (base, copy))
    conn.execute("DELETE FROM hand_notes WHERE game_id = ?", (copy,))
    # Stale, so `pnt rebuild --if-stale` re-derives it from the merged lines.
    conn.execute("UPDATE games SET parsed_with = 0 WHERE game_id = ?", (base,))


def _merge_copy_games(conn: sqlite3.Connection) -> None:
    """Merge every game stored under a browser's copy name into the game it copies.

    Before csv_source read `poker_now_log_X (1).csv` as game X, it stored it as a
    game `X (1)` of its own, counting each hand twice. Nothing else would put it
    right: a sync skips the file, whose stamp has not changed. Once per database,
    marked in `meta`; the usual call is one primary-key lookup.
    """
    done = "SELECT 1 FROM meta WHERE key = ?"
    if conn.execute(done, (COPY_GAMES_KEY,)).fetchone():
        return
    with writing(conn):
        if conn.execute(done, (COPY_GAMES_KEY,)).fetchone():
            return  # another connection got here first
        copies = _copy_games(conn)
        for copy, base in copies:
            _merge_copy_game(conn, copy, base)
        if copies:
            bump_generation(conn)
        conn.execute("INSERT INTO meta (key, value) VALUES (?, 1)", (COPY_GAMES_KEY,))


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


@contextmanager
def reading(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run several reads against one snapshot of the database.

    Outside a transaction every SELECT sees the latest commit, so a reader that
    asks two questions can get answers from either side of a write: the game
    stamps from before a rebuild and the hand ids from after it. A deferred
    `BEGIN` pins the snapshot at the first read under WAL, without blocking the
    writers. Inside a transaction already, the reads share it, so this does nothing.
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.rollback()  # read-only: nothing to keep


#: Key in `meta` holding the derivation generation.
GENERATION_KEY = "derivation"


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
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (GENERATION_KEY,)).fetchone()
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
        (GENERATION_KEY,),
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
