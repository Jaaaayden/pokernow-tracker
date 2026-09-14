"""`pnt` -- import logs, query stats, manage identities."""

from __future__ import annotations

import csv
import glob as globlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import typer

from . import service as svc
from .db.conn import DEFAULT_DB, connect
from .ingest import fetch as fetchmod
from .ingest.importer import (
    apply_aliases,
    export_aliases,
    import_csv,
    merge_players,
    rebuild_game,
    rename_player,
    split_identities,
)
from .ingest.log_folder import LOG_DIR, SAVE_LOGS, log_path, write_log
from .logfmt import redact as rd
from .stats.filters import parse_filter
from .stats.queries import display_names, facts_for, positional_report, report
from .stats.ranges import SIZING_KINDS, composition, range_grid, sizing_tells

app = typer.Typer(add_completion=False, help=__doc__)
alias_app = typer.Typer(help="Manage player identities.")
app.add_typer(alias_app, name="alias")
service_app = typer.Typer(
    help="Keep the server running in the background: starts at login, restarts on crash."
)
app.add_typer(service_app, name="service")

DbOpt = typer.Option(DEFAULT_DB, "--db", help="Path to the tracker database.")

#: LOG_DIR (from `ingest/log_folder.py`, shared with the server's live record) is
#: where PokerNow exports are kept when you do not say. One folder, so `pnt import`
#: with no argument is the whole history.
#:
#: Three ways to point somewhere else, narrowest first: pass paths (or a folder, or
#: a glob) straight to `pnt import`; pass `--log-dir`; or set PNT_LOG_DIR for good.
#: The environment variable is read once, at import, so it has to be set before the
#: command runs -- which is exactly why `--log-dir` exists as well.

#: A real 5,277-hand corpus shipped inside the package, so a fresh install has
#: something to look at before it has logs of its own. `pnt import` and `pnt setup`
#: fall back to it when the log folder is empty; naming a folder or passing paths
#: never reaches it.
#:
#: These are `pnt redact` output, not raw exports: a hole card survives only where
#: it was shown down. See `pnt/logs/README.md` and `pnt/logfmt/redact.py`.
BUNDLED_LOG_DIR = Path(__file__).parent / "logs"

LogDirOpt = typer.Option(
    None, "--log-dir", help=f"Folder of PokerNow exports. Default: {LOG_DIR}"
)
PathsArg = typer.Argument(
    None, help="CSV export path(s), directories or globs. Default: the log folder."
)
PnIdsArg = typer.Argument(..., help="PokerNow IDs to move off their player.")
OutOpt = typer.Option(
    Path("logs"), "--out", help="Folder to write the redacted copies into."
)
SampleOpt = typer.Option(
    True,
    "--sample/--no-sample",
    help="Fall back to the bundled sample logs when the log folder is empty.",
)
AuditOpt = typer.Option(
    False, "--audit", help="Check files for unshown hole cards instead of writing."
)
AliasOpt = typer.Option(..., "--alias", help="Name for the player they move to.")
AliasFileArg = typer.Argument(
    BUNDLED_LOG_DIR / "aliases.csv", help="The alias CSV. Default: the one in the repo."
)

LOG_GLOB = "poker_now_log_*.csv"

_COLUMNS = [
    ("hands", "Hands", 6),
    ("vpip", "VPIP", 6),
    ("pfr", "PFR", 6),
    ("3bet", "3Bet", 6),
    ("fold_to_3bet", "F3Bet", 6),
    ("cbet_flop", "CBetF", 6),
    ("fold_to_cbet_flop", "FCBetF", 7),
    ("af_flop", "AFf", 6),
    ("wtsd", "WTSD", 6),
    ("wsd", "W$SD", 6),
    ("bb_per_100", "bb/100", 7),
]


def _fmt(value) -> str:
    if value is None:
        return "--"  # unknown, not zero: the denominator was empty
    return f"{value:g}"


def _print_table(rows: list[dict], key: str, key_width: int = 22) -> None:
    if not rows:
        typer.echo("(no rows)")
        return
    header = key.ljust(key_width) + "".join(h.rjust(w) for _, h, w in _COLUMNS)
    typer.echo(header)
    typer.echo("-" * len(header))
    for r in rows:
        line = str(r[key])[: key_width - 1].ljust(key_width)
        line += "".join(_fmt(r.get(c)).rjust(w) for c, _, w in _COLUMNS)
        typer.echo(line)


