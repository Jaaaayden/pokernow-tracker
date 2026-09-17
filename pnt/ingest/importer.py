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
from datetime import UTC, datetime
from pathlib import Path

from ..db.conn import bump_generation, writing
from ..logfmt.hero import apply_hero_cards, infer_hero
from ..logfmt.parser import ParsedHand, parse
from .csv_source import RawEntry, game_id_from_filename, read_csv


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ------------------------------------------------------------------ layer 1 --


def ingest_entries(
    conn: sqlite3.Connection, game_id: str, entries: list[RawEntry], source: str
) -> tuple[int, int]:
    """Append entries to `raw_entries`, ignoring ones already present.

    Returns ``(offered, newly_inserted)``. A second import of the same log returns
    ``(n, 0)`` -- which is exactly the signal that live capture missed nothing.
    """
    # Inside one `writing()` block, so the two counts bracket the insert atomically:
    # a concurrent ingest of an overlapping page can no longer land between them and
    # be credited here as new.
    with writing(conn):
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


def merge_players(conn: sqlite3.Connection, source_alias: str, target_alias: str) -> list[str]:
    """Point every ID of `source_alias` at `target_alias`, then drop the empty player.

    Cheap precisely because no statistic is materialized: repointing the identity
    rows *is* the whole merge.

    Returns the `pn_id`s that moved, rather than a count of them. That is what
    makes the merge undoable: the source player row is deleted here, so nothing
    else records which IDs used to be behind it, and `split_identities` needs
    exactly this list to put them back. A typed-out CLI merge hardly needs undo;
    a one-click merge in a UI does.
    """
    with writing(conn):
        src = conn.execute(
            "SELECT player_id FROM players WHERE alias = ?", (source_alias,)
        ).fetchone()
        dst = conn.execute(
            "SELECT player_id FROM players WHERE alias = ?", (target_alias,)
        ).fetchone()
        if not src or not dst:
            raise ValueError(f"unknown alias: {source_alias if not src else target_alias!r}")
        if src["player_id"] == dst["player_id"]:
            return []
        moved = [
            r["pn_id"]
            for r in conn.execute(
                "SELECT pn_id FROM player_identities WHERE player_id = ? ORDER BY pn_id",
                (src["player_id"],),
            )
        ]
        conn.execute(
            "UPDATE player_identities SET player_id = ? WHERE player_id = ?",
            (dst["player_id"], src["player_id"]),
        )
        conn.execute("DELETE FROM players WHERE player_id = ?", (src["player_id"],))
        bump_generation(conn)
    return moved


def split_identities(conn: sqlite3.Connection, pn_ids: list[str], alias: str) -> int:
    """Move `pn_ids` onto a new player called `alias`. The inverse of a merge.

    Undo for `merge_players`, and the only way to separate two humans who were
    joined by mistake. Like the merge it is one UPDATE and recomputes nothing.

    Refuses to empty a player entirely: moving *every* ID off one would leave a
    player row with no identities behind it, which is a rename spelled the long
    way round and would strand the old alias in the list.
    """
    if not pn_ids:
        raise ValueError("no identities given")
    with writing(conn):
        rows = conn.execute(
            "SELECT pn_id, player_id FROM player_identities"
            f" WHERE pn_id IN ({','.join('?' * len(pn_ids))})",
            pn_ids,
        ).fetchall()
        found = {r["pn_id"] for r in rows}
        missing = [i for i in pn_ids if i not in found]
        if missing:
            raise ValueError(f"unknown PokerNow id(s): {missing}")
        if conn.execute("SELECT 1 FROM players WHERE alias = ?", (alias,)).fetchone():
            raise ValueError(f"alias already exists: {alias!r}")

        for player_id in {r["player_id"] for r in rows}:
            total = conn.execute(
                "SELECT COUNT(*) FROM player_identities WHERE player_id = ?", (player_id,)
            ).fetchone()[0]
            taking = sum(1 for r in rows if r["player_id"] == player_id)
            if taking == total:
                raise ValueError(
                    "that would move every identity off a player, leaving it empty;"
                    " rename it instead"
                )

        cur = conn.execute(
            "INSERT INTO players (alias, created_at) VALUES (?, ?)", (alias, _now())
        )
        conn.executemany(
            "UPDATE player_identities SET player_id = ? WHERE pn_id = ?",
            [(cur.lastrowid, i) for i in pn_ids],
        )
        bump_generation(conn)
    return len(pn_ids)


def rename_player(conn: sqlite3.Connection, old: str, new: str) -> None:
    """Rename a canonical player. Shared by `pnt alias rename` and the players page,
    so the two cannot disagree about what counts as a valid name."""
    new = new.strip()
    if not new:
        raise ValueError("the new name cannot be empty")
    with writing(conn):
        if new != old and conn.execute(
            "SELECT 1 FROM players WHERE alias = ?", (new,)
        ).fetchone():
            raise ValueError(f"alias already exists: {new!r} -- merge into it instead")
        if not conn.execute(
            "UPDATE players SET alias = ? WHERE alias = ?", (new, old)
        ).rowcount:
            raise ValueError(f"unknown alias: {old!r}")
        bump_generation(conn)


