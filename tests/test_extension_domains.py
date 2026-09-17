"""The extension and the server must both accept every PokerNow address.

A content script whose `matches` miss the page's domain never loads, and nothing
reports it: no console error, the HUD just never appears and the settings say
"not a PokerNow game page". That is how this was found -- a live table at
`pokernow.com/games/...` while the manifest only listed `pokernow.club`.
"""

from __future__ import annotations

import json

import pytest

#: Taken from the package rather than spelled out again, so moving the extension
#: cannot leave this test asserting about a folder that is no longer shipped.
from pnt.cli import EXTENSION_DIR

HOSTS = [
    "https://www.pokernow.com",
    "https://pokernow.com",
    "https://www.pokernow.club",
    "https://pokernow.club",
]


def _content_script_matches() -> set[str]:
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))
    return {m for script in manifest["content_scripts"] for m in script["matches"]}


def _preflight(origin: str):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from pnt.server.app import app

    return TestClient(app).options(
        "/ingest", headers={"Origin": origin, "Access-Control-Request-Method": "POST"}
    )


@pytest.mark.parametrize("host", HOSTS)
def test_content_script_loads_on_every_pokernow_host(host):
    assert f"{host}/games/*" in _content_script_matches()


@pytest.mark.parametrize("host", HOSTS)
def test_server_accepts_every_pokernow_host(host):
    assert _preflight(host).headers.get("access-control-allow-origin") == host


def test_server_still_refuses_other_origins():
    """The server has no auth; widening the list must not mean opening it."""
    assert "access-control-allow-origin" not in _preflight("https://example.com").headers


def test_the_extension_ships_inside_the_package():
    """A pip or pipx install must contain an extension to load.

    It used to live beside the package rather than inside it, so an installed copy
    had none at all -- `pnt extension` would point at nothing and the only way to
    get the browser half was to clone the repo.
    """
    assert (EXTENSION_DIR / "manifest.json").is_file(), f"no manifest in {EXTENSION_DIR}"
    shipped = (
        "background.js", "content.js", "normalize.js", "pager.js", "popup.html", "popup.js",
        "sidepanel.html", "sidepanel.js", "watch.js",
    )
    for name in shipped:
        assert (EXTENSION_DIR / name).is_file(), f"{name} missing from {EXTENSION_DIR}"
    # Every script the content script relies on must be loaded ahead of it.
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))
    js = manifest["content_scripts"][0]["js"]
    assert js.index("pager.js") < js.index("content.js")
    assert js.index("watch.js") < js.index("content.js")


def test_the_hud_opens_in_the_side_panel():
    """The HUD is drawn in Chrome's side panel, beside the page rather than over it.

    The toolbar icon opens it, which only works while the action has no popup: a
    `default_popup` would take the click and the panel would never open.
    """
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert "sidePanel" in manifest["permissions"]
    path = manifest["side_panel"]["default_path"]
    assert (EXTENSION_DIR / path).is_file()
    assert "default_popup" not in manifest["action"]
