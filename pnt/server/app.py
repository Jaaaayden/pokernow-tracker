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
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi import Path as FastApiPath
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from pnt.db.conn import connect
from pnt.ingest import log_folder, sync
from pnt.ingest.csv_source import RawEntry
from pnt.ingest.importer import (
    ingest_entries,
    merge_players,
    player_notes,
    rebuild_game,
    rename_player,
    set_player_note,
    split_identities,
)
from pnt.stats.allin import allin_hand_list, allin_report
from pnt.stats.bluffs import CATEGORY, bluff_value, members
from pnt.stats.bluffs import pool_facts as bluff_pool_facts
from pnt.stats.derive import Facts
from pnt.stats.filters import parse_filter, vocabulary
from pnt.stats.fold_equity import fold_equity
from pnt.stats.fold_equity import pool_facts as fold_pool_facts
from pnt.stats.lines import DEFAULT_MIN, MODES, line_breakdown
from pnt.stats.lines import pool_facts as line_pool_facts
from pnt.stats.live import snapshot
from pnt.stats.pots import DEFAULT_DAYS, DEFAULT_LIMIT, DEFAULT_MIN_POT, big_pots
from pnt.stats.queries import (
    aggregate,
    display_names,
    facts_cached,
    hand_list,
    player_games,
    positional_report,
    report,
    sat_out_list,
)
from pnt.stats.ranges import composition, range_grid, sizing_tells
from pnt.stats.review import (
    flagged_hands,
    hand_notes,
    mark_reviewed,
    review_hand_list,
    reviewed_marks,
    set_flag,
    set_note,
)
from pnt.stats.tags import tags_for
from pnt.update import EXTENSION_DIR, extension_build

DB_PATH = Path(os.environ.get("PNT_DB", "pokernow.sqlite"))
STATIC = Path(__file__).parent / "static"
ICONS = EXTENSION_DIR / "icons"
#: The extension files this server shipped with, read once: `pnt update` replaces
#: them only while the server is stopped. A loaded extension that sees this change
#: reloads itself to match (see pnt/update.py).
EXTENSION_BUILD = extension_build()

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

# This is a local database with no auth, so what reaches it is limited three ways.
#
# * No CORS at all. The extension's background worker is the only other caller,
#   and its host permission lets it read the answers without CORS. An allowlist
#   for PokerNow's origins would only have let PokerNow's own pages read every
#   hand stored here, hole cards included.
# * The Host header must name this machine. A page on a domain that re-points
#   itself at 127.0.0.1 (DNS rebinding) is then refused rather than served.
# * Every write carries WRITE_HEADER. CORS does not stop a cross-site POST from
#   being *sent*, only its answer from being read. A body-less POST (/rebuild)
#   needs no content type at all, and FastAPI before its strict content-type
#   check parsed a body sent with none as JSON, which any open tab can send. A
#   custom header cannot be sent cross-site without a CORS preflight, which this
#   server never grants, so it holds whatever FastAPI does. A foreign Origin is
#   refused too.
LOCAL_HOSTS = ["127.0.0.1", "localhost"]
#: `pnt serve --host` names one more address the server may be reached by.
ALLOWED_HOSTS = LOCAL_HOSTS + [
    h for h in [os.environ.get("PNT_HOST", "")] if h and h not in LOCAL_HOSTS and h not in ("0.0.0.0", "::")
]
WRITE_HEADER = "x-pnt"
_READS = {"GET", "HEAD", "OPTIONS"}


def _trusted_origin(origin: str) -> bool:
    """The extension, or one of this server's own pages."""
    parts = urlsplit(origin)
    return parts.scheme == "chrome-extension" or parts.hostname in ALLOWED_HOSTS


@app.middleware("http")
async def _writes_from_here_only(request: Request, call_next):
    if request.method not in _READS:
        origin = request.headers.get("origin")
        if WRITE_HEADER not in request.headers or (origin is not None and not _trusted_origin(origin)):
            return JSONResponse(
                {"detail": f"writes need the {WRITE_HEADER} header and a local origin"},
                status_code=403,
            )
    return await call_next(request)


