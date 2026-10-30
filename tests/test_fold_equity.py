"""Fold equity by bet size: the bettor's break-even and the defender's MDF.

The counts are the Foundation's own (`bet_answer`, `faced_bet_size`,
`faced_cbet_size`), so the invariants here are that nothing is lost or counted
twice on the way into the buckets, that the arithmetic beside them is the
textbook one, and that every row's filter finds exactly that row's hands.
"""

from __future__ import annotations

import json
from collections import Counter

import pytest
from typer.testing import CliRunner

from pnt import cli
from pnt.db.conn import connect
from pnt.ingest.importer import import_csv
from pnt.stats.derive import ANSWERS, SIZE_BUCKETS, derive
from pnt.stats.filters import parse_filter
from pnt.stats.fold_equity import MIN_SAMPLE, break_even, fold_equity, pool_facts
from pnt.stats.queries import aggregate, display_names, facts_by_player, facts_for, report
from tests.conftest import ALL_LOGS, local_client
from tests.test_lines import _hand

STREETS = ("flop", "turn", "river")
SIDES = ("bets", "faced", "faced_cbet")


def _players(db):
    return [row["player"] for row in report(db)]


# --- the arithmetic ------------------------------------------------------------


#: Heads-up, 20 in the middle at the flop: A checks, B bets 10 -- half the pot --
#: and A folds.
HALF_POT = _hand(
    ["A", "B"],
    [
        ("preflop", "A", "post", 5), ("preflop", "B", "post", 10),
        ("preflop", "A", "call", 5), ("preflop", "B", "check", 0),
        ("flop", "A", "check", 0), ("flop", "B", "bet", 10, 10), ("flop", "A", "fold", 0),
    ],
    board=("9c", "8d", "2h"),
)


def test_a_half_pot_bet_breaks_even_at_a_third_and_must_be_defended_two_thirds():
    f = {x.pn_id: x for x in derive(HALF_POT)}
    assert f["B"].bet_pot == {"flop": 0.5} and f["B"].bet_answer == {"flop": "fold"}
    assert break_even(0.5) == pytest.approx(1 / 3)

    bet = {r["size"]: r for r in fold_equity([f["B"]])["streets"]["flop"]["bets"]}["medium"]
    assert (bet["n"], bet["folds"], bet["fold"]) == (1, 1, 100.0)
    assert bet["mean_size"] == bet["median_size"] == 0.5
    assert bet["break_even"] == 33.3
    assert bet["edge"] == 66.7, "folded to every time against a 33.3% break-even"
    assert bet["judged"] is False, f"one bet is under MIN_SAMPLE ({MIN_SAMPLE})"

    faced = {r["size"]: r for r in fold_equity([f["A"]])["streets"]["flop"]["faced"]}["medium"]
    assert (faced["n"], faced["folds"]) == (1, 1)
    assert faced["mdf"] == 66.7 and faced["break_even"] == 33.3
    assert faced["overfold"] == 66.7, "folded every time where a third was affordable"
    assert faced["filter"] == "faced_bet_flop=medium" and "fold_filter" not in faced


def test_break_even_is_the_mean_of_each_bets_own():
    """Two bets in one bucket: the break-even averages theirs, not the mean size's."""
    small, big = 1.0, 9.0  # both overbets, say: 1/2 and 9/10
    hands = []
    for i, size in enumerate((small, big)):
        h = _hand(
            ["A", "B"],
            [
                ("preflop", "A", "post", 5), ("preflop", "B", "post", 10),
                ("preflop", "A", "call", 5), ("preflop", "B", "check", 0),
                ("flop", "A", "check", 0), ("flop", "B", "bet", int(20 * size) + 1),
                ("flop", "A", "call", int(20 * size) + 1),
            ],
            board=("9c", "8d", "2h"),
        )
        h.hand_id = i + 1
        hands.append(next(x for x in derive(h) if x.pn_id == "B"))
    row = {r["size"]: r for r in fold_equity(hands)["streets"]["flop"]["bets"]}["overbet"]
    want = (break_even(hands[0].bet_pot["flop"]) + break_even(hands[1].bet_pot["flop"])) / 2
    assert row["break_even"] == round(100 * want, 1)
    assert row["break_even"] < round(100 * break_even(row["mean_size"]), 1)
    assert row["edge"] == round(-100 * want, 1), "called both times: no fold equity at all"


