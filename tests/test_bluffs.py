"""Bluff:value by kind of aggression (`pnt/stats/bluffs.py`).

Every row must be the hands its filter finds, and every class within it the hands
its class filter finds, so a click on the chart reproduces the number it was on.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from pnt import cli
from pnt.server import app as server
from pnt.stats.bluffs import CATEGORY, CLASSES, bluff_value, members, pool_facts, ratio_text
from pnt.stats.derive import derive
from pnt.stats.filters import parse_filter
from pnt.stats.queries import display_names, facts_for, report
from tests.conftest import local_client
from tests.test_lines import _hand


def _players(db):
    return [row["player"] for row in report(db)]


def test_class_counts_sum_to_known(db):
    for facts in [facts_for(db, p) for p in _players(db)] + [pool_facts(db)]:
        for r in bluff_value(facts)["rows"]:
            assert sum(r["counts"].values()) == r["known"] <= r["n"], r["key"]
            assert sum(len(ids) for ids in r["class_ids"].values()) == r["known"]
            assert len(r["hand_ids"]) == r["n"]


def test_every_row_and_class_reproduces_as_a_filter(db):
    names = display_names(db)
    seen = set()
    for player in _players(db):
        facts = facts_for(db, player)
        for r in bluff_value(facts)["rows"]:
            if r["filter"] is None:
                continue  # all aggression: no filter can say "on any street"
            pred = parse_filter(r["filter"], names)
            assert sum(1 for f in facts if pred(f)) == r["n"], (player, r["key"])
            for cls in CLASSES:
                term = r["class_filters"][cls]
                if term is None:
                    assert r["counts"][cls] == 0, (player, r["key"], cls)  # river semi-bluffs
                    continue
                pred = parse_filter(term, names)
                assert sum(1 for f in facts if pred(f)) == r["counts"][cls], (player, r["key"], cls)
            if r["n"]:
                seen.add(r["kind"])
    assert {"bet", "raise", "check_raise", "jam", "all_bets"} <= seen, "the fixtures exercise every kind"


def test_roll_ups_add_up(db):
    for player in _players(db):
        rows = {r["key"]: r for r in bluff_value(facts_for(db, player))["rows"]}
        river = [rows[f"bet_river_{s}"] for s in ("blocker", "small", "medium", "large", "overbet")]
        assert rows["bet_river_all"]["n"] == sum(r["n"] for r in river)
        for cls in CLASSES:
            assert rows["bet_river_all"]["counts"][cls] == sum(r["counts"][cls] for r in river)
        # All aggression is every hand with a bet or raise on some street.
        facts = facts_for(db, player)
        aggressive = [f for f in facts if any(c in line for line in f.line.values() for c in "br")]
        assert rows["aggression"]["n"] == len(aggressive)
        assert rows["aggression"]["n"] >= max(rows[f"raise_{s}"]["n"] for s in ("flop", "turn", "river"))


def test_sizes_sit_above_the_actions(db):
    """Each street opens with one row per size, every bet or raise of it, and the
    bet, raise and check-raise of that size under it. A size row is the union of
    its children (a bet and a re-raise of one size on one street is one hand), and
    the sized actions add back up to the street's summaries."""
    for player in _players(db):
        rows = bluff_value(facts_for(db, player))["rows"]
        by_key = {r["key"]: r for r in rows}
        for street in ("flop", "turn", "river"):
            for size in ("blocker", "small", "medium", "large", "overbet"):
                parent = by_key[f"size_{street}_{size}"]
                kids = [r for r in rows if r["parent"] == parent["key"]]
                assert [r["kind"] for r in kids] == ["bet", "sized_raise", "sized_check_raise"]
                assert set(parent["hand_ids"]) == {i for r in kids for i in r["hand_ids"]}, (player, parent["key"])
            sized = {kind: sum(by_key[f"{kind}_{street}_{z}"]["n"]
                               for z in ("blocker", "small", "medium", "large", "overbet"))
                     for kind in ("raise", "check_raise")}
            assert sized["check_raise"] == by_key[f"check_raise_{street}"]["n"], (player, street)
            assert sized["raise"] + sized["check_raise"] == by_key[f"raise_{street}"]["n"], (player, street)


# --- synthetic hands -----------------------------------------------------------

#: Heads-up, limped, 20 in the middle. B bets 10 into a flop of Ah 7h 2c holding
#: Kh9h -- no pair, a flush draw -- and A calls with top pair. The turn checks
#: through, the river bricks, and B bets 20 into 40 with king high; A folds.
DRAW_THEN_AIR = _hand(
    ["A", "B"],
    [
        ("preflop", "A", "post", 5), ("preflop", "B", "post", 10),
        ("preflop", "A", "call", 5), ("preflop", "B", "check", 0),
        ("flop", "B", "bet", 10, 10), ("flop", "A", "call", 10, 10),
        ("turn", "B", "check", 0), ("turn", "A", "check", 0),
        ("river", "B", "bet", 20, 20), ("river", "A", "fold", 0),
    ],
    board=("Ah", "7h", "2c", "3s", "8d"),
    cards={"A": "AsKd", "B": "Kh9h"},
)


