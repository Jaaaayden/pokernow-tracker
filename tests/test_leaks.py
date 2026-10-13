"""The hero's leak finder: every leak reproduces from its filter, no hero is an
empty answer, and the ranking is the same on every call. See SPEC.md, "Leak
finder"."""

from __future__ import annotations

import json
from itertools import pairwise

import pytest

from pnt.db.conn import connect
from pnt.ingest.importer import import_csv
from pnt.stats.filters import parse_filter
from pnt.stats.leaks import RATES, TOP, confidence, hero_of, leaks, leaks_for, rank, top_leaks
from pnt.stats.queries import display_names, facts_for, rates, report, tally, wilson
from tests.conftest import ALL_LOGS, HU_GAME, local_client


def _count(facts, terms, names):
    pred = parse_filter(terms, names)
    return sum(1 for f in facts if pred(f))


def test_the_fixtures_name_a_hero(db):
    hero = hero_of(db)
    assert hero is not None
    owned = db.execute("SELECT COUNT(*) FROM games WHERE hero_pn_id IS NOT NULL").fetchone()[0]
    assert owned, "the fixture logs carry their owner's hole cards"
    out = leaks(db)
    assert out["hero"] == hero
    assert out["hands"] == len(facts_for(db, hero)) > 0
    assert len(out["candidates"]) >= len(RATES)


def test_no_hero_is_an_empty_result(db):
    db.execute("UPDATE games SET hero_pn_id = NULL")
    db.commit()
    assert hero_of(db) is None
    out = leaks(db)
    assert out["hero"] is None
    assert out["top"] == out["ranked"] == out["candidates"] == []
    assert out["note"]


def test_every_leak_reproduces_from_its_filter(db):
    """A candidate's `filter` finds exactly its `n` over the player's hands, and its
    `leak_filter` exactly its `leak_n`. Held for every player, not only the hero,
    so stats on both sides of their bands are exercised."""
    names = display_names(db)
    checked = {"filter": 0, "leak_filter": 0}
    for player in [r["player"] for r in report(db)]:
        facts = facts_for(db, player)
        for r in leaks_for(facts)["candidates"]:
            assert _count(facts, r["filter"], names) == r["n"], (player, r["key"])
            checked["filter"] += 1
            if r["leak_filter"]:
                assert _count(facts, r["leak_filter"], names) == r["leak_n"], (player, r["key"])
                checked["leak_filter"] += 1
    assert checked["filter"] and checked["leak_filter"]


def test_rate_leaks_agree_with_the_stat_rows(db):
    """The same counts the stats page shows: a leak's value is `rates()`'s figure."""
    keys = {"wsd": "wsd", "cbet_flop": "cbet_flop", "fold_to_cbet_flop": "fold_to_cbet_flop"}
    for player in [r["player"] for r in report(db)]:
        facts = facts_for(db, player)
        row = rates(tally(facts))
        for r in leaks_for(facts)["candidates"]:
            if r["kind"] != "rate":
                continue
            assert r["value"] == row[keys.get(r["key"], r["key"])], (player, r["key"])


def test_fold_equity_leaks_are_the_defense_rows(db):
    from pnt.stats.fold_equity import fold_equity

    hero = hero_of(db)
    facts = facts_for(db, hero)
    fe = fold_equity(facts)
    rows = {f"faced_bet_{s}_{r['size']}": r for s, d in fe["streets"].items() for r in d["faced"]}
    for r in leaks_for(facts)["candidates"]:
        if r["kind"] != "fold_equity":
            continue
        src = rows[r["key"]]
        assert (r["n"], r["hits"], r["break_even"]) == (src["n"], src["folds"], src["break_even"])
        assert r["band"][0] <= r["break_even"] <= r["band"][1]


def test_the_ranking_is_stable(db):
    a, b = leaks(db), leaks(db)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    ranked = a["ranked"]
    assert all(r["side"] and r["judged"] for r in ranked)
    scores = [r["score"] for r in ranked]
    assert scores == sorted(scores, reverse=True)
    # Equal scores keep the candidates' order.
    order = {r["key"]: i for i, r in enumerate(a["candidates"])}
    for x, y in pairwise(ranked):
        if x["score"] == y["score"]:
            assert order[x["key"]] < order[y["key"]]


