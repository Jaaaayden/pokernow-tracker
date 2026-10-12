"""The foundation facts: steals, squeezes, cold calls, limp-reraises, delayed c-bets,
probes, floats, straddles, stack depth, WWSF and the confidence-interval helpers.

Synthetic hands are built with `test_lines._hand`, whose seats run SB, BB, ... and
put the button last: four-handed that is SB, BB, CO, BTN, and heads-up the first
player is the big blind and the second the button (who posts the small blind).
bb = 10 throughout.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from pnt.stats.derive import derive
from pnt.stats.filters import FLAGS, parse_filter
from pnt.stats.queries import Tally, load_hands, rates, tally, wilson
from tests.conftest import EDGE_LOGS, STRADDLE_GAME
from tests.test_lines import _hand

STREETS = ("flop", "turn", "river")
BOARD = ("2c", "7d", "Ts", "Jh", "3s")


def _facts(hand) -> dict:
    return {f.pn_id: f for f in derive(hand)}


# --- preflop -------------------------------------------------------------------


def test_button_steal_folded_to_the_big_blind():
    h = _hand(["S", "B", "C", "D"], [
        ("preflop", "S", "post", 5), ("preflop", "B", "post", 10),
        ("preflop", "C", "fold", 0), ("preflop", "D", "raise", 25, 25),
        ("preflop", "S", "fold", 0), ("preflop", "B", "fold", 0),
    ])
    f = _facts(h)
    s, b, c, d = f["S"], f["B"], f["C"], f["D"]
    assert d.position_label == "BTN" and c.position_label == "CO"
    assert d.first_in and d.steal_opp and d.steal and d.opener
    # The cutoff folded first in from a steal seat: an opportunity, not a steal.
    assert c.first_in and c.steal_opp and not c.steal
    assert all(x.steal_open for x in f.values())
    for blind in (s, b):
        assert not blind.first_in
        assert blind.callers_before == 0
        assert blind.fold_to_steal_opp and blind.fold_to_steal and not blind.three_bet_vs_steal
        assert not blind.cold_call_opp, "a blind is never a cold caller"
    assert d.callers_before is None, "the opener never faces the open"
    for term, who in (("steal", {"D"}), ("steal_opp", {"C", "D"}), ("fold_to_steal", {"S", "B"}),
                      ("fold_to_steal_opp", {"S", "B"}), ("first_in", {"C", "D"})):
        assert {x.pn_id for x in f.values() if parse_filter(term)(x)} == who, term


def test_a_steal_needs_trusted_seats():
    h = _hand(["S", "B", "C", "D"], [
        ("preflop", "S", "post", 5), ("preflop", "B", "post", 10),
        ("preflop", "C", "fold", 0), ("preflop", "D", "raise", 25, 25),
        ("preflop", "S", "fold", 0), ("preflop", "B", "fold", 0),
    ])
    for flag in ("dead_button", "blinds_irregular"):
        setattr(h, flag, True)
        f = _facts(h)
        assert not f["D"].steal and not f["D"].steal_open and not f["B"].fold_to_steal_opp, flag
        assert f["D"].first_in, "first in is about the action, not the seat"
        setattr(h, flag, False)


def test_open_call_then_squeeze():
    h = _hand(["S", "B", "C", "D"], [
        ("preflop", "S", "post", 5), ("preflop", "B", "post", 10),
        ("preflop", "C", "raise", 25, 25), ("preflop", "D", "call", 25, 25),
        ("preflop", "S", "raise", 95, 100), ("preflop", "B", "fold", 0),
        ("preflop", "C", "fold", 0), ("preflop", "D", "fold", 0),
    ])
    f = _facts(h)
    s, b, c, d = f["S"], f["B"], f["C"], f["D"]
    assert c.steal and s.steal_open, "the open was a steal..."
    assert not s.fold_to_steal_opp, "...but one called it, so the blind faced more than a steal"
    assert s.callers_before == 1 and s.squeeze_opp and s.squeeze and s.three_bet
    assert d.callers_before == 0 and d.cold_call_opp and d.cold_call and not d.squeeze_opp
    # The big blind's first decision was facing the squeeze, never the open itself.
    assert b.callers_before is None and not b.squeeze_opp and 2 not in b.pf_faced
    assert parse_filter("callers_before>=1")(s) and not parse_filter("callers_before>=1")(d)
    assert parse_filter("squeeze")(s) and parse_filter("cold_call")(d)


def test_limp_reraise():
    h = _hand(["S", "B", "C", "D"], [
        ("preflop", "S", "post", 5), ("preflop", "B", "post", 10),
        ("preflop", "C", "call", 10, 10), ("preflop", "D", "fold", 0),
        ("preflop", "S", "raise", 35, 40), ("preflop", "B", "fold", 0),
        ("preflop", "C", "raise", 110, 120), ("preflop", "S", "fold", 0),
    ])
    f = _facts(h)
    c, s, d = f["C"], f["S"], f["D"]
    assert c.limp and c.limp_raise and not c.limp_call and not c.limp_fold
    assert c.three_bet and c.callers_before == 0 and not c.cold_call_opp, "a limper is not cold"
    assert not s.first_in and not s.steal_opp, "a limp in front is not first in"
    assert d.first_in is False
    assert parse_filter("limp,faced_open=raise")(c)
    r = rates(tally(f.values()))
    assert r["limp_reraise"] == 100.0 and r["_opp"]["limp_reraise"] == 1


def test_a_straddle_is_forced_and_moves_no_level():
    h = _hand(["S", "B", "C", "D"], [
        ("preflop", "S", "post", 5), ("preflop", "B", "post", 10), ("preflop", "C", "post", 20),
        ("preflop", "D", "raise", 60, 60), ("preflop", "S", "fold", 0), ("preflop", "B", "fold", 0),
        ("preflop", "C", "call", 40, 60),
    ], board=BOARD[:3])
    h.actions[2].post_kind = "straddle"
    f = _facts(h)
    assert all(x.straddled for x in f.values())
    assert all(x.pot_level == 2 for x in f.values()), "the open is still level 2"
    d, c = f["D"], f["C"]
    assert d.first_in and d.steal and d.open_bb == 6.0
    assert c.posted_blind and not c.cold_call_opp, "the straddler has chips in"
    assert c.callers_before == 0 and not c.fold_to_steal_opp, "the straddle seat is not a blind"
    assert f["S"].posted_blind and f["B"].posted_blind and not d.posted_blind
    assert all(x.players_flop == 2 for x in f.values())
    h.actions[2].post_kind = None
    assert not any(x.straddled for x in derive(h))


def test_antes_are_not_blinds():
    h = _hand(["S", "B", "C"], [
        ("preflop", "C", "post", 1), ("preflop", "S", "post", 5), ("preflop", "B", "post", 10),
        ("preflop", "C", "raise", 30, 30), ("preflop", "S", "fold", 0), ("preflop", "B", "call", 20, 30),
    ])
    h.actions[0].post_kind = "ante"
    f = _facts(h)
    assert not f["C"].posted_blind and f["S"].posted_blind and f["B"].posted_blind


# --- after the flop --------------------------------------------------------------

#: Heads-up: B is the button and posts the small blind, A the big blind. B opens
#: to 30 and A calls, so B is the preflop aggressor and A acts first after the flop.
_HU_OPEN = [
    ("preflop", "B", "post", 5), ("preflop", "A", "post", 10),
    ("preflop", "B", "raise", 25, 30), ("preflop", "A", "call", 20, 30),
]


def test_delayed_cbet_after_a_checked_flop():
    h = _hand(["A", "B"], [
        *_HU_OPEN,
        ("flop", "A", "check", 0), ("flop", "B", "check", 0),
        ("turn", "A", "check", 0), ("turn", "B", "bet", 30, 30), ("turn", "A", "call", 30, 30),
    ], board=BOARD[:4])
    f = _facts(h)
    a, b = f["A"], f["B"]
    assert b.pfa and b.check_back == {"flop": True}
    assert b.delayed_cbet_opp == {"turn": True} and b.delayed_cbet == {"turn": True}
    assert b.cbet_opp == {"flop": True} and "turn" not in b.cbet_opp
    # A checked the turn into the aggressor who had checked back: a probe chance, declined.
    assert a.probe_opp == {"turn": True} and a.probe == {}
    assert a.fold_to_probe_opp == {} and a.float_opp == {}
    assert parse_filter("delayed_cbet_turn")(b) and parse_filter("delayed_cbet")(b)
    assert parse_filter("probe_turn_opp")(a) and not parse_filter("probe")(a)
    # The steal and its defense, heads-up.
    assert b.position_label == "BTN/SB" and b.steal and a.fold_to_steal_opp and not a.fold_to_steal


def test_probe_and_fold_to_probe():
    h = _hand(["A", "B"], [
        *_HU_OPEN,
        ("flop", "A", "check", 0), ("flop", "B", "check", 0),
        ("turn", "A", "bet", 20, 20), ("turn", "B", "fold", 0),
    ], board=BOARD[:4])
    f = _facts(h)
    a, b = f["A"], f["B"]
    assert a.probe_opp == {"turn": True} and a.probe == {"turn": True} and not a.pfa
    assert b.fold_to_probe_opp == {"turn": True} and b.fold_to_probe == {"turn": True}
    assert b.delayed_cbet_opp == {}, "the probe came first: no bet left to make"
    assert a.donk_opp == {"flop": True}, "after a checked street there is no aggressor to lead into"
    assert parse_filter("probe_turn")(a) and parse_filter("folded_to_probe_turn")(b)
    assert parse_filter("faced_probe")(b)


def test_float_after_the_aggressor_gives_up():
    # B limps, A raises from the big blind and is the aggressor, out of position.
    # A c-bets the flop, B calls, A checks the turn and B bets.
    h = _hand(["A", "B"], [
        ("preflop", "B", "post", 5), ("preflop", "A", "post", 10),
        ("preflop", "B", "call", 5, 10), ("preflop", "A", "raise", 30, 40),
        ("preflop", "B", "call", 30, 40),
        ("flop", "A", "bet", 40, 40), ("flop", "B", "call", 40, 40),
        ("turn", "A", "check", 0), ("turn", "B", "bet", 60, 60), ("turn", "A", "fold", 0),
    ], board=BOARD[:4])
    f = _facts(h)
    a, b = f["A"], f["B"]
    assert a.pfa and a.cbet == {"flop": True} and a.cbet_opp == {"flop": True, "turn": True}
    assert b.float_opp == {"turn": True} and b.float_bet == {"turn": True}
    assert b.donk_opp == {}, "the aggressor had acted: a float, not a lead"
    assert b.limp_call and not b.limp_raise
    assert parse_filter("float_turn")(b) and parse_filter("float")(b) and parse_filter("float_turn_opp")(b)


def test_float_on_the_flop():
    # The big blind 3-bets the button's open, so the aggressor acts first after
    # the flop. They check, and the button, who called the 3-bet, bets.
    h = _hand(["A", "B"], [
        ("preflop", "B", "post", 5), ("preflop", "A", "post", 10),
        ("preflop", "B", "raise", 25, 30), ("preflop", "A", "raise", 60, 90),
        ("preflop", "B", "call", 60, 90),
        ("flop", "A", "check", 0), ("flop", "B", "bet", 90, 90), ("flop", "A", "fold", 0),
    ], board=BOARD[:3])
    f = _facts(h)
    assert f["A"].pfa and f["A"].cbet_opp == {"flop": True} and not f["A"].cbet
    assert f["B"].float_opp == {"flop": True} and f["B"].float_bet == {"flop": True}
    assert f["B"].donk_opp == {}


# --- stacks and the result ------------------------------------------------------


def _with_stacks(h, stacks):
    for pid, s in stacks.items():
        h.players[pid].starting_stack = s
    return h


def test_short_and_deep_stacks():
    h = _with_stacks(_hand(["A", "B"], [
        *_HU_OPEN, ("flop", "A", "check", 0), ("flop", "B", "check", 0),
    ], board=BOARD[:3]), {"A": 300, "B": 2500})
    f = _facts(h)
    assert f["A"].eff_bb == f["B"].eff_bb == 30.0
    # 270 behind for A, 2470 for B, into 60 on the flop.
    assert f["A"].pot_at["flop"] == 60 and f["A"].spr_flop == f["B"].spr_flop == 4.5
    assert parse_filter("eff_bb<40")(f["A"]) and parse_filter("spr_flop<=4.5")(f["B"])

    three = _with_stacks(_hand(["A", "B", "C"], [
        ("preflop", "A", "post", 5), ("preflop", "B", "post", 10),
        ("preflop", "C", "fold", 0), ("preflop", "A", "call", 5, 10), ("preflop", "B", "check", 0),
    ]), {"A": 300, "B": 2500, "C": 1500})
    f = _facts(three)
    assert (f["A"].eff_bb, f["B"].eff_bb, f["C"].eff_bb) == (30.0, 150.0, 150.0)
    assert all(x.spr_flop is None for x in f.values()), "no flop, no SPR"
    assert all(x.players_flop is None for x in f.values())


def test_an_unknown_stack_is_unknown():
    h = _with_stacks(_hand(["A", "B", "C"], [
        ("preflop", "A", "post", 5), ("preflop", "B", "post", 10), ("preflop", "C", "fold", 0),
        ("preflop", "A", "call", 5, 10), ("preflop", "B", "check", 0),
        ("flop", "A", "check", 0), ("flop", "B", "check", 0),
    ], board=BOARD[:3]), {"A": 300, "B": 2500, "C": None})
    f = _facts(h)
    assert all(x.eff_bb is None for x in f.values()), "judgement call 26"
    # C folded preflop, so the flop players' stacks are all known.
    assert f["A"].spr_flop == round(290 / 20, 2) and f["C"].spr_flop is None
    assert not parse_filter("eff_bb<1000")(f["A"]) and not parse_filter("eff_bb>=0")(f["A"])


def test_won_pot_is_the_pot_not_the_bounty():
    h = _hand(["A", "B"], [*_HU_OPEN, ("flop", "A", "bet", 30, 30), ("flop", "B", "fold", 0)],
              board=BOARD[:3])
    h.players["A"].collected = 90
    h.players["B"].bounty = 20
    f = _facts(h)
    assert f["A"].won_pot and f["A"].wwsf and f["A"].wwsf_nsd
    assert not f["B"].won_pot, "a bounty paid to them is not the pot"
    r = rates(tally([f["A"]]))
    assert r["wwsf"] == r["wwsf_nsd"] == 100.0 and r["_opp"]["wwsf"] == 1


# --- Tally, rates and intervals --------------------------------------------------


def test_bb_sq_adds_across_iadd():
    h1 = _hand(["A", "B"], [*_HU_OPEN, ("flop", "A", "check", 0), ("flop", "B", "check", 0)],
               board=BOARD[:3])
    h1.players["A"].collected = 60
    h2 = _hand(["A", "B"], [("preflop", "B", "post", 5), ("preflop", "A", "post", 10),
                            ("preflop", "B", "fold", 0)])
    h2.players["A"].collected = 15
    facts = [*derive(h1), *derive(h2)]
    whole = tally(facts)
    parts = tally(facts[:2])
    parts += tally(facts[2:])
    assert parts.bb_sq == whole.bb_sq
    expected = sum(Fraction(f.net, f.bb_size) ** 2 for f in facts)
    assert whole.bb_sq == expected and isinstance(whole.bb_sq, Fraction)
    r = rates(whole)
    assert r["bb_per_100_ci"] is not None and r["bb_per_100_ci"] > 0
    one = Tally()
    one.add(facts[0])
    assert rates(one)["bb_per_100_ci"] is None, "one hand has no spread"


def test_wilson():
    assert wilson(0, 0) is None
    lo, hi = wilson(5, 10)
    assert lo == pytest.approx(0.237, abs=1e-3) and hi == pytest.approx(0.763, abs=1e-3)
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and 0 < hi < 0.35
    lo, hi = wilson(10, 10)
    assert hi == 1.0 and lo > 0.65


# --- invariants over every fixture hand ------------------------------------------


@pytest.fixture()
def every_hand(db):
    from pnt.ingest.importer import import_csv

    for path in EDGE_LOGS:
        import_csv(db, path)
    return load_hands(db)


def test_invariants_on_every_fixture_hand(every_hand):
    n_steal = n_squeeze = n_probe_opp = n_float_opp = n_delayed_opp = 0
    for hand in every_hand:
        facts = derive(hand)
        seen = sum(f.saw_flop for f in facts)
        checked_flop = any(f.check_back.get("flop") for f in facts)
        for f in facts:
            assert f.players_flop == (seen if hand.saw_flop else None)
            if f.steal:
                n_steal += 1
                assert f.opener and f.pfr
            if f.squeeze:
                n_squeeze += 1
                assert f.three_bet
            if f.fold_to_steal_opp or f.cold_call_opp or f.squeeze_opp:
                assert 2 in f.pf_faced
            for s in STREETS:
                if f.probe.get(s):
                    assert not f.pfa
                if f.delayed_cbet.get(s):
                    assert f.pfa and checked_flop
                n_probe_opp += bool(f.probe_opp.get(s))
                n_float_opp += bool(f.float_opp.get(s))
                n_delayed_opp += bool(f.delayed_cbet_opp.get(s))
                # The families after a missed c-bet never share a street with a
                # c-bet or a lead, nor with each other.
                opps = [f.cbet_opp.get(s), f.donk_opp.get(s), f.delayed_cbet_opp.get(s),
                        f.probe_opp.get(s), f.float_opp.get(s)]
                assert sum(bool(o) for o in opps) <= 1, (hand.hand_id, f.pn_id, s, opps)
                for opp, did in (("delayed_cbet_opp", "delayed_cbet"), ("probe_opp", "probe"),
                                 ("fold_to_probe_opp", "fold_to_probe"), ("float_opp", "float_bet")):
                    if getattr(f, did).get(s):
                        assert getattr(f, opp).get(s)
            assert not f.delayed_cbet_opp.get("flop") and not f.probe_opp.get("flop")
    # The corpus exercises every family.
    assert n_steal and n_squeeze and n_probe_opp and n_float_opp and n_delayed_opp


def test_every_rate_reproduces_from_its_filter(every_hand):
    """Each new rate's denominator, and its numerator, is a filter's hand count."""
    facts = [f for h in every_hand for f in derive(h)]
    r = rates(tally(facts))

    def n(expr: str) -> int:
        p = parse_filter(expr)
        return sum(1 for f in facts if p(f))

    pairs = {
        "ats": ("steal", "steal_opp"),
        "fold_to_steal": ("fold_to_steal", "fold_to_steal_opp"),
        "3bet_vs_steal": ("3bet_vs_steal", "fold_to_steal_opp"),
        "squeeze": ("squeeze", "squeeze_opp"),
        "cold_call": ("cold_call", "cold_call_opp"),
        "limp_reraise": ("limp,faced_open=raise", "limp,faced_open"),
        "jam_pf": ("jam_preflop", "acted_preflop"),
        "wwsf": ("saw_flop,won_pot", "saw_flop"),
        "wwsf_nsd": ("saw_flop,won_pot,!wtsd", "saw_flop"),
    }
    for s in ("turn", "river"):
        pairs[f"delayed_cbet_{s}"] = (f"delayed_cbet_{s}", f"delayed_cbet_{s}_opp")
        pairs[f"probe_{s}"] = (f"probe_{s}", f"probe_{s}_opp")
        pairs[f"fold_to_probe_{s}"] = (f"folded_to_probe_{s}", f"faced_probe_{s}")
    for s in STREETS:
        pairs[f"float_{s}"] = (f"float_{s}", f"float_{s}_opp")
    for key, (num, den) in pairs.items():
        assert r["_opp"][key] == n(den), key
        expected = round(100.0 * n(num) / n(den), 1) if n(den) else None
        assert r[key] == expected, key
    assert r["wwsf_nsd"] <= r["wwsf"]


def test_wwsf_nsd_never_exceeds_wwsf_per_player(every_hand):
    by_player: dict[str, list] = {}
    for h in every_hand:
        for f in derive(h):
            by_player.setdefault(f.pn_id, []).append(f)
    for pid, facts in by_player.items():
        r = rates(tally(facts))
        if r["wwsf"] is not None:
            assert r["wwsf_nsd"] <= r["wwsf"], pid


def test_straddle_fixture(db):
    from pnt.ingest.importer import import_csv
    from tests.conftest import STRADDLE

    import_csv(db, STRADDLE)
    hands = load_hands(db, STRADDLE_GAME)
    straddled = [h for h in hands if any(a.post_kind == "straddle" for a in h.actions)]
    assert straddled, "the straddle log has straddles"
    for h in straddled:
        facts = derive(h)
        assert all(f.straddled for f in facts)
        raised = any(a.street == "preflop" and not a.is_forced and a.kind in ("bet", "raise")
                     for a in h.actions)
        if not raised:
            assert all(f.pot_level == 1 for f in facts), "the straddle is level 1"
    assert any(not f.straddled for h in hands for f in derive(h))


def test_fold_street_term():
    h = _hand(["A", "B"], [*_HU_OPEN, ("flop", "A", "bet", 30, 30), ("flop", "B", "fold", 0)],
              board=BOARD[:3])
    f = _facts(h)
    assert parse_filter("fold_street=flop")(f["B"]) and not parse_filter("fold_street=flop")(f["A"])
    with pytest.raises(ValueError, match="fold_street"):
        parse_filter("fold_street=sometime")


def test_every_new_flag_parses():
    for term in ("straddled", "won_pot", "first_in", "steal_opp", "steal", "fold_to_steal_opp",
                 "fold_to_steal", "3bet_vs_steal", "squeeze_opp", "squeeze", "cold_call_opp",
                 "cold_call", "delayed_cbet", "delayed_cbet_opp", "probe", "probe_opp",
                 "faced_probe", "folded_to_probe", "float", "float_opp"):
        assert term in FLAGS, term
    for s in ("turn", "river"):
        for t in (f"delayed_cbet_{s}", f"delayed_cbet_{s}_opp", f"probe_{s}", f"probe_{s}_opp",
                  f"faced_probe_{s}", f"folded_to_probe_{s}"):
            assert t in FLAGS, t
    for s in STREETS:
        assert f"float_{s}" in FLAGS and f"float_{s}_opp" in FLAGS
    assert "probe_flop" not in FLAGS and "delayed_cbet_flop" not in FLAGS
    for expr in ("players_flop>=3", "players>=3", "eff_bb<40", "spr_flop<=3", "callers_before>=1"):
        parse_filter(expr)
