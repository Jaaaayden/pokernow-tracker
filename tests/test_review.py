"""Hand review: the rules on hands built by hand, then the fixture corpus.

Each flag is pinned the way the tags are: it fires on a hand that has what it
names, and stays silent one step short -- a pot a little too small, a board a
flush could get there on, a bluff that won. The fixture logs then hold the
invariants a person can check, and pin the hands that fire there.

What fires on the three fixture logs: missed bluffs, failed bluffs, and one
preflop cooler. No missed value, suckout or postflop cooler happens in them, so
those three are held by the hand-built hands alone.
"""

from __future__ import annotations

import pathlib
from collections import Counter

import pytest
from conftest import HU_GAME, MULTIWAY_GAME
from typer.testing import CliRunner

from pnt.stats import equity as eq
from pnt.stats import review as rv
from pnt.stats.allin import AllInRow, allin_rows
from pnt.stats.cards import straight_possible, top_kicker, top_two_pair
from pnt.stats.derive import HandAction, HandPlayerRow, HandRow, derive
from pnt.stats.equity import best_of
from pnt.stats.filters import parse_filter
from pnt.stats.queries import display_names, facts_for, load_hands, report

THREE_GAME = "pglSdQtyFGypDbrqD5IhXXlYz"
HERO, VIL = "hero", "vil"


@pytest.fixture(autouse=True)
def _fast_sampling(monkeypatch):
    monkeypatch.setattr(eq, "SAMPLES", 2000)


# ------------------------------------------------------------ card helpers ---


@pytest.mark.parametrize(
    "board, expected",
    [
        ("7s8d9c", True),
        ("As2d3c", True),  # the wheel: the ace plays low
        ("AsKdTc", True),
        ("2s7dQc", False),
        ("2s7dQcJh", False),
        ("2s7dQcJhTd", True),
        ("Kd7c2s9h3d", False),
        ("AhKd", False),
    ],
)
def test_straight_possible(board, expected):
    assert straight_possible(board) is expected


@pytest.mark.parametrize(
    "hole, board, expected",
    [
        ("AhKd", "Kc7s2d", True),
        ("AhKd", "Ac7s2d", True),
        ("AhQd", "AcKs7d", True),  # K is on the board, so Q is the best kicker left
        ("AhQd", "Ac7s2d", False),
        ("KhKd", "Ac7s2d", False),
        ("7h6d", "Ac7s2d", False),
    ],
)
def test_top_kicker(hole, board, expected):
    assert top_kicker(hole, board) is expected


@pytest.mark.parametrize(
    "hole, board, expected",
    [
        ("AhKd", "AcKs2d", True),
        ("Ah2d", "AcKs2d", False),
        ("AhAd", "AcKs2d", False),
        ("AhKd", "AcKs7d7h", True),
    ],
)
def test_top_two_pair(hole, board, expected):
    assert top_two_pair(hole, board) is expected


def test_best_of_names_the_winner_and_both_sides_of_a_chop():
    board = ["Kd", "7d", "2s", "9d", "3d"]
    assert best_of({"set": "7h7s", "flush": "Ad4c"}, board) == ("flush",)
    assert best_of({"a": "AhTc", "b": "AsTd"}, ["8c", "5s", "3d", "Th", "4d"]) == ("a", "b")


@pytest.mark.parametrize(
    "hole, board, tier, expected",
    [
        ("AhAd", "Kc7s2d", "two_pair_plus", False),  # an overpair is not a stacks hand in an SRP
        ("AhAd", "Kc7s2d", "tptk_plus", True),
        ("AhKd", "Kc7s2d", "tptk_plus", True),
        ("AhKd", "Kc7s2d", "two_pair_plus", False),
        ("Kh7d", "Kc7s2d", "two_pair_plus", True),
        ("7h2d", "Kc7s2d", "two_pair_plus", False),  # bottom two
        ("2h2d", "Kc7s2d", "two_pair_plus", True),  # a set
        ("Ah7d", "7c7s2d", "two_pair_plus", False),  # one card to a paired board
        ("AhKd", "Kc7s7d", "tptk_plus", True),  # TPTK with the board paired too
    ],
)
def test_meets_tier(hole, board, tier, expected):
    assert rv.meets_tier(hole, board, tier) is expected


