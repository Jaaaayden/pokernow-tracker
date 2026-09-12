"""Concurrent writers must queue, not fail.

Two PokerNow tabs on the same table, or `pnt import` run while the background
server is up, put two writers on one SQLite file at once. Under a *deferred*
transaction that reads before it writes, the second one does not wait for
`busy_timeout` -- it fails instantly with "database is locked", because a stale
snapshot cannot be rescued by waiting. `writing()` takes the write lock up front
so the busy handler applies.

No data was ever lost to this (ingest dedupes and the extension's pager resumes),
but it surfaced as an HTTP 500 and a stalled capture, which is indistinguishable
from a real fault to anyone running this.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from pnt.db.conn import connect, writing
from pnt.ingest.importer import ingest_entries

WRITERS = 12


def _ingest_once(path):
    conn = connect(path, init=False)
    try:
        ingest_entries(conn, "g", [], source="probe")
        return None
    except sqlite3.OperationalError as exc:  # pragma: no cover -- the regression
        return str(exc)
    finally:
        conn.close()


def test_concurrent_ingests_all_succeed(tmp_path):
    path = tmp_path / "t.sqlite"
    connect(path).close()  # create the schema once, up front

    with ThreadPoolExecutor(WRITERS) as pool:
        failures = [e for e in pool.map(lambda _: _ingest_once(path), range(WRITERS)) if e]

    assert not failures, f"{len(failures)}/{WRITERS} concurrent writes failed: {failures[0]}"

    conn = connect(path, init=False)
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == WRITERS
    conn.close()


def test_writing_rolls_back_on_failure(tmp_path):
    """A raise inside the block leaves nothing behind -- and releases the lock."""
    conn = connect(tmp_path / "t.sqlite")
    with pytest.raises(ValueError), writing(conn):
        conn.execute(
            "INSERT INTO imports (game_id, source, ingested_at, n_entries, n_new)"
            " VALUES ('g', 'probe', '2026-01-01', 0, 0)"
        )
        raise ValueError("boom")

    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 0
    assert not conn.in_transaction, "the failed transaction must not stay open"
    conn.close()


def test_writing_owns_its_transaction(tmp_path):
    """Nesting is refused loudly rather than committing half a caller's work."""
    conn = connect(tmp_path / "t.sqlite")
    # Kept nested on purpose: the inner `writing` is the thing under test, and
    # flattening these into one statement hides which one is expected to raise.
    with writing(conn):  # noqa: SIM117
        with pytest.raises(RuntimeError, match="must own its transaction"), writing(conn):
            pass
    conn.close()
