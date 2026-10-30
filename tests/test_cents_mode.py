"""Line shapes no real log in the corpus carries: Cents Mode amounts, antes, a `|`
in a name, a hand with no big blind post. Each one used to parse to a hand with
no players or no money, so each is pinned here on synthetic lines.

The fixture `pglCentsModeSynthetic01` is made up (see tests/fixtures/README.md):
two hands in Cents Mode, then the mode switched off and a third in whole units.
"""

from __future__ import annotations

import pytest

from pnt.ingest.csv_source import RawEntry, read_csv
from pnt.logfmt import events as E
from pnt.logfmt.grammar import classify
from pnt.logfmt.parser import parse
from tests.conftest import FIXTURES, TRUNCATED, TRUNCATED_GAME

CENTS = FIXTURES / "poker_now_log_pglCentsModeSynthetic01.csv"
CENTS_GAME = "pglCentsModeSynthetic01"


def _parse_lines(lines):
    return parse([RawEntry(ord=i, at="2026-01-01T00:00:00Z", entry=t) for i, t in enumerate(lines)], "pgltest")


@pytest.fixture(scope="module")
def cents():
    return parse(read_csv(CENTS), CENTS_GAME)


def test_a_cents_mode_log_parses_in_full(cents):
    assert cents.misses == []
    assert [h.hand_number for h in cents.hands] == [1, 2, 3]
    for h in cents.hands:
        assert h.complete and h.n_dealt_in == 3
        assert h.total_contributed == h.total_collected > 0


def test_every_amount_shares_one_unit_across_the_mode_switch(cents):
    """Cents Mode prints each unit divided by 100. Read back as whole units, the
    hands either side of the switch are played at one blind, and each hand's
    stacks are the last hand's plus its result."""
    assert [h.bb for h in cents.hands] == [10, 10, 10]
    for before, after in zip(cents.hands, cents.hands[1:]):
        for pid, p in after.players.items():
            q = before.players[pid]
            assert p.starting_stack == q.starting_stack + q.net
    first = cents.hands[0]
    assert {p.name: p.starting_stack for p in first.players.values()} == {
        "Ann": 1000, "Pipe|Dream": 1000, "Cy": 1000,
    }
    assert {p.name: p.net for p in first.players.values()} == {"Ann": 37, "Pipe|Dream": -6, "Cy": -31}


def test_the_game_s_blinds_are_its_hands_blinds(cents):
    assert cents.blinds == {"sb": 5, "bb": 10, "ante": 1}


def test_a_cents_mode_game_stores_whole_unit_blinds(tmp_path):
    from pnt.db.conn import connect
    from pnt.ingest.importer import import_csv

    conn = connect(tmp_path / "t.sqlite")
    import_csv(conn, CENTS)
    import_csv(conn, TRUNCATED)
    games = {r["game_id"]: (r["sb"], r["bb"]) for r in conn.execute("SELECT game_id, sb, bb FROM games")}
    assert games[CENTS_GAME] == (5, 10)
    # Its blinds were changed to 0.05 in Cents Mode and 10 after it: one unit now.
    assert games[TRUNCATED_GAME] == (5, 10)
    bbs = {r["bb"] for r in conn.execute("SELECT bb FROM hands WHERE game_id = ?", (CENTS_GAME,))}
    assert bbs == {10}


@pytest.mark.parametrize(
    "line, cents, amount",
    [
        ('"A @ x1" calls 0.50', False, 50),
        ('"A @ x1" calls 0.50', True, 50),
        ('"A @ x1" calls 50', False, 50),
        # In Cents Mode a whole amount is still in hundredths.
        ('"A @ x1" calls 1', True, 100),
        ('"A @ x1" raises to 1.25 and go all in', False, 125),
        ('"A @ x1" bets 0.07', False, 7),
    ],
)
def test_decimal_amounts_are_read_as_whole_units(line, cents, amount):
    ev = classify(line, 1, cents=cents)
    assert isinstance(ev, E.Action) and ev.amount_to == amount


@pytest.mark.parametrize(
    "line, kind, amount",
    [
        ('"A @ x1" posts a big blind of 0.10', E.Post, 10),
        ('"A @ x1" posts a bet of 0.40 (bomb pot bet)', E.Post, 40),
        ('Uncalled bet of 0.25 returned to "A @ x1"', E.UncalledReturn, 25),
        ('"A @ x1" collected 1.73 from pot with Pair, 9\'s (combination: 9♥, 9♣, A♠, J♣, 5♥)', E.Collected, 173),
        ('"A @ x1" paid 0.20 for the 7-2 bounty to "B @ y2"', E.BountyPaid, 20),
        ('"A @ x1" paid 0.30 each to "B @ y2", "C @ z3" as the Nit penalty.', E.NitPenalty, 30),
    ],
)
def test_every_money_line_takes_a_decimal(line, kind, amount):
    ev = classify(line, 1)
    assert isinstance(ev, kind) and ev.amount == amount


