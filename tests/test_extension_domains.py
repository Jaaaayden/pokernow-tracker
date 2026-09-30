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
    from pnt.server.app import app
    from tests.conftest import local_client

    return local_client(app).options(
        "/ingest", headers={"Origin": origin, "Access-Control-Request-Method": "POST"}
    )


@pytest.mark.parametrize("host", HOSTS)
def test_content_script_loads_on_every_pokernow_host(host):
    assert f"{host}/games/*" in _content_script_matches()


@pytest.mark.parametrize("host", HOSTS)
def test_server_gives_pokernow_pages_no_access(host):
    """The page never calls the server: the background worker does, and its host
    permission needs no CORS. Allowing these origins would only have let PokerNow's
    own pages read every hand in the database, hole cards included."""
    assert "access-control-allow-origin" not in _preflight(host).headers


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
        "background.js", "content.js", "normalize.js", "pager.js", "spot.js", "popup.html", "popup.js",
        "sidepanel.html", "sidepanel.js", "watch.js",
    )
    for name in shipped:
        assert (EXTENSION_DIR / name).is_file(), f"{name} missing from {EXTENSION_DIR}"
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))
    for icon in {**manifest["icons"], **manifest["action"]["default_icon"]}.values():
        assert (EXTENSION_DIR / icon).is_file(), f"{icon} missing from {EXTENSION_DIR}"
    # Every script the content script relies on must be loaded ahead of it.
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))
    js = manifest["content_scripts"][0]["js"]
    assert js.index("spot.js") < js.index("content.js")
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
    # The panel uses spot.js's helpers, so it must be loaded first.
    html = (EXTENSION_DIR / path).read_text(encoding="utf-8")
    assert 0 <= html.index('src="spot.js"') < html.index('src="sidepanel.js"')


def test_the_settings_are_reachable_without_a_hud():
    """A browser with no side panel API (Opera) must still get a HUD and settings.

    There, a saved "panel" left the toolbar icon doing nothing, and the settings --
    opened from the HUD's gear -- could not be reached to switch to the floating box.
    So the worker reads the mode as "float" wherever the API is missing, and the
    settings page is the extension's options page too (right-click the icon).
    """
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["options_page"] == "popup.html"
    background = (EXTENSION_DIR / "background.js").read_text(encoding="utf-8")
    assert '!chrome.sidePanel ? "float"' in background
    assert "hudMode: modeOf(s.hudMode)" in background


def test_the_floating_hud_can_frame_the_panel_on_every_game_domain():
    """Float mode frames sidepanel.html on the game page itself.

    A page may only frame an extension page listed as web-accessible to it, so
    every domain the content script runs on must be listed -- else the box on
    that domain is blank -- and no other site is given it.
    """
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))
    entries = [e for e in manifest.get("web_accessible_resources", []) if "sidepanel.html" in e["resources"]]
    assert entries, "sidepanel.html is not web-accessible"
    # Every extension page framed inside it needs listing too: Chrome checks each
    # frame whose ancestors include the game page, and blocks the settings (⚙)
    # with "This page has been blocked by Chrome" otherwise.
    framed = {"popup.html"}
    assert all(framed <= set(e["resources"]) for e in entries)
    exposed = {m.split("/*")[0].rstrip("/") for e in entries for m in e["matches"]}
    assert exposed == set(HOSTS)
    assert {m.split("/games/")[0] for m in _content_script_matches()} == set(HOSTS)
    # The HUD mode is a saved setting like the others, with the side panel as default.
    background = (EXTENSION_DIR / "background.js").read_text(encoding="utf-8")
    assert 'hudMode: "panel"' in background


def test_the_side_panel_knows_a_game_tab_by_its_url():
    """The panel is switched on per game tab, found by URL.

    It used to wait for the content script's first report, so after the extension
    was reloaded a game tab left open never reported and the toolbar icon did
    nothing at all. Reading a tab's URL needs host permission for it, so every
    game address the content script runs on must be in host_permissions.
    """
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert _content_script_matches() <= set(manifest["host_permissions"])
