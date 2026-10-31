"""schema.sql is the whole schema; `_migrate` only brings an older file up to it."""

from __future__ import annotations

import sqlite3

from pnt.db.conn import _ADDED_COLUMNS, SCHEMA_PATH, _migrate, _missing_columns


def _fresh(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def _columns(conn: sqlite3.Connection) -> dict[str, set[str]]:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    return {t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})")} for t in tables}


def test_schema_sql_alone_creates_every_column(tmp_path):
    """A new database needs no ALTER: every migrated column is in schema.sql."""
    assert _missing_columns(_fresh(tmp_path / "new.sqlite")) == []


def test_a_migrated_database_has_the_columns_of_a_fresh_one(tmp_path):
    fresh = _fresh(tmp_path / "new.sqlite")
    old = _fresh(tmp_path / "old.sqlite")
    # Back to the first release's tables. Rebuilt rather than ALTER ... DROP COLUMN,
    # which SQLite refuses on a table whose definition ends in comments; the views
    # go first and come back with the schema, as `connect` runs it on an old file.
    for (view,) in old.execute("SELECT name FROM sqlite_master WHERE type = 'view'").fetchall():
        old.execute(f"DROP VIEW {view}")
    for table in {t for t, _, _ in _ADDED_COLUMNS}:
        added = {c for t, c, _ in _ADDED_COLUMNS if t == table}
        kept = [c for c in _columns(old)[table] if c not in added]
        old.execute(f"CREATE TABLE {table}_v1 AS SELECT {', '.join(kept)} FROM {table}")
        old.execute(f"DROP TABLE {table}")
        old.execute(f"ALTER TABLE {table}_v1 RENAME TO {table}")
    old.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert len(_missing_columns(old)) == len(_ADDED_COLUMNS)
    _migrate(old)
    assert _missing_columns(old) == []
    assert _columns(old) == _columns(fresh)
