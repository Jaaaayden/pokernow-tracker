"""Action lines, check-raises, facing-a-bet sizes, raise sizes and what a bet got.

Pinned to hands from the heads-up fixture (bb = 10), printed and worked by hand.
gp's cards are known on every hand; Chris's only where they were shown. Pot
before each bet or raise in brackets, lines as `Facts.line` writes them:

  #2   Limped. Both check the flop and the turn; gp bets 20 [20] on the river and
       Chris folds. Chris x / x / f, gp x / x / b; gp's bet got a fold.
  #10  Chris opens, gp calls. gp leads 20 [40] into Chris, who calls; gp bets
       60 [80], Chris folds. Chris c / f, gp b / b; gp holds 4dTs on Qs3d6s, and
       picks up an open-ender on the 5h turn.
  #12  Limped. Chris bets 10 [20] and gp calls; the turn checks through; gp
       checks the river, Chris bets 10 [40] and gp folds. gp xc / x / xf: two
       chances to check-raise, taken neither time.
  #92  Limped. gp checks, Chris bets 10 [20], gp raises to 40 -- a check-raise of
       3/4 pot once called -- and Chris calls. gp bets the turn and Chris calls;
       gp bets 60 [180] on the river and Chris (9dAs, trips) raises to 150.
       gp xr / b / bc, Chris bc / c / r.
"""

from __future__ import annotations

import re

import pytest

from pnt.stats.cards import made_hand
from pnt.stats.derive import ANSWERS, SIZE_BUCKETS, HandAction, HandPlayerRow, HandRow, derive
from pnt.stats.filters import parse_filter
from pnt.stats.queries import aggregate, hand_list, load_hands
from tests.conftest import EDGE_LOGS, HU_GAME

CHRIS = "5NARaPRkSp"
GP = "gpP9uUffpu"
STREETS = ("flop", "turn", "river")


@pytest.fixture()
def hu(db):
    """{hand_number: {pn_id: Facts}} for the heads-up game."""
    return {h.hand_number: {f.pn_id: f for f in derive(h)} for h in load_hands(db, HU_GAME)}


@pytest.fixture()
def every_hand(db):
    """Every fixture hand: the core corpus plus the edge-case logs."""
    from pnt.ingest.importer import import_csv

    for path in EDGE_LOGS:
        import_csv(db, path)
    return load_hands(db)


# --- the fixture hands ---------------------------------------------------------


def test_lines_on_the_fixture_hands(hu):
    assert hu[2][CHRIS].line == {"flop": "x", "turn": "x", "river": "f"}
    assert hu[2][GP].line == {"flop": "x", "turn": "x", "river": "b"}
    assert hu[10][CHRIS].line == {"flop": "c", "turn": "f"}
    assert hu[10][GP].line == {"flop": "b", "turn": "b"}
    assert hu[12][CHRIS].line == {"flop": "b", "turn": "x", "river": "b"}
    assert hu[12][GP].line == {"flop": "xc", "turn": "x", "river": "xf"}
    assert hu[92][CHRIS].line == {"flop": "bc", "turn": "c", "river": "r"}
    assert hu[92][GP].line == {"flop": "xr", "turn": "b", "river": "bc"}

    assert hu[2][CHRIS].fold_street == "river" and hu[2][GP].fold_street is None
    assert hu[10][CHRIS].fold_street == "turn"
    assert hu[92][CHRIS].fold_street is None and hu[92][GP].fold_street is None


def test_check_raise_and_its_opportunity(hu):
    gp = hu[92][GP]
    assert gp.check_raise == {"flop": True} and gp.check_raise_opp == {"flop": True}
    assert gp.raised_bet == {"flop": True}
    # #12: checked, faced a bet, and called or folded -- two chances, none taken.
    gp = hu[12][GP]
    assert gp.check_raise == {} and gp.check_raise_opp == {"flop": True, "river": True}
    # A check that closed the street was never offered the raise.
    assert hu[2][CHRIS].check_raise_opp == {} and hu[2][CHRIS].check_back == {"flop": True, "turn": True}


