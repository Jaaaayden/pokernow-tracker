"""The replay's showdown summary: who won each pot on each board, with what."""

from __future__ import annotations

import json

import pytest

from pnt.ingest.csv_source import RawEntry
from pnt.stats.cards import parse_cards
from pnt.stats.equity import rank7, rank_name, rank_omaha
from pnt.stats.showdown import summary
from tests.conftest import HU, HU_GAME, local_client


def _name(hole: str, board: str) -> str:
    return rank_name(rank7(parse_cards(hole) + parse_cards(board)))


def test_hands_are_named_without_kickers():
    assert _name("KsKd", "9c9h2s5d3c") == "two pair, kings and nines"
    assert _name("Ah4h", "Kh9h2h5d3c") == "flush, ace high"
    assert _name("6s6d", "6h8c8d2s3c") == "full house, sixes full of eights"
    assert _name("Ts9s", "8s7s6sAdAc") == "straight flush, ten high"
    assert _name("AsKs", "QsJsTs2d3c") == "royal flush"
    assert _name("Ad2c", "3h4s5d9cKc") == "straight, five high"
    assert _name("7c7d", "7h7sKd2c3d") == "four of a kind, sevens"
    assert _name("JcJd", "Jh2s5d9cKc") == "three of a kind, jacks"
    assert _name("QcTd", "Qh2s5d9c3c") == "pair of queens"
    assert _name("AcTd", "Qh2s5d9c3c") == "ace high"


def test_omaha_plays_exactly_two_hole_cards():
    board = parse_cards("AhKhQh2c3d")
    # One heart in hand is no flush in Omaha, though Hold'em's rule would make one.
    assert rank_name(rank_omaha(parse_cards("Jh9s8s7c"), board)) == "ace high"
    assert rank_name(rank_omaha(parse_cards("JhTh9s8s"), board)) == "royal flush"


def _p(pid, hole, put, got, folded=False):
    return {"pn_id": pid, "hole_cards": hole, "contributed": put, "collected": got, "folded": folded}


BOARD = ["Kh", "9c", "9h", "2s", "5d"]


def test_a_heads_up_showdown():
    s = summary([_p("A", "KsKd", 100, 200), _p("B", "AsQd", 100, 0)], [BOARD])
    assert s["uncontested"] is None
    (pot,) = s["pots"]
    assert (pot["label"], pot["chips"]) == ("pot", 200)
    (run,) = pot["boards"]
    assert run["winners"] == [{"pn_id": "A", "hand": "full house, kings full of nines", "won": 200}]
    assert run["losers"] == [{"pn_id": "B", "hand": "pair of nines", "lost": 100}]


def test_a_chop_shows_each_share_and_dead_money_counts():
    s = summary(
        [_p("A", "AsQd", 100, 125), _p("B", "AcQc", 100, 125), _p("C", None, 50, 0, folded=True)],
        [BOARD],
    )
    (pot,) = s["pots"]
    assert pot["chips"] == 250
    assert [(w["pn_id"], w["won"]) for w in pot["boards"][0]["winners"]] == [("A", 125), ("B", 125)]


def test_side_pots_name_each_winner_and_stake():
    # C is all in for 50 with the best hand; A beats B for the side pot.
    s = summary(
        [_p("A", "KsKd", 200, 300), _p("B", "AsQd", 200, 0), _p("C", "9s9d", 50, 150)],
        [BOARD],
    )
    main, side = s["pots"]
    assert (main["label"], main["chips"], side["label"], side["chips"]) == ("main pot", 150, "side pot", 300)
    assert [w["pn_id"] for w in main["boards"][0]["winners"]] == ["C"]
    assert {lo["pn_id"]: lo["lost"] for lo in main["boards"][0]["losers"]} == {"A": 50, "B": 50}
    assert side["boards"][0]["winners"][0] == {"pn_id": "A", "hand": "full house, kings full of nines", "won": 300}
    assert side["boards"][0]["losers"] == [{"pn_id": "B", "hand": "pair of nines", "lost": 150}]


def test_run_it_twice_splits_the_pot_by_board():
    second = ["Kh", "9c", "9h", "As", "Ad"]
    s = summary([_p("A", "KsKd", 100, 100), _p("B", "AcQd", 100, 100)], [BOARD, second])
    (pot,) = s["pots"]
    one, two = pot["boards"]
    assert (one["run"], one["winners"][0]["pn_id"], one["winners"][0]["won"]) == (0, "A", 100)
    assert (two["run"], two["winners"][0]["pn_id"], two["board"]) == (1, "B", second)
    assert two["winners"][0]["hand"] == "full house, aces full of nines"


