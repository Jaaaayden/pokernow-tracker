"""Fold equity by bet size: how often a bet of each size wins the pot on the spot.

Two sides of the same bet, per street and size bucket:

- **The bettor's.** Every first bet the player made on the street, and what it
  got back (`Facts.bet_answer`): everyone folded, someone called, someone raised.
  Beside it the **break-even fold %** of a bet that size, ``s / (1 + s)`` with
  ``s`` its `bet_pot`, averaged over the bucket's bets: a bet with no equity at
  all -- the pure bluff -- shows a profit exactly when it is folded to more often
  than that. The **edge** is the realized fold % minus the break-even, in points.
- **The defender's.** Every first decision facing a bet or raise
  (`Facts.faced_bet_size` / `faced_bet_action`), by the size of what they faced,
  against the **minimum defense frequency**, ``1 / (1 + s)``, averaged the same
  way: the share of the time they must call or raise to keep that same pure bluff
  from profiting. A fold % above ``100 - MDF`` is **over-folding**, and
  over-folders are the players to bluff. The c-bets they faced (`faced_cbet_size`) are listed again on their
  own, the c-bet-only split `rates()` has always carried and no page showed.

Nothing here needs a shown card, so there is no coverage figure to read first:
every bet and every fold is in the log. It is still a statement about frequencies
only -- a pure bluff's break-even, not a verdict on any one bet, most of which
had equity when called.

Each row carries the filter that finds its hands, so a number on any page goes
back to the hands behind it. See SPEC.md, "Fold equity by bet size".
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from statistics import mean, median

from .derive import ANSWERS, POSTFLOP_STREETS, SIZE_BUCKETS, Facts
from .queries import load_hands, play_facts

#: Below this many bets (or bets faced) a row is still reported, but not judged:
#: a 100% fold rate off two bets says nothing about the next one. The views only
#: highlight a positive edge or an over-fold on a row with at least this many.
#: The smallest sample the exploit tags fire on (SPEC.md, "Tags").
MIN_SAMPLE = 10


def _pct(num: int, den: int) -> float | None:
    return round(100.0 * num / den, 1) if den else None


def break_even(size: float) -> float:
    """The fold share a pure bluff of `size` (a pot fraction) needs to break even.

    It risks `size` to win the pot, 1: ``size / (1 + size)``. A half-pot bet needs
    a third, a pot-sized bet half.
    """
    return size / (1.0 + size)


def _row(
    size: str,
    rows: list[Facts],
    sizes: list[float],
    answers: list[str],
    *,
    filter_: str,
    fold_filter: str | None,
    with_ids: bool,
) -> tuple[dict, float | None]:
    """One size bucket: counts, the three rates, the sizes behind them, and the
    bucket's break-even fold share, unrounded, for the caller to measure against.

    `sizes` are the bets' pot fractions, and `answers` gives each row's one answer
    (or "" when it has none), both in the order of `rows`. Rates are of every row
    in the bucket, answered or not: the reading `rates()` gives the same numbers,
    so the two never disagree on a page. A bet nobody answered -- a log that stops
    after it -- is the only kind left out of all three.

    The break-even is the **mean of each bet's own** ``s / (1 + s)``, not the
    break-even of the mean size. A pure bluff of `s` pots, folded to with
    probability p, makes ``p - (1 - p) * s`` pots, which over the pot *after* the
    bet, ``1 + s``, is ``p - s / (1 + s)``. So the realized fold % minus this mean
    is a bluff's average result per bet, each bet measured against the pot it
    made, and it is zero exactly at the break-even. The other reading, the mean
    size's break-even, measures each bet against the pot *before* it, where a
    200-pot jam into a limped pot risks as much as four hundred half-pot bets: in
    the flop overbet bucket that moved the break-even from 70% to 84% on its own.
    Inside the four bounded buckets the two agree to a few tenths.
    """
    n = len(rows)
    counts = {a: sum(1 for x in answers if x == a) for a in ANSWERS}
    be = sum(break_even(x) for x in sizes) / len(sizes) if sizes else None
    out: dict = {
        "size": size,
        "n": n,
        "folds": counts["fold"],
        "calls": counts["call"],
        "raises": counts["raise"],
        **{a: _pct(counts[a], n) for a in ANSWERS},
        # Pot fractions: 0.5 is half the pot. None on an empty bucket. The median
        # is what a view prints as the bucket's size: a jam into a small pot can
        # carry the overbet bucket's mean to five pots.
        "mean_size": round(mean(sizes), 3) if sizes else None,
        "median_size": round(median(sizes), 3) if sizes else None,
        "break_even": round(100.0 * be, 1) if be is not None else None,
        "judged": n >= MIN_SAMPLE,
        "filter": filter_,
    }
    if fold_filter is not None:
        out["fold_filter"] = fold_filter
    if with_ids:
        out["hand_ids"] = [f.hand_id for f in rows]
        out["fold_hand_ids"] = [f.hand_id for f, a in zip(rows, answers) if a == "fold"]
    return out, be


def _beyond(row: dict, be: float | None) -> float | None:
    """The row's fold % minus its break-even, in points."""
    return round(100.0 * row["folds"] / row["n"] - 100.0 * be, 1) if be is not None else None


