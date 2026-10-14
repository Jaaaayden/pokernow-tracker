"""Showdown equity: who wins how much of each pot, over the boards still to come.

The all-in EV figures are built on three pure functions:

- ``rank7``              the strength of the best five of five to seven cards,
                         as a tuple that compares the way hands compare.
- ``side_pots``          the main and side pots of a hand, from what each player
                         put in and who was still in at the end.
- ``expected_collected`` how many chips each player collects on average over the
                         boards that could still be dealt, given those pots.

Exact when two or fewer cards are to come -- 990 turn-and-river pairs after a
flop all-in, 44 rivers after a turn one, a single board on the river. Preflop
there are 1.7 million boards heads-up, which pure Python cannot enumerate in a
time anyone will wait for, so preflop is **sampled**: 50,000 deals from a
generator seeded by the cards and pots themselves, which makes the answer
reproducible and its error under a quarter of a percent. Every result says which
of the two it was.

Nothing here touches the database; ``allin.py`` does the reading and caching.
"""

from __future__ import annotations

import hashlib
import itertools
import random
from collections.abc import Collection, Iterable, Mapping, Sequence

from .cards import Card, parse_cards

#: Deals sampled for a preflop all-in. Tests lower this to stay fast.
SAMPLES = 50_000

#: Every card there is, so the ones still to come are these minus the known ones.
FULL_DECK: tuple[Card, ...] = tuple(Card(v, s) for v in range(2, 15) for s in "cdhs")

#: Boards to come with exactly this many enumerated are done exactly; more are
#: sampled. 990 is the flop case (45 choose 2); the next size up, preflop, is
#: 1,712,304 heads-up and never worth enumerating in pure Python.
EXACT_LIMIT = 2000


def _straight_high(bits: int) -> int:
    """Highest card of a straight in a rank bitmask (bit v set for value v), or 0."""
    if bits & (1 << 14):
        bits |= 1 << 1  # the ace plays low
    for high in range(14, 4, -1):
        if (bits >> (high - 4)) & 0b11111 == 0b11111:
            return high
    return 0


def rank7(cards: Sequence[Card]) -> tuple[int, ...]:
    """The best five-card hand in `cards`, as a tuple that orders hands correctly.

    First element is the category -- 8 straight flush down to 0 high card, the
    same order as ``cards.MADE_CLASSES`` -- followed by the tie-breakers in the
    order they are compared: quads then kicker, trips then pair, the flush cards
    high to low, and so on. Two tuples compare equal exactly when the hands chop.
    """
    counts: dict[int, int] = {}
    by_suit: dict[str, int] = {}
    bits = 0
    for v, s in cards:
        counts[v] = counts.get(v, 0) + 1
        by_suit[s] = by_suit.get(s, 0) | (1 << v)
        bits |= 1 << v

    flush_bits = next((b for b in by_suit.values() if b.bit_count() >= 5), 0)
    if flush_bits:
        high = _straight_high(flush_bits)
        if high:
            return (8, high)

    # Distinct values grouped by how often they appear, biggest group first and
    # higher value first within a group.
    groups = sorted(counts.items(), key=lambda kv: (-kv[1], -kv[0]))
    (v0, n0), *rest = groups
    if n0 == 4:
        kicker = max(v for v in counts if v != v0)
        return (7, v0, kicker)
    if n0 == 3 and rest and rest[0][1] >= 2:
        return (6, v0, rest[0][0])
    if flush_bits:
        top5 = sorted((v for v in range(2, 15) if flush_bits & (1 << v)), reverse=True)[:5]
        return (5, *top5)
    high = _straight_high(bits)
    if high:
        return (4, high)
    if n0 == 3:
        kickers = sorted((v for v in counts if v != v0), reverse=True)[:2]
        return (3, v0, *kickers)
    if n0 == 2 and rest and rest[0][1] == 2:
        v1 = rest[0][0]
        kicker = max(v for v in counts if v not in (v0, v1))
        return (2, v0, v1, kicker)
    if n0 == 2:
        kickers = sorted((v for v in counts if v != v0), reverse=True)[:3]
        return (1, v0, *kickers)
    return (0, *sorted(counts, reverse=True)[:5])


def best_of(hands: Mapping[str, str], board: Sequence[str]) -> tuple[str, ...]:
    """Who holds the best hand on a complete board: more than one on a chop.

    `hands` maps a player to their hole cards; `board` has five cards. Keys come
    back in the order given.
    """
    if not hands:
        return ()
    board_cards = parse_cards(list(board))
    ranks = {pid: rank7(parse_cards(cards) + board_cards) for pid, cards in hands.items()}
    top = max(ranks.values())
    return tuple(pid for pid, r in ranks.items() if r == top)