def test_facing_a_bet_the_first_time(hu):
    chris = hu[10][CHRIS]
    assert chris.faced_level == {"flop": 1, "turn": 1}
    assert chris.faced_bet_pot == {"flop": 0.5, "turn": 0.75}
    assert chris.faced_bet_size == {"flop": "medium", "turn": "large"}
    assert chris.faced_bet_action == {"flop": "call", "turn": "fold"}

    assert hu[2][CHRIS].faced_bet_pot == {"river": 1.0} and hu[2][CHRIS].faced_bet_size == {"river": "large"}

    # #92: Chris's flop bet was raised, so the bet he first faced is gp's raise,
    # sized as gp's raise. gp's river bet was raised too, and he called.
    chris, gp = hu[92][CHRIS], hu[92][GP]
    assert chris.faced_level["flop"] == 2 and chris.faced_bet_action["flop"] == "call"
    assert chris.faced_bet_pot["flop"] == gp.raise_pot["flop"] == 0.75
    assert chris.faced_bet_action["river"] == "raise" and chris.faced_bet_pot["river"] == 0.333
    assert gp.faced_level == {"flop": 1, "river": 2}
    assert gp.faced_bet_action == {"flop": "raise", "river": "call"}
    assert gp.faced_bet_pot["river"] == chris.raise_pot["river"]


def test_raise_sizes_on_the_fixture_hands(hu):
    # gp raises Chris's 10 to 40 into 30: 30 more than the call, over the 40 the
    # call would have made -- three-quarters of the pot.
    assert hu[92][GP].raise_pot == {"flop": 0.75} and hu[92][GP].raise_size == {"flop": "large"}
    # Chris raises gp's 60 to 150 into 240: 90 more, over 300.
    assert hu[92][CHRIS].raise_pot == {"river": 0.3} and hu[92][CHRIS].raise_size == {"river": "small"}


def test_what_each_bet_got(hu):
    assert hu[2][GP].bet_answer == {"river": "fold"}
    assert hu[10][GP].bet_answer == {"flop": "call", "turn": "fold"}
    assert hu[12][CHRIS].bet_answer == {"flop": "call", "river": "fold"}
    assert hu[92][CHRIS].bet_answer == {"flop": "raise"}
    assert hu[92][GP].bet_answer == {"turn": "call", "river": "raise"}
    assert hu[12][CHRIS].bet_size == {"flop": "medium", "river": "small"}, "10 into 40 is a quarter pot"


def test_hand_list_rows_carry_the_line(hu):
    row = hand_list([hu[92][GP]])[0]
    assert row["line"] == {"flop": "xr", "turn": "b", "river": "bc"}


# --- synthetic hands -----------------------------------------------------------


def _hand(players: list[str], actions: list[tuple], board: tuple[str, ...] = (), cards=None) -> HandRow:
    """A hand from (street, pn_id, kind, amount[, amount_to]) tuples; posts are forced."""
    cards = cards or {}
    rows = []
    for seq, (street, pid, kind, amount, *to) in enumerate(actions, 1):
        rows.append(HandAction(
            seq=seq, street=street, pn_id=pid, kind=kind, amount=amount,
            is_forced=kind == "post", all_in=False, amount_to=to[0] if to else None,
        ))
    put = {p: sum(a.amount for a in rows if a.pn_id == p) for p in players}
    folded = {a.pn_id for a in rows if a.kind == "fold"}
    return HandRow(
        hand_id=1, game_id="g", hand_number=1, n_dealt_in=len(players), dead_button=False,
        blinds_irregular=False, went_to_showdown=len(set(players) - folded) > 1,
        saw_flop=len(board) >= 3, complete=True, bb=10, ts=None,
        players={
            p: HandPlayerRow(pn_id=p, seat=i + 1, seats_from_button=(i + 1) % len(players),
                             contributed=put[p], collected=0, folded=p in folded,
                             hole_cards=cards.get(p))
            for i, p in enumerate(players)
        },
        actions=rows,
        board=board,
    )


