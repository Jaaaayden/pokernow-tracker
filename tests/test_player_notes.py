"""Player notes: what you know about a person that no log records.

A note is a judgement, like an alias, so the one invariant that matters is that
nothing which deletes a player row deletes a note with it: a merge appends it to
the target, an alias re-sync carries it to wherever the identities went, and
deleting a game never removes a player you have written about.
"""

from __future__ import annotations

import sqlite3

import pytest
from typer.testing import CliRunner

from pnt.db.conn import connect
from pnt.ingest.importer import (
    NOTE_JOIN,
    apply_aliases,
    delete_game,
    export_aliases,
    import_csv,
    merge_players,
    player_notes,
    set_player_note,
)
from tests.conftest import ALL_LOGS, HU_GAME, MULTIWAY_GAME, local_client


def _note(conn, alias: str) -> str | None:
    return (player_notes(conn).get(alias) or {}).get("note")


def _alias(conn, pn_id: str) -> str | None:
    row = conn.execute(
        "SELECT p.alias FROM players p JOIN player_identities pi USING (player_id)"
        " WHERE pi.pn_id = ?",
        (pn_id,),
    ).fetchone()
    return row[0] if row else None


def _groups(conn) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for pn_id, alias in export_aliases(conn):
        out.setdefault(alias, set()).add(pn_id)
    return out


# ------------------------------------------------------------------ storage --


def test_a_note_is_stripped_stamped_replaced_and_cleared_by_emptying(db):
    out = set_player_note(db, "genericpoker", "  tanks then jams with air \n")
    assert out["note"] == "tanks then jams with air" and out["noted_at"]
    assert set_player_note(db, "genericpoker", "never folds the river")["note"] == "never folds the river"
    assert set_player_note(db, "genericpoker", "   ") is None
    assert player_notes(db) == {}
    row = db.execute("SELECT notes, notes_at FROM players WHERE alias = 'genericpoker'").fetchone()
    assert row["notes"] is None and row["notes_at"] is None


def test_an_unknown_alias_is_refused(db):
    with pytest.raises(ValueError, match="unknown alias"):
        set_player_note(db, "nobody at all", "x")


def test_an_older_database_gains_the_stamp_column(tmp_path):
    """`players.notes` shipped in the first schema; `notes_at` arrives by ALTER."""
    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE players (player_id INTEGER PRIMARY KEY, alias TEXT NOT NULL UNIQUE,"
        " notes TEXT, created_at TEXT NOT NULL)"
    )
    old.execute("INSERT INTO players (alias, created_at) VALUES ('henry', '2026-01-01')")
    old.commit()
    old.close()

    conn = connect(path)
    assert "notes_at" in {r[1] for r in conn.execute("PRAGMA table_info(players)")}
    assert set_player_note(conn, "henry", "limps everything")["noted_at"]


# --------------------------------------------------- surviving row deletions --


def test_a_merge_appends_the_source_note_to_the_target(db):
    set_player_note(db, "onlybluffs", "bluffs every river")
    set_player_note(db, "genericpoker", "folds to 3-bets")

    merge_players(db, "onlybluffs", "genericpoker")

    assert _note(db, "genericpoker") == f"folds to 3-bets{NOTE_JOIN}bluffs every river"
    assert "onlybluffs" not in player_notes(db)


def test_a_merge_into_a_player_without_a_note_hands_it_over(db):
    set_player_note(db, "onlybluffs", "bluffs every river")
    merge_players(db, "onlybluffs", "genericpoker")
    assert _note(db, "genericpoker") == "bluffs every river"


def test_a_merge_from_a_player_without_a_note_leaves_the_target_alone(db):
    before = set_player_note(db, "genericpoker", "folds to 3-bets")
    merge_players(db, "onlybluffs", "genericpoker")
    assert player_notes(db)["genericpoker"] == before


def test_an_alias_resync_carries_an_emptied_players_note_to_its_ids(db):
    (a, _), (b, b_ids) = list(_groups(db).items())[:2]
    set_player_note(db, a, "straddles when tilted")
    set_player_note(db, b, "same person on a phone")

    # The file says `b`'s IDs belong to `a`: `b` is left with none and deleted.
    apply_aliases(db, [(i, a) for i in sorted(b_ids)] + [(i, a) for i in sorted(_groups(db)[a])])

    assert b not in _groups(db)
    assert _note(db, a) == f"straddles when tilted{NOTE_JOIN}same person on a phone"
    # Applied again it moves nothing, and the note does not grow.
    apply_aliases(db, export_aliases(db))
    assert _note(db, a) == f"straddles when tilted{NOTE_JOIN}same person on a phone"


def test_an_alias_resync_onto_a_new_name_keeps_the_note(db):
    """Every ID of a noted player moves onto a name the database has never seen: the
    player row that held the note is replaced by a new one, which inherits it."""
    alias, ids = next(iter(_groups(db).items()))
    set_player_note(db, alias, "calls too wide from the blinds")

    apply_aliases(db, [(i, "renamed in the file") for i in sorted(ids)])

    assert alias not in _groups(db)
    assert _note(db, "renamed in the file") == "calls too wide from the blinds"


