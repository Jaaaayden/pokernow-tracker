"""Ingest raw entries and (re)derive hands from them.

Split deliberately into two steps:

  ``ingest_entries``  append-only, deduped on (game_id, ord)
  ``rebuild_game``    throw away all derived rows for a game and re-parse

Because step 2 is a full rebuild from ``raw_entries``, fixing a parser bug is just
``pnt rebuild`` -- no re-download, no migration, no reconciliation. And because
step 1 dedupes on a globally unique key, re-importing an overlapping or identical
log is a no-op. Together those give the project's success criterion:
*re-importing the authoritative log changes no stat.*
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..logfmt.hero import apply_hero_cards, infer_hero
from ..logfmt.parser import ParsedHand, parse
from .csv_source import RawEntry, game_id_from_filename, read_csv


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ layer 1 --


def ingest_entries(
    conn: sqlite3.Connection, game_id: str, entries: list[RawEntry], source: str
) -> tuple[int, int]:
    """Append entries to `raw_entries`, ignoring ones already present.

    Returns ``(offered, newly_inserted)``. A second import of the same log returns
    ``(n, 0)`` -- which is exactly the signal that live capture missed nothing.
    """
    before = conn.execute(
        "SELECT COUNT(*) FROM raw_entries WHERE game_id = ?", (game_id,)
    ).fetchone()[0]
    conn.executemany(
        "INSERT OR IGNORE INTO raw_entries (game_id, ord, at, entry) VALUES (?, ?, ?, ?)",
        [(game_id, e.ord, e.at, e.entry) for e in entries],
    )
    after = conn.execute(
        "SELECT COUNT(*) FROM raw_entries WHERE game_id = ?", (game_id,)
    ).fetchone()[0]
    n_new = after - before
    conn.execute(
        "INSERT INTO imports (game_id, source, ingested_at, n_entries, n_new)"
        " VALUES (?, ?, ?, ?, ?)",
        (game_id, source, _now(), len(entries), n_new),
    )
    conn.commit()
    return len(entries), n_new


def read_raw(conn: sqlite3.Connection, game_id: str) -> list[RawEntry]:
    rows = conn.execute(
        "SELECT ord, at, entry FROM raw_entries WHERE game_id = ? ORDER BY ord ASC",
        (game_id,),
    ).fetchall()
    return [RawEntry(ord=r["ord"], at=r["at"], entry=r["entry"]) for r in rows]


# ----------------------------------------------------------------- identity --


def resolve_identity(conn: sqlite3.Connection, pn_id: str, name: str, seen_at: str) -> int:
    """Map a PokerNow ID to a canonical player, creating one on first sight.

    New IDs get their own player. Deciding that two IDs are the same human is a
    judgement call, so it stays manual (`pnt alias merge`) rather than guessing
    from display names -- this dataset contains ``hsj`` and ``HSJ`` on different
    IDs, and also one ID that presents as both ``genericpoker`` and ``500``.
    """
    row = conn.execute(
        "SELECT player_id FROM player_identities WHERE pn_id = ?", (pn_id,)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE player_identities SET last_seen_name = ?, last_seen_at = ?"
            " WHERE pn_id = ? AND (last_seen_at IS NULL OR last_seen_at <= ?)",
            (name, seen_at, pn_id, seen_at),
        )
        return row["player_id"]

    alias = name or pn_id
    if conn.execute("SELECT 1 FROM players WHERE alias = ?", (alias,)).fetchone():
        alias = f"{alias} ({pn_id})"
    cur = conn.execute(
        "INSERT INTO players (alias, created_at) VALUES (?, ?)", (alias, _now())
    )
    player_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO player_identities (pn_id, player_id, last_seen_name, last_seen_at,"
        " first_seen_at) VALUES (?, ?, ?, ?, ?)",
        (pn_id, player_id, name, seen_at, seen_at),
    )
    return player_id


def merge_players(conn: sqlite3.Connection, source_alias: str, target_alias: str) -> int:
    """Point every ID of `source_alias` at `target_alias`, then drop the empty player.

    Cheap precisely because no statistic is materialized: repointing the identity
    rows *is* the whole merge.
    """
    src = conn.execute(
        "SELECT player_id FROM players WHERE alias = ?", (source_alias,)
    ).fetchone()
    dst = conn.execute(
        "SELECT player_id FROM players WHERE alias = ?", (target_alias,)
    ).fetchone()
    if not src or not dst:
        raise ValueError(f"unknown alias: {source_alias if not src else target_alias!r}")
    if src["player_id"] == dst["player_id"]:
        return 0
    cur = conn.execute(
        "UPDATE player_identities SET player_id = ? WHERE player_id = ?",
        (dst["player_id"], src["player_id"]),
    )
    conn.execute("DELETE FROM players WHERE player_id = ?", (src["player_id"],))
    conn.commit()
    return cur.rowcount


# ------------------------------------------------------------------ layer 2 --


def _insert_hand(conn: sqlite3.Connection, hand: ParsedHand) -> None:
    cur = conn.execute(
        "INSERT INTO hands (game_id, hand_number, table_hand_id, ts, ord, dealer_seat,"
        " dead_button, blinds_irregular, n_dealt_in, bb, board_json, run_count,"
        " went_to_showdown) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            hand.game_id,
            hand.hand_number,
            hand.table_hand_id,
            hand.ts,
            hand.ord,
            hand.dealer_seat,
            int(hand.dead_button),
            int(hand.blinds_irregular),
            hand.n_dealt_in,
            hand.bb,
            json.dumps(hand.board_runs),
            hand.run_count,
            int(hand.went_to_showdown),
        ),
    )
    hand_id = int(cur.lastrowid)

    conn.executemany(
        "INSERT INTO hand_players (hand_id, pn_id, seat, seats_from_button,"
        " starting_stack, hole_cards, contributed, collected, bounty, folded)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                hand_id,
                p.pn_id,
                p.seat,
                p.seats_from_button,
                p.starting_stack,
                "".join(p.hole_cards) or None,
                p.contributed,
                p.collected,
                p.bounty,
                int(p.folded),
            )
            for p in hand.players.values()
        ],
    )
    conn.executemany(
        "INSERT INTO actions (hand_id, seq, street, pn_id, action_type, amount,"
        " amount_to, is_forced, all_in, post_kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                hand_id,
                a.seq,
                a.street,
                a.pn_id,
                a.kind,
                a.amount,
                a.amount_to,
                int(a.is_forced),
                int(a.all_in),
                a.post_kind,
            )
            for a in hand.actions
        ],
    )


def rebuild_game(conn: sqlite3.Connection, game_id: str) -> dict:
    """Re-derive every hand of a game from its stored raw entries.

    Destroys and recreates all layer-2 rows for the game inside one transaction,
    so a partial failure leaves the previous derivation intact.
    """
    entries = read_raw(conn, game_id)
    result = parse(entries, game_id)

    guess = infer_hero(result.hands)
    hero_pn_id = guess.pn_id if guess.confident else None
    if hero_pn_id:
        apply_hero_cards(result.hands, hero_pn_id)

    started_at = entries[0].at if entries else None

    with conn:  # single transaction
        conn.execute("DELETE FROM hands WHERE game_id = ?", (game_id,))
        conn.execute("DELETE FROM parse_misses WHERE game_id = ?", (game_id,))
        conn.execute(
            "INSERT INTO games (game_id, started_at, sb, bb, ante, variant, hero_pn_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(game_id) DO UPDATE SET started_at=excluded.started_at,"
            " sb=excluded.sb, bb=excluded.bb, ante=excluded.ante, hero_pn_id=excluded.hero_pn_id",
            (
                game_id,
                started_at,
                result.blinds.get("sb"),
                result.blinds.get("bb"),
                result.blinds.get("ante", 0),
                result.hands[0].variant if result.hands else "nlhe",
                hero_pn_id,
            ),
        )
        for hand in result.hands:
            for p in hand.players.values():
                resolve_identity(conn, p.pn_id, p.name, hand.ts)
            _insert_hand(conn, hand)

        conn.executemany(
            "INSERT OR REPLACE INTO parse_misses (game_id, ord, entry, reason)"
            " VALUES (?, ?, ?, ?)",
            [(game_id, o, e, r) for o, e, r in result.misses],
        )

    return {
        "game_id": game_id,
        "hands": len(result.hands),
        "entries": len(entries),
        "parse_misses": len(result.misses),
        "hero_pn_id": hero_pn_id,
        "hero_votes": guess.votes,
    }


def import_csv(conn: sqlite3.Connection, path: str | Path, game_id: str | None = None) -> dict:
    """Ingest a PokerNow CSV export and rebuild that game. Safe to run repeatedly."""
    path = Path(path)
    gid = game_id or game_id_from_filename(path) or path.stem
    entries = read_csv(path)
    offered, n_new = ingest_entries(conn, gid, entries, source=f"csv:{path.name}")
    summary = rebuild_game(conn, gid)
    summary |= {"entries_offered": offered, "entries_new": n_new}
    return summary
