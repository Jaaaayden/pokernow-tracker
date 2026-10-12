"""Strength by street: the slowplay table, its partition, and its filters.

Two promises are checked on every fixture player, street and floor: the cells of
a row add up to its `n`, and every cell's filter, used as a spot, finds exactly
that cell's hands. A synthetic hand pins what a slowplay is.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from pnt import cli
from pnt.db.conn import connect
from pnt.ingest.importer import import_csv
from pnt.stats.cards import MADE_LEAVES
from pnt.stats.derive import derive
from pnt.stats.filters import parse_filter
from pnt.stats.queries import display_names, facts_by_player
from pnt.stats.slowplay import CATEGORIES, SLOWPLAY, by_player, category, slowplay
from tests.conftest import ALL_LOGS, local_client
from tests.test_lines import _hand

STREETS = ("flop", "turn", "river")


def _count(facts, expr, names=None) -> list[int]:
    pred = parse_filter(expr, names)
    return [f.hand_id for f in facts if pred(f)]


def _check(rep: dict, facts, names, spot: str = "") -> None:
    """Every row partitions, and every filter in it reproduces its count."""
    def ids(expr):
        return sorted(_count(facts, ",".join(t for t in (spot, expr) if t), names))

    rows = rep["rows"]
    assert [r["leaf"] for r in rows] == list(MADE_LEAVES[: MADE_LEAVES.index(rep["floor"]) + 1])
    assert rep["total"]["n"] == sum(r["n"] for r in rows)
    assert rep["total"]["all_in"] == sum(r["all_in"] for r in rows)
    for row in [*rows, rep["total"]]:
        cells = row["cells"]
        assert list(cells) == [k for k, *_ in CATEGORIES]
        assert sum(c["n"] for c in cells.values()) == row["n"], row["leaf"]
        assert row["slowplay"] == sum(cells[k]["n"] for k in SLOWPLAY)
        assert len(ids(row["filter"])) == row["n"], row["filter"]
        assert ids(row["all_in_filter"]) == sorted(row["all_in_ids"]), row["all_in_filter"]
        for key, c in cells.items():
            assert ids(c["filter"]) == sorted(c["hand_ids"]), (row["leaf"], key, c["filter"])
            assert len(c["hand_ids"]) == c["n"]
    assert len(ids(rep["hands_filter"])) == rep["hands"]
    assert len(ids(rep["known_filter"])) == rep["known"]


@pytest.fixture()
def pool(db):
    grouped, aliases, _ = facts_by_player(db)
    return grouped, aliases, display_names(db)


def test_every_cell_reproduces_on_the_chart(pool):
    grouped, _, names = pool
    strong = 0
    for facts in grouped.values():
        for street in STREETS:
            for floor in ("set", "two_pair", "high_card"):
                rep = slowplay(facts, street, floor)
                _check(rep, facts, names)
                strong += rep["total"]["n"] if floor == "two_pair" else 0
    assert strong, "the fixtures have strong made hands to split"


def test_cells_reproduce_inside_a_spot(pool):
    grouped, _, names = pool
    everyone = [f for facts in grouped.values() for f in facts]
    for spot in ("srp", "wtsd,position=BB"):
        pred = parse_filter(spot, names)
        facts = [f for f in everyone if pred(f)]
        for street in STREETS:
            _check(slowplay(facts, street, "two_pair"), everyone, names, spot)


def test_the_columns_partition_every_line():
    """Each line a player can write lands in exactly one column, and that column's
    pattern is the only one that matches it."""
    from pnt.stats.slowplay import _cell_terms

    letters = "xbcrf"
    lines = {a + b + c for a in letters for b in ("", *letters) for c in ("", *letters) if b or not c}
    for line in lines:
        for back in (False, True):
            got = category(line, back)
            f = type("F", (), {})()
            f.line, f.check_back, f.seq = {"flop": line}, {"flop": back}, {"flop": line.upper()}
            hits = [k for k, *_ in CATEGORIES if parse_filter(_cell_terms("flop", k))(f)]
            assert hits == [got], (line, back, hits)


def _set_hand(flop: list[tuple]):
    """Heads-up, limped: B holds 88 and flops a set on 8d9c2h. `flop` is the flop's
    actions; B is first to act."""
    return _hand(
        ["A", "B"],
        [
            ("preflop", "A", "post", 5), ("preflop", "B", "post", 10),
            ("preflop", "A", "call", 5), ("preflop", "B", "check", 0),
            *flop,
        ],
        board=("8d", "9c", "2h"),
        cards={"A": "AhKd", "B": "8h8s"},
    )


def test_a_flopped_set_that_check_calls_is_a_slowplay():
    hand = _set_hand([("flop", "B", "check", 0), ("flop", "A", "bet", 10, 10), ("flop", "B", "call", 10, 10)])
    b = {f.pn_id: f for f in derive(hand)}["B"]
    rep = slowplay([b], "flop")
    row = next(r for r in rep["rows"] if r["leaf"] == "set")
    assert row["n"] == 1 and row["cells"]["check_call"]["n"] == 1
    assert row["slowplay"] == 1 and row["slowplay_pct"] == 100.0
    assert row["cells"]["check_call"]["filter"] == "made_flop=set,line_flop=xc*"
    assert parse_filter(row["cells"]["check_call"]["filter"])(b)
    assert rep["total"]["slowplay_pct"] == 100.0


def test_checked_through_splits_into_first_and_back():
    hand = _set_hand([("flop", "B", "check", 0), ("flop", "A", "check", 0)])
    f = {x.pn_id: x for x in derive(hand)}
    rep = slowplay([f["B"]], "flop")
    cells = next(r for r in rep["rows"] if r["leaf"] == "set")["cells"]
    assert cells["checked"]["n"] == 1 and cells["checked_back"]["n"] == 0
    # A has ace high, so give A the set to read the closing check.
    hand = _hand(
        ["A", "B"],
        [
            ("preflop", "A", "post", 5), ("preflop", "B", "post", 10),
            ("preflop", "A", "call", 5), ("preflop", "B", "check", 0),
            ("flop", "B", "check", 0), ("flop", "A", "check", 0),
        ],
        board=("8d", "9c", "2h"),
        cards={"A": "9h9s", "B": "AhKd"},
    )
    a = {x.pn_id: x for x in derive(hand)}["A"]
    cells = next(r for r in slowplay([a], "flop")["rows"] if r["leaf"] == "set")["cells"]
    assert cells["checked_back"]["n"] == 1 and cells["checked"]["n"] == 0
    assert parse_filter(cells["checked_back"]["filter"])(a)


def test_a_flopped_set_that_bets_is_not():
    hand = _set_hand([("flop", "B", "bet", 10, 10), ("flop", "A", "fold", 0)])
    b = {f.pn_id: f for f in derive(hand)}["B"]
    row = next(r for r in slowplay([b], "flop")["rows"] if r["leaf"] == "set")
    assert row["cells"]["bet"]["n"] == 1 and row["slowplay"] == 0


def test_floor_and_street_are_checked():
    with pytest.raises(ValueError, match="unknown floor"):
        slowplay([], "flop", "pair")
    with pytest.raises(ValueError, match="unknown street"):
        slowplay([], "preflop")
    empty = slowplay([], "turn", "two_pair")
    assert empty["coverage"] is None and empty["total"]["slowplay_pct"] is None


def test_players_are_ranked_by_share(pool):
    grouped, aliases, _ = pool
    rows = by_player(grouped, aliases, "flop", "two_pair")
    assert rows and all(r["n"] >= 1 for r in rows)
    shares = [r["slowplay_pct"] for r in rows]
    assert shares == sorted(shares, reverse=True)
    assert all(sum(r["cells"].values()) == r["n"] for r in rows)
    assert not by_player(grouped, aliases, "flop", "two_pair", min_n=10**6)


# --- the server and the CLI ----------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    monkeypatch.setenv("PNT_DB", str(tmp_path / "api.sqlite"))
    import importlib

    from pnt.server import app as app_module

    importlib.reload(app_module)
    conn = connect(tmp_path / "api.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    conn.close()
    return local_client(app_module.app)


def test_slowplay_endpoints(client):
    body = client.get("/players/genericpoker/slowplay", params={"street": "turn", "floor": "two_pair"}).json()
    assert body["player"] == "genericpoker" and body["street"] == "turn" and body["floor"] == "two_pair"
    for row in body["rows"]:
        assert sum(c["n"] for c in row["cells"].values()) == row["n"]
    flop = client.get("/players/genericpoker/slowplay").json()
    spot = client.get("/players/genericpoker/slowplay", params={"filter": "srp"}).json()
    assert flop["street"] == "flop" and flop["floor"] == "set"
    assert spot["filter"] == "srp" and spot["hands"] <= flop["hands"]
    # A cell's filter, added to the spot, lists exactly that cell's hands.
    for row in [*body["rows"], body["total"]]:
        for c in row["cells"].values():
            if c["n"]:
                hands = client.get("/players/genericpoker/hands", params={"filter": c["filter"]}).json()["hands"]
                assert sorted(h["hand_id"] for h in hands) == sorted(c["hand_ids"])

    pool = client.get("/slowplay", params={"street": "flop", "floor": "two_pair"}).json()
    assert pool["players"] and pool["total"]["n"] == sum(r["n"] for r in pool["players"])
    assert client.get("/slowplay", params={"min": 10**6}).json()["players"] == []

    url = "/players/genericpoker/slowplay"
    assert client.get(url, params={"street": "preflop"}).status_code == 422
    assert client.get(url, params={"floor": "pair"}).status_code == 400
    assert client.get(url, params={"filter": "made_flop=nuts"}).status_code == 400
    assert client.get("/slowplay", params={"floor": "pair"}).status_code == 400
    assert client.get("/players/ghost/slowplay").status_code == 404


def test_cli_prints_one_player_and_the_pool(tmp_path):
    path = tmp_path / "sp.sqlite"
    conn = connect(path)
    for log in ALL_LOGS:
        import_csv(conn, log)
    conn.close()
    run = CliRunner().invoke
    result = run(cli.app, ["slowplay", "genericpoker", "--street", "turn", "--floor", "two_pair", "--db", str(path)])
    assert result.exit_code == 0, result.output
    assert "slow%" in result.output and "total" in result.output and "shown hands only" in result.output
    result = run(cli.app, ["slowplay", "--min", "1", "--floor", "two_pair", "--db", str(path)])
    assert result.exit_code == 0, result.output
    assert "who slowplays" in result.output and "genericpoker" in result.output
    result = run(cli.app, ["slowplay", "genericpoker", "--json", "--db", str(path)])
    body = json.loads(result.output)
    assert body["player"] == "genericpoker" and body["floor"] == "set"
    result = run(cli.app, ["slowplay", "genericpoker", "--floor", "pair", "--db", str(path)])
    assert result.exit_code != 0 and "unknown floor" in result.output
