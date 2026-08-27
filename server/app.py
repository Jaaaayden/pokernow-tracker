"""Local HTTP service over the tracker database.

Exists so the HUD and your analysis scripts read the same data through the same
definitions. The database stays a plain SQLite file on disk, so pandas can open it
directly and ignore this server entirely -- keeping the data out of the browser is
the point, and IndexedDB would have trapped it in one profile.

`POST /ingest` is deliberately built now, before any extension exists, so the
capture contract is fixed and testable ahead of the browser work.

Run with:  pnt serve      (or: uvicorn server.app:app --port 8000)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from pnt.db.conn import connect
from pnt.ingest.csv_source import RawEntry
from pnt.ingest.importer import ingest_entries, merge_players, rebuild_game
from pnt.stats.filters import parse_filter
from pnt.stats.queries import positional_report, report

DB_PATH = Path(os.environ.get("PNT_DB", "pokernow.sqlite"))

app = FastAPI(title="PokerNow Tracker", version="0.1.0")

# The content script runs on pokernow.club and posts here. Restricted to that
# origin: this server is a local database with no auth, so it should not be
# callable from arbitrary pages the browser happens to have open.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.pokernow.club", "https://pokernow.club"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def db():
    return connect(DB_PATH)


class Entry(BaseModel):
    entry: str
    at: str
    order: int


class IngestRequest(BaseModel):
    game_id: str
    entries: list[Entry]
    source: str = "extension"
    rebuild: bool = Field(
        True,
        description=(
            "Re-derive the game after ingesting. Live capture may set this False "
            "on most hands and True periodically, since a rebuild is O(game)."
        ),
    )


@app.get("/health")
def health() -> dict:
    conn = db()
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM hands) h, (SELECT COUNT(*) FROM raw_entries) e,"
        " (SELECT COUNT(*) FROM parse_misses) m"
    ).fetchone()
    return {"db": str(DB_PATH), "hands": row["h"], "entries": row["e"], "parse_misses": row["m"]}


@app.post("/ingest")
def ingest(req: IngestRequest) -> dict:
    """Accept raw log entries. Idempotent: entries dedupe on (game_id, order).

    This is the endpoint the extension calls. Because it takes *raw log lines* --
    the same ones the CSV export contains -- live capture and backfill run through
    one parser and cannot drift apart.
    """
    conn = db()
    entries = [RawEntry(ord=e.order, at=e.at, entry=e.entry) for e in req.entries]
    offered, n_new = ingest_entries(conn, req.game_id, entries, req.source)
    out = {"game_id": req.game_id, "offered": offered, "new": n_new}
    if req.rebuild and n_new:
        out |= rebuild_game(conn, req.game_id)
    return out


@app.post("/rebuild/{game_id}")
def rebuild(game_id: str) -> dict:
    return rebuild_game(db(), game_id)


@app.get("/stats")
def stats(
    game: str | None = None,
    filter: Annotated[str | None, Query(description="e.g. '3bet,position=BTN'")] = None,
    min_hands: int = 1,
) -> list[dict]:
    """Per-player stats, optionally restricted to a spot.

    The filter compiles to a predicate over derived per-hand facts, which is only
    possible because actions are stored raw with street and sequence.
    """
    try:
        pred = parse_filter(filter) if filter else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return report(db(), game_id=game, min_hands=min_hands, predicate=pred)


@app.get("/players")
def players() -> list[dict]:
    rows = db().execute(
        "SELECT p.alias, COUNT(pi.pn_id) AS n_ids,"
        " GROUP_CONCAT(pi.pn_id, ',') AS pn_ids,"
        " GROUP_CONCAT(DISTINCT pi.last_seen_name) AS names"
        " FROM players p JOIN player_identities pi ON pi.player_id = p.player_id"
        " GROUP BY p.player_id ORDER BY p.alias"
    ).fetchall()
    return [dict(r) for r in rows]


@app.get("/players/{alias}/positions")
def positions(alias: str, split_by_size: bool = False) -> list[dict]:
    try:
        return positional_report(db(), alias, pool=not split_by_size)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/hands/{hand_id}")
def hand(hand_id: int) -> dict:
    """Full replay of one hand -- for spot-checking a stat you do not believe."""
    conn = db()
    h = conn.execute("SELECT * FROM hands WHERE hand_id = ?", (hand_id,)).fetchone()
    if h is None:
        raise HTTPException(status_code=404, detail="no such hand")
    return {
        "hand": dict(h),
        "players": [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM hand_players WHERE hand_id = ? ORDER BY seats_from_button",
                (hand_id,),
            )
        ],
        "actions": [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM actions WHERE hand_id = ? ORDER BY seq", (hand_id,)
            )
        ],
    }


@app.get("/hud/{game_id}")
def hud(game_id: str) -> dict:
    """Stats for everyone currently at a table, keyed by PokerNow ID.

    Keyed by `pn_id` rather than seat on purpose: the overlay must re-resolve
    seat -> player every hand from the live dealt-in roster, because seats are
    reused as players come and go.
    """
    conn = db()
    latest = conn.execute(
        "SELECT hand_id FROM hands WHERE game_id = ? ORDER BY ord DESC LIMIT 1",
        (game_id,),
    ).fetchone()
    if latest is None:
        raise HTTPException(status_code=404, detail="no hands for that game")
    seated = conn.execute(
        "SELECT hp.pn_id, hp.seat, p.alias FROM hand_players hp"
        " LEFT JOIN player_identities pi ON pi.pn_id = hp.pn_id"
        " LEFT JOIN players p ON p.player_id = pi.player_id"
        " WHERE hp.hand_id = ?",
        (latest["hand_id"],),
    ).fetchall()

    by_alias = {r["player"]: r for r in report(conn)}
    return {
        "game_id": game_id,
        "seats": [
            {
                "seat": r["seat"],
                "pn_id": r["pn_id"],
                "alias": r["alias"],
                # Lifetime stats across every game and every merged identity --
                # not just this session.
                "stats": by_alias.get(r["alias"], {"hands": 0}),
            }
            for r in seated
        ],
    }


class MergeRequest(BaseModel):
    source: str
    target: str


@app.post("/aliases/merge")
def merge(req: Annotated[MergeRequest, Body()]) -> dict:
    try:
        n = merge_players(db(), req.source, req.target)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"moved": n, "source": req.source, "target": req.target}
