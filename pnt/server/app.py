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
import ctypes
import functools
import gc
import logging
import os
import sqlite3
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote, urlsplit

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi import Path as FastApiPath
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from pnt import update
from pnt.db.conn import connect, generation, path_of
from pnt.db.userdata import FLAGGED_TAG, tag_resolver
from pnt.ingest import log_folder, sync
from pnt.ingest import sample as samplemod
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
from pnt.server import annotations
from pnt.stats import strength
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
from pnt.stats.pool import pool_for, pool_from_facts
from pnt.stats.pots import DEFAULT_DAYS, DEFAULT_LIMIT, DEFAULT_MIN_POT, big_pots
from pnt.stats.queries import (
    SPOT_ANSWERS,
    aggregate,
    display_names,
    facts_cached,
    hand_list,
    player_games,
    positional_report,
    remembered,
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
#: The front page's Data switch: on, the pages show the bundled sample, built into
#: a database of its own (pnt/ingest/sample.py); DB_PATH stays yours, and capture
#: keeps writing there. Never saved: every start is back on your own data.
SAMPLE_ON = False
#: The sample's database while SAMPLE_ON (`sample.database()`, read on the switch).
SAMPLE_PATH: Path | None = None
STATIC = Path(__file__).parent / "static"
ICONS = EXTENSION_DIR / "icons"

#: Live capture also keeps each game's CSV in the log folder, so the folder stays a
#: running record: every game in the database, as a file `pnt import` could rebuild
#: it from. On unless PNT_SAVE_LOGS=0.
SAVE_LOGS = log_folder.SAVE_LOGS

log = logging.getLogger(__name__)

#: Seconds between log-folder syncs while the server runs; 0 turns them off. Each
#: pass is a stat per file, so a short interval costs nothing between changes.
SYNC_SECONDS = float(os.environ.get("PNT_SYNC_SECONDS", "5"))

#: Whether to ask GitHub if this install is behind; PNT_UPDATE_CHECK=0 stops it.
UPDATE_CHECK = os.environ.get("PNT_UPDATE_CHECK", "1") != "0"
UPDATE_CHECK_SECONDS = 6 * 3600
#: Commits this install lacks (see update.py), None until GitHub has said.
UPDATE_BEHIND: int | None = None

#: Running from a source checkout (`pip install -e .`), not an archive install.
#: The note overlay and its /annotations routes exist only here: they are for
#: whoever works on the code, and a stray \ key should not reach anyone else.
DEV = update.checkout_root() is not None


def _update_loop(stop: threading.Event, sha: str) -> None:
    """Keep UPDATE_BEHIND current until `stop` is set. A failed check keeps the
    last answer: offline for an afternoon is no reason to drop the banner."""
    global UPDATE_BEHIND
    branch = update.source_branch(update.installed_source())
    while True:
        behind = update.commits_behind(sha, branch)
        if behind is not None:
            UPDATE_BEHIND = behind
        if stop.wait(UPDATE_CHECK_SECONDS):
            return


def _sync_loop(stop: threading.Event) -> None:
    """Keep the database in step with the log folder until `stop` is set.

    Its own connection: sqlite3 connections belong to the thread that opened them.
    Never raises -- a sync that fails is logged and tried again next interval, and
    so is opening the connection, which a migration or a rebuild can hold up past
    `busy_timeout` at start.
    """
    conn = None
    opened = None
    skip: dict[str, tuple[int, int]] = {}
    try:
        while True:
            try:
                if conn is not None and opened != DB_PATH:  # switched on the front page
                    conn.close()
                    conn = None
                if conn is None:
                    opened = DB_PATH
                    conn = capture_db()  # your logs, even while the pages show the sample
                out = sync.sync_folder(conn, log_folder.LOG_DIR, skip)
                for s in out.imported:
                    if s["entries_new"]:
                        log.info("imported %s: %d new entries", s["file"], s["entries_new"])
                for path, why in out.failed.items():
                    log.warning("could not import %s: %s", path, why)
            except Exception:
                log.exception("log folder sync failed")
                if conn is not None and conn.in_transaction:
                    conn.rollback()
            if stop.wait(SYNC_SECONDS):
                return
    finally:
        if conn is not None:
            conn.close()


# Requests being answered right now. The warm-up waits whenever there are any:
# it is ten seconds of CPU on one GIL, and a chart opened just after a start
# would otherwise take that long rather than its own second or two.
_busy = 0
_busy_lock = threading.Lock()
_idle = threading.Event()
_idle.set()


def _wait_idle() -> None:
    _idle.wait()


class _Switched(Exception):
    """The database was switched under a warm-up; the new one has its own."""


#: Database switches so far. Counted, not compared by path: switching A -> B -> A
#: while a warm-up waits would leave it seeing A again, beside the new one for A.
_switches = 0


#: The databases whose lifetime report a warm-up is deriving right now: what a cold
#: `/hud` waits on, and what `/health`'s `warming` says. Not the rest of the
#: warm-up, which can run for minutes after the HUD has its stats.
_warming: set[Path] = set()
#: (database, switch count) of each warm-up running, so one is never started twice
#: on a file -- the switch count, so A -> B -> A starts a fresh one on A.
_warm_running: set[tuple[Path, int]] = set()
_warm_lock = threading.Lock()


def _warm_strength(path: Path | None = None, wanted: Callable[[], bool] | None = None) -> None:
    """Derive the lifetime report, measure every shown holding's hand strength the
    database does not have yet (a no-op once it is complete, a minute or two the
    first time; stats/strength.py), then count the pool the tags are judged
    against, so the first HUD after a start pays for none of them. The report
    goes first: it needs no strengths, and it is most of a cold `/hud`.

    Stops at the next pause once the front page switches the database, or once
    `wanted` says no (the sample, once the pages leave it): left running, it would
    burn CPU on a database nobody reads, and its flush would take values the new
    warm-up computed into the old file. A database already being warmed is left to
    the warm-up on it."""
    path, started = path or DB_PATH, _switches
    with _warm_lock:
        if (path, started) in _warm_running:
            return
        _warm_running.add((path, started))

    def between() -> None:
        _wait_idle()
        if _switches != started or (wanted is not None and not wanted()):
            raise _Switched

    conn = None
    try:
        conn = connect(path)
        between()
        _warming.add(path)
        try:
            report(conn)
        finally:
            _warming.discard(path)
        added = strength.warm(conn, between=between)
        if added:
            log.info("hand strength: %d stored", added)
        pool_for(conn, between=between)
        _warm_hero(conn, between)
    except _Switched:
        log.info("warm-up of %s stopped: database switched", path)
    except Exception:
        log.exception("warm-up failed")
    finally:
        with _warm_lock:
            _warm_running.discard((path, started))
        if conn is not None:
            conn.close()
        _release_memory()


#: glibc's malloc_trim, or None off glibc (Windows, macOS).
try:
    _TRIM = ctypes.CDLL("libc.so.6").malloc_trim
except (OSError, AttributeError):
    _TRIM = None


def _release_memory() -> None:
    """Give the warm-up's garbage back to the system. Deriving every game peaks
    at several times what the caches keep, and glibc keeps the freed pages: on
    the phone copy's 1 GB VM they ended up in swap. No gc.collect() first: on
    this thread it could free the folder picker's Tk objects (see there)."""
    if _TRIM is not None:
        _TRIM(0)


def _warm_hero(conn: sqlite3.Connection, between) -> None:
    """The hero's slow first opens, asked the way the pages ask them: the chart's
    first view (stats and the preflop range, no filter), the Hand review (seconds
    to flag every hand), the spot tree at the chart's depth 4 and 5-hand floor
    (TREE_DEPTH, TREE_MIN in chart.html), Results, Tilt and the Tendencies page.
    Each lands in the cache its endpoint reads."""
    from pnt.stats.leaks import hero_of
    from pnt.stats.results import results_breakdown
    from pnt.stats.review import _review_base
    from pnt.stats.tendencies import tendencies
    from pnt.stats.tilt import tilt_report
    from pnt.stats.tree import tree_for

    hero = hero_of(conn)
    if hero is None:
        return
    between()
    _spot_stats(conn, hero, None, None)
    between()
    _spot_range(conn, hero, None, "preflop", None)
    between()
    _review_base(conn, hero, None)
    between()
    tree_for(conn, hero, max_depth=4, min_hands=5)
    # Each in the scope its endpoint reads, or the warmed answer would not be the
    # one the endpoint gives cold: Results counts PLO's money, Tilt reads play only.
    between()
    remembered(conn, ("results", hero, None, None), lambda: results_breakdown(facts_cached(conn, hero, games="money")))
    between()
    remembered(conn, ("tilt", hero, None, None), lambda: tilt_report(facts_cached(conn, hero)))
    between()
    remembered(conn, ("tendencies", None, hero), lambda: tendencies(conn, None))


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    stop = threading.Event()
    worker = None
    threading.Thread(target=_warm_strength, name="strength-warm", daemon=True).start()
    if SYNC_SECONDS > 0:
        worker = threading.Thread(target=_sync_loop, args=(stop,), name="log-sync", daemon=True)
        worker.start()
    sha = update.installed_commit() if UPDATE_CHECK else None
    if sha is not None:
        threading.Thread(target=_update_loop, args=(stop, sha), name="update-check", daemon=True).start()
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
async def _count_busy(request: Request, call_next):
    global _busy
    if request.url.path == "/settings/paths/browse":
        # Open as long as the dialog is on screen, and the warm-up pauses holding a
        # read snapshot, so counting it would stall checkpoints for as long.
        return await call_next(request)
    with _busy_lock:
        _busy += 1
        _idle.clear()
    try:
        return await call_next(request)
    finally:
        with _busy_lock:
            _busy -= 1
            if not _busy:
                _idle.set()


@app.middleware("http")
async def _writes_from_here_only(request: Request, call_next):
    if request.method not in _READS:
        origin = request.headers.get("origin")
        if WRITE_HEADER not in request.headers or (origin is not None and not _trusted_origin(origin)):
            # Only an extension from before the header writes from chrome-extension://
            # without it: Chrome keeps running that old worker after an update until
            # it is reloaded by hand. Short and front-loaded: the HUD bar cuts it off.
            stale = origin is not None and urlsplit(origin).scheme == "chrome-extension"
            detail = (
                "Old extension: reload it at chrome://extensions"
                if stale and WRITE_HEADER not in request.headers
                else f"writes need the {WRITE_HEADER} header and a local origin"
            )
            return JSONResponse({"detail": detail}, status_code=403)
    return await call_next(request)


app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

#: A PokerNow game ID, as `log_folder.GAME_ID`: the one shape allowed near a file name.
GAME_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
GameId = Annotated[str, FastApiPath(pattern=GAME_ID_PATTERN)]


def db():
    """The database the pages read: the bundled sample's while it is switched on."""
    return connect(SAMPLE_PATH if SAMPLE_ON and SAMPLE_PATH is not None else DB_PATH)


def capture_db():
    """Your own database, whatever the pages show: live capture, the log-folder
    sync and the HUD never read or write the sample."""
    return connect(DB_PATH)


#: `?capture=1` on a write the HUD makes (its 🚩, a card's ✎): its table and its
#: players are in your own database even while the pages show the sample, where
#: the same write from a page is about what the page is showing.
CaptureOpt = Annotated[bool, Query(description="The HUD's write: always to your own database.")]


def _db_for(capture: bool):
    return capture_db() if capture else db()


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
    html = (STATIC / name).read_text(encoding="utf-8")
    # The note overlay rides on every page from here rather than a tag in each
    # file, on a source checkout only (DEV); off until the \ key or ?annotate.
    if DEV:
        html = html.replace("</body>", '<script src="/annotate.js" defer></script>\n</body>', 1)
    return HTMLResponse(
        html,
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


@app.get("/tendencies.html", include_in_schema=False)
def tendencies_page() -> HTMLResponse:
    """Your tags, and the tags most of your opponents share, from /tendencies."""
    return _page("tendencies.html")


@app.get("/allin.html", include_in_schema=False)
def allin_page() -> HTMLResponse:
    """All-in EV: what every player's jams were worth against what they took.
    `/allin` serves this to browsers."""
    return _page("allin.html")


@app.get("/pots.html", include_in_schema=False)
def pots_page() -> HTMLResponse:
    """The biggest pots over a window of days. `/pots` serves this to browsers."""
    return _page("pots.html")


def _script(name: str, folder: Path = STATIC) -> Response:
    return Response(
        (folder / name).read_text(encoding="utf-8"),
        media_type="text/javascript",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/filter-help.js", include_in_schema=False)
def filter_help_script() -> Response:
    """The `?` panel beside every Spot box, shared by the chart, stats and all-in pages."""
    return _script("filter-help.js")


@app.get("/loading.js", include_in_schema=False)
def loading_script() -> Response:
    """Every page's loading bar, seconds counter and what-to-try box when a request
    is slow or the server cannot be reached; loaded first, in each page's <head>."""
    return _script("loading.js")


@app.get("/replay.js", include_in_schema=False)
def replay_script() -> Response:
    """The hand replay renderer, shared by the chart and all-in pages."""
    return _script("replay.js")


def _dev_only() -> None:
    """The note overlay's routes: a 404 outside a source checkout (DEV)."""
    if not DEV:
        raise HTTPException(status_code=404, detail="the note overlay is for source checkouts")


@app.get("/annotate.js", include_in_schema=False, dependencies=[Depends(_dev_only)])
def annotate_script() -> Response:
    """The note overlay. One file in the extension's folder, which the side panel
    loads as its own (an extension page may not run a script from a server)."""
    return _script("annotate.js", EXTENSION_DIR)


@app.get("/filters")
def filters() -> dict:
    """Every term a `filter` parameter understands, grouped, with an example each."""
    return vocabulary()


# ---- term counts ---------------------------------------------------------------
# How many hands each candidate term would leave, for the filter panel. Imported
# inside the endpoint so this block stands alone. See SPEC.md, "Counting terms".


class CountsRequest(BaseModel):
    #: An alias, or None for every player's hands, as `/stats` counts them.
    player: str | None = None
    filter: str = ""
    game: str | None = None
    terms: list[str] = Field(default_factory=list, max_length=200)
    #: The tab's scope (`queries.SCOPES`), so a chip counts what its click would list.
    games: Literal["play", "money", "every"] = "play"


@app.post("/counts")
def counts(req: Annotated[CountsRequest, Body()]) -> dict:
    """`{"base": n, "counts": {term: n}}`: the hands `filter` keeps, and per term
    those `filter,term` keeps; null for a term the parser refuses. A POST only
    because 200 terms do not fit a URL; it writes nothing."""
    from pnt.stats.filters import count_terms
    from pnt.stats.queries import facts_by_player

    conn = db()
    if req.player is None:
        grouped, _, _ = facts_by_player(conn, req.game)
        facts = [f for rows in grouped.values() for f in rows]
    else:
        try:
            facts = facts_cached(conn, req.player, req.game, req.games)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        return count_terms(facts, req.filter, req.terms, display_names(conn), tag_resolver(conn))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _extension_build() -> str | None:
    # Unreadable (the folder mid-update): none, which the extension skips.
    try:
        return extension_build()
    except OSError:
        return None


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
        # The extension files as they are now, not as they were at startup: a
        # checkout edited under a running server once reported the old hash, and
        # the extension, hashing the new files, reloaded itself over and over. A
        # loaded extension that sees this change reloads to match (pnt/update.py).
        "extension_build": _extension_build(),
        # More than 0: `pnt update` would bring this many commits.
        "update_behind": UPDATE_BEHIND,
        # Still deriving your database's lifetime stats after a start: the HUD's
        # first answer is on its way, not lost.
        "warming": DB_PATH in _warming,
        # What the pages show (the counts above are its); capture is always yours.
        "dataset": "sample" if SAMPLE_ON else "own",
        # A source checkout: the HUD switches its note overlay on only then.
        "dev": DEV,
        # The hands the HUD reads from, which the sample never is.
        "capture_hands": _capture_hands() if SAMPLE_ON else row["h"],
    }


def _capture_hands() -> int:
    conn = capture_db()
    try:
        return conn.execute("SELECT COUNT(*) FROM hands").fetchone()[0]
    finally:
        conn.close()


@app.post("/ingest")
def ingest(req: IngestRequest) -> dict:
    """Accept raw log entries. Idempotent: entries dedupe on (game_id, order).

    This is the endpoint the extension calls. Because it takes *raw log lines* --
    the same ones the CSV export contains -- live capture and backfill run through
    one parser and cannot drift apart.
    """
    conn = capture_db()
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
    conn = capture_db()
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
    split: Annotated[list[str] | None, Query(description="sub-spots, e.g. 'position=SB'; repeat for each")] = None,
) -> list[dict] | dict | HTMLResponse:
    """Per-player stats, optionally restricted to a spot.

    With `split` (repeated), the answer is `{rows, splits: [{term, rows}]}`: the
    spot's rows, and the rows within `<filter>,<term>` for each term, all from one
    derivation (`queries.split_report`). A group row is left out for a player
    with no hand in it.

    The filter compiles to a predicate over derived per-hand facts, which is only
    possible because actions are stored raw with street and sequence.

    A browser navigating here asks for HTML and gets the stats page; every other
    caller (the HUD, a script, curl) asks for anything and gets the JSON. The page
    is also at /stats.html, which is what the page itself links to.
    """
    if "text/html" in request.headers.get("accept", ""):
        return _page("stats.html")
    conn = db()
    if split:
        from pnt.stats.queries import split_report

        # The tags read once for every term that names them; the filters compiled
        # only when the answer is not already kept.
        tags = _tag_print(conn) if any(t and "tag" in t for t in (filter, *split)) else None

        def answer() -> dict:
            groups = [_predicate(conn, term) for term in split]
            rows, parts = split_report(conn, game, min_hands, _predicate(conn, filter), groups)
            return {"rows": rows, "splits": [{"term": t, "rows": p} for t, p in zip(split, parts)]}

        return remembered(conn, ("stats", game, _spot_key(conn, filter, tags), min_hands,
                                  tuple(_spot_key(conn, t, tags) for t in split)), answer)
    if not filter:
        return report(conn, game_id=game, min_hands=min_hands)  # cached in queries.py already
    return remembered(conn, ("stats", game, _spot_key(conn, filter), min_hands, ()),
                      lambda: report(conn, game_id=game, min_hands=min_hands, predicate=_predicate(conn, filter)))


def _predicate(conn: sqlite3.Connection, filter: str | None):
    """Compile a spot filter, or 400 on a term the parser does not know."""
    try:
        return parse_filter(filter, display_names(conn), tag_resolver(conn)) if filter else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _spot_key(conn: sqlite3.Connection, filter: str | None, tags: int | None = None) -> str | None:
    """`filter` as a cache key. A `tag=` term reads tags no generation counts, so
    a filter that may hold one carries every tag and flag along with it: `tags`,
    when the caller has already read them (`_tag_print`)."""
    if not filter or "tag" not in filter:
        return filter
    return f"{filter}\x00{_tag_print(conn) if tags is None else tags}"


def _tag_print(conn: sqlite3.Connection) -> int:
    """Every tag and flag, as one number that changes when any of them does."""
    rows = conn.execute(
        "SELECT game_id, hand_number, tag FROM hand_tags UNION ALL"
        " SELECT game_id, hand_number, '' FROM hand_flags ORDER BY 1, 2, 3"
    ).fetchall()
    return hash(tuple(tuple(r) for r in rows))


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
        out = review_hand_list(conn, alias, game, pred)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _with_tags(conn, out["hands"], game)
    return {"filter": filter, **out}


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
def write_player_note(alias: str, req: Annotated[PlayerNoteRequest, Body()], capture: CaptureOpt = False) -> dict:
    """Write down what you know about a player, or clear the note.

    One note per person, not per PokerNow ID: it is kept on the canonical player,
    so it follows them across devices, and the merges, alias re-syncs and game
    deletions that remove player rows carry it along (see importer.py). Shown on
    the players page, beside their name on the stats table and the chart, and on
    their HUD card.
    """
    try:
        note = set_player_note(_db_for(capture), alias, req.note)
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
    alias: str, filter: str | None, game: str | None, conn: sqlite3.Connection | None = None,
    games: str = "play",
) -> list[Facts]:
    """One player's hands in a spot: 400 on a bad filter, 404 on an unknown alias.

    `games` is the scope (`queries.SCOPES`): `play` for every analysis tab,
    `money` for Results and the winnings graph, `every` for the hand lists.
    """
    if conn is None:
        conn = db()
    # The name map is what lets `vs=henry` name a person rather than an ID.
    pred = _predicate(conn, filter)
    try:
        # Cached: every tab of the chart asks for the same player's hands, and
        # deriving them afresh cost seconds per click on a 10,000-hand player.
        facts = facts_cached(conn, alias, game, games)
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
    return {"player": alias, "filter": filter, **_spot_stats(db(), alias, filter, game)}


