"""Local HTTP service over the tracker database.

Exists so the HUD and your analysis scripts read the same data through the same
definitions. The database stays a plain SQLite file on disk, so pandas can open it
directly and ignore this server entirely -- keeping the data out of the browser is
the point, and IndexedDB would have trapped it in one profile.

`POST /ingest` is deliberately built now, before any extension exists, so the
capture contract is fixed and testable ahead of the browser work.

Run with:  pnt serve      (or: uvicorn pnt.server.app:app --port 52000)
"""

from __future__ import annotations

import csv
import logging
import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from pnt.db.conn import connect
from pnt.ingest import log_folder, sync
from pnt.ingest.csv_source import RawEntry
from pnt.ingest.importer import (
    ingest_entries,
    merge_players,
    rebuild_game,
    rename_player,
    split_identities,
)
from pnt.stats.allin import allin_hand_list, allin_report
from pnt.stats.derive import Facts
from pnt.stats.filters import parse_filter, vocabulary
from pnt.stats.pots import DEFAULT_DAYS, DEFAULT_LIMIT, DEFAULT_MIN_POT, big_pots
from pnt.stats.queries import (
    aggregate,
    display_names,
    facts_for,
    hand_list,
    positional_report,
    report,
)
from pnt.stats.ranges import composition, range_grid, sizing_tells
from pnt.stats.review import mark_reviewed, review_hand_list, reviewed_marks

DB_PATH = Path(os.environ.get("PNT_DB", "pokernow.sqlite"))
STATIC = Path(__file__).parent / "static"

#: Live capture also keeps each game's CSV in the log folder, so the folder stays a
#: running record: every game in the database, as a file `pnt import` could rebuild
#: it from. On unless PNT_SAVE_LOGS=0.
SAVE_LOGS = log_folder.SAVE_LOGS

log = logging.getLogger(__name__)

#: Seconds between log-folder syncs while the server runs; 0 turns them off. Each
#: pass is a stat per file, so a short interval costs nothing between changes.
SYNC_SECONDS = float(os.environ.get("PNT_SYNC_SECONDS", "5"))


def _sync_loop(stop: threading.Event) -> None:
    """Keep the database in step with the log folder until `stop` is set.

    Its own connection: sqlite3 connections belong to the thread that opened them.
    Never raises -- a sync that fails is logged and tried again next interval.
    """
    conn = db()
    skip: dict[str, tuple[int, int]] = {}
    try:
        while True:
            try:
                out = sync.sync_folder(conn, log_folder.LOG_DIR, skip)
                for s in out.imported:
                    if s["entries_new"]:
                        log.info("imported %s: %d new entries", s["file"], s["entries_new"])
                for path, why in out.failed.items():
                    log.warning("could not import %s: %s", path, why)
            except Exception:
                log.exception("log folder sync failed")
                if conn.in_transaction:
                    conn.rollback()
            if stop.wait(SYNC_SECONDS):
                return
    finally:
        conn.close()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    stop = threading.Event()
    worker = None
    if SYNC_SECONDS > 0:
        worker = threading.Thread(target=_sync_loop, args=(stop,), name="log-sync", daemon=True)
        worker.start()
    try:
        yield
    finally:
        stop.set()
        if worker is not None:
            worker.join(timeout=10)


app = FastAPI(title="PokerNow Tracker", version="0.1.0", lifespan=_lifespan)

# The content script runs on PokerNow and posts here. Restricted to those origins:
# this server is a local database with no auth, so it should not be callable from
# arbitrary pages the browser happens to have open. Games are served from
# pokernow.com; the pokernow.club addresses this was first built against are kept
# in case links still use them. Keep in step with extension/manifest.json.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://www.pokernow.com",
        "https://pokernow.com",
        "https://www.pokernow.club",
        "https://pokernow.club",
    ],
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


