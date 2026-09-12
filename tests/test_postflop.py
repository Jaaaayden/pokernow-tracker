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

from pnt.stats.derive import POSTFLOP_STREETS, SIZE_BUCKETS, _seat_ranks, derive, size_bucket
from pnt.stats.filters import parse_filter
from pnt.stats.queries import aggregate, facts_for, hand_list, load_hands
from pnt.stats.ranges import SIZING_KINDS, sizing_tells
from tests.conftest import HU_GAME, MULTIWAY_GAME

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
                # The villain maps answer exactly the opportunities they belong to.
                assert f.faced_cbet_by.keys() == f.fold_to_cbet_opp.keys()
                assert f.donk_into.keys() == f.donk_opp.keys()
                assert f.faced_cbet_by.get(s) != f.pn_id
                assert f.donk_into.get(s) != f.pn_id
            assert f.pn_id not in f.opponents
            if f.in_position is None:
                assert f.pos_order is None and f.pos_players is None
            else:
                assert f.pos_players == len(f.opponents) + 1
                assert 0 <= f.pos_order < f.pos_players
                assert f.in_position == (f.pos_order == f.pos_players - 1)


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


# --- who the action was against, and who closed it ---------------------------


def test_the_villain_is_named(hu):
    """The aggressor behind each opportunity, on the hands worked by hand above."""
    # #10: gp donks the flop into Chris, then c-bets the turn and Chris folds.
    assert hu[10][GP].donk_into == {"flop": CHRIS}
    assert hu[10][CHRIS].faced_cbet_by == {"turn": GP}
    assert hu[10][CHRIS].opponents == (GP,)
    # #18: gp c-bets the flop, and leads the river into Chris, the turn aggressor.
    assert hu[18][CHRIS].faced_cbet_by["flop"] == GP
    assert hu[18][GP].donk_into["river"] == CHRIS
    # #92: limped, so the flop has no aggressor and neither map has a flop key.
    assert "flop" not in hu[92][CHRIS].faced_cbet_by
    assert "flop" not in hu[92][CHRIS].donk_into
    assert hu[92][CHRIS].faced_cbet_by == {"turn": GP, "river": GP}


def test_in_position_heads_up(hu):
    """The button acts first preflop and last postflop -- Chris is BTN/SB here."""
    assert hu[10][CHRIS].seats_from_button == 0
    assert hu[10][CHRIS].in_position is True
    assert (hu[10][CHRIS].pos_order, hu[10][CHRIS].pos_players) == (1, 2)
    assert hu[10][GP].in_position is False
    assert (hu[10][GP].pos_order, hu[10][GP].pos_players) == (0, 2)


def test_in_position_survives_a_dead_button(db):
    """Acting order is known on the hands where the position *label* is not.

    SPEC.md judgement calls 4 and 11: these two hands are barred from positional
    splits and carry no BTN/SB name, but who acts after whom is still a fact.
    """
    hands = {
        h.hand_number: h for h in load_hands(db, MULTIWAY_GAME) if h.hand_number in (25, 26)
    }
    facts = {n: {f.pn_id: f for f in derive(h)} for n, h in hands.items()}

    # #25: a dead small blind pushes d-4X to seats_from_button 3 in a 3-handed pot.
    assert hands[25].blinds_irregular and facts[25]["d-4X_F_SSU"].seats_from_button == 3
    assert facts[25]["d-4X_F_SSU"].in_position is True
    assert facts[25]["MBFczOlpuA"].in_position is False
    # #26: no dealer at all; positions are anchored on the big blind.
    assert hands[26].dead_button
    assert facts[26]["PEMYRVPxOS"].in_position is True
    assert facts[26]["d-4X_F_SSU"].in_position is False

    # The label stays withheld on both, while the order is known.
    for n in (25, 26):
        rows = hand_list(derive(hands[n]))
        assert {r["position"] for r in rows} == {None}
        assert {r["ip"] for r in rows} != {None}


def test_position_signals_agree(db):
    """Acting order and seat order reach the same verdict, off the flagged hands.

    The seat fallback only ever runs where the orbit is incomplete, so this is what
    proves it is the same ordering and not a second, subtly different one. Dead
    buttons are excluded because there the seat labels are the ones that are wrong
    -- see SPEC.md judgement call 11.
    """
    compared = 0
    for h in load_hands(db):
        if h.dead_button or h.blinds_irregular:
            continue
        seat = _seat_ranks(h)
        first, orbit = None, []
        for a in h.actions:
            if a.is_forced or a.street not in POSTFLOP_STREETS:
                continue
            if first is None:
                first = a.street
            if a.street == first and a.pn_id not in orbit:
                orbit.append(a.pn_id)
        for f in derive(h):
            group = [f.pn_id, *f.opponents]
            if f.in_position is None or not all(q in orbit and q in seat for q in group):
                continue
            compared += 1
            assert (max(group, key=lambda q: seat[q]) == f.pn_id) == f.in_position
    assert compared > 500, f"too few comparable rows to mean anything: {compared}"


def test_seat_ranks_do_not_collide_on_a_dead_blind(db):
    """The reason the rank is counted in slots and not `(sfb - 1) % n_dealt_in`.

    Hand #25 is three-handed with seats_from_button 0, 2 and 3, because a dead small
    blind leaves a slot no player occupies. The modulo form maps both 0 and 3 onto
    rank 2, silently making two players the same seat; counting slots keeps them
    apart and in the right order.
    """
    hand = next(h for h in load_hands(db, MULTIWAY_GAME) if h.hand_number == 25)
    sfb = {p.pn_id: p.seats_from_button for p in hand.players.values()}
    assert sorted(sfb.values()) == [0, 2, 3] and hand.n_dealt_in == 3

    naive = [(s - 1) % hand.n_dealt_in for s in sfb.values()]
    assert len(set(naive)) == 2, "the bug this guards against is a rank collision"

    ranks = _seat_ranks(hand)
    assert len(set(ranks.values())) == 3
    # The button posts nothing and acts last postflop; the dead slot sits before it.
    assert max(ranks, key=lambda q: ranks[q]) == next(q for q, v in sfb.items() if v == 0)


def test_aggression_frequency_reports_its_sample(db):
    """AF is the one rate whose denominator counts actions, not hands."""
    facts = facts_for(db, "genericpoker")
    out = aggregate(facts)
    for street in STREETS:
        denom = sum(f.agg_denom.get(street, 0) for f in facts)
        num = sum(f.aggressive.get(street, 0) for f in facts)
        # The sample the page shows on hover has to be the rate's own denominator.
        assert out["_opp"][f"af_{street}"] == denom
        assert out[f"af_{street}"] == (round(100.0 * num / denom, 1) if denom else None)
        # Checks are excluded from both sides, so the denominator cannot exceed
        # the actions actually taken, and one hand may contribute several.
        assert denom >= num
