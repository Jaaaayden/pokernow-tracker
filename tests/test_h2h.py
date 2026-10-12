"""Head to head: the money between two players.

Three promises on every fixture hand: one hand's transfers add back up to each
player's net, the pool's matrix is antisymmetric, and a player's row against the
table sums to their net. Synthetic hands pin the exact heads-up attribution, the
multiway proportional rule and the bounty's own ledger. Every row's chart filter
finds the `n` it prints.
"""

from __future__ import annotations

import json
from collections import Counter
from fractions import Fraction

import pytest
from typer.testing import CliRunner

from pnt import cli
from pnt.db.conn import connect
from pnt.ingest.importer import import_csv
from pnt.stats.derive import HandPlayerRow, HandRow
from pnt.stats.filters import parse_filter
from pnt.stats.h2h import hand_flows, pair_flows, player_h2h, pool_h2h, vs_filters
from pnt.stats.queries import display_names, facts_for, identity_map, load_hands
from pnt.stats.review import review_rows
from tests.conftest import ALL_LOGS, EDGE_LOGS, local_client


@pytest.fixture()
def every(tmp_path):
    """The core corpus and the edge logs: a truncated hand among them."""
    conn = connect(tmp_path / "h2h.sqlite")
    for path in [*ALL_LOGS, *EDGE_LOGS]:
        import_csv(conn, path)
    yield conn
    conn.close()


def _person(conn):
    ident = identity_map(conn)
    return lambda pid: ident[pid][1] if pid in ident else pid


def _hand(players: dict[str, tuple[int, int, bool]], bounty: dict[str, int] | None = None,
          showdown: bool = True, bb: int = 10, complete: bool = True) -> HandRow:
    """players: pn_id -> (contributed, collected, folded)."""
    bounty = bounty or {}
    return HandRow(
        hand_id=1, game_id="g", hand_number=1, n_dealt_in=len(players), dead_button=False,
        blinds_irregular=False, went_to_showdown=showdown, saw_flop=True, complete=complete,
        bb=bb, ts=None,
        players={
            p: HandPlayerRow(pn_id=p, seat=i + 1, seats_from_button=i, contributed=put,
                             collected=got, folded=folded, bounty=bounty.get(p, 0))
            for i, (p, (put, got, folded)) in enumerate(players.items())
        },
        actions=[],
    )


def _conserves(hand: HandRow, flows) -> None:
    for pid, p in hand.players.items():
        moved = sum((c for (a, b), c in flows.items() if b == pid), Fraction(0)) - sum(
            (c for (a, b), c in flows.items() if a == pid), Fraction(0)
        )
        assert moved == p.collected - p.contributed + p.bounty, (hand.hand_id, pid)


def test_every_hand_settles_to_its_nets(every):
    hands = load_hands(every)
    settled = 0
    short = 0
    for hand in hands:
        out = hand_flows(hand)
        if out is None:
            ps = hand.players.values()
            assert not hand.complete or sum(p.collected - p.contributed for p in ps) or sum(p.bounty for p in ps)
            short += 1
            continue
        _conserves(hand, out[0])
        settled += 1
    assert settled > 500 and short, "the truncated log carries a hand with no ledger"


def test_the_matrix_is_antisymmetric(every):
    flows = pair_flows(load_hands(every), _person(every))
    assert flows
    for (a, b), e in flows.items():
        back = flows[b, a]
        assert e["net_bb_a_to_b"] == -back["net_bb_a_to_b"], (a, b)
        assert e["bounty_bb_a_to_b"] == -back["bounty_bb_a_to_b"], (a, b)
        for f in ("hands", "counted", "showdowns", "flow_hands", "split_hands"):
            assert e[f] == back[f], (a, b, f)
        assert e["counted"] <= e["hands"] and e["split_hands"] <= e["flow_hands"] <= e["counted"]
        assert e["failed_bluffs"] <= e["bluff_known"] <= e["bluff_spots"]


