"""The 7-2 bounty: a side bet settled between hands, outside the pot.

PokerNow logs the bounty as a run of ``paid N ... to X`` lines followed by a
single ``X collected N from the 7-2 bounty`` summary, emitted *after*
``-- ending hand #N --``. Two things can go wrong and neither is visible in the
resulting numbers: crediting the summary line as well as the payments pays the
winner twice, and folding bounty chips into `collected` breaks the pot
conservation law that every other amount test relies on.
"""

from __future__ import annotations

from pnt.ingest.csv_source import RawEntry
from pnt.logfmt import events as E
from pnt.logfmt.grammar import classify
from pnt.logfmt.parser import parse

GAME = "pgltest_bounty"

#: One three-handed hand where the button wins with 7-2 and is paid 30 by each
#: of the other two. Shaped exactly like the real logs: the settlement lines sit
#: between the hand-end line and the next hand-start line.
LINES = [
    '-- starting hand #1 (id: aaaa) No Limit Texas Hold\'em (dealer: "Win @ AAA") --',
    'Player stacks: #1 "Win @ AAA" (1000) | #2 "Pay1 @ BBB" (1000) | #3 "Pay2 @ CCC" (1000)',
    '"Pay1 @ BBB" posts a small blind of 10',
    '"Pay2 @ CCC" posts a big blind of 20',
    '"Win @ AAA" raises to 60',
    '"Pay1 @ BBB" folds',
    '"Pay2 @ CCC" folds',
    'Uncalled bet of 40 returned to "Win @ AAA"',
    '"Win @ AAA" collected 50 from pot',
    "-- ending hand #1 --",
    '"Pay1 @ BBB" paid 30 for the 7-2 bounty to "Win @ AAA"',
    '"Pay2 @ CCC" paid 30 for the 7-2 bounty to "Win @ AAA"',
    '"Win @ AAA" collected 60 from the 7-2 bounty',
]


def _parsed():
    entries = [RawEntry(ord=i, at="2026-01-01T00:00:00Z", entry=t) for i, t in enumerate(LINES)]
    return parse(entries, GAME)


def test_bounty_lines_are_recognized():
    """Both line kinds parse; the summary is noise, not a second payment."""
    paid = classify('"Pay1 @ BBB" paid 30 for the 7-2 bounty to "Win @ AAA"', 0)
    assert isinstance(paid, E.BountyPaid)
    assert paid.payer.pn_id == "BBB" and paid.payee.pn_id == "AAA"
    assert paid.amount == 30 and paid.kind == "7-2"

    summary = classify('"Win @ AAA" collected 60 from the 7-2 bounty', 0)
    assert isinstance(summary, E.Noise) and summary.kind == "bounty_summary"


def test_pot_collection_is_not_shadowed_by_the_bounty_rule():
    """The ordinary pot line must keep matching its own rule."""
    ev = classify('"Win @ AAA" collected 50 from pot', 0)
    assert isinstance(ev, E.Collected) and ev.amount == 50


def test_settlement_attaches_to_the_hand_that_just_ended():
    """The lines arrive with no open hand builder -- they must not be dropped."""
    res = _parsed()
    assert res.misses == []
    assert len(res.hands) == 1
    h = res.hands[0]
    assert h.players["AAA"].bounty == 60
    assert h.players["BBB"].bounty == -30
    assert h.players["CCC"].bounty == -30


def test_the_summary_line_does_not_pay_the_winner_twice():
    """60 in, 60 out. Counting the summary as well would make the winner's +120."""
    h = _parsed().hands[0]
    assert sum(p.bounty for p in h.players.values()) == 0
    assert h.players["AAA"].bounty == 60


def test_bounty_is_in_net_but_outside_the_pot():
    """`net` includes the side bet; the conservation law must still balance.

    The winner risked 20 (the 40 raise was returned uncalled) and collected 50,
    so the pot alone is +30; the bounty takes them to +90.
    """
    h = _parsed().hands[0]
    assert h.total_contributed == h.total_collected == 50, "the pot still balances"
    assert h.players["AAA"].collected == 50, "bounty chips stayed out of collected"
    assert h.players["AAA"].net == 90
    assert h.players["BBB"].net == -40  # 10 blind + 30 bounty
    assert h.players["CCC"].net == -50  # 20 blind + 30 bounty


def test_a_bounty_paid_to_someone_not_in_the_hand_is_ignored():
    """Defensive: an unknown pn_id is skipped, exactly as every handler does."""
    # Drop all three real settlement lines, then pay a player who was never dealt in.
    lines = LINES[:-3] + ['"Pay1 @ BBB" paid 30 for the 7-2 bounty to "Ghost @ ZZZ"']
    entries = [RawEntry(ord=i, at="t", entry=t) for i, t in enumerate(lines)]
    h = parse(entries, GAME).hands[0]
    assert h.players["BBB"].bounty == -30
    assert "ZZZ" not in h.players