def test_tier_follows_pot_type_and_depth():
    assert rv.tier(2, 100) == "two_pair_plus"
    assert rv.tier(3, 100) == "tptk_plus"
    assert rv.tier(3, 120) == "tptk_plus"
    assert rv.tier(3, 121) == "two_pair_plus"
    assert rv.tier(3, None) == "two_pair_plus"
    assert rv.tier(4, 400) == "tptk_plus"


def test_air_is_board_pair_or_worse():
    from pnt.stats.cards import made_hand

    board = ("Kd", "8c", "3s", "2h", "2d")
    assert rv.is_air(made_hand("QhJc", board))  # a board pair, Q-high
    assert rv.is_air(made_hand("6h5h", board[:4]))  # a draw
    assert not rv.is_air(made_hand("4h4c", board))  # an underpair is a hand
    assert not rv.is_air(made_hand("8h7c", board))


def test_the_spec_table_is_the_other_copy_of_the_thresholds():
    """SPEC.md is the artifact; these numbers live in two places and must agree."""
    spec = (pathlib.Path(__file__).resolve().parents[1] / "pnt" / "stats" / "SPEC.md").read_text(encoding="utf-8")
    section = spec.split("## Hand review", 1)[1].split("## Known judgement calls", 1)[0]
    for name, value in rv.THRESHOLDS.items():
        if name == "premium":
            assert "QQ+, AKs, AKo" in section
            assert rv.THRESHOLDS["premium"] == ("AA", "KK", "QQ", "AKs", "AKo")
            continue
        assert f"| `{name}` | {value:g} |" in section, name
    for kind, group in rv.KINDS.items():
        assert f"`{kind}`" in section, kind
        assert f"**{group}**" in section


# ------------------------------------------------------------- hand builder ---


def mk_hand(
    players: dict[str, dict],
    actions: list[tuple],
    board: str = "",
    bb: int = 10,
    showdown: bool = True,
    hand_id: int = 1,
) -> HandRow:
    """A heads-up-shaped hand. `players` maps a pid to cards, stack, collected and
    optionally folded/contributed; contributed defaults to the sum of their
    actions. `actions` are (street, pid, kind, amount[, all_in])."""
    acts = [
        HandAction(
            seq=i + 1, street=a[0], pn_id=a[1], kind=a[2], amount=a[3],
            is_forced=a[2] == "post", all_in=bool(a[4]) if len(a) > 4 else False,
        )
        for i, a in enumerate(actions)
    ]
    cards = [board[i : i + 2] for i in range(0, len(board), 2)]
    rows = {}
    for seat, (pid, p) in enumerate(players.items()):
        rows[pid] = HandPlayerRow(
            pn_id=pid, seat=seat + 1, seats_from_button=seat,
            contributed=p.get("contributed", sum(a.amount for a in acts if a.pn_id == pid)),
            collected=p.get("collected", 0), folded=p.get("folded", False),
            hole_cards=p.get("cards"), starting_stack=p.get("stack", 1000),
        )
    return HandRow(
        hand_id=hand_id, game_id="g", hand_number=hand_id, n_dealt_in=len(players),
        dead_button=False, blinds_irregular=False, went_to_showdown=showdown,
        saw_flop=len(cards) >= 3, complete=True, bb=bb, ts=None,
        players=rows, actions=acts, board=tuple(cards),
    )


def facts(hand: HandRow) -> dict:
    return {f.pn_id: f for f in derive(hand)}


