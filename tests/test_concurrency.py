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



def test_a_rebuild_that_read_first_cannot_commit_older_lines_last(tmp_path, monkeypatch):
    """Two tabs on one table: A reads half the game and parses slowly, while B adds
    the rest and rebuilds. A commits last, and once stored its half (157 hands of
    312 in the review's repro) until something rebuilt the game again."""
    import threading

    from conftest import HU

    from pnt.ingest import importer
    from pnt.ingest.csv_source import read_csv

    entries = read_csv(HU)
    half = len(entries) // 2
    path = tmp_path / "t.sqlite"
    connect(path).close()

    real_parse = importer.parse
    a_read, b_done = threading.Event(), threading.Event()

    def slow_parse(es, gid):
        if threading.current_thread().name == "A" and not b_done.is_set():
            a_read.set()  # A has read the raw lines; B overtakes it here
            assert b_done.wait(30)
        return real_parse(es, gid)

    monkeypatch.setattr(importer, "parse", slow_parse)

    def a():
        c = connect(path)
        importer.ingest_entries(c, "g", entries[:half], "a")
        importer.rebuild_game(c, "g")
        c.close()

    def b():
        assert a_read.wait(30)
        c = connect(path)
        importer.ingest_entries(c, "g", entries[half:], "b")
        importer.rebuild_game(c, "g")
        c.close()
        b_done.set()

    threads = [threading.Thread(target=a, name="A"), threading.Thread(target=b, name="B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    monkeypatch.setattr(importer, "parse", real_parse)

    conn = connect(path)
    raced = conn.execute("SELECT COUNT(*) FROM hands").fetchone()[0]
    importer.rebuild_game(conn, "g")
    assert raced == conn.execute("SELECT COUNT(*) FROM hands").fetchone()[0] == 188
    conn.close()


def test_rebuilding_a_game_with_no_lines_writes_nothing(tmp_path):
    from pnt.ingest import importer

    conn = connect(tmp_path / "t.sqlite")
    assert importer.rebuild_game(conn, "nosuchgame")["hands"] == 0
    assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0
    conn.close()


def test_migration_waits_for_one_already_under_way(tmp_path, monkeypatch):
    """The server's sync and warm-up threads both migrate at start. One that saw a
    column missing must wait for the other's ALTER and then find it there, not
    add it a second time ("duplicate column name") and kill its thread."""
    import threading

    from pnt.db import conn as dbconn

    path = tmp_path / "t.sqlite"
    connect(path).close()
    old = sqlite3.connect(path)
    old.execute("ALTER TABLE players DROP COLUMN notes_at")
    old.commit()
    old.close()

    # The winner holds the write lock with its ALTER made but not committed.
    winner = sqlite3.connect(path, isolation_level=None)
    winner.execute("BEGIN IMMEDIATE")
    winner.execute("ALTER TABLE players ADD COLUMN notes_at TEXT")

    queued = threading.Event()
    real_writing = dbconn.writing

    def writing_after_signal(c):
        queued.set()  # the loser has seen the column missing and asks for the lock
        return real_writing(c)

    monkeypatch.setattr(dbconn, "writing", writing_after_signal)
    with ThreadPoolExecutor(1) as pool:
        loser = pool.submit(lambda: connect(path).close())
        assert queued.wait(10)
        winner.execute("COMMIT")
        loser.result(timeout=30)
    winner.close()

    conn = connect(path, init=False)
    assert "notes_at" in {r[1] for r in conn.execute("PRAGMA table_info(players)")}
    conn.close()
