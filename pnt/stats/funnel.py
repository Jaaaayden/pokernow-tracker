"""The give-up funnel: where a player's flops went, street by street.

Of the flops they saw, how many turns, rivers and showdowns they were still in
for, and how many showdowns they won. Beside it, WWSF -- how often a flop ended
with them collecting the pot -- and the part of that won without a showdown. And
the other side: where they folded after the flop, split by whether they had been
the previous street's aggressor. Low WWSF beside a normal WTSD is a player who
gives up too much without cards; the fold rows show on which street.

Every row carries the filter terms that find exactly its hands, to add to the
spot they were counted in, and every fold row also the terms that find what it
is a share of. See SPEC.md, "Give-up funnel".
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from .derive import Facts
from .filters import parse_filter

#: The funnel's steps, widest first: (key, label, filter terms). "Reached" is the
#: whole-street sequence's key, which every player still in when the street began
#: has, an all-in player included -- so `action_turn=*` is "was in it on the turn".
STEPS: tuple[tuple[str, str, str], ...] = (
    ("flop", "saw the flop", "saw_flop"),
    ("turn", "reached the turn", "saw_flop,action_turn=*"),
    ("river", "reached the river", "saw_flop,action_river=*"),
    ("showdown", "went to showdown", "saw_flop,wtsd"),
    ("won_sd", "won at showdown", "saw_flop,won_sd"),
)

#: WWSF and its no-showdown part, over the flops seen. The same counts as
#: `rates()["wwsf"]` and `rates()["wwsf_nsd"]`.
WON: tuple[tuple[str, str, str], ...] = (
    ("wwsf", "won the pot", "saw_flop,won_pot"),
    ("wwsf_nsd", "won it without showdown", "saw_flop,won_pot,!wtsd"),
)

#: The streets a flop can be folded on, each with the previous street and the
#: flag that marks its aggressor. The flop's previous street is preflop, whose
#: aggressor is the preflop aggressor.
FOLD_STREETS: tuple[tuple[str, str, str], ...] = (
    ("flop", "preflop", "pfa"),
    ("turn", "flop", "aggressor_flop"),
    ("river", "turn", "aggressor_turn"),
)


def _pct(num: int, den: int) -> float | None:
    return round(100.0 * num / den, 1) if den else None


def _count(facts: list[Facts], terms: str) -> int:
    pred: Callable[[Facts], bool] = parse_filter(terms)
    return sum(1 for f in facts if pred(f))


def _fold_row(key: str, label: str, filt: str, of_filt: str, facts: list[Facts]) -> dict:
    """Folds and what they are a share of: the hands that reached the street the
    same way. `pct` is how often reaching it that way ended in a fold there."""
    n, of = _count(facts, filt), _count(facts, of_filt)
    return {"key": key, "label": label, "filter": filt, "n": n,
            "of_filter": of_filt, "of": of, "pct": _pct(n, of)}


def funnel(facts: Iterable[Facts]) -> dict:
    """One player's flops, already narrowed to a spot, as a funnel and its folds.

    `steps` narrow from the flops seen: each row's `pct` is of the flops seen,
    `step_pct` of the row above. `won` is WWSF and W$WSF over the same flops.
    `folds` has a row per street they folded on after the flop, its `pct` the
    share of the hands that reached that street, and two `roles` -- as the
    previous street's aggressor, and not -- each over the hands that reached the
    street in that role. `share` is a row's part of every fold after the flop.

    Every count is over every hand, an incomplete one included, so every filter
    reproduces its `n`. `complete` says how many of the flops seen have a log
    that ran to the end: a hand cut short reached no further than its log.
    """
    facts = list(facts)
    flops = [f for f in facts if f.saw_flop]
    seen = len(flops)

    steps: list[dict] = []
    prev: int | None = None
    for key, label, filt in STEPS:
        n = _count(flops, filt)
        steps.append({
            "key": key, "label": label, "filter": filt, "n": n,
            "pct": _pct(n, seen), "step_pct": _pct(n, prev) if prev is not None else None,
        })
        prev = n

    won = [
        {"key": key, "label": label, "filter": filt, "n": (n := _count(flops, filt)), "pct": _pct(n, seen)}
        for key, label, filt in WON
    ]

    folds: list[dict] = []
    for street, before, agg in FOLD_STREETS:
        reached = "saw_flop" if street == "flop" else f"saw_flop,action_{street}=*"
        filt = f"saw_flop,fold_street={street}"
        row = _fold_row(street, f"folded the {street}", filt, reached, flops)
        row["roles"] = [
            _fold_row(f"{street}:aggressor", f"as the {before} aggressor",
                      f"{filt},{agg}", f"{reached},{agg}", flops),
            _fold_row(f"{street}:other", f"not the {before} aggressor",
                      f"{filt},!{agg}", f"{reached},!{agg}", flops),
        ]
        folds.append(row)
    total_folds = sum(r["n"] for r in folds)
    for r in folds:
        r["share"] = _pct(r["n"], total_folds)
        for x in r["roles"]:
            x["share"] = _pct(x["n"], total_folds)

    return {
        "hands": len(facts),
        "flops": seen,
        "complete": sum(1 for f in flops if f.complete),
        "steps": steps,
        "won": won,
        "folds": folds,
        "total_folds": total_folds,
    }