#: Three-way, 30 in the middle at the flop. A checks, B bets 15 (1/2 pot), C calls,
#: A check-raises to 90 -- 75 more than the call, into the 75 the call makes, a pot
#: raise -- B folds and C calls. On the turn A bets 22 into 225, a blocker, and C
#: folds. The board runs out to the river all the same, so B's and C's hands can be
#: read on streets they had folded before.
THREE_WAY = _hand(
    ["A", "B", "C"],
    [
        ("preflop", "A", "post", 5), ("preflop", "B", "post", 10),
        ("preflop", "C", "call", 10), ("preflop", "A", "call", 5), ("preflop", "B", "check", 0),
        ("flop", "A", "check", 0), ("flop", "B", "bet", 15, 15), ("flop", "C", "call", 15, 15),
        ("flop", "A", "raise", 90, 90), ("flop", "B", "fold", 0), ("flop", "C", "call", 75, 90),
        ("turn", "A", "bet", 22, 22), ("turn", "C", "fold", 0),
    ],
    board=("9c", "8d", "2h", "Kc", "Ks"),
    cards={"A": "9h9s", "B": "Ac9d", "C": "Tc7c"},
)


def test_three_way_lines_and_sizes():
    f = {x.pn_id: x for x in derive(THREE_WAY)}
    a, b, c = f["A"], f["B"], f["C"]
    assert (a.line, b.line, c.line) == ({"flop": "xr", "turn": "b"}, {"flop": "bf"}, {"flop": "cc", "turn": "f"})
    assert a.check_raise == {"flop": True}
    assert a.raise_pot == {"flop": 1.0} and a.raise_size == {"flop": "large"}, "a pot raise is 1.0"
    assert a.faced_level == {"flop": 1} and a.faced_bet_action == {"flop": "raise"}
    assert a.faced_bet_pot == {"flop": 0.5} and a.faced_bet_size == {"flop": "medium"}
    # B bet and was raised: the first bet B faced is A's raise, at level 2.
    assert b.faced_level == {"flop": 2} and b.faced_bet_pot == {"flop": 1.0}
    assert b.faced_bet_action == {"flop": "fold"} and b.fold_street == "flop"
    # C called B's bet first, so that is C's decision facing a bet, though C
    # went on to call the raise as well. The turn blocker C folded to.
    assert c.faced_level == {"flop": 1, "turn": 1}
    assert c.faced_bet_action == {"flop": "call", "turn": "fold"}
    assert c.faced_bet_size == {"flop": "medium", "turn": "blocker"}
    assert c.called_bet == {"flop": True}
    # A call and then a raise behind it: the raise is the answer.
    assert b.bet_answer == {"flop": "raise"}
    assert a.bet_size == {"turn": "blocker"} and a.bet_answer == {"turn": "fold"}


def test_three_way_filters():
    f = {x.pn_id: x for x in derive(THREE_WAY)}
    a, b, c = f["A"], f["B"], f["C"]
    for term in (
        "check_raise", "check_raise_flop", "check_raise_flop_opp", "line_flop=xr", "line_flop=x*",
        "line=xr/b", "line=xr/b/-", "line=x*//", "raised_bet_flop=large", "raised_bet_flop>=1",
        "faced_bet_flop=medium", "faced_bet_flop<0.6", "bet_turn=blocker", "bet_answer_turn=fold",
        "made_flop=set", "made_flop>=set", "made_turn=set", "made_river=full_house", "made=full_house",
        "!donk_flop",
    ):
        assert parse_filter(term)(a), term
    for term in ("line_flop=x", "line_flop!=x*", "line=xc", "check_raise_turn", "made_flop=trips", "!check_raise"):
        assert not parse_filter(term)(a), term
    assert parse_filter("bet_answer_flop=raise,faced_bet_flop=large,line_flop=bf")(b)
    assert parse_filter("line_flop=cc,line_turn=f,line_river=-")(c)

    # B folded the flop: their hand is known on the flop and on no later street;
    # the final board still reads it, as the range view does.
    assert parse_filter("made_flop=top_pair")(b)
    assert not parse_filter("made_turn=two_pair")(b) and not parse_filter("made_turn!=two_pair")(b)
    assert parse_filter("made=two_pair")(b)
    # C folded the turn, so the turn still counts and the river does not.
    assert parse_filter("made_flop=open_ender,made_turn=combo_draw,made_turn=draw")(c)
    assert not parse_filter("made_river<pair")(c)