# Remembered, as the sizing below: tallying ten thousand hands is a second a call
# on a small server, and every chart tab asks again. The warm-up calls these too,
# so it fills exactly the entries the endpoints read.
def _spot_stats(conn: sqlite3.Connection, alias: str, filter: str | None, game: str | None) -> dict:
    return remembered(conn, ("spot-stats", alias, game, _spot_key(conn, filter)),
                      lambda: aggregate(_spot_facts(alias, filter, game, conn)), SPOT_ANSWERS)


def _spot_range(conn: sqlite3.Connection, alias: str, filter: str | None, by: str, game: str | None) -> dict:
    def answer() -> dict:
        facts = _spot_facts(alias, filter, game, conn)
        return range_grid(facts) if by == "preflop" else composition(facts)

    return remembered(conn, ("range", alias, game, _spot_key(conn, filter), by), answer, SPOT_ANSWERS)


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
    return {"player": alias, "filter": filter, "by": by, **_spot_range(db(), alias, filter, by, game)}


@app.get("/players/{alias:path}/sizing")
def player_sizing(
    alias: str,
    street: Annotated[str, Query(pattern="^(flop|turn|river)$")] = "flop",
    kind: Annotated[str, Query(pattern="^(cbet|bet|faced_cbet)$")] = "cbet",
    filter: Annotated[str | None, Query(description="e.g. 'srp,flop=ace_high'")] = None,
    game: str | None = None,
) -> dict:
    """What a player had at each bet size on one street, within a spot."""
    conn = db()
    out = remembered(conn, ("sizing", alias, game, _spot_key(conn, filter), street, kind),
                     lambda: sizing_tells(_spot_facts(alias, filter, game, conn), street, kind), SPOT_ANSWERS)
    return {"player": alias, "filter": filter, **out}


