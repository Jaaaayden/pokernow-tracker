"""Decision points: a player's path through a hand as filters, and the widening
that finds the closest spot with data behind it.

Pinned to the heads-up hands `test_postflop.py` works by hand (bb = 10):

  #10  Chris opens, gp calls. Flop: gp leads 20 [40] -- a donk; Chris calls.
       Turn: gp c-bets 60 [80], 3/4 pot; Chris folds.
  #18  Chris opens, gp 3-bets, Chris calls. Flop: gp c-bets 30 [120]; Chris calls.
       Turn: gp checks, Chris bets 45, gp calls. River: gp leads 135 [270].
  #92  Limped. Flop: gp checks, Chris bets 10, gp raises to 40, Chris calls -- no
       aggressor came in, so none of it is a c-bet or a donk. gp c-bets the turn
       and river; Chris raises the river c-bet.
"""

from __future__ import annotations

import pytest

from pnt.stats.derive import Facts, derive
from pnt.stats.filters import parse_filter
from pnt.stats.live import hand_row
from pnt.stats.nodes import (
    BUCKETS,
    Node,
    candidates,
    decision_of,
    decisions,
    nodes_for,
    resolve,
    showings,
    texture_terms,
)
from pnt.stats.queries import load_hands
from tests.conftest import HU_GAME

CHRIS = "5NARaPRkSp"
GP = "gpP9uUffpu"


@pytest.fixture()
def hu(db):
    return {h.hand_number: h for h in load_hands(db, HU_GAME)}


def _walk(hand, pid):
    return [(n.label, n.decision, n.filter) for n in nodes_for(hand)[pid]]


# --- the new preflop fact ---------------------------------------------------


def test_pf_faced_records_each_level_once(hu):
    f10 = {f.pn_id: f for f in derive(hu[10])}
    f18 = {f.pn_id: f for f in derive(hu[18])}
    assert f10[CHRIS].pf_faced == {1: "raise"}
    assert f10[GP].pf_faced == {2: "call"}
    assert f18[CHRIS].pf_faced == {1: "raise", 3: "call"}
    assert f18[GP].pf_faced == {2: "raise"}


def test_decision_terms_agree_with_the_stats_they_shadow(parsed_every):
    """`faced_open` is `3bet_opp` by another name, and an open is a raise at level 1."""
    same = [("faced_open", "3bet_opp"), ("unopened=raise", "opener"), ("faced_open=raise", "3bet")]
    for result in parsed_every.values():
        for parsed in result.hands:
            for f in derive(hand_row(parsed)):
                for a, b in same:
                    assert parse_filter(a)(f) == parse_filter(b)(f), (a, b, parsed.hand_number)
                assert not (parse_filter("limp")(f) and not f.vpip)
                assert not (parse_filter("called_open")(f) and f.three_bet)
                assert not (parse_filter("4bet")(f) and not parse_filter("faced_3bet_any")(f))


def test_decision_values_are_validated():
    assert parse_filter("faced_open=call")
    with pytest.raises(ValueError, match="unknown decision"):
        parse_filter("faced_open=lol")


def test_faced_3bet_decision_is_the_openers_alone(hu):
    facts = {f.pn_id: f for f in derive(hu[18])}
    assert parse_filter("faced_3bet=call")(facts[CHRIS])
    assert parse_filter("faced_3bet_any=call")(facts[CHRIS])
    # gp made the 3-bet, so faced none.
    assert not parse_filter("faced_3bet_any")(facts[GP])


# --- the walk ---------------------------------------------------------------


def test_hand_18_walk(hu):
    assert _walk(hu[18], CHRIS) == [
        ("unopened", "raise", "position=BTN/SB,unopened"),
        ("facing 3bet", "call", "position=BTN/SB,opener,faced_3bet"),
        ("facing flop c-bet (small)", "call",
         "position=BTN/SB,opener,faced_3bet=call,3bet_pot,players_flop=2,faced_cbet_flop=small"),
        # gp, the flop's aggressor, checked the turn to the flop caller: a float.
        ("turn float spot", "bet",
         "position=BTN/SB,opener,faced_3bet=call,3bet_pot,players_flop=2,called_cbet_flop,float_turn_opp"),
        ("facing river bet", "call", None),
    ]
    assert _walk(hu[18], GP) == [
        ("facing open", "raise", "position=BB,faced_open"),
        ("flop c-bet spot", "bet", "position=BB,3bet,3bet_pot,players_flop=2,cbet_flop_opp"),
        ("turn c-bet spot", "check", "position=BB,3bet,3bet_pot,players_flop=2,cbet_flop=small,cbet_turn_opp"),
        ("facing turn bet", "call", None),
        ("river lead spot", "bet", "position=BB,3bet,3bet_pot,players_flop=2,cbet_flop=small,donk_river_opp"),
    ]


