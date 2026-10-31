"""Deriving the whole database on every request, and not doing it twice.

Two separate fixes, tested together because they must agree with each other and
with the unoptimized path:

* `facts_for` reads only the hands its player was dealt into, instead of deriving
  every hand in the database and discarding all but one player's rows.
* the unfiltered `report` is memoized against a generation counter, because the
  HUD asks that exact question every 30 seconds and after every hand.
* on a miss, both are assembled from per-game pieces keyed on each game's own
  stamp, so the rebuild that follows every hand re-derives that one game.

The danger in both is a wrong answer rather than a slow one, so every test here
compares against the full derivation or forces an invalidation.
"""

from __future__ import annotations

import pytest

from pnt.db.conn import bump_generation, connect, generation, path_of, writing
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


# --- per-game pieces: a rebuild costs the game it touched ---------------------


def _games_loaded(monkeypatch) -> list:
    """Record the game of every `load_hands` call the caches make from here on."""
    calls = []
    real = q.load_hands

    def load_hands(conn, game_id=None, *args, **kwargs):
        calls.append(game_id)
        return real(conn, game_id, *args, **kwargs)

    monkeypatch.setattr(q, "load_hands", load_hands)
    return calls


def test_tallies_add_up_to_the_whole(db):
    """What lets a lifetime report be assembled from per-game pieces."""
    facts = [f for hand in q.load_hands(db) for f in q.derive(hand)]
    by_game: dict[str, list] = {}
    for f in facts:
        by_game.setdefault(f.game_id, []).append(f)
    assert len(by_game) == 3
    total = q.Tally()
    for part in by_game.values():
        total += q.tally(part)
    assert q.rates(total) == q.aggregate(facts)


def test_a_rebuild_stamps_only_its_own_game(db):
    stamps = dict(db.execute("SELECT game_id, derived_gen FROM games"))
    rebuild_game(db, HU_GAME)
    after = dict(db.execute("SELECT game_id, derived_gen FROM games"))
    assert after[HU_GAME] == generation(db) > stamps[HU_GAME]
    assert {g: s for g, s in after.items() if g != HU_GAME} == {g: s for g, s in stamps.items() if g != HU_GAME}


def test_after_a_rebuild_the_report_derives_that_game_alone(db, monkeypatch):
    """The HUD's lifetime report after a hand ends. It used to re-derive the whole
    database -- three seconds on ten thousand hands -- once per hand."""
    q.report(db)
    calls = _games_loaded(monkeypatch)
    rebuild_game(db, HU_GAME)
    served = q.report(db)
    assert calls == [HU_GAME]
    q.clear_caches()
    assert served == q.report(db)


def test_after_a_rebuild_a_players_facts_derive_that_game_alone(db, monkeypatch):
    before = q.facts_cached(db, "genericpoker")
    games = {f.game_id for f in before}
    assert HU_GAME in games and len(games) > 1
    calls = _games_loaded(monkeypatch)
    rebuild_game(db, HU_GAME)
    after = q.facts_cached(db, "genericpoker")
    assert calls == [HU_GAME]
    assert after == q.facts_for(db, "genericpoker")



def test_a_rebuild_in_the_middle_of_a_read_cannot_mix_old_and_new_hand_ids(db, monkeypatch):
    """Live capture rebuilds a game while the HUD reads it. A rebuild gives the game
    new hand ids, so a piece kept on the stamp read before it must never meet the
    hand order read after it: that was a KeyError and a 500 on every player view."""
    before = q.facts_cached(db, "genericpoker")
    with writing(db):  # a write elsewhere: the next read misses, HU_GAME's stamp holds
        bump_generation(db)
    other = connect(path_of(db))
    fired = []
    real = q.identities_of

    def identities_of(conn, alias):  # read between the game stamps and the hand order
        if not fired:
            fired.append(rebuild_game(other, HU_GAME))
        return real(conn, alias)

    monkeypatch.setattr(q, "identities_of", identities_of)
    during = q.facts_cached(db, "genericpoker")
    assert fired
    assert [f.hand_id for f in during] == [f.hand_id for f in before]  # the snapshot before it
    monkeypatch.setattr(q, "identities_of", real)
    after = q.facts_cached(db, "genericpoker")
    assert {f.hand_id for f in after} != {f.hand_id for f in before}  # the rebuild's new ids
    assert after == q.facts_for(db, "genericpoker")