app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

#: A PokerNow game ID, as `log_folder.GAME_ID`: the one shape allowed near a file name.
GAME_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
GameId = Annotated[str, FastApiPath(pattern=GAME_ID_PATTERN)]


def db():
    return connect(DB_PATH)


class Entry(BaseModel):
    entry: str
    at: str
    order: int


class IngestRequest(BaseModel):
    game_id: str = Field(pattern=GAME_ID_PATTERN)
    entries: list[Entry]
    source: str = "extension"
    rebuild: bool = Field(
        True,
        description=(
            "Re-derive the game after ingesting. Live capture may set this False "
            "on most hands and True periodically, since a rebuild is O(game)."
        ),
    )


#: Who may show these pages in a frame: the extension (the side panel embeds the
#: chart), these pages themselves, and PokerNow, under which the floating HUD's
#: frame sits. Any other site could frame /players invisibly and steer a click
#: onto merge or rename -- and that click, coming from the page itself, carries
#: the write header.
FRAME_ANCESTORS = "frame-ancestors 'self' chrome-extension: " + " ".join(
    f"https://{h}" for h in ("www.pokernow.com", "pokernow.com", "www.pokernow.club", "pokernow.club")
)


def _page(name: str) -> HTMLResponse:
    """One of the static pages.

    Re-read from disk on every request, with `no-store` so the browser holds no
    old copy -- but the Python behind it is loaded once, so after changing stat
    code, restart the server (`pnt service restart`).
    """
    return HTMLResponse(
        (STATIC / name).read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store", "Content-Security-Policy": FRAME_ANCESTORS},
    )