# --- the buckets against the plain counts --------------------------------------


def test_bucket_totals_equal_the_plain_bet_counts(db):
    """Every first bet lands in exactly one bucket, and so does every first decision
    facing one; the rates are the stat row's own."""
    for player in _players(db):
        facts = facts_for(db, player)
        out = fold_equity(facts)
        row = aggregate(facts)
        for s in STREETS:
            d = out["streets"][s]
            assert d["totals"]["bets"] == sum(1 for f in facts if parse_filter(f"bet_{s}")(f))
            assert d["totals"]["faced"] == sum(1 for f in facts if s in f.faced_bet_size)
            assert d["totals"]["faced_cbet"] == sum(1 for f in facts if s in f.faced_cbet_size)
            for r, size in zip(d["bets"], SIZE_BUCKETS, strict=True):
                stat = row[f"bet_{s}_answer_by_size"][size]
                assert r["size"] == size and r["n"] == stat["n"]
                assert r["folds"] + r["calls"] + r["raises"] + r["unanswered"] == r["n"]
                for a in ANSWERS:
                    assert r[a] == stat[a], (player, s, size, a)
            for r, size in zip(d["faced"], SIZE_BUCKETS, strict=True):
                stat = row[f"vs_bet_{s}_by_size"][size]
                assert r["n"] == stat["faced"]
                assert r["folds"] + r["calls"] + r["raises"] == r["n"], "exactly one answer each"
                for a in ANSWERS:
                    assert r[a] == stat[a]
            for r, size in zip(d["faced_cbet"], SIZE_BUCKETS, strict=True):
                stat = row[f"vs_cbet_{s}_by_size"][size]
                assert r["n"] == stat["faced"]
                assert (r["fold"], r["raise"]) == (stat["fold"], stat["raise"])


def test_a_faced_cbet_is_the_first_bet_faced(db):
    """The c-bet table reads its sizes from `faced_bet_pot`: that is only right if
    an unraised c-bet is always the first bet a player faced on the street."""
    seen = 0
    for player in _players(db):
        for f in facts_for(db, player):
            for s, size in f.faced_cbet_size.items():
                seen += 1
                assert f.faced_bet_size[s] == size
    assert seen


def test_break_even_and_mdf_add_to_100(db):
    out = fold_equity(pool_facts(db))
    for d in out["streets"].values():
        for side in ("faced", "faced_cbet"):
            for r in d[side]:
                if r["n"]:
                    assert r["mdf"] + r["break_even"] == pytest.approx(100, abs=0.11)
                    assert r["overfold"] == pytest.approx(r["fold"] - r["break_even"], abs=0.11)
        for r in d["bets"]:
            if r["n"]:
                assert r["edge"] == pytest.approx(r["fold"] - r["break_even"], abs=0.11)
                assert 0 < r["break_even"] < 100


# --- every row is a filter -------------------------------------------------------


def _check_rows(facts, out, names, with_ids):
    rows = 0
    for d in out["streets"].values():
        for side in SIDES:
            for r in d[side]:
                rows += 1
                hit = [f for f in facts if parse_filter(r["filter"], names)(f)]
                assert len(hit) == r["n"], r["filter"]
                if "fold_filter" in r:
                    folded = [f for f in facts if parse_filter(r["fold_filter"], names)(f)]
                    assert len(folded) == r["folds"], r["fold_filter"]
                if with_ids:
                    assert sorted(r["hand_ids"]) == sorted(f.hand_id for f in hit)
                    if "fold_filter" in r:
                        assert sorted(r["fold_hand_ids"]) == sorted(f.hand_id for f in folded)
    return rows


def test_every_row_reproduces_as_a_filter(db):
    names = display_names(db)
    checked = 0
    for player in _players(db):
        facts = facts_for(db, player)
        checked += _check_rows(facts, fold_equity(facts, with_ids=True), names, with_ids=True)
    # And the pool: its counts are rows of every player, found by the same terms.
    facts = pool_facts(db)
    checked += _check_rows(facts, fold_equity(facts), names, with_ids=False)
    assert checked
    assert all(r["fold_filter"].endswith(f"bet_answer_{s}=fold")
               for s, d in fold_equity(facts)["streets"].items() for r in d["bets"])