def _logs_in(directory: Path) -> list[str]:
    return sorted(str(p) for p in directory.glob(LOG_GLOB))


def _announce_sample(folder: Path) -> None:
    """Say, unmissably, that these hands are not the user's own.

    A database that quietly filled itself with someone else's games would read as
    the user's own history -- and every name in `pnt stats` would be a stranger.
    """
    typer.echo(f"no {LOG_GLOB} in {folder}")
    typer.echo(f"importing the bundled sample corpus instead: {BUNDLED_LOG_DIR}")
    typer.echo("These are someone else's hands, kept so a fresh install has something to")
    typer.echo("query. Drop your own exports in the log folder and re-run to add yours;")
    typer.echo("pass --no-sample to skip them.")
    typer.echo("")


def _expand(paths: list[str] | None, folder: Path, sample: bool = False) -> list[str]:
    """Resolve CSV arguments -- paths, directories or globs -- to a list of files.

    With nothing given, that is every export in the log folder; `sample` lets the
    caller fall back to the bundled corpus when that folder is empty, rather than
    failing. Only the two commands that fill a database do that -- asking for a
    specific folder and silently getting a different one would be worse than the
    error it replaces.
    """
    if paths:
        expanded = []
        for pat in paths:
            for p in globlib.glob(pat) or [pat]:
                expanded.extend(_logs_in(Path(p)) if Path(p).is_dir() else [p])
    else:
        expanded = _logs_in(folder)
        if not expanded and sample:
            expanded = _logs_in(BUNDLED_LOG_DIR)
        if not expanded:
            raise typer.BadParameter(
                f"no {LOG_GLOB} in {folder} -- put your exports there, pass paths "
                "explicitly, or point somewhere else with --log-dir or PNT_LOG_DIR"
            )
    if not expanded:
        raise typer.BadParameter("no files matched")
    return expanded


@app.command("import")
def import_cmd(
    paths: list[str] | None = PathsArg,
    db: Path = DbOpt,
    log_dir: Path | None = LogDirOpt,
    sample: bool = SampleOpt,
) -> None:
    """Import PokerNow log exports. Safe to re-run: duplicate entries are ignored.

    With no argument, imports every log in the log folder, which is --log-dir if
    given, else $PNT_LOG_DIR, else ~/Downloads/pokernow-logs. `pnt where` prints
    which one is in effect.

    If that folder is empty, the bundled sample corpus is imported instead, so a
    fresh install has something to query. `--no-sample` turns that off.
    """
    conn = connect(db)
    folder = log_dir or LOG_DIR
    expanded = _expand(paths, folder, sample=sample)
    if not paths and expanded and Path(expanded[0]).parent == BUNDLED_LOG_DIR:
        _announce_sample(folder)
    for path in expanded:
        s = import_csv(conn, path)
        note = "no new entries (pure re-import)" if s["entries_new"] == 0 else ""
        typer.echo(
            f"{Path(path).name}: {s['hands']} hands, "
            f"{s['entries_new']}/{s['entries_offered']} new entries, "
            f"{s['parse_misses']} parse misses. {note}"
        )
        if s["hero_pn_id"]:
            typer.echo(f"    hero: {s['hero_pn_id']} ({s['hero_votes']} showdown matches)")


LinksArg = typer.Argument(None, help="Game links or game IDs.")
FromFileOpt = typer.Option(
    None, "--from-file", "-f", help="A text file of game links, one per line."
)
CookieOpt = typer.Option(
    None,
    "--cookie",
    envvar="PNT_COOKIE",
    show_default=False,
    help="Your PokerNow cookies as 'npt=...; apt=...', for your own hole cards. "
    "Prefer setting PNT_COOKIE: a flag lands in shell history.",
)
RefreshOpt = typer.Option(
    False, "--refresh", help="Re-fetch games already in the folder, merging new lines in."
)
ImportOpt = typer.Option(False, "--import", help="Import each fetched log as well.")


