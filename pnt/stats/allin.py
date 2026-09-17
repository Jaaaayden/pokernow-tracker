"""All-in EV: what a player's jams were worth against what they paid.

Every all-in showdown where every live hand is known gets one row per live
player: their equity when the betting stopped, the chips that equity was worth,
and the chips they actually took. Summed per player that is the classic pair of
lines -- *actual* and *all-in adjusted* -- and the gap between them is the part
of a result that was the deck's doing.

Definitions are in SPEC.md, "All-in EV". The short form:

- the population is complete hands that reached showdown with an all-in action
  and every unfolded player's cards known; a mucked live hand is *counted* as
  skipped, never silently dropped;
- the decision point is the street of the last voluntary action, and equity is
  computed from the board dealt by then;
- pots are layered from `contributed`, so side pots pay only whoever reached them;
- ``adjusted = expected - contributed + bounty`` beside the usual
  ``actual = collected - contributed + bounty``, and ``diff = actual - adjusted``.

Equities are memoized in ``equity_cache`` keyed by cards and pot layout, so the
half-second a sampled preflop all-in costs is paid once per hand, ever.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Collection
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from ..db.conn import writing
from ..logfmt.events import FLOP, PREFLOP, RIVER, TURN
from .cards import board_at
from .derive import Facts, HandRow, derive
from .equity import cache_key, expected_collected, side_pots
from .queries import display_names, identities_of, identity_map, load_hands

STREETS = (PREFLOP, FLOP, TURN, RIVER)


@dataclass(slots=True)
class AllInRow:
    """One live player in one all-in showdown."""

    hand_id: int
    game_id: str
    hand_number: int
    ts: str | None
    pn_id: str
    #: Where the betting stopped: the street of the last voluntary action.
    street: str
    hole_cards: str
    #: The board dealt by the decision point, and the whole first run.
    board: list[str]
    full_board: list[str]
    run_count: int
    #: The other live players and their cards, in seat order.
    villains: list[tuple[str, str]]
    pot: int
    contributed: int
    collected: int
    bounty: int
    bb: int | None
    #: Share of the whole pot, 0..1. Sums to 1 over the live players.
    equity: float
    #: Chips this equity was worth, over every pot they were eligible for.
    expected: float
    actual: int
    adjusted: float
    #: actual - adjusted: positive ran above expectation.
    diff: float
    method: str
    n: int


def allin_hand_ids(
    conn: sqlite3.Connection,
    game_id: str | None = None,
    hand_ids: Collection[int] | None = None,
) -> list[int]:
    """Complete hands that reached showdown with someone all in.

    `hand_ids` narrows the search to those hands, so a caller that only wants one
    player's all-ins never pays for everyone else's equities.
    """
    out: list[int] = []
    wanted = None if hand_ids is None else list(hand_ids)
    if wanted is not None and not wanted:
        return out
    chunks = [None] if wanted is None else [wanted[i : i + 500] for i in range(0, len(wanted), 500)]
    for chunk in chunks:
        clause, params = "", []
        if game_id:
            clause += " AND h.game_id = ?"
            params.append(game_id)
        if chunk is not None:
            clause += f" AND h.hand_id IN ({','.join('?' * len(chunk))})"
            params.extend(chunk)
        out.extend(
            r[0]
            for r in conn.execute(
                "SELECT h.hand_id FROM hands h WHERE h.complete = 1 AND h.went_to_showdown = 1"
                " AND EXISTS (SELECT 1 FROM actions a WHERE a.hand_id = h.hand_id AND a.all_in = 1)"
                f"{clause} ORDER BY h.ord",
                params,
            )
        )
    return out


def decision_street(hand: HandRow) -> str:
    """The street of the last voluntary action -- where the betting stopped."""
    street = PREFLOP
    for a in hand.actions:
        if not a.is_forced:
            street = a.street
    return street


def decision_board(hand: HandRow) -> tuple[str, ...]:
    """The first run's cards dealt by the time the betting stopped."""
    return board_at(hand.board, decision_street(hand)) or ()


