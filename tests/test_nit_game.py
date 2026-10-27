"""The Nit Game: a side bet settled between hands, outside the pot.

Each player escrows the prize per opponent from their stack; the last player
not to win a pot pays it to everyone else, out of that escrow. Buying in,
carrying the escrow over and getting it back on leaving all move a player's own
chips, so only the penalty line changes anyone's net. The lines are shaped
exactly like pglEf0X7JnFpeFvqM2chCaaLV's.
"""

from __future__ import annotations

from pnt.ingest.csv_source import RawEntry
from pnt.logfmt import events as E
from pnt.logfmt.grammar import classify
from pnt.logfmt.parser import parse

GAME = "pgltest_nit"

PENALTY = '"Pay @ CCC" paid 40 each to "Win @ AAA", "Two @ BBB" as the Nit penalty.'

#: A Nit round opening, then the hand whose winner clears the last other Nit.
LINES = [
    "Asking interested players to confirm their Nit Game buy-in source.",
    'The player "Win @ AAA" confirmed the Nit Game using their stack.',
    'The player "Two @ BBB" confirmed the Nit Game using their stack.',
    'The player "Pay @ CCC" confirmed the Nit Game using their stack.',
    "Nit Game activated with 3 players.",
    '-- starting hand #1 (id: aaaa) No Limit Texas Hold\'em (dealer: "Win @ AAA") --',
    'Player stacks: #1 "Win @ AAA" (920) | #2 "Two @ BBB" (920) | #3 "Pay @ CCC" (920)',
    '"Two @ BBB" posts a small blind of 10',
    '"Pay @ CCC" posts a big blind of 20',
    '"Win @ AAA" folds',
    '"Two @ BBB" calls 20',
    '"Pay @ CCC" checks',
    "Flop:  [2♠, 7♦, K♣]",
    '"Two @ BBB" bets 20',
    '"Pay @ CCC" folds',
    'Uncalled bet of 20 returned to "Two @ BBB"',
    '"Two @ BBB" collected 40 from pot',
    "-- ending hand #1 --",
    '"Two @ BBB" cleared the Nit.',
    PENALTY,
]

#: Every other line the Nit Game writes: recognized, and moving nothing.
NOISE = [
    "Asking seated players if they want to join the Nit Game.",
    '"Win @ AAA", "Two @ BBB" confirmed the Nit Game using their existing Nit Escrow.',
    'The player "Two @ BBB" received 80 chips of returned Nit escrow when leaving the table.',
    # pgl-4BCPln1rscQ70WViLUY8D: an escrow funded by an add-on rather than the stack.
    'The player "Pay @ CCC" made an add-on of 90 to their Nit Escrow.',
    'The player "Pay @ CCC" confirmed the Nit Game using an add-on.',
    # pglDEDh0yMNBNqOQDmKmCO0Q_: a round called off for want of players.
    "Nit Game did not start because there were not enough confirmed players.",
]


def _parse(lines):
    return parse([RawEntry(ord=i, at="2026-01-01T00:00:00Z", entry=t) for i, t in enumerate(lines)], GAME)


def test_the_penalty_line_names_each_payee_and_the_amount_each():
    ev = classify(PENALTY, 0)
    assert isinstance(ev, E.NitPenalty)
    assert ev.payer.pn_id == "CCC" and ev.amount == 40
    assert [p.pn_id for p in ev.payees] == ["AAA", "BBB"]


def test_every_other_nit_line_is_noise():
    for line in LINES[:5] + [LINES[-2]] + NOISE:
        assert isinstance(classify(line, 0), E.Noise), line


def test_the_penalty_is_booked_on_the_hand_that_just_ended():
    res = _parse(LINES + NOISE)
    assert res.misses == []
    nit = {pid: p.nit for pid, p in res.hands[0].players.items()}
    assert nit == {"AAA": 40, "BBB": 40, "CCC": -80}


def test_a_round_that_did_not_start_books_nothing():
    """Confirmed buy-ins, then the round is called off: the hand after it is
    parsed as a plain hand, with no miss and no Nit money."""
    lines = LINES[:3] + ["Nit Game did not start because there were not enough confirmed players."]
    res = _parse(lines + LINES[5:-2])
    assert res.misses == []
    assert len(res.hands) == 1
    assert all(p.nit == 0 for p in res.hands[0].players.values())


def test_the_penalty_is_in_net_but_outside_the_pot_and_the_bounty():
    h = _parse(LINES).hands[0]
    assert h.total_contributed == h.total_collected == 40, "the pot still balances"
    assert all(p.bounty == 0 for p in h.players.values()), "not a 7-2 bounty"
    assert h.players["AAA"].net == 40  # folded for free, paid the prize
    assert h.players["BBB"].net == 20 + 40  # won the big blind, paid the prize
    assert h.players["CCC"].net == -20 - 80  # lost the big blind, paid both


def test_whoever_was_dealt_in_is_booked_like_the_bounty():
    """A side not dealt into the hand is skipped and the rest still booked: the
    money each player present moved is theirs, and the board's balance shows the gap."""
    lines = LINES[:-1] + ['"Pay @ CCC" paid 40 each to "Win @ AAA", "Ghost @ ZZZ" as the Nit penalty.']
    nit = {pid: p.nit for pid, p in _parse(lines).hands[0].players.items()}
    assert nit == {"AAA": 40, "BBB": 0, "CCC": -80}
    # The Nit sat out the hand that cleared the last other Nit: the payees are still paid.
    lines = LINES[:-1] + ['"Gone @ ZZZ" paid 40 each to "Win @ AAA", "Two @ BBB" as the Nit penalty.']
    nit = {pid: p.nit for pid, p in _parse(lines).hands[0].players.items()}
    assert nit == {"AAA": 40, "BBB": 40, "CCC": 0}


def test_the_penalty_reaches_the_database_and_net(tmp_path):
    from pnt.db.conn import connect
    from pnt.ingest.importer import ingest_entries, rebuild_game
    from pnt.stats.derive import derive
    from pnt.stats.h2h import hand_flows
    from pnt.stats.queries import load_hands

    entries = [RawEntry(ord=i, at="2026-01-01T00:00:00Z", entry=t) for i, t in enumerate(LINES)]
    conn = connect(tmp_path / "n.sqlite")
    ingest_entries(conn, GAME, entries, "test")
    rebuild_game(conn, GAME)
    hand = load_hands(conn, GAME)[0]
    conn.close()
    assert {pid: p.nit for pid, p in hand.players.items()} == {"AAA": 40, "BBB": 40, "CCC": -80}
    f = {x.pn_id: x for x in derive(hand)}
    assert (f["AAA"].net, f["BBB"].net, f["CCC"].net) == (40, 60, -100)
    flows, split = hand_flows(hand)
    assert flows[("CCC", "AAA")] == 40 and flows[("CCC", "BBB")] == 60
    assert not split, "one payer: the penalty settles exactly"
