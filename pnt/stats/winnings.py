"""Red line and blue line: where a player's big blinds came from, hand by hand.

The total winnings line split two ways on whether the player went to showdown:
the **blue** line sums the hands they took to showdown, the **red** line every
other hand -- the pots taken with a bet, the pots given up, the blinds folded.
The two add up to the total at every hand. A blue line that climbs while the red
one falls is a player who wins with cards and gives pots up without them.

The money is bb/100's money: each complete hand's net over its own big blind,
summed exactly. A hand whose log stops early, or that has no known big blind, is
counted among the hands and left out of every line, as it is left out of bb/100.

Optionally a third line: the total with each all-in showdown's `adjusted` net
(from `allin.allin_rows`) in place of what it actually paid -- the line the
all-in page draws alone. And one bar per session: the net bb of each game.

See SPEC.md, "Red and blue lines".
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from fractions import Fraction

from .allin import allin_hand_ids, allin_rows
from .derive import Facts

#: The filter terms that find each line's hands, to add to the spot.
RED_FILTER = "!wtsd"
BLUE_FILTER = "wtsd"


def _r(x: Fraction | float) -> float:
    return round(float(x), 2)


def _counted(f: Facts) -> bool:
    """A hand with a result to add: complete, with a known big blind."""
    return bool(f.complete and f.bb_size)


def allin_adjusted(conn: sqlite3.Connection, facts: Iterable[Facts]) -> dict:
    """Each all-in showdown's adjusted net among these hands, and what could not be.

    Returns ``{"by_hand": {(hand_id, pn_id): adjusted chips}, "skipped": n}``.
    `skipped` counts the hands this player took to an all-in showdown that have
    no adjusted figure -- a live hand was mucked, so no equity could be computed;
    those keep their actual net on the adjusted line.
    """
    facts = [f for f in facts if _counted(f)]
    ids = {f.hand_id for f in facts}
    rows, _, _ = allin_rows(conn, hand_ids=ids)
    mine = {(f.hand_id, f.pn_id) for f in facts}
    by_hand = {(r.hand_id, r.pn_id): r.adjusted for r in rows if (r.hand_id, r.pn_id) in mine}
    shown = set(allin_hand_ids(conn, hand_ids=ids))
    skipped = sum(
        1 for f in facts if f.wtsd and f.hand_id in shown and (f.hand_id, f.pn_id) not in by_hand
    )
    return {"by_hand": by_hand, "skipped": skipped}


def winnings_series(facts: Iterable[Facts], adjusted: Mapping | None = None) -> dict:
    """One player's cumulative net bb in hand order, split into red and blue.

    `facts` is one player's hands in a spot, oldest first (the order `facts_for`
    returns). `adjusted` is `allin_adjusted`'s result; without it the points carry
    no `adjusted` figure.

    Every point is one counted hand. `total`, `red` and `blue` are running sums
    of net / big blind, rounded from the exact sum, so ``red + blue == total`` to
    the rounding and the last `total` is `rates()["net_bb"]` for the same hands.
    """
    by_hand = adjusted["by_hand"] if adjusted is not None else None
    total = red = blue = Fraction(0)
    adj = 0.0
    hands = 0
    red_n = blue_n = red_bb_n = blue_bb_n = 0
    points: list[dict] = []
    sessions: dict[str, dict] = {}
    for f in facts:
        hands += 1
        s = sessions.get(f.game_id)
        if s is None:
            s = sessions[f.game_id] = {
                "game_id": f.game_id, "first_ts": f.ts, "last_ts": f.ts,
                "hands": 0, "bb_hands": 0, "_net": Fraction(0), "_red": Fraction(0),
                "_blue": Fraction(0), "hand_ids": [],
            }
        s["hands"] += 1
        s["hand_ids"].append(f.hand_id)
        if f.ts:
            s["first_ts"] = s["first_ts"] or f.ts
            s["last_ts"] = f.ts
        if f.wtsd:
            blue_n += 1
        else:
            red_n += 1
        if not _counted(f):
            continue
        x = Fraction(f.net, f.bb_size)
        total += x
        s["_net"] += x
        s["bb_hands"] += 1
        if f.wtsd:
            blue += x
            blue_bb_n += 1
            s["_blue"] += x
        else:
            red += x
            red_bb_n += 1
            s["_red"] += x
        point = {
            "hand_id": f.hand_id,
            "hand_number": f.hand_number,
            "ts": f.ts,
            "game_id": f.game_id,
            # This hand alone, for the hover caption, and which line it moved.
            "net_bb": _r(x),
            "sd": f.wtsd,
            "total": _r(total),
            "red": _r(red),
            "blue": _r(blue),
        }
        if by_hand is not None:
            a = by_hand.get((f.hand_id, f.pn_id))
            adj += float(x) if a is None else a / f.bb_size
            point["adjusted"] = round(adj, 2)
            point["allin"] = a is not None
        points.append(point)

    bb_n = len(points)
    totals = {
        "hands": hands,
        # The hands behind every line: complete, with a known big blind.
        "bb_hands": bb_n,
        "net_bb": _r(total) if bb_n else None,
        "bb_per_100": _r(100 * total / bb_n) if bb_n else None,
        "red": {"filter": RED_FILTER, "hands": red_n, "bb_hands": red_bb_n,
                "net_bb": _r(red) if bb_n else None},
        "blue": {"filter": BLUE_FILTER, "hands": blue_n, "bb_hands": blue_bb_n,
                 "net_bb": _r(blue) if bb_n else None},
    }
    if by_hand is not None:
        totals["adjusted"] = {
            "net_bb": round(adj, 2) if bb_n else None,
            "allin_hands": sum(1 for p in points if p["allin"]),
            "skipped": adjusted["skipped"],
        }
    out_sessions = []
    for s in sessions.values():
        n = s["bb_hands"]
        out_sessions.append({
            "game_id": s["game_id"],
            "first_ts": s["first_ts"],
            "last_ts": s["last_ts"],
            "hands": s["hands"],
            "bb_hands": n,
            "net_bb": _r(s["_net"]) if n else None,
            "bb_per_100": _r(100 * s["_net"] / n) if n else None,
            "red": _r(s["_red"]) if n else None,
            "blue": _r(s["_blue"]) if n else None,
            # No filter term names a game, so a session lists its hands by id.
            "hand_ids": s["hand_ids"],
        })
    return {"points": points, "totals": totals, "sessions": out_sessions}