def test_hand_10_walk(hu):
    assert _walk(hu[10], GP) == [
        ("facing open", "call", "position=BB,faced_open"),
        ("flop lead spot", "bet", "position=BB,called_open,srp,players_flop=2,donk_flop_opp"),
        ("turn c-bet spot", "bet", "position=BB,called_open,srp,players_flop=2,donk_flop,cbet_turn_opp"),
    ]
    assert _walk(hu[10], CHRIS)[-1] == (
        "facing turn c-bet (large)", "fold", "position=BTN/SB,opener,srp,players_flop=2,faced_cbet_turn=large",
    )


def test_hand_92_limped_pot_has_untracked_flop(hu):
    chris = _walk(hu[92], CHRIS)
    assert chris[0] == ("unopened", "call", "position=BTN/SB,unopened")
    assert chris[1] == ("checked to (flop)", "bet", None)
    assert chris[2] == ("facing flop raise", "call", None)
    assert chris[3][2] == "position=BTN/SB,limp,limped,players_flop=2,faced_cbet_turn=small"
    assert chris[4][2] == "position=BTN/SB,limp,limped,players_flop=2,called_cbet_turn,faced_cbet_river=small"
    gp = _walk(hu[92], GP)
    assert gp[0] == ("unopened", "check", "position=BB,unopened")
    assert gp[1] == ("first in (flop)", "check", None)
    assert gp[3][2] == "position=BB,limped,players_flop=2,cbet_turn_opp"


def test_every_node_satisfies_its_own_filter(parsed_every):
    """The whole point: a node names the facts derive() sets on the finished hand.

    Every decided node's filter, and every history term it adds, must hold on that
    player's own Facts row. This is what keeps nodes.py and derive.py agreeing.
    """
    checked = 0
    for result in parsed_every.values():
        for parsed in result.hands:
            if not parsed.complete:
                continue
            hand = hand_row(parsed)
            facts = {f.pn_id: f for f in derive(hand)}
            for pid, path in nodes_for(hand).items():
                for node in path:
                    assert node.decision is not None
                    terms = [*node.pf_history, *node.post_history]
                    if node.filter:
                        terms.append(node.filter)
                    for term in terms:
                        assert parse_filter(term)(facts[pid]), (parsed.hand_number, pid, term)
                        checked += 1
    assert checked > 1000


def test_pending_node_is_the_spot_before_the_action(hu):
    """Cut #18 after gp's 3-bet: Chris is to act facing it."""
    hand = hu[18]
    cut = hand_row_cut(hand, 4)  # sb, bb, open, 3bet
    paths = nodes_for(cut, pending_for=CHRIS)
    pending = paths[CHRIS][-1]
    assert pending.decision is None
    assert pending.filter == "position=BTN/SB,opener,faced_3bet"
    assert [n.label for n in paths[CHRIS]] == ["unopened", "facing 3bet"]


def hand_row_cut(hand, n_actions):
    """A stored hand truncated to its first `n_actions` actions, board included."""
    from dataclasses import replace

    actions = hand.actions[:n_actions]
    order = ["preflop", "flop", "turn", "river"]
    furthest = max((a.street for a in actions), key=order.index, default="preflop")
    board = hand.board[: {"flop": 3, "turn": 4, "river": 5}.get(furthest, 0)]
    return replace(hand, actions=actions, board=board, complete=False)


# --- widening ---------------------------------------------------------------


def _node(**kw) -> Node:
    base = {"kind": "preflop", "street": "preflop", "level": None, "label": "", "position": None,
            "pf_history": [], "pot": None, "post_history": [], "situation": None}
    return Node(**{**base, **kw})


