"""The action-line explorer: lines grouped, the strength behind each, and the filter
that finds each group again. See SPEC.md, "Strength by line".

Pinned to the heads-up fixture hands `test_lines.py` works by hand:

  #10  gp (4dTs) bets the flop and the turn on Qs3d6s5h; Chris folds the turn.
       gp b / b / -, read on the turn: an open-ender, a draw.
  #92  gp (Jd2h) check-raises the Jc9h7d flop, bets the 9c turn, bets the Qh river
       and calls Chris's raise: xr / b / bc, whole street XbRc / Bc / BrC. Chris
       (9dAs) made trips on the turn with one card to the paired board, which beat
       97% of holdings there: nutted, by the turn's hand-strength cut.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from pnt import cli
from pnt.db.conn import connect
from pnt.ingest.importer import import_csv
from pnt.stats.derive import POSTFLOP_STREETS, derive
from pnt.stats.filters import parse_filter
from pnt.stats.lines import (
    MODES,
    TIERS,
    line_breakdown,
    tier_at,
)
from pnt.stats.queries import display_names, facts_for, load_hands, pool_facts, report
from pnt.stats.strength import HS_CUTS
from tests.conftest import ALL_LOGS, EDGE_LOGS, HU_GAME, local_client

CHRIS = "5NARaPRkSp"
GP = "gpP9uUffpu"
SCOPES = (None, *POSTFLOP_STREETS)


@pytest.fixture()
def hu(db):
    """{hand_number: {pn_id: Facts}} for the heads-up game."""
    return {h.hand_number: {f.pn_id: f for f in derive(h)} for h in load_hands(db, HU_GAME)}


def _players(db):
    return [(row["player"], facts_for(db, row["player"])) for row in report(db)]


def _count(facts, expr, names):
    pred = parse_filter(expr, names)
    return sum(1 for f in facts if pred(f))


# --- the fixture hands ---------------------------------------------------------


def test_a_fixture_hands_line_and_strength(hu):
    gp = hu[92][GP]
    own = line_breakdown([gp], "own", min_n=1)["groups"]
    assert [g["line"] for g in own] == ["xr/b/bc"]
    assert own[0]["filter"] == "line=xr/b/bc" and own[0]["rated_on"] == "river"
    # Jd2h on Jc9h7d9cQh: middle pair of jacks, not two pair -- the nines are the board's.
    assert {t: v["n"] for t, v in own[0]["tiers"].items()} == {
        "nutted": 0, "strong": 0, "marginal": 1, "draw": 0, "air": 0,
    }

    whole = line_breakdown([gp], "action", min_n=1)["groups"]
    assert [g["line"] for g in whole] == ["XbRc/Bc/BrC"]
    assert whole[0]["filter"] == "action=XbRc/Bc/BrC"

    flop = line_breakdown([gp], "own", "flop", min_n=1)["groups"][0]
    assert flop["line"] == "xr" and flop["filter"] == "line_flop=xr" and flop["rated_on"] == "flop"

    # Folded on the turn: read on the turn, where the open-ender is a draw.
    g = line_breakdown([hu[10][GP]], "own", min_n=1)["groups"][0]
    assert g["line"] == "b/b/-" and g["rated_on"] == "turn"
    assert g["tiers"]["draw"]["n"] == 1 and g["draw_heavy"] == 100.0

    # Chris's cards are unknown on #10: shown 0, nothing to split.
    g = line_breakdown([hu[10][CHRIS]], "own", min_n=1)["groups"][0]
    assert g["line"] == "c/f/-" and g["shown"] == g["known"] == 0 and g["coverage"] == 0.0


def test_the_nutted_tier_is_hand_strength(hu):
    """Nutted is a hand of their own at or over the street's nutted cut, not a made
    hand's name: a middle set can fall short of it and trips can clear it."""
    assert HS_CUTS["flop"]["nutted"] >= HS_CUTS["flop"]["strong"]
    chris = hu[92][CHRIS]  # 9dAs on Jc9h7d9c: trips, 97% of holdings beaten
    assert tier_at(chris.hole_cards, chris.board, "turn") == "nutted"
    assert tier_at("Td8s", ("Jc", "9h", "7d"), "flop") == "nutted", "the nut straight"
    assert tier_at("9d9s", ("Jc", "9h", "7d"), "flop") == "strong", "middle set: JJ and T8 beat it"
    assert tier_at("JdTs", ("Jc", "9h", "7d"), "flop") == "strong", "top pair"
    assert tier_at("8d8s", ("Jc", "9h", "7d"), "flop") == "marginal", "an underpair"
    assert tier_at("AhKh", ("Qh", "9h", "2d"), "flop") == "draw"
    assert tier_at("AhKh", ("Qh", "9h", "2d", "3c", "4s"), "river") == "air", "a missed draw"
    assert tier_at(None, ("Qh", "9h", "2d"), "flop") is None
    assert tier_at("AhKh", ("Qh", "9h", "2d"), "turn") is None, "no turn was dealt"


