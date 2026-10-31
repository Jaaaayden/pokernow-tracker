"""Work out which player is the log's owner ("hero").

``Your hand is 5♦, 8♣`` carries no name, so on a downloaded log there is nothing
directly linking hero's hole cards to a player ID. But at showdown the same cards
appear again attached to a name -- hand #188 of the sample log shows
``tablebot`` holding exactly ``5♦, 8♣``.

Inference is by elimination plus voting, which makes it self-validating:

* A player who shows two cards that are **not** hero's cards for that hand cannot
  be hero, ever. One counter-example eliminates them permanently.
* A player who shows exactly hero's cards gets a vote.

Requiring several votes and zero contradictions means a wrong answer needs a
sustained coincidence rather than a single unlucky hand. A short session can fall
short of `MIN_VOTES`, so two votes are enough when nobody else could be hero:
hero is dealt in on every hand that names their cards, so a player missing from
any of those rosters is ruled out, and the guess must be the only player left
who never showed other cards. Live capture knows hero directly from the page, so
this only matters for backfill.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .parser import ParsedHand

#: Votes required before we name a hero. Two players sharing one showdown holding
#: is impossible; sharing three across a session is not worth worrying about.
MIN_VOTES = 3
#: Votes that are enough when every other player is ruled out (module docstring).
MIN_VOTES_UNCONTESTED = 2


@dataclass(frozen=True, slots=True)
class HeroGuess:
    pn_id: str | None
    votes: int
    contradictions: dict[str, int]
    confident: bool


def infer_hero(hands: list[ParsedHand]) -> HeroGuess:
    votes: Counter[str] = Counter()
    eliminated: Counter[str] = Counter()
    #: Who was dealt in on every hand that named hero's cards: hero always is.
    dealt_every: set[str] | None = None

    for hand in hands:
        # Two cards in Hold'em, four or five in PLO: a show is compared with hero's
        # cards only when it is a whole hand of the same game.
        if len(hand.hero_cards) < 2:
            continue
        roster = set(hand.players)
        dealt_every = roster if dealt_every is None else dealt_every & roster
        hero_set = frozenset(hand.hero_cards)
        for p in hand.players.values():
            if len(p.hole_cards) != len(hand.hero_cards):
                continue
            if frozenset(p.hole_cards) == hero_set:
                votes[p.pn_id] += 1
            else:
                eliminated[p.pn_id] += 1

    viable = {pid: n for pid, n in votes.items() if pid not in eliminated}
    if not viable:
        return HeroGuess(None, 0, dict(eliminated), False)

    pn_id, n = max(viable.items(), key=lambda kv: kv[1])
    # Everyone who could still be hero: dealt in every time, never caught out.
    rivals = (dealt_every or set()) - set(eliminated) - {pn_id}
    uncontested = n >= MIN_VOTES_UNCONTESTED and pn_id in (dealt_every or ()) and not rivals
    return HeroGuess(pn_id, n, dict(eliminated), confident=n >= MIN_VOTES or uncontested)


def apply_hero_cards(hands: list[ParsedHand], hero_pn_id: str) -> int:
    """Fill in hero's hole cards on every hand they were dealt in.

    Hero's cards are known on *every* hand, not just showdowns -- which is why a
    correct hero id roughly doubles the hole-card coverage of a session.
    """
    filled = 0
    for hand in hands:
        p = hand.players.get(hero_pn_id)
        if p is not None and len(hand.hero_cards) >= 2 and not p.hole_cards:
            p.hole_cards = hand.hero_cards
            filled += 1
    return filled
