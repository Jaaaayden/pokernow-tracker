"""Hand strength (`pnt.stats.strength`): the measure, the grades, the cache, the filters.

SPEC.md, "Hand strength". The worked values are the ones the grading was argued
from: queens on K-K-7 beat as many holdings as top pair on K-7-2, and aces on a
four-flush, four-straight river beat barely half.
"""

from __future__ import annotations

import itertools

import pytest
from conftest import HU_GAME

from pnt.stats import strength
from pnt.stats.cards import parse_cards
from pnt.stats.derive import derive
from pnt.stats.equity import FULL_DECK, rank7
from pnt.stats.filters import parse_filter
from pnt.stats.queries import load_hands
from pnt.stats.strength import HS_CUTS, cache_key, hand_strength, strength_at, tier_at

KK7, K72, NINE87 = ("Kc", "Ks", "7d"), ("Kc", "7s", "2d"), ("9c", "8s", "7d")
SCARY = ("9h", "8h", "7h", "6c", "2d")


@pytest.mark.parametrize(
    "hole, board, expected",
    [
        ("QhQd", KK7, 0.91), ("Ah7c", KK7, 0.88), ("7h2c", KK7, 0.80), ("3h3c", KK7, 0.75), ("AhQc", KK7, 0.74),
        ("Kh5d", K72, 0.91), ("7h6h", K72, 0.79), ("2h3h", K72, 0.64),
        ("JhJd", NINE87, 0.91), ("9h2c", NINE87, 0.83),
        ("AhAd", SCARY, 0.55), ("Th4c", SCARY, 0.89), ("Ah5h", SCARY, 1.00),
    ],
)
def test_worked_values(hole, board, expected):
    assert hand_strength(hole, board) == pytest.approx(expected, abs=0.01)


def _brute(hole: str, board: tuple[str, ...]) -> float:
    h, b = parse_cards(hole), parse_cards(list(board))
    me = rank7(h + b)
    num2 = n = 0
    for o in itertools.combinations([c for c in FULL_DECK if c not in {*h, *b}], 2):
        r = rank7([*o, *b])
        num2 += 2 if me > r else 1 if me == r else 0
        n += 1
    return num2 / (2 * n)


@pytest.mark.parametrize("hole, board", [
    ("AsKd", ("Ah", "Kc", "2d")), ("7h7d", ("7c", "2s", "2h", "Kd")), ("Tc9c", ("8c", "7c", "2h", "Jc", "Qd")),
    ("2c3d", ("Ah", "Ad", "Ac", "As", "Kd")),
])
def test_card_removal_matches_brute_force(hole, board):
    assert hand_strength(hole, board) == pytest.approx(_brute(hole, board), abs=1e-12)


def test_the_key_ignores_order_and_bad_input_is_unknown():
    assert cache_key("KdAh", ["7s", "2c", "9d"]) == cache_key("AhKd", ["9d", "2c", "7s"]) == "AhKd|2c7s9d"
    assert hand_strength("AhKd", ("Ac", "2d")) is None, "no flop"
    assert hand_strength("AhKd", ("Ah", "2d", "3c")) is None, "a card used twice"
    assert hand_strength(None, KK7) is None


def test_cuts_are_ordered_on_every_street():
    for street, cut in HS_CUTS.items():
        assert 0 < cut["weak"] <= cut["strong"] <= cut["nutted"] <= 1, street


def test_grades_follow_the_board():
    assert strength_at("QhQd", KK7, "flop") == "strong", "an 'underpair' that beats 91%"
    assert strength_at("7h2c", KK7, "flop") == "medium"
    assert strength_at("AhQc", KK7, "flop") == "air", "the kings are the board's"
    assert strength_at("AhAd", SCARY, "river") == "medium", "an overpair that beats 55%"
    assert strength_at("Ah5h", SCARY, "river") == "strong"
    assert tier_at("Ah5h", SCARY, "river") == "nutted"
    assert tier_at("AhAd", SCARY, "river") == "marginal"
    assert strength_at("AhKh", ("Qh", "9h", "2d"), "flop") == "draw"
    assert strength_at("AhAd", (), "preflop") == "strong", "preflop is hand_pct, untouched"


def test_warm_stores_every_value_and_prime_restores_them(db, monkeypatch):
    jobs = strength.needed(db)
    assert jobs, "the fixture logs show hands with pairs of their own"
    stored = db.execute("SELECT COUNT(*) FROM strength_cache").fetchone()[0]
    assert stored >= len({cache_key(c, b) for c, b in jobs}), "rebuild_game warmed every game on import"
    row = db.execute("SELECT key, num2, n FROM strength_cache LIMIT 1").fetchone()

    # A fresh process: nothing memoized, the table read back, no board ranked.
    monkeypatch.setattr(strength, "_HS", {})
    monkeypatch.setattr(strength, "_PRIMED", set())
    strength._board.cache_clear()
    strength._strength_on.cache_clear()

    def boom(*_):
        raise AssertionError("ranked a board the cache already had")

    monkeypatch.setattr(strength, "_Board", boom)
    strength.prime(db)
    assert strength._HS[row["key"]] == (row["num2"], row["n"])
    for cards, board in jobs[:200]:
        strength.counts(cards, board)
    strength.warm(db)  # every value known: nothing ranked, the table only re-affirmed
    assert db.execute("SELECT COUNT(*) FROM strength_cache").fetchone()[0] == stored


def test_between_runs_before_each_holding_measured(db, monkeypatch):
    listing = []
    strength.warm(db, between=lambda: listing.append(1))
    assert listing, "every value already known: only the listing waits"

    # Nothing memoized and nothing read back: every holding is measured afresh.
    monkeypatch.setattr(strength, "_HS", {})
    monkeypatch.setattr(strength, "prime", lambda _conn: None)
    jobs = strength.needed(db)
    calls = []
    strength.warm(db, between=lambda: calls.append(1))
    assert len(listing) < len(calls) <= len(listing) + len(jobs)


def test_tier_and_hs_terms(db):
    names = {}
    facts = [f for h in load_hands(db, HU_GAME) for f in derive(h)]
    known = [f for f in facts if strength.tier_of(f, "river")]
    assert known
    for tier in strength.TIERS:
        p = parse_filter(f"tier_river={tier}", names)
        assert sum(map(p, facts)) == sum(strength.tier_of(f, "river") == tier for f in facts)
    strong_up = parse_filter("tier_river>=strong", names)
    assert sum(map(strong_up, facts)) == sum(strength.tier_of(f, "river") in ("nutted", "strong") for f in facts)
    either = parse_filter("tier_flop=draw|air", names)
    assert sum(map(either, facts)) == sum(strength.tier_of(f, "flop") in ("draw", "air") for f in facts)
    hs = parse_filter("hs_river>=0.8", names)
    assert sum(map(hs, facts)) == sum((strength.hs_of(f, "river") or 0) >= 0.8 for f in facts)
    # A fold before the street says nothing about it, `!=` included.
    folded = [f for f in facts if f.hole_cards and f.fold_street == "flop" and len(f.board) == 5]
    for f in folded:
        assert not parse_filter("tier_river!=air", names)(f)
    with pytest.raises(ValueError, match="unknown tier"):
        parse_filter("tier_river=monster", names)
    with pytest.raises(ValueError, match="several tiers"):
        parse_filter("tier_river>=strong|air", names)
