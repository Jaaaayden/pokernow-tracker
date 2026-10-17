"""The front page's Files panel: switching the database and log folder at runtime.

PUT /settings/paths switches what the running server opens and records it in the
machine config (pnt/local_config.py), which the next start reads. The native
picker itself is not exercised -- it opens a window.
"""

from __future__ import annotations

import importlib
import json

import pytest
from conftest import ALL_LOGS, HU, local_client

from pnt import local_config
from pnt.db.conn import connect
from pnt.ingest import log_folder
from pnt.ingest.importer import import_csv


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("PNT_DB", str(tmp_path / "api.sqlite"))
    from pnt.server import app as app_module

    importlib.reload(app_module)
    conn = connect(tmp_path / "api.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    conn.close()
    return app_module


@pytest.fixture()
def other_db(tmp_path):
    path = tmp_path / "other" / "second.sqlite"
    path.parent.mkdir()
    conn = connect(path)
    import_csv(conn, HU)
    conn.close()
    return path


def test_switching_the_database_takes_effect_and_is_recorded(server, other_db):
    client = local_client(server.app)
    before = client.get("/health").json()["hands"]

    got = client.put("/settings/paths", json={"db": str(other_db)})
    assert got.status_code == 200 and got.json()["db"] == str(other_db.resolve())

    after = client.get("/health").json()
    assert after["db"] == str(other_db.resolve())
    assert 0 < after["hands"] < before  # one log against three
    assert local_config.get("db") == str(other_db.resolve())
    assert local_config.database(server.DB_PATH.parent / "nope") == other_db.resolve()


@pytest.mark.parametrize(
    "make, why",
    [
        (lambda d: d / "missing.sqlite", "no file"),
        (lambda d: (d / "notes.txt").write_text("hi") and d / "notes.txt", "not a SQLite"),
        (lambda d: "relative.sqlite", "full path"),
    ],
)
def test_a_path_that_is_not_a_tracker_database_is_refused(server, tmp_path, make, why):
    client = local_client(server.app)
    before = server.DB_PATH
    got = client.put("/settings/paths", json={"db": str(make(tmp_path))})
    assert got.status_code == 400 and why in got.json()["detail"]
    assert server.DB_PATH == before and local_config.get("db") is None


def test_a_sqlite_file_of_something_else_is_refused_and_left_untouched(server, tmp_path):
    import sqlite3

    path = tmp_path / "unrelated.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE things (x)")
    conn.commit()
    conn.close()
    got = local_client(server.app).put("/settings/paths", json={"db": str(path)})
    assert got.status_code == 400 and "not a tracker database" in got.json()["detail"]
    conn = sqlite3.connect(path)
    assert [r[0] for r in conn.execute("SELECT name FROM sqlite_master")] == ["things"]
    conn.close()


def test_switching_the_log_folder(server, tmp_path):
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    client = local_client(server.app)
    got = client.put("/settings/paths", json={"log_dir": str(folder)})
    assert got.status_code == 200 and got.json()["log_dir"] == str(folder.resolve())
    assert log_folder.LOG_DIR == folder.resolve()
    assert json.loads(local_config.PATH.read_text())["log_dir"] == str(folder.resolve())

    missing = client.put("/settings/paths", json={"log_dir": str(tmp_path / "nope")})
    assert missing.status_code == 400 and log_folder.LOG_DIR == folder.resolve()


def test_the_log_folder_stays_put_while_the_environment_sets_it(server, tmp_path, monkeypatch):
    monkeypatch.setattr(log_folder, "FROM_ENV", True)
    got = local_client(server.app).put("/settings/paths", json={"log_dir": str(tmp_path)})
    assert got.status_code == 409 and "PNT_LOG_DIR" in got.json()["detail"]
    assert local_config.get("log_dir") is None


def test_one_bad_path_moves_neither(server, tmp_path, other_db):
    before = server.DB_PATH
    got = local_client(server.app).put(
        "/settings/paths", json={"db": str(other_db), "log_dir": str(tmp_path / "nope")}
    )
    assert got.status_code == 400 and server.DB_PATH == before


def test_browse_needs_the_write_header(server):
    from fastapi.testclient import TestClient

    bare = TestClient(server.app, base_url="http://127.0.0.1")
    assert bare.post("/settings/paths/browse", json={"kind": "db"}).status_code == 403
    assert local_client(server.app).post("/settings/paths/browse", json={"kind": "x"}).status_code == 422