def test_a_row_against_the_table_sums_to_the_players_net(every):
    """What a player won from everyone is their net over the hands with a ledger."""
    key = _person(every)
    hands = load_hands(every)
    nets: Counter = Counter()
    for h in hands:
        if h.bb and hand_flows(h) is not None:
            for pid, p in h.players.items():
                nets[key(pid)] += Fraction(p.collected - p.contributed + p.bounty, h.bb)
    pool = pool_h2h(every)
    assert {p["player"] for p in pool["players"]} == set(nets)
    for p in pool["players"]:
        assert p["net_bb"] == round(float(nets[p["player"]]), 2), p["player"]
        cells = pool["cells"][p["player"]]
        for opp, c in cells.items():
            assert c["net_bb"] == -pool["cells"][opp][p["player"]]["net_bb"]
    assert pool["skipped"]["incomplete"] >= 1
    assert "hand_ids" not in json.dumps(pool)


def test_heads_up_at_the_end_attributes_exactly():
    """B called A down; C folded after putting 10 in. A won it all."""
    hand = _hand({"A": (50, 110, False), "B": (50, 0, False), "C": (10, 0, True)})
    flows, split = hand_flows(hand)
    assert flows == {("B", "A"): 50, ("C", "A"): 10} and not split
    out = pair_flows([hand])
    assert out["B", "A"]["net_bb_a_to_b"] == 5 and out["A", "B"]["net_bb_a_to_b"] == -5
    assert out["C", "A"]["net_bb_a_to_b"] == 1
    assert out["B", "C"]["net_bb_a_to_b"] == 0 and out["B", "C"]["flow_hands"] == 0
    assert out["A", "B"]["showdowns"] == 1 and out["A", "C"]["showdowns"] == 0
    assert out["A", "B"]["split_hands"] == 0 and out["A", "B"]["flow_hands"] == 1


def test_several_winners_split_each_loss_by_what_each_won():
    """A is all in for 20 and wins the main pot of 60; B wins the 60 side pot from C.

    A won 40 and B won 10, so C's 50 goes 40 to A and 10 to B. Split by what each
    *collected* (60 each) it would be 25 and 25, and A's transfers would no longer
    add up to A's net.
    """
    hand = _hand({"A": (20, 60, False), "B": (50, 60, False), "C": (50, 0, False)})
    flows, split = hand_flows(hand)
    assert flows == {("C", "A"): 40, ("C", "B"): 10} and split
    _conserves(hand, flows)
    out = pair_flows([hand])
    assert out["C", "A"]["net_bb_a_to_b"] == 4 and out["C", "B"]["net_bb_a_to_b"] == 1
    assert out["A", "C"]["split_hands"] == 1 and out["A", "B"]["flow_hands"] == 0


def test_a_chop_moves_nothing_between_the_choppers():
    hand = _hand({"A": (30, 45, False), "B": (30, 45, False), "C": (30, 0, False)})
    flows, split = hand_flows(hand)
    assert flows == {("C", "A"): 15, ("C", "B"): 15} and split


def test_bounties_go_to_the_player_they_were_paid_to():
    """B wins the pot from A; A and B each pay C, whose 7-2 folded, a bounty of 5."""
    hand = _hand(
        {"A": (40, 0, False), "B": (40, 80, False), "C": (0, 0, True)},
        bounty={"A": -5, "B": -5, "C": 10},
    )
    flows, split = hand_flows(hand)
    assert flows == {("A", "B"): 40, ("A", "C"): 5, ("B", "C"): 5} and not split
    _conserves(hand, flows)
    out = pair_flows([hand])
    assert out["B", "C"]["bounty_bb_a_to_b"] == Fraction(1, 2)
    assert out["B", "C"]["net_bb_a_to_b"] == Fraction(1, 2)
    assert out["A", "B"]["bounty_bb_a_to_b"] == 0


def test_a_hand_short_of_its_chips_moves_nothing_but_still_counts():
    cut = _hand({"A": (50, 0, False), "B": (50, 0, False)}, complete=False)
    unbalanced = _hand({"A": (50, 90, False), "B": (50, 0, False)})
    assert hand_flows(cut) is None and hand_flows(unbalanced) is None
    out = pair_flows([cut, unbalanced])
    assert out["A", "B"]["hands"] == 2 and out["A", "B"]["counted"] == 0
    assert out["A", "B"]["net_bb_a_to_b"] == 0


