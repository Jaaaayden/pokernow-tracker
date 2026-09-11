"""`pnt` -- import logs, query stats, manage identities."""

from __future__ import annotations

import glob as globlib
import importlib.util
import json
import os
from pathlib import Path

import typer

from . import service as svc
from .db.conn import DEFAULT_DB, connect
from .ingest.importer import import_csv, merge_players, rebuild_game
from .stats.filters import parse_filter
from .stats.queries import facts_for, positional_report, report
from .stats.ranges import SIZING_KINDS, composition, range_grid, sizing_tells

app = typer.Typer(add_completion=False, help=__doc__)
alias_app = typer.Typer(help="Manage player identities.")
app.add_typer(alias_app, name="alias")
service_app = typer.Typer(
    help="Keep the server running in the background: starts at login, restarts on crash."
)
app.add_typer(service_app, name="service")

DbOpt = typer.Option(DEFAULT_DB, "--db", help="Path to the tracker database.")

#: Where PokerNow exports are kept. One folder, so `pnt import` with no argument
#: is the whole history. Override with the PNT_LOG_DIR environment variable.
LOG_DIR = Path(os.environ.get("PNT_LOG_DIR") or Path.home() / "Downloads" / "pokernow-logs")

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


@app.command("import")
def import_cmd(
    paths: list[str] | None = typer.Argument(
        None, help="CSV export path(s), directories or globs. Default: the log folder."
    ),
    db: Path = DbOpt,
) -> None:
    """Import PokerNow log exports. Safe to re-run: duplicate entries are ignored.

    With no argument, imports every log in the log folder (PNT_LOG_DIR, default
    ~/Downloads/pokernow-logs).
    """
    conn = connect(db)
    if paths:
        expanded = []
        for pat in paths:
            for p in globlib.glob(pat) or [pat]:
                expanded.extend(_logs_in(Path(p)) if Path(p).is_dir() else [p])
    else:
        expanded = _logs_in(LOG_DIR)
        if not expanded:
            raise typer.BadParameter(
                f"no {LOG_GLOB} in {LOG_DIR} -- put your exports there, "
                "pass paths explicitly, or set PNT_LOG_DIR"
            )
    if not expanded:
        raise typer.BadParameter("no files matched")
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
    pred = parse_filter(filter_) if filter_ else None
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
        pred = parse_filter(filter_)
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
            pred = parse_filter(filter_)
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
def serve(
    db: Path = DbOpt,
    host: str = "127.0.0.1",
    port: int = 8000,
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
    n = merge_players(conn, source, target)
    typer.echo(f"moved {n} identity row(s) from {source!r} to {target!r}")


@alias_app.command("rename")
def alias_rename(old: str, new: str, db: Path = DbOpt) -> None:
    """Rename a canonical player."""
    conn = connect(db)
    with conn:
        cur = conn.execute("UPDATE players SET alias = ? WHERE alias = ?", (new, old))
    if not cur.rowcount:
        raise typer.BadParameter(f"unknown alias: {old!r}")
    typer.echo(f"{old!r} -> {new!r}")


if __name__ == "__main__":  # pragma: no cover
    app()
