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
from pnt.ingest.csv_source import RawEntry
from pnt.ingest.importer import ingest_entries

WRITERS = 12


def _ingest_once(path, i):
    conn = connect(path, init=False)
    try:
        ingest_entries(conn, "g", [RawEntry(i, "2026-01-01", f"line {i}")], source="probe")
        return None
    except sqlite3.OperationalError as exc:  # pragma: no cover -- the regression
        return str(exc)
    finally:
        conn.close()


def test_concurrent_ingests_all_succeed(tmp_path):
    path = tmp_path / "t.sqlite"
    connect(path).close()  # create the schema once, up front

    with ThreadPoolExecutor(WRITERS) as pool:
        failures = [e for e in pool.map(lambda i: _ingest_once(path, i), range(WRITERS)) if e]

    assert not failures, f"{len(failures)}/{WRITERS} concurrent writes failed: {failures[0]}"

    conn = connect(path, init=False)
    assert conn.execute("SELECT COUNT(*) FROM raw_entries").fetchone()[0] == WRITERS
    # One row for the source, and every writer's new line counted on it once.
    assert [tuple(r) for r in conn.execute("SELECT n_entries, n_new FROM imports")] == [(1, WRITERS)]
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


def test_imports_keeps_one_row_per_game_and_source(tmp_path):
    """Live capture ingests every few seconds; a row per call grew the table to
    79k rows that nothing reads. Re-ingesting updates the source's one row."""
    conn = connect(tmp_path / "t.sqlite")
    for i in range(3):
        ingest_entries(conn, "g", [RawEntry(i, "2026-01-01", f"line {i}")], source="extension")
        ingest_entries(conn, "g", [RawEntry(i, "2026-01-01", f"line {i}")], source="extension")
    ingest_entries(conn, "g", [RawEntry(9, "2026-01-01", "x")], source="csv:a.csv")
    rows = conn.execute("SELECT source, n_entries, n_new FROM imports ORDER BY source").fetchall()
    assert [tuple(r) for r in rows] == [("csv:a.csv", 1, 1), ("extension", 1, 3)]
    conn.close()


def test_an_older_database_has_its_imports_folded_and_the_extra_index_dropped(tmp_path):
    """One row per (game, source) left, keeping the latest call and the new lines
    summed; then the unique index. And `idx_hands_game`, which the UNIQUE
    (game_id, hand_number) index makes redundant, is dropped."""
    from pnt.db.conn import IMPORTS_INDEX

    path = tmp_path / "t.sqlite"
    connect(path).close()
    old = sqlite3.connect(path)
    old.execute(f"DROP INDEX {IMPORTS_INDEX}")
    old.executemany(
        "INSERT INTO imports (game_id, source, ingested_at, n_entries, n_new) VALUES (?, ?, ?, ?, ?)",
        [("g", "extension", f"2026-01-0{d}", d * 10, d) for d in (1, 2, 3)]
        + [("g", "csv:a.csv", "2026-02-01", 5, 5), ("h", "extension", "2026-03-01", 7, 0)],
    )
    old.execute("CREATE INDEX idx_hands_game ON hands(game_id)")
    old.commit()
    old.close()

    conn = connect(path)
    rows = conn.execute(
        "SELECT game_id, source, ingested_at, n_entries, n_new FROM imports ORDER BY game_id, source"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("g", "csv:a.csv", "2026-02-01", 5, 5),
        ("g", "extension", "2026-01-03", 30, 6),
        ("h", "extension", "2026-03-01", 7, 0),
    ]
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert IMPORTS_INDEX in indexes and "idx_hands_game" not in indexes
    plan = " ".join(
        r[3] for r in conn.execute("EXPLAIN QUERY PLAN SELECT hand_id FROM hands WHERE game_id = 'g'")
    )
    assert "sqlite_autoindex_hands_1" in plan
    conn.close()