def test_failed_bluffs_are_the_review_rows(db):
    """A player's failed bluffs into each opponent are exactly Hand review's rows."""
    key = _person(db)
    flows = pair_flows(load_hands(db), key)
    seen = 0
    for alias in {key(pid) for pid in identity_map(db)}:
        rows, *_ = review_rows(db, alias)
        want: Counter = Counter()
        for r in rows:
            if r.kind == "failed_bluff":
                want.update({key(v.pn_id) for v in r.villains} - {alias})
        got = Counter({b: e["failed_bluffs"] for (a, b), e in flows.items() if a == alias and e["failed_bluffs"]})
        assert got == want, alias
        seen += sum(want.values())
    assert seen, "the fixtures have a failed bluff to attribute"


def test_every_row_reproduces_from_its_filter(db):
    names = display_names(db)
    checked = 0
    for alias in {a for _, a in identity_map(db).values()}:
        facts = facts_for(db, alias)
        out = player_h2h(db, alias)
        assert [abs(r["net_bb"]) for r in out["rows"]] == sorted((abs(r["net_bb"]) for r in out["rows"]), reverse=True)
        assert abs(out["net_bb"] - sum(r["net_bb"] for r in out["rows"])) <= 0.005 * (len(out["rows"]) + 1)
        for r in out["rows"]:
            assert r["filter"], r
            pred = parse_filter(r["filter"], names)
            assert sum(1 for f in facts if pred(f)) == r["n"], (alias, r["filter"])
            assert r["n"] <= r["hands"]
            checked += 1
    assert checked


def test_two_people_one_name_apart_in_case_are_named_by_id(db):
    """`hsj` and `HSJ` are two players; `vs=hsj` would find both, so each is
    named by their one PokerNow ID instead, and a plain name stays a name."""
    key = _person(db)
    names = display_names(db)
    filters = vs_filters(names, key)
    ids = {a: pid for pid, (_, a) in identity_map(db).items()}
    assert filters["hsj"] == f"vs={ids['hsj']}" and filters["HSJ"] == f"vs={ids['HSJ']}"
    assert filters["genericpoker"] == "vs=genericpoker"
    # Two IDs and a shared name: nothing one term can say.
    names2 = {**names, "zzz": "HSJ"}
    ident = identity_map(db)
    two = vs_filters(names2, lambda pid: "HSJ" if pid == "zzz" else ident[pid][1])
    assert two["HSJ"] is None


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


def test_endpoints_and_the_chart_drill(client):
    pool = client.get("/h2h").json()
    assert len(pool["players"]) >= 2
    alias = pool["players"][0]["player"]
    body = client.get(f"/players/{alias}/h2h").json()
    assert body["player"] == alias and body["rows"]
    for r in body["rows"]:
        assert pool["cells"][alias][r["opponent"]]["net_bb"] == r["net_bb"]
        hands = client.get(f"/players/{alias}/hands", params={"filter": r["filter"]}).json()["hands"]
        assert len(hands) == r["n"], r["filter"]
    assert client.get("/players/ghost/h2h").status_code == 404


def test_a_name_with_a_slash_reaches_h2h(client):
    pool = client.get("/h2h").json()
    alias = pool["players"][0]["player"]
    assert client.post("/aliases/rename", json={"old": alias, "new": "9/2"}).status_code == 200
    r = client.get("/players/9%2F2/h2h")
    assert r.status_code == 200 and r.json()["rows"]
    assert "9/2" in client.get("/h2h").json()["cells"]
    other = r.json()["rows"][0]["opponent"]
    back = client.get(f"/players/{other}/h2h").json()
    row = next(x for x in back["rows"] if x["opponent"] == "9/2")
    hands = client.get(f"/players/{other}/hands", params={"filter": row["filter"]}).json()["hands"]
    assert len(hands) == row["n"]


def test_cli(tmp_path):
    path = tmp_path / "cli.sqlite"
    conn = connect(path)
    for log in ALL_LOGS:
        import_csv(conn, log)
    alias = pool_h2h(conn)["players"][0]["player"]
    conn.close()
    runner = CliRunner()
    matrix = runner.invoke(cli.app, ["h2h", "--db", str(path)])
    assert matrix.exit_code == 0, matrix.output
    assert "who pays whom" in matrix.output and alias in matrix.output
    one = runner.invoke(cli.app, ["h2h", alias, "--db", str(path), "--json"])
    assert one.exit_code == 0, one.output
    assert json.loads(one.output)["player"] == alias
    assert runner.invoke(cli.app, ["h2h", "ghost", "--db", str(path)]).exit_code != 0