def _page(name: str) -> HTMLResponse:
    """One of the static pages.

    Re-read from disk on every request, with `no-store` so the browser holds no
    old copy -- but the Python behind it is loaded once, so after changing stat
    code, restart the server (`pnt service restart`).
    """
    return HTMLResponse(
        (STATIC / name).read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/", include_in_schema=False)
def index() -> HTMLResponse:
    """The front door: what is in the database and where everything is.

    Without it, `127.0.0.1:52000` answered 404 and every page had to be reached by
    typing its path -- fine for whoever built it, useless for anyone else.
    """
    return _page("index.html")


@app.get("/players.html", include_in_schema=False)
def players_page() -> HTMLResponse:
    """Aliases and the PokerNow IDs behind them: merge, split and rename.

    The one piece of upkeep this database needs that nothing can do for you. The
    same human on a second device is a second ID, and only someone who was at the
    table knows which two are the same person -- so this presents the evidence and
    leaves the judgement alone. `/players` serves this to browsers.
    """
    return _page("players.html")


@app.get("/chart", include_in_schema=False)
def chart() -> HTMLResponse:
    """The range chart page. One static file; all data comes from /players/{alias}/range.

    Query parameters (`?player=henry&filter=opener,srp&by=made&color=size`) seed
    the page state, so a bookmark -- or an extension iframe -- lands on a
    specific player and spot.
    """
    return _page("chart.html")


@app.get("/stats.html", include_in_schema=False)
def stats_page() -> HTMLResponse:
    """Every player's stats, as a sortable table. `/stats` serves this to browsers."""
    return _page("stats.html")


@app.get("/allin.html", include_in_schema=False)
def allin_page() -> HTMLResponse:
    """All-in EV: what every player's jams were worth against what they took.
    `/allin` serves this to browsers."""
    return _page("allin.html")


@app.get("/pots.html", include_in_schema=False)
def pots_page() -> HTMLResponse:
    """The biggest pots over a window of days. `/pots` serves this to browsers."""
    return _page("pots.html")


def _script(name: str) -> Response:
    return Response(
        (STATIC / name).read_text(encoding="utf-8"),
        media_type="text/javascript",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/filter-help.js", include_in_schema=False)
def filter_help_script() -> Response:
    """The `?` panel beside every Spot box, shared by the chart, stats and all-in pages."""
    return _script("filter-help.js")


@app.get("/replay.js", include_in_schema=False)
def replay_script() -> Response:
    """The hand replay renderer, shared by the chart and all-in pages."""
    return _script("replay.js")


@app.get("/filters")
def filters() -> dict:
    """Every term a `filter` parameter understands, grouped, with an example each."""
    return vocabulary()


@app.get("/health")
def health() -> dict:
    conn = db()
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM hands) h, (SELECT COUNT(*) FROM raw_entries) e,"
        " (SELECT COUNT(*) FROM parse_misses) m"
    ).fetchone()
    return {
        "db": str(DB_PATH),
        "hands": row["h"],
        "entries": row["e"],
        "parse_misses": row["m"],
        "log_folder": str(log_folder.LOG_DIR) if SAVE_LOGS else None,
    }


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
        _save_log(conn, req.game_id)
    return out


@app.post("/rebuild/{game_id}")
def rebuild(game_id: str) -> dict:
    conn = db()
    out = rebuild_game(conn, game_id)
    _save_log(conn, game_id)
    return out


def _save_log(conn: sqlite3.Connection, game_id: str) -> None:
    """Bring the game's CSV in the log folder up to date with the database.

    Tied to rebuilds, not to every ingest: the extension ingests a history walk page
    by page and rebuilds at checkpoints, and a rewrite is O(game) just as a rebuild
    is (~40 ms for an 8,500-line game). Never fails the request -- the database
    already has the lines, and a CSV open in Excel must not stop capture.
    """
    if not SAVE_LOGS:
        return
    try:
        # A log deleted since the last save means the game is to go, not to be
        # written straight back out of the database.
        if sync.prune(conn, [game_id]):
            return
        log_folder.save_game(conn, game_id, log_folder.LOG_DIR)
        path = log_folder.log_path(log_folder.LOG_DIR, game_id)
        if path.exists():
            sync.record_file(conn, path, game_id)
    except (OSError, ValueError, csv.Error, sqlite3.Error) as exc:  # locked, unwritable, malformed
        log.warning("could not save the log for %s to %s: %s", game_id, log_folder.LOG_DIR, exc)


@app.get("/stats", response_model=None)
def stats(
    request: Request,
    game: str | None = None,
    filter: Annotated[str | None, Query(description="e.g. '3bet,position=BTN'")] = None,
    min_hands: int = 1,
) -> list[dict] | HTMLResponse:
    """Per-player stats, optionally restricted to a spot.

    The filter compiles to a predicate over derived per-hand facts, which is only
    possible because actions are stored raw with street and sequence.

    A browser navigating here asks for HTML and gets the stats page; every other
    caller (the HUD, a script, curl) asks for anything and gets the JSON. The page
    is also at /stats.html, which is what the page itself links to.
    """
    if "text/html" in request.headers.get("accept", ""):
        return _page("stats.html")
    conn = db()
    return report(conn, game_id=game, min_hands=min_hands, predicate=_predicate(conn, filter))