def test_preflop_ladder_walks_up_the_path():
    node = _node(level=5, position="position=BTN", pf_history=["opener", "4bet"],
                 situation="faced_5bet")
    assert [c[0] for c in candidates(node)] == [
        "position=BTN,opener,4bet,faced_5bet",
        "opener,4bet,faced_5bet",
        "position=BTN,opener,faced_3bet",
        "opener,faced_3bet",
        "position=BTN,unopened",
        "unopened",
    ]
    _, notes, key = candidates(node)[2]
    assert notes == ["their facing 3bet spot"] and key == ("preflop", 3)
    assert candidates(node)[3][1] == ["their facing 3bet spot", "any position"]


def test_preflop_parent_of_a_cold_4bet_is_faced_3bet_any():
    node = _node(level=5, pf_history=["called_open", "4bet"], situation="faced_5bet")
    assert candidates(node)[1][0] == "called_open,faced_3bet_any"
    assert candidates(node)[2][0] == "faced_open"


def test_postflop_ladder():
    node = _node(kind="faced_cbet", street="flop", position="position=BTN",
                 pf_history=["opener", "faced_3bet=call"], pot="3bet_pot",
                 post_history=[], situation="faced_cbet_flop=small")
    assert [c[0] for c in candidates(node)] == [
        "position=BTN,opener,faced_3bet=call,3bet_pot,faced_cbet_flop=small",
        "position=BTN,opener,faced_3bet=call,3bet_pot,faced_cbet_flop",
        "opener,faced_3bet=call,3bet_pot,faced_cbet_flop",
        "3bet_pot,faced_cbet_flop",
        "srp,faced_cbet_flop",
        "faced_cbet_flop",
    ]
    assert candidates(node)[-1][1] == [
        "any size", "any position", "any preflop line", "single-raised pots", "any pot type",
    ]


def test_untracked_node_has_no_candidates():
    assert candidates(_node(kind=None, street="flop", situation=None)) == []


def _fact(**kw) -> Facts:
    """A 6-max Facts row seated under the gun, so `position=BTN` never matches."""
    kw.setdefault("seats_from_button", 3)
    return Facts(hand_id=0, pn_id="x", n_dealt_in=6, dead_button=False, **kw)


def test_resolve_takes_the_first_rung_with_enough_hands():
    facts = [
        _fact(pf_faced={1: "raise", 3: "fold"}, opener=True, fold_to_3bet_opp=True, fold_to_3bet=True),
        _fact(pf_faced={1: "raise", 3: "call"}, opener=True, fold_to_3bet_opp=True),
        _fact(pf_faced={1: "raise"}, opener=True),
    ]
    node = _node(level=5, position="position=BTN", pf_history=["opener", "4bet"],
                 situation="faced_5bet")
    got = resolve(facts, [node])
    assert got["filter"] == "opener,faced_3bet"
    assert got["hands"] == 2 and not got["exact"]
    assert got["relaxed"] == ["their facing 3bet spot", "any position"]
    assert got["decisions"] == {"fold": 1, "call": 1, "raise": 0}
    assert got["label"] == "facing 3bet"

    assert resolve(facts, [node], min_hands=3)["filter"] == "unopened"
    assert resolve(facts, [node], min_hands=4) is None


def test_resolve_exact_hit_is_marked_exact():
    facts = [_fact(pf_faced={2: "call"}, seats_from_button=2)]
    node = _node(level=2, position="position=BB", situation="faced_open")
    got = resolve(facts, [node])
    assert got["exact"] and got["relaxed"] == [] and got["filter"] == "position=BB,faced_open"


def test_resolve_ignores_incomplete_hands():
    facts = [_fact(pf_faced={2: "call"}, complete=False)]
    assert resolve(facts, [_node(level=2, situation="faced_open")]) is None


def test_untracked_node_resolves_from_its_nearest_ancestor():
    facts = [_fact(pf_faced={1: "raise"}, opener=True)]
    path = [
        _node(level=1, label="unopened", situation="unopened", decision="raise"),
        _node(kind=None, street="flop", label="facing flop bet", situation=None),
    ]
    got = resolve(facts, path)
    assert got["filter"] == "unopened"
    assert got["relaxed"] == ["facing flop bet is not a tracked spot; showing unopened"]
    assert resolve(facts, [path[1]]) is None


