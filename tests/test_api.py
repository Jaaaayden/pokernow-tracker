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


def test_live_capture_keeps_a_csv_of_the_game_in_the_log_folder(client):
    """The folder is a running record: a captured game lands there as an export."""
    from pnt.ingest import log_folder

    rows = read_csv(HU)
    first, rest = rows[:1500], rows[1500:]
    wire = lambda es: [{"entry": e.entry, "at": e.at, "order": e.ord} for e in es]

    client.post("/ingest", json={"game_id": "live-game", "entries": wire(first)})
    path = log_folder.log_path(log_folder.LOG_DIR, "live-game")
    assert read_csv(path) == first

    # The extension's history walk: ingest without rebuilding, then rebuild once.
    client.post("/ingest", json={"game_id": "live-game", "entries": wire(rest), "rebuild": False})
    assert read_csv(path) == first  # nothing written until the game is rebuilt
    client.post("/rebuild/live-game")
    assert read_csv(path) == rows


def test_saving_logs_can_be_turned_off(client, monkeypatch):
    from pnt.ingest import log_folder
    from pnt.server import app as app_module

    monkeypatch.setattr(app_module, "SAVE_LOGS", False)
    entries = [{"entry": e.entry, "at": e.at, "order": e.ord} for e in read_csv(HU)]
    client.post("/ingest", json={"game_id": "unsaved", "entries": entries})
    assert not log_folder.log_path(log_folder.LOG_DIR, "unsaved").exists()
    assert client.get("/health").json()["log_folder"] is None


def test_a_log_folder_that_cannot_be_written_never_fails_capture(client, monkeypatch, tmp_path):
    from pnt.ingest import log_folder

    blocker = tmp_path / "not-a-folder"
    blocker.write_text("a file where the folder should be")
    monkeypatch.setattr(log_folder, "LOG_DIR", blocker)
    entries = [{"entry": e.entry, "at": e.at, "order": e.ord} for e in read_csv(HU)]
    r = client.post("/ingest", json={"game_id": "still-captured", "entries": entries})
    assert r.status_code == 200
    assert r.json()["hands"] == 188


def test_hud_endpoint_keys_by_pn_id_not_seat(client):
    body = client.get(f"/hud/{HU_GAME}").json()
    assert body["seats"]
    from pnt.server.app import db

    conn = db()
    for s in body["seats"]:
        assert s["pn_id"]
        # Lifetime stats, across every game this identity appears in.
        assert s["stats"]["hands"] >= 188
        # This game alone, beside them, with the same keys.
        dealt_here = conn.execute(
            "SELECT COUNT(*) FROM hand_players hp JOIN hands h ON h.hand_id = hp.hand_id"
            " WHERE h.game_id = ? AND hp.pn_id = ?",
            (HU_GAME, s["pn_id"]),
        ).fetchone()[0]
        assert 0 < s["session"]["hands"] == dealt_here <= s["stats"]["hands"]
        assert set(s["session"]) == set(s["stats"])


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
    assert "hands-sort" in r.text, "the hand list can be ordered by pot size"


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


def test_hands_rows_name_the_villain(client):
    """The drill-down payload carries who the hand was against, resolved to names."""
    rows = client.get("/players/genericpoker/hands").json()["hands"]
    assert rows
    # The contract chart.html's stale-server guard tests against.
    assert "ip" in rows[0]

    aliases = {p["alias"] for p in client.get("/players").json()}
    for r in rows:
        assert r["ip"] in (True, False, None)
        assert set(r["vs_cbet"]) <= {"flop", "turn", "river"}
        assert set(r["led_into"]) <= {"flop", "turn", "river"}
        assert "genericpoker" not in r["vs"], "a player is never their own opponent"
        assert set(r["vs"]) <= aliases, "opponents come through as display names"
        if r["ip"] is None:
            assert r["pos_order"] is None
        else:
            assert r["pos_players"] == len(r["vs"]) + 1
            assert r["ip"] == (r["pos_order"] == r["pos_players"] - 1)

    # Every hand in a faced-a-flop-c-bet spot knows who made that c-bet.
    faced = client.get(
        "/players/genericpoker/hands", params={"filter": "faced_cbet_flop=small"}
    ).json()["hands"]
    assert faced and all(r["vs_cbet"].get("flop") for r in faced)


