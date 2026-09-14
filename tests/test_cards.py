"""Card classification: preflop classes and made-hand strength."""

from __future__ import annotations

import pytest

from pnt.stats.cards import (
    ALL_CLASSES,
    DRAW_DETAILS,
    MADE_CLASSES,
    TEXTURE_TAGS,
    board_texture,
    grid_labels,
    hand_class,
    made_class,
    made_hand,
    parse_cards,
)


@pytest.mark.parametrize(
    "cards, expected",
    [
        ("QsQh", "QQ"),
        ("AsKs", "AKs"),
        ("KdAh", "AKo"),  # order printed by PokerNow does not matter
        ("2c3c", "32s"),
        ("Ts9d", "T9o"),
        (["10s", "9d"], "T9o"),  # both spellings of ten
        ("AsAd", "AA"),
    ],
)
def test_hand_class_is_canonical(cards, expected):
    assert hand_class(cards) == expected


def test_grid_has_169_distinct_cells_in_chart_layout():
    rows = grid_labels()
    assert len(rows) == 13 and all(len(r) == 13 for r in rows)
    assert len(set(ALL_CLASSES)) == 169
    assert rows[0][0] == "AA" and rows[12][12] == "22"
    assert rows[0][1] == "AKs", "suited above the diagonal"
    assert rows[1][0] == "AKo", "offsuit below the diagonal"
    # every class a real holding can produce is a cell
    assert hand_class("KdAh") in ALL_CLASSES


def test_bad_card_token_is_rejected():
    with pytest.raises(ValueError, match="bad card token"):
        parse_cards("ZZ")


@pytest.mark.parametrize(
    "cards, expected",
    [
        ("AsKsQsJsTs2c3d", "straight_flush"),
        ("As2s3s4s5sKdKc", "straight_flush"),  # steel wheel beats the kings
        ("9c9d9h9sAcKd2s", "quads"),
        ("9c9d9hAcAd2s3s", "full_house"),
        ("9c9d9hAcAdAh2s", "full_house"),  # two trips still make a boat
        ("2s7s9sJsKs3c4d", "flush"),
        ("5c6d7h8s9c2d3h", "straight"),
        ("Ac2d3h4s5cKdQd", "straight"),  # the wheel
        ("9c9d9hAcKd2s3s", "trips"),
        ("9c9dAcAd2s3s7h", "two_pair"),
        ("9c9dAcKd2s3s7h", "pair"),
        ("9cJdAcKd2s3s7h", "high_card"),
    ],
)
def test_made_class(cards, expected):
    assert made_class(parse_cards(cards)) == expected
    assert expected in MADE_CLASSES


@pytest.mark.parametrize(
    "hole, board, cls, detail",
    [
        ("ThTc", ["7c", "3s", "Qh", "8h", "Ac"], "pair", "pocket_pair"),  # HU hand 18
        ("KhKc", ["7c", "3s", "Qh", "8h", "2c"], "pair", "overpair"),
        ("AhKc", ["Ac", "3s", "Qh", "8h", "2c"], "pair", "top_pair"),
        ("QhKc", ["Ac", "3s", "Qd", "8h", "2c"], "pair", "middle_pair"),
        ("2h5c", ["Ac", "3s", "Qd", "8h", "2c"], "pair", "bottom_pair"),
        ("Jh5c", ["Ac", "3s", "Qd", "8h", "8c"], "pair", "board_pair"),
        ("9dAs", ["Jc", "9h", "7d", "9c", "Qh"], "trips", "trips"),  # HU hand 92
        ("9d9s", ["Jc", "9h", "7d", "2c", "Qh"], "trips", "set"),
        ("AhKh", ["Qh", "Jh", "Th", "2c", "3d"], "straight_flush", None),
        ("AhKd", [], "no_board", None),  # shown after a preflop win
        ("AhKd", ["Ac", "Kc", "2d"], "two_pair", None),  # classifies on a flop-only board
    ],
)
def test_made_hand_detail(hole, board, cls, detail):
    assert made_hand(hole, board) == (cls, detail)


DRAW_CASES = [
    ("AhKh", ["Qh", "Jh", "2c"], "draw", "combo_draw"),  # flush draw plus a gutshot to the T
    ("Ah8h", ["Kh", "9h", "2c", "7d", "3s"], "draw", "flush_draw"),  # missed on the river
    ("9c8d", ["7h", "6s", "2c", "Kd", "Ah"], "draw", "open_ender"),  # T and 5 both complete it
    ("9d7c", ["5h", "8s", "Jd", "2c"], "draw", "open_ender"),  # double gutshot: 6 and T
    ("JcTd", ["7h", "8s", "2c"], "draw", "gutshot"),  # flop-only board
    ("Ah2c", ["3d", "4s", "Kh", "9c"], "draw", "gutshot"),  # wheel draw, the ace plays low
    ("Kc2d", ["5h", "6s", "7d", "8c", "Ah"], "high_card", None),  # board straight draw only
    ("Kc2d", ["5h", "6h", "7h", "8h", "As"], "high_card", None),  # board flush draw only
    ("AhKh", ["Ac", "Jh", "2h", "7d", "3s"], "pair", "top_pair"),  # a pair keeps its class
    ("9cJd", ["Ac", "Kd", "2s", "3s", "7h"], "high_card", None),  # true air stays high card
]


@pytest.mark.parametrize("hole, board, cls, detail", DRAW_CASES)
def test_draws_split_from_high_card(hole, board, cls, detail):
    assert made_hand(hole, board) == (cls, detail)


def test_draw_details_are_declared():
    assert MADE_CLASSES.index("pair") < MADE_CLASSES.index("draw") < MADE_CLASSES.index("high_card")
    for _, _, cls, detail in DRAW_CASES:
        if cls == "draw":
            assert detail in DRAW_DETAILS



@pytest.mark.parametrize(
    "board, present, absent",
    [
        (["Ah", "7d", "2c"], {"ace_high", "rainbow", "unpaired", "connected"}, {"paired", "twotone", "low"}),
        # A-2 counts as connected (the wheel), A-7-2 has no three in a row
        (["Ah", "7d", "2c"], {"connected"}, {"three_connected"}),
        (["Ks", "Qs", "Js"], {"king_high", "monotone", "flush_possible", "three_connected", "all_broadway"}, {"rainbow"}),
        (["8h", "7h", "3c"], {"low", "twotone", "connected", "no_broadway"}, {"three_connected", "flush_possible"}),
        (["9c", "9d", "4s"], {"low", "paired", "rainbow", "disconnected"}, {"unpaired", "connected"}),
        (["Tc", "Td", "Th"], {"trips", "paired", "ten_high"}, {"unpaired", "double_paired"}),
        (["Qc", "Qd", "5h", "5s"], {"double_paired", "paired", "queen_high"}, {"trips"}),
        (["Jc", "6d", "2h"], {"jack_high", "disconnected"}, {"connected"}),
        # a five-card board: three hearts means a flush is possible, not monotone
        (["Ah", "Kh", "2c", "9h", "3d"], {"flush_possible", "connected"}, {"monotone", "rainbow", "twotone"}),
    ],
)
def test_board_texture(board, present, absent):
    tags = board_texture(board)
    assert present <= tags, tags
    assert not (absent & tags), tags
    assert tags <= set(TEXTURE_TAGS)


def test_no_texture_before_the_flop():
    assert board_texture([]) == frozenset()
    assert board_texture(["Ah"]) == frozenset()