def test_config_round_trip_and_fallback(tmp_path):
    assert local_config.load() == {}
    local_config.put("db", str(tmp_path / "gone.sqlite"))
    local_config.put("log_dir", str(tmp_path))
    assert local_config.load() == {"db": str(tmp_path / "gone.sqlite"), "log_dir": str(tmp_path)}
    # A chosen database that has since gone falls back rather than failing to start.
    assert local_config.database(tmp_path / "fallback.sqlite") == tmp_path / "fallback.sqlite"
    local_config.put("db", None)
    assert local_config.load() == {"log_dir": str(tmp_path)}

    local_config.PATH.write_text("not json")
    assert local_config.load() == {} and local_config.get("db") is None


def test_a_database_from_another_pc_keeps_games_whose_files_are_not_here(server, other_db, tmp_path):
    """Its log_files name a folder this machine also has, without that PC's files:
    switching to it must not let the next sync read those games as deleted."""
    from pnt.ingest import sync

    folder = tmp_path / "logs"
    folder.mkdir()
    conn = connect(other_db)
    (gid,) = conn.execute("SELECT game_id FROM games").fetchone()
    conn.execute(
        "INSERT INTO log_files (path, game_id, mtime_ns, size) VALUES (?, ?, 0, 0)",
        (str(folder / f"poker_now_log_{gid}.csv"), gid),
    )
    conn.commit()
    conn.close()

    assert local_client(server.app).put("/settings/paths", json={"db": str(other_db)}).status_code == 200
    conn = connect(other_db)
    sync.sync_folder(conn, folder)
    assert conn.execute("SELECT COUNT(*) FROM hands WHERE game_id = ?", (gid,)).fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM log_files").fetchone()[0] == 0
    conn.close()


def test_a_failed_save_switches_nothing(server, other_db, tmp_path, monkeypatch):
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    before_db, before_logs = server.DB_PATH, log_folder.LOG_DIR

    def locked(key, value):
        raise PermissionError("config.json is in use")

    monkeypatch.setattr(local_config, "put", locked)
    client = local_client(server.app, raise_server_exceptions=False)
    got = client.put("/settings/paths", json={"db": str(other_db), "log_dir": str(folder)})
    assert got.status_code == 500
    assert server.DB_PATH == before_db and log_folder.LOG_DIR == before_logs


def test_a_warm_up_stops_once_the_database_is_switched(server, monkeypatch):
    """Even switched away and back (A -> B -> A), when the path matches again."""
    from pnt.stats import strength

    counted = []

    def warm(conn, between):
        server._switches += 2  # A -> B -> A on the front page, mid-warm-up
        between()
        return 0

    monkeypatch.setattr(strength, "warm", warm)
    monkeypatch.setattr(server, "pool_for", lambda *a, **k: counted.append(1))
    server._warm_strength()
    assert counted == []


@pytest.mark.parametrize(
    "path, uri",
    [
        (r"C:\Users\me\poker db.sqlite", "file:/C:/Users/me/poker%20db.sqlite?mode=ro"),
        (r"\\nas\poker\pokernow.sqlite", "file:////nas/poker/pokernow.sqlite?mode=ro"),
    ],
)
def test_the_read_only_probe_reaches_drives_and_shares(server, path, uri):
    from pathlib import PureWindowsPath

    assert server._read_only_uri(PureWindowsPath(path)) == uri


def test_saves_from_two_threads_keep_both_keys():
    import threading

    keys = [f"k{i}" for i in range(16)]
    threads = [threading.Thread(target=local_config.put, args=(k, "v")) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(keys) <= set(local_config.load())


def test_the_folder_the_environment_already_names_is_not_refused(server, tmp_path, monkeypatch):
    monkeypatch.setattr(log_folder, "FROM_ENV", True)
    monkeypatch.setattr(log_folder, "LOG_DIR", tmp_path / "logs" / ".." / "logs")
    (tmp_path / "logs").mkdir()
    got = local_client(server.app).put("/settings/paths", json={"log_dir": str(tmp_path / "logs")})
    assert got.status_code == 200


def test_a_failed_service_install_leaves_the_chosen_database(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from pnt import service as svc
    from pnt.cli import app

    def refuse(*args, **kwargs):
        raise svc.ServiceError("schtasks said no")

    monkeypatch.setattr(svc, "port_in_use", lambda *a: False)
    monkeypatch.setattr(svc, "install", refuse)
    local_config.put("db", str(tmp_path / "chosen.sqlite"))
    result = CliRunner().invoke(app, ["service", "install", "--db", str(tmp_path / "other.sqlite")])
    assert result.exit_code == 1 and "schtasks said no" in result.output
    assert local_config.get("db") == str(tmp_path / "chosen.sqlite")
