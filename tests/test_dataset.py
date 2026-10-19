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
    return app_module


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

    # The HUD is a live table's, so it reads your database: the heads-up game is not in the sample.
    hud = client.get(f"/hud/{HU_GAME}")
    assert hud.status_code == 200 and hud.json()["dataset"] == "sample"
    assert client.get(f"/live/{HU_GAME}").status_code == 200

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


def test_a_restart_and_a_database_picked_by_hand_leave_the_sample(server, tmp_path):
    client = local_client(server.app)
    client.put("/settings/dataset", json={"which": "sample"})
    _wait_for_sample(client)
    assert client.put("/settings/paths", json={"db": str(tmp_path / "own.sqlite")}).status_code == 200
    assert client.get("/settings/dataset").json()["active"] == "own"

    client.put("/settings/dataset", json={"which": "sample"})
    assert client.get("/settings/dataset").json()["active"] == "sample"
    importlib.reload(server)
    assert server.SAMPLE_ON is False