@app.get("/players/{alias:path}/hands")
def player_hands(
    alias: str,
    filter: Annotated[str | None, Query(description="e.g. 'cbet_flop=overbet'")] = None,
    game: str | None = None,
    games: Annotated[
        Literal["play", "money", "every"],
        Query(description="The scope (queries.SCOPES): the tab the list is opened from."),
    ] = "every",
) -> dict:
    """Every hand in a spot as a compact row, newest first. Replay one with /hands/{id}.

    Session lists `every` hand; a list opened from another tab's count lists the
    hands that count came from, so the two agree.
    """
    # One connection for both halves: db() opens a fresh one per call.
    conn = db()
    facts = _spot_facts(alias, filter, game, conn, games=games)
    rows = hand_list(facts, display_names(conn))
    return {"player": alias, "filter": filter, "hands": _with_marks(conn, rows, game)}


def _with_marks(conn, rows: list[dict], game: str | None) -> list[dict]:
    """Rows with the marks, notes, manual flags and tags the review rows carry, so
    any hand -- flagged or not -- can be ticked off and written on from the session view."""
    marks = reviewed_marks(conn, game)
    notes = hand_notes(conn, game)
    flags = flagged_hands(conn, game)
    _with_tags(conn, rows, game)
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
    facts = _spot_facts(alias, filter, game, conn, games="every")
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
    """
    conn = db()
    out = tags_for(_spot_facts(alias, filter, game, conn), _tag_pool(conn, filter, game), alias)
    return {"player": alias, "filter": filter, **out}


def _tag_pool(conn: sqlite3.Connection, filter: str | None, game: str | None):
    """The pool a player's tags are judged against: every player's hands in the
    same scope and spot (SPEC.md, "Pool baseline")."""
    pool = pool_for(conn, game)
    if not filter:
        return pool
    pred = _predicate(conn, filter)
    key = (path_of(conn), generation(conn), game, _spot_key(conn, filter))
    return pool_from_facts(
        {a: [f for f in facts_cached(conn, a, game) if pred(f)] for a in pool.players},
        pool.owners, f"spot: {filter}", key,
    )


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
def write_flag(
    game_id: GameId, hand_number: int, req: Annotated[FlagRequest, Body()], capture: CaptureOpt = False,
) -> dict:
    """Flag one hand for manual review, or clear the flag.

    Addressed by the log's own (game_id, hand_number) rather than a hand_id: the
    HUD flags the hand that just ended, which the rebuild may not have derived yet.
    """
    try:
        at = set_flag(_db_for(capture), game_id, hand_number, req.flagged)
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


class AnnotationRequest(BaseModel):
    #: What is wrong with the element, in your words.
    note: str = Field(min_length=1, max_length=4000)
    #: Where: the page's path ("/chart", or "sidepanel" for the HUD) and its query.
    page: str = Field(max_length=200)
    query: str = Field("", max_length=2000)
    #: Which element, as the overlay saw it. A selector may not survive a redesign,
    #: so the tag, classes and text come along to find it again by.
    selector: str = Field("", max_length=1000)
    tag: str = Field("", max_length=40)
    classes: str = Field("", max_length=300)
    text: str = Field("", max_length=300)
    #: The nearest heading or labelled container above it: the spot as a person names it.
    context: str = Field("", max_length=300)
    rect: dict[str, float] = Field(default_factory=dict)
    viewport: dict[str, float] = Field(default_factory=dict)


class AnnotationChange(BaseModel):
    status: Literal["open", "resolved", "closed"] | None = None
    #: The answer: what was changed, or why not.
    reply: str | None = Field(None, max_length=4000)
    note: str | None = Field(None, min_length=1, max_length=4000)


@app.get("/annotations", dependencies=[Depends(_dev_only)])
def list_annotations(
    status: Literal["open", "resolved", "closed"] | None = Query(None),
    page: str | None = Query(None, description='A path such as "/chart", or "sidepanel".'),
) -> list[dict]:
    """Notes left on the pages with the overlay, oldest first (pnt/server/annotations.py)."""
    return annotations.listing(status, page)


@app.post("/annotations", dependencies=[Depends(_dev_only)])
def add_annotation(req: Annotated[AnnotationRequest, Body()]) -> dict:
    return annotations.add(req.model_dump())


@app.post("/annotations/{note_id}", dependencies=[Depends(_dev_only)])
def change_annotation(note_id: str, req: Annotated[AnnotationChange, Body()]) -> dict:
    note = annotations.change(note_id, req.model_dump(exclude_none=True))
    if note is None:
        raise HTTPException(status_code=404, detail="no such note")
    return note


@app.delete("/annotations/{note_id}", dependencies=[Depends(_dev_only)])
def delete_annotation(note_id: str) -> dict:
    if not annotations.remove(note_id):
        raise HTTPException(status_code=404, detail="no such note")
    return {"deleted": note_id}


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

    `columns` is the stats chosen on the front page (`/settings/hud`), in order,
    each with its label, hover text and `_opp` key; `hands` comes before them.

    Always your own database (`capture_db`), even while the pages show the
    sample; `dataset` says which one the pages -- and the panel's chart -- read.
    """
    conn = capture_db()
    columns = _hud_columns(conn)
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
    tag_pool = pool_for(conn)  # lifetime tags, judged against the lifetime pool
    notes = player_notes(conn)
    return {
        "game_id": game_id,
        "columns": columns,
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
                "tags": tags_for(
                    facts_cached(conn, r["alias"]) if r["alias"] else [], tag_pool, r["alias"]),
                # What you have written about them; null when nothing.
                "note": notes.get(r["alias"], {}).get("note"),
                # Whether their next hand here falls inside a post-loss window
                # (SPEC.md, "Session dynamics"), and which loss opened it.
                # Per seat: one player merged from two accounts can hold two.
                **_post_loss(conn, r["alias"], r["pn_id"], game_id),
            }
            for r in seated
        ],
        "dataset": "sample" if SAMPLE_ON else "own",
    }