SRP = [
    ("preflop", HERO, "post", 5), ("preflop", VIL, "post", 10),
    ("preflop", HERO, "raise", 25), ("preflop", VIL, "call", 20),
]
THREE_BET = [
    ("preflop", HERO, "post", 5), ("preflop", VIL, "post", 10),
    ("preflop", HERO, "raise", 25), ("preflop", VIL, "raise", 80), ("preflop", HERO, "call", 60),
]


def checks(*streets: str) -> list[tuple]:
    return [a for s in streets for a in ((s, VIL, "check", 0), (s, HERO, "check", 0))]


# ------------------------------------------------------------ missed bluff ---


def _checkdown(flop_bet: int, river: list[tuple] | None = None, vil_cards="7h6h") -> HandRow:
    actions = [*SRP]
    if flop_bet:
        actions += [("flop", VIL, "check", 0), ("flop", HERO, "bet", flop_bet), ("flop", VIL, "call", flop_bet)]
    else:
        actions += checks("flop")
    actions += checks("turn")
    actions += river if river is not None else checks("river")
    pot = 60 + 2 * flop_bet + sum(a[3] for a in (river or []))
    return mk_hand(
        {HERO: {"cards": "QhJc", "collected": pot}, VIL: {"cards": vil_cards}},
        actions, board="Kd8c3s2h2d",
    )


def test_missed_bluff_fires_on_a_bloated_pot_checked_down_with_air():
    hand = _checkdown(flop_bet=50)  # 60 + 100 = 16bb
    row = rv.missed_bluff(hand, facts(hand), HERO, rv.Context())
    assert row is not None and row.kind == "missed_bluff" and row.street == "river"
    assert row.pot_bb == 16.0
    assert [v.pn_id for v in row.villains] == [VIL]
    assert "16bb pot checked down" in row.why
    # the other side of the table missed the same chance
    assert rv.missed_bluff(hand, facts(hand), VIL, rv.Context()) is not None


def test_missed_bluff_is_silent_one_step_short():
    small = _checkdown(flop_bet=40)  # 14bb
    assert rv.missed_bluff(small, facts(small), HERO, rv.Context()) is None
    bet = _checkdown(flop_bet=50, river=[("river", VIL, "check", 0), ("river", HERO, "bet", 20), ("river", VIL, "call", 20)])
    assert rv.missed_bluff(bet, facts(bet), HERO, rv.Context()) is None
    made = _checkdown(flop_bet=50, vil_cards="4h4c")  # an underpair is a hand
    assert rv.missed_bluff(made, facts(made), HERO, rv.Context()) is None
    mucked = _checkdown(flop_bet=50, vil_cards=None)
    assert rv.missed_bluff(mucked, facts(mucked), HERO, rv.Context()) is None


# ------------------------------------------------------------ missed value ---


def _value(pre=SRP, board="Kd7c2s9h3d", hero="Kh7h", vil="2c2h", stack=1000, turn_bet=40, allin=False):
    actions = [*pre, *checks("flop")]
    actions += [("turn", VIL, "check", 0), ("turn", HERO, "bet", turn_bet, allin), ("turn", VIL, "call", turn_bet)]
    actions += checks("river")
    pot = sum(a[3] for a in actions)
    return mk_hand(
        {HERO: {"cards": hero, "stack": stack}, VIL: {"cards": vil, "stack": stack, "collected": pot}},
        actions, board=board,
    )


def test_missed_value_fires_on_two_stacks_hands_that_played_small():
    hand = _value()  # top two vs a set, 100bb deep, a 14bb pot
    row = rv.missed_value(hand, facts(hand), HERO, rv.Context())
    assert row is not None and row.street == "flop"
    assert row.eff_bb == 100.0 and row.pot_bb == 14.0
    assert "top two pair vs set (vil) on the flop" in row.why
    assert rv.missed_value(hand, facts(hand), VIL, rv.Context()) is not None


