"""Pool-relative tags (`pnt.stats.pool`, `tags._verdict`): SPEC.md, "Pool baseline".

A tag fires on a player who stands out from the other players at the table, not
on one who crosses a number fixed for every pool. Synthetic players make each
case exact: six folding to c-bets 45% of the time, one at 75%, one at 52%, and
one at 75% on too few hands to say.
"""

from __future__ import annotations

from collections import Counter

from pnt.stats import tags
from pnt.stats.derive import Facts
from pnt.stats.pool import Pool, pool_for, pool_from_facts
from pnt.stats.tags import POOL_MIN_PLAYERS, baseline, counts, tags_for


def _player(name: str, n: int, folds: int, games: tuple[tuple[str, int, int], ...] | None = None) -> list[Facts]:
    """`n` flop c-bets faced, `folds` of them folded; or per game (game, n, folds)."""
    games = games or (("g1", n, folds),)
    out = []
    i = 0
    for game, gn, gf in games:
        for k in range(gn):
            i += 1
            out.append(Facts(
                hand_id=hash((name, i)) & 0xFFFFFF, pn_id=name, n_dealt_in=6, seats_from_button=0,
                dead_button=False, game_id=game, hand_number=i,
                fold_to_cbet_opp={"flop": True}, fold_to_cbet={"flop": k < gf},
            ))
    return out


def _table(extra: dict[str, list[Facts]] | None = None, typical: int = 6) -> dict[str, list[Facts]]:
    t = {f"typical{i}": _player(f"typical{i}", 60, 27) for i in range(typical)}
    t["outlier"] = _player("outlier", 60, 45)
    t["near"] = _player("near", 60, 31)
    t["thin"] = _player("thin", 12, 9)
    t.update(extra or {})
    return t


def _ids(out: dict) -> set[str]:
    return {t["id"] for t in out["tags"]}


def test_only_the_clear_outlier_fires():
    table = _table()
    pool = pool_from_facts(table)
    out = tags_for(table["outlier"], pool, "outlier")
    (tag,) = [t for t in out["tags"] if t["id"] == "folds_to_cbet"]
    assert tag["basis"] == "pool" and tag["pool_pct"] == 45.0 and "pool 45%" in tag["tip"]
    assert tag["ci"][0] > 45
    assert "folds_to_cbet" not in _ids(tags_for(table["near"], pool, "near")), "52% is inside the pool's noise"
    assert "folds_to_cbet" not in _ids(tags_for(table["thin"], pool, "thin")), "12 hands say nothing"
    assert out["pool"] == {"scope": "spot", "players": len(table)}


def test_a_small_pool_falls_back_to_the_fixed_cut():
    table = {k: v for k, v in _table().items() if k in ("typical0", "outlier", "near")}
    pool = pool_from_facts(table)
    assert "folds_to_cbet" not in pool.baseline("near"), f"under {POOL_MIN_PLAYERS} players there is no baseline"
    near = tags_for(table["near"], pool, "near")
    (tag,) = [t for t in near["tags"] if t["id"] == "folds_to_cbet"]
    assert tag["basis"] == "fixed", "52% clears the fixed 50% cut"
    assert [t["id"] for t in near["tags"]] == [t["id"] for t in tags_for(table["near"])["tags"]]


def test_the_judged_player_and_volume_do_not_move_the_line():
    table = _table({"grinder": _player("grinder", 600, 180)})  # 30%, ten times the hands
    pool = pool_from_facts(table)
    with_outlier = pool.baseline(None)["folds_to_cbet"].pct
    without = pool.baseline("outlier")["folds_to_cbet"].pct
    assert without == 45.0, "the median of the others, each player once"
    assert with_outlier == 45.0 and pool.baseline("grinder")["folds_to_cbet"].pct == 45.0


def test_a_one_session_tag_is_streaky_against_the_same_line():
    streaky = _player("streaky", 0, 0, games=(("a", 40, 36), ("b", 10, 3), ("c", 10, 3)))  # 70% in all
    table = _table({"streaky": streaky})
    pool = pool_from_facts(table)
    out = tags_for(streaky, pool, "streaky")
    assert "folds_to_cbet" not in _ids(out)
    (s,) = [t for t in out["streaky"] if t["id"] == "folds_to_cbet"]
    assert s["carried_by"]["game_id"] == "a"


def test_own_logs_stay_out_of_the_baseline_and_decide_fixed():
    # Seven players' shown river aggression: 20% air each, plus one log owner
    # whose own log (every card shown) makes up most of their sample.
    players = {f"p{i}": Counter({"no_bluff.n": 40, "no_bluff.hits": 8}) for i in range(6)}
    players["owner"] = Counter({"no_bluff.n": 100, "no_bluff.hits": 50, "no_bluff.own.n": 95, "no_bluff.own.hits": 48})
    base = baseline(players)
    assert base["no_bluff"].pct == 20.0 and base["no_bluff"].players == 6, "the owner's 5 others are too few"
    m = {"hits": 50, "n": 100, "pct": 50.0, "own_n": 95}
    assert tags._verdict("bluffs_river", m, True, base)["basis"] == "fixed"


def test_counts_split_own_rows_and_still_add():
    rows = _player("x", 30, 10)
    a, b = counts(rows[:10]), counts(rows[10:])
    assert a + b == counts(rows)


def test_pool_for_is_remembered_until_the_generation_moves(db):
    from pnt.db.conn import bump_generation, writing

    first = pool_for(db)
    assert isinstance(first, Pool) and first.players
    assert pool_for(db) is first
    with writing(db):
        bump_generation(db)
    again = pool_for(db)
    assert again is not first and again.players == first.players


def test_between_runs_before_each_game_derived_and_not_on_a_hit(db):
    from pnt.db.conn import bump_generation, writing
    from pnt.stats import pool

    pool.clear()
    calls = []
    pool_for(db, between=lambda: calls.append(1))
    games = db.execute("SELECT COUNT(DISTINCT game_id) FROM hands").fetchone()[0]
    assert len(calls) == games > 0
    with writing(db):
        bump_generation(db)  # the pool is counted again, from games whose stamps held
    calls.clear()
    pool_for(db, between=lambda: calls.append(1))
    assert calls == []


def test_the_api_says_how_each_tag_was_judged(tmp_path, monkeypatch):
    import importlib

    import pytest

    pytest.importorskip("fastapi")
    from conftest import ALL_LOGS, HU_GAME, local_client

    from pnt.db.conn import connect
    from pnt.ingest.importer import import_csv

    monkeypatch.setenv("PNT_DB", str(tmp_path / "api.sqlite"))
    from pnt.server import app as app_module

    importlib.reload(app_module)
    conn = connect(tmp_path / "api.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    conn.close()
    client = local_client(app_module.app)
    seats = client.get(f"/hud/{HU_GAME}").json()["seats"]
    exploits = [t for s in seats for t in s["tags"]["tags"] if t["kind"] == "exploit"]
    assert all(t["basis"] in ("pool", "fixed") for t in exploits)
    body = client.get("/players/genericpoker/tags").json()
    assert body["pool"] and body["pool"]["players"] >= 2
    spot = client.get("/players/genericpoker/tags", params={"filter": "srp"}).json()
    assert spot["pool"]["scope"] == "spot: srp"
    assert "table" in client.get("/tendencies").json()
