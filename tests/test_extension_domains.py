"""The extension and the server must both accept every PokerNow address.

A content script whose `matches` miss the page's domain never loads, and nothing
reports it: no console error, the overlay just never appears and the popup says
"not a PokerNow game page". That is how this was found -- a live table at
`pokernow.com/games/...` while the manifest only listed `pokernow.club`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

HOSTS = [
    "https://www.pokernow.com",
    "https://pokernow.com",
    "https://www.pokernow.club",
    "https://pokernow.club",
]


def _content_script_matches() -> set[str]:
    manifest = json.loads((ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
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
