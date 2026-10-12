"""The action-line explorer: every way a player played a hand, and what they had.

A spot's hands are grouped by the line they took after the flop, and each group
reports how often the hands in it that were shown were nutted, strong, marginal,
drawing or air. "When they go check-call, check-call, check-call, what do they
have?" is one row of this; so is "they check, we bet, they check-raise".

Two ways to group, the two halves of a street (SPEC.md, "Action lines" and "The
whole street"):

- ``own``     their own actions, `Facts.line`: ``xc/xc/xc``, ``b/x/b``.
- ``action``  the whole street from their seat, `Facts.seq`: ``xBc/xBr/-`` is
              checked to, bet, called; then bet into a check-raise.

Every group carries the filter that finds exactly its hands, so a row on the chart
drills into its hand list, and each strength tier carries one as well. See SPEC.md,
"Strength by line".

The strength split reads shown cards, so it carries the bias every range view
does: a line that ended with everyone folding to them was never shown, and that
is where the bluffs are. Read `coverage` first.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from fractions import Fraction

from .cards import MADE_LEAVES, leaf_of, made_at
from .derive import POSTFLOP_STREETS, Facts, derive
from .queries import load_hands
from .tags import strength_at

#: How hands are grouped: by their own line, or by the whole street's sequence.
MODES = ("own", "action")

#: The strength tiers, strongest first. Every shown hand with a street to judge it
#: on lands in exactly one.
TIERS = ("nutted", "strong", "marginal", "draw", "air")

#: The weakest made hand that counts as nutted: this leaf of `cards.MADE_LEAVES`
#: and everything above it -- a set, a straight, a flush, a full house, quads, a
#: straight flush. Trips made with one hole card on a paired board sit just below
#: it and are only strong. A judgement value; see SPEC.md, judgement call 35.
NUTTED_FROM = "set"

#: Groups with fewer hands than this are left out of the rows, and counted in
#: `hidden`. Every two-street permutation of a three-letter alphabet is a group
#: somewhere, and a one-hand line says nothing about a habit.
DEFAULT_MIN = 3

#: A street the player had no line on (`own`), or was not in the hand for
#: (`action`): what `-` matches in a filter.
NOT_PLAYED = "-"

#: A street the player was in, on which nobody acted: the run-out after an all-in.
#: Only `action` mode has one, since a line is never empty.
NO_ACTION = "."

_RANK = {leaf: i for i, leaf in enumerate(MADE_LEAVES)}

#: `tags.strength_at`'s words, folded into the tiers. `nutted` is cut out of
#: `strong` first, in `tier_at`.
_TIER_OF_STRENGTH = {"strong": "strong", "medium": "marginal", "weak": "marginal", "draw": "draw", "air": "air"}


def tier_at(hole: str | None, board: tuple[str, ...], street: str) -> str | None:
    """The strength tier of a holding on `street`'s board, or None when unknown.

    ``nutted``    `NUTTED_FROM` or better, by `cards.made_at`
    ``strong``    the rest of `tags.strength_at`'s strong: trips, two pair, an
                  overpair, top pair
    ``marginal``  its medium and weak: an underpair, middle, bottom or board pair
    ``draw``      a flush or straight draw with no pair, on the flop or turn
    ``air``       high card, and on the river a draw that missed

    Unknown when the cards are, or the board never reached the street.
    """
    if not hole:
        return None
    mh = made_at(hole, board, street)
    if mh is None:
        return None
    if _RANK[leaf_of(mh)] <= _RANK[NUTTED_FROM]:
        return "nutted"
    return _TIER_OF_STRENGTH[strength_at(hole, board, street)]


def tier_terms(tier: str, street: str) -> str | None:
    """The `made_<street>` terms that find exactly the hands of `tier` on `street`,
    or None for a tier that cannot occur there (a draw on the river is air).

    Each tier is one unbroken run of `cards.MADE_LEAVES`, so two comparisons
    bound it. `test_every_tier_filter_reproduces_its_count` holds these to
    `tier_at`, which is the definition.
    """
    term = f"made_{street}"
    if tier == "nutted":
        return f"{term}>={NUTTED_FROM}"
    if tier == "strong":
        return f"{term}<{NUTTED_FROM},{term}>=top_pair"
    if tier == "marginal":
        return f"{term}<top_pair,{term}>=board_pair"
    if street == "river":
        # On the river a missed draw is air, so air is everything below a pair.
        return None if tier == "draw" else f"{term}<pair"
    return f"{term}=draw" if tier == "draw" else f"{term}=high_card"


def _segment(f: Facts, street: str, mode: str) -> str:
    """One street of a hand's group key."""
    if mode == "own":
        return f.line.get(street, NOT_PLAYED)
    seq = f.seq.get(street)
    if seq is None:
        return NOT_PLAYED
    return seq or NO_ACTION


def group_filter(segments: tuple[str, ...], streets: tuple[str, ...], mode: str) -> str:
    """The filter that finds exactly the hands of one group.

    ``own``: ``line=xc/x/-`` across three streets, ``line_flop=xc`` for one. Every
    segment is a whole line or `-`, so it matches itself and nothing else.

    ``action``: the same with ``action``. A street on which nobody acted (`.`) has
    no letters to write, and an empty segment means "anything", so it becomes two
    terms instead: ``action_turn=*`` (in the hand on the turn) and
    ``action_turn!=?*`` (and not one action on it).
    """
    kind = "line" if mode == "own" else "action"
    extra: list[str] = []
    parts: list[str] = []
    for street, seg in zip(streets, segments):
        if seg == NO_ACTION:
            extra += [f"action_{street}=*", f"action_{street}!=?*"]
            parts.append("")
        else:
            parts.append(seg)
    terms: list[str] = []
    if any(parts):
        if len(streets) == 1:
            terms.append(f"{kind}_{streets[0]}={parts[0]}")
        else:
            terms.append(f"{kind}={'/'.join(parts)}")
    return ",".join(terms + extra)