# ------------------------------------------------------------- HUD columns ---
# Which stats the HUD shows, chosen on the front page. Kept in the database, not
# the extension, so every browser pointed at this server shows the same set.
HUD_SETTING = "hud_columns"


def _hud_columns(conn: sqlite3.Connection) -> list[dict]:
    """The chosen columns as `/hud` sends them: catalog entries in order."""
    from pnt.db.userdata import get_setting
    from pnt.stats.hud_catalog import BY_KEY, chosen

    return [
        {k: BY_KEY[key][k] for k in ("key", "label", "tip", "opp")}
        for key in chosen(get_setting(conn, HUD_SETTING))
    ]


def _hud_settings(columns: list[str]) -> dict:
    from pnt.stats.hud_catalog import CATALOG, DEFAULT, MAX

    return {"catalog": CATALOG, "columns": columns, "default": DEFAULT, "max": MAX}


class HudColumnsRequest(BaseModel):
    #: Catalog keys, in the order the HUD prints them after `hands`.
    columns: list[str]


@app.get("/settings/hud")
def read_hud_settings() -> dict:
    """Every stat the HUD can show (`catalog`), the ones it shows (`columns`),
    and the default and cap the front page offers."""
    from pnt.db.userdata import get_setting
    from pnt.stats.hud_catalog import chosen

    return _hud_settings(chosen(get_setting(capture_db(), HUD_SETTING)))  # the HUD's own: see /hud


@app.put("/settings/hud")
def write_hud_settings(req: Annotated[HudColumnsRequest, Body()]) -> dict:
    """Choose the HUD's stats. 400 on a key not in the catalog, a key listed
    twice, or more than the cap. The HUD picks the change up on its next poll."""
    from pnt.db.userdata import set_setting
    from pnt.stats.hud_catalog import check

    try:
        check(req.columns)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _hud_settings(set_setting(capture_db(), HUD_SETTING, req.columns))


# ---- who you are -------------------------------------------------------------
# The hero the leaks, tendencies and stats pages are about. Guessed from whose logs
# these are, which goes wrong once a friend's downloads are imported here; picked on
# the front page or with `pnt hero`, it is kept in the database like the HUD columns.


def _hero_settings(conn: sqlite3.Connection) -> dict:
    from pnt.stats.leaks import chosen_hero, guessed_hero, hero_of

    players = [
        r["alias"]
        for r in conn.execute(
            "SELECT p.alias, COUNT(*) AS n FROM players p"
            " JOIN player_identities pi ON pi.player_id = p.player_id"
            " JOIN hand_players hp ON hp.pn_id = pi.pn_id"
            " GROUP BY p.player_id ORDER BY n DESC, p.alias"
        )
    ]
    return {"hero": hero_of(conn), "chosen": chosen_hero(conn), "guess": guessed_hero(conn), "players": players}


class HeroRequest(BaseModel):
    #: An alias, or null to go back to the guess.
    player: str | None = None


@app.get("/settings/hero")
def read_hero_settings() -> dict:
    """Who the hero is (`hero`), the one picked (`chosen`, null when none is), the
    guess from whose logs these are (`guess`), and every player, most hands first."""
    return _hero_settings(db())