# --- invariants over every player ----------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_groups_add_up_to_the_hands(db, mode):
    for _, facts in _players(db):
        for street in SCOPES:
            out = line_breakdown(facts, mode, street, min_n=1)
            assert out["hidden"] == {"groups": 0, "hands": 0}
            assert sum(g["n"] for g in out["groups"]) == out["hands"] == len(facts)
            assert sum(g["known"] for g in out["groups"]) == out["known"]
            assert sum(g["shown"] for g in out["groups"]) == out["shown"]
            # With the default minimum the small groups are hidden, and counted.
            out = line_breakdown(facts, mode, street)
            assert sum(g["n"] for g in out["groups"]) + out["hidden"]["hands"] == len(facts)
            assert all(g["n"] >= out["min"] for g in out["groups"])
            assert [g["n"] for g in out["groups"]] == sorted((g["n"] for g in out["groups"]), reverse=True)


@pytest.mark.parametrize("mode", MODES)
def test_tier_shares_add_up_to_the_known_hands(db, mode):
    for _, facts in _players(db):
        for street in SCOPES:
            for g in line_breakdown(facts, mode, street, min_n=1)["groups"]:
                assert sum(t["n"] for t in g["tiers"].values()) == g["known"] <= g["shown"] <= g["n"]
                assert list(g["tiers"]) == list(TIERS)
                if g["known"]:
                    assert sum(t["pct"] for t in g["tiers"].values()) == pytest.approx(100, abs=0.3)
                    assert g["draw_heavy"] == g["tiers"]["draw"]["pct"]
                else:
                    assert g["draw_heavy"] is None
                if g["rated_on"] == "river":
                    assert g["tiers"]["draw"]["n"] == 0, "a draw on the river is air"


@pytest.mark.parametrize("mode", MODES)
def test_every_group_reproduces_as_a_filter(db, mode):
    """The chart's click-through: each row's filter finds exactly its hands, and each
    tier's filter exactly that tier's -- inside the same spot as the rows."""
    names = display_names(db)
    for player, facts in _players(db):
        for spot in ("", "srp", "wtsd"):
            inside = [f for f in facts if parse_filter(spot, names)(f)] if spot else facts
            for street in SCOPES:
                for g in line_breakdown(inside, mode, street, min_n=1)["groups"]:
                    assert _count(inside, g["filter"], names) == g["n"], (player, spot, g["line"])
                    for tier, t in g["tiers"].items():
                        if t["filter"] is None:
                            assert t["n"] == 0, (player, g["line"], tier)
                        else:
                            assert _count(inside, t["filter"], names) == t["n"], (player, g["line"], tier)


def _walk(nodes):
    for g in nodes:
        yield g
        yield from _walk(g["children"])


@pytest.mark.parametrize("mode", MODES)
def test_the_tree_adds_up_and_reproduces(db, mode):
    """The grouped view: flop lines, the turn under each, the river under those.
    Every node's filter finds exactly its hands, and so does each tier's; a node's
    children and hidden ways on add up to it, unless the line ended there."""
    names = display_names(db)
    for player, facts in _players(db):
        for min_n in (1, 3):
            tree = line_breakdown(facts, mode, None, min_n=min_n)["tree"]
            top = sum(g["n"] for g in tree)
            assert top <= len(facts)
            if min_n == 1:
                assert top == len(facts), player
            for g in _walk(tree):
                assert _count(facts, g["filter"], names) == g["n"], (player, g["line"])
                for tier, t in g["tiers"].items():
                    if t["filter"] is not None:
                        assert _count(facts, t["filter"], names) == t["n"], (player, g["line"], tier)
                if g["children"] or g["hidden"]["groups"]:
                    assert sum(c["n"] for c in g["children"]) + g["hidden"]["hands"] == g["n"], g["line"]
            leaves = {g["line"]: g["n"] for g in _walk(tree) if len(g["streets"]) == 3}
            flat = {g["line"]: g["n"] for g in line_breakdown(facts, mode, None, min_n=1)["groups"]}
            assert all(flat[line] == n for line, n in leaves.items()), player


