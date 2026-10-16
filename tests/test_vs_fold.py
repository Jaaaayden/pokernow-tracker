"""bb/100 against folding, and the forced posts it is measured from.
See SPEC.md, "bb/100 against folding"."""

from __future__ import annotations

import math
from fractions import Fraction

import pytest

from pnt.stats.derive import HandAction, HandPlayerRow, HandRow, derive
from pnt.stats.queries import Tally, aggregate, facts_for, load_hands, rates, report, tally
from tests.conftest import EDGE_LOGS, HU_GAME, MISSED_BLINDS_GAME, STRADDLE_GAME

HERO, A, B = "hero", "a", "b"


def mk(players: list[str], actions: list[tuple], collected: dict[str, int], *, hand_id: int = 1,
       complete: bool = True, bb: int = 10) -> HandRow:
    """Seats in `players` order from the button. `actions` are (pid, kind, amount)
    or (pid, "post", amount, post_kind), all preflop."""
    acts = [
        HandAction(seq=i + 1, street="preflop", pn_id=a[0], kind=a[1], amount=a[2],
                   is_forced=a[1] == "post", all_in=False, post_kind=a[3] if len(a) > 3 else None)
        for i, a in enumerate(actions)
    ]
    folded = {a.pn_id for a in acts if a.kind == "fold"}
    rows = {
        pid: HandPlayerRow(
            pn_id=pid, seat=i + 1, seats_from_button=i,
            contributed=sum(a.amount for a in acts if a.pn_id == pid),
            collected=collected.get(pid, 0), folded=pid in folded, starting_stack=1000,
        )
        for i, pid in enumerate(players)
    }
    return HandRow(
        hand_id=hand_id, game_id="g", hand_number=hand_id, n_dealt_in=len(players), dead_button=False,
        blinds_irregular=False, went_to_showdown=False, saw_flop=False, complete=complete, bb=bb,
        ts=None, players=rows, actions=acts,
    )


def hero(hand: HandRow):
    return next(f for f in derive(hand) if f.pn_id == HERO)


# Three-handed, the button first: hero folds at every decision, from every seat.
SB_FOLDS = mk([A, HERO, B], [(HERO, "post", 5, "sb"), (B, "post", 10, "bb"), (A, "raise", 30),
                             (HERO, "fold", 0), (B, "fold", 0)], {A: 45}, hand_id=1)
BB_FOLDS = mk([B, A, HERO], [(A, "post", 5, "sb"), (HERO, "post", 10, "bb"), (B, "raise", 30),
                             (A, "fold", 0), (HERO, "fold", 0)], {B: 45}, hand_id=2)
BUTTON_FOLDS = mk([HERO, A, B], [(A, "post", 5, "sb"), (B, "post", 10, "bb"), (HERO, "fold", 0),
                                 (A, "call", 5), (B, "check", 0)], {B: 20}, hand_id=3)
#: Everyone folds to hero's big blind: no decision, and +5 whatever they would have done.
WALK = mk([B, A, HERO], [(A, "post", 5, "sb"), (HERO, "post", 10, "bb"), (B, "fold", 0),
                         (A, "fold", 0)], {HERO: 15}, hand_id=4)
#: A dead post: the ante is gone the moment hero folds, like a blind.
ANTE_FOLDS = mk([HERO, A, B], [(HERO, "post", 2, "ante"), (A, "post", 5, "sb"), (B, "post", 10, "bb"),
                               (HERO, "fold", 0), (A, "fold", 0)], {B: 17}, hand_id=5)
FOLDS = [SB_FOLDS, BB_FOLDS, BUTTON_FOLDS, WALK, ANTE_FOLDS]


def test_posted_is_every_forced_chip_in_big_blinds():
    assert hero(SB_FOLDS).posted_bb == Fraction(1, 2)
    assert hero(BB_FOLDS).posted_bb == 1
    assert hero(BUTTON_FOLDS).posted_bb == 0
    assert hero(ANTE_FOLDS).posted == 2 and hero(ANTE_FOLDS).posted_bb == Fraction(1, 5)
    unknown = mk([HERO, A], [(HERO, "post", 5, "sb"), (A, "post", 10, "bb")], {}, bb=None)
    assert hero(unknown).posted == 5 and hero(unknown).posted_bb is None


def test_folding_every_hand_is_zero_against_folding():
    rows = [hero(h) for h in FOLDS]
    r = aggregate(rows)
    assert r["bb_per_100"] < 0  # raw: the blinds and the ante, less the walk
    assert r["bb_per_100_vs_fold"] == 0
    assert r["bb_per_100_vs_fold_ci"] == 0


