"""The cumulative-vs-incremental bet amount rule.

`bets N`, `raises to N` and `calls N` all state a player's TOTAL commitment for
the street. Reading any of them as incremental corrupts every pot, every net-won
figure and every bb/100 -- while still producing plausible-looking numbers. That
is exactly the class of bug this project exists to avoid, so it gets the
strongest test available: a conservation law over every hand.
"""

from __future__ import annotations

import pytest


def test_chips_are_conserved_in_every_hand(parsed_every):
    """Σ contributed == Σ collected, per complete hand, in every fixture log.

    One assertion covering the whole corpus. If `calls N` were treated as
    incremental, callers would under-contribute and this would fail immediately
    and loudly.

    Hands the export truncated mid-play are excluded: their chips are genuinely
    only half-recorded, so they are partial rather than wrong. They are asserted
    on separately in `test_parser.py`, so exclusion here cannot hide a hand that
    is simply mis-parsed.
    """
    bad = []
    for gid, res in parsed_every.items():
        for h in res.hands:
            if not h.complete:
                continue
            if h.total_contributed != h.total_collected:
                bad.append((gid, h.hand_number, h.total_contributed, h.total_collected))
    assert not bad, f"{len(bad)} hand(s) where chips in != chips out: {bad[:5]}"


def test_live_forced_posts_state_a_street_total(parsed_edge):
    """A straddle is a raise: it restates the street total, it does not add to it.

    Hand #2 is heads-up 2/5. The small blind posts 2, then straddles to 10 -- a
    total of 10, not 12 -- the big blind folds, 5 comes back and 10 is collected.
    Read additively the pot is 12 in / 10 out, and the 2 goes missing from every
    downstream figure.
    """
    from tests.conftest import STRADDLE_GAME

    h = next(x for x in parsed_edge[STRADDLE_GAME].hands if x.hand_number == 2)
    straddler = next(p for p in h.players.values() if p.collected == 10)
    assert straddler.committed == 10, "straddle restates the street total"
    assert straddler.contributed == 5, "…of which 5 came back uncalled"
    assert h.total_contributed == h.total_collected == 10


def test_missed_big_blind_absorbs_the_small_blind_posted_with_it(parsed_edge):
    """A returning player's missed BB is cumulative too, for the same reason.

    Hand #74: pebus posts a small blind of 5, a *missing* small blind of 5 (dead
    chips, real money) and a missed big blind of 10. The live commitment is 10,
    not 15 -- the 5 already posted is inside it -- so 15 goes in, and the 35 the
    winner collects proves it.
    """
    from tests.conftest import MISSED_BLINDS_GAME

    h = next(x for x in parsed_edge[MISSED_BLINDS_GAME].hands if x.hand_number == 74)
    pebus = h.players["ILQFEWJ0SW"]
    assert pebus.committed == 15, "live 10 (SB absorbed into the missed BB) + 5 dead"
    assert h.total_contributed == h.total_collected == 35


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
