"""Line facts, line filters, and the range views built on them.

The sizing facts are pinned to hands worked by hand from the heads-up fixture,
the same way the core stats are: a sizing fact that is off by the blind looks
completely plausible forever.
"""

from __future__ import annotations

import pytest

from pnt.stats.cards import ALL_CLASSES
from pnt.stats.derive import derive
from pnt.stats.filters import parse_filter
from pnt.stats.queries import facts_for, load_hands
from pnt.stats.ranges import composition, range_grid
from tests.conftest import HU_GAME

CHRIS = "5NARaPRkSp"
GP = "gpP9uUffpu"


@pytest.fixture()
def hu(db):
    """{hand_number: {pn_id: Facts}} for the heads-up game."""
    return {
        h.hand_number: {f.pn_id: f for f in derive(h)}
        for h in load_hands(db, HU_GAME)
    }


# Worked by hand from the raw log (bb = 10 throughout):
#   #10  Chris opens to 20, gp calls. gp bets 20 into 40 on the flop, 60 into 80
#        on the turn, Chris folds.
#   #18  Chris opens to 20, gp 3-bets to 60, Chris calls. gp bets 30 into 120 on
#        the flop; turn checks to Chris who bets 45 into 180; gp bets 135 into
#        270 on the river. gp shows Th Tc on 7c 3s Qh 8h Ac.
#   #19  gp opens to 30, Chris 3-bets to 60, gp calls. Flop checks through;
#        Chris bets 50 into 120 on the turn.
#   #92  Chris completes, gp checks -- limped. Flop: Chris bets 10 into 20,
#        gp raises to 40. Turn: gp bets 40 into 100. River: gp bets 60 into 180.


def test_single_raised_pot_facts(hu):
    chris, gp = hu[10][CHRIS], hu[10][GP]
    assert chris.opener and chris.pfa and not gp.opener and not gp.pfa
    assert chris.pot_level == gp.pot_level == 2
    assert chris.open_bb == gp.open_bb == 2.0
    assert chris.pf_raise_bb == 2.0 and gp.pf_raise_bb is None
    assert gp.bet_pot == {"flop": 0.5, "turn": 0.75}
    assert chris.bet_pot == {}


def test_three_bet_pot_facts(hu):
    chris, gp = hu[18][CHRIS], hu[18][GP]
    assert chris.opener and not chris.pfa
    assert gp.pfa and not gp.opener
    assert chris.pot_level == gp.pot_level == 3
    assert chris.open_bb == gp.open_bb == 2.0
    assert gp.pf_raise_bb == 6.0
    assert gp.bet_pot == {"flop": 0.25, "river": 0.5}
    assert chris.bet_pot == {"turn": 0.25}
    assert gp.hole_cards == "ThTc"
    assert gp.board == ("7c", "3s", "Qh", "8h", "Ac")


def test_open_size_is_the_opener_size_not_the_three_bet(hu):
    chris, gp = hu[19][CHRIS], hu[19][GP]
    assert gp.opener and gp.pf_raise_bb == 3.0
    assert chris.three_bet and chris.pf_raise_bb == 6.0
    assert chris.open_bb == gp.open_bb == 3.0
    assert chris.bet_pot == {"turn": round(50 / 120, 3)}


def test_limped_pot_has_no_open(hu):
    chris, gp = hu[92][CHRIS], hu[92][GP]
    assert chris.pot_level == gp.pot_level == 1
    assert chris.open_bb is None and gp.open_bb is None
    assert not chris.opener and not gp.opener and not chris.pfa and not gp.pfa
    assert chris.bet_pot == {"flop": 0.5}
    # gp's flop action was a raise, not a bet, so it is not a bet size
    assert gp.bet_pot == {"turn": 0.4, "river": round(60 / 180, 3)}


def test_pot_level_is_shared_and_opener_is_unique(db):
    for h in load_hands(db):
        fs = derive(h)
        assert len({f.pot_level for f in fs}) == 1
        assert sum(f.opener for f in fs) == (1 if fs[0].pot_level >= 2 else 0)
        assert sum(f.pfa for f in fs) <= 1
        for f in fs:
            if f.opener:
                # Their *last* raise; an opener who 4-bets ends above the open.
                assert f.pf_raise_bb >= f.open_bb
            for frac in f.bet_pot.values():
                assert frac > 0