@app.put("/settings/hero")
def write_hero_settings(req: Annotated[HeroRequest, Body()]) -> dict:
    """Pick the hero by alias, or null to go back to the guess. 404 on an alias
    with no player."""
    from pnt.db.userdata import delete_setting, set_setting
    from pnt.stats.leaks import HERO_SETTING

    conn = db()
    if req.player is None:
        delete_setting(conn, HERO_SETTING)
    else:
        row = conn.execute("SELECT player_id FROM players WHERE alias = ?", (req.player,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"no player {req.player!r}")
        set_setting(conn, HERO_SETTING, row["player_id"])
    return _hero_settings(conn)


# ---- where the files are --------------------------------------------------------
# The database and the log folder, chosen on the front page. Applied at once, with no
# restart, and recorded in the machine config (pnt/local_config.py) so the next start
# -- the task at login, `pnt serve`, the CLI -- opens the same ones.


def _paths() -> dict:
    return {
        "db": str(DB_PATH.resolve()),
        "log_dir": str(log_folder.LOG_DIR),
        # Set outside this program, so the page shows it but cannot change it.
        "log_dir_env": log_folder.FROM_ENV,
        "save_logs": SAVE_LOGS,
        "picker": _picker_available(),
    }


@functools.cache
def _picker_available() -> bool:
    """Whether tkinter can start, not just import: a Python can ship the package
    without a Tcl it can find, and then Tk() fails on every click."""
    try:
        import tkinter
    except ImportError:
        return False
    try:
        tkinter.Tcl()  # reads init.tcl, the part that goes missing; opens no window
    except tkinter.TclError:
        return False
    return True


class PathsRequest(BaseModel):
    db: str | None = None
    log_dir: str | None = None


def _full_path(text: str) -> Path:
    """`text` as typed or pasted (quotes and ~ allowed), or a 400 unless it is absolute."""
    path = Path(text.strip().strip('"')).expanduser()
    if not path.is_absolute():
        raise HTTPException(status_code=400, detail=f"give the full path, not {text!r}")
    return path


def _read_only_uri(path: Path) -> str:
    # Not Path.as_uri(): a share's \\server\... becomes file://server/..., and SQLite
    # refuses any host but localhost. Four slashes put the server in the path.
    posix = path.as_posix()
    return f"file:{'//' if posix.startswith('/') else '/'}{quote(posix, safe='/:')}?mode=ro"


def _tracker_db(text: str) -> Path:
    """`text` as a database this server can switch to, or a 400 saying why not.

    Only an existing tracker database: opening any other path would create an empty
    one there, and the HUD would go blank with nothing to say why.
    """
    path = _full_path(text)
    if not path.is_file():
        raise HTTPException(status_code=400, detail=f"no file at {path}")
    try:
        probe = sqlite3.connect(_read_only_uri(path.resolve()), uri=True)
        try:
            tables = {r[0] for r in probe.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            probe.close()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=400, detail=f"not a SQLite database: {path}") from exc
    if not {"hands", "raw_entries"} <= tables:
        raise HTTPException(
            status_code=400,
            detail=f"not a tracker database: {path}. `pnt setup --db <path>` makes a new one.",
        )
    return path.resolve()


def _log_dir(text: str) -> Path:
    path = _full_path(text)
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"no folder at {path}")
    return path.resolve()


@app.get("/settings/paths")
def read_paths() -> dict:
    """The database and log folder in use, and whether the page can open a picker."""
    return _paths()


@app.put("/settings/paths")
def write_paths(req: Annotated[PathsRequest, Body()]) -> dict:
    """Switch the database and/or the log folder, now and for every later start.

    400 on a path that is not an existing tracker database or folder; 409 on the
    log folder while $PNT_LOG_DIR fixes it. Both are checked before either moves,
    and both are recorded before either is applied: a failed save switches nothing,
    rather than leaving this run on the new one and the next start on the old.
    """
    global DB_PATH, _switches
    from pnt import local_config
    from pnt.stats.queries import clear_caches

    new_db = _tracker_db(req.db) if req.db is not None else None
    new_logs = _log_dir(req.log_dir) if req.log_dir is not None else None
    # Resolved: $PNT_LOG_DIR may be relative or start with ~, and new_logs never does.
    if new_logs is not None and log_folder.FROM_ENV and new_logs != log_folder.LOG_DIR.expanduser().resolve():
        raise HTTPException(
            status_code=409, detail="PNT_LOG_DIR is set, and it decides the log folder; unset it first."
        )

    switching = new_db is not None and new_db != DB_PATH.resolve()
    if switching:
        conn = connect(new_db)  # its migrations now, not halfway through someone's request
        try:
            # Before the sync loop sees it: files recorded on another PC would read as deleted.
            if forgotten := sync.forget_missing(conn):
                log.info("%s: forgot %d log files not on this machine", new_db, forgotten)
        finally:
            conn.close()
    if new_db is not None:
        local_config.put("db", str(new_db))
    if new_logs is not None and not log_folder.FROM_ENV:
        local_config.put("log_dir", str(new_logs))

    if new_db is not None:
        _leave_sample()  # a database picked by hand is the one to look at
    if switching:
        DB_PATH = new_db
        _switches += 1  # before the new warm-up starts, so only the old ones stop
        clear_caches()  # keyed by path, so only memory: the old database's entries
        threading.Thread(target=_warm_strength, name="strength-warm", daemon=True).start()
        log.info("database switched to %s", new_db)
    if new_logs is not None and not log_folder.FROM_ENV:
        log_folder.LOG_DIR = new_logs  # the sync loop reads it on its next pass
    return _paths()


# ---- the sample, or your own data ------------------------------------------------
# The front page's Data switch, for a demo or a first look before any logs of your
# own. Only what the pages read moves (`db()`); see SAMPLE_ON. Building the sample's
# database takes a minute the first time, so it runs in a thread the page polls.
_sample_lock = threading.Lock()
#: (files done, files in all) while the sample's database is being built.
_sample_building: tuple[int, int] | None = None
#: Whether the build under way should switch to the sample when it is done: Your
#: hands, pressed meanwhile, says not.
_sample_wanted = False
_sample_error: str | None = None


class DatasetRequest(BaseModel):
    which: str = Field(pattern="^(own|sample)$")


def _dataset() -> dict:
    b = _sample_building
    return {
        "active": "sample" if SAMPLE_ON else "own",
        "building": {"done": b[0], "total": b[1]} if b else None,
        "error": _sample_error,
        "games": len(samplemod.logs()),
    }


def _enter_sample(path: Path) -> None:
    global SAMPLE_ON, SAMPLE_PATH
    SAMPLE_PATH, SAMPLE_ON = path, True
    # Not counted as a switch: your own database's warm-up has the HUD to serve,
    # and strength values are the cards' alone, so two at once share them safely.
    # It stops once the pages leave this sample; a toggle back while it still runs
    # leaves it be rather than starting a second on the same file.
    threading.Thread(
        target=_warm_strength, args=(path, lambda: SAMPLE_ON and SAMPLE_PATH == path),
        name="sample-warm", daemon=True,
    ).start()
    log.info("pages show the sample: %s", path)


def _leave_sample() -> None:
    global SAMPLE_ON, _sample_wanted
    _sample_wanted = False
    if SAMPLE_ON:
        SAMPLE_ON = False
        log.info("pages show your own data again")


def _build_sample(path: Path) -> None:
    """Build the sample's database, then switch to it if that is still wanted. A
    build cut short is finished on the next try: importing is idempotent."""
    global _sample_building, _sample_error

    def progress(done: int, total: int) -> None:
        global _sample_building
        _sample_building = (done, total)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = connect(path)
        try:
            samplemod.build(conn, on_file=progress)
        finally:
            conn.close()
    except Exception as exc:
        log.exception("could not build the sample")
        _sample_error = f"could not build the sample: {exc}"
    else:
        for old in samplemod.stale():
            for p in (old, old.with_name(old.name + "-wal"), old.with_name(old.name + "-shm")):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass  # still open somewhere: the next build tidies it
        with _sample_lock:
            if _sample_wanted:
                _enter_sample(path)
    finally:
        _sample_building = None


def _sample_current(path: Path) -> bool:
    if not path.is_file():
        return False
    conn = connect(path)
    try:
        return samplemod.is_current(conn)
    finally:
        conn.close()


@app.get("/settings/dataset")
def read_dataset() -> dict:
    """What the pages show, `own` or `sample`, and the sample's build while it runs."""
    return _dataset()


