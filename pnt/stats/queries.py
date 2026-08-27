"""Load hands from SQLite, derive facts, aggregate.

Deliberately reads raw rows and derives in Python rather than expressing the
opportunity logic as SQL window functions. The bet-level walk and the c-bet
opportunity rules are sequential reasoning about a hand; written in SQL they are
correct-looking and unreviewable, and SPEC.md could no longer be checked against
them by eye.

If this ever gets slow, the escape hatch is the one the design already allows:
materialize `Facts` into a rollup table marked derived and disposable, rebuilt
from `actions`. Never make it the source of truth.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable

from ..logfmt.parser import position_name
from .derive import Facts, HandAction, HandPlayerRow, HandRow, derive


def load_hands(
    conn: sqlite3.Connection, game_id: str | None = None
) -> list[HandRow]:
    """Load every hand (optionally one game) with its roster and actions."""
    where, params = ("WHERE h.game_id = ?", (game_id,)) if game_id else ("", ())

    hands: dict[int, HandRow] = {}
    for r in conn.execute(
        f"""SELECT h.hand_id, h.game_id, h.hand_number, h.n_dealt_in, h.dead_button,
                   h.blinds_irregular, h.went_to_showdown, h.board_json, h.ts,
                   COALESCE(h.bb, g.bb) AS bb
            FROM hands h LEFT JOIN games g ON g.game_id = h.game_id
            {where} ORDER BY h.ord""",
        params,
    ):
        runs = json.loads(r["board_json"] or "[]")
        # "saw a flop" == the first run has at least three cards
        saw_flop = bool(runs) and len(runs[0]) >= 3
        hands[r["hand_id"]] = HandRow(
            hand_id=r["hand_id"],
            game_id=r["game_id"],
            hand_number=r["hand_number"],
            n_dealt_in=r["n_dealt_in"],
            dead_button=bool(r["dead_button"]),
            blinds_irregular=bool(r["blinds_irregular"]),
            went_to_showdown=bool(r["went_to_showdown"]),
            saw_flop=saw_flop,
            bb=r["bb"],
            ts=r["ts"],
            players={},
            actions=[],
        )

    for r in conn.execute(
        f"""SELECT hp.* FROM hand_players hp JOIN hands h ON h.hand_id = hp.hand_id {where}""",
        params,
    ):
        h = hands.get(r["hand_id"])
        if h is not None:
            h.players[r["pn_id"]] = HandPlayerRow(
                pn_id=r["pn_id"],
                seat=r["seat"],
                seats_from_button=r["seats_from_button"],
                contributed=r["contributed"],
                collected=r["collected"],
                folded=bool(r["folded"]),
                hole_cards=r["hole_cards"],
            )

    for r in conn.execute(
        f"""SELECT a.* FROM actions a JOIN hands h ON h.hand_id = a.hand_id {where}
            ORDER BY a.hand_id, a.seq""",
        params,
    ):
        h = hands.get(r["hand_id"])
        if h is not None:
            h.actions.append(
                HandAction(
                    seq=r["seq"],
                    street=r["street"],
                    pn_id=r["pn_id"],
                    kind=r["action_type"],
                    amount=r["amount"],
                    is_forced=bool(r["is_forced"]),
                    all_in=bool(r["all_in"]),
                )
            )

    return list(hands.values())


def identity_map(conn: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    """pn_id -> (player_id, alias). Collapses multi-device identities into one person."""
    return {
        r["pn_id"]: (r["player_id"], r["alias"])
        for r in conn.execute(
            "SELECT pi.pn_id, pi.player_id, p.alias FROM player_identities pi"
            " JOIN players p ON p.player_id = pi.player_id"
        )
    }


def _rate(num: int, den: int) -> float | None:
    return round(100.0 * num / den, 1) if den else None


def aggregate(facts: Iterable[Facts]) -> dict:
    """Collapse Facts rows into the v1 stat set.

    Every rate returns None -- not 0 -- when the denominator is empty. A player
    with no 3-bet opportunities has an *unknown* 3-bet percentage, and showing 0%
    would be a claim we cannot support.
    """
    f = list(facts)
    if not f:
        return {"hands": 0}

    def s(attr: str) -> int:
        return sum(1 for x in f if getattr(x, attr))

    def sd(attr: str, street: str) -> int:
        return sum(1 for x in f if getattr(x, attr).get(street))

    def counter(attr: str, street: str) -> int:
        return sum(getattr(x, attr).get(street, 0) for x in f)

    net = sum(x.net for x in f)
    # Normalize each hand by *its own* big blind. Blind levels move within a game,
    # so dividing a whole session's net by one bb silently rescales history.
    scaled = [x.net / x.bb_size for x in f if x.bb_size]

    out = {
        "hands": len(f),
        "vpip": _rate(s("vpip"), s("vpip_opp")),
        "pfr": _rate(s("pfr"), s("pfr_opp")),
        "3bet": _rate(s("three_bet"), s("three_bet_opp")),
        "fold_to_3bet": _rate(s("fold_to_3bet"), s("fold_to_3bet_opp")),
        "wtsd": _rate(s("wtsd"), s("wtsd_opp")),
        "wsd": _rate(s("wsd"), s("wtsd")),
        "net": net,
        "bb_per_100": round(100.0 * sum(scaled) / len(scaled), 2) if scaled else None,
        "_opp": {
            "vpip": s("vpip_opp"),
            "3bet": s("three_bet_opp"),
            "fold_to_3bet": s("fold_to_3bet_opp"),
            "wtsd": s("wtsd_opp"),
        },
    }
    for street in ("flop", "turn", "river"):
        out[f"cbet_{street}"] = _rate(sd("cbet", street), sd("cbet_opp", street))
        out[f"fold_to_cbet_{street}"] = _rate(
            sd("fold_to_cbet", street), sd("fold_to_cbet_opp", street)
        )
        out[f"af_{street}"] = _rate(counter("aggressive", street), counter("agg_denom", street))
    return out


def facts_by_player(
    conn: sqlite3.Connection, game_id: str | None = None
) -> tuple[dict[int, list[Facts]], dict[int, str], list[HandRow]]:
    """Derive every hand and group the resulting facts by canonical player."""
    hands = load_hands(conn, game_id)
    ident = identity_map(conn)
    grouped: dict[int, list[Facts]] = defaultdict(list)
    aliases: dict[int, str] = {}
    for hand in hands:
        for fact in derive(hand):
            pid, alias = ident.get(fact.pn_id, (-1, fact.pn_id))
            grouped[pid].append(fact)
            aliases[pid] = alias
    return grouped, aliases, hands


def report(
    conn: sqlite3.Connection,
    game_id: str | None = None,
    min_hands: int = 1,
    predicate=None,
) -> list[dict]:
    """Stats per player, most hands first.

    `predicate` is an optional `Facts -> bool` filter -- this is the Holdem-Manager
    move: restrict to the hands where a player reached some specific spot, then
    report their behaviour within it.
    """
    grouped, aliases, _ = facts_by_player(conn, game_id)
    rows = []
    for pid, facts in grouped.items():
        if predicate is not None:
            facts = [f for f in facts if predicate(f)]
        if len(facts) < min_hands:
            continue
        rows.append({"player": aliases.get(pid, "?"), **aggregate(facts)})
    rows.sort(key=lambda r: -r["hands"])
    return rows


def positional_report(conn: sqlite3.Connection, alias: str, pool: bool = True) -> list[dict]:
    """Break one player's stats down by position.

    Table size is *pooled at query time* from raw `n_dealt_in`, never pre-bucketed
    in storage -- pooling later is always possible, unpooling is not.
    """
    grouped, aliases, _ = facts_by_player(conn)
    pid = next((p for p, a in aliases.items() if a == alias), None)
    if pid is None:
        raise ValueError(f"unknown alias: {alias!r}")

    buckets: dict[str, list[Facts]] = defaultdict(list)
    for f in grouped[pid]:
        if f.seats_from_button is None or f.dead_button or f.blinds_irregular:
            # Positions are best-effort when the button or a blind is dead, so
            # they are left out of positional splits entirely. See SPEC.md.
            continue
        key = position_name(f.seats_from_button, f.n_dealt_in)
        if not pool:
            key = f"{key} ({f.n_dealt_in}-handed)"
        buckets[key].append(f)

    return sorted(
        ({"position": k, **aggregate(v)} for k, v in buckets.items()),
        key=lambda r: -r["hands"],
    )
