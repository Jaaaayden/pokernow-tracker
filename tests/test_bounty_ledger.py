"""The 7-2 bounty ledger: per player won, paid and net, and hero's 72 counts.

A side bet moves chips between players and creates none, so the ledger must sum
to zero. The hero half must agree with the filters it prints, `hand=72` and
`hand=72,vpip`, run over the same hands.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from pnt import cli
from pnt.db.conn import connect
from pnt.ingest.csv_source import RawEntry
from pnt.ingest.importer import import_csv, ingest_entries, rebuild_game
from pnt.stats.bounty import DEALT_FILTER, PLAYED_FILTER, ledger
from pnt.stats.filters import parse_filter
from pnt.stats.queries import display_names, facts_for
from tests.conftest import ALL_LOGS, EDGE_LOGS, HU_GAME, local_client
from tests.test_bounty import GAME, LINES


def _add_bounty_game(conn) -> None:
    """The hand of test_bounty.py: Win @ AAA wins with 7-2, paid 30 by each of two."""
    entries = [
        RawEntry(ord=i, at=f"2026-01-01T00:00:{i:02d}.000Z", entry=t) for i, t in enumerate(LINES)
    ]
    ingest_entries(conn, GAME, entries, source="test")
    rebuild_game(conn, GAME)


@pytest.fixture()
def bdb(tmp_path):
    conn = connect(tmp_path / "b.sqlite")
    for path in ALL_LOGS + EDGE_LOGS:
        import_csv(conn, path)
    _add_bounty_game(conn)
    yield conn
    conn.close()


def _row(out: dict, player: str) -> dict:
    return next(r for r in out["players"] if r["player"] == player)


def test_the_fixture_bounty_hand_appears(bdb):
    out = ledger(bdb)
    win, pay1, pay2 = _row(out, "Win"), _row(out, "Pay1"), _row(out, "Pay2")
    assert win["won"] == {"count": 1, "chips": 60, "bb": 3.0}  # the hand's BB post is 20
    assert win["paid"] == {"count": 0, "chips": 0, "bb": 0.0}
    assert win["net"] == {"chips": 60, "bb": 3.0}
    assert pay1["paid"] == {"count": 1, "chips": 30, "bb": 1.5}
    assert pay2["net"] == {"chips": -30, "bb": -1.5}
    assert win["hands"] == pay1["hands"] == 1 and win["bb_known"] == 1
    # Ranked by net: the winner leads.
    assert out["players"][0]["player"] == "Win"
    assert out["total"] == {"won": 60, "paid": 60, "bounties": 1}


def test_the_ledger_sums_to_zero_across_players(bdb):
    for game in (None, GAME):
        out = ledger(bdb, game)
        assert out["balance"] == 0
        assert sum(r["net"]["chips"] for r in out["players"]) == 0
        assert sum(r["net"]["bb"] for r in out["players"]) == pytest.approx(0)
        assert out["total"]["won"] == out["total"]["paid"]


def test_a_game_with_no_bounties_has_an_empty_ledger(bdb):
    out = ledger(bdb, HU_GAME)
    assert out["players"] == [] and out["balance"] == 0


def test_only_bounty_players_are_listed(bdb):
    names = {r["player"] for r in ledger(bdb)["players"]}
    assert names == {"Win", "Pay1", "Pay2"}


def test_hero_72_counts(bdb):
    """Make Win the log's owner, holding 72, and the raise counts as played."""
    bdb.execute("UPDATE games SET hero_pn_id = 'AAA' WHERE game_id = ?", (GAME,))
    bdb.execute(
        "UPDATE hand_players SET hole_cards = '7h2d' WHERE pn_id = 'AAA'"
        " AND hand_id IN (SELECT hand_id FROM hands WHERE game_id = ?)",
        (GAME,),
    )
    bdb.commit()
    hero = next(h for h in ledger(bdb, GAME)["hero"] if h["player"] == "Win")
    assert hero["hands"] == hero["known"] == 1
    assert hero["dealt_72"] == hero["played_72"] == 1 and hero["played_pct"] == 100.0
    assert hero["dealt_filter"] == DEALT_FILTER and hero["played_filter"] == PLAYED_FILTER

    # A folded 72 is dealt but not played.
    bdb.execute(
        "DELETE FROM actions WHERE pn_id = 'AAA' AND action_type = 'raise'"
        " AND hand_id IN (SELECT hand_id FROM hands WHERE game_id = ?)",
        (GAME,),
    )
    bdb.commit()
    hero = next(h for h in ledger(bdb, GAME)["hero"] if h["player"] == "Win")
    assert hero["dealt_72"] == 1 and hero["played_72"] == 0 and hero["played_pct"] == 0.0


