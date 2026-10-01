"""The hand in progress, straight from the raw log.

Live capture lands each log line in `raw_entries` within seconds, but a hand is
only derived into `hands` once its ending line arrives. Everything here reads the
raw lines of the newest hand instead and runs them through the *same* parser, which
already returns a hand that has not ended as `complete=False`. No second parser, no
state kept between polls: the answer is recomputed from the log every time.
"""

from __future__ import annotations

import sqlite3

from ..ingest.csv_source import RawEntry
from ..logfmt import events as E
from ..logfmt.parser import DEAD_POSTS, ParsedHand, parse, position_name
from .derive import HandAction, HandPlayerRow, HandRow
from .nodes import nodes_for, resolve, street_of_board
from .queries import facts_cached, identity_map
from .review import is_flagged, last_ended_hand


def live_entries(conn: sqlite3.Connection, game_id: str) -> list[RawEntry]:
    """The raw lines from the newest hand-start line onward."""
    row = conn.execute(
        "SELECT MAX(ord) AS o FROM raw_entries WHERE game_id = ?"
        " AND entry LIKE '-- starting hand #%'",
        (game_id,),
    ).fetchone()
    if row is None or row["o"] is None:
        return []
    return [
        RawEntry(ord=r["ord"], at=r["at"], entry=r["entry"])
        for r in conn.execute(
            "SELECT ord, at, entry FROM raw_entries WHERE game_id = ? AND ord >= ? ORDER BY ord",
            (game_id, row["o"]),
        )
    ]


def live_hand(conn: sqlite3.Connection, game_id: str) -> ParsedHand | None:
    """The hand in progress, or None between hands (or before the first)."""
    entries = live_entries(conn, game_id)
    if not entries:
        return None
    result = parse(entries, game_id)
    if not result.hands:
        return None
    hand = result.hands[-1]
    if hand.complete or not hand.players:
        return None
    return hand


def hand_row(parsed: ParsedHand, hand_id: int = 0) -> HandRow:
    """A parsed hand as the derive layer's row -- the same shape `load_hands` builds
    from the database, so `derive()` and `nodes_for()` see no difference."""
    board = tuple(parsed.board_runs[0]) if parsed.board_runs else ()
    return HandRow(
        hand_id=hand_id,
        game_id=parsed.game_id,
        hand_number=parsed.hand_number,
        n_dealt_in=parsed.n_dealt_in,
        dead_button=parsed.dead_button,
        blinds_irregular=parsed.blinds_irregular,
        went_to_showdown=parsed.went_to_showdown,
        saw_flop=parsed.saw_street(E.FLOP),
        complete=parsed.complete,
        bb=parsed.bb,
        ts=parsed.ts,
        players={
            pid: HandPlayerRow(
                pn_id=pid,
                seat=p.seat,
                seats_from_button=p.seats_from_button,
                contributed=p.contributed,
                collected=p.collected,
                folded=p.folded,
                hole_cards="".join(p.hole_cards) or None,
                bounty=p.bounty,
                starting_stack=p.starting_stack,
            )
            for pid, p in parsed.players.items()
        },
        actions=[
            HandAction(
                seq=a.seq,
                street=a.street,
                pn_id=a.pn_id,
                kind=a.kind,
                amount=a.amount,
                is_forced=a.is_forced,
                all_in=a.all_in,
                amount_to=a.amount_to,
            )
            for a in parsed.actions
        ],
        board=board,
        run_count=parsed.run_count,
    )


def current_street(parsed: ParsedHand) -> str:
    return street_of_board(parsed.board_runs[0] if parsed.board_runs else ())


def street_committed(parsed: ParsedHand, street: str) -> dict[str, int]:
    """Each player's live commitment on a street: what a call has to match.

    Dead posts (antes, a dead small blind) are real chips that buy no part of the
    street's bet, so they are left out, exactly as the parser leaves them out of
    `_committed`.
    """
    out: dict[str, int] = {}
    for a in parsed.actions:
        if a.street == street and a.post_kind not in DEAD_POSTS:
            out[a.pn_id] = out.get(a.pn_id, 0) + a.amount
    return out