# ---------------------------------------------------------------- filters ----


def test_line_filter_terms(hu):
    assert parse_filter("opener,open_bb>=2,srp")(hu[10][CHRIS])
    assert not parse_filter("opener,open_bb>=3")(hu[10][CHRIS])
    assert parse_filter("3bet_pot,pfa,raise_bb=6")(hu[18][GP])
    assert parse_filter("bet_river>=0.5")(hu[18][GP])
    assert not parse_filter("bet_river>=1")(hu[18][GP]), "half pot is not an overbet"
    assert parse_filter("limped")(hu[92][CHRIS])
    assert not parse_filter("bet_flop")(hu[92][GP]), "a raise is not a bet"
    assert parse_filter("cards_known")(hu[18][GP])


def test_comparison_against_missing_value_is_false_not_error(hu):
    """A limped pot has no open size; `open_bb<3` must not match it."""
    assert not parse_filter("open_bb<3")(hu[92][CHRIS])
    assert not parse_filter("open_bb>=0")(hu[92][CHRIS])


def test_unknown_numeric_term_still_rejected():
    with pytest.raises(ValueError, match="unknown filter term"):
        parse_filter("open_bbb>=4")


def test_pot_is_the_chips_that_stayed_in_the_middle(hu, db):
    # #10: 20 + 20 preflop, 20 + 20 on the flop; gp's uncalled 60 on the turn goes back.
    chris, gp = hu[10][CHRIS], hu[10][GP]
    assert chris.pot == gp.pot == 80
    for h in load_hands(db):
        fs = derive(h)
        assert len({f.pot for f in fs}) == 1, "one pot per hand"
        assert fs[0].pot == sum(p.contributed for p in h.players.values())


def test_pot_filter_terms(hu):
    chris = hu[10][CHRIS]
    assert parse_filter("pot>=80")(chris)
    assert parse_filter("pot=80")(chris)
    assert not parse_filter("pot>80")(chris)
    assert parse_filter("pot_bb>=8")(chris) and not parse_filter("pot_bb>8")(chris)
    # `pot_bb` is reached through the `pot` prefix without tripping on it
    assert parse_filter("pot_bb<=8,pot<=80")(chris)


def test_vs_filter_names_an_opponent(hu):
    names = {GP: "genericpoker", CHRIS: "Chris"}
    chris, gp = hu[10][CHRIS], hu[10][GP]
    assert parse_filter("vs=genericpoker", names)(chris)
    assert parse_filter("vs=GenericPoker", names)(chris), "names are matched case-insensitively"
    assert not parse_filter("vs=genericpoker", names)(gp), "never against yourself"
    assert parse_filter("vs=Chris", names)(gp)
    assert not parse_filter("vs!=genericpoker", names)(chris)
    # A raw ID works without the map; with it, a name nobody goes by is a typo.
    assert parse_filter(f"vs={GP}")(chris)
    with pytest.raises(ValueError, match="unknown player"):
        parse_filter("vs=nobody", names)
    with pytest.raises(ValueError, match="needs a player name"):
        parse_filter("vs=", names)
    # #1 is a walk for Chris: nobody was still in, so no one was faced.
    assert not parse_filter("vs=genericpoker", names)(hu[1][CHRIS])


# ------------------------------------------------------------------ views ----


def test_range_grid_accounts_for_every_known_hand(db):
    facts = facts_for(db, "Chris")  # an opponent: cards known only when shown
    grid = range_grid(facts)
    assert grid["hands"] == len(facts)
    assert grid["known"] == sum(1 for f in facts if f.hole_cards)
    assert 0 < grid["known"] < grid["hands"]
    assert set(grid["cells"]) == set(ALL_CLASSES)
    assert sum(c["n"] for c in grid["cells"].values()) == grid["known"]
    assert [label for row in grid["rows"] for label in row] == list(ALL_CLASSES)