def _predicate(conn: sqlite3.Connection, filter: str | None):
    """Compile a spot filter, or 400 on a term the parser does not know."""
    try:
        return parse_filter(filter, display_names(conn)) if filter else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/allin", response_model=None)
def allin(
    request: Request,
    game: str | None = None,
    filter: Annotated[str | None, Query(description="e.g. 'position=BTN,3bet_pot'")] = None,
    min_hands: int = 1,
) -> list[dict] | HTMLResponse:
    """Per player: all-in showdowns, actual net, all-in adjusted net, and the gap.

    Same shape of contract as /stats: a browser gets the page, everything else the
    JSON. Equities are memoized in `equity_cache`, so the first request after an
    import pays for the sampling once -- about half a second per preflop all-in --
    and every request after it is a read.
    """
    if "text/html" in request.headers.get("accept", ""):
        return _page("allin.html")
    conn = db()
    return allin_report(conn, game_id=game, min_hands=min_hands, predicate=_predicate(conn, filter))


@app.get("/pots", response_model=None)
def pots(
    request: Request,
    days: Annotated[float, Query(gt=0, description="Window in days, counted back from now.")] = DEFAULT_DAYS,
    all_time: Annotated[bool, Query(description="Ignore the window and read all of history.")] = False,
    min_pot: Annotated[int, Query(ge=0, description="Chips a pot must reach to be listed.")] = DEFAULT_MIN_POT,
    game: str | None = None,
    player: Annotated[str | None, Query(description="Only hands this player was dealt into.")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = DEFAULT_LIMIT,
) -> dict | HTMLResponse:
    """The biggest pots in a window, largest first -- everyone's, not one player's.

    Same contract as /stats and /allin: a browser gets the page, everything else
    the JSON. `all_time` is how "no window" is asked for, since a window of zero
    days would otherwise have to mean two different things. See SPEC.md,
    "Biggest pots".
    """
    if "text/html" in request.headers.get("accept", ""):
        return _page("pots.html")
    try:
        return big_pots(
            db(),
            days=None if all_time else days,
            min_pot=min_pot,
            game_id=game,
            player=player,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/players/{alias}/allin")
def player_allin(
    alias: str,
    filter: Annotated[str | None, Query(description="e.g. 'vs=henry'")] = None,
    game: str | None = None,
) -> dict:
    """One player's all-in showdowns, oldest first: the rows behind their two lines."""
    conn = db()
    pred = _predicate(conn, filter)
    try:
        return {"filter": filter, **allin_hand_list(conn, alias, game, pred)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/players/{alias}/review")
def player_review(
    alias: str,
    filter: Annotated[str | None, Query(description="e.g. 'srp,vs=henry'")] = None,
    game: str | None = None,
) -> dict:
    """One player's flagged hands, newest first: the mistakes worth a replay and the
    bad beats, one row per hand and flag. See SPEC.md, "Hand review"."""
    conn = db()
    pred = _predicate(conn, filter)
    try:
        return {"filter": filter, **review_hand_list(conn, alias, game, pred)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/players", response_model=None)
def players(request: Request) -> list[dict] | HTMLResponse:
    """Every canonical player, the PokerNow IDs behind them, and hands per ID.

    A browser navigating here gets the players page; fetch, curl and the chart's
    dropdown get the JSON, exactly as /stats works.

    Hand counts come straight from `hand_players` rather than through `report()`.
    That is a plain join -- milliseconds -- where the derived path walks every hand
    in the database, and nothing on this page needs a derived statistic.
    """
    if "text/html" in request.headers.get("accept", ""):
        return _page("players.html")
    conn = db()
    counts = {
        r["pn_id"]: r["n"]
        for r in conn.execute("SELECT pn_id, COUNT(*) AS n FROM hand_players GROUP BY pn_id")
    }
    out: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT p.alias, pi.pn_id, pi.last_seen_name, pi.first_seen_at, pi.last_seen_at"
        " FROM players p JOIN player_identities pi ON pi.player_id = p.player_id"
        " ORDER BY p.alias, pi.pn_id"
    ):
        entry = out.setdefault(
            r["alias"], {"alias": r["alias"], "n_ids": 0, "hands": 0, "identities": []}
        )
        entry["n_ids"] += 1
        entry["hands"] += counts.get(r["pn_id"], 0)
        entry["identities"].append(
            {
                "pn_id": r["pn_id"],
                "name": r["last_seen_name"],
                "hands": counts.get(r["pn_id"], 0),
                "first_seen_at": r["first_seen_at"],
                "last_seen_at": r["last_seen_at"],
            }
        )
    rows = list(out.values())
    for entry in rows:
        # Kept for callers written against the original shape -- the chart page's
        # dropdown among them.
        entry["pn_ids"] = ",".join(i["pn_id"] for i in entry["identities"])
        entry["names"] = ",".join(
            dict.fromkeys(i["name"] for i in entry["identities"] if i["name"])
        )
    return rows


@app.get("/players/{alias}/positions")
def positions(alias: str, split_by_size: bool = False) -> list[dict]:
    try:
        return positional_report(db(), alias, pool=not split_by_size)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _spot_facts(
    alias: str, filter: str | None, game: str | None, conn: sqlite3.Connection | None = None
) -> list[Facts]:
    """One player's hands in a spot: 400 on a bad filter, 404 on an unknown alias."""
    if conn is None:
        conn = db()
    # The name map is what lets `vs=henry` name a person rather than an ID.
    pred = _predicate(conn, filter)
    try:
        facts = facts_for(conn, alias, game)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [f for f in facts if pred(f)] if pred is not None else facts


@app.get("/players/{alias}/stats")
def player_stats(
    alias: str,
    filter: Annotated[str | None, Query(description="e.g. 'srp,flop=ace_high'")] = None,
    game: str | None = None,
) -> dict:
    """One player's stats inside a spot -- what the chart page's postflop strip reads."""
    return {"player": alias, "filter": filter, **aggregate(_spot_facts(alias, filter, game))}


@app.get("/players/{alias}/range")
def player_range(
    alias: str,
    filter: Annotated[str | None, Query(description="e.g. 'opener,open_bb>=4,srp'")] = None,
    by: Annotated[str, Query(pattern="^(preflop|made)$")] = "preflop",
    game: str | None = None,
) -> dict:
    """The range chart (`by=preflop`) or line composition (`by=made`) for one player.

    This is what the chart page renders. Every cell of the 169-grid is present,
    in chart order, so the client needs no card logic of its own.
    """
    facts = _spot_facts(alias, filter, game)
    out = range_grid(facts) if by == "preflop" else composition(facts)
    return {"player": alias, "filter": filter, "by": by, **out}


@app.get("/players/{alias}/sizing")
def player_sizing(
    alias: str,
    street: Annotated[str, Query(pattern="^(flop|turn|river)$")] = "flop",
    kind: Annotated[str, Query(pattern="^(cbet|bet|faced_cbet)$")] = "cbet",
    filter: Annotated[str | None, Query(description="e.g. 'srp,flop=ace_high'")] = None,
    game: str | None = None,
) -> dict:
    """What a player had at each bet size on one street, within a spot."""
    facts = _spot_facts(alias, filter, game)
    return {"player": alias, "filter": filter, **sizing_tells(facts, street, kind)}


@app.get("/players/{alias}/hands")
def player_hands(
    alias: str,
    filter: Annotated[str | None, Query(description="e.g. 'cbet_flop=overbet'")] = None,
    game: str | None = None,
) -> dict:
    """Every hand in a spot as a compact row, newest first. Replay one with /hands/{id}."""
    # One connection for both halves: db() opens a fresh one per call.
    conn = db()
    facts = _spot_facts(alias, filter, game, conn)
    return {"player": alias, "filter": filter, "hands": hand_list(facts, display_names(conn))}


@app.get("/hands/{hand_id}")
def hand(hand_id: int) -> dict:
    """Full replay of one hand -- for spot-checking a stat you do not believe."""
    conn = db()
    h = conn.execute(
        "SELECT h.*, COALESCE(h.bb, g.bb) AS bb_effective"
        " FROM hands h LEFT JOIN games g ON g.game_id = h.game_id WHERE h.hand_id = ?",
        (hand_id,),
    ).fetchone()
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
        # pn_id -> the name a replay should print: the canonical alias when there
        # is one, otherwise the last name PokerNow showed for that ID.
        "names": {
            r["pn_id"]: r["alias"] or r["last_seen_name"] or r["pn_id"]
            for r in conn.execute(
                "SELECT hp.pn_id, p.alias, pi.last_seen_name FROM hand_players hp"
                " LEFT JOIN player_identities pi ON pi.pn_id = hp.pn_id"
                " LEFT JOIN players p ON p.player_id = pi.player_id"
                " WHERE hp.hand_id = ?",
                (hand_id,),
            )
        },
        "actions": [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM actions WHERE hand_id = ? ORDER BY seq", (hand_id,)
            )
        ],
        # Shown after the hand ended -- never part of `players[].hole_cards`.
        "voluntary_shows": [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM voluntary_shows WHERE hand_id = ? ORDER BY ord", (hand_id,)
            )
        ],
    }


