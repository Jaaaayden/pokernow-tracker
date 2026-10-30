"""The front page's Data switch: the pages show the bundled sample, or your own data.

PUT /settings/dataset builds the sample into a database of its own the first time
and points the pages' reads at it; live capture, the log-folder sync and the HUD
stay on your own database throughout, and a restart is always back on yours.
"""

from __future__ import annotations

import importlib
import shutil
import time

import pytest
from conftest import HU, HU_GAME, THREE, local_client

from pnt import local_config
from pnt.db.conn import connect
from pnt.ingest import sample as samplemod
from pnt.ingest.csv_source import read_csv
from pnt.ingest.importer import import_csv

pytest.importorskip("fastapi")


@pytest.fixture()
def server(tmp_path, monkeypatch):
    """Your database holds the heads-up log; the "sample" is the three-handed one."""
    monkeypatch.setenv("PNT_DB", str(tmp_path / "own.sqlite"))
    monkeypatch.setenv("PNT_SYNC_SECONDS", "0")
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    shutil.copy(THREE, bundled / THREE.name)
    (bundled / "aliases.csv").write_text("pn_id,alias\n", encoding="utf-8")
    monkeypatch.setattr(samplemod, "LOG_DIR", bundled)
    monkeypatch.setattr(samplemod, "ALIASES", bundled / "aliases.csv")
    monkeypatch.setattr(samplemod, "HOME", tmp_path / "pnt-home")
    from pnt.server import app as app_module

    importlib.reload(app_module)
    conn = connect(tmp_path / "own.sqlite")
    import_csv(conn, HU)
    conn.close()
    local_config.put("dataset", None)  # one config.json for the whole run
    yield app_module
    local_config.put("dataset", None)


def _hands(path) -> int:
    conn = connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM hands").fetchone()[0]
    finally:
        conn.close()


def _wait_for_sample(client) -> dict:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        got = client.get("/settings/dataset").json()
        if got["active"] == "sample" or got["error"]:
            return got
        time.sleep(0.05)
    raise AssertionError("the sample never finished building")


def test_the_pages_show_the_sample_while_capture_and_the_hud_stay_yours(server, tmp_path):
    client = local_client(server.app)
    own = client.get("/health").json()
    assert client.get("/settings/dataset").json()["active"] == "own"

    first = client.put("/settings/dataset", json={"which": "sample"}).json()
    assert first["building"] == {"done": 0, "total": 1}
    assert _wait_for_sample(client)["error"] is None

    health = client.get("/health").json()
    assert health["dataset"] == "sample" and health["hands"] == _hands(server.SAMPLE_PATH) != own["hands"]

    # The HUD is a live table's, so it reads your database (the heads-up game is not in
    # the sample), but it is being shown to someone: the names are masked.
    hud = client.get(f"/hud/{HU_GAME}")
    assert hud.status_code == 200 and hud.json()["dataset"] == "sample"
    seats = hud.json()["seats"]
    conn = connect(tmp_path / "own.sqlite")
    real = {r[0] for r in conn.execute("SELECT alias FROM players")}
    conn.close()
    assert seats and all(s["stats"]["hands"] > 0 and s["session"]["hands"] > 0 for s in seats)
    assert sorted(s["alias"] for s in seats) == ["player 1", "player 2"]
    assert client.get(f"/hud/{HU_GAME}").json() == hud.json(), "a seat keeps its mask"
    live = client.get(f"/live/{HU_GAME}")
    assert live.status_code == 200
    assert not any(name in live.text or name in hud.text for name in real)

    # A live game lands in your database, not the sample's.
    entries = [{"entry": r.entry, "at": r.at, "order": r.ord} for r in read_csv(HU)]
    assert client.post("/ingest", json={"game_id": "played-meanwhile", "entries": entries}).status_code == 200
    conn = connect(tmp_path / "own.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM hands WHERE game_id = 'played-meanwhile'").fetchone()[0] > 0
    conn.close()
    conn = connect(server.SAMPLE_PATH)
    assert conn.execute("SELECT COUNT(*) FROM raw_entries WHERE game_id = 'played-meanwhile'").fetchone()[0] == 0
    conn.close()

    assert client.put("/settings/dataset", json={"which": "own"}).json()["active"] == "own"
    assert client.get("/health").json()["dataset"] == "own"