def test_uncontested_and_unfinished_hands():
    s = summary([_p("A", None, 30, 45), _p("B", None, 15, 0, folded=True)], [])
    assert s == {"uncontested": {"pn_id": "A", "won": 45}, "pots": []}
    assert summary([_p("A", None, 30, 0), _p("B", None, 15, 0)], []) is None


def test_a_mucked_winner_falls_back_to_who_collected():
    # Only the loser's cards are on record: the pots alone would hand it to them.
    s = summary([_p("A", "AsQd", 100, 0), _p("B", None, 100, 200)], [BOARD])
    (run,) = s["pots"][0]["boards"]
    assert run["winners"] == [{"pn_id": "B", "hand": None, "won": 200}]
    assert run["losers"] == [{"pn_id": "A", "hand": "pair of nines", "lost": 100}]


# --- the replay endpoint -----------------------------------------------------

fastapi = pytest.importorskip("fastapi")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """The heads-up log (with a run-it-twice), a Double Board hand and PLO."""
    import importlib

    from pnt.db.conn import connect
    from pnt.ingest.importer import import_csv, ingest_entries, rebuild_game
    from pnt.server import app as app_module
    from tests.test_double_board import LINES as DOUBLE
    from tests.test_game_modes import PLO_SHOWDOWN

    monkeypatch.setenv("PNT_DB", str(tmp_path / "sd.sqlite"))
    importlib.reload(app_module)
    conn = connect(tmp_path / "sd.sqlite")
    import_csv(conn, HU)
    plo = [t.replace("{n}", "1") for t in PLO_SHOWDOWN]
    for game, lines in (("pgltest_sd_double", DOUBLE), ("pgltest_sd_plo", plo)):
        ingest_entries(conn, game, [RawEntry(ord=i, at="2026-01-01T00:00:00Z", entry=t) for i, t in enumerate(lines)], "test")
        rebuild_game(conn, game)
    ids = {g: conn.execute("SELECT hand_id FROM hands WHERE game_id = ?", (g,)).fetchone()[0]
           for g in ("pgltest_sd_double", "pgltest_sd_plo")}
    ids["rit"] = conn.execute(
        "SELECT hand_id FROM hands WHERE game_id = ? AND run_count = 2", (HU_GAME,)
    ).fetchone()[0]
    ids["hu"] = [r[0] for r in conn.execute("SELECT hand_id FROM hands WHERE game_id = ?", (HU_GAME,))]
    conn.close()
    return local_client(app_module.app), ids


def _winners(body):
    return [(w["pn_id"], w["hand"], w["won"]) for p in body["showdown"]["pots"] for r in p["boards"] for w in r["winners"]]


def test_replay_names_the_double_board_winners(client):
    c, ids = client
    body = c.get(f"/hands/{ids['pgltest_sd_double']}").json()
    (pot,) = body["showdown"]["pots"]
    assert [r["board"] for r in pot["boards"]] == json.loads(body["hand"]["board_json"])
    # As the log's own collected lines say: Bob's full house, then Ann's two pair.
    assert _winners(body) == [("BBB", "full house, jacks full of eights", 195), ("AAA", "two pair, tens and nines", 195)]


def test_replay_names_the_plo_winner(client):
    c, ids = client
    body = c.get(f"/hands/{ids['pgltest_sd_plo']}").json()
    # The log: "collected 120 from pot with Flush, Q High", over a lower Q-high flush.
    assert _winners(body) == [("BBB", "flush, queen high", 120)]
    (run,) = body["showdown"]["pots"][0]["boards"]
    assert run["losers"] == [{"pn_id": "AAA", "hand": "flush, queen high", "lost": 60}]


def test_replay_showdowns_pay_out_what_was_collected(client):
    """Every hand of the heads-up log: the summary's winnings are its chips."""
    c, ids = client
    seen = set()
    for hid in ids["hu"]:
        body = c.get(f"/hands/{hid}").json()
        s = body["showdown"]
        collected = {p["pn_id"]: p["collected"] for p in body["players"] if p["collected"]}
        if s is None:
            continue
        if s["uncontested"]:
            seen.add("uncontested")
            assert collected == {s["uncontested"]["pn_id"]: s["uncontested"]["won"]}
            continue
        seen.add("showdown")
        won: dict[str, int] = {}
        for pid, _, chips in _winners(body):
            won[pid] = won.get(pid, 0) + chips
        assert won == collected
    assert seen == {"uncontested", "showdown"}
    rit = c.get(f"/hands/{ids['rit']}").json()["showdown"]
    assert rit and len(rit["pots"][0]["boards"]) == 2