@app.put("/settings/dataset")
def write_dataset(req: Annotated[DatasetRequest, Body()]) -> dict:
    """Show the bundled sample on the pages, or your own data again.

    The first switch to the sample builds its database (~/.pnt/sample-*.sqlite),
    and so does the first after an update changes the sample: the answer then
    carries `building`, and the switch happens when the build is done. Capture, the
    log folder and the HUD stay on your own database throughout.
    """
    global _sample_building, _sample_wanted, _sample_error
    with _sample_lock:
        if req.which == "own":
            _leave_sample()
            return _dataset()
        _sample_wanted, _sample_error = True, None
        if SAMPLE_ON or _sample_building is not None:
            return _dataset()
        path = samplemod.database()
        if _sample_current(path):
            _enter_sample(path)
            return _dataset()
        _sample_building = (0, len(samplemod.logs()))
        threading.Thread(target=_build_sample, args=(path,), name="sample-build", daemon=True).start()
        return _dataset()


class BrowseRequest(BaseModel):
    kind: str = Field(pattern="^(db|log_dir)$")


#: One picker at a time: a second click would stack a second dialog behind the first.
_picker = threading.Lock()


@app.post("/settings/paths/browse")
def browse_path(req: Annotated[BrowseRequest, Body()]) -> dict:
    """Open Windows' own file or folder picker on this machine and return the
    choice, `{"path": null}` if cancelled. It picks only; PUT /settings/paths applies.

    A page cannot do this itself: browsers never tell a page where a picked file
    lives. This server runs on the same desktop, so it opens the dialog instead.
    501 without tkinter; 409 while a picker is already open.
    """
    if not _picker_available():
        raise HTTPException(status_code=501, detail="this Python has no tkinter, so type the path instead")
    if not _picker.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="the picker is already open")
    try:
        return {"path": _ask(req.kind)}
    except Exception as exc:  # tkinter.TclError, without importing tkinter here
        log.warning("file picker failed: %s", exc)
        raise HTTPException(status_code=501, detail="the picker would not open, so type the path instead") from exc
    finally:
        _picker.release()


def _ask(kind: str) -> str | None:
    # Tk lives and dies in this one thread, as it must; nothing else here uses it.
    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    root.withdraw()
    # The server is a background process, so Windows would put the dialog behind
    # the browser that asked for it.
    root.attributes("-topmost", True)
    try:
        if kind == "db":
            current = DB_PATH.resolve()
            chosen = filedialog.askopenfilename(
                parent=root,
                title="Choose the tracker database",
                initialdir=str(current.parent),
                initialfile=current.name,
                filetypes=[("Tracker database", "*.sqlite *.sqlite3 *.db"), ("All files", "*.*")],
            )
        else:
            chosen = filedialog.askdirectory(
                parent=root,
                title="Choose the log folder",
                initialdir=str(log_folder.LOG_DIR if log_folder.LOG_DIR.is_dir() else Path.home()),
                mustexist=True,
            )
    finally:
        root.destroy()
        # Free every Tk object here and now. Left to the collector, one could be
        # freed on another thread, and Tcl aborts the whole process when that happens.
        del root
        gc.collect()
    return str(Path(chosen)) if chosen else None


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
    return snapshot(capture_db(), game_id, min_hands=min_hands, min_known=min_known)


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

    conn = db()
    out = remembered(conn, ("results", alias, game, _spot_key(conn, filter)),
                      lambda: results_breakdown(_spot_facts(alias, filter, game, conn, games="money")))
    return {"player": alias, "filter": filter, **out}


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
# --- value:bluff -----------------------------------------------------------------


@app.get("/players/{alias:path}/bluffs")
def player_bluffs(
    alias: str,
    filter: Annotated[str | None, Query(description="e.g. 'srp,vs=henry'")] = None,
    game: str | None = None,
) -> dict:
    """One player's value:bluff by kind of aggression: bets by size, check-raises,
    raises and jams per street, with the shown hands read on the street of the
    action. Each row carries its hand ids, so the chart lists them on a click.
    See SPEC.md, "Value:bluff"."""
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


@app.get("/nit")
def nit(game: Annotated[str | None, Query(description="Restrict to one game_id.")] = None) -> dict:
    """Who has been paid and who has paid the Nit Game penalty, ranked by net.
    The net sums to zero across players; `balance` says if it does not."""
    from pnt.stats.bounty import nit_ledger

    return nit_ledger(db(), game)


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
    facts = _spot_facts(alias, filter, game, conn, games="money")
    adj = allin_adjusted(conn, facts) if adjusted else None
    return {"player": alias, "filter": filter, **winnings_series(facts, adj)}


# ---- the give-up funnel ----------------------------------------------------------
# Imported inside the endpoint so this block stands alone. See SPEC.md, "Give-up
# funnel".


@app.get("/players/{alias:path}/funnel")
def player_funnel(
    alias: str,
    filter: Annotated[str | None, Query(description="e.g. 'srp,pfa'")] = None,
    game: str | None = None,
) -> dict:
    """Of the flops one player saw in a spot: how many turns, rivers and showdowns
    they reached and how many showdowns they won; WWSF and its no-showdown part;
    and where they folded, as the previous street's aggressor or not. Every row's
    `filter` finds exactly its `n`, and a fold row's `of_filter` its `of`."""
    from pnt.stats.funnel import funnel

    return {"player": alias, "filter": filter, **funnel(_spot_facts(alias, filter, game))}


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

    conn = db()
    out = remembered(conn, ("tilt", alias, game, _spot_key(conn, filter)),
                      lambda: tilt_report(_spot_facts(alias, filter, game, conn)))
    return {"player": alias, "filter": filter, **out}


def _post_loss(conn: sqlite3.Connection, alias: str | None, pn_id: str, game_id: str) -> dict:
    """A HUD seat's `post_loss`: whether their next hand in this game falls inside
    a post-loss window, with how many hands since the loss that opened it and its
    size. Read from the lifetime Facts the seat's tags already fetched -- annotated
    per game, so this game's rows carry this game's context only -- and from this
    seat's account alone: a player merged from two accounts sitting at once has two
    runs of hands, and each card shows its own."""
    from pnt.stats.tilt import next_hand

    # Every hand of the session, PLO and Double Board too: a big pot lost at either
    # is still a big loss, and still a hand since one.
    facts = facts_cached(conn, alias, games="money") if alias else []
    out = next_hand([f for f in facts if f.pn_id == pn_id], game_id)
    return {"post_loss": out["post_loss"], "post_loss_since": out["since"], "post_loss_bb": out["loss_bb"]}


# ---- the hero's leaks ------------------------------------------------------------
# Imported inside the endpoint so this block stands alone. See SPEC.md, "Leak
# finder".


@app.get("/leaks")
def hero_leaks(game: Annotated[str | None, Query(description="Restrict to one game_id.")] = None) -> dict:
    """The hero's stats furthest outside their bands, ranked by cost x confidence:
    `top` is the three costliest habits, `ranked` every judged leak outside its
    band, `candidates` every stat judged. Each carries the filter that finds its
    `n` on the hero's chart. No hero (no log names its owner) is an empty result."""
    from pnt.stats.leaks import leaks

    return leaks(db(), game)


# ---- tendencies ------------------------------------------------------------------
# Imported inside the endpoint so this block stands alone.


@app.get("/tendencies")
def tendencies_report(
    game: Annotated[str | None, Query(description="Restrict to one game_id.")] = None,
    player: Annotated[str | None, Query(description="Add `player`: this one player's tags and measures.")] = None,
) -> dict:
    """The tags on the hero (`you`), and per tag the regular opponents it fires on
    (`pool`), most first. A regular has `min_hands` hands or more. Fun tags are left
    out. Each player's entry carries the filter that opens the tag's hands.

    With `player`, the answer adds `player`: their tags, each with how many
    regulars share it, and every measure the tags read beside the pool's rate."""
    from pnt.stats.leaks import hero_of
    from pnt.stats.tendencies import player_tendencies, tendencies

    conn = db()
    # The hero is a setting, not a derived fact: picking another moves no generation.
    hero = hero_of(conn, game)
    out = remembered(conn, ("tendencies", game, hero), lambda: tendencies(conn, game))
    if not player:
        return out
    shared = {t["id"]: t["count"] for t in out["pool"]}
    try:
        one = remembered(conn, ("tendencies", game, hero, "player", player),
                         lambda: player_tendencies(conn, player, game, shared))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {**out, "player": one}