def test_ties_keep_candidate_order():
    def row(key, score):
        return {"key": key, "family": key, "label": key, "side": "high", "judged": True, "score": score}

    rows = [row("a", 1.0), row("b", 2.0), row("c", 1.0), row("d", 0.0), {**row("e", 9.0), "judged": False}]
    assert [r["key"] for r in rank(rows)] == ["b", "a", "c", "d"]
    assert [r["key"] for r in top_leaks(rank(rows))] == ["b", "a", "c"]


def test_top_is_one_per_habit_and_never_free():
    def row(key, family, score):
        return {"key": key, "family": family, "label": key, "side": "high", "judged": True, "score": score}

    ranked = rank([row("r1", "river", 5.0), row("r2", "river", 4.0), row("v", "vpip", 3.0),
                   row("w", "wtsd", 2.0), row("x", "x", 1.0), row("z", "z", 0.0)])
    top = top_leaks(ranked)
    assert [r["key"] for r in top] == ["r1", "v", "w"] and len(top) == TOP
    assert top[0]["also"] == ["r2"]
    assert all(r["score"] > 0 for r in top)


def test_confidence_is_the_share_of_the_interval_outside_the_band():
    assert confidence((0.6, 0.7), (30, 55)) == 1.0
    assert confidence((0.4, 0.5), (30, 55)) == 0.0
    assert confidence((0.5, 0.6), (30, 55)) == 0.5
    assert confidence((0.1, 0.4), (30, 55)) == pytest.approx(2 / 3, abs=1e-3)
    assert confidence(None, (30, 55)) == 0.0
    # More hands at the same rate are surer.
    small, big = wilson(6, 10), wilson(60, 100)
    assert confidence(small, (30, 55)) < confidence(big, (30, 55))


def test_a_judged_leak_needs_its_sample(db):
    hero = hero_of(db)
    for r in leaks(db)["candidates"]:
        assert r["judged"] == (r["n"] >= r["min_n"])
        if not r["judged"]:
            assert r["score"] == 0.0
        if not r["side"]:
            assert r["leak_filter"] is None and r["cost_bb_per_100"] is None
    assert hero


def test_one_game(db):
    hero = hero_of(db, HU_GAME)
    out = leaks(db, HU_GAME)
    assert out["game"] == HU_GAME and out["hero"] == hero
    if hero:
        assert out["hands"] == len(facts_for(db, hero, HU_GAME))


def test_leaks_endpoint(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import importlib

    monkeypatch.setenv("PNT_DB", str(tmp_path / "api.sqlite"))
    from pnt.server import app as app_module

    importlib.reload(app_module)
    conn = connect(tmp_path / "api.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    expect = leaks(conn)
    conn.execute("UPDATE games SET hero_pn_id = NULL WHERE game_id != ?", (HU_GAME,))
    conn.commit()
    one = leaks(conn)
    conn.close()
    client = local_client(app_module.app)

    body = client.get("/leaks").json()
    assert body["hero"] == one["hero"]
    assert [r["key"] for r in body["ranked"]] == [r["key"] for r in one["ranked"]]
    assert expect["hero"]

    conn = connect(tmp_path / "api.sqlite")
    conn.execute("UPDATE games SET hero_pn_id = NULL")
    conn.commit()
    conn.close()
    from pnt.stats.queries import clear_caches

    clear_caches()
    r = client.get("/leaks")
    assert r.status_code == 200 and r.json()["hero"] is None and r.json()["top"] == []


def test_leaks_cli(db, tmp_path):
    from typer.testing import CliRunner

    from pnt.cli import app

    path = tmp_path / "t.sqlite"
    runner = CliRunner()
    res = runner.invoke(app, ["leaks", "--db", str(path)])
    assert res.exit_code == 0, res.output
    assert hero_of(db) in res.output
    res = runner.invoke(app, ["leaks", "--db", str(path), "--json"])
    assert json.loads(res.output)["hero"] == hero_of(db)