@app.command()
def backfill(
    links: list[str] | None = LinksArg,
    from_file: Path | None = FromFileOpt,
    log_dir: Path | None = LogDirOpt,
    cookie: str | None = CookieOpt,
    refresh: bool = RefreshOpt,
    import_: bool = ImportOpt,
    db: Path = DbOpt,
) -> None:
    """Download games' logs from their PokerNow links into the log folder.

    For games played before live capture: no clicking "download full log" on each.
    Files land where a manual export would (`poker_now_log_<id>.csv` in the log
    folder), so a bare `pnt import` afterwards picks them up.

    Without a cookie you get every action and every showdown, but not your own
    unshown hole cards -- PokerNow only sends `Your hand is` to the logged-in player.
    Copy the `npt` and `apt` cookies from DevTools (Application > Cookies >
    pokernow.com) into PNT_COOKIE as `npt=...; apt=...` to get them; `npt` alone is
    not enough. They are your login: keep them out of files and chat.

    Games already in the folder are skipped; `--refresh` fetches them again and adds
    only lines the file lacks, e.g. your hole cards after a first run without a cookie.
    """
    folder = log_dir or LOG_DIR
    raw = list(links or [])
    if from_file:
        raw += [
            line.strip()
            for line in from_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    if not raw:
        raise typer.BadParameter("give at least one game link, or --from-file")
    try:
        game_ids = list(dict.fromkeys(fetchmod.game_id_from_link(x) for x in raw))
    except fetchmod.FetchError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"{len(game_ids)} game(s) -> {folder}")
    if not cookie:
        typer.echo("no cookie: your own unshown hole cards will be missing (see --help)")
    elif "apt=" not in cookie:
        typer.echo("cookie has no apt=: without it your hole cards will be missing (see --help)")
    conn = connect(db) if import_ else None
    pacer = fetchmod.Pacer()
    # A game is a minute of paging; show it moving, but only where \r redraws a line.
    live = sys.stdout.isatty()
    failed = 0
    for gid in game_ids:
        path = log_path(folder, gid)
        if path.exists() and not refresh:
            typer.echo(f"{gid}: already in the folder, skipped (--refresh to fetch again)")
            continue
        if not live:
            typer.echo(f"{gid}: fetching...")
        try:
            entries = fetchmod.fetch_log(
                gid,
                cookie,
                pacer=pacer,
                on_page=lambda pages, lines, gid=gid: typer.echo(
                    f"\r{gid}: page {pages}, {lines} lines", nl=False
                )
                if live
                else None,
                on_wait=lambda s, gid=gid: typer.echo(
                    f"{chr(10) if live else ''}{gid}: rate limited by PokerNow, waiting {s:g}s"
                ),
            )
        except fetchmod.FetchError as exc:
            if live:
                typer.echo("")
            typer.echo(str(exc), err=True)
            failed += 1
            continue
        if live:
            typer.echo("")
        if not entries:
            typer.echo(f"{gid}: the log is empty, nothing written")
            continue
        new = write_log(entries, path)
        hands = sum(e.entry.startswith("-- starting hand #") for e in entries)
        hero = fetchmod.count_hero_lines(entries)
        typer.echo(
            f"{gid}: {hands} hands, {new} new lines, {hero} of your hole cards -> {path.name}"
        )
        if cookie and not hero and hands:
            typer.echo(
                "    no hole cards of yours: you did not play this game, or PokerNow did"
                " not accept the cookies (expired, or `apt` missing -- `npt` alone is not enough)"
            )
        if conn is not None:
            s = import_csv(conn, str(path))
            typer.echo(f"    imported: {s['entries_new']} new entries, {s['parse_misses']} parse misses")
    if failed:
        raise typer.Exit(1)
    if not import_:
        typer.echo("\nnext: pnt import")


#: The unpacked Chrome extension, shipped inside the package so that a pip or
#: pipx install has one to load. `pnt extension` prints this path.
EXTENSION_DIR = Path(__file__).parent / "extension"


@app.command()
def extension(
    open_folder: bool = typer.Option(False, "--open", help="Reveal it in a file manager."),
) -> None:
    """Where the Chrome extension lives, for `Load unpacked`.

    Printed rather than assumed, because an installed copy sits inside site-packages
    (or a pipx venv) rather than next to a checkout.
    """
    if not (EXTENSION_DIR / "manifest.json").exists():
        raise typer.BadParameter(f"no extension at {EXTENSION_DIR} -- this install is incomplete")
    typer.echo(str(EXTENSION_DIR))
    if open_folder:
        _reveal(EXTENSION_DIR)