def to_act(parsed: ParsedHand) -> str | None:
    """Who the action is on, or None when that cannot be said with confidence.

    None also stands for "nobody": the street is closed and the next card is due,
    everyone but one has folded, or every live player is all in. Guessing wrong
    would point the HUD at the wrong player's spot, so any doubt is None.
    """
    players = parsed.players
    unfolded = [p for p in players.values() if not p.folded]
    if len(unfolded) < 2:
        return None
    all_in = {a.pn_id for a in parsed.actions if a.all_in}
    live = [p for p in unfolded if p.pn_id not in all_in]
    if not live or any(p.seats_from_button is None for p in live):
        return None

    street = current_street(parsed)
    slots = max(
        [parsed.n_dealt_in, *((p.seats_from_button or 0) + 1 for p in players.values())]
    )
    if street == E.PREFLOP:
        # First to act sits after the big blind, or after the last straddle.
        ref = 1 if parsed.n_dealt_in == 2 else 2
        for a in parsed.actions:
            if a.post_kind == E.POST_STRADDLE and a.pn_id in players:
                sfb = players[a.pn_id].seats_from_button
                if sfb is not None:
                    ref = sfb

        def rank(p) -> int:
            return (p.seats_from_button - ref - 1) % slots
    else:

        def rank(p) -> int:
            return (p.seats_from_button - 1) % slots

    ordered = sorted(live, key=rank)
    voluntary = [a for a in parsed.actions if a.street == street and not a.is_forced]
    if not voluntary:
        return ordered[0].pn_id
    last = players.get(voluntary[-1].pn_id)
    if last is None or last.seats_from_button is None:
        return None
    last_rank = rank(last)
    committed = street_committed(parsed, street)
    target = max(committed.get(p.pn_id, 0) for p in unfolded)
    acted = {a.pn_id for a in voluntary}
    around = [p for p in ordered if rank(p) > last_rank] + [p for p in ordered if rank(p) < last_rank]
    for p in around:
        if p.pn_id not in acted or committed.get(p.pn_id, 0) < target:
            return p.pn_id
    return None


def previous_hand(conn: sqlite3.Connection, game_id: str) -> dict | None:
    """The last hand to end in a game, as {"hand_number", "flagged"}, or None."""
    n = last_ended_hand(conn, game_id)
    return None if n is None else {"hand_number": n, "flagged": is_flagged(conn, game_id, n)}


def snapshot(conn: sqlite3.Connection, game_id: str, min_hands: int = 1, min_known: int = 5) -> dict:
    """The `/live/{game}` payload: the hand in progress, everyone's node, and the
    closest spot with data behind it for everyone still in.

    `min_known` is the shown hands a spot narrowed to this board's texture must
    keep to stay narrowed; see `nodes.resolve`.

    `previous` is the newest hand whose ending line has arrived -- the one the
    HUD's 🚩 flags for manual review -- and whether it is flagged already, or None
    before the first hand ends. It is there between hands too."""
    parsed = live_hand(conn, game_id)
    if parsed is None:
        return {"game_id": game_id, "hand": None, "previous": previous_hand(conn, game_id)}

    row = hand_row(parsed)
    street = current_street(parsed)
    who = to_act(parsed)
    paths = nodes_for(row, pending_for=who)
    ident = identity_map(conn)
    labelled = not parsed.dead_button and not parsed.blinds_irregular
    all_in = {a.pn_id for a in parsed.actions if a.all_in}
    acted_street = {a.pn_id for a in parsed.actions if a.street == street and not a.is_forced}

    hero = None
    hero_row = conn.execute("SELECT hero_pn_id FROM games WHERE game_id = ?", (game_id,)).fetchone()
    if hero_row and hero_row["hero_pn_id"] in parsed.players and parsed.hero_cards:
        hero = {"pn_id": hero_row["hero_pn_id"], "cards": list(parsed.hero_cards)}

    players = []
    for pid, p in sorted(parsed.players.items(), key=lambda kv: kv[1].seat):
        alias = ident.get(pid, (None, None))[1]
        path = paths.get(pid, [])
        node = path[-1] if path else None
        resolved = None
        if alias and not p.folded and path:
            resolved = resolve(
                facts_cached(conn, alias), path, min_hands=min_hands, board=row.board, min_known=min_known,
            )
        players.append(
            {
                "pn_id": pid,
                "alias": alias,
                "name": p.name,
                "seat": p.seat,
                "position": (
                    position_name(p.seats_from_button, parsed.n_dealt_in)
                    if labelled and p.seats_from_button is not None
                    else None
                ),
                "stack": p.starting_stack - p.committed,
                "committed": p.committed,
                "folded": p.folded,
                "all_in": pid in all_in,
                "acted_street": pid in acted_street,
                "node": node.as_dict() if node else None,
                "path": [
                    f"{n.label} → {n.decision}" if n.decision else n.label for n in path
                ],
                "resolved": resolved,
            }
        )

    return {
        "game_id": game_id,
        "hand_number": parsed.hand_number,
        "street": street,
        "board": list(parsed.board_runs[0]) if parsed.board_runs else [],
        "bb": parsed.bb,
        "pot": parsed.total_contributed,
        "n_dealt_in": parsed.n_dealt_in,
        "positions_known": labelled,
        "to_act": who,
        "hero": hero,
        "players": players,
        "previous": previous_hand(conn, game_id),
    }
