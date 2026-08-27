"""The project's success criterion, as a test.

> After a session, re-importing the authoritative log changes no stat by more
> than rounding.

Here it is stronger than that: re-importing changes no stat *at all*, because
`raw_entries` dedupes on a globally unique key and the derived layer is a pure
function of it.
"""

from __future__ import annotations

from pnt.db.conn import connect
from pnt.ingest.importer import import_csv, rebuild_game
from pnt.stats.queries import report
from tests.conftest import ALL_LOGS, HU, HU_GAME


def test_reimport_changes_nothing(db):
    before = report(db)
    for path in ALL_LOGS:
        import_csv(db, path)
    assert report(db) == before


def test_reimport_inserts_no_new_raw_entries(db):
    n_before = db.execute("SELECT COUNT(*) FROM raw_entries").fetchone()[0]
    summary = import_csv(db, HU)
    assert summary["entries_new"] == 0, "a second import must be a pure no-op"
    assert db.execute("SELECT COUNT(*) FROM raw_entries").fetchone()[0] == n_before


def test_rebuild_is_a_pure_function_of_raw_entries(db):
    """Re-deriving without re-ingesting reproduces identical stats.

    This is what makes a parser fix safe: `pnt rebuild` replays history rather
    than patching it.
    """
    before = report(db)
    for gid in ("pgl41zM3_CKphpnKM1DMIosUT", "pgl1UViJ4BhoVP-KKHpux1Mpv"):
        rebuild_game(db, gid)
    assert report(db) == before


def test_partial_capture_then_full_import_converges(tmp_path):
    """Simulates live capture missing a chunk, then the log import repairing it.

    Ingest only the first 60% of a game's entries, derive stats, then ingest the
    whole log. The result must equal a clean single import -- which is the actual
    guarantee behind "live capture and log import cannot disagree".
    """
    from pnt.ingest.csv_source import read_csv
    from pnt.ingest.importer import ingest_entries

    entries = read_csv(HU)

    partial = connect(tmp_path / "partial.sqlite")
    cut = int(len(entries) * 0.6)
    ingest_entries(partial, HU_GAME, entries[:cut], source="fake-live")
    rebuild_game(partial, HU_GAME)
    assert report(partial)  # some stats exist, but they are incomplete
    ingest_entries(partial, HU_GAME, entries, source="csv")
    rebuild_game(partial, HU_GAME)

    clean = connect(tmp_path / "clean.sqlite")
    import_csv(clean, HU)

    assert report(partial) == report(clean)


def test_out_of_order_ingest_converges(tmp_path):
    """Entries arriving shuffled (as overlapping fetches will) change nothing:
    `order` re-sorts them and dedupes them."""
    import random

    from pnt.ingest.csv_source import read_csv
    from pnt.ingest.importer import ingest_entries

    entries = read_csv(HU)
    shuffled = entries[:]
    random.Random(0).shuffle(shuffled)

    a = connect(tmp_path / "a.sqlite")
    # deliberately overlapping, out-of-order batches
    ingest_entries(a, HU_GAME, shuffled[:1500], source="live")
    ingest_entries(a, HU_GAME, shuffled[1000:], source="live")
    rebuild_game(a, HU_GAME)

    b = connect(tmp_path / "b.sqlite")
    import_csv(b, HU)

    assert report(a) == report(b)