def test_missed_value_is_silent_one_step_short():
    wet = _value(board="Kd7d2d9h3c")  # three diamonds from the flop on
    assert rv.missed_value(wet, facts(wet), HERO, rv.Context()) is None
    shoved = _value(allin=True)
    assert rv.missed_value(shoved, facts(shoved), HERO, rv.Context()) is None
    shallow = _value(stack=100)  # 140 in against 2 x 100 stacks: more than half went in
    assert rv.missed_value(shallow, facts(shallow), HERO, rv.Context()) is None
    weak = _value(hero="Ah7h")  # one pair
    assert rv.missed_value(weak, facts(weak), HERO, rv.Context()) is None


def test_missed_value_in_a_3bet_pot_wants_tptk_at_100bb_and_two_pair_deeper():
    board = "Kd7c2s9h3d"
    at_100 = _value(pre=THREE_BET, board=board, hero="AhKc", vil="AsKs", stack=1000)
    assert rv.missed_value(at_100, facts(at_100), HERO, rv.Context()) is not None
    at_150 = _value(pre=THREE_BET, board=board, hero="AhKc", vil="AsKs", stack=1500)
    assert rv.missed_value(at_150, facts(at_150), HERO, rv.Context()) is None


def test_an_unknown_stack_is_skipped_and_counted():
    hand = _value()
    hand.players[VIL].starting_stack = None
    ctx = rv.Context()
    assert rv.missed_value(hand, facts(hand), HERO, ctx) is None
    assert ctx.stack_unknown == {(hand.hand_id, HERO)}


# ------------------------------------------------------------ failed bluff ---


def _bluff(turn: list[tuple], showdown=True, hero_collected=0, vil_contributed=None, flop_cards="QhJc"):
    actions = [
        *SRP,
        ("flop", VIL, "check", 0), ("flop", HERO, "bet", 40), ("flop", VIL, "call", 40),
        *turn,
    ]
    players = {
        HERO: {"cards": flop_cards, "collected": hero_collected},
        VIL: {"cards": "Kc9c" if showdown else None, "folded": False},
    }
    if vil_contributed is not None:
        players[VIL]["contributed"] = vil_contributed
    return mk_hand(players, actions, board="Kd8c3s2h2d" if showdown else "Kd8c3s2h", showdown=showdown)


def test_a_called_turn_bluff_is_one_row_on_the_turn():
    hand = _bluff([("turn", VIL, "check", 0), ("turn", HERO, "bet", 100), ("turn", VIL, "call", 100), *checks("river")])
    row = rv.failed_bluff(hand, facts(hand), HERO, rv.Context())
    assert row is not None and row.street == "turn"
    assert (row.bluff_kind, row.bluff_size, row.bluff_pot, row.answer) == ("bet", "medium", 0.71, "called")
    (v,) = row.villains
    assert v.pn_id == VIL and v.answer == "called" and v.uncapped is False
    assert v.made is not None and v.made.detail == "top_pair"
    assert row.why.startswith("Bet the turn 71% pot with Q-high, called by vil with top pair")


def test_a_raised_bluff_names_the_raise_and_needs_no_showdown():
    hand = _bluff(
        [("turn", VIL, "check", 0), ("turn", HERO, "bet", 100), ("turn", VIL, "raise", 300), ("turn", HERO, "fold", 0)],
        showdown=False, vil_contributed=170,
    )
    hand.players[HERO].folded = True
    row = rv.failed_bluff(hand, facts(hand), HERO, rv.Context(villain_info=lambda _: {"archetype": "STATION", "wtsd_pct": 44.0}))
    assert row is not None and row.answer == "raised"
    assert row.villains[0].cards is None and row.villains[0].archetype == "STATION"
    assert "raised by vil (STATION, WTSD 44%)" in row.why


