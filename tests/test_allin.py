"""All-in EV over the fixture corpus: 30 all-in showdowns with every hand known.

The figures are pinned by invariants a person can check rather than by numbers
nobody re-derived: expected chips sum to the pot, the gaps in one hand sum to
zero, a river all-in has no gap, and the ledger side is the same `net` every
other page prints. Sampling is turned down so the preflop hands cost milliseconds.
"""

from __future__ import annotations

from collections import defaultdict

import pytest

from pnt.stats import allin as al
from pnt.stats import equity as eq
from pnt.stats.derive import derive
from pnt.stats.filters import parse_filter
from pnt.stats.queries import display_names


@pytest.fixture(autouse=True)
def _fast_sampling(monkeypatch):
    monkeypatch.setattr(eq, "SAMPLES", 2000)


@pytest.fixture()
def rows(db):
    return al.allin_rows(db)


def test_the_population_is_every_allin_showdown_with_every_hand_known(rows):
    rows, skipped, _ = rows
    assert len({r.hand_id for r in rows}) == 30
    assert skipped == {"cards_unknown": 1}
    assert all(r.hole_cards for r in rows)
    assert all(v[1] for r in rows for v in r.villains)


def test_expected_chips_sum_to_the_pot_and_the_gaps_cancel(rows):
    """Nobody's luck comes from nowhere: within a hand it is zero-sum."""
    rows, _, _ = rows
    by_hand = defaultdict(list)
    for r in rows:
        by_hand[r.hand_id].append(r)
    for rs in by_hand.values():
        assert len(rs) >= 2
        assert sum(r.expected for r in rs) == pytest.approx(rs[0].pot, abs=0.05)
        assert sum(r.equity for r in rs) == pytest.approx(1.0, abs=1e-3)
        assert sum(r.diff for r in rs) == pytest.approx(0.0, abs=0.05)


def test_a_river_allin_has_no_gap(rows):
    river = [r for r in rows[0] if r.street == "river"]
    assert river
    for r in river:
        assert (r.method, r.n, r.diff) == ("exact", 1, 0.0)
        assert r.equity in (0.0, 0.5, 1.0)


def test_the_board_is_what_was_dealt_when_the_betting_stopped(rows):
    rows, _, _ = rows
    dealt = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}
    assert {r.street for r in rows} == set(dealt)
    for r in rows:
        assert len(r.board) == dealt[r.street]
        assert r.board == r.full_board[: len(r.board)]
        assert r.method == ("sampled" if r.street == "preflop" else "exact")
    # Ran twice after a turn all-in: the shared four cards are the decision board.
    twice = [r for r in rows if r.run_count == 2 and r.street == "turn"]
    assert twice and all(len(r.board) == 4 for r in twice)


def test_actual_is_the_same_net_every_other_page_prints(rows):
    rows, _, hands = rows
    for r in rows:
        f = next(f for f in derive(hands[r.hand_id]) if f.pn_id == r.pn_id)
        assert (r.actual, r.pot, r.bb) == (f.net, f.pot, f.bb_size)
        assert r.adjusted == pytest.approx(r.expected - r.contributed + r.bounty, abs=0.01)


def test_the_second_pass_reads_the_cache_and_computes_nothing(db, monkeypatch):
    first, *_ = al.allin_rows(db)
    assert db.execute("SELECT COUNT(*) FROM equity_cache").fetchone()[0] > 0
    monkeypatch.setattr(al, "expected_collected", lambda *a, **k: pytest.fail("recomputed"))
    again, *_ = al.allin_rows(db)
    assert again == first


def test_dropping_the_cache_changes_no_number(db):
    first, *_ = al.allin_rows(db)
    db.execute("DELETE FROM equity_cache")
    db.commit()
    second, *_ = al.allin_rows(db)
    assert second == first


def test_the_report_sums_its_rows_under_the_players_alias(db):
    report = al.allin_report(db)
    rows, skipped, _ = al.allin_rows(db)
    names = display_names(db)
    assert {r["player"] for r in report} == {names[r.pn_id] for r in rows}
    for row in report:
        mine = [r for r in rows if names[r.pn_id] == row["player"]]
        assert row["hands"] == len(mine) == sum(row["by_street"][s]["hands"] for s in al.STREETS)
        assert row["net"] == sum(r.actual for r in mine)
        assert row["diff"] == pytest.approx(row["net"] - row["adjusted"], abs=0.01)
        assert row["diff_bb"] == pytest.approx(row["net_bb"] - row["adjusted_bb"], abs=0.02)
        assert row["skipped"] == skipped
        assert 0 <= row["equity_avg"] <= 100
    assert report == sorted(report, key=lambda r: -r["hands"])
    assert al.allin_report(db, min_hands=10**6) == []


def test_a_spot_filter_narrows_the_report(db):
    everything = {r["player"]: r for r in al.allin_report(db)}
    vs = al.allin_report(db, predicate=parse_filter("vs=Chris", display_names(db)))
    assert vs and "Chris" not in {r["player"] for r in vs}
    for r in vs:
        assert r["hands"] <= everything[r["player"]]["hands"]
    preflop = al.allin_report(db, predicate=parse_filter("limped", display_names(db)))
    assert sum(r["hands"] for r in preflop) < sum(r["hands"] for r in everything.values())


def test_the_hand_list_is_oldest_first_and_names_the_villains(db):
    body = al.allin_hand_list(db, "genericpoker")
    hands = body["hands"]
    assert hands and body["hands"] == sorted(hands, key=lambda h: (h["ts"], h["hand_id"]))
    aliases = set(display_names(db).values())
    for h in hands:
        assert h["villains"] and all(v["player"] in aliases and v["cards"] for v in h["villains"])
        assert "genericpoker" not in {v["player"] for v in h["villains"]}
        assert h["actual_bb"] == pytest.approx(h["actual"] / h["bb"], abs=0.01)
    assert body["hands"] == al.allin_hand_list(db, "genericpoker")["hands"]
    with pytest.raises(ValueError, match="unknown alias"):
        al.allin_hand_list(db, "nobody")