def _reveal(path: Path) -> None:
    """Open a folder in the platform's file manager. Best-effort: never fatal."""
    import subprocess
    import sys

    try:
        if sys.platform == "win32":
            os.startfile(path)  # a directory this module constructed, not user input
        else:
            subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", str(path)],
                           check=False)  # fmt: skip
    except OSError as exc:
        typer.echo(f"(could not open the folder: {exc})")


@app.command()
def setup(
    db: Path = DbOpt,
    host: str = svc.DEFAULT_HOST,
    port: int = svc.DEFAULT_PORT,
    service: bool = typer.Option(True, help="Also install the always-on background server."),
    log_dir: Path | None = LogDirOpt,
    sample: bool = SampleOpt,
) -> None:
    """First run: create the database, import any logs, start the server.

    Exists because the steps had an ordering trap. `service install` refuses a
    database that does not exist yet -- rightly, since a missing file would be
    created empty and the HUD would silently show nothing -- but nothing created
    it for you, so a new install hit an error with no obvious next move. This does
    them in the order that works, and is safe to re-run.

    With no exports to import, it falls back to the bundled sample corpus, so the
    page it points you at has hands on it. `--no-sample` leaves the database empty
    for live capture to fill.
    """
    db = db.resolve()
    fresh = not db.exists()
    connect(db).close()
    typer.echo(f"{'created' if fresh else 'using'} database: {db}")

    folder = log_dir or LOG_DIR
    logs = _logs_in(folder)
    if not logs and sample:
        _announce_sample(folder)
        logs = _logs_in(BUNDLED_LOG_DIR)
    if logs:
        typer.echo(f"importing {len(logs)} log(s) from {Path(logs[0]).parent}")
        conn = connect(db)
        for path in logs:
            s = import_csv(conn, path)
            typer.echo(f"  {Path(path).name}: {s['hands']} hands, {s['parse_misses']} parse misses")
        conn.close()
    else:
        # Not an error. Live capture fills an empty database on its own; the export
        # folder only matters for backfilling games played before the extension.
        typer.echo(f"no {LOG_GLOB} in {folder} -- skipping import (live capture will fill it)")

    hands = connect(db).execute("SELECT COUNT(*) FROM hands").fetchone()[0]

    if service and sys.platform == "win32":
        service_install(db=db, host=host, port=port)
    elif service:
        typer.echo("")
        typer.echo(
            f"`pnt service` is Windows-only. Run `pnt serve --db {db}` in a terminal,"
            " or put that command under launchd (macOS) or systemd (Linux)."
        )

    typer.echo("")
    typer.echo("Next: load the extension in Chrome -- open chrome://extensions,")
    typer.echo("turn on Developer mode, choose 'Load unpacked', and pick this folder:")
    typer.echo(f"    {EXTENSION_DIR}")
    typer.echo("")
    typer.echo(f"Then open a PokerNow game. Charts: http://{host}:{port}/chart  ({hands} hands)")


@app.command()
def where(db: Path = DbOpt) -> None:
    """Print the paths this install is using: database, log folder, extension, log file.

    Every one of them has a default that can be overridden three different ways, so
    "which one is it actually using" is a question worth being able to answer without
    reading the source.
    """
    source = (
        "$PNT_LOG_DIR" if os.environ.get("PNT_LOG_DIR") else "default (~/Downloads/pokernow-logs)"
    )
    n = len(_logs_in(BUNDLED_LOG_DIR))
    rows = [
        ("database", str(db.resolve()), "--db" if db != DEFAULT_DB else "default"),
        ("log folder", str(LOG_DIR), source),
        (
            "live record",
            "on: live capture writes each game's CSV to the log folder"
            if SAVE_LOGS
            else "off",
            "PNT_SAVE_LOGS=0 turns it off; restart the service after changing it",
        ),
        ("sample logs", str(BUNDLED_LOG_DIR), f"{n} inside the package, used when the above is empty"),
        ("extension", str(EXTENSION_DIR), "inside the package"),
        ("server log", str(svc.LOG_FILE), "fixed"),
    ]
    width = max(len(name) for name, _, _ in rows)
    for name, value, note in rows:
        typer.echo(f"{name:<{width}}  {value}")
        typer.echo(f"{'':<{width}}  ({note})")