def test_a_bluff_that_won_or_a_semi_bluff_is_not_a_failed_bluff():
    won = _bluff([("turn", VIL, "check", 0), ("turn", HERO, "bet", 100), ("turn", VIL, "call", 100), *checks("river")], hero_collected=340)
    assert rv.failed_bluff(won, facts(won), HERO, rv.Context()) is None
    # A flush draw on the flop has outs; checked down after, it is no bluff row.
    draw = mk_hand(
        {HERO: {"cards": "QdJd"}, VIL: {"cards": "Kc9c", "collected": 140}},
        [*SRP, ("flop", VIL, "check", 0), ("flop", HERO, "bet", 40), ("flop", VIL, "call", 40), *checks("turn", "river")],
        board="Kd8d3s2h2c",
    )
    assert rv.failed_bluff(draw, facts(draw), HERO, rv.Context()) is None


def test_a_small_pot_bluff_is_not_worth_a_row():
    hand = mk_hand(
        {HERO: {"cards": "QhJc"}, VIL: {"cards": "Kc9c", "collected": 100}},
        [*SRP, ("flop", VIL, "check", 0), ("flop", HERO, "bet", 20), ("flop", VIL, "call", 20), *checks("turn", "river")],
        board="Kd8c3s2h2d",
    )  # 10bb
    assert rv.failed_bluff(hand, facts(hand), HERO, rv.Context()) is None


# ------------------------------------------------------------------ beats ---


def _allin_hand(hero: str, vil: str, board: str, flop_allin: bool = False) -> HandRow:
    if flop_allin:
        actions = [*SRP, ("flop", VIL, "bet", 970, True), ("flop", HERO, "call", 970, True)]
    else:
        actions = [
            ("preflop", HERO, "post", 5), ("preflop", VIL, "post", 10),
            ("preflop", HERO, "raise", 995, True), ("preflop", VIL, "call", 990, True),
        ]
    return mk_hand({HERO: {"cards": hero}, VIL: {"cards": vil, "collected": 2000}}, actions, board=board)


def _allin_row(hand: HandRow, equity: float, actual: int = -1000, street: str = "preflop") -> AllInRow:
    board = list(hand.board[: {"preflop": 0, "flop": 3}[street]])
    return AllInRow(
        hand_id=hand.hand_id, game_id="g", hand_number=1, ts=None, pn_id=HERO, street=street,
        hole_cards=hand.players[HERO].hole_cards, board=board, full_board=list(hand.board),
        run_count=1, villains=[(VIL, hand.players[VIL].hole_cards)], pot=2000, contributed=1000,
        collected=1000 + actual, bounty=0, bb=10, equity=equity, expected=equity * 2000,
        actual=actual, adjusted=equity * 2000 - 1000, diff=actual - (equity * 2000 - 1000),
        method="exact", n=1,
    )


def _ctx(row: AllInRow) -> rv.Context:
    return rv.Context(allin={(row.hand_id, row.pn_id): row})


def test_suckout_is_well_ahead_and_lost():
    hand = _allin_hand("AhAd", "KhKd", "Kc7s2d9h3c")
    row = rv.suckout(hand, facts(hand), HERO, _ctx(_allin_row(hand, 0.82)))
    assert row is not None and row.equity == 0.82 and row.street == "preflop"
    assert row.why.startswith("82% to win when it went in preflop: AA vs KK (vil), lost 100bb")
    for flip in (_allin_row(hand, 0.59), _allin_row(hand, 0.82, actual=0)):  # a flip; a chop
        assert rv.suckout(hand, facts(hand), HERO, _ctx(flip)) is None


def test_preflop_cooler_is_a_premium_behind():
    hand = _allin_hand("KhKd", "AhAd", "Kc7s2d9h3c".replace("Kc", "5c"))
    row = rv.cooler_pre(hand, facts(hand), HERO, _ctx(_allin_row(hand, 0.18)))
    assert row is not None and row.why.startswith("KK ran into AA (vil) preflop: 18% equity")
    jacks = _allin_hand("JhJd", "AhAd", "5c7s2d9h3c")
    assert rv.cooler_pre(jacks, facts(jacks), HERO, _ctx(_allin_row(jacks, 0.18))) is None


