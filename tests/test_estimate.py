"""Estimated ranges and the board breakdown: the fill is the right size, a call is
capped, calibration moves only past its threshold, and a board's combos are the
ones counted by hand. See SPEC.md, "Estimated ranges"."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pnt.db.conn import connect
from pnt.ingest.importer import import_csv
from pnt.stats import estimate as est
from pnt.stats.cards import HAND_RANKING, board_texture
from pnt.stats.derive import Facts
from pnt.stats.queries import aggregate, facts_for, report
from pnt.stats.strength import hand_strength
from tests.conftest import ALL_LOGS, local_client

CHART = Path(__file__).resolve().parents[1] / "pnt" / "server" / "static" / "chart.html"


def _facts(level: int, decisions: list[str], shown: dict[int, str] | None = None, seat: int = 0) -> list[Facts]:
    """One hand per decision at `level`, the ones in `shown` with those cards;
    seat 0 is the button."""
    shown = shown or {}
    return [
        Facts(hand_id=i, pn_id="x", n_dealt_in=6, seats_from_button=seat, dead_button=False,
              pf_faced={level: d}, hole_cards=shown.get(i))
        for i, d in enumerate(decisions)
    ]


# ------------------------------------------------------------------ the fill --


@pytest.mark.parametrize("pct", [0, 0.3, 10, 33.3, 62, 100])
def test_the_fill_takes_exactly_that_share_of_combos(pct):
    cells = est.top_fill(pct)
    assert est.combo_count(cells) == pytest.approx(pct / 100 * est.COMBOS)
    # In ranking order, full, then one partial class at most.
    weights = [cells[k] for k in HAND_RANKING if k in cells]
    assert list(cells) == [k for k in HAND_RANKING if k in cells]
    assert all(w == 1 for w in weights[:-1])
    assert sum(0 < w < 1 for w in weights) <= 1


def test_combo_weights():
    assert (est.combos_of("AA"), est.combos_of("AKs"), est.combos_of("AKo")) == (6, 4, 12)
    assert sum(est.combos_of(k) for k in HAND_RANKING) == est.COMBOS


def test_a_capped_range_leaves_out_the_raise_slice():
    cells = est.capped_fill(30, 8)
    assert est.combo_count(cells) == pytest.approx(0.22 * est.COMBOS)
    for k, w in est.top_fill(8).items():
        if w == 1:
            assert k not in cells, k
    assert "AA" not in cells and "KK" not in cells
    assert all(0 < w <= 1 for w in cells.values())


# ------------------------------------------------------------------ the node --


def test_the_node_is_the_highest_decision_named():
    n = est.resolve_node("position=BTN,unopened=raise")
    assert (n.situation, n.decision, n.level, n.method) == ("unopened", "raise", 1, "linear")
    assert n.context == ["position=BTN"] and n.ignored == []
    assert n.filter == "position=BTN,unopened=raise" and n.spot_filter == "position=BTN,unopened"

    n = est.resolve_node("opener,faced_3bet=call,srp,flop=ace_high,hand_pct>=50")
    assert (n.situation, n.decision, n.method) == ("faced_3bet", "call", "capped")
    assert n.context == ["opener"]
    assert n.ignored == ["srp", "flop=ace_high", "hand_pct>=50"]

    assert est.resolve_node("steal").context == ["steal_opp"]
    assert est.resolve_node("called_open,open_bb>=4,eff_bb<40").context == ["open_bb>=4", "eff_bb<40"]
    # An open's own size says it raised: not settled before the decision.
    assert est.resolve_node("opener,open_bb>=4").ignored == ["open_bb>=4"]
    assert est.resolve_node("position=BTN|CO,limp").context == ["position=BTN|CO"]


@pytest.mark.parametrize("expr, says", [
    ("", "pick one preflop spot"),
    ("srp,flop=ace_high", "pick one preflop spot"),
    ("(opener|limp)", "pick one preflop spot"),
    ("faced_open=fold", "raise or a call"),
    ("faced_open=call,3bet", "two decisions"),
])
def test_no_usable_node(expr, says):
    with pytest.raises(est.NoNode, match=says):
        est.resolve_node(expr)


# -------------------------------------------------------------- the estimate --


def test_an_open_frequency_fills_its_share():
    facts = _facts(1, ["raise"] * 40 + ["fold"] * 60)
    out = est.estimate_range(facts, est.resolve_node("unopened=raise"))
    assert (out["n"], out["count"], out["pct"], out["method"]) == (100, 40, 40.0, "linear")
    assert out["combos"] == pytest.approx(0.4 * est.COMBOS, abs=0.1)
    lo, hi = out["ci"]
    assert lo < 40 < hi and out["pm"] == round((hi - lo) / 2)
    assert not out["calibrated"] and out["shown"] == 0


def test_a_call_vs_open_is_capped_by_the_3bets():
    facts = _facts(2, ["call"] * 20 + ["raise"] * 10 + ["fold"] * 70)
    out = est.estimate_range(facts, est.resolve_node("faced_open=call"))
    assert (out["pct"], out["top_pct"], out["slice_pct"], out["method"]) == (20.0, 30.0, 10.0, "capped")
    assert out["combos"] == pytest.approx(0.2 * est.COMBOS, abs=0.1)
    assert "AA" not in out["cells"]


def test_no_hands_at_the_spot():
    out = est.estimate_range(_facts(2, ["call"]), est.resolve_node("unopened=raise"))
    assert out["n"] == 0 and out["cells"] == {} and out["pct"] is None


def test_calibration_waits_for_enough_shown_hands():
    # A 20% open range; 72o is far outside it.
    decisions = ["raise"] * 20 + ["fold"] * 80
    four = {0: "7h2c", 1: "AsAd", 2: "KsKd", 3: "QsQd"}
    out = est.estimate_range(_facts(1, decisions, four), est.resolve_node("unopened=raise"))
    assert out["shown"] == 4 and not out["calibrated"]
    assert "72o" not in out["cells"]
    assert next(r for r in out["calibration"] if r["class"] == "72o") == {
        "class": "72o", "shown": 1, "outside": True, "family": None, "moved": []}
    assert out["outside"] == 1


def test_one_stray_show_does_not_pull_a_class_in():
    """A class shown once, far below how often it would show if it were always
    played, stays out however many hands were shown."""
    cells = est.top_fill(20)
    in_range = [k for k, w in cells.items() if w == 1]
    for spread in (1, 20):
        shown = est.Counter({k: spread for k in in_range}) + est.Counter({"72o": 1})
        out, _, _, _ = est.calibrate(dict(cells), shown)
        assert out.get("72o", 0) < 0.5
        assert est.combo_count(out) == pytest.approx(est.combo_count(cells))


def test_a_class_shown_as_often_as_the_range_moves_in():
    cells = est.top_fill(20)
    in_range = [k for k, w in cells.items() if w == 1]
    shown = est.Counter({k: 4 for k in in_range}) + est.Counter({"54s": 4, "72o": 12})
    out, rows, demoted, moved = est.calibrate(dict(cells), shown)
    assert moved and out["54s"] == 1 and out["72o"] == 1
    assert next(r for r in rows if r["class"] == "54s")["moved"] == ["54s"]
    assert est.combo_count(out) == pytest.approx(est.combo_count(cells))  # the range keeps its size
    assert demoted and not {"54s", "72o"} & {d["class"] for d in demoted}


def test_a_calling_range_takes_the_hands_actually_flatted():
    """The capped guess leaves the top out for the 3-bets; a player who flats AQo
    gets it back."""
    cells = est.capped_fill(30, 10)
    assert "AQo" not in cells
    in_range = [k for k, w in cells.items() if w == 1]
    shown = est.Counter({k: 3 for k in in_range}) + est.Counter({"AQo": 9})
    out, _, _, moved = est.calibrate(dict(cells), shown)
    assert moved and out["AQo"] == 1
    assert est.combo_count(out) == pytest.approx(est.combo_count(cells))


def test_estimate_reads_the_spot_from_the_filter():
    """The seat narrows the spot; a postflop term is left out, not counted."""
    facts = _facts(1, ["raise"] * 3 + ["fold"]) + _facts(1, ["fold"] * 4, seat=3)
    out = est.estimate(facts, "position=BTN,unopened=raise,cbet_flop")
    assert (out["n"], out["pct"]) == (4, 75.0) and out["node"]["ignored"] == ["cbet_flop"]
    assert est.estimate(facts, "unopened=raise")["pct"] == 37.5


# --------------------------------------------------------------- the board --


def test_sets_on_a_known_board_by_hand():
    """Pairs on AhKd2c: AA, KK and 22 lose a combo each to the board and are sets;
    77 keeps all six and is an underpair."""
    out = est.board_breakdown({"AA": 1, "KK": 1, "22": 1, "77": 1}, ("Ah", "Kd", "2c"))
    assert out["combos"] == 15
    by = {c["group"]: c for c in out["categories"]}
    assert by["trips"]["combos"] == 9 and by["weak_pair"]["combos"] == 6
    assert by["trips"]["pct"] == 60.0
    assert {x["leaf"]: x["combos"] for x in by["trips"]["leaves"]} == {"set": 9}
    assert {x["leaf"] for x in by["weak_pair"]["leaves"]} == {"pocket_pair"}
    assert out["value_pct"] == 100.0


def test_card_removal_and_weights():
    out = est.board_breakdown({"AKs": 1}, ("Ah", "Kd", "2c"))
    assert out["combos"] == 2  # AhKh and AdKd share a board card
    assert {c["group"]: c["combos"] for c in out["categories"]}["two_pair"] == 2
    half = est.board_breakdown({"AKs": 0.5, "77": 1}, ("Ah", "Kd", "2c"))
    assert half["combos"] == 7


def test_the_linear_cut_takes_the_strongest_first():
    out = est.board_breakdown({"AA": 1, "KK": 1, "22": 1, "77": 1}, ("Ah", "Kd", "2c"), next_freq=60)
    cut = out["linear_cut"]
    assert cut["combos"] == 9 and cut["group"] == "trips" and cut["at"] == "set"
    assert {m["group"]: m["pct"] for m in cut["mix"]}["trips"] == 100.0
    assert cut["bluff_pct"] == 0


def test_hand_strength_matches_strength_module():
    board = ("Ks", "Qs", "9h", "2d")
    table = est._table(board)
    for label in ("AKs", "JTs", "22", "A5o"):
        holdings = [(a, b) for a, b in est._HOLDINGS[label]
                    if est._text(a) not in board and est._text(b) not in board]
        for (_, hs), (a, b) in zip(table[label], holdings, strict=True):
            assert hs == pytest.approx(hand_strength(est._text(a) + est._text(b), list(board)), abs=1e-12)


@pytest.mark.parametrize("text", ["Ah7c", "Ah7c2d5s9h3c", "AhAh7c", "Xx7c2d", ""])
def test_a_bad_board(text):
    with pytest.raises(ValueError):
        est.parse_board(text)


def test_sampled_flops_have_the_texture_and_repeat():
    terms = est.flop_terms("unopened=raise,flop=ace_high,flop!=paired,turn=paired")
    assert terms == [("ace_high", True), ("paired", False)]
    flops = est.sample_flops(terms)
    assert len(flops) == est.SAMPLE_FLOPS == len(set(map(frozenset, flops)))
    assert all("ace_high" in board_texture(f) and "paired" not in board_texture(f) for f in flops)
    assert est.sample_flops(terms) == flops


def test_implied_bluffs():
    out = est.implied_bluffs(30.0, 60.0, 10.0, 200)
    assert (out["floor_pct"], out["pct"], out["bet_combos"], out["value_combos"]) == (50.0, 40.0, 120.0, 60.0)
    assert est.implied_bluffs(70.0, 60.0, 10.0, 200)["pct"] == 0
    assert est.implied_bluffs(30.0, 60.0, None, 200)["pct"] is None
    assert est.implied_bluffs(30.0, None, 10.0, 200) is None


def test_next_action_matches_the_stats_rates(db):
    for player in [r["player"] for r in report(db)]:
        facts = facts_for(db, player)
        rates = aggregate(facts)
        cbet = est.next_action(facts, est.resolve_node("opener"), "flop")
        if rates["_opp"]["cbet_flop"]:
            assert cbet["pct"] == rates["cbet_flop"] and cbet["n"] == rates["_opp"]["cbet_flop"]
        cont = est.next_action(facts, est.resolve_node("called_open"), "flop")
        if rates["_opp"]["fold_to_cbet_flop"]:
            assert cont["pct"] == pytest.approx(100 - rates["fold_to_cbet_flop"], abs=0.11)
        assert est.next_action(facts, est.resolve_node("limp"), "flop") is None


# ------------------------------------------------------------------ the API --


@pytest.fixture()
def api(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import importlib

    monkeypatch.setenv("PNT_DB", str(tmp_path / "api.sqlite"))
    from pnt.server import app as app_module

    importlib.reload(app_module)
    conn = connect(tmp_path / "api.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    players = [r["player"] for r in report(conn)]
    conn.close()
    return local_client(app_module.app), players


def test_estimate_endpoint(api):
    client, players = api
    body = None
    for p in players:
        r = client.get(f"/players/{p}/estimate", params={"filter": "unopened=raise"})
        assert r.status_code == 200
        if r.json()["n"]:
            body = r.json()
            break
    assert body and body["cells"] and body["node"]["label"] == "open"
    assert body["combos"] == pytest.approx(body["count"] / body["n"] * est.COMBOS, abs=0.1)
    p = players[0]
    bad = client.get(f"/players/{p}/estimate", params={"filter": "srp"})
    assert bad.status_code == 400 and "pick one preflop spot" in bad.json()["detail"]
    assert client.get(f"/players/{p}/estimate", params={"filter": "nonsense"}).status_code == 400
    assert client.get("/players/ghost/estimate", params={"filter": "opener"}).status_code == 404


def test_board_endpoint(api):
    client, players = api
    p = next(p for p in players
             if client.get(f"/players/{p}/estimate", params={"filter": "opener"}).json()["n"] >= 10)
    typed = client.get(f"/players/{p}/board", params={"filter": "opener", "board": "Ah7c2d"})
    assert typed.status_code == 200, typed.text
    body = typed.json()
    assert body["board"] == ["Ah", "7c", "2d"] and not body["sampled"]
    assert sum(c["combos"] for c in body["categories"]) == pytest.approx(body["combos"], abs=0.1)
    assert body["next"]["label"] == "c-bet flop"
    sampled = client.get(f"/players/{p}/board", params={"filter": "opener,flop=king_high"}).json()
    assert sampled["sampled"] and len(sampled["boards"]) == est.SAMPLE_FLOPS
    assert sampled["flop_terms"] == ["flop=king_high"]
    for params, says in [
        ({"filter": "opener", "board": "Ah7c"}, "3 to 5 cards"),
        ({"filter": "opener", "board": "AhAh2d"}, "twice"),
        ({"filter": "opener"}, "type a board"),
        ({"filter": "srp", "board": "Ah7c2d"}, "pick one preflop spot"),
    ]:
        r = client.get(f"/players/{p}/board", params=params)
        assert r.status_code == 400 and says in r.json()["detail"], (params, r.text)


# ---------------------------------------------------------------- the chart --


def test_the_chart_has_an_estimate_toggle_and_a_board_panel():
    html = CHART.read_text("utf-8")
    assert 'id="esttoggle"' in html and 'aria-pressed="false"' in html[html.index('id="esttoggle"') - 80:][:200]
    assert '<div id="est" class="est hidden"' in html
    assert "/estimate?" in html and "/board?" in html
    assert 'q.set("est", "1")' in html and 'q.get("est")' in html
    body = html[html.index("function renderBluffs("):]
    hidden = set(re.findall(r'"(\w+)"', body[body.index("for (const id of [") : body.index("]) {")]))
    assert {"est", "esttoggle"} <= hidden