def test_hero_cards_are_known_on_every_hand(db):
    """Hero's cards are printed every hand, so hero's own coverage is ~total."""
    grid = range_grid(facts_for(db, "genericpoker", HU_GAME))
    assert grid["coverage"] > 95


def test_composition_sums_to_known(db):
    facts = [f for f in facts_for(db, "Chris") if f.saw_flop]
    comp = composition(facts)
    assert sum(c["n"] for c in comp["classes"]) == comp["known"]
    for c in comp["classes"]:
        if "details" in c:
            assert sum(d["n"] for d in c["details"]) == c["n"]
    assert abs(sum(c["pct"] for c in comp["classes"]) - 100) < 0.5
    # strongest first
    order = [c["class"] for c in comp["classes"]]
    assert order.index("pair") > order.index("two_pair")


def test_filtered_line_composition(db):
    """The line stat: what did they have when they took this line?"""
    pred = parse_filter("pfa,srp")
    facts = [f for f in facts_for(db, "Chris") if pred(f)]
    comp = composition(facts)
    assert comp["hands"] == 14 and comp["known"] == 3  # counted from the HU log
    assert comp["known"] < comp["hands"], "the ones that folded out are the unknown ones"


def test_empty_spot_reports_unknown_coverage():
    grid = range_grid([])
    assert grid["hands"] == 0 and grid["coverage"] is None
    assert composition([])["classes"] == []


def test_unknown_alias(db):
    with pytest.raises(ValueError, match="unknown alias"):
        facts_for(db, "nobody")


# --------------------------------------------------------------- textures ----


def test_board_texture_filters(hu):
    # #18: 7c 3s Qh 8h Ac -> flop 7c3sQh is queen-high, rainbow, unpaired, disconnected
    gp = hu[18][GP]
    assert parse_filter("flop=queen_high,flop=rainbow,flop=unpaired")(gp)
    assert parse_filter("flop!=paired")(gp)
    assert not parse_filter("flop=ace_high")(gp)
    # the river card makes the whole board ace-high
    assert parse_filter("river=ace_high")(gp) and parse_filter("board=ace_high")(gp)
    # #19: Qc Tc 2c 4c -> monotone flop, no river dealt
    chris = hu[19][CHRIS]
    assert parse_filter("flop=monotone,flop=flush_possible")(chris)
    assert not parse_filter("river=monotone")(chris), "no river was dealt"
    # #14 ended preflop: no texture matches, positively or negatively
    assert not parse_filter("flop=ace_high")(hu[14][CHRIS])
    assert not parse_filter("flop!=ace_high")(hu[14][CHRIS])


def test_unknown_texture_tag_is_rejected():
    with pytest.raises(ValueError, match="unknown board texture"):
        parse_filter("flop=wet")


def test_texture_filter_narrows_and_composes(db):
    facts = facts_for(db, "Chris")
    flops = [f for f in facts if parse_filter("saw_flop")(f)]
    ace = [f for f in facts if parse_filter("saw_flop,flop=ace_high")(f)]
    assert 0 < len(ace) < len(flops)
    for f in ace:
        assert any(c.startswith("A") for c in f.board[:3])


# ----------------------------------------------------------------- sizing ----


def test_size_stats():
    from pnt.stats.ranges import size_stats

    assert size_stats([]) is None
    s = size_stats([2.0, 3.0, 3.0, 4.0, 10.0])
    assert s == {"n": 5, "min": 2.0, "max": 10.0, "mean": 4.4, "median": 3.0, "mode": 3.0}
    # tie between sizes that genuinely repeat -> the one nearest the median
    assert size_stats([2.0, 2.0, 6.0, 6.0, 3.0])["mode"] == 2.0


def test_no_mode_when_nothing_repeats():
    """3.5bb once and 15bb once has no most-common size; picking one is list order."""
    from pnt.stats.ranges import size_stats

    assert size_stats([3.5, 15.0])["mode"] is None
    assert size_stats([2.0, 3.0, 4.0])["mode"] is None
    assert size_stats([7.0])["mode"] is None
    # one repeat is enough to make a mode real
    assert size_stats([3.5, 3.5, 15.0])["mode"] == 3.5


