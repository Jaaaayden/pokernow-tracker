"""Strength by street: what a player did on a street when they had a big hand there.

`made_flop>=set,line_flop=x*` already finds the sets that checked the flop. This
is the table of every such question at once: one row per made hand at or above a
floor (set by default), one column per thing the player did on that street, and
the share of it that was a slowplay -- a check or a call where a bet or a raise
was there to be made.

The columns are a **partition of the line** (`Facts.line[street]`): every hand in
a row lands in exactly one, and each column is a line pattern, so a cell's filter
is ``made_<street>=<leaf>,line_<street>=<pattern>`` and finds exactly that cell's
hands. SPEC.md, "Strength by street", pins them.

Cards are known only when shown, so the rows are built from shown hands, and the
bias runs one way: a set that bet and got folds to is never shown, while a set
that checked or called usually reaches showdown. An opponent's slowplay share
reads high for that reason; the hero's cards are known on every hand, so theirs
does not. ``coverage`` is reported beside every table, as in the range views.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .cards import MADE_LEAVES
from .derive import POSTFLOP_STREETS, Facts

# The filter's own reading of a holding on a street -- cards known, the board got
# there, not folded on an earlier street -- so a row cannot disagree with the
# `made_<street>=` term that reproduces it.
from .filters import _made_leaf

#: The made hand a report starts from when none is asked for.
DEFAULT_FLOOR = "set"

#: The columns, in display order: (key, label, line pattern, check-back flag,
#: slowplay). The flag is None where it does not apply, True for "and their check
#: closed the street", False for "and it did not". Every line a player can have
#: lands in exactly one: a line starts with the first thing they did, and only a
#: check is ever followed by a second decision of a different kind.
#:
#: - ``bet``           ``b*``   bet first: led, c-bet or probed. Whatever came
#:                              after (calling a raise, folding to one) is the bet's.
#: - ``raise``         ``r*``   raised the first bet they faced, without checking.
#: - ``check_raise``   ``xr*``  checked, then raised the bet behind them.
#: - ``check_call``    ``xc*``  checked, then called. A slowplay.
#: - ``call``          ``c*``   called the first bet they faced. A slowplay: a
#:                              raise was there to be made.
#: - ``checked``       ``x``    and not a check-back: checked and nobody behind
#:                              bet, first to act or from the middle. A slowplay.
#: - ``checked_back``  ``x``    and a check-back: last to act, their check closed
#:                              a street that checked through. A slowplay.
#: - ``check_fold``    ``xf``   checked, then folded to the bet.
#: - ``fold``          ``f``    folded to the first bet they faced.
#: - ``other``         anything else -- a check followed by a bet or a second
#:                              check, which a complete log never writes. Kept so
#:                              the columns still add up if one ever does.
CATEGORIES: tuple[tuple[str, str, str | None, bool | None, bool], ...] = (
    ("bet", "bet", "b*", None, False),
    ("raise", "raised", "r*", None, False),
    ("check_raise", "check-raised", "xr*", None, False),
    ("check_call", "check-called", "xc*", None, True),
    ("call", "called", "c*", None, True),
    ("checked", "checked, no bet", "x", False, True),
    ("checked_back", "checked back", "x", True, True),
    ("check_fold", "check-folded", "xf", None, False),
    ("fold", "folded", "f", None, False),
    ("other", "other", None, None, False),
)

#: The columns that are a slowplay: a strong hand that checked or called.
SLOWPLAY = tuple(key for key, *_, slow in CATEGORIES if slow)


def category(line: str, check_back: bool) -> str:
    """The column of one line on one street. `check_back` is whether their check
    closed the street, read only for a lone ``x``."""
    first, rest = line[0], line[1:]
    if first == "b":
        return "bet"
    if first == "r":
        return "raise"
    if first == "c":
        return "call"
    if line == "f":
        return "fold"
    if first == "x":
        if not rest:
            return "checked_back" if check_back else "checked"
        if rest[0] == "r":
            return "check_raise"
        if rest[0] == "c":
            return "check_call"
        if rest == "f":
            return "check_fold"
    return "other"


def _cell_terms(street: str, key: str) -> str:
    """The line half of a cell's filter: the terms that find exactly that column."""
    term = f"line_{street}"
    if key == "other":
        # Not one of the named patterns, and acted: every named column, negated.
        named = dict.fromkeys(pat for _, _, pat, _, _ in CATEGORIES if pat)
        return ",".join([f"{term}=*", *(f"{term}!={p}" for p in named)])
    _, _, pattern, back, _ = next(c for c in CATEGORIES if c[0] == key)
    out = f"{term}={pattern}"
    if back is not None:
        out += f",{'' if back else '!'}check_back_{street}"
    return out


def _pct(num: int, den: int) -> float | None:
    return round(100.0 * num / den, 1) if den else None