@app.get("/", include_in_schema=False)
def index() -> HTMLResponse:
    """The front door: what is in the database and where everything is.

    Without it, `127.0.0.1:52000` answered 404 and every page had to be reached by
    typing its path -- fine for whoever built it, useless for anyone else.
    """
    return _page("index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """The extension's chip icon, so these pages' tabs carry it too.

    Browsers ask for /favicon.ico on their own; without this route every tab
    showed the default globe. A PNG under the .ico name is fine for them.
    """
    return Response((ICONS / "icon32.png").read_bytes(), media_type="image/png")


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
        # Which process answered: `pnt service restart` tells the new server from
        # one that outlived it on the same port.
        "pid": os.getpid(),
        "extension_build": EXTENSION_BUILD,
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
    # The oldest line stored for the game: the extension's history walk jumps to it
    # when it meets stored lines, rather than assuming everything older is stored.
    oldest = conn.execute(
        "SELECT MIN(ord) FROM raw_entries WHERE game_id = ?", (req.game_id,)
    ).fetchone()[0]
    out = {"game_id": req.game_id, "offered": offered, "new": n_new, "oldest": oldest}
    if req.rebuild and n_new:
        out |= rebuild_game(conn, req.game_id)
        _save_log(conn, req.game_id)
    return out


@app.post("/rebuild/{game_id}")
def rebuild(game_id: GameId) -> dict:
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


@app.get("/players/{alias:path}/allin")
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


@app.get("/players/{alias:path}/review")
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

    `notes` is what you have written about the player (`POST /players/{alias}/note`)
    and `notes_at` when; both null when you have written nothing.
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
        "SELECT p.alias, p.notes, p.notes_at, pi.pn_id, pi.last_seen_name, pi.first_seen_at,"
        " pi.last_seen_at"
        " FROM players p JOIN player_identities pi ON pi.player_id = p.player_id"
        " ORDER BY p.alias, pi.pn_id"
    ):
        noted = bool((r["notes"] or "").strip())
        entry = out.setdefault(
            r["alias"],
            {
                "alias": r["alias"],
                "n_ids": 0,
                "hands": 0,
                "notes": r["notes"] if noted else None,
                "notes_at": r["notes_at"] if noted else None,
                "identities": [],
            },
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


class PlayerNoteRequest(BaseModel):
    #: What you know about them. Empty, or nothing but spaces, clears the note.
    note: str = ""


@app.post("/players/{alias:path}/note")
def write_player_note(alias: str, req: Annotated[PlayerNoteRequest, Body()]) -> dict:
    """Write down what you know about a player, or clear the note.

    One note per person, not per PokerNow ID: it is kept on the canonical player,
    so it follows them across devices, and the merges, alias re-syncs and game
    deletions that remove player rows carry it along (see importer.py). Shown on
    the players page, beside their name on the stats table and the chart, and on
    their HUD card.
    """
    try:
        note = set_player_note(db(), alias, req.note)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "alias": alias,
        "note": note["note"] if note else None,
        "noted_at": note["noted_at"] if note else None,
    }


@app.get("/players/{alias:path}/positions")
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
        # Cached: every tab of the chart asks for the same player's hands, and
        # deriving them afresh cost seconds per click on a 10,000-hand player.
        facts = facts_cached(conn, alias, game)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [f for f in facts if pred(f)] if pred is not None else facts


@app.get("/players/{alias:path}/stats")
def player_stats(
    alias: str,
    filter: Annotated[str | None, Query(description="e.g. 'srp,flop=ace_high'")] = None,
    game: str | None = None,
) -> dict:
    """One player's stats inside a spot -- what the chart page's postflop strip reads."""
    return {"player": alias, "filter": filter, **aggregate(_spot_facts(alias, filter, game))}


@app.get("/players/{alias:path}/range")
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


@app.get("/players/{alias:path}/sizing")
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


@app.get("/players/{alias:path}/hands")
def player_hands(
    alias: str,
    filter: Annotated[str | None, Query(description="e.g. 'cbet_flop=overbet'")] = None,
    game: str | None = None,
) -> dict:
    """Every hand in a spot as a compact row, newest first. Replay one with /hands/{id}."""
    # One connection for both halves: db() opens a fresh one per call.
    conn = db()
    facts = _spot_facts(alias, filter, game, conn)
    rows = hand_list(facts, display_names(conn))
    return {"player": alias, "filter": filter, "hands": _with_marks(conn, rows, game)}


def _with_marks(conn, rows: list[dict], game: str | None) -> list[dict]:
    """Rows with the marks, notes and manual flags the review rows carry, so any
    hand -- flagged or not -- can be ticked off and written on from the session view."""
    marks = reviewed_marks(conn, game)
    notes = hand_notes(conn, game)
    flags = flagged_hands(conn, game)
    for r in rows:
        key = (r["game_id"], r["hand_number"])
        note = notes.get(key) or {}
        r.update(
            reviewed=key in marks,
            reviewed_at=marks.get(key),
            note=note.get("note"),
            noted_at=note.get("noted_at"),
            flagged=key in flags,
            flagged_at=flags.get(key),
        )
    return rows


@app.get("/players/{alias:path}/flagged")
def player_flagged(
    alias: str,
    filter: Annotated[str | None, Query(description="e.g. 'srp,vs=henry'")] = None,
    game: str | None = None,
) -> dict:
    """The hands of one player's you flagged for manual review, newest first.

    A flagged hand the rebuild has not reached yet is not listed until it has:
    there is no row to draw before then.
    """
    conn = db()
    flags = flagged_hands(conn, game)
    facts = _spot_facts(alias, filter, game, conn)
    facts = [f for f in facts if (f.game_id, f.hand_number) in flags]
    rows = hand_list(facts, display_names(conn))
    return {"player": alias, "filter": filter, "hands": _with_marks(conn, rows, game)}


@app.get("/players/{alias:path}/sat-out")
def player_sat_out(alias: str, game: str) -> dict:
    """The hands of one game the player was not dealt into: the session view's "Sat out".

    No filter: every filter describes the player's own spot, and they had none here.
    """
    conn = db()
    try:
        rows = sat_out_list(conn, alias, game, display_names(conn))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"player": alias, "game": game, "hands": _with_marks(conn, rows, game)}


@app.get("/players/{alias:path}/games")
def player_game_list(alias: str) -> list[dict]:
    """Every game one player was dealt into, newest first: the session view's picker."""
    try:
        return player_games(db(), alias)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/players/{alias:path}/tags")
def player_tags(
    alias: str,
    filter: Annotated[str | None, Query(description="e.g. 'players>=4'")] = None,
    game: str | None = None,
) -> dict:
    """One player's archetype and exploit tags, judged on the hands in a spot.

    Each tag carries the count it was judged on and the filter that puts those
    hands on the chart. See SPEC.md, "Tags", for every rule and threshold.

    With no filter, the voluntary-show fun tags come last: shows are not `Facts`,
    so a spot cannot narrow them.
    """
    from pnt.stats.shows import with_show_tags

    out = tags_for(_spot_facts(alias, filter, game))
    if not filter:
        out = with_show_tags(db(), alias, out, game)
    return {"player": alias, "filter": filter, **out}


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


def _hand_key(conn, hand_id: int):
    """The (game_id, hand_number) a hand is stored under, or 404.

    Both judgement writes take a `hand_id` -- that is what a row on the page
    already holds -- and store what they are told under the hand's own number in
    its game, which a rebuild does not move. See schema.sql.
    """
    row = conn.execute(
        "SELECT game_id, hand_number FROM hands WHERE hand_id = ?", (hand_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="no such hand")
    return row


class ReviewedRequest(BaseModel):
    reviewed: bool = True


@app.post("/hands/{hand_id}/reviewed")
def set_reviewed(hand_id: int, req: Annotated[ReviewedRequest, Body()]) -> dict:
    """Mark one hand as reviewed, or clear the mark.

    Stored under the hand's (game_id, hand_number) -- see `_hand_key` -- so the
    mark survives the rebuild that gives the hand a new id.
    """
    conn = db()
    row = _hand_key(conn, hand_id)
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


class FlagRequest(BaseModel):
    flagged: bool = True


@app.post("/games/{game_id}/hands/{hand_number}/flag")
def write_flag(game_id: GameId, hand_number: int, req: Annotated[FlagRequest, Body()]) -> dict:
    """Flag one hand for manual review, or clear the flag.

    Addressed by the log's own (game_id, hand_number) rather than a hand_id: the
    HUD flags the hand that just ended, which the rebuild may not have derived yet.
    """
    try:
        at = set_flag(db(), game_id, hand_number, req.flagged)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"game_id": game_id, "hand_number": hand_number, "flagged": at is not None, "flagged_at": at}


@app.get("/flagged")
def flagged(game: str = Query(None, description="Restrict to one game_id.")) -> list[dict]:
    """Every hand flagged for manual review, newest flag first."""
    flags = flagged_hands(db(), game)
    return [
        {"game_id": g, "hand_number": n, "flagged_at": at}
        for (g, n), at in sorted(flags.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    ]


class NoteRequest(BaseModel):
    #: What you found in the hand. Empty, or nothing but spaces, clears the note.
    note: str = ""


@app.post("/hands/{hand_id}/note")
def write_note(hand_id: int, req: Annotated[NoteRequest, Body()]) -> dict:
    """Write down what went wrong in one hand, or clear the note.

    Keyed like the mark, and addressed by `hand_id` for the same reason. Setting a
    note does not mark the hand reviewed and clearing the mark does not erase the
    note: the two are separate judgements, and a hand you have written a question
    about is often one you have not finished with.
    """
    conn = db()
    row = _hand_key(conn, hand_id)
    note = set_note(conn, row["game_id"], row["hand_number"], req.note)
    return {
        "hand_id": hand_id,
        "game_id": row["game_id"],
        "hand_number": row["hand_number"],
        "note": note["note"] if note else None,
        "noted_at": note["noted_at"] if note else None,
    }


@app.get("/notes")
def notes(game: str = Query(None, description="Restrict to one game_id.")) -> list[dict]:
    """Every hand you have written a note on, newest note first."""
    rows = hand_notes(db(), game)
    return [
        {"game_id": g, "hand_number": n, **v}
        for (g, n), v in sorted(rows.items(), key=lambda kv: (kv[1]["noted_at"], kv[0]), reverse=True)
    ]


@app.get("/hud/{game_id}")
def hud(game_id: GameId) -> dict:
    """Stats for everyone currently at a table, keyed by PokerNow ID.

    Keyed by `pn_id` rather than seat on purpose: the overlay must re-resolve
    seat -> player every hand from the live dealt-in roster, because seats are
    reused as players come and go.

    Each seat carries two reports with identical keys: `stats`, lifetime across
    every game and merged identity, and `session`, this game alone -- the pair
    the overlay prints side by side so a player drifting from their history is
    visible while it happens. Both come from `report()`, whose cache is keyed on
    the game, so a 30-second poll re-derives nothing between hands.

    `tags` is the player's lifetime archetype and exploit tags (SPEC.md, "Tags").
    They read the same per-alias `Facts` cache `/live` fills on every poll, so
    they cost a few milliseconds on top of it.

    `note` is what you have written about the player, or null -- the one thing
    on the card no log could have told it.
    """
    from pnt.stats.shows import with_show_tags

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
    notes = player_notes(conn)
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
                # Lifetime tags; a seat with no identity yet has none.
                "tags": with_show_tags(conn, r["alias"], tags_for(facts_cached(conn, r["alias"]) if r["alias"] else [])),
                # What you have written about them; null when nothing.
                "note": notes.get(r["alias"], {}).get("note"),
                # Whether their next hand here falls inside a post-loss window
                # (SPEC.md, "Session dynamics"), and which loss opened it.
                **_post_loss(conn, r["alias"], game_id),
            }
            for r in seated
        ],
    }


