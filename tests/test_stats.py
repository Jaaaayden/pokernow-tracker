"""Stat definitions, checked against a hand-worked manual count.

Verifying stats by hand is tedious exactly once, and it is the only way to know
the *denominators* are right. A wrong numerator usually looks wrong; a wrong
denominator looks completely plausible forever.
"""

from __future__ import annotations

import pytest

from pnt.stats.derive import derive
from pnt.stats.filters import parse_filter
from pnt.stats.queries import aggregate, load_hands, report
from tests.conftest import HU_GAME

CHRIS = "5NARaPRkSp"

# Worked by hand from the raw log, hands 1-20, for Chris.
#   BB walks (never acted)         : 1, 5, 7, 11, 13, 15, 17
#   acted preflop -> opportunity   : the other 13
#   VPIP                           : 2, 8, 10, 12, 14, 18, 19
#   PFR                            : 10, 14, 18, 19
#   3bet opportunity (faced lvl 2) : 3, 9, 19
#   3bet                           : 19
#   opened then faced a 3bet       : 18   (he called, so did not fold to it)
MANUAL = {
    "vpip_opp": 13,
    "vpip": 7,
    "pfr_opp": 13,
    "pfr": 4,
    "three_bet_opp": 3,
    "three_bet": 1,
    "fold_to_3bet_opp": 1,
    "fold_to_3bet": 0,
}
MANUAL_WALKS = [1, 5, 7, 11, 13, 15, 17]


@pytest.fixture()
def chris_first20(db):
    hands = [h for h in load_hands(db, HU_GAME) if h.hand_number <= 20]
    assert len(hands) == 20
    return {
        h.hand_number: next(f for f in derive(h) if f.pn_id == CHRIS)
        for h in hands
    }


@pytest.mark.parametrize("attr, expected", sorted(MANUAL.items()))
def test_matches_manual_count(chris_first20, attr, expected):
    assert sum(bool(getattr(f, attr)) for f in chris_first20.values()) == expected


def test_bb_walks_are_excluded_from_the_denominator(chris_first20):
    """The big blind in a walk never acts, so the hand is not a VPIP opportunity.

    Counting walks would drag every heads-up player's VPIP down by ~35% here --
    and would look entirely believable.
    """
    walks = sorted(n for n, f in chris_first20.items() if not f.vpip_opp)
    assert walks == MANUAL_WALKS


def test_forced_posts_never_count_as_vpip(chris_first20):
    """Every one of those walk hands still had Chris posting a big blind."""
    for n in MANUAL_WALKS:
        assert chris_first20[n].vpip is False


def test_sb_completing_is_vpip_but_bb_checking_is_not(chris_first20):
    assert chris_first20[2].vpip is True   # SB completed to the BB
    assert chris_first20[3].vpip is False  # folded facing a raise


def test_rates_are_none_not_zero_when_denominator_is_empty():
    """A player with no opportunities has an *unknown* rate, not 0%.

    This is the bug that makes other PokerNow HUDs show `--` everywhere: they
    pre-segment by table size, fragment the denominator, and then cannot tell
    'no data' from 'never does it'.
    """
    from pnt.stats.derive import Facts

    f = Facts(hand_id=1, pn_id="x", n_dealt_in=6, seats_from_button=0, dead_button=False)
    out = aggregate([f])
    assert out["hands"] == 1
    assert out["vpip"] is None
    assert out["3bet"] is None
    assert out["cbet_flop"] is None


def test_showdown_is_not_inferred_from_shows_lines(db):
    """Players voluntarily show after winning uncontested, and rabbit-hunt shows
    appear between hands entirely. WTSD counts live players, not `shows`."""
    hands = load_hands(db, HU_GAME)
    # Hand #134: opponent folds, then the winner shows a card anyway.
    h = next(h for h in hands if h.hand_number == 134)
    assert h.went_to_showdown is False
    assert all(not f.wtsd for f in derive(h))


def test_wsd_is_a_subset_of_wtsd(db):
    for h in load_hands(db):
        for f in derive(h):
            assert not (f.wsd and not f.wtsd)
            assert not (f.wtsd and not f.saw_flop)


def test_cbet_requires_being_the_previous_aggressor(db):
    """A bet on a street that checked through is a probe, not a continuation bet."""
    for h in load_hands(db):
        for f in derive(h):
            for street in ("flop", "turn", "river"):
                if f.cbet.get(street):
                    assert f.cbet_opp.get(street), "cbet without opportunity"


def test_aggregate_denominators_are_reported(db):
    rows = report(db, min_hands=50)
    assert rows
    for r in rows:
        assert r["_opp"]["vpip"] <= r["hands"]


def test_filters_restrict_the_hand_set(db):
    """The Holdem-Manager move: stats *within* a chosen spot."""
    everything = {r["player"]: r for r in report(db)}
    only_3bet = {r["player"]: r for r in report(db, predicate=parse_filter("3bet"))}
    for name, row in only_3bet.items():
        assert row["hands"] < everything[name]["hands"]
        assert row["vpip"] == 100.0, "every 3-bet hand is by definition voluntary"


def test_filter_composition_and_positions(db):
    pred = parse_filter("saw_flop,players=2")
    rows = report(db, predicate=pred, min_hands=1)
    assert rows
    pos = parse_filter("position=BB")
    assert report(db, predicate=pos, min_hands=1)


def test_unknown_filter_term_is_rejected_with_help():
    with pytest.raises(ValueError, match="unknown filter term"):
        parse_filter("definitely_not_a_stat")


def test_filter_vocabulary_covers_every_term():
    """The pages' `?` panel is built from VOCABULARY; a term missing from it is a
    term nobody using the page can discover."""
    import re

    from pnt.stats.filters import FLAGS, NUMERIC, SIZED, STREETS, VOCABULARY

    documented: set[str] = set()
    for _, terms in VOCABULARY:
        for term, example, _ in terms:
            parse_filter(example)  # every example is a working filter
            for s in STREETS:
                documented.add(re.split(r"[!<>=]", term.replace("<street>", s), maxsplit=1)[0])
    for key in [*FLAGS, *NUMERIC, *SIZED]:
        assert key in documented, f"{key} is missing from filters.VOCABULARY"
