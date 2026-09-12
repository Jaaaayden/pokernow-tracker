"""A stack shorter than the blind it owes posts what it has, all in.

PokerNow writes that on one line, with the same ` and go all in` suffix it puts on
`bets`, `calls` and `raises to`:

    "LingWae @ FQN9hzhzP_" posts a big blind of 1 and go all in

The forced-post rule did not accept the suffix, so the line matched nothing. An
unmatched post is not a cosmetic gap: those chips never enter the pot, the hand
records no big-blind post at all, and every net figure in it is wrong. It is also
invisible in aggregate -- two lines in a 6,000-hand database -- which is exactly
why it gets a test rather than a fix alone.
"""

from __future__ import annotations

import pytest

from pnt.logfmt import events as E
from pnt.logfmt.grammar import classify

PLAYER = '"LingWae @ FQN9hzhzP_"'


@pytest.mark.parametrize(
    ("kind", "amount"),
    [
        ("small blind", 4),
        ("big blind", 1),
        ("ante", 2),
        ("straddle", 20),
        ("missed big blind", 10),
        ("missing small blind", 5),
    ],
)
def test_every_forced_post_may_be_all_in(kind, amount):
    ev = classify(f"{PLAYER} posts a {kind} of {amount} and go all in", 1)
    assert isinstance(ev, E.Post), f"{kind} all-in post did not parse: {ev}"
    assert ev.amount == amount
    assert ev.all_in is True


def test_a_plain_post_is_not_all_in():
    ev = classify(f"{PLAYER} posts a big blind of 10", 1)
    assert isinstance(ev, E.Post)
    assert ev.amount == 10
    assert ev.all_in is False


def test_the_trailing_space_the_live_log_carries_is_tolerated():
    """The live line ends in a space; the CSV export's does not. Same event."""
    live = classify(f"{PLAYER} posts a big blind of 1 and go all in ", 1)
    export = classify(f"{PLAYER} posts a big blind of 1 and go all in", 1)
    assert isinstance(live, E.Post) and isinstance(export, E.Post)
    assert (live.amount, live.all_in) == (export.amount, export.all_in)


def test_the_blind_reaches_the_pot_and_chips_are_conserved():
    """The whole point: an all-in blind is contributed, so the hand still balances.

    Hand #86 of a real game, trimmed to the lines that carry chips. LingWae is all
    in for 1 against a big blind of 10, so the main pot is 1 from each of the four
    players and LingWae can win only that; the other 27 goes to SplayWash. Before
    the fix LingWae contributed 0 and the pot was exactly one chip short.
    """
    from pnt.ingest.csv_source import RawEntry
    from pnt.logfmt.parser import parse

    lines = [
        (
    '-- starting hand #86 (id: vhc0qzlaif7f)  No Limit Texas Hold\'em'
    ' (dealer: "no more 72game @ gpP9uUffpu") --'
        ),
        (
    'Player stacks: #1 "LingWae @ FQN9hzhzP_" (1) | #3 "DK @ kCN0-Qy2Mz" (1079)'
    ' | #5 "no more 72game @ gpP9uUffpu" (3186) | #10 "SplayWash @ L0mhqQt-8H" (2734)'
        ),
        '"SplayWash @ L0mhqQt-8H" posts a small blind of 5',
        '"LingWae @ FQN9hzhzP_" posts a big blind of 1 and go all in ',
        '"DK @ kCN0-Qy2Mz" calls 10',
        '"no more 72game @ gpP9uUffpu" calls 10',
        '"SplayWash @ L0mhqQt-8H" calls 10',
        '"LingWae @ FQN9hzhzP_" collected 4 from pot',
        '"SplayWash @ L0mhqQt-8H" collected 27 from pot',
        "-- ending hand #86 --",
    ]
    entries = [RawEntry(ord=i, at="2026-09-11T00:00:00Z", entry=e) for i, e in enumerate(lines)]
    result = parse(entries, "test-allin-blind")

    assert not result.misses, f"unparsed lines: {result.misses}"
    (hand,) = result.hands
    assert hand.total_contributed == hand.total_collected == 31

    lingwae = hand.players["FQN9hzhzP_"]
    assert lingwae.committed == 1, "the all-in blind must reach the pot"

    post = next(a for a in hand.actions if a.pn_id == "FQN9hzhzP_" and a.kind == "post")
    assert post.post_kind == "bb"
    assert post.all_in is True