@app.get("/live/{game_id}")
def live(
    game_id: GameId,
    min_hands: Annotated[
        int, Query(alias="min", ge=1, description="hands a spot needs before it counts")
    ] = 1,
    min_known: Annotated[
        int,
        Query(alias="known", ge=0, description="shown hands a spot narrowed to this board's texture must keep"),
    ] = 5,
) -> dict:
    """The hand in progress: everyone's spot right now, resolved against their history.

    Read straight from the raw lines, so it needs no rebuild and is current as of
    the last poll. Between hands it is `{"hand": null}`. Each player still in
    carries `resolved`: the closest spot with at least `min` hands behind it, the
    filter that names it (paste it into the chart), how they played it there, and
    what they showed up with per decision (`showings`), read on the live street.
    A postflop spot is narrowed to boards like this one while at least `known`
    shown hands survive the narrowing.
    """
    return snapshot(db(), game_id, min_hands=min_hands, min_known=min_known)


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


@app.get("/players/{alias:path}/results")
def player_results(
    alias: str,
    filter: Annotated[str | None, Query(description="e.g. 'position=BTN'")] = None,
    game: str | None = None,
) -> dict:
    """One player's big blinds won and lost inside a spot, by pot type, seat and position.

    Every row carries the filter terms that find its hands, to add to `filter`.
    See SPEC.md, "Results by pot type".
    """
    # Imported here rather than up top, where every feature's imports meet: one
    # module, read by this endpoint alone.
    from pnt.stats.results import results_breakdown

    return {"player": alias, "filter": filter, **results_breakdown(_spot_facts(alias, filter, game))}