def test_postflop_cooler_is_a_stacks_hand_behind_on_a_dry_board():
    hand = _allin_hand("7h7d", "KsKc", "Kd7c2s9h3d", flop_allin=True)
    row = _allin_row(hand, 0.04, street="flop")
    (flag,) = rv.review_hand(hand, facts(hand), HERO, _ctx(row))
    assert flag.kind == "cooler_post" and flag.street == "flop"
    assert flag.why.startswith("set into set (vil) on the flop, all in on the flop, 4% to win")


def test_a_hand_is_at_most_one_beat():
    hand = _allin_hand("7h7d", "KsKc", "Kd7c2s9h3d", flop_allin=True)
    ahead = _allin_row(hand, 0.9, street="flop")  # as if the set had been ahead
    kinds = [r.kind for r in rv.review_hand(hand, facts(hand), HERO, _ctx(ahead))]
    assert kinds == ["suckout"]


# ----------------------------------------------------------------- plumbing ---


def test_starting_stacks_reach_the_derive_layer(db):
    for hand in load_hands(db, HU_GAME):
        assert all(isinstance(p.starting_stack, int) for p in hand.players.values())


def test_pot_at_counts_the_streets_before(db):
    hand = _checkdown(flop_bet=50)
    assert facts(hand)[HERO].pot_at == {"flop": 60, "turn": 160, "river": 160}
    for h in load_hands(db):
        f = derive(h)[0]
        values = [f.pot_at[s] for s in ("flop", "turn", "river") if s in f.pot_at]
        assert values == sorted(values) and all(v <= f.pot for v in values)
        assert ("flop" in f.pot_at) == (len(h.board) >= 3)


# ----------------------------------------------------------- fixture corpus ---


@pytest.fixture()
def aliases(db):
    return [r["player"] for r in report(db)]


@pytest.fixture()
def reviews(db, aliases):
    return {a: rv.review_hand_list(db, a) for a in aliases}


def test_every_row_is_one_of_the_players_own_hands(db, reviews):
    for alias, out in reviews.items():
        mine = {f.hand_id for f in facts_for(db, alias)}
        assert {h["hand_id"] for h in out["hands"]} <= mine
        assert Counter(h["kind"] for h in out["hands"]) == Counter({k: n for k, n in out["counts"].items() if n})
        for h in out["hands"]:
            assert h["group"] == rv.KINDS[h["kind"]]
        beats = Counter(h["hand_id"] for h in out["hands"] if h["group"] == "beat")
        assert all(n == 1 for n in beats.values())


def test_showdown_flags_have_every_live_hand_known(db, reviews):
    ids = {h["hand_id"] for out in reviews.values() for h in out["hands"]
           if h["kind"] in ("missed_bluff", "missed_value", "cooler_post")}
    for hand in load_hands(db, hand_ids=ids):
        assert hand.complete and hand.went_to_showdown
        assert all(p.hole_cards for p in hand.players.values() if not p.folded)


def test_beats_agree_with_the_allin_rows(db, reviews):
    allin = {(r.hand_id, r.hole_cards): r for r in allin_rows(db)[0]}
    for out in reviews.values():
        for h in out["hands"]:
            if h["kind"] in ("suckout", "cooler_pre") or h["equity"] is not None:
                r = allin[(h["hand_id"], h["hole_cards"])]
                assert h["equity"] == r.equity


def test_each_flag_is_inside_its_spot_filter(db, reviews):
    """The review needs cards the filter cannot see, so each flag is a subset of
    the hands its widest filter lists, never the whole of them."""
    names = display_names(db)
    widest = {
        "missed_bluff": "wtsd,pot_bb>=15",
        "missed_value": "wtsd",
        "failed_bluff": "lost,pot_bb>=15",
        "suckout": "wtsd,lost,jam",
        "cooler_pre": "wtsd,lost",
        "cooler_post": "wtsd,lost",
    }
    for alias, out in reviews.items():
        facts_ = facts_for(db, alias)
        for kind, expr in widest.items():
            pred = parse_filter(expr, names)
            listed = {f.hand_id for f in facts_ if pred(f)}
            assert {h["hand_id"] for h in out["hands"] if h["kind"] == kind} <= listed, (alias, kind)