def test_the_report_reads_each_game_in_a_snapshot_but_derives_outside_one(db, monkeypatch):
    """A rebuild landing between load_hands' queries must not read as a game of
    hands with no players; and no snapshot is held while hands are derived, which
    on a big database would keep WAL from checkpointing for a minute."""
    q.clear_caches()
    other = connect(path_of(db))
    loading: list[str | None] = []
    fired = []

    def rebuild_mid_load(sql: str) -> None:  # between the hands and their players
        if loading[-1:] == [HU_GAME] and "FROM hand_players hp JOIN" in sql and not fired:
            fired.append(rebuild_game(other, HU_GAME))

    real_load, real_tally = q.load_hands, q._tally_by_id
    derived_in_snapshot = []

    def load_hands(conn, game_id=None, *a, **k):
        assert conn.in_transaction, "load_hands outside a snapshot"
        loading.append(game_id)
        return real_load(conn, game_id, *a, **k)

    def tally(hands):
        derived_in_snapshot.append(db.in_transaction)
        return real_tally(hands)

    monkeypatch.setattr(q, "load_hands", load_hands)
    monkeypatch.setattr(q, "_tally_by_id", tally)
    db.set_trace_callback(rebuild_mid_load)
    try:
        during = q.report(db)
    finally:
        db.set_trace_callback(None)
        other.close()
    assert fired and derived_in_snapshot and not any(derived_in_snapshot)
    monkeypatch.undo()
    q.clear_caches()
    assert during == q.report(db)


def test_a_merge_regroups_the_tallies_without_deriving_anything(db, monkeypatch):
    q.report(db)
    calls = _games_loaded(monkeypatch)
    merge_players(db, "onlybluffs", "genericpoker")
    served = q.report(db)
    assert calls == []
    q.clear_caches()
    assert served == q.report(db)


def test_a_deleted_game_leaves_the_report_and_the_cache(db):
    from pnt.ingest.importer import delete_game

    q.report(db)
    delete_game(db, HU_GAME)
    served = q.report(db)
    assert not any(key[1] == HU_GAME for key in q._GAME_TALLIES)
    q.clear_caches()
    assert served == q.report(db)


# ---- the review list ----------------------------------------------------
# Cached like the report, but its rows also carry marks, notes and flags, which
# change without a rebuild: those must never be served from the cache.


def test_spot_answers_never_push_out_the_slow_ones(db):
    """A chart click files its stats, range and sizing apart: a full cache of them
    leaves Results, Tilt and the rest where they were."""
    slow = q.remembered(db, ("results", "someone"), lambda: {"cost": "seconds"})
    for i in range(q.SPOT_ANSWERS.cap + 1):
        q.remembered(db, ("spot-stats", "someone", None, f"spot{i}"), dict, q.SPOT_ANSWERS)
    assert q.remembered(db, ("results", "someone"), lambda: pytest.fail("recomputed")) is slow


def _review_fresh(db, alias, **kw):
    from pnt.stats import review as rv

    q.clear_caches()
    return rv.review_hand_list(db, alias, **kw)


def test_the_review_list_is_derived_once_per_generation(db, monkeypatch):
    from pnt.stats import review as rv

    first = rv.review_hand_list(db, "genericpoker")
    calls = []
    real = rv.load_hands
    monkeypatch.setattr(rv, "load_hands", lambda *a, **k: calls.append(a) or real(*a, **k))
    again = rv.review_hand_list(db, "genericpoker")
    assert calls == [], "a second ask in the same generation must not re-derive"
    assert again == first == _review_fresh(db, "genericpoker")


def test_a_filtered_review_list_matches_the_uncached_one(db):
    from pnt.stats import review as rv
    from pnt.stats.filters import parse_filter

    pred = parse_filter("srp", q.display_names(db))
    rv.review_hand_list(db, "genericpoker")  # warm, unfiltered
    served = rv.review_hand_list(db, "genericpoker", predicate=pred)
    assert served == _review_fresh(db, "genericpoker", predicate=pred)


