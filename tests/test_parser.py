"""Parse completeness, rosters, and position derivation."""

from __future__ import annotations

import pytest

from pnt.ingest.csv_source import read_csv
from pnt.logfmt.grammar import classify
from pnt.logfmt.parser import assign_positions, position_name

EXPECTED_HANDS = {
    "pgl41zM3_CKphpnKM1DMIosUT": 188,
    "pgl1UViJ4BhoVP-KKHpux1Mpv": 128,
    "pglSdQtyFGypDbrqD5IhXXlYz": 233,
}


def test_no_parse_misses(parsed_all):
    """Every line in every fixture is recognized.

    An empty `misses` list is the claim that the parse was total. When it is not
    empty the entries are recorded rather than dropped, which is what makes a gap
    detectable and repairable by re-import.
    """
    for gid, res in parsed_all.items():
        assert res.misses == [], f"{gid}: {res.misses[:3]}"


def test_hand_counts_and_no_gaps(parsed_all):
    for gid, expected in EXPECTED_HANDS.items():
        nums = sorted(h.hand_number for h in parsed_all[gid].hands)
        assert len(nums) == expected
        assert nums == list(range(1, expected + 1)), "hand numbers must be contiguous"


def test_entries_are_read_in_order_ascending():
    """`order` is the only safe sort key: `at` timestamps tie constantly."""
    from tests.conftest import HU

    entries = read_csv(HU)
    assert entries == sorted(entries, key=lambda e: e.ord)
    assert len({e.ord for e in entries}) == len(entries), "order must be unique"
    # Six entries share one millisecond in this log -- proving `at` is not enough.
    assert len({e.at for e in entries}) < len(entries)


def test_multiline_entries_survive_csv_parsing():
    """The 'Game Config Changes' block contains newlines inside one CSV field."""
    from tests.conftest import HU

    multiline = [e for e in read_csv(HU) if "\n" in e.entry]
    assert multiline, "fixture should contain a multi-line entry"
    assert any(e.entry.startswith("Game Config Changes") for e in multiline)


def test_roster_comes_from_player_stacks_not_join_events(parsed_all):
    """`joined the game` lines land *inside* the hand-start block; the roster is
    still taken from `Player stacks:` alone."""
    res = parsed_all["pgl41zM3_CKphpnKM1DMIosUT"]
    hand183 = next(h for h in res.hands if h.hand_number == 183)
    # Both players emit a "joined the game" line during this hand's start block.
    assert hand183.n_dealt_in == 2
    assert len(hand183.players) == 2


def test_every_dealt_in_player_gets_a_position(parsed_all):
    for gid, res in parsed_all.items():
        for h in res.hands:
            for p in h.players.values():
                assert p.seats_from_button is not None, f"{gid} #{h.hand_number}"
                assert p.seats_from_button >= 0
                if not h.blinds_irregular:
                    assert p.seats_from_button < h.n_dealt_in
                assert len({q.seats_from_button for q in h.players.values()}) == len(
                    h.players
                ), f"{gid} #{h.hand_number}: duplicate positions"


def test_big_blind_poster_always_lands_on_the_big_blind_slot(parsed_all):
    """The strongest available check on position derivation: whoever posts the BB
    must be at seats_from_button 2 (or 1 heads-up), on every single hand."""
    bad = []
    for gid, res in parsed_all.items():
        for h in res.hands:
            bb_actions = [a for a in h.actions if a.post_kind == "bb"]
            if len(bb_actions) != 1:
                continue
            p = h.players.get(bb_actions[0].pn_id)
            if p is None:
                continue
            expected = 1 if h.n_dealt_in == 2 else 2
            if p.seats_from_button != expected:
                bad.append((gid, h.hand_number, p.seats_from_button, expected))
    assert not bad, f"{len(bad)} hand(s) with a misplaced big blind: {bad[:5]}"


def test_heads_up_button_is_the_small_blind(parsed_all):
    """HU dealer posts the SB -- so the dealer must be seats_from_button 0."""
    checked = 0
    for res in parsed_all.values():
        for h in res.hands:
            if h.n_dealt_in != 2 or h.dealer_seat is None:
                continue
            dealer = next(p for p in h.players.values() if p.seat == h.dealer_seat)
            assert dealer.seats_from_button == 0
            assert position_name(0, 2) == "BTN/SB"
            checked += 1
    assert checked > 300


