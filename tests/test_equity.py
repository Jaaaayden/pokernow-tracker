"""The equity engine, checked against answers that can be worked by hand.

A hand evaluator that mis-ranks one category looks fine on almost every board and
is wrong on the pots that matter, so every category boundary is pinned, and the
exact enumerations are pinned to counts a person can verify with a deck.
"""

from __future__ import annotations

import pytest

from pnt.stats.cards import parse_cards
from pnt.stats.equity import cache_key, expected_collected, rank7, side_pots


def r(text: str) -> tuple[int, ...]:
    return rank7(parse_cards(text))


# ------------------------------------------------------------------ rank7 ----


@pytest.mark.parametrize(
    "better, worse",
    [
        ("AsKsQsJsTs2c3d", "AhAdAcAs2c3d4h"),  # straight flush > quads
        ("AhAdAcAs2c3d4h", "KhKdKcQsQc3d4h"),  # quads > full house
        ("KhKdKcQsQc3d4h", "Ah9h5h3h2hKcQd"),  # full house > flush
        ("Ah9h5h3h2hKcQd", "9s8d7c6h5sAcAd"),  # flush > straight
        ("9s8d7c6h5sAcKd", "AhAdAc9s7c3d4h"),  # straight > trips
        ("AhAdAc9s7c3d4h", "AhAdKcKs7c3d4h"),  # trips > two pair
        ("AhAdKcKs7c3d4h", "AhAd9cKs7c3d4h"),  # two pair > pair
        ("2h2d9cKs7c3d4h", "AhQd9cKs7c3d4h"),  # a pair beats ace high
        ("6s5d4c3h2sKcQd", "5s4d3c2hAsKcQd"),  # six-high straight beats the wheel
        ("AhAd9cKs7c3d4h", "AhAd9cQs7c3d4h"),  # kicker decides a pair
        ("Ah9h5h3h2hKcQd", "Kh9h5h3h2hAcQd"),  # flush high card decides
        ("AhAdKcKsQhQc2s", "AhAdQhQcKc3s2s"),  # three pairs: the top two, then the kicker
    ],
)
def test_categories_and_kickers_order_correctly(better, worse):
    assert r(better) > r(worse)


def test_the_wheel_is_a_straight_and_the_ace_high_straight_beats_it():
    assert r("5s4d3c2hAsKdQc")[0] == 4
    assert r("AsKdQcJhTs2c3d") > r("5s4d3c2hAsKdQc")


def test_a_flush_uses_its_best_five_of_six():
    assert r("Ah9h5h3h2h8h7c")[1:] == (14, 9, 8, 5, 3)


def test_identical_strength_hands_chop():
    assert r("AsKs2c7h9dJcQd") == r("AdKd2c7h9dJcQd")


# -------------------------------------------------------------- side pots ----


def test_heads_up_equal_stacks_is_one_pot():
    assert side_pots({"a": 100, "b": 100}, ["a", "b"]) == [(200, ("a", "b"))]


def test_chips_nobody_else_could_win_go_with_the_pot_below():
    """A caller put in 5 more than the all-in and PokerNow returned nothing (hand
    #73 of pgl8vNV4WURe): the winner collected the lot, so the layer only the caller
    reached is not a pot of their own."""
    assert side_pots({"a": 910, "b": 915}, ["a", "b"]) == [(1825, ("a", "b"))]
    pots = side_pots({"a": 100, "b": 300, "c": 320}, ["a", "b", "c"])
    assert pots == [(300, ("a", "b", "c")), (420, ("b", "c"))]


def test_a_short_stack_makes_a_main_pot_and_a_side_pot():
    pots = side_pots({"a": 100, "b": 300, "c": 300}, ["a", "b", "c"])
    assert pots == [(300, ("a", "b", "c")), (400, ("b", "c"))]