def test_hands_filter_by_pot_and_opponent(client):
    """The two drill-down filters: big enough pots, and hands against one person."""
    rows = client.get("/players/genericpoker/hands").json()["hands"]
    assert all(isinstance(r["pot"], int) and r["pot"] >= 0 for r in rows)
    floor = sorted(r["pot"] for r in rows)[len(rows) // 2]
    big = client.get("/players/genericpoker/hands", params={"filter": f"pot>={floor}"}).json()
    assert 0 < len(big["hands"]) < len(rows)
    assert all(r["pot"] >= floor for r in big["hands"])

    vs = client.get("/players/genericpoker/hands", params={"filter": "vs=Chris"}).json()
    assert vs["hands"] and all("Chris" in r["vs"] for r in vs["hands"])
    assert len(vs["hands"]) < len(rows)
    r = client.get("/players/genericpoker/hands", params={"filter": "vs=nobody"})
    assert r.status_code == 400 and "unknown player" in r.json()["detail"]
    # /stats takes the same terms: everyone's figures in hands against one player.
    r = client.get("/stats", params={"filter": "vs=Chris"})
    assert r.status_code == 200 and "Chris" not in {row["player"] for row in r.json()}
    assert client.get("/stats", params={"filter": "vs=nobody"}).status_code == 400


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


def test_spot_help_lists_every_term(client):
    body = client.get("/filters").json()
    assert {"groups", "positions", "sizes", "textures", "operators"} <= set(body)
    assert any(t["term"] == "vs=NAME" for g in body["groups"] for t in g["terms"])
    script = client.get("/filter-help.js")
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    for page in ("/chart", "/stats.html", "/allin.html"):
        assert "/filter-help.js" in client.get(page).text


def test_chart_page_links_to_the_stats_page(client):
    assert "/stats.html" in client.get("/chart").text


@pytest.fixture()
def fast_sampling(monkeypatch):
    from pnt.stats import equity

    monkeypatch.setattr(equity, "SAMPLES", 2000)


def test_allin_serves_json_to_scripts_and_a_page_to_browsers(client, fast_sampling):
    rows = client.get("/allin").json()
    assert rows
    assert {"player", "hands", "net_bb", "adjusted_bb", "diff_bb", "by_street", "skipped"} <= set(rows[0])
    fewer = client.get("/allin", params={"min_hands": 5}).json()
    assert 0 < len(fewer) <= len(rows) and all(r["hands"] >= 5 for r in fewer)
    assert client.get("/allin", params={"filter": "nope"}).status_code == 400
    page = client.get("/allin", headers={"accept": "text/html"})
    assert page.headers["content-type"].startswith("text/html")
    assert client.get("/allin.html").text == page.text
    assert "/players/" in page.text and "/allin" in page.text


def test_player_allin_drilldown(client, fast_sampling):
    body = client.get("/players/genericpoker/allin").json()
    assert body["player"] == "genericpoker" and body["hands"]
    row = body["hands"][0]
    assert {"equity", "expected", "actual_bb", "adjusted_bb", "diff_bb", "villains", "board", "method"} <= set(row)
    assert client.get("/players/ghost/allin").status_code == 404
    assert client.get("/players/genericpoker/allin", params={"filter": "nope"}).status_code == 400
    vs = client.get("/players/genericpoker/allin", params={"filter": "vs=Chris"}).json()
    assert vs["hands"] and all(v["player"] == "Chris" for h in vs["hands"] for v in h["villains"])


def test_the_replay_renderer_is_one_script_shared_by_both_pages(client):
    script = client.get("/replay.js")
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert "pntReplay" in script.text
    for page in ("/chart", "/allin.html"):
        assert "/replay.js" in client.get(page).text
    assert "function renderReplay" not in client.get("/chart").text


def test_the_front_door_links_every_page(client):
    text = client.get("/").text
    for href in ("/stats.html", "/chart", "/players.html", "/allin.html"):
        assert href in text