# ---- timing tells --------------------------------------------------------------
# Imported inside the endpoints so this block stands alone. See SPEC.md, "Timing
# tells".


@app.get("/players/{alias:path}/timing")
def player_timing(
    alias: str,
    filter: Annotated[str | None, Query(description="e.g. 'srp,vs=henry'")] = None,
    game: str | None = None,
) -> dict:
    """How long one player's first decision on each street took, and what their
    hands were by tempo bucket: flop and turn calls, river bets and river calls,
    against the pool's cuts and against their own median (`relative`). Every row
    carries its filter and its hand ids. `timed.rebuild` is true while some hands
    predate the stored clock: `pnt rebuild` times them."""
    from pnt.stats.timing import timing_report

    conn = db()
    facts = _spot_facts(alias, filter, game, conn)
    # Their whole record sets their own pace, so a spot never moves their median.
    baseline = facts_cached(conn, alias, game)
    return {"player": alias, "filter": filter, **timing_report(facts, baseline=baseline, with_ids=True)}


@app.get("/timing")
def pool_timing(
    filter: Annotated[str | None, Query(description="e.g. 'srp'")] = None,
    game: str | None = None,
) -> dict:
    """The same over the pool, every player and every hand, with each row's
    strength split into the logs' owners' own cards and everyone else's shown
    hands, and `thin`: who has enough known hands to read alone. No hand ids --
    one hand is several players' rows here."""
    from pnt.stats.fold_equity import pool_facts
    from pnt.stats.timing import owners, timing_report

    conn = db()
    facts = pool_facts(conn, game, _predicate(conn, filter))
    names = display_names(conn)
    return {"player": None, "filter": filter, "players": len({names.get(f.pn_id, f.pn_id) for f in facts}),
            **timing_report(facts, owners=owners(conn), who=names)}


# --- group by (SPEC.md, "Group by") ------------------------------------------
# One spot split by one or two dimensions, each cell with its own filter term.
# Imported inside the endpoint so this block stands alone.


@app.get("/groupby")
def groupby(
    by: Annotated[str, Query(description="one or two dimensions, e.g. 'position,pot_type'")],
    player: str | None = None,
    filter: Annotated[str | None, Query(description="e.g. 'srp'")] = None,
    game: str | None = None,
    min: Annotated[int, Query(ge=0)] = 0,
    street: Annotated[str, Query(pattern="^(flop|turn|river)$")] = "flop",
) -> dict:
    """The spot's hands by `by`: one player's (as the chart has them), or the
    pool's (as /stats has them), which also allows `player`. Each cell carries
    `term`; `dims` is the catalog of dimensions to offer. 400 on an unknown
    dimension or filter term, 404 on an unknown player."""
    from pnt.stats.groupby import catalog, check_dims, group
    from pnt.stats.queries import facts_by_player

    dims = [d.strip() for d in by.split(",") if d.strip()]
    try:
        check_dims(dims, pool=player is None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    conn = db()
    player_of = None
    if player is not None:
        facts = _spot_facts(player, filter, game, conn)
    else:
        pred = _predicate(conn, filter)
        grouped, aliases, _ = facts_by_player(conn, game)
        whose = {f.pn_id: aliases.get(pid, "?") for pid, rows in grouped.items() for f in rows}
        facts = [f for rows in grouped.values() for f in rows if pred is None or pred(f)]

        def player_of(f: Facts) -> str:
            return whose[f.pn_id]

    out = group(facts, dims, street=street, min_hands=min, names=display_names(conn), player_of=player_of)
    return {"player": player, "filter": filter, "game": game, "dims": catalog(pool=player is None), **out}


# ---- the spot tree and made hands by street ------------------------------------
# Imported inside the endpoints so this block stands alone. See SPEC.md, "Spot
# tree" and "Holdings by street".


@app.get("/players/{alias:path}/tree")
def player_tree(
    alias: str,
    filter: Annotated[str | None, Query(description="e.g. 'srp'")] = None,
    game: str | None = None,
    depth: Annotated[int, Query(ge=1, le=12, description="Decisions deep below the seat.")] = 6,
    texture: Annotated[bool, Query(description="Split the first postflop node by flop texture.")] = False,
    min_hands: Annotated[int, Query(ge=1, description="Hide nodes with fewer hands.")] = 1,
) -> dict:
    """One player's decision points as a tree: seat, then each spot they reached,
    what they chose there, and the spot those hands reached next. Every node and
    every decision carries the filter that finds exactly its hands, to add to
    `filter`."""
    from pnt.stats.tree import tree_for

    conn = db()
    pred = _predicate(conn, filter)
    try:
        out = tree_for(conn, alias, game, pred, spot=_spot_key(conn, filter), max_depth=depth,
                       min_hands=min_hands, texture=texture)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {**out, "filter": filter}


@app.get("/players/{alias:path}/streets")
def player_streets(
    alias: str,
    filter: Annotated[str | None, Query(description="e.g. 'srp,pfa'")] = None,
    game: str | None = None,
) -> dict:
    """Flop holding against river holding over the shown hands that reached the
    river, and for a log's owner, where each flop holding went: folded, won with
    no showdown, or shown down. Every cell and path carries its filter."""
    from pnt.stats.pool import owners
    from pnt.stats.streets import made_by_street

    conn = db()
    facts = _spot_facts(alias, filter, game, conn)
    return {"player": alias, "filter": filter, **made_by_street(facts, owners(conn))}


# ---- saved spots and hand tags --------------------------------------------------
# What you told the tracker (db/userdata.py): spot filters kept under a name for
# the pages' star menu, and your words on a hand. Imported inside the endpoints so
# this block stands alone. See SPEC.md, "Saved spots and hand tags".


class SpotRequest(BaseModel):
    name: str
    filter: str


class SpotPatch(BaseModel):
    name: str | None = None
    filter: str | None = None
    pinned: bool | None = None
    #: Its place in `GET /spots`, from 0. Pinned spots still list first.
    ord: int | None = Field(None, ge=0)


def _spot_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=404 if isinstance(exc, LookupError) else 400, detail=str(exc))


@app.get("/spots")
def spots() -> list[dict]:
    """Every saved spot, pinned first, then in your order: `spot_id`, `name`,
    `filter`, `pinned`, `ord`, `created_at`."""
    from pnt.db.userdata import list_spots

    return list_spots(db())


@app.post("/spots")
def save_spot(req: Annotated[SpotRequest, Body()]) -> dict:
    """Save a filter under a name. 400 on a filter the parser refuses (its own
    message), an empty name, or a name already taken."""
    from pnt.db.userdata import save_spot as save

    conn = db()
    _predicate(conn, req.filter.strip())
    try:
        return save(conn, req.name, req.filter)
    except ValueError as exc:
        raise _spot_error(exc) from exc


@app.patch("/spots/{spot_id}")
def edit_spot(spot_id: int, req: Annotated[SpotPatch, Body()]) -> dict:
    """Rename, re-filter, pin or move one spot; a field left out is left alone.
    404 on an unknown id, 400 as `POST /spots`."""
    from pnt.db.userdata import list_spots, reorder_spots, update_spot

    conn = db()
    if req.filter is not None:
        _predicate(conn, req.filter.strip())
    try:
        out = update_spot(conn, spot_id, name=req.name, filter=req.filter, pinned=req.pinned)
        if req.ord is not None:
            rest = [s["spot_id"] for s in list_spots(conn) if s["spot_id"] != spot_id]
            rest.insert(min(req.ord, len(rest)), spot_id)
            out = next(s for s in reorder_spots(conn, rest) if s["spot_id"] == spot_id)
    except (LookupError, ValueError) as exc:
        raise _spot_error(exc) from exc
    return out


@app.delete("/spots/{spot_id}")
def remove_spot(spot_id: int) -> dict:
    """Forget one saved spot. 404 when there is none with that id."""
    from pnt.db.userdata import delete_spot

    if not delete_spot(db(), spot_id):
        raise HTTPException(status_code=404, detail=f"no saved spot {spot_id}")
    return {"spot_id": spot_id, "deleted": True}


class TagRequest(BaseModel):
    tag: str


