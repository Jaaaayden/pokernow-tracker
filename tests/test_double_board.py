"""Double Board: two boards dealt on every street, one pot split between them.

Seen in a real game (`pglZajS5mhezuh8YfoFI0q0KO`, hands #152-153) but absent from
the fixture corpus, so these lines are copied from that log with the names
changed. Before the grammar knew `(second board)`, each such hand dropped its
second board as six parse misses. The chips still balanced -- the split-pot
`collected` lines parse -- which is exactly why nothing looked wrong.
"""

from __future__ import annotations

from pnt.ingest.csv_source import RawEntry
from pnt.logfmt import events as E
from pnt.logfmt.grammar import classify
from pnt.logfmt.parser import parse

#: Hand #153: a 3-bet pot, both boards checked to the river, a river bet called,
#: and each player wins one board. Spacing is as logged, double spaces included.
LINES = [
    '-- starting hand #153 (id: nbgmaba7fkve)  No Limit Texas Hold\'em (dealer: "Cat @ CCC") --',
    'Player stacks: #1 "Ann @ AAA" (1100) | #2 "Cat @ CCC" (1399) | #9 "Bob @ BBB" (3066)',
    "Your hand is 10♠, 10♣",
    '"Bob @ BBB" posts a small blind of 5',
    '"Ann @ AAA" posts a big blind of 10',
    '"Cat @ CCC" calls 10',
    '"Bob @ BBB" raises to 40',
    '"Ann @ AAA" raises to 160',
    '"Cat @ CCC" folds',
    '"Bob @ BBB" calls 160',
    "Flop:  [8♦, J♦, Q♥]",
    "Flop (second board):  [4♠, 9♠, 3♠]",
    '"Bob @ BBB" checks',
    '"Ann @ AAA" checks',
    "Turn: 8♦, J♦, Q♥ [8♥]",
    "Turn (second board): 4♠, 9♠, 3♠ [6♦]",
    '"Bob @ BBB" checks',
    '"Ann @ AAA" checks',
    "River: 8♦, J♦, Q♥, 8♥ [J♠]",
    "River (second board): 4♠, 9♠, 3♠, 6♦ [9♣]",
    '"Bob @ BBB" bets 30',
    '"Ann @ AAA" calls 30',
    '"Bob @ BBB" shows a J♥, K♥.',
    '"Ann @ AAA" shows a 10♠, 10♣.',
    (
        "\"Bob @ BBB\" collected 195 from pot with Full House, J's over 8's"
        " (combination: 8♦, 8♥, J♥, J♦, J♠)"
    ),
    (
        "\"Ann @ AAA\" collected 195 from pot with Two Pair, 10's & 9's on the second board"
        "  (combination: 6♦, 9♠, 9♣, 10♠, 10♣)"
    ),
    "-- ending hand #153 --",
]


def _hand():
    entries = [RawEntry(ord=i, at="2026-09-03T09:28:22Z", entry=t) for i, t in enumerate(LINES)]
    result = parse(entries, "pgltest_double_board")
    assert result.misses == []
    (hand,) = result.hands
    return hand


def test_second_board_lines_are_recognized():
    ev = classify("Flop (second board):  [4♠, 9♠, 3♠]", 0)
    assert isinstance(ev, E.StreetDealt)
    assert (ev.street, ev.run, ev.board) == (E.FLOP, 1, ("4s", "9s", "3s"))


def test_run_it_twice_lines_still_parse_as_run_one():
    ev = classify("Turn (second run): 5♥, 3♥, 6♠ [5♦]", 0)
    assert isinstance(ev, E.StreetDealt) and ev.run == 1


def test_both_boards_are_kept():
    h = _hand()
    assert h.run_count == 2
    assert h.board_runs == [["8d", "Jd", "Qh", "8h", "Js"], ["4s", "9s", "3s", "6d", "9c"]]
    assert h.saw_street(E.RIVER), "streets follow the first board"


def test_the_split_pot_balances():
    h = _hand()
    assert h.total_contributed == h.total_collected == 390
    assert h.players["AAA"].collected == h.players["BBB"].collected == 195
    assert h.players["CCC"].contributed == 10


def test_betting_between_the_boards_lands_on_its_street():
    """Unlike run it twice, action continues after a second board is dealt."""
    h = _hand()
    river = [(a.pn_id, a.kind, a.amount) for a in h.actions if a.street == E.RIVER]
    assert river == [("BBB", "bet", 30), ("AAA", "call", 30)]
    assert h.players["AAA"].contributed == h.players["BBB"].contributed == 190  # 160 + 30