@app.command()
def rebuild(db: Path = DbOpt) -> None:
    """Re-derive every hand from stored raw entries. Run this after a parser change."""
    conn = connect(db)
    for (gid,) in conn.execute("SELECT game_id FROM raw_entries GROUP BY game_id").fetchall():
        s = rebuild_game(conn, gid)
        typer.echo(f"{gid}: {s['hands']} hands, {s['parse_misses']} parse misses")


@app.command()
def stats(
    db: Path = DbOpt,
    game: str = typer.Option(None, "--game", help="Restrict to one game_id."),
    filter_: str = typer.Option(None, "--filter", help="e.g. '3bet,position=BTN'"),
    min_hands: int = typer.Option(1, "--min-hands"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Per-player stats. Rates show `--` when the denominator is empty."""
    conn = connect(db)
    pred = parse_filter(filter_, display_names(conn)) if filter_ else None
    rows = report(conn, game_id=game, min_hands=min_hands, predicate=pred)
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
    else:
        if filter_:
            typer.echo(f"filter: {filter_}\n")
        _print_table(rows, "player")


@app.command()
def positions(
    alias: str,
    db: Path = DbOpt,
    split_by_size: bool = typer.Option(
        False, "--split-by-size", help="Separate 6-max from 5-max instead of pooling."
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """One player's stats broken down by position."""
    conn = connect(db)
    rows = positional_report(conn, alias, pool=not split_by_size)
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
    else:
        _print_table(rows, "position", key_width=24)


def _print_grid(grid: dict) -> None:
    """13x13 chart, one cell as `LABEL n`. A dot means never shown in this spot."""
    for row in grid["rows"]:
        typer.echo(
            " ".join(
                f"{label:<4}{grid['cells'][label]['n'] or '.':>3}" for label in row
            )
        )


def _print_composition(comp: dict) -> None:
    typer.echo(f"{'class':<24}{'n':>5}{'pct':>7}{'won':>5}{'net bb':>9}")
    typer.echo("-" * 50)
    for c in comp["classes"]:
        typer.echo(
            f"{c['class']:<24}{c['n']:>5}{_fmt(c['pct']):>7}{c['won']:>5}{c['net_bb']:>9}"
        )
        for d in c.get("details", []):
            typer.echo(
                f"  {d['class']:<22}{d['n']:>5}{_fmt(d['pct']):>7}{d['won']:>5}{d['net_bb']:>9}"
            )


@app.command("range")
def range_cmd(
    alias: str,
    db: Path = DbOpt,
    filter_: str = typer.Option(
        None, "--filter", help="The spot or line, e.g. 'opener,open_bb>=4,srp,cbet_flop'"
    ),
    by: str = typer.Option(
        "preflop", "--by", help="'preflop' for the 169-cell chart, 'made' for hand strength."
    ),
    game: str = typer.Option(None, "--game", help="Restrict to one game_id."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """What a player *had* in a spot, from the hands where their cards were shown.

    Coverage is the honest part: cards are only known at showdown, so the chart
    sees the hands that got there and not the ones that folded on the way.
    """
    if by not in ("preflop", "made"):
        raise typer.BadParameter("--by must be 'preflop' or 'made'")
    conn = connect(db)
    try:
        facts = facts_for(conn, alias, game)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if filter_:
        try:
            pred = parse_filter(filter_, display_names(conn))
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        facts = [f for f in facts if pred(f)]

    out = range_grid(facts) if by == "preflop" else composition(facts)
    if as_json:
        typer.echo(json.dumps(out, indent=2))
        return

    typer.echo(f"{alias}  filter: {filter_ or '(none)'}")
    typer.echo(
        f"hands in this spot: {out['hands']}   cards known: {out['known']}"
        f"   coverage: {_fmt(out['coverage'])}%"
    )
    r = out.get("raise")
    if r:
        typer.echo(
            f"preflop raise-to (bb), all {r['n']} raised hands: median {r['median']}"
            f"  mean {r['mean']}  mode {_fmt(r['mode'])}  min {r['min']}  max {r['max']}"
        )
    typer.echo("")
    if by == "preflop":
        _print_grid(out)
    else:
        _print_composition(out)


@app.command()
def sizing(
    alias: str,
    db: Path = DbOpt,
    street: str = typer.Option("flop", "--street", help="flop, turn or river."),
    kind: str = typer.Option(
        "cbet",
        "--kind",
        help="'cbet' their c-bets, 'bet' every first bet, 'faced_cbet' the c-bets they faced.",
    ),
    filter_: str = typer.Option(None, "--filter", help="The spot, e.g. 'srp,flop=ace_high'"),
    game: str = typer.Option(None, "--game", help="Restrict to one game_id."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """What a player had at each bet size: what they bet small with, what they overbet with.

    Each size lists the made hands that were shown, so read coverage first: the
    bluffs at any size are the hands that folded out before showdown.
    """
    if kind not in SIZING_KINDS:
        raise typer.BadParameter(f"--kind must be one of: {', '.join(SIZING_KINDS)}")
    conn = connect(db)
    try:
        facts = facts_for(conn, alias, game)
        if filter_:
            pred = parse_filter(filter_, display_names(conn))
            facts = [f for f in facts if pred(f)]
        out = sizing_tells(facts, street, kind)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if as_json:
        typer.echo(json.dumps(out, indent=2))
        return

    spot = {"cbet": "c-bet chances", "bet": "bets", "faced_cbet": "c-bets faced"}[kind]
    typer.echo(f"{alias}  {street} {kind}  filter: {filter_ or '(none)'}")
    typer.echo(f"hands in this spot: {out['hands']}   {spot}: {out['spot']}\n")
    for b in out["blocks"]:
        line = f"{b['size']:<8}{b['n']:>5}x  {_fmt(b['pct']):>5}% of {spot}"
        if kind == "faced_cbet" and b["n"]:
            line += f"   fold {_fmt(b['fold'])}%  call {_fmt(b['call'])}%  raise {_fmt(b['raise'])}%"
        if b["n"]:
            line += f"   shown {b['known']} ({_fmt(b['coverage'])}%)"
        typer.echo(line)
        if b["classes"]:
            _print_composition(b)
        typer.echo("")


@app.command()
def misses(db: Path = DbOpt, limit: int = 20) -> None:
    """Log lines the grammar did not recognize. Empty means the parse was total."""
    conn = connect(db)
    rows = conn.execute(
        "SELECT game_id, ord, reason, entry FROM parse_misses LIMIT ?", (limit,)
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM parse_misses").fetchone()[0]
    typer.echo(f"{total} parse miss(es)")
    for r in rows:
        typer.echo(f"  [{r['reason']}] {r['entry'][:120]}")


@app.command()
def redact(
    paths: list[str] | None = PathsArg,
    out: Path = OutOpt,
    log_dir: Path | None = LogDirOpt,
    audit: bool = AuditOpt,
) -> None:
    """Write publishable copies of your logs with your unshown hole cards removed.

    A raw export names your cards on every hand you were dealt in, folds included.
    This keeps a `Your hand is` entry only where the same two cards also appear in
    a `shows a` entry for that hand -- cards the table already saw -- and drops the
    rest. Everything else in the log is copied byte for byte.

    The copies are still importable: `pnt import` infers hero from showdowns, which
    is exactly what survives. Nothing is written over your originals.

    `--audit` skips writing and checks files instead. Run it on the folder you are
    about to commit; anything it prints is a hand you did not show.
    """
    folder = log_dir or LOG_DIR
    expanded = _expand(paths, folder)

    if audit:
        leaks = [line for p in expanded for line in rd.audit(Path(p))]
        for line in leaks:
            typer.echo(line)
        typer.echo(
            f"{len(expanded)} file(s): {len(leaks)} unshown hole-card entr"
            f"{'y' if len(leaks) == 1 else 'ies'}"
        )
        raise typer.Exit(1 if leaks else 0)

    out = out.resolve()
    sources = {Path(p).resolve().parent for p in expanded}
    if out in sources:
        raise typer.BadParameter(
            f"--out {out} is where the originals live; pick a different folder"
        )

    hero_hands = kept = 0
    for path in expanded:
        src = Path(path)
        p = rd.redact_file(src, out / src.name)
        hero_hands += p.hero_hands
        kept += p.kept
        typer.echo(f"{src.name}: removed {p.dropped} of {p.hero_hands} hole-card entries")

    leaks = [line for f in sorted(out.glob(LOG_GLOB)) for line in rd.audit(f)]
    if leaks:  # redact_file already refuses to write a partial redaction
        for line in leaks:
            typer.echo(line)
        raise typer.Exit(1)
    typer.echo("")
    typer.echo(
        f"{len(expanded)} file(s) -> {out}\n"
        f"{kept} of {hero_hands} hands kept their hole cards (the showdowns); "
        f"{hero_hands - kept} redacted. Verified: no unshown holding remains."
    )


@app.command()
def serve(
    db: Path = DbOpt,
    host: str = "127.0.0.1",
    port: int = svc.DEFAULT_PORT,
) -> None:
    """Run the local API that the HUD and your analysis scripts read.

    Lasts until the terminal closes. To keep it running in the background instead,
    use `pnt service install`.
    """
    typer.echo(f"range chart: http://{host}:{port}/chart   api docs: http://{host}:{port}/docs")
    if importlib.util.find_spec("uvicorn") is None:  # pragma: no cover
        raise typer.BadParameter("server extras not installed. Run: pip install -e '.[server]'")
    svc.run_server(db, host, port)


def _svc(fn, *args, **kwargs):
    """Call into `pnt.service`, turning its errors into a one-line CLI failure."""
    try:
        return fn(*args, **kwargs)
    except svc.ServiceError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


@service_app.command("install")
def service_install(
    db: Path = DbOpt, host: str = svc.DEFAULT_HOST, port: int = svc.DEFAULT_PORT
) -> None:
    """Run the server in the background for this Windows user, starting now.

    Re-run to change --db or --port: it replaces the existing task. --db must be
    the database you actually use; the task does not start in this folder.
    """
    busy = svc.port_in_use(host, port)
    interpreter = _svc(svc.install, db, host, port)
    typer.echo(f"registered task {svc.TASK_NAME!r}: starts at login, restarts on crash")
    typer.echo(f"  database:    {db.resolve()}")
    typer.echo(f"  interpreter: {interpreter}")
    typer.echo(f"  log:         {svc.LOG_FILE}")
    if busy:
        typer.echo(
            f"\nport {port} is already in use, probably by a `pnt serve` in a terminal."
            " The task is waiting and takes over as soon as that one stops."
        )
        return
    body = svc.wait_for_health(host, port)
    if body is None:
        typer.echo("\nnot answering yet. Check `pnt service log`.")
    else:
        typer.echo(f"\nup: http://{host}:{port}/chart  ({body['hands']} hands)")


@service_app.command("uninstall")
def service_uninstall() -> None:
    """Stop the background server and remove the task. The database is untouched."""
    removed = _svc(svc.uninstall)
    typer.echo(f"removed task {svc.TASK_NAME!r}" if removed else "not installed")


@service_app.command("start")
def service_start() -> None:
    """Start the background server now. It also starts at every login."""
    _svc(svc.start)
    typer.echo("started")


@service_app.command("stop")
def service_stop() -> None:
    """Stop the background server until `pnt service start` or the next login."""
    _svc(svc.stop)
    typer.echo("stopped")


@service_app.command("restart")
def service_restart() -> None:
    """Restart it. Needed after pulling code changes: a running server keeps the old code."""
    _svc(svc.restart)
    typer.echo("restarted")


@service_app.command("status")
def service_status(host: str = svc.DEFAULT_HOST, port: int = svc.DEFAULT_PORT) -> None:
    """Whether the task is installed and running, and whether the server answers."""
    state = _svc(svc.task_state)
    typer.echo(f"task:   {state or 'not installed'}")
    body = svc.health(host, port)
    if body is None:
        typer.echo(f"server: not answering on http://{host}:{port}")
    else:
        typer.echo(f"server: up on http://{host}:{port}  {body['hands']} hands  db {body['db']}")
    typer.echo(f"log:    {svc.LOG_FILE}")


@service_app.command("log")
def service_log(lines: int = typer.Option(40, "--lines", "-n")) -> None:
    """The last lines of the background server's log."""
    if not svc.LOG_FILE.exists():
        typer.echo(f"no log yet at {svc.LOG_FILE}")
        return
    for line in svc.tail(svc.LOG_FILE, lines):
        typer.echo(line)


@service_app.command("run", hidden=True)
def service_run(
    db: Path = DbOpt, host: str = svc.DEFAULT_HOST, port: int = svc.DEFAULT_PORT
) -> None:
    """What the scheduled task executes: the supervised server, logging to a file."""
    svc.run_service(db, host, port)


@alias_app.command("list")
def alias_list(db: Path = DbOpt) -> None:
    """Every canonical player and the PokerNow IDs mapped to them."""
    conn = connect(db)
    for r in conn.execute(
        "SELECT p.alias, COUNT(pi.pn_id) n, GROUP_CONCAT(pi.pn_id, ', ') ids,"
        " GROUP_CONCAT(DISTINCT pi.last_seen_name) names"
        " FROM players p JOIN player_identities pi ON pi.player_id = p.player_id"
        " GROUP BY p.player_id ORDER BY p.alias"
    ):
        typer.echo(f"{r['alias']:24s} {r['n']} id(s): {r['ids']}")
        if r["names"] and r["names"] != r["alias"]:
            typer.echo(f"{'':24s}   seen as: {r['names']}")


@alias_app.command("merge")
def alias_merge(source: str, target: str, db: Path = DbOpt) -> None:
    """Merge SOURCE into TARGET -- use when one human played from two devices.

    Nothing is recomputed: because no statistic is stored, repointing the identity
    rows *is* the merge.
    """
    conn = connect(db)
    moved = merge_players(conn, source, target)
    typer.echo(f"moved {len(moved)} identity row(s) from {source!r} to {target!r}")
    if moved:
        # Printed because the merge cannot be undone without them: the source
        # player row is gone, so this list is the only record of what was behind it.
        typer.echo(f"  ids: {' '.join(moved)}")
        typer.echo(f"  undo: pnt alias split {' '.join(moved)} --alias {source}")


@alias_app.command("rename")
def alias_rename(old: str, new: str, db: Path = DbOpt) -> None:
    """Rename a canonical player."""
    try:
        rename_player(connect(db), old, new)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"{old!r} -> {new!r}")


@alias_app.command("split")
def alias_split(
    pn_ids: list[str] = PnIdsArg,
    alias: str = AliasOpt,
    db: Path = DbOpt,
) -> None:
    """Move PokerNow IDs onto a new player. The inverse of `alias merge`.

    Undoes a merge (the merge prints the exact command), and separates two humans
    who were joined by mistake.
    """
    try:
        n = split_identities(connect(db), pn_ids, alias)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"moved {n} identity row(s) to {alias!r}")


@alias_app.command("export")
def alias_export(path: Path = AliasFileArg, db: Path = DbOpt) -> None:
    """Write every PokerNow ID and its alias to a CSV, so the table can be committed.

    Merges and renames are the one thing a re-import cannot rebuild; this file is
    how they survive a fresh database. `pnt alias import` reads it back.
    """
    pairs = export_aliases(connect(db))
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["pn_id", "alias"])
        writer.writerows(pairs)
    typer.echo(f"wrote {len(pairs)} id(s) across {len({a for _, a in pairs})} player(s) to {path}")


@alias_app.command("import")
def alias_import(path: Path = AliasFileArg, db: Path = DbOpt) -> None:
    """Apply a CSV from `pnt alias export`: every known ID moves to its alias.

    Safe to re-run. IDs not in the database yet are skipped -- import their logs,
    then run this again.
    """
    if not path.exists():
        raise typer.BadParameter(f"no alias file at {path}")
    with path.open(newline="", encoding="utf-8") as fh:
        pairs = [(r["pn_id"], r["alias"]) for r in csv.DictReader(fh)]
    moved, unknown = apply_aliases(connect(db), pairs)
    typer.echo(f"moved {moved} identity row(s); {unknown} id(s) not in this database yet")


if __name__ == "__main__":  # pragma: no cover
    app()