def test_hero_72_counts_reproduce_from_their_filters(bdb):
    """Each hero figure is the size of its filter over hero's own-log hands."""
    out = ledger(bdb)
    assert out["hero"], "the fixtures name a hero"
    assert any(h["dealt_72"] > h["played_72"] > 0 for h in out["hero"]), "a 72 played and one folded"
    names = display_names(bdb)
    dealt, played = parse_filter(DEALT_FILTER, names), parse_filter(PLAYED_FILTER, names)
    heroes = {(r["game_id"], r["hero_pn_id"]) for r in bdb.execute("SELECT game_id, hero_pn_id FROM games")}
    for h in out["hero"]:
        every = facts_for(bdb, h["player"])
        own = [f for f in every if (f.game_id, f.pn_id) in heroes]
        assert len(own) == h["hands"]
        assert sum(f.hole_cards is not None for f in own) == h["known"]
        assert sum(map(dealt, own)) == h["dealt_72"]
        assert sum(map(played, own)) == h["played_72"]
        _check_filters(h, every, names)


def _check_filters(h: dict, every, names) -> None:
    """A printed filter reproduces its count over every hand of the player, which
    is what the chart link runs it on; where it would not, none is printed."""
    for key, flt, count in (("dealt_filter", DEALT_FILTER, "dealt_72"), ("played_filter", PLAYED_FILTER, "played_72")):
        found = sum(map(parse_filter(flt, names), every))
        if h[key] is None:
            assert found > h[count]
        else:
            assert h[key] == flt and found == h[count]


def test_a_72_shown_in_someone_elses_log_unlinks_the_filter(bdb):
    """genericpoker's hands in a log they do not own count only when shown. Show a
    72 there: the counts stay their own logs', and the filter, which would find it,
    is withdrawn."""
    before = next(h for h in ledger(bdb)["hero"] if h["player"] == "genericpoker")
    assert before["dealt_filter"] == DEALT_FILTER
    row = bdb.execute(
        "SELECT hp.hand_id, hp.pn_id FROM hand_players hp JOIN hands h ON h.hand_id = hp.hand_id"
        " JOIN games g ON g.game_id = h.game_id"
        " JOIN player_identities pi ON pi.pn_id = hp.pn_id JOIN players p ON p.player_id = pi.player_id"
        " WHERE p.alias = 'genericpoker' AND g.hero_pn_id != hp.pn_id AND hp.hole_cards IS NULL LIMIT 1"
    ).fetchone()
    assert row is not None, "genericpoker sits in a log they do not own"
    bdb.execute("UPDATE hand_players SET hole_cards = '7c2c' WHERE hand_id = ? AND pn_id = ?", tuple(row))
    bdb.commit()
    after = next(h for h in ledger(bdb)["hero"] if h["player"] == "genericpoker")
    assert after["dealt_72"] == before["dealt_72"] and after["hands"] == before["hands"]
    assert after["dealt_filter"] is None
    _check_filters(after, facts_for(bdb, "genericpoker"), display_names(bdb))


def test_bounties_endpoint_and_cli(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    path = tmp_path / "api.sqlite"
    monkeypatch.setenv("PNT_DB", str(path))
    import importlib

    from pnt.server import app as app_module

    importlib.reload(app_module)
    conn = connect(path)
    import_csv(conn, ALL_LOGS[0])
    _add_bounty_game(conn)
    conn.close()

    client = local_client(app_module.app)
    body = client.get("/bounties").json()
    assert [r["player"] for r in body["players"]] == ["Win", "Pay1", "Pay2"]
    assert body["balance"] == 0 and body["hero"]
    assert client.get("/bounties", params={"game": GAME}).json()["total"]["bounties"] == 1

    run = CliRunner().invoke
    res = run(cli.app, ["bounties", "--db", str(path), "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["players"][0]["player"] == "Win"
    res = run(cli.app, ["bounties", "--db", str(path)])
    assert res.exit_code == 0, res.output
    assert "Win" in res.output and "+60" in res.output and "dealt 72" in res.output
