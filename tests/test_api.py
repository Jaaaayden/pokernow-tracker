"""Service tests. The /ingest contract is fixed here, before the extension exists."""

from __future__ import annotations

import pytest

from pnt.ingest.csv_source import read_csv
from tests.conftest import ALL_LOGS, HU, HU_GAME

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PNT_DB", str(tmp_path / "api.sqlite"))
    import importlib

    from pnt.server import app as app_module

    importlib.reload(app_module)

    from pnt.db.conn import connect
    from pnt.ingest.importer import import_csv

    conn = connect(tmp_path / "api.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    conn.close()
    return TestClient(app_module.app)


def test_health_reports_a_total_parse(client):
    body = client.get("/health").json()
    assert body["hands"] == 549
    assert body["parse_misses"] == 0


def test_stats_endpoint(client):
    rows = client.get("/stats", params={"min_hands": 100}).json()
    assert rows
    assert {"player", "hands", "vpip", "pfr"} <= set(rows[0])


def test_filter_is_validated_not_silently_ignored(client):
    """A typo'd filter must 400, not quietly return unfiltered stats."""
    r = client.get("/stats", params={"filter": "nonsense_stat"})
    assert r.status_code == 400
    assert "unknown filter term" in r.json()["detail"]


def test_filter_narrows_results(client):
    everything = {r["player"]: r for r in client.get("/stats").json()}
    threebet = {r["player"]: r for r in client.get("/stats", params={"filter": "3bet"}).json()}
    assert threebet
    for name, row in threebet.items():
        assert row["hands"] <= everything[name]["hands"]


def test_ingest_is_idempotent(client):
    """The extension will re-send overlapping windows; that must be free."""
    entries = [
        {"entry": e.entry, "at": e.at, "order": e.ord} for e in read_csv(HU)
    ]
    before = client.get("/stats").json()

    r = client.post("/ingest", json={"game_id": HU_GAME, "entries": entries})
    assert r.status_code == 200
    assert r.json()["new"] == 0, "already-known entries must not be re-inserted"
    assert client.get("/stats").json() == before


def test_ingest_new_game_then_query(client, tmp_path):
    entries = [
        {"entry": e.entry, "at": e.at, "order": e.ord} for e in read_csv(HU)
    ]
    r = client.post("/ingest", json={"game_id": "fresh-game", "entries": entries})
    body = r.json()
    assert body["new"] == len(entries)
    assert body["hands"] == 188
    assert body["parse_misses"] == 0

    rows = client.get("/stats", params={"game": "fresh-game"}).json()
    assert sum(r["hands"] for r in rows) == 188 * 2  # two seats per heads-up hand


def test_hud_endpoint_keys_by_pn_id_not_seat(client):
    body = client.get(f"/hud/{HU_GAME}").json()
    assert body["seats"]
    for s in body["seats"]:
        assert s["pn_id"]
        # Lifetime stats, across every game this identity appears in.
        assert s["stats"]["hands"] >= 188


def test_hand_replay(client):
    rows = client.get("/stats").json()
    assert rows
    body = client.get("/hands/1").json()
    assert body["hand"]["hand_number"]
    assert body["players"]
    assert body["actions"]
    assert [a["seq"] for a in body["actions"]] == sorted(a["seq"] for a in body["actions"])


def test_merge_endpoint(client):
    before = {r["player"]: r for r in client.get("/stats").json()}
    total = before["genericpoker"]["hands"] + before["onlybluffs"]["hands"]
    r = client.post("/aliases/merge", json={"source": "onlybluffs", "target": "genericpoker"})
    assert r.json()["moved"] == 1
    after = {r["player"]: r for r in client.get("/stats").json()}
    assert "onlybluffs" not in after
    assert after["genericpoker"]["hands"] == total


def test_merge_unknown_alias_404s(client):
    r = client.post("/aliases/merge", json={"source": "ghost", "target": "genericpoker"})
    assert r.status_code == 404