def test_decisions_per_kind():
    f = _fact(cbet_opp={"flop": True}, cbet={"flop": True},
              fold_to_cbet_opp={"turn": True}, raise_cbet={"turn": True},
              donk_opp={"river": True})
    assert decisions([f], ("cbet", "flop")) == {"bet": 1, "check": 0}
    assert decisions([f], ("faced_cbet", "turn")) == {"fold": 0, "call": 0, "raise": 1}
    assert decisions([f], ("donk", "river")) == {"bet": 0, "check": 1}
    assert decision_of(f, ("cbet", "flop")) == "bet"
    assert decision_of(f, ("faced_cbet", "turn")) == "raise"
    assert decision_of(f, ("donk", "river")) == "check"
    assert decision_of(f, ("cbet", "turn")) is None, "never first in on the turn"
    assert decision_of(f, ("preflop", 2)) is None
    assert decisions([], ("preflop", 2)) == {"fold": 0, "call": 0, "raise": 0}
    assert decisions([], ("nothing", "flop")) == {}


def test_decision_of_agrees_with_the_walk(parsed_every):
    """Every decided node on a finished hand's path is the decision `decision_of`
    reads off that player's Facts at the node's key: the walk and derive agree.

    The one translation: PokerNow lets a player fold with no bet in front of
    them, and at a c-bet, lead, delayed c-bet, probe or float spot that has
    always counted as declining to bet -- a check."""
    checked = 0
    for result in parsed_every.values():
        for parsed in result.hands:
            row = hand_row(parsed)
            facts = {f.pn_id: f for f in derive(row)}
            for pid, path in nodes_for(row).items():
                for node in path:
                    if node.key is None or node.decision is None:
                        continue
                    want = node.decision
                    if node.kind in ("cbet", "donk", "delayed_cbet", "probe", "float") and want == "fold":
                        want = "check"
                    assert decision_of(facts[pid], node.key) == want, (parsed.hand_number, pid, node)
                    checked += 1
    assert checked > 1000


# --- what they showed up with -----------------------------------------------

FLOP = ("Ac", "7c", "2d")


def _shown(hole, board=FLOP, **kw) -> Facts:
    return _fact(hole_cards=hole, board=board, fold_to_cbet_opp={"flop": True}, **kw)


def test_showings_bucket_each_decision_on_the_streets_board():
    facts = [
        _shown("AhKh"),                                   # called: top pair
        _shown("Kc3c"),                                   # called: flush draw
        _shown("Qh3d"),                                   # called: air
        _shown("7h3h", raise_cbet={"flop": True}),        # raised: middle pair
        _shown(None),                                     # called, cards unknown
        _shown("AhAd", fold_to_cbet={"flop": True}),      # folded (a hero hand)
    ]
    got = showings(facts, ("faced_cbet", "flop"), "flop")
    assert set(got) == {"fold", "call", "raise"}
    assert all(set(r) == {"hands", "known", *BUCKETS} for r in got.values())
    assert got["call"] == {"hands": 4, "known": 3, "strong": 1, "medium": 0, "weak": 0, "draw": 1, "air": 1}
    assert got["raise"] == {"hands": 1, "known": 1, "strong": 0, "medium": 1, "weak": 0, "draw": 0, "air": 0}
    assert got["fold"]["known"] == 1 and got["fold"]["strong"] == 1
    # Asked about the turn, a hand whose board stopped at the flop is unknown.
    assert showings(facts, ("faced_cbet", "flop"), "turn")["call"]["known"] == 0
    # The same rows read preflop: percentiles, no board needed.
    pre = [_fact(hole_cards="AhAd", pf_faced={2: "raise"}), _fact(hole_cards="7h3h", pf_faced={2: "call"})]
    got = showings(pre, ("preflop", 2), "preflop")
    assert got["raise"]["strong"] == 1 and got["call"]["weak"] == 1


def test_texture_terms_are_the_high_card_and_the_pairing():
    assert texture_terms(("Ac", "7s", "2d"), "flop") == ["flop=ace_high", "flop=unpaired"]
    assert texture_terms(("9c", "9d", "4s", "Kh"), "turn") == ["turn=king_high", "turn=paired"]
    assert texture_terms(("Tc", "Td", "Th", "2c", "2d"), "river") == ["river=ten_high", "river=paired"]
    assert texture_terms(("Ac", "7s", "2d"), "turn") == [], "the board has not reached the turn"
    assert texture_terms(("Ac", "7s", "2d"), "preflop") == []
    assert texture_terms((), "flop") == []