def test_what_fires_on_the_fixture_logs(reviews):
    totals = Counter()
    for out in reviews.values():
        totals.update(out["counts"])
    assert totals["missed_value"] == totals["suckout"] == totals["cooler_post"] == 0

    def rows(alias, game, number, kind):
        return [h for h in reviews[alias]["hands"]
                if h["game_id"] == game and h["hand_number"] == number and h["kind"] == kind]

    # #212: a 16bb pot checked down with a board pair on each side -- flagged for both.
    for alias in ("HSJ", "genericpoker"):
        (h,) = rows(alias, THREE_GAME, 212, "missed_bluff")
        assert h["pot_bb"] == 16.0 and h["made"]["label"] == "board pair"

    # #53: a 60% pot river bet with a board pair, called by a flush.
    (h,) = rows("genericpoker", HU_GAME, 53, "failed_bluff")
    assert (h["street"], h["bluff_kind"], h["bluff_size"], h["answer"]) == ("river", "bet", "medium", "called")
    (v,) = h["villains"]
    assert v["player"] == "Chris" and v["made"]["cls"] == "flush"

    # #16: AKs all in preflop against 77, three-handed, and lost.
    (h,) = rows("all in", MULTIWAY_GAME, 16, "cooler_pre")
    assert h["street"] == "preflop" and h["equity"] < 0.5


def test_a_spot_filter_narrows_the_rows(db, reviews):
    pred = parse_filter("vs=Chris", display_names(db))
    out = rv.review_hand_list(db, "genericpoker", predicate=pred)
    assert out["hands"] and all("Chris" in h["vs"] for h in out["hands"])
    assert out["examined"] < reviews["genericpoker"]["examined"]


def test_an_unknown_alias_raises(db):
    with pytest.raises(ValueError, match="unknown alias"):
        rv.review_hand_list(db, "nobody")


def test_the_cli_lists_the_rows(db, tmp_path):
    from pnt import cli

    path = str(tmp_path / "t.sqlite")
    runner = CliRunner()
    result = runner.invoke(cli.app, ["review", "genericpoker", "--db", path])
    assert result.exit_code == 0, result.output
    assert "Missed bluff 2" in result.output and "#212" in result.output

    result = runner.invoke(cli.app, ["review", "genericpoker", "--db", path, "--kind", "missed_bluff", "--json"])
    assert result.exit_code == 0, result.output
    import json

    body = json.loads(result.output)
    assert {"player", "counts", "skipped", "hands"} <= set(body)
    assert {h["kind"] for h in body["hands"]} == {"missed_bluff"}

    assert runner.invoke(cli.app, ["review", "genericpoker", "--db", path, "--kind", "nope"]).exit_code != 0
    assert runner.invoke(cli.app, ["review", "nobody", "--db", path]).exit_code != 0


# ------------------------------------------------------------------ marks ---


def _flagged(db, alias="genericpoker"):
    """One flagged hand of this player's, as (game_id, hand_number)."""
    h = rv.review_hand_list(db, alias)["hands"][0]
    return h["game_id"], h["hand_number"]


def test_a_mark_survives_the_rebuild_that_moves_every_hand_id(db):
    """The reason the mark is keyed on (game_id, hand_number): a rebuild deletes a
    game's hands and re-inserts them, so every hand_id in it changes."""
    from pnt.ingest.importer import rebuild_game

    ids = lambda: dict(db.execute(
        "SELECT hand_number, hand_id FROM hands WHERE game_id = ?", (HU_GAME,)).fetchall())
    before = ids()
    rv.mark_reviewed(db, HU_GAME, 53)
    rebuild_game(db, HU_GAME)
    after = ids()
    assert before and all(before[n] != after[n] for n in before), "hand_ids did not move"
    assert rv.is_reviewed(db, HU_GAME, 53)
    assert rv.reviewed_marks(db, HU_GAME).keys() == {(HU_GAME, 53)}


