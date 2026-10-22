"""Notes left on the pages with the overlay (pnt/server/annotations.py, extension/annotate.js)."""

from __future__ import annotations

import json

import pytest

from pnt import local_config
from pnt.update import EXTENSION_DIR
from tests.conftest import local_client

fastapi = pytest.importorskip("fastapi")

NOTE = {
    "note": "the 3bet column is cut off",
    "page": "/chart",
    "query": "player=henry",
    "selector": "#lines > table > tr:nth-of-type(2)",
    "tag": "tr",
    "text": "3bet 12%",
    "context": "Lines · #lines",
    "rect": {"x": 10, "y": 20, "w": 300, "h": 18},
    "viewport": {"w": 402, "h": 900},
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # No hands needed: the notes live beside the config, not in the database.
    monkeypatch.setenv("PNT_DB", str(tmp_path / "api.sqlite"))
    import importlib

    from pnt.server import app as app_module

    importlib.reload(app_module)
    return local_client(app_module.app)


def test_a_note_is_kept_beside_the_config_and_listed(client):
    made = client.post("/annotations", json=NOTE).json()
    assert made["status"] == "open" and made["reply"] is None and made["selector"] == NOTE["selector"]
    stored = json.loads((local_config.PATH.parent / "annotations.json").read_text(encoding="utf-8"))
    assert [a["id"] for a in stored] == [made["id"]]
    assert [a["id"] for a in client.get("/annotations", params={"status": "open"}).json()] == [made["id"]]
    assert client.get("/annotations", params={"page": "sidepanel"}).json() == []


def test_a_reply_resolves_a_note_and_dismissing_it_closes_it(client):
    made = client.post("/annotations", json=NOTE).json()
    done = client.post(f"/annotations/{made['id']}", json={"status": "resolved", "reply": "widened, 1a2b3c"}).json()
    assert done["status"] == "resolved" and done["reply"] == "widened, 1a2b3c" and done["resolved_at"]
    assert client.post(f"/annotations/{made['id']}", json={"status": "closed"}).json()["note"] == NOTE["note"]
    assert client.get("/annotations", params={"status": "open"}).json() == []


def test_unknown_notes_and_bad_fields_are_refused(client):
    assert client.post("/annotations/nope", json={"status": "closed"}).status_code == 404
    assert client.delete("/annotations/nope").status_code == 404
    assert client.post("/annotations", json={**NOTE, "note": ""}).status_code == 422
    made = client.post("/annotations", json=NOTE).json()
    assert client.post(f"/annotations/{made['id']}", json={"status": "done"}).status_code == 422
    assert client.delete(f"/annotations/{made['id']}").json() == {"deleted": made["id"]}
    assert client.get("/annotations").json() == []


def test_writing_a_note_needs_the_write_header(client):
    """Any open tab could otherwise fill the file that steers the next UI change."""
    bare = local_client(client.app)
    bare.headers.pop("x-pnt")
    assert bare.post("/annotations", json=NOTE).status_code == 403


def test_every_page_carries_the_overlay_and_the_side_panel_loads_it(client):
    for path in ("/", "/chart", "/players.html", "/stats.html"):
        html = client.get(path).text
        assert html.count('src="/annotate.js"') == 1 and html.index("annotate.js") < html.index("</body>")
    script = client.get("/annotate.js")
    assert script.headers["content-type"].startswith("text/javascript") and "__pntAnnotate" in script.text
    panel = (EXTENSION_DIR / "sidepanel.html").read_text(encoding="utf-8")
    assert panel.index('src="sidepanel.js"') < panel.index('src="annotate.js"')


def test_an_archive_install_has_no_overlay(client, monkeypatch):
    """Friends install from the archive: a stray backslash key must not reach them."""
    from pnt.server import app as app_module

    assert client.get("/health").json()["dev"] is True  # the suite runs from a checkout
    monkeypatch.setattr(app_module, "DEV", False)
    assert client.get("/health").json()["dev"] is False
    assert "annotate.js" not in client.get("/chart").text
    assert client.get("/annotate.js").status_code == 404
    assert client.get("/annotations").status_code == 404
    assert client.post("/annotations", json=NOTE).status_code == 404
    assert client.delete("/annotations/x").status_code == 404
    script = (EXTENSION_DIR / "annotate.js").read_text(encoding="utf-8")
    assert 'type: "health"' in script and "r.data?.dev" in script
