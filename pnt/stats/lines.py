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

from .derive import POSTFLOP_STREETS, Facts, derive
from .queries import load_hands
from .strength import TIERS, tier_at
from .tilt import annotate

#: How hands are grouped: by their own line, or by the whole street's sequence.
MODES = ("own", "action")

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


def tier_terms(tier: str, street: str) -> str | None:
    """The filter term that finds exactly the hands of `tier` on `street`, or None
    for a tier that cannot occur there (a draw on the river is air).

    `tier_<street>` reads `strength.tier_at` itself, so the term is the definition
    and `test_every_tier_filter_reproduces_its_count` holds every row to it.
    """
    if tier == "draw" and street == "river":
        return None
    return f"tier_{street}={tier}"


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


def _summary(segments: tuple[str, ...], streets: tuple[str, ...], rows: list[Facts], mode: str) -> dict:
    """One group's row: its filter, money, coverage and strength split. `streets`
    are the streets `segments` cover -- all three for a whole line, fewer for a
    branch of the tree, whose strength is read where it last acted."""
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
    base = group_filter(segments, streets, mode)
    tiers = {}
    for tier in TIERS:
        terms = tier_terms(tier, on) if on else None
        tiers[tier] = {
            "n": counts[tier],
            "pct": _pct(counts[tier], known),
            "filter": f"{base},{terms}" if terms and base else (terms if terms else None),
        }
    return {
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
    }


#: What a line looks like when it has stopped: nothing more to open below it.
_ENDED = (NOT_PLAYED, NO_ACTION)


def line_tree(facts: list[Facts], mode: str = "own", min_n: int = DEFAULT_MIN) -> list[dict]:
    """The same lines as a tree: what they did on the flop, then the turn under each
    flop line, then the river under each of those -- so every way of checking the
    flop sits together, under one row that adds them up.

    A node is `_summary` of every hand whose line starts that way, with its own
    filter (``line_flop=x``, ``line=x/b``) and its strength read on the last street
    of the node they acted on: "checked the flop" is what they had on the flop.
    ``children`` are the ways on with `min_n` hands or more, biggest first, and
    ``hidden`` counts the rest. A line that stopped there -- they folded, or were
    all in -- has no children. Children and hidden add up to the node's ``n``.
    """
    streets = POSTFLOP_STREETS

    def node(prefix: tuple[str, ...], rows: list[Facts]) -> dict:
        out = _summary(prefix, streets[:len(prefix)], rows, mode)
        out["children"], out["hidden"] = [], {"groups": 0, "hands": 0}
        if len(prefix) == len(streets):
            return out
        nxt = streets[len(prefix)]
        ways: dict[str, list[Facts]] = defaultdict(list)
        for f in rows:
            ways[_segment(f, nxt, mode)].append(f)
        if set(ways) <= set(_ENDED):
            return out  # the line ended here
        for seg, sub in ways.items():
            if len(sub) < min_n:
                out["hidden"]["groups"] += 1
                out["hidden"]["hands"] += len(sub)
            else:
                out["children"].append(node((*prefix, seg), sub))
        out["children"].sort(key=lambda g: (-g["n"], g["line"]))
        return out

    top: dict[str, list[Facts]] = defaultdict(list)
    for f in facts:
        top[_segment(f, streets[0], mode)].append(f)
    roots = [node((seg,), rows) for seg, rows in top.items() if len(rows) >= min_n]
    # No action after the flop is the biggest group and says nothing about a line:
    # it goes last.
    roots.sort(key=lambda g: (g["line"] in _ENDED, -g["n"], g["line"]))
    return roots


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
        g = _summary(segments, streets, rows, mode)
        shown_all += g["shown"]
        known_all += g["known"]
        if len(rows) < min_n:
            hidden_groups += 1
            hidden_hands += len(rows)
            continue
        groups.append(g)
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
        "tree": line_tree(facts, mode, min_n) if street is None else None,
    }


def pool_facts(conn: sqlite3.Connection, game_id: str | None = None) -> list[Facts]:
    """Every player's Facts on every hand: the pool `GET /lines` groups.

    Derived afresh, since the per-player cache holds a handful of players and this
    is all of them. A row is one player's view of one hand, so a heads-up hand is
    two rows, one from each seat -- the pool's ``xB`` and ``Xb`` are the same
    streets read from opposite chairs.
    """
    return annotate([f for hand in load_hands(conn, game_id) for f in derive(hand)])
