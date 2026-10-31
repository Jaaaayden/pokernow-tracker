"""Results by pot type: where a player's big blinds were won and where they went.

One player's hands, already narrowed to a spot, split three ways -- by the kind of
pot (walk, limped, single-raised, 3-bet, 4-bet and up, each split again by who made
the last raise), by seat, and by whether they were in position after the flop.
Every row is a `queries.Tally`, so the money is the same exact sum bb/100 is made
of everywhere else: each complete hand's net in its own big blinds, with an
incomplete hand counted among the hands and left out of every bb figure.

Each table partitions the hands, so its rows add up to the player's total, chip
for chip. And each row carries the filter terms that find exactly its hands --
added to the spot on the chart, they open the hands behind the number. The one
split no filter can say yet is in position; those rows carry their hand ids
instead. See SPEC.md, "Results by pot type".
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from ..logfmt.parser import position_name
from .derive import Facts
from .queries import rates, tally

#: A row's bb/100 is only held up as the worst once it stands on this many hands
#: with a known result. Below it a single big pot decides the sign.
WORST_MIN_HANDS = 100

#: The pot types, in order of how many times the pot was raised preflop:
#: (key, label, filter terms, test). `limped` and `walk` divide `pot_level` 1
#: between them on whether the player ever had a preflop decision -- the big blind
#: everyone folded to never does. A bomb pot, a PLO hand and a Double Board hand
#: have `pot_level` 0: no preflop betting read. See SPEC.md.
POT_TYPES: tuple[tuple[str, str, str, Callable[[Facts], bool]], ...] = (
    ("walk", "walk", "limped,!acted_preflop", lambda f: f.pot_level == 1 and not f.vpip_opp),
    ("limped", "limped", "limped,acted_preflop", lambda f: f.pot_level == 1 and f.vpip_opp),
    ("srp", "single-raised", "srp", lambda f: f.pot_level == 2),
    ("3bet", "3-bet pot", "3bet_pot", lambda f: f.pot_level == 3),
    ("4bet", "4-bet pot+", "4bet_pot", lambda f: f.pot_level >= 4),
    ("bomb", "bomb pot", "holdem,!double_board,bomb_pot", lambda f: f.analysed and f.bomb_pot),
    ("double", "double board", "holdem,double_board", lambda f: f.holdem and f.double_board),
    ("plo", "PLO", "plo", lambda f: not f.holdem),
)

#: Who made the last preflop raise. Only a raised pot has one, so only the raised
#: pot types are split by it; in a limped pot everyone -- limper, the big blind
#: checking its option, the player who folded -- is "caller / other".
ROLES: tuple[tuple[str, str, str, Callable[[Facts], bool]], ...] = (
    ("pfa", "PFA", "pfa", lambda f: f.pfa),
    ("other", "caller / other", "!pfa", lambda f: not f.pfa),
)

#: Seats in the order they act preflop, the blinds last. Heads-up's button is
#: also the small blind and sits between the two.
SEAT_ORDER = ("UTG", "UTG+1", "UTG+2", "UTG+3", "LJ", "HJ", "CO", "BTN", "BTN/SB", "SB", "BB")


def _pct(num: int, den: int) -> float | None:
    return round(100.0 * num / den, 1) if den else None


def _row(key: str, label: str, filt: str | None, facts: list[Facts]) -> dict:
    """One row: its hands as a Tally, read out the way the stat rows are."""
    t = tally(facts)
    r = rates(t) if t.hands else {}
    c = t.counts
    # VPIP is the stat rows' own: over the hands with a preflop decision
    # (`vpip_opp`), so a walk, a bomb pot (no preflop decision) and PLO or Double
    # Board (money only, never analysed) are out of it. See SPEC.md, "Preflop".
    row = {
        "key": key,
        "label": label,
        # The terms that find exactly these hands, to add to the spot they were
        # counted in. None where no filter term can say it: then `hand_ids` does.
        "filter": filt,
        "hands": t.hands,
        # The hands behind the bb figures: complete, with a known big blind.
        "bb_hands": t.bb_n,
        "net_bb": r.get("net_bb"),
        "bb_per_100": r.get("bb_per_100"),
        # Its 95% half-width (SPEC.md, "Confidence intervals"); the chart's tile hover.
        "bb_per_100_ci": r.get("bb_per_100_ci"),
        # How often they put chips in, of the hands where they had a preflop
        # decision (`vpip_hands`; None when there are none), and how often they
        # saw a flop, of every hand. What separates "lost playing it" from "lost
        # the blind folding it".
        "vpip_pct": _pct(c["vpip"], c["vpip_opp"]),
        "vpip_hands": c["vpip_opp"],
        "saw_flop_pct": _pct(c["wtsd_opp"], t.hands),
        "wtsd": r.get("wtsd"),
        "wsd": r.get("wsd"),
        "worst": False,
    }
    if filt is None:
        row["hand_ids"] = [f.hand_id for f in facts]
    return row


def _pot_table(facts: list[Facts]) -> list[dict]:
    rows = []
    for key, label, filt, test in POT_TYPES:
        mine = [f for f in facts if test(f)]
        if not mine and key in ("bomb", "double", "plo"):  # most tables never deal one
            continue
        row = _row(key, label, filt, mine)
        if key in ("srp", "3bet", "4bet"):  # only a raised pot has a preflop aggressor
            row["roles"] = [
                _row(f"{key}:{rkey}", rlabel, f"{filt},{rfilt}", [f for f in mine if rtest(f)])
                for rkey, rlabel, rfilt, rtest in ROLES
            ]
        rows.append(row)
    return rows


def _seat(f: Facts) -> str | None:
    """The `position=` filter's own label for this hand, or None where it gives none.

    The filter, not `positional_report`, is the rule here: a row has to reproduce
    from its filter, and the filter labels a hand with a dead small blind
    (`blinds_irregular`) where the report leaves it out. See SPEC.md.
    """
    if f.seats_from_button is None or f.dead_button:
        return None
    return position_name(f.seats_from_button, f.n_dealt_in, f.position_slots)


def _position_table(facts: list[Facts]) -> list[dict]:
    by_seat: dict[str | None, list[Facts]] = {}
    for f in facts:
        by_seat.setdefault(_seat(f), []).append(f)
    order = {s: i for i, s in enumerate(SEAT_ORDER)}
    rows = [
        _row(seat, seat, f"position={seat}", by_seat[seat])
        for seat in sorted((s for s in by_seat if s is not None), key=lambda s: (order.get(s, len(order)), s))
    ]
    if None in by_seat:
        # Only a dead button leaves a hand without a seat label. Pooled across
        # table sizes, like the filter and `pnt positions`.
        rows.append(_row("unknown", "dead button", "dead_button", by_seat[None]))
    return rows


def _ip_table(facts: list[Facts]) -> list[dict]:
    """In position after the flop, out of it, and the hands that never got there.

    `in_position` is set on preflop folds as well, read from the seats; a fold has
    no position to play, so those hands are their own row rather than half of IP.
    """
    groups: dict[str, list[Facts]] = {"ip": [], "oop": [], "unknown": [], "no_flop": []}
    for f in facts:
        if not f.saw_flop:
            groups["no_flop"].append(f)
        elif f.in_position is None:
            groups["unknown"].append(f)
        else:
            groups["ip" if f.in_position else "oop"].append(f)
    rows = [
        _row("ip", "in position", None, groups["ip"]),
        _row("oop", "out of position", None, groups["oop"]),
    ]
    if groups["unknown"]:
        rows.append(_row("unknown", "saw flop, order unknown", None, groups["unknown"]))
    rows.append(_row("no_flop", "no flop", "!saw_flop", groups["no_flop"]))
    # Never the worst: a hand with no flop loses its blinds by nature, so its row
    # is below zero for everyone and says nothing about how the flop was played.
    rows[-1]["rank"] = False
    return rows


def _mark_worst(rows: Iterable[dict], min_hands: int) -> None:
    """Flag the lowest bb/100 among the rows that stand on `min_hands` results.

    A table's role rows are candidates alongside their pot type: the worst place
    to be is usually one side of a pot, not the whole of it. A tie marks the
    first, and a table where nothing is losing marks nothing.
    """
    flat = [x for r in rows for x in (r, *r.get("roles", ()))]
    ok = [r for r in flat
          if r.get("rank", True) and r["bb_hands"] >= min_hands and r["bb_per_100"] is not None]
    if not ok:
        return
    worst = min(ok, key=lambda r: r["bb_per_100"])
    if worst["bb_per_100"] < 0:
        worst["worst"] = True


def results_breakdown(facts: Iterable[Facts], min_hands: int = WORST_MIN_HANDS) -> dict:
    """One player's results split by pot type, by seat, and by position.

    `facts` is the player's hands in a spot, already filtered. Every table's rows
    partition them, so each table's `hands` and exact net add to `total`'s.
    """
    facts = list(facts)
    total = _row("total", "all hands", "", facts)
    tables = [
        {"key": "pot", "title": "By pot type", "rows": _pot_table(facts)},
        {"key": "position", "title": "By position", "rows": _position_table(facts)},
        {"key": "ip", "title": "In position after the flop", "rows": _ip_table(facts)},
    ]
    for t in tables:
        _mark_worst(t["rows"], min_hands)
    return {"hands": total["hands"], "total": total, "min_hands": min_hands, "tables": tables}
