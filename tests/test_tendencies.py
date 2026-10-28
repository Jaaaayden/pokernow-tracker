"""Tendencies: the hero's own tags, and the tags shared across their regular
opponents, each opening its hands by its own filter."""

from __future__ import annotations

import pytest

from pnt.db.conn import connect
from pnt.ingest.importer import import_csv
from pnt.stats.filters import parse_filter
from pnt.stats.leaks import hero_of
from pnt.stats.queries import display_names, facts_for, report
from pnt.stats.tags import tags_for
from pnt.stats.tendencies import advice, tendencies
from tests.conftest import ALL_LOGS, local_client


def test_advice_drops_the_figures():
    assert advice("Folded to a river bet 53.7% of the time (907 of 1689). Bet the river.") == "Bet the river."
    assert advice("Loose. Wide. Value bet.") == "Wide. Value bet."
    assert advice("No figures") == "No figures"


def test_the_hero_gets_their_own_tags_and_never_counts_in_the_pool(db):
    out = tendencies(db, min_hands=1)
    hero = hero_of(db)
    assert out["hero"] == hero
    want = [t["id"] for t in tags_for(facts_for(db, hero))["tags"] if t["kind"] != "fun"]
    assert [t["id"] for t in out["you"]] == want
    assert all(p["player"] != hero for t in out["pool"] for p in t["players"])
    assert out["regulars"] == len([r for r in report(db) if r["player"] != hero])


def test_pool_tags_count_their_players_and_open_their_hands(db):
    out = tendencies(db, min_hands=1)
    names = display_names(db)
    counts = [t["count"] for t in out["pool"]]
    assert counts == sorted(counts, reverse=True)
    for t in out["pool"]:
        assert t["kind"] in ("archetype", "exploit") and t["advice"]
        assert t["count"] == len(t["players"]) <= out["regulars"]
        for p in t["players"]:
            # A tag's filter is a working spot that opens its hands (tags.py), not
            # always its exact n, so the check is that it finds some.
            pred = parse_filter(p["filter"], names)
            assert any(pred(f) for f in facts_for(db, p["player"])), (t["id"], p)


def test_min_hands_keeps_short_samples_out(db):
    assert tendencies(db, min_hands=10**9)["regulars"] == 0
    assert tendencies(db, min_hands=10**9)["pool"] == []


def test_no_hero_still_lists_the_pool(db):
    db.execute("UPDATE games SET hero_pn_id = NULL")
    db.commit()
    out = tendencies(db, min_hands=1)
    assert out["hero"] is None and out["you"] == []
    assert out["regulars"] == len(report(db))


def test_the_endpoint_and_page(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import importlib

    monkeypatch.setenv("PNT_DB", str(tmp_path / "api.sqlite"))
    from pnt.server import app as app_module

    importlib.reload(app_module)
    conn = connect(tmp_path / "api.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    expect = tendencies(conn)
    conn.close()
    client = local_client(app_module.app)
    body = client.get("/tendencies").json()
    assert body["hero"] == expect["hero"] and body["regulars"] == expect["regulars"]
    assert [t["id"] for t in body["you"]] == [t["id"] for t in expect["you"]]
    page = client.get("/tendencies.html")
    assert page.status_code == 200 and "/tendencies" in page.text


def test_one_player_gets_their_tags_and_every_measure(db):
    from pnt.stats.pool import pool_for
    from pnt.stats.tendencies import MEASURES, player_tendencies

    alias = report(db)[0]["player"]
    out = player_tendencies(db, alias, shared={"fish": 3})
    want = tags_for(facts_for(db, alias), pool_for(db, None), alias)
    assert out["player"] == alias and out["hands"] == want["profile"]["hands"]
    assert [t["id"] for t in out["tags"]] == [t["id"] for t in want["tags"] if t["kind"] != "fun"]
    assert all(t["advice"] and t["shared"] == (3 if t["id"] == "fish" else 0) for t in out["tags"])
    names = display_names(db)
    rows = [r for g in out["measures"] for r in g["rows"]]
    assert [r["id"] for r in rows] == [m[0] for _, ms in MEASURES for m in ms]
    for r in rows:
        assert r["n"] >= r["hits"] >= 0
        assert (r["pct"] is None) == (r["n"] == 0)
        parse_filter(r["filter"], names)  # every measure opens its hands on the chart


def test_an_unknown_player_raises(db):
    from pnt.stats.tendencies import player_tendencies

    with pytest.raises(ValueError):
        player_tendencies(db, "nobody-at-all")


def test_the_endpoint_adds_one_player(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import importlib

    monkeypatch.setenv("PNT_DB", str(tmp_path / "api.sqlite"))
    from pnt.server import app as app_module

    importlib.reload(app_module)
    conn = connect(tmp_path / "api.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    alias = report(conn)[0]["player"]
    conn.close()
    client = local_client(app_module.app)
    assert "player" not in client.get("/tendencies").json()
    body = client.get("/tendencies", params={"player": alias}).json()
    shared = {t["id"]: t["count"] for t in body["pool"]}
    assert body["player"]["player"] == alias
    assert all(t["shared"] == shared.get(t["id"], 0) for t in body["player"]["tags"])
    assert client.get("/tendencies", params={"player": "nobody-at-all"}).status_code == 404