def export_aliases(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Every ``(pn_id, alias)`` pair, in the order `pnt alias list` shows them.

    The alias table is the one piece of the database that is not rebuilt from
    logs -- it is a judgement about who is who -- so it is the one piece worth
    keeping outside the database.
    """
    return [
        (r["pn_id"], r["alias"])
        for r in conn.execute(
            "SELECT pi.pn_id, p.alias FROM player_identities pi"
            " JOIN players p ON p.player_id = pi.player_id ORDER BY p.alias, pi.pn_id"
        )
    ]


def apply_aliases(conn: sqlite3.Connection, pairs: list[tuple[str, str]]) -> tuple[int, int]:
    """Make every known ``pn_id`` belong to the player named beside it.

    The inverse of `export_aliases`, and idempotent: applying the same pairs twice
    moves nothing the second time. IDs the database has not seen yet are skipped
    rather than invented, since an identity row needs a name and a date only a log
    can supply -- re-apply after importing more.

    A player that already has an alias from the file but also holds IDs the file
    puts elsewhere (or does not mention) is a different person who happens to share
    the name; it is renamed out of the way the same way `resolve_identity` settles a
    collision, instead of being silently merged in.

    Returns ``(moved, unknown)``.
    """
    wanted: dict[str, list[str]] = {}
    for pn_id, alias in pairs:
        wanted.setdefault(alias, []).append(pn_id)

    moved = 0
    with writing(conn):
        known = {r["pn_id"] for r in conn.execute("SELECT pn_id FROM player_identities")}
        for alias, ids in wanted.items():
            ids = [i for i in ids if i in known]
            if not ids:
                continue
            row = conn.execute("SELECT player_id FROM players WHERE alias = ?", (alias,)).fetchone()
            if row:
                strays = [
                    r["pn_id"]
                    for r in conn.execute(
                        "SELECT pn_id FROM player_identities WHERE player_id = ? ORDER BY pn_id",
                        (row["player_id"],),
                    )
                    if r["pn_id"] not in ids
                ]
                if strays:
                    conn.execute(
                        "UPDATE players SET alias = ? WHERE player_id = ?",
                        (f"{alias} ({strays[0]})", row["player_id"]),
                    )
                    row = None
            if row:
                player_id = row["player_id"]
            else:
                cur = conn.execute(
                    "INSERT INTO players (alias, created_at) VALUES (?, ?)", (alias, _now())
                )
                player_id = int(cur.lastrowid)
            moved += conn.executemany(
                "UPDATE player_identities SET player_id = ? WHERE pn_id = ? AND player_id != ?",
                [(player_id, i, player_id) for i in ids],
            ).rowcount
        unknown = sum(1 for pn_id, _ in pairs if pn_id not in known)
        conn.execute(
            "DELETE FROM players WHERE player_id NOT IN (SELECT player_id FROM player_identities)"
        )
        bump_generation(conn)
    return moved, unknown


# ------------------------------------------------------------------ layer 2 --


def _insert_hand(conn: sqlite3.Connection, hand: ParsedHand) -> None:
    cur = conn.execute(
        "INSERT INTO hands (game_id, hand_number, table_hand_id, ts, ord, dealer_seat,"
        " dead_button, blinds_irregular, n_dealt_in, bb, board_json, run_count,"
        " went_to_showdown, complete) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            int(hand.complete),
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
    # After hand_players: every show references the player row it came from.
    conn.executemany(
        "INSERT INTO voluntary_shows (hand_id, pn_id, cards, ord) VALUES (?, ?, ?, ?)",
        [
            (hand_id, s.pn_id, "".join(s.cards), s.ord)
            for s in hand.voluntary_shows.values()
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

    with writing(conn):  # single transaction, write lock taken up front
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
        bump_generation(conn)

    return {
        "game_id": game_id,
        "hands": len(result.hands),
        "entries": len(entries),
        "parse_misses": len(result.misses),
        "hero_pn_id": hero_pn_id,
        "hero_votes": guess.votes,
    }


def delete_game(conn: sqlite3.Connection, game_id: str) -> int:
    """Remove a game: its raw lines, every hand derived from them, its import history.

    The inverse of importing it, so re-importing the log puts back exactly what this
    took away. Returns the number of hands removed.

    Identities mostly stay. A merge or a rename is a judgement no log can rebuild, so
    a player is removed only when nothing about it came from a person: a single
    PokerNow ID, still under the name `resolve_identity` gave it, in no hand left.
    """
    with writing(conn):
        hands = conn.execute(
            "SELECT COUNT(*) FROM hands WHERE game_id = ?", (game_id,)
        ).fetchone()[0]
        # Hands first: hand_players, actions and voluntary_shows cascade from them.
        for table in ("hands", "games", "raw_entries", "parse_misses", "imports", "log_files"):
            conn.execute(f"DELETE FROM {table} WHERE game_id = ?", (game_id,))
        conn.execute(
            "DELETE FROM players WHERE player_id IN ("
            " SELECT pi.player_id FROM player_identities pi"
            " JOIN players p ON p.player_id = pi.player_id"
            " WHERE pi.pn_id NOT IN (SELECT pn_id FROM hand_players)"
            "   AND p.alias IN"
            "     (pi.pn_id, pi.last_seen_name, pi.last_seen_name || ' (' || pi.pn_id || ')')"
            "   AND (SELECT COUNT(*) FROM player_identities o"
            "        WHERE o.player_id = pi.player_id) = 1)"
        )
        bump_generation(conn)
    return hands


def import_csv(conn: sqlite3.Connection, path: str | Path, game_id: str | None = None) -> dict:
    """Ingest a PokerNow CSV export and rebuild that game. Safe to run repeatedly."""
    path = Path(path)
    gid = game_id or game_id_from_filename(path) or path.stem
    entries = read_csv(path)
    offered, n_new = ingest_entries(conn, gid, entries, source=f"csv:{path.name}")
    summary = rebuild_game(conn, gid)
    summary |= {"entries_offered": offered, "entries_new": n_new}
    return summary
