"""Service tests. The /ingest contract is fixed here, before the extension exists."""

from __future__ import annotations

import pytest

from pnt.ingest.csv_source import read_csv
from tests.conftest import ALL_LOGS, HU, HU_GAME

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


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
    assert isinstance(body["voluntary_shows"], list)


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


def test_range_endpoint(client):
    body = client.get("/players/genericpoker/range", params={"filter": "opener,srp"}).json()
    assert body["by"] == "preflop"
    assert len(body["cells"]) == 169
    assert body["known"] <= body["hands"]
    made = client.get("/players/genericpoker/range", params={"by": "made", "filter": "wtsd"}).json()
    assert made["classes"]
    assert sum(c["n"] for c in made["classes"]) == made["known"]


def test_range_endpoint_validates_input(client):
    assert client.get("/players/ghost/range").status_code == 404
    assert client.get("/players/genericpoker/range", params={"filter": "nope"}).status_code == 400
    assert client.get("/players/genericpoker/range", params={"by": "sideways"}).status_code == 422


def test_chart_page_is_served(client):
    r = client.get("/chart")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "/range" in r.text, "the page must read from the range endpoint"
    assert "/sizing" in r.text and "/hands" in r.text


def test_sizing_endpoint(client):
    body = client.get("/players/genericpoker/sizing", params={"street": "flop"}).json()
    assert body["kind"] == "cbet"
    assert [b["size"] for b in body["blocks"]] == ["small", "medium", "large", "overbet", "check"]
    assert sum(b["n"] for b in body["blocks"]) == body["spot"]
    faced = client.get(
        "/players/genericpoker/sizing", params={"street": "turn", "kind": "faced_cbet"}
    ).json()
    assert all({"fold", "call", "raise", "continued"} <= set(b) for b in faced["blocks"])


def test_sizing_endpoint_validates_input(client):
    url = "/players/genericpoker/sizing"
    assert client.get(url, params={"street": "preflop"}).status_code == 422
    assert client.get(url, params={"kind": "limp"}).status_code == 422
    assert client.get(url, params={"filter": "cbet_flop=huge"}).status_code == 400
    assert client.get("/players/ghost/sizing").status_code == 404


def test_hands_endpoint_lists_the_spot(client):
    rows = client.get("/players/genericpoker/hands", params={"filter": "wtsd"}).json()["hands"]
    grid = client.get("/players/genericpoker/range", params={"filter": "wtsd"}).json()
    assert len(rows) == grid["hands"]
    assert all(r["wtsd"] for r in rows)
    ids = {r["hand_id"] for r in rows}
    for cell in grid["cells"].values():
        assert len(cell["hand_ids"]) == cell["n"]
        assert set(cell["hand_ids"]) <= ids


def test_hand_replay_names_every_player(client):
    body = client.get("/hands/1").json()
    assert set(body["names"]) == {p["pn_id"] for p in body["players"]}
    assert body["hand"]["bb_effective"]


def test_player_stats_endpoint(client):
    """What the chart page's postflop strip reads: one player, inside a spot."""
    all_hands = client.get("/players/genericpoker/stats").json()
    srp = client.get("/players/genericpoker/stats", params={"filter": "srp"}).json()
    assert srp["player"] == "genericpoker"
    assert {"cbet_flop", "raise_cbet_flop", "donk_flop", "cbet_flop_sizes"} <= set(srp)
    assert srp["hands"] < all_hands["hands"]
    assert client.get("/players/ghost/stats").status_code == 404
    assert client.get("/players/genericpoker/stats", params={"filter": "nope"}).status_code == 400


def test_stats_serves_json_to_scripts_and_a_page_to_browsers(client):
    """The HUD and curl must keep getting JSON from /stats; a browser gets the page."""
    assert isinstance(client.get("/stats").json(), list)
    assert isinstance(client.get("/stats", headers={"accept": "application/json"}).json(), list)
    page = client.get("/stats", headers={"accept": "text/html,application/xhtml+xml"})
    assert page.headers["content-type"].startswith("text/html")
    assert "/chart?" in page.text, "the page must link back to the range chart"
    direct = client.get("/stats.html")
    assert direct.status_code == 200 and direct.text == page.text


def test_chart_page_links_to_the_stats_page(client):
    assert "/stats.html" in client.get("/chart").text