def test_a_bet_nobody_answered_has_no_answer():
    """The log stops after the bet: there is nothing to say about what it got."""
    hand = _hand(
        ["A", "B"],
        [
            ("preflop", "A", "post", 5), ("preflop", "B", "post", 10),
            ("preflop", "A", "call", 5), ("preflop", "B", "check", 0),
            ("flop", "B", "bet", 10, 10),
        ],
        board=("9c", "8d", "2h"),
    )
    b = {x.pn_id: x for x in derive(hand)}["B"]
    assert b.bet_size == {"flop": "medium"} and b.bet_answer == {}


def _raise(to: int) -> dict:
    """Heads-up, 100 in the middle at the flop: A bets 50, B raises to `to`."""
    hand = _hand(
        ["A", "B"],
        [
            ("preflop", "A", "post", 25), ("preflop", "B", "post", 50),
            ("preflop", "A", "call", 25), ("preflop", "B", "check", 0),
            ("flop", "A", "bet", 50, 50), ("flop", "B", "raise", to, to), ("flop", "A", "fold", 0),
        ],
        board=("9c", "8d", "2h"),
    )
    return {x.pn_id: x for x in derive(hand)}


@pytest.mark.parametrize(
    "to, frac, bucket",
    [
        (100, 0.25, "small"),  # a min-raise: 50 more, over 200
        (150, 0.5, "medium"),  # PokerNow's 1/2-pot raise
        (200, 0.75, "large"),  # its 3/4
        (250, 1.0, "large"),  # its pot raise: a pot-sized raise is not an overbet
        (300, 1.25, "overbet"),
    ],
)
def test_raise_sizing_convention(to, frac, bucket):
    f = _raise(to)
    assert f["B"].raise_pot == {"flop": frac} and f["B"].raise_size == {"flop": bucket}
    assert f["A"].faced_bet_pot == {"flop": frac} and f["A"].faced_level == {"flop": 2}
    assert f["A"].bet_answer == {"flop": "raise"}


def test_a_re_raise_is_level_three():
    """A bets, B raises, C re-raises: A first faces a bet with two raises over it."""
    hand = _hand(
        ["A", "B", "C"],
        [
            ("preflop", "A", "post", 5), ("preflop", "B", "post", 10), ("preflop", "C", "call", 10),
            ("preflop", "A", "call", 5), ("preflop", "B", "check", 0),
            ("flop", "A", "bet", 10, 10), ("flop", "B", "raise", 40, 40), ("flop", "C", "raise", 120, 120),
            ("flop", "A", "fold", 0), ("flop", "B", "fold", 0),
        ],
        board=("9c", "8d", "2h"),
    )
    f = {x.pn_id: x for x in derive(hand)}
    assert f["A"].faced_level == {"flop": 3}
    assert f["B"].faced_level == {"flop": 1} and f["B"].faced_bet_action == {"flop": "raise"}
    assert f["C"].faced_level == {"flop": 2} and f["C"].faced_bet_action == {"flop": "raise"}
    assert f["A"].faced_bet_pot == f["C"].raise_pot


# --- invariants over every fixture hand ----------------------------------------

LINE = re.compile(r"^[xbcr]*f?$")


def test_line_invariants(every_hand):
    checked = 0
    for hand in every_hand:
        facts = derive(hand)
        for f in facts:
            for s, line in f.line.items():
                checked += 1
                assert line and LINE.match(line), (hand.hand_number, line)
                if line.endswith("f"):
                    assert f.fold_street == s
            for s in STREETS:
                line = f.line.get(s, "")
                if f.check_back.get(s):
                    assert line.endswith("x")
                if f.cbet.get(s) or f.donk.get(s):
                    assert line.startswith("b")
                if f.check_raise.get(s):
                    assert f.raised_bet.get(s) and f.check_raise_opp.get(s)
                assert bool(f.check_raise_opp.get(s)) == (line.startswith("x") and len(line) >= 2)
                assert ("b" in line) == (s in f.bet_size)
            # The facing-a-bet facts answer exactly the faced_bet opportunities.
            keys = f.faced_bet.keys()
            assert f.faced_level.keys() == keys
            assert f.faced_bet_pot.keys() == keys
            assert f.faced_bet_size.keys() == keys
            assert f.faced_bet_action.keys() == keys
            assert f.raise_size.keys() == f.raise_pot.keys() == f.raised_bet.keys()
            assert f.bet_answer.keys() <= f.bet_size.keys()
            for s, did in f.faced_bet_action.items():
                assert did in ANSWERS and f.faced_level[s] >= 1
                assert f.faced_bet_size[s] in SIZE_BUCKETS
                flag = {"fold": f.folded_to_bet, "call": f.called_bet, "raise": f.raised_bet}[did]
                assert flag.get(s)
            assert set(f.bet_answer.values()) <= set(ANSWERS)
            assert (f.fold_street is None) == (not any(
                a.kind == "fold" and a.pn_id == f.pn_id and not a.is_forced for a in hand.actions
            ))
    assert checked > 1000, f"too few lines to mean anything: {checked}"


