"""Service tests. The /ingest contract is fixed here, before the extension exists."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pnt.ingest.csv_source import read_csv
from tests.conftest import ALL_LOGS, HU, HU_GAME, local_client

fastapi = pytest.importorskip("fastapi")


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
    return local_client(app_module.app)


def test_live_endpoint_is_quiet_between_hands_and_validates_min(client):
    assert client.get(f"/live/{HU_GAME}").json() == {"game_id": HU_GAME, "hand": None}
    assert client.get("/live/nope").json()["hand"] is None
    assert client.get(f"/live/{HU_GAME}", params={"min": 0}).status_code == 422
    assert client.get(f"/live/{HU_GAME}", params={"known": -1}).status_code == 422
    assert client.get(f"/live/{HU_GAME}", params={"known": 0}).status_code == 200


def test_live_endpoint_shows_the_hand_in_progress(client):
    """Push the heads-up log back in minus the tail of its last hand: that hand is
    live again, with everyone's spot resolved against the history already stored."""
    entries = sorted(read_csv(HU), key=lambda e: e.ord)
    last = max(int(e.entry.split("#")[1].split(" ")[0]) for e in entries if e.entry.startswith("-- starting hand #"))
    start = next(i for i, e in enumerate(entries) if e.entry.startswith(f"-- starting hand #{last} "))
    # A fresh game id, so the stored history is the whole heads-up log and the
    # live hand is this one alone.
    game = "live-probe"
    body = {
        "game_id": game,
        "entries": [{"entry": e.entry, "at": e.at, "order": e.ord} for e in entries[start : start + 6]],
        "source": "test",
        "rebuild": False,
    }
    assert client.post("/ingest", json=body).status_code == 200
    snap = client.get(f"/live/{game}").json()
    assert snap["hand_number"] == last
    assert snap["to_act"] in {p["pn_id"] for p in snap["players"]}
    assert {p["position"] for p in snap["players"]} == {"BTN/SB", "BB"}
    acting = next(p for p in snap["players"] if p["pn_id"] == snap["to_act"])
    assert acting["node"]["decision"] is None
    assert acting["resolved"] is not None and acting["resolved"]["hands"] > 0
    assert "filter" in acting["resolved"]
    assert set(acting["resolved"]) >= {
        "filter", "hands", "known", "exact", "relaxed", "decisions", "label",
        "decision", "arrived", "street", "texture", "showings",
    }
    assert set(acting["resolved"]["showings"]) == set(acting["resolved"]["decisions"])


def test_health_reports_a_total_parse(client):
    body = client.get("/health").json()
    assert body["hands"] == 549
    assert body["parse_misses"] == 0
    # `pnt service restart` tells the new server from a stale one by this.
    assert body["pid"] == os.getpid()


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


def test_live_capture_writes_no_log_until_a_hand_is_dealt(client):
    """Joining a table and leaving before a deal must not leave a file of nothing."""
    from pnt.ingest import log_folder

    rows = read_csv(HU)
    first_hand = next(i for i, e in enumerate(rows) if e.entry.startswith("-- starting hand #"))
    before, after = rows[:first_hand], rows[first_hand:]
    assert before, "the sample should have lines ahead of its first hand"
    wire = lambda es: [{"entry": e.entry, "at": e.at, "order": e.ord} for e in es]

    client.post("/ingest", json={"game_id": "not-dealt-yet", "entries": wire(before)})
    path = log_folder.log_path(log_folder.LOG_DIR, "not-dealt-yet")
    assert not path.exists()

    # The first hand writes the file, with the lines from before it.
    client.post("/ingest", json={"game_id": "not-dealt-yet", "entries": wire(after)})
    assert read_csv(path) == rows


