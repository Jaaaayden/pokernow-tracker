"""Made hands by street: what a holding was on the flop against what it was by the
river, and, for a log's owner, where each flop holding went.

The shown matrix reads every hand with the cards known that reached the river,
so it leans the way shown hands lean: the ones that went to showdown. The owner's
funnel has no such lean -- every card of their own logs is known -- and follows
each flop holding to a fold, a pot won uncontested, or a showdown.

Every cell and path carries the filter terms that find its hands, to add to the
spot they were counted in. See SPEC.md, "Holdings by street".
"""

from __future__ import annotations

from collections.abc import Collection, Iterable

from .cards import MADE_GROUPS, MADE_LEAVES, leaf_of, made_at
from .derive import Facts
from .filters import parse_filter

#: The holding groups, strongest first: (key, label, the `made_<street>` operator
#: and names that say it, leaves). Built from `cards.MADE_GROUPS`, with `pair`
#: split by what the pair is worth and the board pair put with high card -- the
#: hole cards add nothing to either.
_STRONG = ("straight_flush", "quads", "full_house", "flush", "straight")
_TOP = ("overpair", "top_pair")
_WEAK = ("pocket_pair", "middle_pair", "bottom_pair")
assert set(_TOP + _WEAK + ("board_pair",)) == set(MADE_GROUPS["pair"])
GROUPS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("strong", "straight+", ">=straight", tuple(x for g in _STRONG for x in MADE_GROUPS[g])),
    ("trips", "set / trips", "=set|trips", MADE_GROUPS["trips"]),
    ("two_pair", "two pair", "=two_pair", MADE_GROUPS["two_pair"]),
    ("top_pair", "top pair+", "=" + "|".join(_TOP), _TOP),
    ("weak_pair", "weaker pair", "=" + "|".join(_WEAK), _WEAK),
    ("draw", "draw", "=draw", MADE_GROUPS["draw"]),
    ("air", "air", "=board_pair|high_card", ("board_pair", *MADE_GROUPS["high_card"])),
)
GROUP_OF = {leaf: key for key, _, _, leaves in GROUPS for leaf in leaves}
assert set(GROUP_OF) == set(MADE_LEAVES), "every leaf in exactly one group"

#: Any known holding on the river: what "reached the river, cards known" is as a term.
REACHED_RIVER = "made_river>=high_card"

#: The owner's funnel paths, in the order a hand can end: (key, label, terms).
#: "Reached" a street is the whole-street sequence's key, as in the give-up funnel.
PATHS: tuple[tuple[str, str, str], ...] = (
    ("fold_flop", "folded the flop", "fold_street=flop"),
    ("won_flop", "won on the flop, no showdown", "won_pot,!wtsd,action_turn=-"),
    ("fold_turn", "folded the turn", "fold_street=turn"),
    ("won_turn", "won on the turn, no showdown", "won_pot,!wtsd,action_turn=*,action_river=-"),
    ("fold_river", "folded the river", "fold_street=river"),
    ("won_river", "won on the river, no showdown", "won_pot,!wtsd,action_river=*"),
    ("showdown", "reached showdown", "wtsd"),
)

_ORDER = {"preflop": 0, "flop": 1, "turn": 2, "river": 3}


def _leaf(f: Facts, street: str) -> str | None:
    """The `made_<street>` reading: their holding on that street's board, or None
    when the cards are unknown, the board never got there, or they had folded on
    an earlier street."""
    if f.hole_cards is None:
        return None
    if f.fold_street is not None and _ORDER[f.fold_street] < _ORDER[street]:
        return None
    mh = made_at(f.hole_cards, f.board, street)
    return None if mh is None else leaf_of(mh)


def group_at(f: Facts, street: str) -> str | None:
    leaf = _leaf(f, street)
    return None if leaf is None else GROUP_OF[leaf]


_SPEC = {key: spec for key, _, spec, _ in GROUPS}


def _term(street: str, group: str) -> str:
    return f"made_{street}{_SPEC[group]}"


def _net_bb(facts: Iterable[Facts]) -> float:
    """Net big blinds over the complete hands, as bb/100 is made of."""
    return round(sum(f.net / f.bb_size for f in facts if f.complete and f.bb_size), 1)


def _pct(n: int, d: int) -> float | None:
    return round(100.0 * n / d, 1) if d else None