def test_per_size_totals_add_up(every_hand):
    facts = [f for hand in every_hand for f in derive(hand)]
    out = aggregate(facts)
    for s in STREETS:
        bets = out[f"bet_{s}_answer_by_size"]
        assert sum(v["n"] for v in bets.values()) == sum(1 for f in facts if s in f.bet_size)
        for v in bets.values():
            got = [v[a] for a in ANSWERS if v[a] is not None]
            assert not v["n"] or sum(got) <= 100.2
        faced = out[f"vs_bet_{s}_by_size"]
        assert sum(v["faced"] for v in faced.values()) == sum(1 for f in facts if f.faced_bet.get(s))
        for v in faced.values():
            if v["faced"]:
                assert abs(v["fold"] + v["call"] + v["raise"] - 100) < 0.3, "exclusive answers"
        opp = sum(1 for f in facts if f.check_raise_opp.get(s))
        assert out["_opp"][f"check_raise_{s}"] == opp
        n = sum(1 for f in facts if f.check_raise.get(s))
        assert out[f"check_raise_{s}"] == (round(100 * n / opp, 1) if opp else None)
    complete = [f for f in facts if f.bb_size and f.complete]
    assert out["net_bb"] == pytest.approx(sum(f.net / f.bb_size for f in complete), abs=0.01)
    assert out["bb_per_100"] == pytest.approx(100 * out["net_bb"] / len(complete), abs=0.01)


def test_made_terms_agree_with_the_card_functions(every_hand):
    """`made=` reproduces the final-board classification on every known holding,
    and `made_<street>=` the per-street one wherever the player was still in."""
    from pnt.stats.cards import leaf_of, made_at

    order = {"preflop": 0, "flop": 1, "turn": 2, "river": 3}
    checked = 0
    for hand in every_hand:
        for f in derive(hand):
            if not f.hole_cards or len(f.board) < 3:
                continue
            checked += 1
            assert parse_filter(f"made={leaf_of(made_hand(f.hole_cards, f.board))}")(f)
            for s in STREETS:
                mh = made_at(f.hole_cards, f.board, s)
                still_in = f.fold_street is None or order[f.fold_street] >= order[s]
                if mh is None or not still_in:
                    assert not parse_filter(f"made_{s}>=high_card")(f)
                else:
                    assert parse_filter(f"made_{s}={leaf_of(mh)}")(f)
                    if mh.cls != "trips":  # where the leaf `trips` wins over the class
                        assert parse_filter(f"made_{s}={mh.cls}")(f)
    assert checked > 100


# --- filter terms ---------------------------------------------------------------


def test_line_terms(hu):
    assert parse_filter("line_flop=xc,line_turn=x,line_river=xf")(hu[12][GP])
    assert parse_filter("line=xc/x/xf")(hu[12][GP])
    assert parse_filter("line=xc//*f")(hu[12][GP])
    assert parse_filter("line=x*")(hu[12][GP]), "segments left off the end match anything"
    assert parse_filter("line_flop=*c")(hu[12][GP]) and not parse_filter("line_flop=*f")(hu[12][GP])
    assert parse_filter("line_river=-")(hu[10][CHRIS]), "no river: did not act"
    assert not parse_filter("line_river=*")(hu[10][CHRIS])
    assert parse_filter("line_river!=b*")(hu[10][CHRIS]), "!= is the exact complement"
    assert parse_filter("line_flop!=x*")(hu[10][GP])
    assert parse_filter("line_flop=XR")(hu[92][GP]), "patterns forgive case"