# ---- the action-line explorer -------------------------------------------------

_LINES_MODE = f"^({'|'.join(MODES)})$"


def _lines(facts: list[Facts], mode: str, street: str | None, min: int) -> dict:
    try:
        return line_breakdown(facts, mode, street or None, min)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/players/{alias:path}/lines")
def player_lines(
    alias: str,
    filter: Annotated[str | None, Query(description="e.g. 'srp,pfa'")] = None,
    mode: Annotated[str, Query(pattern=_LINES_MODE, description="own: their line; action: the whole street")] = "own",
    street: Annotated[str | None, Query(pattern="^(flop|turn|river)?$")] = None,
    min: Annotated[int, Query(ge=1, description="Hide lines with fewer hands.")] = DEFAULT_MIN,
    game: str | None = None,
) -> dict:
    """One player's hands in a spot, grouped by the line they took after the flop,
    with how often each line was nutted, strong, marginal, a draw or air when shown.

    Every group carries the `filter` that finds exactly its hands. See SPEC.md,
    "Strength by line".
    """
    facts = _spot_facts(alias, filter, game)
    return {"player": alias, "filter": filter, **_lines(facts, mode, street, min)}


@app.get("/lines")
def pool_lines(
    filter: Annotated[str | None, Query(description="e.g. 'srp,pfa'")] = None,
    mode: Annotated[str, Query(pattern=_LINES_MODE)] = "own",
    street: Annotated[str | None, Query(pattern="^(flop|turn|river)?$")] = None,
    min: Annotated[int, Query(ge=1)] = DEFAULT_MIN,
    game: str | None = None,
) -> dict:
    """The same over every player's hands: what the pool has behind each line.

    Hand ids are left out: one hand is a row per player in it, and a list of
    every hand in the database is not something a view can use.
    """
    conn = db()
    pred = _predicate(conn, filter)
    facts = line_pool_facts(conn, game)
    if pred is not None:
        facts = [f for f in facts if pred(f)]
    out = _lines(facts, mode, street, min)
    for g in out["groups"]:
        del g["hand_ids"]
    return {"player": None, "filter": filter, **out}