def test_resolve_narrows_to_the_board_while_enough_hands_were_shown():
    node = _node(kind="faced_cbet", street="flop", situation="faced_cbet_flop")
    ace_high = [_shown("AhKh"), _shown("Kc3c"), _shown("Qh3d")]
    low = [_shown("9h8h", board=("9c", "4s", "2d")), _shown(None, board=("8c", "4s", "2d"))]
    facts = ace_high + low

    got = resolve(facts, [node], board=("Ad", "9s", "3c"), min_known=3)
    assert got["filter"] == "faced_cbet_flop,flop=ace_high,flop=unpaired"
    assert got["texture"] == ["flop=ace_high", "flop=unpaired"]
    assert got["hands"] == 3 and got["exact"] and got["relaxed"] == []
    assert got["showings"]["call"]["known"] == 3
    assert got["street"] == "flop" and got["decision"] is None and not got["arrived"]

    # One shown hand short of the gate: the plain rung, over every board.
    plain = resolve(facts, [node], board=("Ad", "9s", "3c"), min_known=4)
    assert plain["filter"] == "faced_cbet_flop" and plain["texture"] == []
    assert plain["hands"] == 5 and plain["showings"]["call"]["known"] == 4
    # A paired ace-high board has no such hands: the high card alone still does.
    partial = resolve(facts, [node], board=("Ad", "As", "3c"), min_known=3)
    assert partial["texture"] == ["flop=ace_high"]
    # No board, no narrowing; and min_known=0 always narrows when hands allow.
    assert resolve(facts, [node])["texture"] == []
    assert resolve(facts, [node], board=("9d", "5s", "3c"), min_known=0)["filter"] == "faced_cbet_flop,flop=low,flop=unpaired"


def test_texture_gate_counts_the_decision_the_line_shows():
    """A decided c-bet is shown the bet row, so narrowing must keep shown *bets*:
    shown checks on boards like this one say nothing about how they bet it."""
    node = _node(kind="cbet", street="flop", situation="cbet_flop_opp", decision="bet")
    low = ("9c", "4s", "2d")
    facts = [
        _fact(hole_cards="Kh3d", board=low, cbet_opp={"flop": True}),                     # checked, low board
        _fact(hole_cards="9h8h", board=FLOP, cbet_opp={"flop": True}, cbet={"flop": True}),  # bet, ace-high
        _fact(hole_cards="AhKh", board=FLOP, cbet_opp={"flop": True}, cbet={"flop": True}),
    ]
    got = resolve(facts, [node], board=("8d", "5s", "3c"), min_known=1)
    assert got["texture"] == [], "the low-board hand only checked, so there is no bet to show"
    assert got["showings"]["bet"]["known"] == 2
    # Still to act, every decision's shown hands count, so the narrowing holds.
    pending = resolve(facts, [_node(kind="cbet", street="flop", situation="cbet_flop_opp")],
                      board=("8d", "5s", "3c"), min_known=1)
    assert pending["texture"] == ["flop=low", "flop=unpaired"]


def test_resolve_reports_the_decision_the_spot_answers_for():
    facts = [_fact(pf_faced={1: "raise"}, opener=True, hole_cards="AhAd"),
             _fact(pf_faced={1: "raise"}, opener=True, hole_cards="7h2d")]
    decided = _node(level=1, label="unopened", situation="unopened", decision="raise")
    got = resolve(facts, [decided])
    assert got["decision"] == "raise" and not got["arrived"] and got["street"] == "preflop"
    assert got["showings"]["raise"] == {"hands": 2, "known": 2, "strong": 1, "medium": 0, "weak": 1, "draw": 0, "air": 0}
    # Pending at a tracked spot: no decision yet.
    pending = resolve(facts, [_node(level=1, situation="unopened")])
    assert pending["decision"] is None and not pending["arrived"]
    # An untracked flop node answered by that raise: the range they arrived with,
    # read on the flop -- which these hands never saw, so nothing is known there.
    path = [decided, _node(kind=None, street="flop", label="facing flop raise", situation=None)]
    got = resolve(facts, path)
    assert got["decision"] == "raise" and got["arrived"] and got["street"] == "flop"
    assert got["showings"]["raise"]["known"] == 0
    # Widened up to a parent preflop spot: the decision made there this hand.
    path = [
        _node(level=1, situation="unopened", decision="raise", pf_history=[]),
        _node(level=5, position="position=BTN", pf_history=["opener", "4bet"], situation="faced_5bet", decision="call"),
    ]
    got = resolve(facts, path)
    assert got["filter"] == "unopened" and got["decision"] == "raise" and got["arrived"]
