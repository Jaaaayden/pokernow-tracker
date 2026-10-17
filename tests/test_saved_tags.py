"""Saved spots and hand tags through the server, and the `tag=` filter term."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from pnt.stats.filters import parse_filter, vocabulary
from tests.conftest import ALL_LOGS, HU_GAME, MULTIWAY_GAME, ROOT, local_client

fastapi = pytest.importorskip("fastapi")

STATIC = ROOT / "pnt" / "server" / "static"


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


def _bare(client):
    bare = local_client(client.app)
    bare.headers.pop("x-pnt")
    return bare


def _total(rows) -> int:
    return sum(r["hands"] for r in rows)


# ------------------------------------------------------------ saved spots ---


def test_spots_crud(client):
    assert client.get("/spots").json() == []
    a = client.post("/spots", json={"name": " BTN opens ", "filter": "opener,position=BTN"})
    assert a.status_code == 200, a.text
    a = a.json()
    assert a["name"] == "BTN opens" and a["pinned"] is False
    b = client.post("/spots", json={"name": "river", "filter": "faced_bet_river,3bet_pot|4bet_pot"}).json()
    assert [s["name"] for s in client.get("/spots").json()] == ["BTN opens", "river"]

    # Pinned first; then rename, re-filter and move.
    assert client.patch(f"/spots/{b['spot_id']}", json={"pinned": True}).json()["pinned"] is True
    assert [s["name"] for s in client.get("/spots").json()] == ["river", "BTN opens"]
    out = client.patch(f"/spots/{a['spot_id']}", json={"name": "BTN", "filter": "position=BTN"}).json()
    assert (out["name"], out["filter"]) == ("BTN", "position=BTN")
    c = client.post("/spots", json={"name": "srp", "filter": "srp"}).json()
    client.patch(f"/spots/{c['spot_id']}", json={"ord": 0})
    assert [s["name"] for s in client.get("/spots").json()] == ["river", "srp", "BTN"]

    assert client.delete(f"/spots/{a['spot_id']}").json()["deleted"] is True
    assert client.delete(f"/spots/{a['spot_id']}").status_code == 404
    assert client.patch(f"/spots/{a['spot_id']}", json={"pinned": True}).status_code == 404
    assert [s["name"] for s in client.get("/spots").json()] == ["river", "srp"]


def test_spots_refuse_a_bad_filter_a_taken_name_and_a_write_without_the_header(client):
    r = client.post("/spots", json={"name": "x", "filter": "position=XYZ"})
    assert r.status_code == 400 and "unknown position" in r.json()["detail"]
    assert client.post("/spots", json={"name": "x", "filter": "srp|"}).status_code == 400
    assert client.post("/spots", json={"name": " ", "filter": "srp"}).status_code == 400
    s = client.post("/spots", json={"name": "x", "filter": "srp"}).json()
    assert client.post("/spots", json={"name": "x", "filter": "limped"}).status_code == 400
    assert client.patch(f"/spots/{s['spot_id']}", json={"filter": "nope"}).status_code == 400
    assert client.get("/spots").json()[0]["filter"] == "srp"

    bare = _bare(client)
    assert bare.post("/spots", json={"name": "y", "filter": "srp"}).status_code == 403
    assert bare.patch(f"/spots/{s['spot_id']}", json={"pinned": True}).status_code == 403
    assert bare.delete(f"/spots/{s['spot_id']}").status_code == 403
    assert [x["name"] for x in client.get("/spots").json()] == ["x"]


# -------------------------------------------------------------- hand tags ---


def test_tag_crud_and_normalisation(client):
    url = f"/games/{HU_GAME}/hands/3/tags"
    assert client.get(url).json()["tags"] == []
    out = client.post(url, json={"tag": " River Bluff "})
    assert out.status_code == 200 and out.json()["tags"] == ["river-bluff"]
    assert client.post(url, json={"tag": "river-bluff"}).json()["tags"] == ["river-bluff"]  # idempotent
    assert client.post(url, json={"tag": "Cooler"}).json()["tags"] == ["cooler", "river-bluff"]
    client.post(f"/games/{HU_GAME}/hands/4/tags", json={"tag": "cooler"})
    assert client.get("/tags").json()[0] | {"last": None} == {"tag": "cooler", "hands": 2, "last": None}

    assert client.delete(f"{url}/RIVER-BLUFF").json()["tags"] == ["cooler"]
    assert client.delete(f"{url}/river-bluff").json()["tags"] == ["cooler"]  # gone already

    for bad in ("", "a,b", "x" * 41, "flagged"):
        assert client.post(url, json={"tag": bad}).status_code == 400, bad
    assert client.post(f"/games/{HU_GAME}/hands/99999/tags", json={"tag": "x"}).status_code == 404
    bare = _bare(client)
    assert bare.post(url, json={"tag": "x"}).status_code == 403
    assert bare.delete(f"{url}/cooler").status_code == 403
    assert client.get(url).json()["tags"] == ["cooler"]


def test_tag_filters_stats_by_hand(client):
    for n in (3, 4, 5):
        client.post(f"/games/{HU_GAME}/hands/{n}/tags", json={"tag": "study"})
    client.post(f"/games/{MULTIWAY_GAME}/hands/2/tags", json={"tag": "cooler"})

    study = client.get("/stats", params={"filter": "tag=study"}).json()
    # Heads-up: both seats of three hands.
    assert _total(study) == 6 and len(study) == 2
    either = client.get("/stats", params={"filter": "tag=study|cooler"}).json()
    cooler = client.get("/stats", params={"filter": "tag=cooler"}).json()
    assert _total(either) == _total(study) + _total(cooler) > _total(study)
    # A tag with no hands is an empty spot, not an error; a tag that cannot be one is.
    assert client.get("/stats", params={"filter": "tag=nothing-here"}).json() == []
    assert client.get("/stats", params={"filter": "tag=a.b"}).status_code == 400
    everyone = _total(client.get("/stats").json())
    assert _total(client.get("/stats", params={"filter": "tag!=study"}).json()) == everyone - 6

    # The 🚩 is `tag=flagged`.
    client.post(f"/games/{HU_GAME}/hands/7/flag", json={"flagged": True})
    assert _total(client.get("/stats", params={"filter": "tag=flagged"}).json()) == 2


def test_tagged_lists_the_rows_with_flagged_first(client):
    client.post(f"/games/{HU_GAME}/hands/3/tags", json={"tag": "study"})
    client.post(f"/games/{HU_GAME}/hands/5/tags", json={"tag": "study"})
    client.post(f"/games/{HU_GAME}/hands/5/tags", json={"tag": "cooler"})
    client.post(f"/games/{HU_GAME}/hands/7/flag", json={"flagged": True})
    player = client.get("/stats", params={"game": HU_GAME}).json()[0]["player"]

    out = client.get("/tagged", params={"tag": "study", "player": player}).json()
    assert [r["hand_number"] for r in out["hands"]] == [5, 3]  # newest first
    assert out["hands"][0]["tags"] == ["cooler", "study"] and "flagged" in out["hands"][0]
    assert out["tags"][0] == {"tag": "flagged", "hands": 1, "builtin": True}
    assert out["tags"][1:] == [{"tag": "study", "hands": 2}, {"tag": "cooler", "hands": 1}]
    assert [r["hand_number"] for r in client.get("/tagged", params={"tag": "flagged", "player": player})
            .json()["hands"]] == [7]
    # No tag: every hand you tagged. No player: one row per hand, with whose seat it is.
    assert len(client.get("/tagged", params={"player": player}).json()["hands"]) == 2
    rows = client.get("/tagged", params={"tag": "study"}).json()["hands"]
    assert [r["hand_number"] for r in rows] == [5, 3] and all(r["player"] for r in rows)
    # A spot narrows the player's rows, and the session and review rows carry tags too.
    some = client.get("/tagged", params={"tag": "study", "player": player, "filter": "vpip"}).json()
    assert len(some["hands"]) <= 2
    hands = client.get(f"/players/{player}/hands", params={"game": HU_GAME}).json()["hands"]
    assert next(h for h in hands if h["hand_number"] == 5)["tags"] == ["cooler", "study"]
    assert all("tags" in r for r in client.get(f"/players/{player}/review").json()["hands"])


def test_a_tag_change_reaches_a_cached_spot(client):
    """The spot pool behind a player's archetype tags is cached by filter: a new
    hand tag must not be served the pool from before it."""
    player = client.get("/stats", params={"game": HU_GAME}).json()[0]["player"]
    before = client.get(f"/players/{player}/tags", params={"filter": "tag=study"})
    assert before.status_code == 200
    client.post(f"/games/{HU_GAME}/hands/3/tags", json={"tag": "study"})
    from pnt.server import app as app_module

    conn = app_module.db()
    assert app_module._spot_key(conn, "tag=study") != "tag=study"
    assert app_module._spot_key(conn, "srp") == "srp"


# ---------------------------------------------------------- the filter term ---


def test_tag_term_needs_a_resolver_and_matches_by_hand():
    with pytest.raises(ValueError, match="hand tags"):
        parse_filter("tag=study")
    with pytest.raises(ValueError, match="needs a tag"):
        parse_filter("tag=", tags=lambda t: ())
    keys = {"study": {("g", 1)}, "cooler": {("g", 2)}}
    pred = parse_filter("tag=study|cooler", tags=lambda t: keys.get(t, set()))

    class F:
        def __init__(self, n):
            self.game_id, self.hand_number = "g", n

    assert [pred(F(n)) for n in (1, 2, 3)] == [True, True, False]
    # A flag after a tag list is the flag, as after any list but vs=.
    seen = []
    parse_filter("tag=study|won", tags=lambda t: seen.append(t) or ())
    assert seen == ["study"]


def test_vocabulary_has_tag():
    terms = [t for g in vocabulary()["groups"] for t in g["terms"]]
    assert any(t["term"] == "tag=TAG" and t["example"].startswith("tag=") for t in terms)


# ------------------------------------------------------------------- pages ---


def _script_of(html: str) -> str:
    blocks = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    assert blocks
    return "\n".join(blocks)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("name", ["filter-help.js", "replay.js", "chart.html"])
def test_the_pages_scripts_parse(tmp_path, name):
    text = (STATIC / name).read_text("utf-8")
    path = Path(tmp_path / (name + ".js"))
    path.write_text(_script_of(text) if name.endswith(".html") else text, "utf-8")
    out = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_the_chart_has_a_tagged_view_that_bluff_value_hides():
    html = (STATIC / "chart.html").read_text("utf-8")
    assert 'data-view="tagged"' in html and '"/tagged?"' in html
    body = html[html.index("function renderBluffs("):]
    hidden = set(re.findall(r'"(\w+)"', body[body.index("for (const id of [") : body.index("]) {")]))
    assert "tagged" in hidden