def _bets(facts: list[Facts], street: str, with_ids: bool) -> list[dict]:
    """The bettor's side: their first bet on `street`, by size, and what it got."""
    out = []
    for size in SIZE_BUCKETS:
        rows = [f for f in facts if f.bet_size.get(street) == size]
        row, be = _row(
            size,
            rows,
            [f.bet_pot[street] for f in rows],
            [f.bet_answer.get(street, "") for f in rows],
            filter_=f"bet_{street}={size}",
            fold_filter=f"bet_{street}={size},bet_answer_{street}=fold",
            with_ids=with_ids,
        )
        row["unanswered"] = row["n"] - row["folds"] - row["calls"] - row["raises"]
        # Positive: a pure bluff this size, made as often as they made these,
        # would have shown a profit.
        row["edge"] = _beyond(row, be)
        out.append(row)
    return out


def _defense(row: dict, be: float | None) -> dict:
    """MDF and the over-fold on a defender's row."""
    return row | {
        "mdf": round(100.0 * (1.0 - be), 1) if be is not None else None,
        # Points above the fold % they can afford, `break_even`: positive
        # over-folds, negative under-folds (defends wider than the bet demands).
        "overfold": _beyond(row, be),
    }


def _faced(facts: list[Facts], street: str, with_ids: bool) -> list[dict]:
    """The defender's side: their first decision facing a bet or raise on `street`."""
    out = []
    for size in SIZE_BUCKETS:
        rows = [f for f in facts if f.faced_bet_size.get(street) == size]
        out.append(_defense(*_row(
            size,
            rows,
            [f.faced_bet_pot[street] for f in rows],
            [f.faced_bet_action.get(street, "") for f in rows],
            filter_=f"faced_bet_{street}={size}",
            # No term reads `faced_bet_action`, and `folded_to_bet_` is any fold on
            # the street, a call and then a fold to the raise behind it included:
            # the folded part has no exact filter of its own.
            fold_filter=None,
            with_ids=with_ids,
        )))
    return out


def _faced_cbet(facts: list[Facts], street: str, with_ids: bool) -> list[dict]:
    """The c-bets they faced, by that c-bet's size: the subset of `_faced` where
    the bet was the previous street's aggressor's, before anyone raised it.

    The c-bet is the first bet of the street and nobody has raised it, so it is
    the first bet these players face there: its pot fraction is their
    `faced_bet_pot`, and the MDF reads it from there.
    """
    out = []
    for size in SIZE_BUCKETS:
        rows = [f for f in facts if f.faced_cbet_size.get(street) == size]
        answers = [
            "fold" if f.fold_to_cbet.get(street) else "raise" if f.raise_cbet.get(street) else "call"
            for f in rows
        ]
        out.append(_defense(*_row(
            size,
            rows,
            [f.faced_bet_pot[street] for f in rows if street in f.faced_bet_pot],
            answers,
            filter_=f"faced_cbet_{street}={size}",
            fold_filter=f"faced_cbet_{street}={size},folded_to_cbet_{street}",
            with_ids=with_ids,
        )))
    return out


def fold_equity(facts: Iterable[Facts], with_ids: bool = False) -> dict:
    """Fold equity by bet size, both sides, for an already-filtered set of Facts.

    One player's facts give their own bets and their own defense; the whole pool's
    give everyone's, and every count of the pool is the sum of the players'.
    `with_ids` adds each row's hand ids (and the folded ones), for a view that
    lists them on a click -- one player's only, where a hand id is one row.
    """
    facts = list(facts)
    streets = {}
    for street in POSTFLOP_STREETS:
        bets = _bets(facts, street, with_ids)
        faced = _faced(facts, street, with_ids)
        cbet = _faced_cbet(facts, street, with_ids)
        streets[street] = {
            "bets": bets,
            "faced": faced,
            "faced_cbet": cbet,
            "totals": {
                "bets": sum(r["n"] for r in bets),
                "faced": sum(r["n"] for r in faced),
                "faced_cbet": sum(r["n"] for r in cbet),
            },
        }
    return {"hands": len(facts), "min_sample": MIN_SAMPLE, "streets": streets}


def pool_facts(
    conn: sqlite3.Connection,
    game_id: str | None = None,
    predicate: Callable[[Facts], bool] | None = None,
) -> list[Facts]:
    """Every (hand, player) row in the database, or in one game, inside a spot.

    The whole table's betting rather than one player's: the bets everybody made
    and the bets everybody faced. Derived afresh, like a filtered `report()` -- a
    predicate cannot key a cache, and this is asked by a person, not on a timer.
    """
    # The session facts first, so a spot like `post_loss` reads whole sessions.
    facts = play_facts(load_hands(conn, game_id))
    return [f for f in facts if predicate(f)] if predicate is not None else facts