def test_the_tree_is_left_out_for_one_street(db):
    facts = facts_for(db, report(db)[0]["player"])
    assert line_breakdown(facts, "own", "flop")["tree"] is None


def test_the_pool_reproduces_too_runouts_included(db):
    """Every seat of every hand, edge logs too. The all-in run-outs (`.`, nobody
    acted) are the one segment with no letters to write, and must still match."""
    for path in EDGE_LOGS:
        import_csv(db, path)
    names = display_names(db)
    facts = pool_facts(db)
    assert len(facts) == sum(len(h.players) for h in load_hands(db))
    runouts = 0
    for street in SCOPES:
        out = line_breakdown(facts, "action", street, min_n=1)
        assert sum(g["n"] for g in out["groups"]) == len(facts)
        for g in out["groups"]:
            runouts += "." in g["streets"].values()
            assert _count(facts, g["filter"], names) == g["n"], g["line"]
    assert runouts, "the fixtures have all-ins with a run-out"


def test_bad_mode_or_street_is_refused():
    with pytest.raises(ValueError, match="mode"):
        line_breakdown([], "sideways")
    with pytest.raises(ValueError, match="street"):
        line_breakdown([], "own", "preflop")
    out = line_breakdown([])
    assert out["hands"] == 0 and out["groups"] == [] and out["coverage"] is None


# --- endpoints and CLI ---------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PNT_DB", str(tmp_path / "api.sqlite"))
    import importlib

    from pnt.server import app as app_module

    importlib.reload(app_module)
    conn = connect(tmp_path / "api.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    conn.close()
    return local_client(app_module.app)


def test_lines_endpoints(client):
    body = client.get("/players/genericpoker/lines", params={"filter": "srp"}).json()
    assert body["player"] == "genericpoker" and body["mode"] == "own" and body["streets"] == list(POSTFLOP_STREETS)
    assert sum(g["n"] for g in body["groups"]) + body["hidden"]["hands"] == body["hands"]
    g = body["groups"][0]
    assert {"line", "filter", "n", "known", "coverage", "tiers", "draw_heavy", "hand_ids", "net_bb"} <= set(g)
    hands = client.get("/players/genericpoker/hands", params={"filter": f"srp,{g['filter']}"}).json()["hands"]
    assert sorted(h["hand_id"] for h in hands) == sorted(g["hand_ids"])

    river = client.get("/players/genericpoker/lines", params={"mode": "action", "street": "river", "min": 1}).json()
    assert river["streets"] == ["river"] and river["hidden"]["groups"] == 0


    assert client.get("/players/ghost/lines").status_code == 404
    assert client.get("/players/genericpoker/lines", params={"filter": "nope"}).status_code == 400
    assert client.get("/players/genericpoker/lines", params={"mode": "sideways"}).status_code == 422
    assert client.get("/players/genericpoker/lines", params={"street": "preflop"}).status_code == 422
    assert client.get("/players/genericpoker/lines", params={"min": 0}).status_code == 422


def test_chart_has_a_lines_view(client):
    page = client.get("/chart").text
    assert 'data-view="lines"' in page and "/lines?" in page and "function renderLines" in page
    for chip in ("line=xc/xc/xc", "line=c/c/c", "line=b/b/b", "line=b/x/b", "line=x/b/", "line=xr*//", "action=xB*/xB*/"):
        assert chip in page, chip
    # Each chip is a filter the server takes.
    for chip in ("line=x/b/", "line=xr*//", "action=xB*/xB*/"):
        assert client.get("/players/genericpoker/lines", params={"filter": chip}).status_code == 200


def test_cli_prints_lines_and_json(tmp_path):
    path = tmp_path / "lines.sqlite"
    conn = connect(path)
    for log in ALL_LOGS:
        import_csv(conn, log)
    conn.close()
    result = CliRunner().invoke(cli.app, ["lines", "genericpoker", "--db", str(path)])
    assert result.exit_code == 0, result.output
    assert "nutted" in result.output and "line=" in result.output and "never shown" in result.output
    result = CliRunner().invoke(
        cli.app, ["lines", "genericpoker", "--mode", "action", "--filter", "srp", "--json", "--db", str(path)]
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["mode"] == "action" and all(g["filter"].startswith("action") for g in body["groups"])
    result = CliRunner().invoke(cli.app, ["lines", "genericpoker", "--mode", "sideways", "--db", str(path)])
    assert result.exit_code != 0
