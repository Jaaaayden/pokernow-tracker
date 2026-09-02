"""SQLite connection handling."""

from __future__ import annotations

import sqlite3
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
)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any missing columns. Idempotent, and safe on a fresh database."""
    for table, column, decl in _ADDED_COLUMNS:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if existing and column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
