"""`tag=` on the command line: every `--filter` reads your hand tags and the 🚩."""

from __future__ import annotations

import json
from collections import Counter

import pytest
from typer.testing import CliRunner

from pnt import cli
from pnt.db.conn import connect
from pnt.db.userdata import add_tag, tag_resolver
from pnt.ingest.importer import import_csv
from pnt.stats.filters import parse_filter
from pnt.stats.queries import facts_by_player
from pnt.stats.review import set_flag
from tests.conftest import ALL_LOGS


@pytest.fixture()
def tagged_db(tmp_path):
    """The fixtures with one hand tagged `study` and another flagged."""
    path = tmp_path / "t.sqlite"
    conn = connect(path)
    for log in ALL_LOGS:
        import_csv(conn, log)
    grouped, aliases, _ = facts_by_player(conn)
    rows = Counter((f.game_id, f.hand_number) for fs in grouped.values() for f in fs)
    (k1, n1), (k2, n2) = sorted(rows.items())[:2]
    add_tag(conn, *k1, "Study")
    set_flag(conn, *k2)
    alias = next(aliases[pid] for pid, fs in grouped.items() if any((f.game_id, f.hand_number) == k1 for f in fs))
    conn.close()
    return path, n1, n2, alias


def _run(*args: str):
    return CliRunner().invoke(cli.app, list(args))


def _hands(path, filter_: str) -> int:
    res = _run("stats", "--db", str(path), "--filter", filter_, "--json")
    assert res.exit_code == 0, res.output
    return sum(r["hands"] for r in json.loads(res.output))


def test_stats_filter_reads_tags_and_the_flag(tagged_db):
    path, n1, n2, _ = tagged_db
    # The hand, not the seat: every player's row of the tagged hand.
    assert _hands(path, "tag=study") == n1
    assert _hands(path, "tag=flagged") == n2
    assert _hands(path, "tag=study|flagged") == n1 + n2
    assert _hands(path, "tag=nobody-has-this") == 0

    total = _hands(path, "srp|!srp")
    assert _hands(path, "tag!=study") == total - n1


def test_other_commands_take_tag_too(tagged_db):
    path, _, _, alias = tagged_db
    res = _run("range", alias, "--db", str(path), "--filter", "tag=study", "--json")
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["hands"] == 1
    for cmd in ("tags", "lines", "bluffs", "timing"):
        res = _run(cmd, alias, "--db", str(path), "--filter", "tag=study", "--json")
        assert res.exit_code == 0, (cmd, res.output)


def test_a_bad_tag_is_an_error_not_a_crash(tagged_db):
    path, _, _, alias = tagged_db
    res = _run("range", alias, "--db", str(path), "--filter", "tag=a.b")
    assert res.exit_code != 0 and "tag" in res.output


def test_the_resolver_matches_the_servers_reading(tagged_db):
    path, *_ = tagged_db
    conn = connect(path)
    try:
        resolve = tag_resolver(conn)
        assert len(resolve("STUDY")) == 1 and len(resolve("flagged")) == 1
        assert parse_filter("tag=study", None, resolve) is not None
    finally:
        conn.close()
