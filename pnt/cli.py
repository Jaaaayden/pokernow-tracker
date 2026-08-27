"""`pnt` -- import logs, query stats, manage identities."""

from __future__ import annotations

import glob as globlib
import json
from pathlib import Path

import typer

from .db.conn import DEFAULT_DB, connect
from .ingest.importer import import_csv, merge_players, rebuild_game
from .stats.filters import parse_filter
from .stats.queries import positional_report, report

app = typer.Typer(add_completion=False, help=__doc__)
alias_app = typer.Typer(help="Manage player identities.")
app.add_typer(alias_app, name="alias")

DbOpt = typer.Option(DEFAULT_DB, "--db", help="Path to the tracker database.")

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


@app.command("import")
def import_cmd(
    paths: list[str] = typer.Argument(..., help="CSV export path(s); globs allowed."),
    db: Path = DbOpt,
) -> None:
    """Import PokerNow log exports. Safe to re-run: duplicate entries are ignored."""
    conn = connect(db)
    expanded = [p for pat in paths for p in (globlib.glob(pat) or [pat])]
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
    """Run the local API that the HUD and your analysis scripts read."""
    import os

    os.environ["PNT_DB"] = str(db)
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise typer.BadParameter(
            "server extras not installed. Run: pip install -e '.[server]'"
        ) from exc
    uvicorn.run("server.app:app", host=host, port=port)


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