@pytest.mark.parametrize(
    "line",
    [
        'The player "A @ x1" joined the game with a stack of 10.00.',
        'The admin approved the player "A @ x1" participation with a stack of 10.00.',
        'The admin updated the player "A @ x1" stack from 10.00 to 12.50.',
        'The player "A @ x1" requested a rebuy of 10.00.',
        'The player "A @ x1" rebought. New stack 10.00.',
        'The player "A @ x1" made an add-on of 0.30 to their Nit Escrow.',
        'The player "A @ x1" received 0.30 chips of returned Nit escrow when leaving the table.',
        '"A @ x1" collected 0.40 from the 7-2 bounty',
    ],
)
def test_decimal_admin_lines_are_recognized(line):
    assert not isinstance(classify(line, 1), E.Unknown)


def test_a_cents_blind_change_is_in_whole_units():
    ev = classify("The game's small blind was changed from 0.10 to 0.05.", 1)
    assert isinstance(ev, E.BlindChange) and (ev.from_amount, ev.to_amount) == (10, 5)


@pytest.mark.parametrize("article", ["a", "an"])
def test_an_ante_post_is_read_with_either_article(article):
    ev = classify(f'"A @ x1" posts {article} ante of 5', 1)
    assert isinstance(ev, E.Post) and ev.kind == E.POST_ANTE and ev.amount == 5


def test_a_pipe_in_a_name_keeps_the_roster():
    ev = classify('Player stacks: #1 "a|b @ x1" (1000) | #3 "c | d @ y2" (950) | #10 "e @ z3" (5)', 1)
    assert isinstance(ev, E.PlayerStacks)
    assert [(s, ref.name, ref.pn_id, stack) for s, ref, stack in ev.seats] == [
        (1, "a|b", "x1", 1000), (3, "c | d", "y2", 950), (10, "e", "z3", 5),
    ]


def test_a_name_with_a_quote_and_paren_keeps_the_roster():
    ev = classify('Player stacks: #1 "a" (b @ x1" (1000) | #2 "c @ y2" (950)', 1)
    assert isinstance(ev, E.PlayerStacks)
    assert [ref.name for _, ref, _ in ev.seats] == ['a" (b', "c"]


def test_a_roster_that_does_not_parse_leaves_the_hand_incomplete():
    """No players means no chips on record: the hand cannot pass for a finished one."""
    res = _parse_lines([
        '-- starting hand #1 (id: aaaa)  No Limit Texas Hold\'em (dealer: "A @ AAA") --',
        'Player stacks: #1 "A @ AAA" (1000) | garbage',
        '"A @ AAA" posts a small blind of 5',
        "-- ending hand #1 --",
    ])
    assert len(res.misses) == 1
    assert [(h.n_dealt_in, h.complete) for h in res.hands] == [(0, False)]


def test_a_hand_with_no_big_blind_post_is_played_at_the_table_blind():
    lines = [
        '-- starting hand #1 (id: aaaa)  No Limit Texas Hold\'em (dealer: "A @ AAA") --',
        'Player stacks: #1 "A @ AAA" (1000) | #2 "B @ BBB" (1000) | #3 "C @ CCC" (1000)',
        '"B @ BBB" posts a small blind of 5',
        '"C @ CCC" posts a big blind of 10',
        '"A @ AAA" folds',
        '"B @ BBB" folds',
        'Uncalled bet of 5 returned to "C @ CCC"',
        '"C @ CCC" collected 10 from pot',
        "-- ending hand #1 --",
        # The big blind left: a dead big blind, so nobody posts one.
        '-- starting hand #2 (id: bbbb)  No Limit Texas Hold\'em (dealer: "B @ BBB") --',
        'Player stacks: #1 "A @ AAA" (1000) | #2 "B @ BBB" (995)',
        '"A @ AAA" posts a small blind of 5',
        '"B @ BBB" folds',
        '"A @ AAA" collected 5 from pot',
        "-- ending hand #2 --",
    ]
    res = _parse_lines(lines)
    assert [h.bb for h in res.hands] == [10, 10]