def test_rows_reproduce_inside_a_spot(db):
    """A filtered view's row filters still find their hands -- with the spot added."""
    names = display_names(db)
    spot = "saw_flop,srp"
    pred = parse_filter(spot, names)
    for player in _players(db)[:2]:
        facts = [f for f in facts_for(db, player) if pred(f)]
        out = fold_equity(facts, with_ids=True)
        for d in out["streets"].values():
            for r in d["bets"]:
                every = facts_for(db, player)
                both = parse_filter(f"{spot},{r['filter']}", names)
                assert sum(1 for f in every if both(f)) == r["n"]


# --- the pool ----------------------------------------------------------------------


def test_the_pool_is_the_sum_of_the_players(db):
    grouped, _, _ = facts_by_player(db)
    pool = fold_equity(pool_facts(db))
    per_player = [fold_equity(facts) for facts in grouped.values()]
    assert pool["hands"] == sum(p["hands"] for p in per_player)
    for s in STREETS:
        for side in SIDES:
            for i, r in enumerate(pool["streets"][s][side]):
                parts = [p["streets"][s][side][i] for p in per_player]
                for key in ("n", "folds", "calls", "raises"):
                    assert r[key] == sum(p[key] for p in parts), (s, side, r["size"], key)
                if r["n"]:
                    # The sizes are the players' sizes, pooled: the pool's mean is
                    # their n-weighted mean, give or take each one's rounding.
                    weighted = sum(p["mean_size"] * p["n"] for p in parts if p["n"]) / r["n"]
                    assert r["mean_size"] == pytest.approx(weighted, abs=0.002)
            assert pool["streets"][s]["totals"] == {
                k: sum(p["streets"][s]["totals"][k] for p in per_player)
                for k in pool["streets"][s]["totals"]
            }


def test_pool_facts_take_a_spot(db):
    every = pool_facts(db)
    srp = pool_facts(db, predicate=parse_filter("srp"))
    assert 0 < len(srp) < len(every)
    assert Counter(f.pot_level for f in srp) == Counter({2: len(srp)})


# --- endpoints and the CLI -----------------------------------------------------------


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


def test_player_and_pool_endpoints(client):
    body = client.get("/players/genericpoker/fold_equity").json()
    assert body["player"] == "genericpoker" and set(body["streets"]) == set(STREETS)
    flop = body["streets"]["flop"]
    assert [r["size"] for r in flop["bets"]] == list(SIZE_BUCKETS)
    assert all({"hand_ids", "fold_hand_ids", "break_even", "edge"} <= set(r) for r in flop["bets"])
    assert all({"hand_ids", "mdf", "overfold"} <= set(r) for r in flop["faced"])

    pool = client.get("/fold_equity").json()
    assert pool["player"] is None and pool["hands"] > body["hands"]
    assert "hand_ids" not in pool["streets"]["flop"]["bets"][0], "a pool row's ids are not one row each"
    narrowed = client.get("/fold_equity", params={"filter": "srp"}).json()
    assert narrowed["filter"] == "srp" and narrowed["hands"] < pool["hands"]


def test_endpoints_validate_input(client):
    assert client.get("/players/ghost/fold_equity").status_code == 404
    assert client.get("/players/genericpoker/fold_equity", params={"filter": "bet_flop=huge"}).status_code == 400
    assert client.get("/fold_equity", params={"filter": "nope"}).status_code == 400


def test_cli_prints_a_player_and_the_pool(tmp_path):
    path = tmp_path / "foldeq.sqlite"
    conn = connect(path)
    for log in ALL_LOGS:
        import_csv(conn, log)
    conn.close()
    result = CliRunner().invoke(cli.app, ["foldeq", "genericpoker", "--pool", "--db", str(path)])
    assert result.exit_code == 0, result.output
    assert "genericpoker" in result.output and "pool" in result.output and "MDF" in result.output
    result = CliRunner().invoke(cli.app, ["foldeq", "--json", "--db", str(path)])
    assert result.exit_code == 0, result.output
    assert set(json.loads(result.output)["streets"]) == set(STREETS)
    result = CliRunner().invoke(cli.app, ["foldeq", "genericpoker", "--pool", "--json", "--db", str(path)])
    assert set(json.loads(result.output)) == {"genericpoker", "pool"}
    result = CliRunner().invoke(cli.app, ["foldeq", "ghost", "--db", str(path)])
    assert result.exit_code != 0