def test_deleting_a_captured_log_removes_the_game_instead_of_rewriting_it(client):
    """Before sync, the next rebuild wrote the whole deleted file back out of the database."""
    from pnt.ingest import log_folder

    wire = [{"entry": e.entry, "at": e.at, "order": e.ord} for e in read_csv(HU)]
    client.post("/ingest", json={"game_id": "deleted-game", "entries": wire})
    path = log_folder.log_path(log_folder.LOG_DIR, "deleted-game")
    path.unlink()

    client.post("/rebuild/deleted-game")
    assert not path.exists()
    assert client.get("/stats", params={"game": "deleted-game"}).json() == []


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
        # Lifetime tags beside them, every one with the count it rests on.
        assert {"archetype", "tags", "profile"} <= set(s["tags"])
        assert s["tags"]["profile"]["hands"] == s["stats"]["hands"]
        for t in s["tags"]["tags"]:
            assert {"id", "label", "kind", "tip", "n", "hits", "pct", "filter", "by"} <= set(t)


def test_player_tags_endpoint(client):
    body = client.get("/players/genericpoker/tags").json()
    assert body["player"] == "genericpoker" and body["filter"] is None
    assert {"archetype", "tags", "profile"} <= set(body)
    assert body["archetype"] is not None, "421 hands is enough for an archetype"
    assert body["tags"][0] == body["archetype"]
    spot = client.get("/players/genericpoker/tags", params={"filter": "srp"}).json()
    assert spot["filter"] == "srp" and spot["profile"]["hands"] < body["profile"]["hands"]
    assert client.get("/players/genericpoker/tags", params={"filter": "nope"}).status_code == 400
    assert client.get("/players/ghost/tags").status_code == 404


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
    assert 'data-view="review"' in r.text and 'data-view="beats"' in r.text
    assert "/review" in r.text and "review-sort" in r.text, "the review views order by recency and pot"
    assert 'data-view="session"' in r.text and "session-game" in r.text and "/games" in r.text
    assert "review-split" in r.text, "the list views can put the replay beside the list"


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
    assert "hands-sort" in page.text, "the hand list can be ordered by pot size and by swing"


def test_player_allin_drilldown(client, fast_sampling):
    body = client.get("/players/genericpoker/allin").json()
    assert body["player"] == "genericpoker" and body["hands"]
    row = body["hands"][0]
    assert {"equity", "expected", "actual_bb", "adjusted_bb", "diff_bb", "villains", "board", "method"} <= set(row)
    assert client.get("/players/ghost/allin").status_code == 404
    assert client.get("/players/genericpoker/allin", params={"filter": "nope"}).status_code == 400
    vs = client.get("/players/genericpoker/allin", params={"filter": "vs=Chris"}).json()
    assert vs["hands"] and all(v["player"] == "Chris" for h in vs["hands"] for v in h["villains"])


def test_player_review(client, fast_sampling):
    from pnt.stats.review import KINDS

    body = client.get("/players/genericpoker/review").json()
    assert {"player", "filter", "examined", "showdowns", "known_showdowns", "counts", "skipped", "hands"} <= set(body)
    assert set(body["counts"]) == set(KINDS)
    assert body["hands"]
    for row in body["hands"]:
        assert {"kind", "label", "group", "why", "street", "made", "villains", "pot_bb", "eff_bb", "spr"} <= set(row)
        # every field the drill-down list renders, so the page draws both the same way
        assert {"hand_id", "hand_number", "hole_cards", "board", "pot", "bb", "net_bb", "ip", "vs", "bet_size"} <= set(row)
    assert client.get("/players/ghost/review").status_code == 404
    assert client.get("/players/genericpoker/review", params={"filter": "nope"}).status_code == 400
    vs = client.get("/players/genericpoker/review", params={"filter": "vs=Chris"}).json()
    assert vs["hands"] and all("Chris" in h["vs"] for h in vs["hands"])


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
    for href in ("/stats.html", "/chart", "/players.html", "/allin.html", "/chart?by=review"):
        assert href in text