def test_a_folded_players_chips_are_in_the_pot_but_never_theirs():
    pots = side_pots({"a": 100, "b": 100, "folder": 40}, ["a", "b"])
    assert pots == [(240, ("a", "b"))]
    assert sum(chips for chips, _ in pots) == 240


def test_pots_always_sum_to_the_total_contributed():
    contributed = {"a": 55, "b": 300, "c": 120, "d": 10, "e": 300}
    pots = side_pots(contributed, ["a", "b", "c", "e"])
    assert sum(chips for chips, _ in pots) == sum(contributed.values())
    assert [e for _, e in pots] == [("a", "b", "c", "e"), ("b", "c", "e"), ("b", "e")]


# ------------------------------------------------------ expected_collected ----


def test_on_the_river_the_winner_takes_it_all():
    got, method, n = expected_collected(["AsAh", "KsKh"], ["2c", "7d", "9h", "Jc", "3s"], [(100, (0, 1))])
    assert (got, method, n) == ([100.0, 0.0], "exact", 1)


def test_a_river_chop_splits_the_pot():
    got, *_ = expected_collected(["AsKs", "AdKd"], ["2c", "7h", "9d", "Jc", "3s"], [(100, (0, 1))])
    assert got == [50.0, 50.0]


def test_turn_outs_are_counted_exactly():
    """Ah7h on 2h 9h Qc 3s against KK: nine hearts and three aces of 44 cards."""
    got, method, n = expected_collected(["Ah7h", "KcKd"], ["2h", "9h", "Qc", "3s"], [(440, (0, 1))])
    assert (method, n) == ("exact", 44)
    assert got == pytest.approx([120.0, 320.0])


def test_a_flop_all_in_enumerates_every_turn_and_river():
    got, method, n = expected_collected(["8s8c", "AhAd"], ["8d", "Kc", "2h"], [(990, (0, 1))])
    assert (method, n) == ("exact", 990)
    assert sum(got) == pytest.approx(990)
    assert got[0] > 850  # a set against an overpair is a huge favourite


def test_side_pots_pay_only_the_eligible():
    """The short stack cannot win the side pot however good the river is."""
    hands = ["AsAh", "2c3d", "QhQc"]
    board = ["Ac", "Ad", "Kh", "Kd", "Qs"]
    pots = [(300, (0, 1, 2)), (200, (1, 2))]
    got, *_ = expected_collected(hands, board, pots)
    assert got == [300.0, 0.0, 200.0]


def test_preflop_is_sampled_reproducibly_and_lands_near_the_known_answer():
    """AA against KK is 82.4% in the books; the seed comes from the cards."""
    a, method, n = expected_collected(["AsAh", "KsKh"], [], [(1000, (0, 1))], samples=20000)
    b, *_ = expected_collected(["AsAh", "KsKh"], [], [(1000, (0, 1))], samples=20000)
    assert method == "sampled" and n == 20000
    assert a == b, "the same all-in must sample the same deals"
    assert a[0] == pytest.approx(824, abs=15)


def test_known_cards_are_never_dealt_again():
    """AsAh against 2h2d on 2c 9s 5c, counted by hand: AA wins 107 of 990 boards.

    45 cards are unseen. AA wins with one ace and any other card but the last
    deuce (2 x 43 - 2 = 84 boards: trips aces, or aces full over deuces full),
    both aces (1), running nines or running fives (3 + 3: board trips give AA the
    bigger full house), or a three and a four for the wheel (4 x 4 = 16). Deal a
    known card twice and the count moves, so this pins the deck as well as the
    ranking.
    """
    got, *_ = expected_collected(["AsAh", "2h2d"], ["2c", "9s", "5c"], [(990, (0, 1))])
    assert got == pytest.approx([107.0, 883.0])


def test_cache_key_keeps_player_order():
    assert cache_key(["AsAh", "KsKh"], ["2c"], [(10, (0, 1))]) != cache_key(
        ["KsKh", "AsAh"], ["2c"], [(10, (0, 1))]
    )
