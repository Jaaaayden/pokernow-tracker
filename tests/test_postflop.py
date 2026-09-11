"""Postflop tendencies: bet-size buckets, donk bets, raising a c-bet, sizing tells.

Pinned to hands worked by hand from the heads-up fixture (bb = 10), like the core
stats. Pot before each bet in brackets:

  #10  Chris opens, gp calls. Flop: gp leads 20 [40] into Chris, the preflop
       aggressor -- a donk, exactly 1/2 pot; Chris calls. Turn: gp, now the
       aggressor, bets 60 [80] -- a c-bet of exactly 3/4 pot; Chris folds.
  #18  gp 3-bets, Chris calls. Flop: gp c-bets 30 [120]. Turn: gp checks, then
       Chris bets -- not a donk, gp had already acted. River: gp leads 135 [270]
       into Chris, the turn aggressor -- a donk of 1/2 pot.
  #92  Limped, so the flop has no aggressor and Chris's bet there is no donk. gp
       raises the flop, c-bets 40 [100] on the turn and 60 [180] on the river,
       and Chris raises the river c-bet.
"""

from __future__ import annotations

import pytest

from pnt.stats.derive import SIZE_BUCKETS, derive, size_bucket
from pnt.stats.filters import parse_filter
from pnt.stats.queries import aggregate, facts_for, load_hands
from pnt.stats.ranges import SIZING_KINDS, sizing_tells
from tests.conftest import HU_GAME

CHRIS = "5NARaPRkSp"
GP = "gpP9uUffpu"
STREETS = ("flop", "turn", "river")


@pytest.fixture()
def hu(db):
    """{hand_number: {pn_id: Facts}} for the heads-up game."""
    return {
        h.hand_number: {f.pn_id: f for f in derive(h)}
        for h in load_hands(db, HU_GAME)
    }


def test_size_bucket_edges():
    """Each PokerNow button starts a bucket; an edge is rounded down to a chip."""
    assert size_bucket(9, 20) == "small"
    assert size_bucket(10, 20) == "medium"
    assert size_bucket(21, 30) == "medium"
    assert size_bucket(22, 30) == "large", "a 3/4 click into 30 chips rounds to 22"
    assert size_bucket(23, 30) == "large"
    assert size_bucket(100, 100) == "large", "a pot-sized bet is not an overbet"
    assert size_bucket(101, 100) == "overbet"


def test_donk_then_cbet_in_a_single_raised_pot(hu):
    chris, gp = hu[10][CHRIS], hu[10][GP]
    assert gp.donk == {"flop": True} and gp.donk_opp == {"flop": True}
    assert gp.cbet == {"turn": True}
    assert gp.bet_size == {"flop": "medium", "turn": "large"}
    assert chris.faced_cbet_size == {"turn": "large"}
    assert chris.fold_to_cbet == {"turn": True} and chris.raise_cbet == {}
    assert chris.donk_opp == {}


def test_donk_needs_the_aggressor_still_to_act(hu):
    chris, gp = hu[18][CHRIS], hu[18][GP]
    assert gp.cbet == {"flop": True} and gp.bet_size["flop"] == "small"
    assert chris.faced_cbet_size == {"flop": "small"}
    assert chris.donk_opp == {}, "gp checked the turn before Chris bet"
    assert gp.donk == {"river": True} and gp.bet_size["river"] == "medium"


def test_raise_cbet_and_no_donk_without_an_aggressor(hu):
    chris, gp = hu[92][CHRIS], hu[92][GP]
    assert chris.donk_opp == {} and gp.donk_opp == {}, "a limped flop has no aggressor"
    assert gp.cbet == {"turn": True, "river": True}
    assert chris.faced_cbet_size == {"turn": "small", "river": "small"}
    assert chris.raise_cbet == {"river": True}
    assert chris.fold_to_cbet == {}


def test_postflop_filter_terms(hu):
    assert parse_filter("donk_flop,bet_flop=medium")(hu[10][GP])
    assert parse_filter("cbet_turn=large")(hu[10][GP])
    assert not parse_filter("cbet_turn=overbet")(hu[10][GP])
    assert not parse_filter("cbet_flop=medium")(hu[10][GP]), "a donk is not a c-bet"
    assert parse_filter("faced_cbet_turn=large,folded_to_cbet_turn")(hu[10][CHRIS])
    assert parse_filter("cbet_flop=small,donk_river,bet_river=medium")(hu[18][GP])
    assert parse_filter("raised_cbet_river,faced_cbet_river=small")(hu[92][CHRIS])
    assert parse_filter("donk_flop_opp")(hu[10][GP])
    # A number after `=` on the same name is still the numeric comparison.
    assert parse_filter("bet_river=0.5")(hu[18][GP])


def test_bad_size_terms_are_rejected():
    with pytest.raises(ValueError, match="unknown bet size"):
        parse_filter("cbet_flop=huge")
    with pytest.raises(ValueError, match="unknown filter term"):
        parse_filter("bet_river>=big")


def test_postflop_invariants(db):
    for h in load_hands(db):
        for f in derive(h):
            for s in STREETS:
                if f.raise_cbet.get(s) or f.fold_to_cbet.get(s):
                    assert f.fold_to_cbet_opp.get(s)
                assert not (f.raise_cbet.get(s) and f.fold_to_cbet.get(s))
                if f.fold_to_cbet_opp.get(s):
                    assert f.faced_cbet_size[s] in SIZE_BUCKETS
                if f.donk.get(s):
                    assert f.donk_opp.get(s) and not f.cbet_opp.get(s)
                if f.cbet.get(s):
                    assert f.bet_size[s] in SIZE_BUCKETS
                assert f.bet_size.keys() == f.bet_pot.keys()


def test_size_breakdowns_add_up(db):
    facts = facts_for(db, "genericpoker")
    out = aggregate(facts)
    for s in STREETS:
        cbets = sum(1 for f in facts if f.cbet.get(s))
        assert sum(v["n"] for v in out[f"cbet_{s}_sizes"].values()) == cbets
        faced = sum(v["faced"] for v in out[f"vs_cbet_{s}_by_size"].values())
        assert faced == out["_opp"][f"fold_to_cbet_{s}"]
        assert out[f"raise_cbet_{s}"] is None or 0 <= out[f"raise_cbet_{s}"] <= 100


def test_sizing_tells_partition_the_spot(db):
    facts = facts_for(db, "genericpoker")
    for kind in SIZING_KINDS:
        for s in STREETS:
            out = sizing_tells(facts, s, kind)
            assert sum(b["n"] for b in out["blocks"]) == out["spot"]
            for b in out["blocks"]:
                assert len(b["hand_ids"]) == b["n"]
                assert sum(c["n"] for c in b["classes"]) == b["known"]
                if kind == "faced_cbet" and b["n"]:
                    assert b["known"] <= b["continued"] <= b["n"]
                    assert abs(b["fold"] + b["call"] + b["raise"] - 100) < 0.3
                else:
                    assert b["known"] <= b["n"]
    blocks = sizing_tells(facts, "flop", "cbet")["blocks"]
    assert [b["size"] for b in blocks] == [*SIZE_BUCKETS, "check"]


def test_sizing_tells_rejects_bad_input(db):
    facts = facts_for(db, "genericpoker")
    with pytest.raises(ValueError, match="unknown street"):
        sizing_tells(facts, "preflop", "cbet")
    with pytest.raises(ValueError, match="unknown sizing kind"):
        sizing_tells(facts, "flop", "limp")