def _rated_on(segments: tuple[str, ...], streets: tuple[str, ...], mode: str) -> str | None:
    """The last street of the group the player acted on: the board their strength is
    read on. Fixed per group, since the key says which streets they acted on -- a
    line that is not `-`, or a sequence with a capital in it."""
    for street, seg in reversed(list(zip(streets, segments))):
        if seg in (NOT_PLAYED, NO_ACTION):
            continue
        if mode == "own" or any(ch.isupper() for ch in seg):
            return street
    return None


def _pct(num: int, den: int) -> float | None:
    return round(100.0 * num / den, 1) if den else None


def _money(rows: list[Facts]) -> dict:
    """Net won in big blinds, and per 100 hands, by the bb/100 rule: complete hands
    with a known big blind only. See SPEC.md, "Money"."""
    total, n = Fraction(0), 0
    for f in rows:
        if f.bb_size and f.complete:
            total += Fraction(f.net) / Fraction(f.bb_size)
            n += 1
    return {
        "net_bb": round(float(total), 2) if n else None,
        "bb_per_100": round(float(100 * total / n), 2) if n else None,
    }


def line_breakdown(
    facts: list[Facts],
    mode: str = "own",
    street: str | None = None,
    min_n: int = DEFAULT_MIN,
) -> dict:
    """Group an already-filtered list of Facts by line, with the strength behind each.

    `mode` is ``own`` (their `line`) or ``action`` (the street's `seq`). `street`
    limits the key to one street; None reads flop, turn and river together, with
    `-` on the streets they did not play. Every hand lands in exactly one group,
    the ones with no postflop action included (``-/-/-``), so the groups add up
    to `hands`.

    Per group:

    - ``n``, ``net_bb`` and ``bb_per_100``.
    - ``shown``: hands with cards known. ``known``: those that also have a street
      to judge them on -- the last one of the group they acted on, ``rated_on``.
      A group with no action after the flop has none, and its known is 0.
    - ``tiers``: ``{tier: {n, pct, filter}}``, ``pct`` of ``known``, and
      ``draw_heavy``, the draw share, said outright.
    - ``filter``: the terms that find exactly these hands, inside the same spot.

    Groups under `min_n` hands are left out of ``groups`` and counted in ``hidden``.
    """
    if mode not in MODES:
        raise ValueError(f"unknown lines mode {mode!r}. Known: {', '.join(MODES)}")
    if street is not None and street not in POSTFLOP_STREETS:
        raise ValueError(f"unknown street {street!r}. Known: {', '.join(POSTFLOP_STREETS)}")
    streets = (street,) if street else POSTFLOP_STREETS

    grouped: dict[tuple[str, ...], list[Facts]] = defaultdict(list)
    for f in facts:
        grouped[tuple(_segment(f, s, mode) for s in streets)].append(f)

    groups, hidden_groups, hidden_hands = [], 0, 0
    shown_all = known_all = 0
    for segments, rows in grouped.items():
        on = _rated_on(segments, streets, mode)
        counts = dict.fromkeys(TIERS, 0)
        shown = 0
        for f in rows:
            if not f.hole_cards:
                continue
            shown += 1
            tier = tier_at(f.hole_cards, f.board, on) if on else None
            if tier is not None:
                counts[tier] += 1
        known = sum(counts.values())
        shown_all += shown
        known_all += known
        if len(rows) < min_n:
            hidden_groups += 1
            hidden_hands += len(rows)
            continue
        base = group_filter(segments, streets, mode)
        tiers = {}
        for tier in TIERS:
            terms = tier_terms(tier, on) if on else None
            tiers[tier] = {
                "n": counts[tier],
                "pct": _pct(counts[tier], known),
                "filter": f"{base},{terms}" if terms else None,
            }
        groups.append({
            "line": "/".join(segments),
            "streets": dict(zip(streets, segments)),
            "filter": base,
            "n": len(rows),
            **_money(rows),
            "shown": shown,
            "known": known,
            "coverage": _pct(known, len(rows)),
            "rated_on": on,
            "tiers": tiers,
            "draw_heavy": tiers["draw"]["pct"],
            "hand_ids": [f.hand_id for f in rows],
        })
    groups.sort(key=lambda g: (-g["n"], g["line"]))

    return {
        "mode": mode,
        "street": street,
        "streets": list(streets),
        "min": min_n,
        "hands": len(facts),
        "shown": shown_all,
        "known": known_all,
        "coverage": _pct(known_all, len(facts)),
        **_money(facts),
        "groups": groups,
        "hidden": {"groups": hidden_groups, "hands": hidden_hands},
    }


def pool_facts(conn: sqlite3.Connection, game_id: str | None = None) -> list[Facts]:
    """Every player's Facts on every hand: the pool `GET /lines` groups.

    Derived afresh, since the per-player cache holds a handful of players and this
    is all of them. A row is one player's view of one hand, so a heads-up hand is
    two rows, one from each seat -- the pool's ``xB`` and ``Xb`` are the same
    streets read from opposite chairs.
    """
    return [f for hand in load_hands(conn, game_id) for f in derive(hand)]