def test_marking_is_idempotent_both_ways_and_keeps_the_first_time(db):
    game, number = _flagged(db)
    first = rv.mark_reviewed(db, game, number)
    assert first and rv.mark_reviewed(db, game, number) == first
    assert rv.mark_reviewed(db, game, number, reviewed=False) is None
    assert rv.mark_reviewed(db, game, number, reviewed=False) is None  # no-op, not an error
    assert not rv.is_reviewed(db, game, number)


def test_marking_a_hand_that_is_not_there_raises_but_clearing_one_does_not(db):
    with pytest.raises(ValueError, match="no hand #99999"):
        rv.mark_reviewed(db, HU_GAME, 99999)
    with pytest.raises(ValueError):
        rv.mark_reviewed(db, "nosuchgame", 1)
    assert rv.mark_reviewed(db, "nosuchgame", 1, reviewed=False) is None


def test_the_marks_are_scoped_to_their_game(db):
    rv.mark_reviewed(db, HU_GAME, 53)
    rv.mark_reviewed(db, MULTIWAY_GAME, 16)
    assert set(rv.reviewed_marks(db)) == {(HU_GAME, 53), (MULTIWAY_GAME, 16)}
    assert set(rv.reviewed_marks(db, HU_GAME)) == {(HU_GAME, 53)}


def test_every_row_of_a_marked_hand_carries_the_mark(db):
    """The mark is on the hand: both flags of one hand, and both players of a
    shared one, see it."""
    game, number = _flagged(db)
    at = rv.mark_reviewed(db, game, number)
    out = rv.review_hand_list(db, "genericpoker")
    marked = [h for h in out["hands"] if (h["game_id"], h["hand_number"]) == (game, number)]
    assert marked and all(h["reviewed"] and h["reviewed_at"] == at for h in marked)
    assert all(not h["reviewed"] for h in out["hands"] if h not in marked)
    assert out["reviewed"] == len(marked)


def test_a_review_with_no_marks_says_so(db):
    out = rv.review_hand_list(db, "genericpoker")
    assert out["reviewed"] == 0 and not any(h["reviewed"] for h in out["hands"])
    assert all(h["reviewed_at"] is None for h in out["hands"])


def test_the_cli_marks_lists_and_filters(db, tmp_path):
    from pnt import cli

    path = str(tmp_path / "t.sqlite")
    runner = CliRunner()
    assert "nothing marked" in runner.invoke(cli.app, ["reviewed", "--db", path]).output

    r = runner.invoke(cli.app, ["reviewed", THREE_GAME, "212", "--db", path])
    assert r.exit_code == 0 and "reviewed" in r.output
    assert f"{THREE_GAME}  #212" in runner.invoke(cli.app, ["reviewed", "--db", path]).output

    listed = runner.invoke(cli.app, ["review", "genericpoker", "--db", path])
    assert listed.exit_code == 0, listed.output
    assert "reviewed 1" in listed.output
    assert "x #212" in listed.output

    left = runner.invoke(cli.app, ["review", "genericpoker", "--db", path, "--unreviewed"])
    assert left.exit_code == 0 and "#212" not in left.output

    assert runner.invoke(cli.app, ["reviewed", THREE_GAME, "212", "--db", path, "--undo"]).exit_code == 0
    assert "nothing marked" in runner.invoke(cli.app, ["reviewed", "--db", path]).output
    assert runner.invoke(cli.app, ["reviewed", THREE_GAME, "99999", "--db", path]).exit_code != 0
    assert runner.invoke(cli.app, ["reviewed", THREE_GAME, "--db", path]).exit_code != 0