def test_spot_sizes_cover_every_hand_not_just_shown_ones(db):
    facts = [f for f in facts_for(db, "Chris") if parse_filter("opener")(f)]
    grid = range_grid(facts)
    assert grid["raise"]["n"] == len(facts) > grid["known"]
    for c in grid["cells"].values():
        if c["raised"]:
            assert c["raise"]["n"] == c["raised"]
            assert c["raise"]["min"] <= c["raise"]["median"] <= c["raise"]["max"]
            assert c["raise_bb"] == c["raise"]["median"]


# -------------------------------------------------------------- raise pct ----


def test_raise_pct_is_pfr_over_opportunities(db):
    facts = [f for f in facts_for(db, "Chris") if parse_filter("acted_preflop")(f)]
    grid = range_grid(facts)
    seen = 0
    for c in grid["cells"].values():
        if c["n"] == 0:
            assert c["raise_pct"] is None and c["pf_opp"] == 0
            continue
        assert c["pfr"] <= c["pf_opp"] <= c["n"]
        assert 0 <= c["raise_pct"] <= 100
        seen += 1
    assert seen > 0
    # openers raised by definition
    opened = range_grid([f for f in facts if f.opener])
    assert all(c["raise_pct"] == 100 for c in opened["cells"].values() if c["n"])


def test_walks_are_not_raise_opportunities(hu):
    """Hand #1 is a BB walk for Chris: shown or not, it is not a decision."""
    f = hu[1][CHRIS]
    assert not f.pfr_opp
    from pnt.stats.ranges import _summary

    f.hole_cards = "AsAd"
    assert _summary([f]) | {"pf_opp": 0, "pfr": 0, "raise_pct": None} == _summary([f])


# ------------------------------------------------------- typical raise size ---


def test_typical_size_picks_mean_when_sizes_agree():
    from pnt.stats.ranges import typical_size

    assert typical_size([2.5, 3.0, 3.0, 3.5], 3.0) == (3.0, "mean")
    assert typical_size([], 3.0) == (None, "none")
    assert typical_size([6.7], 3.0) == (6.7, "single")


def test_typical_size_steps_over_a_tilt_jam():
    """One 270bb shove among 3bb opens: the mean is 57bb, the habit is 3bb."""
    from pnt.stats.ranges import typical_size

    jam = [3.0, 3.0, 3.0, 3.0, 3.0, 270.0]
    assert sum(jam) / len(jam) > 40, "the mean really is destroyed by one jam"
    assert typical_size(jam, 3.0) == (3.0, "median")


def test_two_raises_that_disagree_fall_back_to_their_midpoint():
    """Deliberately a size never used: it summarises two contradictory raises."""
    from pnt.stats.ranges import typical_size

    assert typical_size([3.0, 100.0], 3.0) == (51.5, "midpoint")
    # ...but two raises that agree are averaged as usual
    assert typical_size([3.0, 4.0], 3.0) == (3.5, "mean")


def test_tolerance_scales_with_the_spot(db):
    from pnt.stats.ranges import typical_size

    # the same 3bb spread is agreement in a big-blind-heavy game, noise in a small one
    assert typical_size([8.0, 11.0], 10.0)[1] == "mean"
    assert typical_size([8.0, 11.0], 1.0)[1] == "midpoint"


def test_grid_exposes_the_basis_and_a_jam_does_not_move_it(db):
    facts = [f for f in facts_for(db, "genericpoker") if parse_filter("opener")(f)]
    grid = range_grid(facts)
    assert grid["raise_tolerance"] >= 1.0
    for c in grid["cells"].values():
        assert c["raise_basis"] in {"none", "single", "mean", "median", "midpoint"}
        if c["raise_basis"] == "none":
            assert c["raise_typical"] is None and c["raised"] == 0
        if c["raise_typical"] is not None and c["raise"]:
            # a habitual size is always one the player could plausibly have used
            assert c["raise"]["min"] <= c["raise_typical"] <= c["raise"]["max"]