def test_the_huds_writes_go_to_your_database_while_the_pages_show_the_sample(server, tmp_path):
    """The 🚩 and a card's ✎ say `capture=1`: the table and its players are yours."""
    client = local_client(server.app)
    own_hands = client.get("/health").json()["hands"]
    client.put("/settings/dataset", json={"which": "sample"})
    _wait_for_sample(client)
    assert client.get("/health").json()["capture_hands"] == own_hands, "the HUD's count, not the sample's"

    def flags():
        conn = connect(tmp_path / "own.sqlite")
        try:
            return conn.execute("SELECT COUNT(*) FROM hand_flags WHERE game_id = ?", (HU_GAME,)).fetchone()[0]
        finally:
            conn.close()

    flag = f"/games/{HU_GAME}/hands/3/flag?capture=1"
    assert client.post(flag, json={"flagged": True}).json()["flagged"] is True
    assert flags() == 1
    # Clearing used to land on the sample, report success and leave yours flagged.
    assert client.post(flag, json={"flagged": False}).json()["flagged"] is False
    assert flags() == 0

    conn = connect(tmp_path / "own.sqlite")
    seat = conn.execute("SELECT alias FROM players ORDER BY alias LIMIT 1").fetchone()[0]
    conn.close()
    note = client.post(f"/players/{seat}/note?capture=1", json={"note": "jams light"})
    assert note.status_code == 200
    # Kept on your player, and off the HUD while it masks the table.
    assert all(s["note"] is None for s in client.get(f"/hud/{HU_GAME}").json()["seats"])
    client.put("/settings/dataset", json={"which": "own"})
    assert "jams light" in [s["note"] for s in client.get(f"/hud/{HU_GAME}").json()["seats"]]
    client.put("/settings/dataset", json={"which": "sample"})
    # Without it, the same write is about what the pages show: the sample has no such player.
    assert client.post(f"/players/{seat}/note", json={"note": "x"}).status_code == 404


def test_a_built_sample_is_reused_and_an_updated_one_rebuilt(server, monkeypatch):
    client = local_client(server.app)
    client.put("/settings/dataset", json={"which": "sample"})
    _wait_for_sample(client)
    client.put("/settings/dataset", json={"which": "own"})

    again = client.put("/settings/dataset", json={"which": "sample"}).json()
    assert again["active"] == "sample" and again["building"] is None, "already built: no wait"
    client.put("/settings/dataset", json={"which": "own"})

    monkeypatch.setattr(samplemod, "version", lambda: "a newer sample")
    assert client.put("/settings/dataset", json={"which": "sample"}).json()["building"] is not None
    assert _wait_for_sample(client)["active"] == "sample"


def test_a_database_picked_by_hand_leaves_the_sample_for_good(server, tmp_path):
    client = local_client(server.app)
    client.put("/settings/dataset", json={"which": "sample"})
    _wait_for_sample(client)
    assert client.put("/settings/paths", json={"db": str(tmp_path / "own.sqlite")}).status_code == 200
    assert client.get("/settings/dataset").json()["active"] == "own"

    importlib.reload(server)  # and the next start is on it too
    with local_client(server.app) as client:
        assert client.get("/settings/dataset").json()["active"] == "own"


def test_the_sample_stays_switched_on_across_a_restart(server):
    client = local_client(server.app)
    client.put("/settings/dataset", json={"which": "sample"})
    _wait_for_sample(client)

    importlib.reload(server)  # a restart: the switch is back to its default in memory
    assert server.SAMPLE_ON is False
    with local_client(server.app) as client:
        assert client.get("/settings/dataset").json()["active"] == "sample"
        client.put("/settings/dataset", json={"which": "own"})

    importlib.reload(server)
    with local_client(server.app) as client:
        assert client.get("/settings/dataset").json()["active"] == "own"


def test_the_samples_folder_is_never_the_log_folder(server):
    """Synced, its anonymized copies of your games would sit in your database beside them."""
    got = local_client(server.app).put("/settings/paths", json={"log_dir": str(samplemod.LOG_DIR)})
    assert got.status_code == 400 and "Data" in got.json()["detail"]
    assert local_config.get("log_dir") != str(samplemod.LOG_DIR.resolve())


def test_a_log_folder_already_set_to_the_samples_is_not_synced(server, tmp_path, monkeypatch):
    """A config.json from before the check above: the sync loop skips the folder."""
    import threading

    from pnt.ingest import log_folder

    monkeypatch.setattr(log_folder, "LOG_DIR", samplemod.LOG_DIR)
    before = _hands(tmp_path / "own.sqlite")
    stop = threading.Event()
    stop.set()  # one pass, then out
    server._sync_loop(stop)
    assert _hands(tmp_path / "own.sqlite") == before