def test_dead_button_hand_is_flagged_and_still_positioned(parsed_all):
    """One real hand reads '(dead button)' and names no dealer at all."""
    dead = [h for res in parsed_all.values() for h in res.hands if h.dead_button]
    assert len(dead) == 1, "fixtures contain exactly one dead-button hand"
    h = dead[0]
    assert h.dealer_seat is None
    # Positions fall back to the big-blind anchor, so nobody is left unlabelled.
    assert all(p.seats_from_button is not None for p in h.players.values())


def test_positions_are_derived_over_dealt_in_players_not_seat_numbers():
    """A real hand is seated #1 #2 #3 #10. Seat 10 is not 'nine seats from the
    button' -- it is the next dealt-in player after seat 3."""
    got, irregular = assign_positions([1, 2, 3, 10], dealer_seat=1, bb_seat=3)
    assert got == {1: 0, 2: 1, 3: 2, 10: 3}
    assert irregular is False


def test_dead_small_blind_leaves_a_gap_in_the_rotation():
    """Hand #25 of `pgl1UViJ4`: a player quits, the small blind goes dead, and the
    button sits on seat 2 while seat 10 posts the BIG blind.

    A plain 0..n-1 rotation would call seat 10 the small blind. Reconciling
    against the big blind shifts everyone past the dead slot instead.
    """
    got, irregular = assign_positions([1, 2, 10], dealer_seat=2, bb_seat=10)
    assert irregular is True
    assert got[2] == 0, "the named dealer is still the button"
    assert got[10] == 2, "the big-blind poster must land on the big-blind slot"
    assert got[1] == 3, "the remaining player is pushed past the dead slot"


def test_position_anchors_agree_on_regular_hands(parsed_all):
    """Button-anchored and BB-anchored derivations are independent, so agreement
    across every regular hand is real evidence both are right.

    Hands with a dead blind are excluded -- that is precisely the case where a
    bare rotation is known to be wrong, and it is flagged rather than trusted.
    """
    mismatches = []
    irregular = 0
    for gid, res in parsed_all.items():
        for h in res.hands:
            if h.dealer_seat is None:
                continue
            if h.blinds_irregular:
                irregular += 1
                continue
            seats = [p.seat for p in h.players.values()]
            bb_seat = next(
                (
                    h.players[a.pn_id].seat
                    for a in h.actions
                    if a.post_kind == "bb" and a.pn_id in h.players
                ),
                None,
            )
            if bb_seat is None:
                continue
            by_button, _ = assign_positions(seats, h.dealer_seat, None)
            by_bb, _ = assign_positions(seats, None, bb_seat)
            if by_button != by_bb:
                mismatches.append((gid, h.hand_number, by_button, by_bb))
    assert not mismatches, f"{len(mismatches)} disagreement(s): {mismatches[:3]}"
    assert irregular == 1, "fixtures contain exactly one dead-blind hand"


@pytest.mark.parametrize(
    "sfb, n, expected",
    [
        (0, 2, "BTN/SB"), (1, 2, "BB"),
        (0, 3, "BTN"), (1, 3, "SB"), (2, 3, "BB"),
        (3, 4, "CO"),
        (3, 5, "HJ"), (4, 5, "CO"),
        # 6-max has no lojack: BTN SB BB UTG HJ CO.
        (0, 6, "BTN"), (1, 6, "SB"), (2, 6, "BB"),
        (3, 6, "UTG"), (4, 6, "HJ"), (5, 6, "CO"),
        # 7-handed inserts LJ and keeps a single UTG.
        (3, 7, "UTG"), (4, 7, "LJ"), (5, 7, "HJ"), (6, 7, "CO"),
        (4, 9, "UTG+1"), (8, 9, "CO"),
    ],
)
def test_position_names(sfb, n, expected):
    assert position_name(sfb, n) == expected


def test_unknown_lines_become_unknown_not_exceptions():
    """One unfamiliar line must not abort an import of thousands of hands."""
    from pnt.logfmt.events import Unknown

    ev = classify('"someone @ abc" invents a brand new action', 1)
    assert isinstance(ev, Unknown)
    assert ev.raw.startswith('"someone')


def test_hostile_player_names_parse_correctly():
    """Real players in these logs are named `all in` and `500`."""
    from pnt.logfmt.events import Action

    ev = classify('"all in @ ILQFEWJ0SW" calls 630 and go all in', 1)
    assert isinstance(ev, Action)
    assert ev.player.name == "all in"
    assert ev.player.pn_id == "ILQFEWJ0SW"
    assert ev.kind == "call"
    assert ev.amount_to == 630
    assert ev.all_in is True