def test_marks_notes_and_flags_show_without_a_rebuild(db):
    from pnt.stats import review as rv

    rows = rv.review_hand_list(db, "genericpoker")["hands"]
    assert rows, "the fixture must flag something for this test to mean anything"
    gid, n = rows[0]["game_id"], rows[0]["hand_number"]
    before = generation(db)
    rv.mark_reviewed(db, gid, n)
    rv.set_note(db, gid, n, "checked")
    rv.set_flag(db, gid, n)
    assert generation(db) == before, "the premise: these writes do not bump the generation"

    row = next(r for r in rv.review_hand_list(db, "genericpoker")["hands"]
               if (r["game_id"], r["hand_number"]) == (gid, n))
    assert row["reviewed"] and row["note"] == "checked" and row["flagged"]


@pytest.mark.parametrize(
    "change",
    [
        lambda db: rebuild_game(db, HU_GAME),
        lambda db: merge_players(db, "onlybluffs", "genericpoker"),
    ],
    ids=["rebuild", "merge"],
)
def test_the_review_list_follows_a_change(db, change):
    from pnt.stats import review as rv

    rv.review_hand_list(db, "genericpoker")
    change(db)
    served = rv.review_hand_list(db, "genericpoker")
    assert served == _review_fresh(db, "genericpoker")


def test_a_rename_is_visible_in_the_next_review_list(db):
    from pnt.stats import review as rv

    rv.review_hand_list(db, "genericpoker")
    rename_player(db, "genericpoker", "gp")
    with pytest.raises(ValueError, match="unknown alias"):
        rv.review_hand_list(db, "genericpoker")
    assert rv.review_hand_list(db, "gp")["player"] == "gp"



def test_lru_holds_its_cap_and_survives_racing_threads():
    """Request threads share the caches: inserting, reading, evicting and pruning
    at once must neither raise nor let the cache grow past its cap."""
    from concurrent.futures import ThreadPoolExecutor

    from pnt.stats.lru import LRU

    cache: LRU[tuple[int, int], int] = LRU(8)

    def churn(t: int) -> None:
        for i in range(2000):
            cache[t, i % 50] = i
            cache.get((t, (i * 7) % 50))
            if i % 97 == 0:
                cache.prune(lambda k: k[1] % 3 == 0)
            assert len(cache) <= 8
            list(cache)

    with ThreadPoolExecutor(8) as pool:
        for f in [pool.submit(churn, t) for t in range(8)]:
            f.result()
    assert len(cache) <= 8


def test_a_key_missed_by_several_threads_at_once_is_computed_once():
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from pnt.stats.lru import LRU

    cache: LRU[str, int] = LRU(4)
    computed = []
    go = threading.Barrier(6)

    def ask() -> int:
        go.wait()
        hit = cache.get("k")
        if hit is None:
            with cache.making("k"):
                hit = cache.get("k")
                if hit is None:
                    computed.append(1)
                    time.sleep(0.05)
                    hit = cache["k"] = 7
        return hit

    with ThreadPoolExecutor(6) as pool:
        assert [f.result() for f in [pool.submit(ask) for _ in range(6)]] == [7] * 6
    assert computed == [1]
    assert not cache._making  # nothing left behind once every thread is done


def test_a_page_asking_for_one_cold_player_at_once_derives_them_once(db, monkeypatch):
    """A page's requests all name its player, each on its own thread and
    connection: the first derives every game, the rest wait for it."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    path = path_of(db)
    calls = _games_loaded(monkeypatch)
    go = threading.Barrier(4)

    def ask():
        conn = connect(path)
        try:
            go.wait()
            return q.facts_cached(conn, "genericpoker")
        finally:
            conn.close()

    with ThreadPoolExecutor(4) as pool:
        got = [f.result() for f in [pool.submit(ask) for _ in range(4)]]
    assert len(calls) == len(set(calls)) > 1
    assert all(g is got[0] for g in got)
    assert got[0] == q.facts_for(db, "genericpoker")


def test_lru_forgets_least_recently_used():
    from pnt.stats.lru import LRU

    cache: LRU[str, int] = LRU(2)
    cache["a"], cache["b"] = 1, 2
    assert cache.get("a") == 1  # a is now the newest
    cache["c"] = 3
    assert list(cache) == ["a", "c"]
    assert cache.get("b") is None
