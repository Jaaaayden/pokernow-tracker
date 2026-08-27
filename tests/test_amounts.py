"""The cumulative-vs-incremental bet amount rule.

`bets N`, `raises to N` and `calls N` all state a player's TOTAL commitment for
the street. Reading any of them as incremental corrupts every pot, every net-won
figure and every bb/100 -- while still producing plausible-looking numbers. That
is exactly the class of bug this project exists to avoid, so it gets the
strongest test available: a conservation law over every hand.
"""

from __future__ import annotations

import pytest


def test_chips_are_conserved_in_every_hand(parsed_all):
    """Σ contributed == Σ collected, per hand, everywhere.

    One assertion covering all 549 hands. If `calls N` were treated as
    incremental, callers would under-contribute and this would fail immediately
    and loudly.
    """
    bad = []
    for gid, res in parsed_all.items():
        for h in res.hands:
            if h.total_contributed != h.total_collected:
                bad.append((gid, h.hand_number, h.total_contributed, h.total_collected))
    assert not bad, f"{len(bad)} hand(s) where chips in != chips out: {bad[:5]}"


@pytest.mark.parametrize(
    "hand_number, expected_pot",
    [
        (180, 200),  # bet/call on all three streets
        (92, 480),   # flop bet 10 -> raise to 40 -> call 40 (incremental 30)
        (44, 100),   # all-in raise to 605 vs 50 committed; 555 returned
        (128, 300),  # raise/re-raise preflop then two barrels
        (113, 550),
    ],
)
def test_pinned_pot_totals(parsed_all, hand_number, expected_pot):
    """Pots reconstructed by hand from the raw log."""
    hands = {h.hand_number: h for h in parsed_all["pgl41zM3_CKphpnKM1DMIosUT"].hands}
    assert hands[hand_number].total_collected == expected_pot


def test_uncalled_bets_are_not_contributed(parsed_all):
    """Hand #187: Chris posts SB 5, raises to 30, opponent folds, 20 returned.

    He collects 20 and nets +10 -- the opponent's big blind, nothing more. If
    uncalled returns were counted as risked chips he would show -10.
    """
    hands = {h.hand_number: h for h in parsed_all["pgl41zM3_CKphpnKM1DMIosUT"].hands}
    chris = next(p for p in hands[187].players.values() if p.name == "Chris")
    assert chris.committed == 30
    assert chris.uncalled == 20
    assert chris.contributed == 10
    assert chris.collected == 20
    assert chris.net == 10


def test_forced_posts_are_flagged(parsed_all):
    """Every blind/ante/dead post carries is_forced; no voluntary action does."""
    for res in parsed_all.values():
        for h in res.hands:
            for a in h.actions:
                if a.kind == "post":
                    assert a.is_forced, f"post not flagged forced: {a}"
                    assert a.post_kind is not None
                else:
                    assert not a.is_forced, f"voluntary action flagged forced: {a}"


def test_run_it_twice_hands_still_balance(parsed_all):
    """Run-it-twice produces two `collected` lines; both must be accounted for."""
    rit = [h for res in parsed_all.values() for h in res.hands if h.run_count > 1]
    assert len(rit) >= 20, "fixtures should exercise run-it-twice"
    for h in rit:
        assert h.total_contributed == h.total_collected
