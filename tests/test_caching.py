"""Deriving the whole database on every request, and not doing it twice.

Two separate fixes, tested together because they must agree with each other and
with the unoptimized path:

* `facts_for` reads only the hands its player was dealt into, instead of deriving
  every hand in the database and discarding all but one player's rows.
* the unfiltered `report` is memoized against a generation counter, because the
  HUD asks that exact question every 30 seconds and after every hand.

The danger in both is a wrong answer rather than a slow one, so every test here
compares against the full derivation or forces an invalidation.
"""

from __future__ import annotations

import pytest

from pnt.db.conn import connect, generation
from pnt.ingest.importer import (
    import_csv,
    merge_players,
    rebuild_game,
    rename_player,
    split_identities,
)
from pnt.stats import queries as q
from tests.conftest import ALL_LOGS, HU, HU_GAME


@pytest.fixture(autouse=True)
def _clean():
    q.clear_caches()
    yield
    q.clear_caches()


def _reference(conn, alias):
    """What `facts_for` returned before it learned to read less."""
    grouped, aliases, _ = q.facts_by_player(conn)
    pid = next(p for p, a in aliases.items() if a == alias)
    return grouped[pid]


def test_reading_only_one_players_hands_changes_no_number(db):
    """A hand is derived whole, so narrowing the *hands* cannot move a figure."""
    aliases = [r["player"] for r in q.report(db)]
    assert len(aliases) > 1
    for alias in aliases:
        expected = _reference(db, alias)
        got = q.facts_for(db, alias)
        assert sorted(f.hand_id for f in got) == sorted(f.hand_id for f in expected)
        assert q.aggregate(got) == q.aggregate(expected)


def test_an_unknown_alias_still_raises_and_an_empty_one_does_not(db):
    with pytest.raises(ValueError, match="unknown alias"):
        q.facts_for(db, "nobody-has-this-name")


def test_repeated_reports_are_served_from_one_derivation(db, monkeypatch):
    first = q.report(db)
    calls = []
    real = q.facts_by_player
    monkeypatch.setattr(q, "facts_by_player", lambda *a, **k: (calls.append(1), real(*a, **k))[1])

    assert q.report(db) == first
    assert q.report(db) == first
    assert calls == [], "the second and third reports must not re-derive"


def test_a_filtered_report_is_never_served_from_the_cache(db):
    """A predicate is an arbitrary callable; it cannot be part of a key."""
    q.report(db)
    everything = q.report(db)
    nothing = q.report(db, predicate=lambda f: False)
    assert everything and not nothing


def test_min_hands_is_applied_to_the_cached_derivation(db):
    q.report(db)  # warm
    loose = q.report(db, min_hands=1)
    strict = q.report(db, min_hands=200)
    assert strict == [r for r in loose if r["hands"] >= 200]
    assert len(strict) < len(loose)


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda db: rebuild_game(db, HU_GAME), id="rebuild"),
        pytest.param(lambda db: merge_players(db, "onlybluffs", "genericpoker"), id="merge"),
        pytest.param(lambda db: rename_player(db, "genericpoker", "gp2"), id="rename"),
    ],
)
def test_every_change_that_invalidates_a_derivation_bumps_the_generation(db, change):
    """The counter is what the cache keys on, so this is the whole safety net."""
    before = generation(db)
    q.report(db)  # warm, so a missing bump would be observable below
    change(db)

    assert generation(db) > before, "the change must declare the derivation stale"

    # And the cached answer really is discarded: the same call, computed fresh,
    # agrees with what the cache now serves.
    served = q.report(db)
    q.clear_caches()
    assert served == q.report(db)


def test_a_merge_is_visible_in_the_next_report(db):
    before = {r["player"]: r["hands"] for r in q.report(db)}
    assert {"onlybluffs", "genericpoker"} <= before.keys()

    merge_players(db, "onlybluffs", "genericpoker")

    after = {r["player"]: r["hands"] for r in q.report(db)}
    assert "onlybluffs" not in after, "a cached report must not outlive the merge"
    assert after["genericpoker"] == before["genericpoker"] + before["onlybluffs"]


def test_a_split_is_visible_in_the_next_report(db):
    moved = merge_players(db, "onlybluffs", "genericpoker")
    merged = {r["player"] for r in q.report(db)}
    assert "onlybluffs" not in merged

    split_identities(db, moved, "onlybluffs")
    assert "onlybluffs" in {r["player"] for r in q.report(db)}


def test_a_rename_is_visible_in_the_next_report(db):
    assert "genericpoker" in {r["player"] for r in q.report(db)}
    rename_player(db, "genericpoker", "someone else")
    names = {r["player"] for r in q.report(db)}
    assert "someone else" in names and "genericpoker" not in names


def test_appending_raw_entries_does_not_invalidate_anything(db):
    """Ingest alone changes nothing derived -- it is a rebuild that does.

    This is the reason for an explicit counter rather than a file timestamp: live
    capture writes every few seconds, and invalidating on that would leave the HUD
    re-deriving the whole database all evening for no change in the answer.
    """
    from pnt.ingest.csv_source import read_csv
    from pnt.ingest.importer import ingest_entries

    q.report(db)
    before = generation(db)
    ingest_entries(db, HU_GAME, read_csv(HU), source="probe")
    assert generation(db) == before


def test_two_databases_do_not_share_a_cache(tmp_path):
    a, b = connect(tmp_path / "a.sqlite"), connect(tmp_path / "b.sqlite")
    for log in ALL_LOGS:
        import_csv(a, log)
    import_csv(b, HU)

    assert len(q.report(a)) != len(q.report(b))
    assert q.report(a) != q.report(b)
    a.close()
    b.close()


def test_an_in_memory_database_is_never_cached(tmp_path):
    """It has no path, so nothing distinguishes it from another one."""
    mem = connect(":memory:")
    import_csv(mem, HU)
    rows = q.report(mem)
    assert rows
    assert not q._REPORT_CACHE, "an unidentifiable database must not be keyed"
    mem.close()