# --- bluff:value -----------------------------------------------------------------


@app.get("/players/{alias:path}/bluffs")
def player_bluffs(
    alias: str,
    filter: Annotated[str | None, Query(description="e.g. 'srp,vs=henry'")] = None,
    game: str | None = None,
) -> dict:
    """One player's bluff:value by kind of aggression: bets by size, check-raises,
    raises and jams per street, with the shown hands read on the street of the
    action. Each row carries its hand ids, so the chart lists them on a click.
    See SPEC.md, "Bluff:value"."""
    facts = _spot_facts(alias, filter, game)
    return {"scope": "player", "player": alias, "filter": filter, **bluff_value(facts)}


def _pool_spot(conn: sqlite3.Connection, filter: str | None, game: str | None) -> list[Facts]:
    pred = _predicate(conn, filter)
    facts = bluff_pool_facts(conn, game)
    return [f for f in facts if pred(f)] if pred is not None else facts


@app.get("/bluffs")
def pool_bluffs(
    filter: Annotated[str | None, Query(description="e.g. 'srp'")] = None,
    game: str | None = None,
) -> dict:
    """The same table over the pool: every player, every hand. A hand counts once
    per player who took the line in it. No hand ids, since one names nobody; the
    pool's hands are listed by /bluffs/hands."""
    conn = db()
    facts = _pool_spot(conn, filter, game)
    players = len({f.pn_id for f in facts})
    return {"scope": "pool", "player": None, "filter": filter, "players": players,
            **bluff_value(facts, ids=False)}