def test_marking_a_hand_reviewed_round_trips_and_reaches_the_review(client):
    """The mark is addressed by hand_id but stored under the hand's own number, so
    it comes back on every review row for that hand."""
    listed = client.get("/players/genericpoker/review").json()
    flagged = listed["hands"][0]
    assert listed["reviewed"] == 0 and not flagged["reviewed"]

    out = client.post(f"/hands/{flagged['hand_id']}/reviewed", json={"reviewed": True}).json()
    assert out["reviewed"] and out["reviewed_at"]
    assert (out["game_id"], out["hand_number"]) == (flagged["game_id"], flagged["hand_number"])
    assert client.get("/reviewed").json() == [
        {"game_id": out["game_id"], "hand_number": out["hand_number"], "reviewed_at": out["reviewed_at"]}
    ]
    assert client.get("/reviewed", params={"game": "nosuchgame"}).json() == []

    again = client.get("/players/genericpoker/review").json()
    same_hand = lambda h: (h["game_id"], h["hand_number"]) == (out["game_id"], out["hand_number"])
    assert again["reviewed"] == sum(1 for h in again["hands"] if same_hand(h)) >= 1
    for h in again["hands"]:
        assert h["reviewed"] == same_hand(h)

    cleared = client.post(f"/hands/{flagged['hand_id']}/reviewed", json={"reviewed": False}).json()
    assert cleared["reviewed"] is False and cleared["reviewed_at"] is None
    assert client.get("/reviewed").json() == []


def test_marking_an_unknown_hand_is_a_404(client):
    assert client.post("/hands/999999/reviewed", json={"reviewed": True}).status_code == 404


def test_noting_a_hand_round_trips_and_reaches_the_review(client):
    """Same keying as the mark, and independent of it: a note written on a hand
    that is not marked stays put when the mark is set and cleared again."""
    listed = client.get("/players/genericpoker/review").json()
    flagged = listed["hands"][0]
    assert listed["noted"] == 0 and flagged["note"] is None

    out = client.post(f"/hands/{flagged['hand_id']}/note", json={"note": " raise the flop "}).json()
    assert out["note"] == "raise the flop" and out["noted_at"]
    assert (out["game_id"], out["hand_number"]) == (flagged["game_id"], flagged["hand_number"])
    assert client.get("/notes").json() == [
        {"game_id": out["game_id"], "hand_number": out["hand_number"],
         "note": out["note"], "noted_at": out["noted_at"]}
    ]
    assert client.get("/notes", params={"game": "nosuchgame"}).json() == []

    again = client.get("/players/genericpoker/review").json()
    same_hand = lambda h: (h["game_id"], h["hand_number"]) == (out["game_id"], out["hand_number"])
    assert again["noted"] == sum(1 for h in again["hands"] if same_hand(h)) >= 1
    for h in again["hands"]:
        assert (h["note"] == out["note"]) == same_hand(h)

    # The mark comes and goes; the note does not go with it.
    client.post(f"/hands/{flagged['hand_id']}/reviewed", json={"reviewed": True})
    client.post(f"/hands/{flagged['hand_id']}/reviewed", json={"reviewed": False})
    assert client.get("/notes").json()[0]["note"] == out["note"]

    cleared = client.post(f"/hands/{flagged['hand_id']}/note", json={"note": "  "}).json()
    assert cleared["note"] is None and cleared["noted_at"] is None
    assert client.get("/notes").json() == []


def test_noting_an_unknown_hand_is_a_404(client):
    assert client.post("/hands/999999/note", json={"note": "x"}).status_code == 404


def test_games_endpoint_lists_a_players_sessions_newest_first(client):
    games = client.get("/players/genericpoker/games").json()
    assert games, "genericpoker played in the fixture logs"
    last = [g["last_ts"] for g in games]
    assert last == sorted(last, reverse=True)
    every = client.get("/players/genericpoker/hands").json()["hands"]
    # A seat per hand, so a hand counts once however many IDs the alias merges.
    assert sum(g["hands"] for g in games) == len({h["hand_id"] for h in every})
    assert client.get("/players/ghost/games").status_code == 404