def test_a_walk_adds_nothing_and_a_defended_blind_adds_its_post():
    """+5 in a walk is not won against folding; the big blind's post is, once
    they had a decision, whatever they did with it."""
    assert aggregate([hero(WALK)])["bb_per_100_vs_fold"] == 0
    # Hero defends the big blind and takes the 65 pot: net +35 is +3.5 bb, and
    # against folding (−1 bb) it is +4.5.
    won = mk([B, A, HERO], [(A, "post", 5, "sb"), (HERO, "post", 10, "bb"), (B, "raise", 30),
                            (A, "fold", 0), (HERO, "call", 20)], {HERO: 65}, hand_id=6)
    r = aggregate([hero(won)])
    assert r["bb_per_100"] == 350.0 and r["bb_per_100_vs_fold"] == 450.0


def test_an_incomplete_hand_is_out_of_both_sides():
    cut = mk([A, HERO, B], [(HERO, "post", 5, "sb"), (B, "post", 10, "bb"), (A, "raise", 30),
                            (HERO, "call", 25)], {}, hand_id=7, complete=False)
    base = aggregate([hero(h) for h in FOLDS])
    with_cut = aggregate([*(hero(h) for h in FOLDS), hero(cut)])
    assert with_cut["hands"] == base["hands"] + 1
    assert with_cut["bb_per_100_vs_fold"] == base["bb_per_100_vs_fold"]


def _manual(rows) -> tuple[float, float]:
    xs = [
        (Fraction(f.net, f.bb_size) + f.posted_bb) if f.vpip_opp else Fraction(0)
        for f in rows if f.bb_size and f.complete
    ]
    n = len(xs)
    mean = sum(xs) / n
    sd = math.sqrt(float(sum((x - mean) ** 2 for x in xs) / (n - 1)))
    return round(float(100 * mean), 2), round(1.96 * sd / math.sqrt(n) * 100, 2)


def test_the_rate_and_its_interval_match_a_hand_count(db):
    rows = facts_for(db, "genericpoker")
    r = aggregate(rows)
    assert (r["bb_per_100_vs_fold"], r["bb_per_100_vs_fold_ci"]) == _manual(rows)
    # Heads-up, every hand pays a blind: against folding is well above raw.
    assert r["bb_per_100_vs_fold"] > r["bb_per_100"]


def test_tallies_add(db):
    rows = facts_for(db, "Chris")
    half = len(rows) // 2
    t = tally(rows[:half])
    t += tally(rows[half:])
    whole = tally(rows)
    for slot in Tally.__slots__:
        assert getattr(t, slot) == getattr(whole, slot), slot
    assert rates(t)["bb_per_100_vs_fold"] == rates(whole)["bb_per_100_vs_fold"]


def test_the_cached_report_agrees_with_a_fresh_derivation(db):
    """The report is assembled from per-game tallies; vs fold must add across them."""
    for row in report(db):
        mine = aggregate(facts_for(db, row["player"]))
        assert row["bb_per_100_vs_fold"] == mine["bb_per_100_vs_fold"], row["player"]
        assert row["bb_per_100_vs_fold_ci"] == mine["bb_per_100_vs_fold_ci"], row["player"]


@pytest.fixture()
def edge_db(db):
    from pnt.ingest.importer import import_csv

    for path in EDGE_LOGS:
        import_csv(db, path)
    return db


def _posts(hand: HandRow, pid: str) -> list[HandAction]:
    return [a for a in hand.actions if a.is_forced and a.pn_id == pid]


def test_fixture_blinds_post_a_half_and_one_big_blind(db):
    """Heads-up, every regular hand: the button's small blind is 0.5, the big blind 1."""
    seen = 0
    for h in load_hands(db, HU_GAME):
        for f in derive(h):
            kinds = [a.post_kind for a in _posts(h, f.pn_id)]
            if kinds == ["sb"] and h.bb == 2 * _posts(h, f.pn_id)[0].amount:
                assert f.posted_bb == Fraction(1, 2)
                seen += 1
            elif kinds == ["bb"]:
                assert f.posted_bb == 1
                seen += 1
    assert seen > 100


def test_every_fixture_post_counts_straddles_and_dead_posts_included(edge_db):
    for game, kind in ((STRADDLE_GAME, "straddle"), (MISSED_BLINDS_GAME, "missing_sb")):
        hits = 0
        for h in load_hands(edge_db, game):
            for f in derive(h):
                posts = _posts(h, f.pn_id)
                assert f.posted == sum(a.amount for a in posts)
                if any(a.post_kind == kind for a in posts):
                    hits += 1
                    assert f.posted >= sum(a.amount for a in posts if a.post_kind == kind) > 0
        assert hits, f"{game} has a {kind} post"
    straddler = next(
        f for h in load_hands(edge_db, STRADDLE_GAME) for f in derive(h)
        if any(a.post_kind == "straddle" for a in _posts(h, f.pn_id))
    )
    assert straddler.posted_bb >= 2