def side_pots(
    contributed: Mapping[str, int], live: Collection[str]
) -> list[tuple[int, tuple[str, ...]]]:
    """Main and side pots: ``[(chips, eligible players), ...]``, smallest level first.

    Every chip contributed is in some pot -- a folded player's chips included, they
    are just not eligible for any of them. The layers are the distinct amounts the
    live players put in: the players who put in at least a layer's amount can win
    it. The sizes sum to the total contributed, which is how `Facts.pot` is defined,
    so the two agree by construction.

    `contributed` should already exclude uncalled returns (``hand_players.contributed``
    does). When it does not -- PokerNow occasionally lets a caller put in a few chips
    more than the all-in and returns nothing (hand #73 of `pgl8vNV4WURe`: 915 called
    against 910, and the winner collected all 1,945) -- the top layer has a single
    eligible player. Such a layer is folded into the pot below it, because that is
    where PokerNow paid it: to the winner, not back to its owner.
    """
    alive = [p for p in live if p in contributed]
    levels = sorted({contributed[p] for p in alive})
    pots: list[tuple[int, tuple[str, ...]]] = []
    floor = 0
    for level in levels:
        chips = sum(max(0, min(c, level) - floor) for c in contributed.values())
        eligible = tuple(p for p in alive if contributed[p] >= level)
        if chips > 0:
            if len(eligible) == 1 and pots:
                below, below_eligible = pots[-1]
                pots[-1] = (below + chips, below_eligible)
            else:
                pots.append((chips, eligible))
        floor = level
    return pots


def cache_key(
    hands: Sequence[str], board: Sequence[str], pots: Sequence[tuple[int, Sequence[int]]]
) -> str:
    """One string naming a computation: hands in player order, the board, the pots.

    Player order is part of the key because the result is a list in that order.
    """
    pot_text = ";".join(f"{chips}:{','.join(map(str, elig))}" for chips, elig in pots)
    return f"{'|'.join(hands)}/{''.join(board)}/{pot_text}"


def _seed(key: str) -> int:
    """A seed fixed by the key, so the same all-in samples the same deals every time
    (``hash()`` of a string changes between Python processes; this does not)."""
    return int(hashlib.sha256(key.encode()).hexdigest()[:16], 16)


def expected_collected(
    hands: Sequence[str],
    board: Sequence[str],
    pots: Sequence[tuple[int, Sequence[int]]],
    *,
    samples: int | None = None,
    seed: int | None = None,
) -> tuple[list[float], str, int]:
    """Chips each player collects on average, over the boards still to come.

    `hands` are the live players' hole cards, `board` the cards dealt when the
    betting stopped, and `pots` ``(chips, eligible player indices)`` as
    ``side_pots`` produces. Returns ``(expected per player, method, n)``: `method`
    is ``exact`` when every remaining board was enumerated and ``sampled`` when
    `n` random deals stood in for them. Each pot goes to the best eligible hand on
    each board, split equally on a chop.
    """
    holes = [parse_cards(h) for h in hands]
    known = parse_cards(list(board))
    dead = set(known)
    for h in holes:
        dead.update(h)
    deck = [c for c in FULL_DECK if c not in dead]
    to_come = 5 - len(known)
    if to_come < 0:
        raise ValueError(f"a board has at most five cards, not {len(known)}")

    pots = [(chips, tuple(elig)) for chips, elig in pots]
    won = [0.0] * len(holes)

    def settle(extra: Iterable[Card]) -> None:
        full = known + list(extra)
        ranks = [rank7(h + full) for h in holes]
        for chips, elig in pots:
            best = max(ranks[i] for i in elig)
            winners = [i for i in elig if ranks[i] == best]
            share = chips / len(winners)
            for i in winners:
                won[i] += share

    n_boards = _choose(len(deck), to_come)
    if n_boards <= EXACT_LIMIT:
        for extra in itertools.combinations(deck, to_come):
            settle(extra)
        n = n_boards
        method = "exact"
    else:
        n = samples if samples is not None else SAMPLES
        rng = random.Random(_seed(cache_key(hands, board, pots)) if seed is None else seed)
        for _ in range(n):
            settle(rng.sample(deck, to_come))
        method = "sampled"

    return [w / n for w in won], method, n


def _choose(n: int, k: int) -> int:
    out = 1
    for i in range(k):
        out = out * (n - i) // (i + 1)
    return out