def test_session_hands_carry_play_marks_and_notes(client):
    game = client.get("/players/genericpoker/games").json()[0]["game_id"]
    rows = client.get("/players/genericpoker/hands", params={"game": game}).json()["hands"]
    assert rows and all(r["game_id"] == game for r in rows)
    for r in rows:
        assert {"vpip", "saw_flop", "reviewed", "reviewed_at", "note", "noted_at"} <= set(r)
        assert not r["reviewed"] and r["note"] is None
    assert any(r["vpip"] for r in rows) and not all(r["vpip"] for r in rows)

    # Any hand can be marked and written on, flagged or not, and the row says so.
    hand = rows[0]
    client.post(f"/hands/{hand['hand_id']}/reviewed", json={"reviewed": True})
    client.post(f"/hands/{hand['hand_id']}/note", json={"note": "fold the river"})
    again = client.get("/players/genericpoker/hands", params={"game": game}).json()["hands"]
    mine = next(r for r in again if r["hand_id"] == hand["hand_id"])
    assert mine["reviewed"] and mine["reviewed_at"] and mine["note"] == "fold the river"
    assert sum(r["reviewed"] for r in again) == 1



# --- who may call it ------------------------------------------------------------


def test_a_game_id_that_is_not_one_is_refused(client):
    """The ID names a file in the log folder, so `..` or a slash would escape it."""
    assert client.post("/ingest", json={"game_id": r"..\..\escape", "entries": []}).status_code == 422
    assert client.post("/ingest", json={"game_id": "../escape", "entries": []}).status_code == 422
    assert client.post("/rebuild/..%5Cescape").status_code == 422
    assert client.get("/hud/a.b").status_code == 422
    assert client.get("/live/a.b").status_code == 422

    from pnt.ingest.log_folder import log_path

    with pytest.raises(ValueError):
        log_path(Path("logs"), "../escape")


def test_writes_need_the_header(client):
    """A page in any other tab can send a POST to 127.0.0.1; CORS only hides the
    answer. A body-less POST needs no content type, and a no-cors fetch of a Blob
    sends none, so the header -- which no page can add cross-site -- is what
    stops both, whatever FastAPI's content-type handling does."""
    body = json.dumps({"game_id": "csrf-probe", "entries": []})
    bare = local_client(client.app)
    bare.headers.pop("x-pnt")
    assert bare.post("/ingest", content=body).status_code == 403
    assert bare.post("/rebuild/csrf-probe").status_code == 403
    assert bare.get("/health").status_code == 200  # reads need nothing
    assert client.post("/ingest", json=json.loads(body)).status_code == 200


@pytest.mark.parametrize(
    "origin, status",
    [
        ("https://evil.example", 403),
        ("https://www.pokernow.com", 403),
        ("null", 403),
        ("chrome-extension://abcdefghijklmnop", 200),
        ("http://127.0.0.1:52000", 200),  # the server's own pages
    ],
)
def test_writes_from_a_foreign_origin_are_refused(client, origin, status):
    r = client.post("/ingest", json={"game_id": "origin-probe", "entries": []}, headers={"origin": origin})
    assert r.status_code == status


def test_only_this_machine_may_be_named_as_host(client):
    """A DNS-rebinding page reaches 127.0.0.1 under its own name, and is refused."""
    from fastapi.testclient import TestClient

    assert TestClient(client.app, base_url="http://evil.example").get("/health").status_code == 400
    assert TestClient(client.app, base_url="http://localhost").get("/health").status_code == 200



@pytest.mark.parametrize("path", ["/", "/players.html", "/chart", "/stats.html", "/allin.html", "/pots.html"])
def test_pages_may_be_framed_only_by_the_extension_and_pokernow(client, path):
    """Framed by any other site, /players could be clickjacked into a merge."""
    csp = client.get(path).headers["content-security-policy"]
    assert csp.startswith("frame-ancestors 'self' chrome-extension: ")
    assert "https://www.pokernow.com" in csp and "*" not in csp