def test_check_raise_and_negation_terms(hu):
    assert parse_filter("check_raise,check_raise_flop,check_raise_flop_opp")(hu[92][GP])
    assert parse_filter("!check_raise,check_raise_flop_opp")(hu[12][GP])
    assert not parse_filter("!check_raise")(hu[92][GP])
    assert parse_filter("!cbet_flop,!wtsd")(hu[12][GP])


def test_size_and_answer_terms(hu):
    chris, gp = hu[92][CHRIS], hu[92][GP]
    assert parse_filter("faced_bet_flop=large,faced_bet_flop>=0.75,raised_bet_river=small")(chris)
    assert parse_filter("raised_bet_flop=large,raised_bet_flop>=0.7,raised_bet_flop<1")(gp)
    assert not parse_filter("raised_bet_turn>=0")(gp), "no raise, no size"
    assert parse_filter("bet_answer_flop=raise")(chris)
    assert parse_filter("bet_answer_turn=call,bet_answer_river=raise")(gp)
    assert parse_filter("bet_answer_river=fold")(hu[2][GP])
    assert not parse_filter("bet_answer_river=call")(hu[2][GP])
    # The flag forms are untouched by the sized ones sharing their names.
    assert parse_filter("faced_bet_flop,raised_bet_flop")(gp)


def test_made_terms(hu):
    chris = hu[92][CHRIS]  # 9dAs on Jc 9h 7d | 9c | Qh
    for term in (
        "made_flop=middle_pair", "made_flop=pair", "made_flop<two_pair", "made_flop<=pair",
        "made_turn=trips", "made_turn!=set", "made_turn>=trips", "made_turn>two_pair",
        "made_river>=pair", "made=trips", "made!=set",
    ):
        assert parse_filter(term)(chris), term
    for term in ("made_flop=set", "made_turn=set", "made_flop>pair", "made_flop>=two_pair", "made=pair"):
        assert not parse_filter(term)(chris), term

    gp = hu[10][GP]  # 4dTs on Qs 3d 6s | 5h: nothing, then an open-ender
    assert parse_filter("made_flop=high_card,made_turn=open_ender,made_turn=draw,made=open_ender")(gp)
    assert parse_filter("made_turn<pair,made_turn>high_card")(gp)
    assert not parse_filter("made_river=high_card")(gp), "the river was never dealt"
    assert not parse_filter("made_flop!=set")(hu[10][CHRIS]), "an unknown holding matches nothing"


def test_trips_is_still_the_board_texture(hu):
    """`flop=trips` is three of a rank on the flop; the holding is `made_flop=trips`."""
    chris = hu[92][CHRIS]  # the turn pairs the board: trips in hand, not on the board
    assert parse_filter("made_turn=trips")(chris)
    assert not parse_filter("turn=trips")(chris) and parse_filter("turn=paired")(chris)
    f = derive(THREE_WAY)[0]
    f.board = ("9c", "9d", "9h", "2s", "3d")
    f.hole_cards = "AsKd"
    assert parse_filter("flop=trips")(f)
    assert not parse_filter("made_flop=set")(f)


@pytest.mark.parametrize(
    "term, message",
    [
        ("line_flop=xq", "bad line pattern"),
        ("line_flop=", "bad line pattern"),
        ("line=", "bad line"),
        ("line=x/x/x/x", "bad line"),
        ("line=//", "bad line"),
        ("made_flop=sets", "unknown made hand"),
        ("made=", "unknown made hand"),
        ("bet_answer_flop=maybe", "unknown answer"),
        ("faced_bet_flop=huge", "unknown bet size"),
        ("raised_bet_river=huge", "unknown bet size"),
        ("!pot>=500", "only a flag"),
        ("!nonsense", "only a flag"),
        ("check_raise_preflop", "unknown filter term"),
    ],
)
def test_bad_terms_are_rejected(term, message):
    with pytest.raises(ValueError, match=message):
        parse_filter(term)