def _cached(conn: sqlite3.Connection, keys: Collection[str]) -> dict[str, tuple[list[float], str, int]]:
    out = {}
    keys = list(keys)
    for i in range(0, len(keys), 500):
        chunk = keys[i : i + 500]
        for r in conn.execute(
            f"SELECT key, expected, method, n FROM equity_cache WHERE key IN ({','.join('?' * len(chunk))})",
            chunk,
        ):
            out[r["key"]] = (json.loads(r["expected"]), r["method"], r["n"])
    return out


def allin_rows(
    conn: sqlite3.Connection,
    game_id: str | None = None,
    hand_ids: Collection[int] | None = None,
) -> tuple[list[AllInRow], dict[str, int], dict[int, HandRow]]:
    """Every all-in showdown row, oldest hand first, plus what was skipped and why.

    Also returns the hands themselves, keyed by id, so a caller that wants to
    filter on derived facts can derive exactly these and no others. `hand_ids`
    narrows the population, as in `allin_hand_ids`.
    """
    hands = load_hands(conn, game_id, hand_ids=allin_hand_ids(conn, game_id, hand_ids))
    skipped = {"cards_unknown": 0}

    # First pass: what each hand needs computed, so the cache is read once and the
    # misses are written once.
    jobs: list[tuple[HandRow, list[str], list[str], list[tuple[int, tuple[int, ...]]], str]] = []
    for hand in hands:
        live = [
            p for p in sorted(hand.players.values(), key=lambda p: p.seat) if not p.folded
        ]
        if len(live) < 2:
            continue
        if any(not p.hole_cards for p in live):
            skipped["cards_unknown"] += 1
            continue
        contributed = {pid: p.contributed for pid, p in hand.players.items()}
        pots = side_pots(contributed, [p.pn_id for p in live])
        index = {p.pn_id: i for i, p in enumerate(live)}
        pot_idx = [(chips, tuple(index[q] for q in elig)) for chips, elig in pots]
        cards = [p.hole_cards for p in live]
        board = list(decision_board(hand))
        jobs.append((hand, [p.pn_id for p in live], cards, pot_idx, cache_key(cards, board, pot_idx)))

    cached = _cached(conn, {j[4] for j in jobs})
    fresh: dict[str, tuple[list[float], str, int]] = {}
    for hand, _, cards, pot_idx, key in jobs:
        if key in cached or key in fresh:
            continue
        fresh[key] = expected_collected(cards, list(decision_board(hand)), pot_idx)
    if fresh:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with writing(conn):
            conn.executemany(
                "INSERT OR IGNORE INTO equity_cache (key, expected, method, n, computed_at)"
                " VALUES (?, ?, ?, ?, ?)",
                [(k, json.dumps(e), m, n, now) for k, (e, m, n) in fresh.items()],
            )
        cached.update(fresh)

    rows: list[AllInRow] = []
    for hand, live_ids, cards, _, key in jobs:
        expected, method, n = cached[key]
        pot = sum(p.contributed for p in hand.players.values())
        street = decision_street(hand)
        board = list(decision_board(hand))
        for i, pid in enumerate(live_ids):
            p = hand.players[pid]
            actual = p.collected - p.contributed + p.bounty
            adjusted = expected[i] - p.contributed + p.bounty
            rows.append(
                AllInRow(
                    hand_id=hand.hand_id,
                    game_id=hand.game_id,
                    hand_number=hand.hand_number,
                    ts=hand.ts,
                    pn_id=pid,
                    street=street,
                    hole_cards=cards[i],
                    board=board,
                    full_board=list(hand.board),
                    run_count=hand.run_count,
                    villains=[(q, cards[j]) for j, q in enumerate(live_ids) if q != pid],
                    pot=pot,
                    contributed=p.contributed,
                    collected=p.collected,
                    bounty=p.bounty,
                    bb=hand.bb,
                    equity=round(expected[i] / pot, 4) if pot else 0.0,
                    expected=round(expected[i], 2),
                    actual=actual,
                    adjusted=round(adjusted, 2),
                    diff=round(actual - adjusted, 2),
                    method=method,
                    n=n,
                )
            )
    return rows, skipped, {h.hand_id: h for h in hands}


