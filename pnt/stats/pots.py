"""Biggest pots: the hands worth remembering, over a window of days.

The one view here that asks nothing about a player. Every other page starts
"for this player, in this spot"; this one starts with the table -- the pots that
actually mattered in the last week, whoever was in them -- and only then says who
won and who paid. Pass `player` to narrow it to the hands one person was dealt
into, which is the same list seen from one seat rather than a different question.

Two knobs, both defaulted here and mirrored in SPEC.md, "Biggest pots":

``days``     how far back the window reaches, from now. None is all of history.
``min_pot``  the chips a pot must reach to be listed at all.

The pot is `SUM(hand_players.contributed)`, which is `derive`'s `Facts.pot`
line for line -- the same definition, evaluated in SQL so that finding the
biggest pots in 9,000 hands does not mean deriving 9,000 hands. Only the handful
that clear the bar are loaded and derived, for the seat-by-seat figures.

Nothing is stored: the window is measured from the clock at request time, so the
same URL means "the last seven days" tomorrow too.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from .cards import street_of_board
from .derive import Facts, HandPlayerRow, derive
from .queries import display_names, identities_of, load_hands

#: The window and the bar the pages and the CLI start from.
DEFAULT_DAYS = 7
DEFAULT_MIN_POT = 2000
#: How many pots one answer carries. The summary still counts every pot that
#: cleared the bar, so a capped list says so instead of quietly shortening.
DEFAULT_LIMIT = 50


def cutoff(days: float, now: datetime | None = None) -> str:
    """The timestamp a hand must reach to be inside a window of `days`.

    Formatted exactly as the parser stores `hands.ts` -- ISO-8601 UTC, to the
    millisecond, with the `Z` -- because the comparison is a string comparison.
    SQLite's own `datetime('now', '-7 days')` is *not* usable here: it renders
    `2026-09-09 07:45:01`, a space where every stored timestamp has a `T`. A
    space sorts *before* `T`, so every hand dealt on the cutoff's own date
    compares as later than the cutoff and leaks into the window however early it
    was -- almost a day of stale hands, on the one boundary nobody would check.
    """
    at = (now or datetime.now(UTC)) - timedelta(days=days)
    return f"{at.strftime('%Y-%m-%dT%H:%M:%S')}.{at.microsecond // 1000:03d}Z"


def _window(
    conn: sqlite3.Connection,
    since: str | None,
    game_id: str | None,
    pn_ids: list[str] | None,
) -> tuple[str, list]:
    """The SQL selecting (hand_id, pot, ord) for every hand in the window."""
    clauses, params = [], []
    if since is not None:
        clauses.append("h.ts >= ?")
        params.append(since)
    if game_id:
        clauses.append("h.game_id = ?")
        params.append(game_id)
    if pn_ids is not None:
        marks = ",".join("?" * len(pn_ids))
        clauses.append(f"hp.hand_id IN (SELECT hand_id FROM hand_players WHERE pn_id IN ({marks}))")
        params += pn_ids
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT hp.hand_id AS hand_id, SUM(hp.contributed) AS pot, MAX(h.ord) AS ord"
        " FROM hand_players hp JOIN hands h ON h.hand_id = hp.hand_id"
        f"{where} GROUP BY hp.hand_id"
    )
    return sql, params


def _seat(f: Facts, p: HandPlayerRow, name: str) -> dict:
    """One player's side of the pot: the money from `Facts`, the rest from the seat.

    `net` is taken from `derive` rather than recomputed here, because the bounty
    belongs to the player but never to the pot and that is the one place the rule
    is written down.
    """
    return {
        "player": name,
        "pn_id": f.pn_id,
        "net": f.net,
        "net_bb": round(f.net / f.bb_size, 1) if f.bb_size else None,
        "hole_cards": p.hole_cards,
        "folded": p.folded,
        "contributed": p.contributed,
        "collected": p.collected,
        "bounty": p.bounty,
    }


def big_pots(
    conn: sqlite3.Connection,
    days: float | None = DEFAULT_DAYS,
    min_pot: int = DEFAULT_MIN_POT,
    game_id: str | None = None,
    player: str | None = None,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> dict:
    """The biggest pots in the window, largest first.

    `days` of None reaches back over everything. `now` is for tests, which cannot
    wait a day to watch a hand leave the window. Raises ValueError on an unknown
    `player`, as the player pages do.
    """
    since = None if days is None else cutoff(days, now)
    pn_ids = None if player is None else list(identities_of(conn, player))
    if pn_ids is not None and not pn_ids:
        pn_ids = ["\x00none"]  # a known player with no identities matches no hand
    sql, params = _window(conn, since, game_id, pn_ids)

    # One pass for the shape of the window: how many hands are in it, how many
    # cleared the bar, and what the biggest was -- so a list capped at `limit`
    # can still say what it is a slice of.
    summary = conn.execute(
        "SELECT COUNT(*) AS hands, COALESCE(SUM(pot >= ?), 0) AS over,"
        " COALESCE(MAX(pot), 0) AS biggest,"
        " COALESCE(SUM(CASE WHEN pot >= ? THEN pot ELSE 0 END), 0) AS chips"
        f" FROM ({sql})",
        [min_pot, min_pot, *params],
    ).fetchone()

    picked = conn.execute(
        f"SELECT hand_id, pot, ord FROM ({sql}) WHERE pot >= ? ORDER BY pot DESC, ord DESC LIMIT ?",
        [*params, min_pot, limit],
    ).fetchall()

    names = display_names(conn)
    # `ord` rides along so the rows are ordered by exactly the key the LIMIT cut
    # them on. Sorting the answer any other way would let the list disagree with
    # the slice it came from -- the 50th pot shown not being the 50th pot chosen.
    by_id = {r["hand_id"]: (r["pot"], r["ord"]) for r in picked}
    rows = []
    for hand in load_hands(conn, hand_ids=list(by_id)):
        pot, ord_ = by_id[hand.hand_id]
        facts = derive(hand)
        seats = sorted(
            (_seat(f, hand.players[f.pn_id], names.get(f.pn_id, f.pn_id)) for f in facts),
            key=lambda s: s["net"],
            reverse=True,
        )
        bb = hand.bb
        won = seats[0] if seats and seats[0]["net"] > 0 else None
        lost = seats[-1] if seats and seats[-1]["net"] < 0 else None
        rows.append(
            {
                "hand_id": hand.hand_id,
                "game_id": hand.game_id,
                "hand_number": hand.hand_number,
                "ts": hand.ts,
                "pot": pot,
                "bb": bb,
                "pot_bb": round(pot / bb, 1) if bb else None,
                "players": hand.n_dealt_in,
                "board": list(hand.board),
                "run_count": hand.run_count,
                "street": street_of_board(hand.board),
                "went_to_showdown": hand.went_to_showdown,
                # A log that stopped mid-hand has only some of its chips on
                # record, so its pot is short. Said out loud rather than dropped.
                "complete": hand.complete,
                "all_in": any(a.all_in for a in hand.actions),
                # Two or more players collected, so "winner +13" would be a lie
                # about a pot everyone mostly got back. A split pot, or a
                # run-it-twice the players shared one board each.
                "chopped": sum(1 for s in seats if s["collected"] > 0) > 1,
                "winner": won["player"] if won else None,
                "won": won["net"] if won else None,
                "won_bb": won["net_bb"] if won else None,
                "loser": lost["player"] if lost else None,
                "lost": lost["net"] if lost else None,
                "seats": seats,
                "_ord": ord_,
            }
        )
    rows.sort(key=lambda r: (r["pot"], r["_ord"]), reverse=True)
    for r in rows:
        del r["_ord"]

    return {
        "days": days,
        "since": since,
        "min_pot": min_pot,
        "player": player,
        "game_id": game_id,
        #: Hands in the window, whatever their size: the denominator.
        "hands": summary["hands"],
        #: How many cleared the bar -- more than `len(pots)` when the list is capped.
        "over": summary["over"],
        "biggest": summary["biggest"],
        #: Chips that passed through those pots, added up.
        "chips": summary["chips"],
        "limit": limit,
        "pots": rows,
    }