def _cell(facts: list[Facts], filt: str) -> dict:
    return {"hands": len(facts), "net_bb": _net_bb(facts), "filter": filt}


def _matrix(facts: list[Facts], rows: str) -> dict:
    """Hands by their group on street `rows` against the river's, over the hands
    with both: the ones with cards known that reached the river."""
    cols = "river"
    grid: dict[str, dict[str, list[Facts]]] = {g: {c: [] for c, *_ in GROUPS} for g, *_ in GROUPS}
    for f in facts:
        r, c = group_at(f, rows), group_at(f, cols)
        if r is not None and c is not None:
            grid[r][c].append(f)
    out_rows = []
    for g, label, *_ in GROUPS:
        hit = [f for c in grid[g].values() for f in c]
        out_rows.append({
            rows: g,
            "label": label,
            **_cell(hit, f"{_term(rows, g)},{REACHED_RIVER}"),
            "cells": [
                {cols: c, **_cell(grid[g][c], f"{_term(rows, g)},{_term(cols, c)}")} for c, *_ in GROUPS
            ],
        })
    totals = [
        {cols: c, **_cell([f for g in grid for f in grid[g][c]], _term(cols, c))} for c, *_ in GROUPS
    ]
    return {"hands": sum(r["hands"] for r in out_rows), "rows": out_rows, "totals": totals}


def shown_matrix(facts: Iterable[Facts]) -> dict:
    """Flop group against river group over the hands shown that reached the river,
    and per flop group how many of the shown hands that saw the flop got there."""
    facts = list(facts)
    out = _matrix(facts, "flop")
    flops = [f for f in facts if _leaf(f, "flop") is not None]
    for row in out["rows"]:
        seen = sum(1 for f in flops if group_at(f, "flop") == row["flop"])
        row["saw_flop"] = seen
        row["saw_flop_filter"] = _term("flop", row["flop"])
        row["reach_river_pct"] = _pct(row["hands"], seen)
    out["saw_flop"] = len(flops)
    return out


def hero_funnel(facts: Iterable[Facts]) -> dict:
    """Of the flops seen, by flop group: how each ended, and the turn group against
    the river group over the hands that reached the river. `facts` must be hands
    with every card known -- the owner's own logs."""
    flops = [f for f in facts if _leaf(f, "flop") is not None]
    preds = [(key, label, terms, parse_filter(terms)) for key, label, terms in PATHS]
    rows = []
    for g, label, *_ in GROUPS:
        mine = [f for f in flops if group_at(f, "flop") == g]
        base = _term("flop", g)
        paths, left = [], mine
        for key, plabel, terms, pred in preds:
            hit = [f for f in left if pred(f)]
            left = [f for f in left if not pred(f)]
            paths.append({"key": key, "label": plabel, **_cell(hit, f"{base},{terms}"),
                          "pct": _pct(len(hit), len(mine))})
        # A log cut short ends none of these ways: no term says it, so no filter.
        paths.append({"key": "other", "label": "log cut short", "hands": len(left),
                      "net_bb": _net_bb(left), "filter": None, "pct": _pct(len(left), len(mine))})
        rows.append({"flop": g, "label": label, **_cell(mine, base), "paths": paths})
    return {
        "flops": len(flops),
        "rows": rows,
        "turn_river": _matrix(flops, "turn"),
    }


def made_by_street(facts: Iterable[Facts], owners: Collection[tuple[str, str]] = ()) -> dict:
    """The `/players/{alias}/streets` payload. `owners` is (game, PokerNow ID) for
    each log's owner (`pool.owners`); the funnel reads those hands alone, and is
    None when the player owns no log in the spot."""
    facts = list(facts)
    own = set(owners)
    mine = [f for f in facts if (f.game_id, f.pn_id) in own]
    notes = [
        "Shown matrix: cards known and reached the river; showdowns are most of it.",
        "reach river: of the shown hands that saw the flop; runs high, since shown hands went far.",
        "net bb: complete hands; filters add to the spot's.",
    ]
    if mine:
        notes.append("Funnel: your own logs, every card known; no showdown lean.")
    return {
        "groups": [{"key": k, "label": label, "leaves": list(leaves)} for k, label, _, leaves in GROUPS],
        "shown_matrix": shown_matrix(facts),
        "hero_funnel": hero_funnel(mine) if mine else None,
        "notes": notes,
    }
