"""Red line and blue line: the two add up to the total at every hand, incomplete
hands are in no line, and the total is bb/100's money. See SPEC.md, "Red and blue
lines"."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from pnt import cli
from pnt.db.conn import connect
from pnt.ingest.importer import import_csv
from pnt.stats.allin import allin_hand_list
from pnt.stats.derive import Facts
from pnt.stats.filters import parse_filter
from pnt.stats.queries import display_names, facts_for, rates, report, tally
from pnt.stats.winnings import allin_adjusted, winnings_series
from tests.conftest import ALL_LOGS, TRUNCATED, local_client

#: Two independently rounded running sums can sit a cent off their rounded total.
CENT = 0.0101


def _players(conn):
    return [r["player"] for r in report(conn)]


def _fact(hand_id: int, net: int, *, game: str = "g1", **kw) -> Facts:
    return Facts(
        hand_id=hand_id, pn_id="p", n_dealt_in=2, seats_from_button=0, dead_button=False,
        game_id=game, net=net, bb_size=10, **kw,
    )


def test_red_and_blue_add_up_to_the_total_at_every_hand(db):
    for player in _players(db):
        facts = facts_for(db, player)
        out = winnings_series(facts)
        assert out["points"], player
        for p in out["points"]:
            assert abs(p["red"] + p["blue"] - p["total"]) <= CENT, (player, p)
        t = out["totals"]
        assert abs(t["red"]["net_bb"] + t["blue"]["net_bb"] - t["net_bb"]) <= CENT


def test_the_final_total_is_rates_net_bb(db):
    for player in _players(db):
        facts = facts_for(db, player)
        out = winnings_series(facts)
        r = rates(tally(facts))
        assert out["points"][-1]["total"] == out["totals"]["net_bb"] == r["net_bb"], player
        assert out["totals"]["bb_per_100"] == r["bb_per_100"], player
        assert out["totals"]["bb_hands"] == tally(facts).bb_n == len(out["points"])


def test_points_are_in_hand_order_and_each_line_moves_on_its_own_hands():
    facts = [
        _fact(1, 15, wtsd=True),   # +1.5 at showdown
        _fact(2, -10),             # -1 without
        _fact(3, 30, game="g2"),   # +3 without, next session
        _fact(4, -20, game="g2", wtsd=True),
    ]
    out = winnings_series(facts)
    assert [p["hand_id"] for p in out["points"]] == [1, 2, 3, 4]
    assert [(p["red"], p["blue"], p["total"]) for p in out["points"]] == [
        (0.0, 1.5, 1.5), (-1.0, 1.5, 0.5), (2.0, 1.5, 3.5), (2.0, -0.5, 1.5),
    ]
    assert [p["sd"] for p in out["points"]] == [True, False, False, True]
    s1, s2 = out["sessions"]
    assert (s1["game_id"], s1["hands"], s1["net_bb"], s1["red"], s1["blue"]) == ("g1", 2, 0.5, -1.0, 1.5)
    assert (s2["game_id"], s2["hands"], s2["net_bb"], s2["bb_per_100"]) == ("g2", 2, 1.0, 50.0)
    assert s2["hand_ids"] == [3, 4]


def test_incomplete_and_bb_less_hands_are_in_no_line():
    facts = [
        _fact(1, 25),
        _fact(2, -400, complete=False, wtsd=True),  # a log that stopped mid-hand
        Facts(hand_id=3, pn_id="p", n_dealt_in=2, seats_from_button=0, dead_button=False,
              game_id="g1", net=-50, bb_size=None),  # no known big blind
        _fact(4, -10, wtsd=True),
    ]
    out = winnings_series(facts)
    assert [p["hand_id"] for p in out["points"]] == [1, 4]
    t = out["totals"]
    assert (t["hands"], t["bb_hands"], t["net_bb"]) == (4, 2, 1.5)
    # They still count as hands on their side of the split -- the filter finds them.
    assert (t["red"]["hands"], t["red"]["bb_hands"]) == (2, 1)
    assert (t["blue"]["hands"], t["blue"]["bb_hands"]) == (2, 1)
    assert out["sessions"][0]["hands"] == 4 and out["sessions"][0]["bb_hands"] == 2
    # Nothing known: unknown, not zero.
    empty = winnings_series([_fact(9, -400, complete=False)])
    assert empty["points"] == [] and empty["totals"]["net_bb"] is None
    assert empty["sessions"][0]["net_bb"] is None


def test_incomplete_hands_on_a_truncated_log(tmp_path):
    conn = connect(tmp_path / "t.sqlite")
    import_csv(conn, TRUNCATED)
    seen = 0
    for player in _players(conn):
        facts = facts_for(conn, player)
        broken = {f.hand_id for f in facts if not f.complete}
        out = winnings_series(facts)
        assert not broken & {p["hand_id"] for p in out["points"]}
        assert out["totals"]["hands"] == len(facts)
        assert out["totals"]["net_bb"] == rates(tally(facts))["net_bb"]
        seen += len(broken)
    assert seen, "the truncated log stops mid-hand"


def test_every_line_and_session_reproduces_its_hands(db):
    names = display_names(db)
    for player in _players(db):
        facts = facts_for(db, player)
        out = winnings_series(facts)
        for key in ("red", "blue"):
            line = out["totals"][key]
            pred = parse_filter(line["filter"], names)
            assert sum(1 for f in facts if pred(f)) == line["hands"], (player, key)
        sessions = out["sessions"]
        assert sum(s["hands"] for s in sessions) == len(facts)
        for s in sessions:
            assert len(s["hand_ids"]) == s["hands"]
            assert {f.game_id for f in facts if f.hand_id in set(s["hand_ids"])} == {s["game_id"]}
        known = [s["net_bb"] for s in sessions if s["net_bb"] is not None]
        assert abs(sum(known) - out["totals"]["net_bb"]) <= CENT * len(known)


def test_the_adjusted_line_moves_only_by_the_all_in_gap(db):
    seen = 0
    for player in _players(db):
        facts = facts_for(db, player)
        out = winnings_series(facts, allin_adjusted(db, facts))
        allin = allin_hand_list(db, player)
        adj = out["totals"]["adjusted"]
        assert adj["allin_hands"] == len(allin["hands"]), player
        assert adj["allin_hands"] == sum(1 for p in out["points"] if p["allin"])
        if not allin["hands"]:
            assert adj["net_bb"] == out["totals"]["net_bb"]
            continue
        seen += 1
        gap = allin["adjusted_bb"] - allin["net_bb"]
        assert abs(adj["net_bb"] - out["totals"]["net_bb"] - gap) <= 0.05, player
        assert out["points"][-1]["adjusted"] == adj["net_bb"]
    assert seen, "the core logs have all-in showdowns"


def test_winnings_endpoint(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import importlib

    from pnt.ingest.importer import rename_player

    monkeypatch.setenv("PNT_DB", str(tmp_path / "api.sqlite"))
    from pnt.server import app as app_module

    importlib.reload(app_module)
    conn = connect(tmp_path / "api.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    rename_player(conn, "genericpoker", "9/2")
    conn.close()
    client = local_client(app_module.app)

    body = client.get("/players/HSJ/winnings").json()
    assert body["player"] == "HSJ" and body["filter"] is None
    assert body["totals"]["hands"] == 231 and body["totals"]["net_bb"] == -894.4
    assert "adjusted" not in body["totals"] and "adjusted" not in body["points"][0]
    spot = client.get("/players/HSJ/winnings", params={"filter": "position=BB"}).json()
    assert spot["filter"] == "position=BB" and spot["totals"]["hands"] == 111
    adj = client.get("/players/HSJ/winnings", params={"adjusted": "true"}).json()
    assert adj["totals"]["adjusted"]["allin_hands"] > 0 and "adjusted" in adj["points"][-1]
    assert client.get("/players/9%2F2/winnings").status_code == 200
    assert client.get("/players/9%2F2/winnings").json()["totals"]["hands"] > 0
    assert client.get("/players/HSJ/winnings", params={"filter": "nonsense"}).status_code == 400
    assert client.get("/players/ghost/winnings").status_code == 404


def test_cli_prints_the_totals_and_sessions(tmp_path):
    path = tmp_path / "w.sqlite"
    conn = connect(path)
    for p in ALL_LOGS:
        import_csv(conn, p)
    conn.close()
    result = CliRunner().invoke(cli.app, ["winnings", "HSJ", "--adjusted", "--db", str(path)])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "-894.4" in out and "red (no showdown)" in out and "blue (showdown)" in out
    assert "all-in adjusted" in out and "pglSdQtyFGypDbrqD5IhXXlYz" in out
    result = CliRunner().invoke(cli.app, ["winnings", "HSJ", "--json", "--filter", "srp", "--db", str(path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["totals"]["hands"] == 129
    result = CliRunner().invoke(cli.app, ["winnings", "nobody", "--db", str(path)])
    assert result.exit_code != 0 and "unknown alias" in result.output


def test_the_chart_draws_the_lines():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "pnt" / "server" / "static" / "chart.html").read_text("utf-8")
    assert "function renderWinnings(" in html and "/winnings?" in html
    # Drawn at the top of the Results card, from the data fetched beside /results.
    assert "renderWinnings(d.winnings)" in html.split("function renderResults(")[1]
    assert "data.winnings = await" in html