@app.get("/bluffs/hands")
def pool_bluff_hands(
    category: Annotated[str, Query(description="a row's key, e.g. 'bet_river_overbet'")],
    cls: Annotated[str | None, Query(description="value, marginal, semi_bluff or bluff")] = None,
    filter: Annotated[str | None, Query(description="the spot the table was drawn in")] = None,
    game: str | None = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> dict:
    """The pool's hands behind one row of /bluffs, or one class of it, newest first,
    each with the player who took the line. By key rather than filter, so the one
    row without a filter, all aggression, lists the same way as the rest."""
    conn = db()
    try:
        rows = members(_pool_spot(conn, filter, game), category, cls)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    names = display_names(conn)
    # hand_list sorts newest first, and the sort is stable: sorted the same way
    # here, its rows line up with these facts, which is where the name comes from.
    rows.sort(key=lambda x: (x.ts or "", x.hand_id), reverse=True)
    total = len(rows)
    rows = rows[:limit]
    hands = hand_list(rows, names)
    for h, f in zip(hands, rows):
        h["player"] = names.get(f.pn_id, f.pn_id)
    cat = CATEGORY[category]
    return {"category": category, "cls": cls, "label": cat["label"], "street": cat["street"],
            "filter": filter, "total": total, "hands": _with_marks(conn, hands, game)}
@app.get("/players/{alias:path}/fold_equity")
def player_fold_equity(
    alias: str,
    filter: Annotated[str | None, Query(description="e.g. 'srp,flop=ace_high'")] = None,
    game: str | None = None,
) -> dict:
    """Fold equity by bet size for one player, within a spot: what each size of
    their bets got against its break-even, and how they defend against each size
    against its MDF. Rows carry their hand ids for the chart's drill-down, and the
    filter that finds them. See SPEC.md, "Fold equity by bet size"."""
    facts = _spot_facts(alias, filter, game)
    return {"player": alias, "filter": filter, **fold_equity(facts, with_ids=True)}


@app.get("/fold_equity")
def pool_fold_equity(
    filter: Annotated[str | None, Query(description="e.g. 'srp'")] = None,
    game: str | None = None,
) -> dict:
    """The same for the whole pool: every player's bets and every player's
    defense, summed. No hand ids -- one hand is several players' rows here."""
    conn = db()
    facts = fold_pool_facts(conn, game, _predicate(conn, filter))
    return {"player": None, "filter": filter, **fold_equity(facts)}


# ---- strength by street (slowplays) ------------------------------------------
# Imported inside the endpoints so this block stands alone. See SPEC.md,
# "Strength by street".


@app.get("/players/{alias:path}/slowplay")
def player_slowplay(
    alias: str,
    street: Annotated[str, Query(pattern="^(flop|turn|river)$")] = "flop",
    floor: Annotated[str, Query(description="The weakest made hand counted, e.g. 'set' or 'two_pair'.")] = "set",
    filter: Annotated[str | None, Query(description="e.g. 'srp,players=2'")] = None,
    game: str | None = None,
) -> dict:
    """What a player did on one street with each strong made hand: bet, raised,
    check-raised, or slowplayed it with a check or a call. Every cell carries the
    filter that finds its hands. Built from shown hands, so read coverage first."""
    from pnt.stats.slowplay import slowplay

    facts = _spot_facts(alias, filter, game)
    try:
        out = slowplay(facts, street, floor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"player": alias, "filter": filter, **out}


@app.get("/slowplay")
def pool_slowplay(
    street: Annotated[str, Query(pattern="^(flop|turn|river)$")] = "flop",
    floor: Annotated[str, Query(description="The weakest made hand counted, e.g. 'set' or 'two_pair'.")] = "set",
    filter: Annotated[str | None, Query(description="e.g. 'srp,players=2'")] = None,
    game: str | None = None,
    min: Annotated[int, Query(ge=1, description="Strong hands a player needs to be listed.")] = 1,
) -> dict:
    """The same table over everyone's hands pooled, plus each player's total row
    ranked by slowplay share: who traps. Derived afresh, like a filtered /stats."""
    from pnt.stats.queries import facts_by_player
    from pnt.stats.slowplay import by_player, check_floor, slowplay

    conn = db()
    pred = _predicate(conn, filter)
    try:
        floor = check_floor(floor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    grouped, aliases, _ = facts_by_player(conn, game)
    if pred is not None:
        grouped = {pid: [f for f in facts if pred(f)] for pid, facts in grouped.items()}
    out = slowplay([f for facts in grouped.values() for f in facts], street, floor)
    return {"filter": filter, **out, "players": by_player(grouped, aliases, street, floor, min)}


# ---- 7-2 bounty ledger -------------------------------------------------------
# Imported inside the endpoint so this block stands alone. See SPEC.md,
# "7-2 bounty ledger".


@app.get("/bounties")
def bounties(game: Annotated[str | None, Query(description="Restrict to one game_id.")] = None) -> dict:
    """Who has collected and paid the 7-2 side bet, ranked by net, and how often
    hero played 72 when dealt it. Hero only: anyone else's folded 72 is unseen.
    The ledger's net sums to zero across players; `balance` says if it does not."""
    from pnt.stats.bounty import ledger

    return ledger(db(), game)


# --- voluntary shows -------------------------------------------------------------


@app.get("/players/{alias:path}/shows")
def player_shows(alias: str, game: str | None = None) -> dict:
    """The hands one player showed without being asked: after a fold, after a pot
    nobody contested, or after mucking at showdown. Two-card shows are classed
    strong, marginal, draw or air on the board as it stood; one-card shows are
    counted only. No filter: shows are not `Facts`, so each row is listed by its
    hand. See SPEC.md, "Voluntary shows"."""
    from pnt.stats.shows import voluntary_shows

    try:
        return voluntary_shows(db(), alias, game)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---- head to head ------------------------------------------------------------
# Imported inside the endpoints so this block stands alone. See SPEC.md,
# "Head to head".


@app.get("/players/{alias:path}/h2h")
def player_h2h(alias: str, game: str | None = None) -> dict:
    """The money between one player and each opponent, biggest either way first.
    `net_bb` is what the player won from them; each row's `filter` opens the hands
    against them on the chart and `n` is how many it finds."""
    from pnt.stats.h2h import player_h2h as h2h_rows

    try:
        return h2h_rows(db(), alias, game)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/h2h")
def pool_h2h(game: str | None = None) -> dict:
    """Who pays whom: every pair in the pool, as a matrix read from the row's side.
    No hand ids -- one cell is two players' hands."""
    from pnt.stats.h2h import pool_h2h as h2h_matrix

    return h2h_matrix(db(), game)


# ---- red line and blue line ----------------------------------------------------
# Imported inside the endpoint so this block stands alone. See SPEC.md, "Red and
# blue lines".


@app.get("/players/{alias:path}/winnings")
def player_winnings(
    alias: str,
    filter: Annotated[str | None, Query(description="e.g. 'position=BTN'")] = None,
    game: str | None = None,
    adjusted: Annotated[bool, Query(description="Add the all-in adjusted line.")] = False,
) -> dict:
    """One player's cumulative net bb in hand order, split on showdown: the blue
    line is the hands they took to showdown, the red line every other hand. With
    `adjusted`, a third line pays each all-in showdown by equity. Plus net bb per
    session. Incomplete hands are in no line, as in bb/100. See SPEC.md, "Red and
    blue lines"."""
    from pnt.stats.winnings import allin_adjusted, winnings_series

    conn = db()
    facts = _spot_facts(alias, filter, game, conn)
    adj = allin_adjusted(conn, facts) if adjusted else None
    return {"player": alias, "filter": filter, **winnings_series(facts, adj)}


# ---- tilt and session dynamics ------------------------------------------------
# Imported inside the endpoint so this block stands alone. See SPEC.md, "Session
# dynamics".


@app.get("/players/{alias:path}/tilt")
def player_tilt(
    alias: str,
    filter: Annotated[str | None, Query(description="e.g. 'position=BTN'")] = None,
    game: str | None = None,
) -> dict:
    """One player's VPIP, PFR, aggression and bb/100 after a big loss against every
    other hand, and by hours into the session. Every row carries the filter that
    finds its hands; `clock` is how many hands have a timestamp to band by."""
    from pnt.stats.tilt import tilt_report

    return {"player": alias, "filter": filter, **tilt_report(_spot_facts(alias, filter, game))}


def _post_loss(conn: sqlite3.Connection, alias: str | None, game_id: str) -> dict:
    """A HUD seat's `post_loss`: whether their next hand in this game falls inside
    a post-loss window, with how many hands since the loss that opened it and its
    size. Read from the lifetime Facts the seat's tags already fetched -- annotated
    per game, so this game's rows carry this game's context only."""
    from pnt.stats.tilt import next_hand

    out = next_hand(facts_cached(conn, alias) if alias else [], game_id)
    return {"post_loss": out["post_loss"], "post_loss_since": out["since"], "post_loss_bb": out["loss_bb"]}
