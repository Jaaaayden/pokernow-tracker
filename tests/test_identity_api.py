"""Merging, splitting and renaming through the API.

These are the operations the players page performs, and the only ones that change
what a stat *means* rather than what it is computed from. The invariant that
matters is the round trip: a merge followed by its undo must leave every number
exactly where it started -- which is only true because nothing is materialized.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from pnt.stats.queries import report
from tests.conftest import ALL_LOGS


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """The API over a throwaway database, never the real one.

    It opens its own connections through PNT_DB rather than being handed one:
    FastAPI runs sync endpoints in a worker thread, and a SQLite connection may
    only be used on the thread that created it.
    """
    path = tmp_path / "api.sqlite"
    monkeypatch.setenv("PNT_DB", str(path))
    import importlib

    from pnt.server import app as app_module

    importlib.reload(app_module)

    from pnt.db.conn import connect
    from pnt.ingest.importer import import_csv

    conn = connect(path)
    for log in ALL_LOGS:
        import_csv(conn, log)
    conn.close()
    return TestClient(app_module.app), path


@pytest.fixture()
def client(api):
    return api[0]


@pytest.fixture()
def stats(api):
    """Every player's derived stats, read fresh each call."""
    from pnt.db.conn import connect

    def read():
        conn = connect(api[1])
        try:
            return {r["player"]: r for r in report(conn)}
        finally:
            conn.close()

    return read


def test_merge_reports_the_ids_it_moved(client):
    body = client.post("/aliases/merge", json={"source": "onlybluffs", "target": "genericpoker"})
    assert body.status_code == 200
    data = body.json()
    assert data["moved"] == len(data["pn_ids"]) == 1
    # The undo instructions must be complete on their own: after this call the
    # source player row is gone and nothing else records what was behind it.
    assert data["undo"] == {"pn_ids": data["pn_ids"], "alias": "onlybluffs"}


def test_merge_then_undo_restores_every_stat(client, stats):
    before = stats()
    assert {"onlybluffs", "genericpoker"} <= before.keys()

    undo = client.post(
        "/aliases/merge", json={"source": "onlybluffs", "target": "genericpoker"}
    ).json()["undo"]
    merged = stats()
    assert "onlybluffs" not in merged
    assert merged["genericpoker"]["hands"] == (
        before["genericpoker"]["hands"] + before["onlybluffs"]["hands"]
    )

    assert client.post("/aliases/split", json=undo).status_code == 200
    assert stats() == before, "undoing a merge must leave every number where it was"


def test_split_refuses_to_empty_a_player(client):
    """Taking every id off a player would strand an empty row; that is a rename."""
    ids = next(p for p in client.get("/players").json() if p["alias"] == "genericpoker")
    r = client.post(
        "/aliases/split",
        json={"pn_ids": [i["pn_id"] for i in ids["identities"]], "alias": "somewhere-else"},
    )
    assert r.status_code == 400
    assert "empty" in r.json()["detail"]


def test_split_rejects_an_unknown_id_and_a_taken_alias(client):
    assert client.post("/aliases/split", json={"pn_ids": ["nope"], "alias": "x"}).status_code == 400
    real = client.get("/players").json()[0]["identities"][0]["pn_id"]
    r = client.post("/aliases/split", json={"pn_ids": [real], "alias": "HSJ"})
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"]


def test_rename_round_trips_and_guards_collisions(client):
    assert client.post("/aliases/rename", json={"old": "HSJ", "new": " Zay "}).status_code == 200
    aliases = {p["alias"] for p in client.get("/players").json()}
    assert "Zay" in aliases and "HSJ" not in aliases, "the new name is stored trimmed"

    # Renaming onto an existing player is a merge in disguise, and would lose one
    # of them to the UNIQUE constraint; it is refused with the remedy named.
    clash = client.post("/aliases/rename", json={"old": "Zay", "new": "genericpoker"})
    assert clash.status_code == 400
    assert "merge" in clash.json()["detail"]

    assert client.post("/aliases/rename", json={"old": "nobody", "new": "x"}).status_code == 400
    assert client.post("/aliases/rename", json={"old": "Zay", "new": "   "}).status_code == 400


def test_players_counts_hands_without_deriving_them(client, stats):
    """The page's numbers must agree with the derived report, by a cheaper route."""
    derived = stats()
    for p in client.get("/players").json():
        assert p["hands"] == sum(i["hands"] for i in p["identities"])
        if p["alias"] in derived:
            assert p["hands"] == derived[p["alias"]]["hands"]