class ReviewedRequest(BaseModel):
    reviewed: bool = True


@app.post("/hands/{hand_id}/reviewed")
def set_reviewed(hand_id: int, req: Annotated[ReviewedRequest, Body()]) -> dict:
    """Mark one hand as reviewed, or clear the mark.

    Addressed by `hand_id` because that is what a row on the page already holds,
    but stored under the hand's (game_id, hand_number) -- see schema.sql -- so the
    mark survives the rebuild that gives the hand a new id.
    """
    conn = db()
    row = conn.execute(
        "SELECT game_id, hand_number FROM hands WHERE hand_id = ?", (hand_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="no such hand")
    at = mark_reviewed(conn, row["game_id"], row["hand_number"], req.reviewed)
    return {
        "hand_id": hand_id,
        "game_id": row["game_id"],
        "hand_number": row["hand_number"],
        "reviewed": at is not None,
        "reviewed_at": at,
    }


@app.get("/reviewed")
def reviewed(game: str = Query(None, description="Restrict to one game_id.")) -> list[dict]:
    """Every hand marked reviewed, newest mark first."""
    marks = reviewed_marks(db(), game)
    return [
        {"game_id": g, "hand_number": n, "reviewed_at": at}
        for (g, n), at in sorted(marks.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    ]


@app.get("/hud/{game_id}")
def hud(game_id: str) -> dict:
    """Stats for everyone currently at a table, keyed by PokerNow ID.

    Keyed by `pn_id` rather than seat on purpose: the overlay must re-resolve
    seat -> player every hand from the live dealt-in roster, because seats are
    reused as players come and go.

    Each seat carries two reports with identical keys: `stats`, lifetime across
    every game and merged identity, and `session`, this game alone -- the pair
    the overlay prints side by side so a player drifting from their history is
    visible while it happens. Both come from `report()`, whose cache is keyed on
    the game, so a 30-second poll re-derives nothing between hands.
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
    by_session = {r["player"]: r for r in report(conn, game_id=game_id)}
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
                # The same figures over this game only.
                "session": by_session.get(r["alias"], {"hands": 0}),
            }
            for r in seated
        ],
    }


class MergeRequest(BaseModel):
    source: str
    target: str


class RenameRequest(BaseModel):
    old: str
    new: str


class SplitRequest(BaseModel):
    pn_ids: list[str]
    alias: str


@app.post("/aliases/merge")
def merge(req: Annotated[MergeRequest, Body()]) -> dict:
    """Fold every ID of `source` into `target`. Returns the IDs that moved.

    The IDs are returned, not just counted, so the caller can undo this: the source
    player row is deleted here, and this list is the only remaining record of what
    was behind it. `POST /aliases/split` with it puts things back.
    """
    try:
        moved = merge_players(db(), req.source, req.target)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "moved": len(moved),
        "pn_ids": moved,
        "source": req.source,
        "target": req.target,
        "undo": {"pn_ids": moved, "alias": req.source},
    }


@app.post("/aliases/split")
def split(req: Annotated[SplitRequest, Body()]) -> dict:
    """Move PokerNow IDs onto a new player. Undo for a merge, and the fix when two
    people were joined by mistake."""
    try:
        n = split_identities(db(), req.pn_ids, req.alias)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"moved": n, "alias": req.alias, "pn_ids": req.pn_ids}


@app.post("/aliases/rename")
def rename(req: Annotated[RenameRequest, Body()]) -> dict:
    try:
        rename_player(db(), req.old, req.new)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"old": req.old, "new": req.new.strip()}
