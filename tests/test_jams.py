"""Jams: all-in bets and raises, and the players who faced and called them.

Pinned to hands from the fixtures. `AI` marks an action logged all in.

  heads-up
  #44   Chris calls, gp raises, Chris re-raises AI, gp folds -- a preflop jam
        faced and folded to.
  #182  Flop raising war: gp bets, Chris raises, gp raises, Chris raises AI, gp
        calls -- a flop jam, called.
  #188  River: gp bets AI, Chris calls AI -- the call puts Chris all in too, but
        a call is never a jam.

  multiway
  #16   "all in" (a player's name) raises, onlybluffs re-raises, hsj calls, then
        "all in" raises AI: onlybluffs, who already raised this street, folds to
        it and hsj calls AI.
  #20   "all in" raises AI; onlybluffs calls, getting even calls AI, hsj folds --
        three players face one jam.
"""

from __future__ import annotations

import pytest

from pnt.stats.derive import HandAction, HandPlayerRow, HandRow, derive
from pnt.stats.filters import parse_filter
from pnt.stats.queries import load_hands
from tests.conftest import HU_GAME, MULTIWAY_GAME

CHRIS = "5NARaPRkSp"
GP = "gpP9uUffpu"
ALL_IN = "ILQFEWJ0SW"
ONLYBLUFFS = "MBFczOlpuA"
GETTING_EVEN = "d-4X_F_SSU"
HSJ = "PEMYRVPxOS"


def _facts(db, game):
    return {h.hand_number: {f.pn_id: f for f in derive(h)} for h in load_hands(db, game)}


@pytest.fixture()
def hu(db):
    return _facts(db, HU_GAME)


@pytest.fixture()
def multi(db):
    return _facts(db, MULTIWAY_GAME)


def test_a_preflop_jam_folded_to(hu):
    chris, gp = hu[44][CHRIS], hu[44][GP]
    assert chris.jam == {"preflop": True} and chris.faced_jam == {}
    assert gp.faced_jam == {"preflop": True} and gp.called_jam == {}
    assert gp.jam == {}, "gp's raise before the jam was not all in"


def test_a_flop_jam_called(hu):
    chris, gp = hu[182][CHRIS], hu[182][GP]
    assert chris.jam == {"flop": True}
    assert gp.faced_jam == {"flop": True} and gp.called_jam == {"flop": True}


def test_an_all_in_call_is_not_a_jam(hu):
    chris, gp = hu[188][CHRIS], hu[188][GP]
    assert gp.jam == {"river": True}
    assert chris.jam == {}, "calling all in is calling, not jamming"
    assert chris.called_jam == {"river": True}


def test_raising_first_does_not_excuse_facing_the_jam(multi):
    hand = multi[16]
    assert hand[ALL_IN].jam == {"preflop": True}
    assert hand[ONLYBLUFFS].faced_jam == {"preflop": True}
    assert hand[ONLYBLUFFS].called_jam == {}
    assert hand[HSJ].called_jam == {"preflop": True}


def test_everyone_left_to_act_faces_the_jam(multi):
    hand = multi[20]
    assert hand[ALL_IN].jam == {"preflop": True}
    assert hand[ALL_IN].faced_jam == {}, "nobody faces their own jam"
    for pid in (ONLYBLUFFS, GETTING_EVEN, HSJ):
        assert hand[pid].faced_jam == {"preflop": True}
    assert hand[ONLYBLUFFS].called_jam == hand[GETTING_EVEN].called_jam == {"preflop": True}
    assert hand[HSJ].called_jam == {}


def test_an_all_in_blind_is_not_a_jam():
    """Rule 1: a forced post is not a decision, so a blind that puts a short stack
    all in is neither a jam nor something the caller faced."""
    short, caller = "short", "caller"
    hand = HandRow(
        hand_id=1, game_id="g", hand_number=1, n_dealt_in=2, dead_button=False,
        blinds_irregular=False, went_to_showdown=True, saw_flop=False, complete=True,
        bb=10, ts=None,
        players={
            short: HandPlayerRow(pn_id=short, seat=1, seats_from_button=1, contributed=1,
                                 collected=0, folded=False),
            caller: HandPlayerRow(pn_id=caller, seat=2, seats_from_button=0, contributed=1,
                                  collected=2, folded=False),
        },
        actions=[
            HandAction(seq=1, street="preflop", pn_id=short, kind="post", amount=1,
                       is_forced=True, all_in=True),
            HandAction(seq=2, street="preflop", pn_id=caller, kind="call", amount=1,
                       is_forced=False, all_in=False),
        ],
    )
    facts = {f.pn_id: f for f in derive(hand)}
    assert facts[short].jam == {}
    assert facts[caller].faced_jam == {} and facts[caller].called_jam == {}


def test_jam_filters(hu, multi):
    assert parse_filter("jam")(hu[188][GP])
    assert parse_filter("jam_river")(hu[188][GP])
    assert not parse_filter("jam_turn")(hu[188][GP])
    assert not parse_filter("jam")(hu[188][CHRIS])
    assert parse_filter("called_jam_river,wtsd")(hu[188][CHRIS])
    assert parse_filter("faced_jam_preflop")(hu[44][GP])
    assert not parse_filter("called_jam")(hu[44][GP])
    assert parse_filter("faced_jam,called_jam_preflop")(multi[20][GETTING_EVEN])
