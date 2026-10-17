"""Choosing the HUD's stats on the front page.

The catalog names `rates()` keys by hand, so the first test pins it to what
`rates()` actually returns: a renamed stat fails here rather than as a blank
column on the table.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from pnt.stats.hud_catalog import BY_KEY, CATALOG, DEFAULT, GROUPS, MAX, chosen
from pnt.stats.queries import aggregate, facts_by_player
from tests.conftest import ALL_LOGS, HU_GAME, ROOT, local_client

fastapi = pytest.importorskip("fastapi")


def test_every_catalog_key_and_opp_key_is_in_rates(db):
    grouped, _, _ = facts_by_player(db)
    row = aggregate([f for facts in grouped.values() for f in facts])
    for c in CATALOG:
        assert c["key"] in row, c["key"]
        assert c["opp"] is None or c["opp"] in row["_opp"], c
    # Both of these are rates with no count of their own.
    assert {c["key"] for c in CATALOG if c["opp"] is None} == {"wsd", "bb_per_100"}


def test_catalog_shape():
    keys = [c["key"] for c in CATALOG]
    assert len(keys) == len(set(keys))
    assert "hands" not in keys
    assert all(len(c["label"]) <= 5 and c["tip"] and c["group"] in GROUPS for c in CATALOG)
    # Two columns with one label would be told apart only by hovering.
    labels = [c["label"] for c in CATALOG]
    assert len(labels) == len(set(labels))
    assert [c["group"] for c in CATALOG] == sorted((c["group"] for c in CATALOG), key=GROUPS.index)
    assert set(DEFAULT) <= set(BY_KEY) and len(DEFAULT) <= MAX


def test_chosen_drops_keys_the_catalog_lost():
    assert chosen(None) == DEFAULT
    assert chosen(["wtsd", "gone", "vpip"]) == ["wtsd", "vpip"]
    assert chosen(["gone"]) == DEFAULT  # nothing left of it
    assert chosen([]) == []  # hands alone is a choice


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


def test_get_defaults_then_put_round_trips(client):
    got = client.get("/settings/hud").json()
    assert got["columns"] == DEFAULT and got["default"] == DEFAULT and got["max"] == MAX
    assert [c["key"] for c in got["catalog"]] == [c["key"] for c in CATALOG]

    pick = ["bb_per_100", "vpip", "cbet_turn"]
    put = client.put("/settings/hud", json={"columns": pick})
    assert put.status_code == 200 and put.json()["columns"] == pick
    assert client.get("/settings/hud").json()["columns"] == pick


def test_put_needs_the_write_header(client):
    bare = local_client(client.app)
    bare.headers.pop("x-pnt")
    assert bare.put("/settings/hud", json={"columns": ["vpip"]}).status_code == 403
    assert client.get("/settings/hud").json()["columns"] == DEFAULT


@pytest.mark.parametrize(
    "columns",
    [
        ["vpip", "nope"],
        ["vpip", "vpip"],
        [c["key"] for c in CATALOG[: MAX + 1]],
        ["hands"],
    ],
    ids=["unknown", "duplicate", "too-many", "hands"],
)
def test_put_refuses_a_bad_list(client, columns):
    r = client.put("/settings/hud", json={"columns": columns})
    assert r.status_code == 400
    assert client.get("/settings/hud").json()["columns"] == DEFAULT


def test_hud_sends_the_chosen_columns(client):
    cols = client.get(f"/hud/{HU_GAME}").json()["columns"]
    assert [c["key"] for c in cols] == DEFAULT
    assert cols[1] == {k: BY_KEY["pfr"][k] for k in ("key", "label", "tip", "opp")}

    client.put("/settings/hud", json={"columns": ["raise_cbet_flop", "wsd"]})
    hud = client.get(f"/hud/{HU_GAME}").json()
    assert [(c["key"], c["opp"]) for c in hud["columns"]] == [("raise_cbet_flop", "fold_to_cbet_flop"), ("wsd", None)]
    # Every column the HUD is told to print is in each seat's figures.
    for seat in hud["seats"]:
        for c in hud["columns"]:
            assert c["key"] in seat["stats"]
            assert c["opp"] is None or c["opp"] in seat["stats"]["_opp"]


# ------------------------------------------------------------------ scripts --

NODE = shutil.which("node")


def _check(path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    out = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_sidepanel_parses(tmp_path):
    _check(tmp_path / "sidepanel.js", (ROOT / "pnt/extension/sidepanel.js").read_text(encoding="utf-8"))


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_front_page_script_parses(tmp_path):
    html = (ROOT / "pnt/server/static/index.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
    assert scripts
    _check(tmp_path / "index.js", "\n".join(scripts))