def _apply(
    rows: list[AllInRow], hands: dict[int, HandRow], predicate: Callable[[Facts], bool] | None
) -> list[AllInRow]:
    """Keep the rows whose (hand, player) Facts satisfy the spot filter."""
    if predicate is None:
        return rows
    facts: dict[tuple[int, str], Facts] = {}
    for hid in {r.hand_id for r in rows}:
        for f in derive(hands[hid]):
            facts[(hid, f.pn_id)] = f
    return [r for r in rows if predicate(facts[(r.hand_id, r.pn_id)])]


def _bb(chips: float, bb: int | None) -> float | None:
    return round(chips / bb, 2) if bb else None


def _summary(rows: list[AllInRow]) -> dict:
    net = sum(r.actual for r in rows)
    adjusted = sum(r.adjusted for r in rows)
    scaled = [(r.actual / r.bb, r.adjusted / r.bb) for r in rows if r.bb]
    net_bb = sum(a for a, _ in scaled)
    adj_bb = sum(b for _, b in scaled)
    return {
        "hands": len(rows),
        "net": net,
        "adjusted": round(adjusted, 2),
        "diff": round(net - adjusted, 2),
        "net_bb": round(net_bb, 2) if scaled else None,
        "adjusted_bb": round(adj_bb, 2) if scaled else None,
        "diff_bb": round(net_bb - adj_bb, 2) if scaled else None,
    }


def allin_report(
    conn: sqlite3.Connection,
    game_id: str | None = None,
    min_hands: int = 1,
    predicate: Callable[[Facts], bool] | None = None,
) -> list[dict]:
    """Per player: all-in showdowns, actual and adjusted net, and the gap, by street.

    `skipped` rides on every row so a page needs no second request for it.
    """
    rows, skipped, hands = allin_rows(conn, game_id)
    rows = _apply(rows, hands, predicate)
    ident = identity_map(conn)
    grouped: dict[int, list[AllInRow]] = defaultdict(list)
    aliases: dict[int, str] = {}
    for r in rows:
        pid, alias = ident.get(r.pn_id, (-1, r.pn_id))
        grouped[pid].append(r)
        aliases[pid] = alias

    out = []
    for pid, mine in grouped.items():
        if len(mine) < min_hands:
            continue
        out.append(
            {
                "player": aliases[pid],
                **_summary(mine),
                "sampled": sum(1 for r in mine if r.method == "sampled"),
                "equity_avg": round(100.0 * sum(r.equity for r in mine) / len(mine), 1),
                "by_street": {
                    s: _summary([r for r in mine if r.street == s]) for s in STREETS
                },
                "skipped": skipped,
            }
        )
    out.sort(key=lambda r: -r["hands"])
    return out


def allin_hand_list(
    conn: sqlite3.Connection,
    alias: str,
    game_id: str | None = None,
    predicate: Callable[[Facts], bool] | None = None,
) -> dict:
    """One player's all-in rows, oldest first -- the order a cumulative graph reads.

    Raises ValueError on an unknown alias, as `facts_for` does.
    """
    ids = set(identities_of(conn, alias))
    rows, skipped, hands = allin_rows(conn, game_id)
    rows = _apply([r for r in rows if r.pn_id in ids], hands, predicate)
    names = display_names(conn)
    out = []
    for r in rows:
        d = asdict(r)
        d["villains"] = [{"player": names.get(q, q), "cards": c} for q, c in r.villains]
        d["actual_bb"] = _bb(r.actual, r.bb)
        d["adjusted_bb"] = _bb(r.adjusted, r.bb)
        d["diff_bb"] = _bb(r.diff, r.bb)
        out.append(d)
    return {"player": alias, "skipped": skipped, **_summary(rows), "hands": out}