def test_a_flop_draw_bet_is_a_semi_bluff_and_a_river_air_bet_a_bluff():
    b = next(f for f in derive(DRAW_THEN_AIR) if f.pn_id == "B")
    rows = {r["key"]: r for r in bluff_value([b])["rows"]}
    assert rows["bet_flop_medium"]["counts"] == {"value": 0, "marginal": 0, "semi_bluff": 1, "bluff": 0}
    assert rows["bet_river_medium"]["counts"] == {"value": 0, "marginal": 0, "semi_bluff": 0, "bluff": 1}
    assert rows["bet_river_medium"]["folded"] == 1 and rows["bet_river_medium"]["folded_pct"] == 100.0
    assert rows["bet_flop_medium"]["folded"] == 0, "the flop bet was called"
    # Read on its last aggressive street, the river: a bluff, once.
    assert rows["aggression"]["n"] == 1 and rows["aggression"]["counts"]["bluff"] == 1
    assert rows["aggression"]["ratio_text"] == "0.0 : 1"
    assert members([b], "bet_flop_medium", "semi_bluff") == [b]
    # The caller made no bet or raise: in no row at all.
    a = next(f for f in derive(DRAW_THEN_AIR) if f.pn_id == "A")
    assert all(r["n"] == 0 for r in bluff_value([a])["rows"])


def test_unknown_cards_count_but_are_not_classified():
    b = next(f for f in derive(DRAW_THEN_AIR) if f.pn_id == "B")
    b.hole_cards = None
    row = next(r for r in bluff_value([b])["rows"] if r["key"] == "bet_river_medium")
    assert (row["n"], row["known"], row["coverage"], row["ratio_text"]) == (1, 0, 0.0, "--")


def test_ratio_text():
    assert ratio_text(21, 10) == "2.1 : 1"
    assert ratio_text(3, 0) == "∞ : 1"
    assert ratio_text(0, 4) == "0.0 : 1"
    assert ratio_text(0, 0) == "--"


# --- the pool, the endpoints and the CLI -----------------------------------------


def test_pool_is_the_sum_over_players(db, tmp_path, monkeypatch):
    players = _players(db)
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "t.sqlite")  # the `db` fixture's file
    client = local_client(server.app)
    pool = {r["key"]: r for r in client.get("/bluffs").json()["rows"]}
    per = [{r["key"]: r for r in client.get(f"/players/{p}/bluffs").json()["rows"]} for p in players]
    for key in ("bet_river_all", "raise_flop", "aggression"):
        assert pool[key]["n"] == sum(rows[key]["n"] for rows in per), key
        assert pool[key]["known"] == sum(rows[key]["known"] for rows in per), key
        for cls in CLASSES:
            assert pool[key]["counts"][cls] == sum(rows[key]["counts"][cls] for rows in per)
    assert pool["bet_river_all"]["n"] > 0
    assert "hand_ids" not in pool["aggression"], "a pool's hand ids name nobody"

    # The pool's hands behind a row, and behind one class of it, each named.
    body = client.get("/bluffs/hands", params={"category": "bet_river_all"}).json()
    assert body["total"] == pool["bet_river_all"]["n"] == len(body["hands"])
    assert set(players) >= {h["player"] for h in body["hands"]}
    body = client.get("/bluffs/hands", params={"category": "aggression", "cls": "value"}).json()
    assert body["total"] == pool["aggression"]["counts"]["value"]
    assert client.get("/bluffs/hands", params={"category": "nope"}).status_code == 400
    assert client.get("/bluffs/hands", params={"category": "aggression", "cls": "nope"}).status_code == 400
    assert client.get("/bluffs", params={"filter": "nope"}).status_code == 400
    assert client.get("/players/nobody/bluffs").status_code == 404

    # A spot narrows both scopes alike.
    srp = client.get("/bluffs", params={"filter": "srp"}).json()
    assert srp["filter"] == "srp" and srp["hands"] < client.get("/bluffs").json()["hands"]


def test_cli_prints_a_player_and_the_pool(db, tmp_path):
    path = tmp_path / "t.sqlite"  # the `db` fixture's file
    result = CliRunner().invoke(cli.app, ["bluffs", "genericpoker", "--pool", "--db", str(path)])
    assert result.exit_code == 0, result.output
    assert "genericpoker" in result.output and "pool" in result.output and "all aggression" in result.output
    result = CliRunner().invoke(cli.app, ["bluffs", "--json", "--db", str(path)])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert {r["key"] for r in body["rows"]} == set(CATEGORY)
