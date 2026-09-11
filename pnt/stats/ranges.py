"""Range views: what a player *had* in a spot, from the hands where it was shown.

Three bucketings over the same filtered hand set:

- ``preflop``  the 169-cell chart. Which starting hands took this line, how
  often, and what they did with them (net won, open size).
- ``made``     what those hands had made by the end of the hand: pair or worse,
  two pair, and so on -- the composition of a line.
- ``sizing``   the made-hand composition split by bet size on one street: what
  they bet small with, and what they bet big with.

All carry a **coverage** figure and it is the most important number on the page.
Cards are only known when a hand is shown, and hands that are shown are hands
that reached showdown. A "3-bet range" built this way is really "3-bet hands that
went to showdown": the bluffs that folded out are the ones missing. Coverage is
how much of the line the chart is actually looking at.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import mean, median, multimode

from .cards import ALL_CLASSES, MADE_CLASSES, grid_labels, hand_class, made_hand
from .derive import POSTFLOP_STREETS, SIZE_BUCKETS, Facts

SIZING_KINDS = ("cbet", "bet", "faced_cbet")


def _bb(f: Facts) -> float | None:
    return f.net / f.bb_size if f.bb_size else None


def _pct(num: int, den: int) -> float | None:
    return round(100.0 * num / den, 1) if den else None


def size_stats(values: list[float]) -> dict | None:
    """min / max / mean / median / mode of a list of sizes, or None when empty.

    The mode is the most frequent size; on a tie between sizes that genuinely
    repeat, the one nearest the median. **`mode` is None when nothing repeats**:
    two raises of 3.5bb and 15bb have no most-common size, and picking one of
    them would be an artifact of list order rather than a fact about the player.
    """
    if not values:
        return None
    med = median(values)
    modes = multimode(values)
    # multimode returns every distinct value when all are equally frequent.
    no_mode = len(modes) == len(set(values))
    return {
        "n": len(values),
        "min": round(min(values), 1),
        "max": round(max(values), 1),
        "mean": round(mean(values), 2),
        "median": round(med, 1),
        "mode": None if no_mode else round(min(modes, key=lambda m: abs(m - med)), 1),
    }


def typical_size(values: list[float], tolerance: float) -> tuple[float | None, str]:
    """The size a player *habitually* uses, ignoring one-offs. -> (size, basis).

    An all-in is logged as a normal ``raises to N``, so a single tilt jam enters a
    cell as a 270bb "raise" beside a row of 3bb opens. Which average survives that
    depends on whether the cell's sizes agree:

    - **They agree** (spread within `tolerance`): no outlier is present, so the
      **mean** uses every observation and is the more precise centre.
    - **They disagree**, with three or more hands: an outlier is present, so the
      **median** steps over it. This is the case the mean cannot survive -- one
      270bb jam among six 3bb opens drags the mean to 57bb.
    - **They disagree, with only two raises**: there is no third raise to break the
      tie, so this falls back to their **midpoint**. That is deliberately a size
      the player never used: it is a summary of two contradictory raises, not a
      claim about a habit, and the basis says so. Reporting it still beats
      reporting nothing, because "raised small once and large once" is itself
      worth seeing on the chart.

    A single raise is reported as-is under the basis ``single``: it is the only
    evidence there is, and the cell shows its own sample count beside it.
    """
    if not values:
        return None, "none"
    if len(values) == 1:
        return values[0], "single"
    if max(values) - min(values) <= tolerance:
        return round(mean(values), 1), "mean"
    if len(values) >= 3:
        return round(median(values), 1), "median"
    return round(median(values), 1), "midpoint"


def _summary(rows: list[Facts], tolerance: float = 1.0) -> dict:
    scaled = [v for v in (_bb(f) for f in rows) if v is not None]
    raises = [f.pf_raise_bb for f in rows if f.pf_raise_bb is not None]
    stats = size_stats(raises)
    typical, basis = typical_size(raises, tolerance)
    # Raise frequency uses the PFR definition from SPEC.md: raised / hands where
    # the player actually got to act preflop. A big-blind walk is not a decision.
    pf_opp = sum(1 for f in rows if f.pfr_opp)
    pfr = sum(1 for f in rows if f.pfr)
    return {
        "n": len(rows),
        "net_bb": round(sum(scaled), 1) if scaled else 0.0,
        "won": sum(1 for f in rows if f.net > 0),
        "pf_opp": pf_opp,
        "pfr": pfr,
        "raise_pct": round(100.0 * pfr / pf_opp, 1) if pf_opp else None,
        # Typical open/raise size with this holding; the "usually 3bb, but 5bb
        # with these" signal. Median so one shove does not move it.
        "raise_bb": stats["median"] if stats else None,
        # What the chart colours by: the habitual size, or None when this cell
        # has no settled one. `raise_basis` says which rule produced it.
        "raise_typical": typical,
        "raise_basis": basis,
        "raised": len(raises),
        "raise": stats,
        # The hands behind this row, so a view can list and replay them.
        "hand_ids": [f.hand_id for f in rows],
    }


def _tolerance(spot: dict | None) -> float:
    """How far apart two sizes can be and still count as "the same size".

    One of the player's usual raises: a 3bb opener varying between 2bb and 5bb is
    sizing consistently, and the same 3bb spread would be noise in a 10bb game.
    """
    return max(1.0, spot["median"]) if spot else 1.0


def _spot_sizes(facts: list[Facts]) -> dict | None:
    """Raise sizing over *every* hand in the spot, cards known or not.

    Sizing needs no showdown, so this is the one range figure with full coverage.
    The chart colours each cell by its distance from this median.
    """
    return size_stats([f.pf_raise_bb for f in facts if f.pf_raise_bb is not None])


def range_grid(facts: list[Facts]) -> dict:
    """The 169-cell chart for an already-filtered list of Facts.

    Every cell is present, in chart order (``grid_labels``), so a renderer can
    lay the result out without knowing anything about cards.
    """
    known = [f for f in facts if f.hole_cards]
    by_class: dict[str, list[Facts]] = defaultdict(list)
    for f in known:
        by_class[hand_class(f.hole_cards)].append(f)

    spot = _spot_sizes(facts)
    tol = _tolerance(spot)
    cells = {label: _summary(by_class.get(label, []), tol) for label in ALL_CLASSES}
    return {
        "hands": len(facts),
        "known": len(known),
        "coverage": round(100.0 * len(known) / len(facts), 1) if facts else None,
        "raise": spot,
        "raise_tolerance": tol,
        "rows": grid_labels(),
        "cells": cells,
    }


def composition(facts: list[Facts]) -> dict:
    """What the shown hands in this set had made, strongest class first.

    ``pct`` is of *known* hands, not of the line -- the unknown ones are exactly
    the ones this cannot speak for.
    """
    known = [f for f in facts if f.hole_cards]
    by_cls: dict[str, list[Facts]] = defaultdict(list)
    by_detail: dict[str, dict[str, list[Facts]]] = defaultdict(lambda: defaultdict(list))
    for f in known:
        mh = made_hand(f.hole_cards, f.board)
        by_cls[mh.cls].append(f)
        if mh.detail:
            by_detail[mh.cls][mh.detail].append(f)

    spot = _spot_sizes(facts)
    tol = _tolerance(spot)

    def row(label: str, rows: list[Facts]) -> dict:
        return {
            "class": label,
            "pct": round(100.0 * len(rows) / len(known), 1) if known else None,
            **_summary(rows, tol),
        }

    order = (*MADE_CLASSES, "no_board")
    classes = []
    for cls in order:
        if cls not in by_cls:
            continue
        entry = row(cls, by_cls[cls])
        if cls in by_detail:
            entry["details"] = [
                row(d, rows)
                for d, rows in sorted(by_detail[cls].items(), key=lambda kv: -len(kv[1]))
            ]
        classes.append(entry)

    return {
        "hands": len(facts),
        "known": len(known),
        "coverage": round(100.0 * len(known) / len(facts), 1) if facts else None,
        "raise": spot,
        "raise_tolerance": tol,
        "classes": classes,
    }


def sizing_tells(facts: list[Facts], street: str, kind: str = "cbet") -> dict:
    """What a player had at each bet size on one street: the sizing tell.

    `kind` picks the bets:

    - ``cbet``        their c-bets by size, plus a ``check`` block for the c-bet
                      chances they declined. ``pct`` is of those chances.
    - ``bet``         every first bet on the street, c-bet or not. ``pct`` is of bets.
    - ``faced_cbet``  the c-bets they faced, by that c-bet's size, with fold / call /
                      raise rates. ``pct`` is of c-bets faced, and the made hands are
                      the ones that continued.

    Every block lists all of its hands in ``hand_ids``, shown or not; ``classes``
    is the composition of the shown ones, so read ``coverage`` first here too.
    """
    if street not in POSTFLOP_STREETS:
        raise ValueError(f"unknown street {street!r}. Known: {', '.join(POSTFLOP_STREETS)}")
    if kind not in SIZING_KINDS:
        raise ValueError(f"unknown sizing kind {kind!r}. Known: {', '.join(SIZING_KINDS)}")

    if kind == "cbet":
        pool = [f for f in facts if f.cbet_opp.get(street)]
        order = (*SIZE_BUCKETS, "check")

        def bucket(f: Facts) -> str:
            return f.bet_size.get(street, "check") if f.cbet.get(street) else "check"
    elif kind == "bet":
        pool = [f for f in facts if street in f.bet_size]
        order = SIZE_BUCKETS

        def bucket(f: Facts) -> str:
            return f.bet_size[street]
    else:
        pool = [f for f in facts if street in f.faced_cbet_size]
        order = SIZE_BUCKETS

        def bucket(f: Facts) -> str:
            return f.faced_cbet_size[street]

    groups: dict[str, list[Facts]] = defaultdict(list)
    for f in pool:
        groups[bucket(f)].append(f)

    blocks = []
    for size in order:
        rows = groups.get(size, [])
        block: dict = {"size": size, "n": len(rows), "pct": _pct(len(rows), len(pool))}
        shown = rows
        if kind == "faced_cbet":
            folded = sum(1 for f in rows if f.fold_to_cbet.get(street))
            raised = sum(1 for f in rows if f.raise_cbet.get(street))
            block |= {
                "fold": _pct(folded, len(rows)),
                "call": _pct(len(rows) - folded - raised, len(rows)),
                "raise": _pct(raised, len(rows)),
            }
            shown = [f for f in rows if not f.fold_to_cbet.get(street)]
            block["continued"] = len(shown)
        comp = composition(shown)
        block |= {
            "known": comp["known"],
            "coverage": comp["coverage"],
            "classes": comp["classes"],
            "hand_ids": [f.hand_id for f in rows],
        }
        blocks.append(block)

    return {
        "street": street,
        "kind": kind,
        "hands": len(facts),
        "spot": len(pool),
        "blocks": blocks,
    }