def _only_in(conn, game_id: str) -> list[str]:
    """PokerNow IDs dealt into `game_id` and no other game."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT hp.pn_id FROM hand_players hp JOIN hands h USING (hand_id)"
            " WHERE h.game_id = ? AND hp.pn_id NOT IN ("
            "   SELECT hp.pn_id FROM hand_players hp JOIN hands h USING (hand_id)"
            "   WHERE h.game_id != ?)"
            " ORDER BY hp.pn_id",
            (game_id, game_id),
        )
    ]


def test_deleting_a_game_never_deletes_a_player_with_a_note(db):
    noted, untouched, *_ = _only_in(db, MULTIWAY_GAME)
    alias = _alias(db, noted)
    set_player_note(db, alias, "only ever played that one night")

    delete_game(db, MULTIWAY_GAME)

    assert _alias(db, noted) == alias
    assert _note(db, alias) == "only ever played that one night"
    assert _alias(db, untouched) is None, "an unwritten-about player still goes with its game"


# ----------------------------------------------------------------------- API --


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PNT_DB", str(tmp_path / "api.sqlite"))
    import importlib

    from pnt.server import app as app_module

    importlib.reload(app_module)

    conn = connect(tmp_path / "api.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    conn.close()
    return local_client(app_module.app)


def _listed(client, alias: str) -> dict:
    return next(p for p in client.get("/players").json() if p["alias"] == alias)


def test_the_api_writes_lists_and_clears_a_note(client):
    assert _listed(client, "genericpoker")["notes"] is None

    out = client.post("/players/genericpoker/note", json={"note": " overbets the turn with draws "}).json()
    assert out["alias"] == "genericpoker"
    assert out["note"] == "overbets the turn with draws" and out["noted_at"]
    listed = _listed(client, "genericpoker")
    assert listed["notes"] == out["note"] and listed["notes_at"] == out["noted_at"]

    cleared = client.post("/players/genericpoker/note", json={"note": ""}).json()
    assert cleared["note"] is None and cleared["noted_at"] is None
    assert _listed(client, "genericpoker")["notes"] is None


def test_the_api_refuses_an_unknown_player_and_a_write_without_the_header(client):
    from fastapi.testclient import TestClient

    assert client.post("/players/nobody/note", json={"note": "x"}).status_code == 404
    # Any open tab can POST to 127.0.0.1; only this server's pages and the
    # extension can add the header.
    bare = TestClient(client.app, base_url="http://127.0.0.1")
    assert bare.post("/players/genericpoker/note", json={"note": "x"}).status_code == 403
    assert _listed(client, "genericpoker")["notes"] is None


def test_the_hud_carries_each_seats_note(client):
    seats = client.get(f"/hud/{HU_GAME}").json()["seats"]
    assert all(s["note"] is None for s in seats)
    alias = seats[0]["alias"]
    client.post(f"/players/{alias}/note", json={"note": "min-raises the nuts"})
    again = {s["alias"]: s["note"] for s in client.get(f"/hud/{HU_GAME}").json()["seats"]}
    assert again[alias] == "min-raises the nuts"
    assert all(n is None for a, n in again.items() if a != alias)


def test_a_merge_through_the_api_keeps_both_notes(client):
    client.post("/players/onlybluffs/note", json={"note": "bluffs every river"})
    client.post("/players/genericpoker/note", json={"note": "folds to 3-bets"})
    client.post("/aliases/merge", json={"source": "onlybluffs", "target": "genericpoker"})
    assert _listed(client, "genericpoker")["notes"] == f"folds to 3-bets{NOTE_JOIN}bluffs every river"


# ----------------------------------------------------------------------- CLI --


def test_the_cli_writes_prints_lists_and_clears(db, tmp_path):
    from pnt import cli

    path = str(tmp_path / "t.sqlite")
    runner = CliRunner()
    assert "nothing noted" in runner.invoke(cli.app, ["player-note", "--db", path]).output

    r = runner.invoke(cli.app, ["player-note", "genericpoker", "never folds a pair", "--db", path])
    assert r.exit_code == 0 and "noted" in r.output
    shown = runner.invoke(cli.app, ["player-note", "genericpoker", "--db", path]).output
    assert shown.strip() == "never folds a pair"
    listed = runner.invoke(cli.app, ["player-note", "--db", path]).output
    assert "genericpoker" in listed and "| never folds a pair" in listed

    assert runner.invoke(cli.app, ["player-note", "genericpoker", "--clear", "--db", path]).exit_code == 0
    assert "nothing noted" in runner.invoke(cli.app, ["player-note", "--db", path]).output
    assert "no note" in runner.invoke(cli.app, ["player-note", "genericpoker", "--db", path]).output
    assert runner.invoke(cli.app, ["player-note", "nobody", "x", "--db", path]).exit_code != 0
    assert runner.invoke(cli.app, ["player-note", "nobody", "--db", path]).exit_code != 0
    assert runner.invoke(cli.app, ["player-note", "genericpoker", "x", "--clear", "--db", path]).exit_code != 0