def _hand_tags(conn: sqlite3.Connection, game_id: str, hand_number: int) -> dict:
    from pnt.db.userdata import tags_for_hand

    return {"game_id": game_id, "hand_number": hand_number, "tags": tags_for_hand(conn, game_id, hand_number)}


def _tag_or_400(tag: str) -> str:
    """The tag as stored, or 400. `flagged` is the 🚩's, never a tag of its own."""
    from pnt.db.userdata import normalise_tag

    try:
        out = normalise_tag(tag)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if out == FLAGGED_TAG:
        raise HTTPException(status_code=400, detail="flagged is the 🚩: flag the hand instead")
    return out


def _with_tags(conn: sqlite3.Connection, rows: list[dict], game: str | None) -> list[dict]:
    """Each row with its hand's `tags`, alphabetically."""
    from pnt.db.userdata import tags_for_hands

    tags = tags_for_hands(conn, game)
    for r in rows:
        r["tags"] = tags.get((r["game_id"], r["hand_number"]), [])
    return rows


@app.get("/tags")
def tag_list() -> list[dict]:
    """Every tag in use, most used first: `tag`, `hands`, and `last`, when it was
    last put on a hand. What a tag box suggests."""
    from pnt.db.userdata import all_tags

    return all_tags(db())


@app.get("/games/{game_id}/hands/{hand_number}/tags")
def hand_tags(game_id: GameId, hand_number: int) -> dict:
    """One hand's tags, by the log's own name for it, as the flag is addressed."""
    return _hand_tags(db(), game_id, hand_number)


@app.post("/games/{game_id}/hands/{hand_number}/tags")
def add_hand_tag(game_id: GameId, hand_number: int, req: Annotated[TagRequest, Body()]) -> dict:
    """Tag one hand; tagging it again is a no-op. Returns the hand's tags. 400 on
    a tag that cannot be one, 404 on a hand the log does not have."""
    from pnt.db.userdata import add_tag

    tag = _tag_or_400(req.tag)
    conn = db()
    try:
        add_tag(conn, game_id, hand_number, tag)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _hand_tags(conn, game_id, hand_number)


@app.delete("/games/{game_id}/hands/{hand_number}/tags/{tag}")
def remove_hand_tag(game_id: GameId, hand_number: int, tag: str) -> dict:
    """Take one tag off a hand; one it did not carry is a no-op. Returns the
    hand's tags."""
    from pnt.db.userdata import remove_tag

    conn = db()
    remove_tag(conn, game_id, hand_number, _tag_or_400(tag))
    return _hand_tags(conn, game_id, hand_number)


def _owner_seats(conn: sqlite3.Connection, keys: set[tuple[str, int]], game: str | None) -> list[Facts]:
    """One row per hand in `keys`: its log owner's seat when they were dealt in,
    else the first seat from the button. Derived on the spot, a hand alone, so
    the session terms (`post_loss`, `session_hand`) have no run to read."""
    from pnt.stats.derive import derive
    from pnt.stats.pool import owners
    from pnt.stats.queries import load_hands

    sql, args = "SELECT hand_id, game_id, hand_number FROM hands", ()
    if game:
        sql, args = sql + " WHERE game_id = ?", (game,)
    ids = [r["hand_id"] for r in conn.execute(sql, args) if (r["game_id"], r["hand_number"]) in keys]
    if not ids:
        return []
    own = owners(conn)
    out = []
    for h in load_hands(conn, game, hand_ids=ids):
        seats = derive(h, every_game=True)
        if seats:
            out.append(min(seats, key=lambda f: (
                (f.game_id, f.pn_id) not in own,
                f.seats_from_button is None,
                f.seats_from_button or 0,
            )))
    return out


@app.get("/tagged")
def tagged(
    tag: Annotated[str | None, Query(description="e.g. 'river-bluff'; 'flagged' for the 🚩 ones")] = None,
    player: str | None = None,
    filter: Annotated[str | None, Query(description="e.g. 'srp'")] = None,
    game: str | None = None,
) -> dict:
    """The hands carrying `tag`, newest first, in the rows `/players/{alias}/flagged`
    lists, each with its `tags`; with no tag, every hand you tagged. `tags` counts
    hands per tag over the same hands, `flagged` (the 🚩) first, for the chips.

    With `player` the rows are theirs and `filter` narrows their spot, as the
    chart's other views do; without, each hand is one row (see `_owner_seats`).
    """
    from pnt.db.userdata import tags_for_hands

    want = None
    if tag:
        want = FLAGGED_TAG if tag.strip().lower() == FLAGGED_TAG else _tag_or_400(tag)
    conn = db()
    flags = flagged_hands(conn, game)
    tagged_ = tags_for_hands(conn, game)
    marked = set(tagged_) | set(flags)
    if player is not None:
        facts = [f for f in _spot_facts(player, filter, game, conn, games="every")
                 if (f.game_id, f.hand_number) in marked]
    else:
        pred = _predicate(conn, filter)
        facts = [f for f in _owner_seats(conn, marked, game) if pred is None or pred(f)]
    hands = {(f.game_id, f.hand_number) for f in facts}
    counts: dict[str, int] = {}
    for key in hands:
        for t in tagged_.get(key, []):
            counts[t] = counts.get(t, 0) + 1
    chips = [{"tag": FLAGGED_TAG, "hands": len(hands & set(flags)), "builtin": True}]
    chips += [{"tag": t, "hands": n} for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    keep = set(flags) if want == FLAGGED_TAG else {k for k, ts in tagged_.items() if want is None or want in ts}
    facts = [f for f in facts if (f.game_id, f.hand_number) in keep]
    names = display_names(conn)
    rows = _with_marks(conn, hand_list(facts, names), game)
    if player is None:
        whose = {f.hand_id: names.get(f.pn_id, f.pn_id) for f in facts}
        for r in rows:
            r["player"] = whose[r["hand_id"]]
    return {"tag": want, "player": player, "filter": filter, "game": game, "tags": chips, "hands": rows}


# ---- estimated ranges and the board breakdown -----------------------------------
# A range from how often the player takes the action, not only from what they
# showed, and what it makes on a board. Imported inside the endpoints so this
# block stands alone. See SPEC.md, "Estimated ranges".


def _player_facts(conn: sqlite3.Connection, alias: str, game: str | None) -> list[Facts]:
    """Every hand of one player's, unfiltered: 404 on an unknown alias."""
    try:
        return facts_cached(conn, alias, game)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/players/{alias:path}/estimate")
def player_estimate(
    alias: str,
    filter: Annotated[str | None, Query(description="one preflop spot, e.g. 'position=BTN,unopened=raise'")] = None,
    game: str | None = None,
) -> dict:
    """The estimated range at the preflop decision the spot names: the top X% of
    hands at the player's frequency there (capped for a call), calibrated by the
    hands they showed. 400 when the spot names no single raise or call."""
    from pnt.stats.estimate import estimate

    conn = db()
    _predicate(conn, filter)  # a bad term is the parser's 400, in its words
    facts = _player_facts(conn, alias, game)
    try:
        out = estimate(facts, filter, display_names(conn), tag_resolver(conn))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"player": alias, "filter": filter, **out}


@app.get("/players/{alias:path}/board")
def player_board(
    alias: str,
    filter: Annotated[str | None, Query(description="e.g. 'position=BTN,unopened=raise,flop=ace_high'")] = None,
    board: Annotated[str | None, Query(description="3 to 5 cards, e.g. 'Ah7c2d'; blank samples the flop texture")] = None,
    game: str | None = None,
) -> dict:
    """The estimated range on a board: its made-hand groups, the cut at the
    player's next-action rate, what they showed after that action, and the bluffs
    that implies were never shown. 400 on a bad board or no usable spot."""
    from pnt.stats.estimate import board_report

    conn = db()
    names, tags = display_names(conn), tag_resolver(conn)
    pred = _predicate(conn, filter)
    facts = _player_facts(conn, alias, game)
    spot = [f for f in facts if pred(f)] if pred is not None else facts
    try:
        out = board_report(facts, spot, filter, (board or "").strip() or None, names, tags)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"player": alias, "filter": filter, **out}