def _row(leaf: str, made_term: str, street: str, hands: list[Facts], all_in: list[Facts]) -> dict:
    """One row: the hands that made `leaf` (or the floor and up, for the total) and
    acted on the street, split into the columns."""
    groups: dict[str, list[Facts]] = {key: [] for key, *_ in CATEGORIES}
    for f in hands:
        groups[category(f.line[street], bool(f.check_back.get(street)))].append(f)
    n = len(hands)
    slow = sum(len(groups[k]) for k in SLOWPLAY)
    return {
        "leaf": leaf,
        "n": n,
        "slowplay": slow,
        "slowplay_pct": _pct(slow, n),
        "filter": f"{made_term},line_{street}=*",
        # Already all in from an earlier street: they made the hand, but had no
        # decision to make with it, so they are counted here and in no column.
        "all_in": len(all_in),
        "all_in_filter": f"{made_term},line_{street}=-",
        "all_in_ids": [f.hand_id for f in all_in],
        "cells": {
            key: {
                "n": len(rows),
                "pct": _pct(len(rows), n),
                "filter": f"{made_term},{_cell_terms(street, key)}",
                "hand_ids": [f.hand_id for f in rows],
            }
            for key, rows in groups.items()
        },
    }


def check_floor(floor: str) -> str:
    floor = floor.strip().lower()
    if floor not in MADE_LEAVES:
        raise ValueError(f"unknown floor {floor!r}. Known: {', '.join(MADE_LEAVES)}")
    return floor


def slowplay(facts: Iterable[Facts], street: str, floor: str = DEFAULT_FLOOR) -> dict:
    """What a player did on `street` with each made hand from the strongest down to
    `floor`, inclusive. `facts` is already filtered to the spot.

    Rows are the `MADE_LEAVES` from ``straight_flush`` to `floor`, every one present
    so the table keeps its shape, and a ``total`` over all of them. A row counts the
    hands where the cards are known, the hand was made on that street, and they
    acted on it; ``n`` is that count, every cell's ``pct`` is of it, and the cells
    add up to it. ``slowplay_pct`` is the checked and called share.

    ``hands`` is every hand they were still in on that street, ``known`` those with
    their cards known -- the coverage every made-hand figure here is read against.
    """
    if street not in POSTFLOP_STREETS:
        raise ValueError(f"unknown street {street!r}. Known: {', '.join(POSTFLOP_STREETS)}")
    floor = check_floor(floor)
    leaves = MADE_LEAVES[: MADE_LEAVES.index(floor) + 1]
    wanted = set(leaves)

    hands = known = 0
    acted: dict[str, list[Facts]] = {leaf: [] for leaf in leaves}
    idle: dict[str, list[Facts]] = {leaf: [] for leaf in leaves}
    for f in facts:
        # In the hand when the street began: what `action_<street>=*` finds.
        if street in f.seq:
            hands += 1
        leaf = _made_leaf(f, street)
        if leaf is None:
            continue
        known += 1
        if leaf in wanted:
            (acted if street in f.line else idle)[leaf].append(f)

    rows = [_row(leaf, f"made_{street}={leaf}", street, acted[leaf], idle[leaf]) for leaf in leaves]
    total = _row(
        "total", f"made_{street}>={floor}", street,
        [f for leaf in leaves for f in acted[leaf]],
        [f for leaf in leaves for f in idle[leaf]],
    )
    return {
        "street": street,
        "floor": floor,
        "hands": hands,
        "known": known,
        "coverage": _pct(known, hands),
        # The spot filters behind `hands` and `known`, so the tiles reproduce too.
        "hands_filter": f"action_{street}=*",
        "known_filter": f"made_{street}>=high_card",
        "categories": [
            {"key": key, "label": label, "slowplay": slow} for key, label, _, _, slow in CATEGORIES
        ],
        "rows": rows,
        "total": total,
    }


def by_player(
    grouped: Mapping[int, list[Facts]],
    aliases: Mapping[int, str],
    street: str,
    floor: str = DEFAULT_FLOOR,
    min_n: int = 1,
) -> list[dict]:
    """Every player's total row, most slowplays by share first: who traps.

    `grouped` is `queries.facts_by_player`'s, already filtered to the spot. A player
    under `min_n` strong hands acted on is left out, since a share of two is noise.
    """
    out = []
    for pid, facts in grouped.items():
        rep = slowplay(facts, street, floor)
        t = rep["total"]
        if t["n"] < max(1, min_n):
            continue
        out.append({
            "player": aliases.get(pid, "?"),
            "hands": rep["hands"],
            "known": rep["known"],
            "coverage": rep["coverage"],
            "n": t["n"],
            "slowplay": t["slowplay"],
            "slowplay_pct": t["slowplay_pct"],
            "cells": {k: c["n"] for k, c in t["cells"].items()},
        })
    out.sort(key=lambda r: (-(r["slowplay_pct"] or 0), -r["n"]))
    return out
