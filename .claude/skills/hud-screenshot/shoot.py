"""Screenshot the HUD side panel in any state with headless Chrome.

    python .claude/skills/hud-screenshot/shoot.py --out DIR [--states loaded,key] [--game ID]

Copies pnt/extension's panel into DIR/site with a chrome.* stub, fills it with a
real `GET /hud/<game>` from a running server, serves it, and writes DIR/<state>.png.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
EXT = ROOT / "pnt" / "extension"
STATES = ["loading", "between", "stuck-loading", "stuck-restart", "stuck-down", "stuck-tab", "waiting",
          "empty", "error", "loaded", "update", "sample", "plo", "double", "narrow", "key", "twins"]
#: The stuck boxes appear at 10/15/30 s; virtual time skips straight there.
SLOW = {"stuck-loading", "stuck-restart", "stuck-down", "stuck-tab", "waiting"}
CHROMES = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
           r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
           "/usr/bin/google-chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
POSITIONS = ["BTN", "SB", "BB", "UTG", "UTG+1", "MP", "LJ", "HJ", "CO", "UTG+2"]


def default_server() -> str:
    # In a worktree, its own server; the live tracker's is only for the main checkout.
    port_file = ROOT / ".pnt-dev" / "port"
    port = port_file.read_text().strip() if port_file.exists() else "52000"
    return f"http://127.0.0.1:{port}"


def get(server: str, path: str):
    with urllib.request.urlopen(server + path, timeout=60) as r:
        return json.load(r)


def latest_game(server: str) -> str:
    hero = get(server, "/settings/hero")["hero"]
    games = get(server, f"/players/{urllib.parse.quote(hero)}/games")
    if not games:
        sys.exit(f"{hero} has no games on {server}; pass --game")
    return games[0]["game_id"]


def live_from_hud(hud: dict, previous) -> dict:
    """A preflop, unopened hand at the HUD's table, for when no hand is in progress."""
    players = [{
        "seat": s["seat"], "pn_id": s["pn_id"], "alias": s["alias"], "name": s["alias"],
        "position": POSITIONS[i % len(POSITIONS)], "stack": 1000, "committed": 0,
        "folded": False, "path": ["unopened"],
        "node": {"label": "unopened", "decision": None}, "resolved": None,
    } for i, s in enumerate(hud["seats"])]
    return {"game_id": hud["game_id"], "hand_number": (previous or {}).get("hand_number", 0) + 1,
            "street": "preflop", "pot": 15, "board": [],
            "to_act": players[-1]["pn_id"] if players else None,
            "players": players, "previous": previous}


def build_site(site: Path, server: str, game: str) -> None:
    site.mkdir(parents=True, exist_ok=True)
    for name in ("sidepanel.js", "spot.js", "annotate.js"):
        if (EXT / name).exists():  # annotate.js: the note overlay, newer than this harness
            shutil.copy(EXT / name, site / name)
    for name in ("stub.js", "scenario.js"):
        shutil.copy(HERE / "harness" / name, site / name)
    hands = get(server, "/health").get("hands", 0)
    conf = json.dumps({"server": server, "hands": hands})
    html = (EXT / "sidepanel.html").read_text(encoding="utf-8")
    spot, panel = '<script src="spot.js"></script>', '<script src="sidepanel.js"></script>'
    if spot not in html or panel not in html:
        sys.exit("sidepanel.html's script tags changed; update shoot.py's injection")
    html = html.replace(spot, f'<script type="application/json" id="harness-conf">{conf}</script>\n'
                              f'  <script src="stub.js"></script>\n  {spot}')
    html = html.replace(panel, f'{panel}\n  <script src="scenario.js"></script>')
    (site / "sidepanel.html").write_text(html, encoding="utf-8")
    hud = get(server, f"/hud/{game}")
    live = get(server, f"/live/{game}")
    if live.get("hand", "") is None:  # between hands
        live = live_from_hud(hud, live.get("previous"))
    (site / "hud.json").write_text(json.dumps(hud), encoding="utf-8")
    (site / "live.json").write_text(json.dumps(live), encoding="utf-8")


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path, help="folder for the site and the PNGs")
    ap.add_argument("--states", default="loaded", help=f"comma list or 'all': {', '.join(STATES)}")
    ap.add_argument("--server", default=default_server())
    ap.add_argument("--game", help="game ID (default: the hero's latest game)")
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--chrome", default=next((c for c in CHROMES if os.path.exists(c)), "chrome"))
    args = ap.parse_args()

    states = STATES if args.states == "all" else args.states.split(",")
    unknown = set(states) - set(STATES)
    if unknown:
        sys.exit(f"unknown states: {', '.join(sorted(unknown))}")
    game = args.game or latest_game(args.server)
    out = args.out.resolve()
    site = out / "site"
    build_site(site, args.server, game)

    handler = functools.partial(QuietHandler, directory=str(site))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        for s in states:
            png = out / f"{s}.png"
            url = f"http://127.0.0.1:{port}/sidepanel.html?s={s}" + ("#update=3" if s == "update" else "")
            subprocess.run([
                args.chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                f"--user-data-dir={out / 'profile'}",  # shared, so localStorage (pnt-cols) persists
                f"--window-size=402,{args.height}", "--force-device-scale-factor=2",
                f"--virtual-time-budget={35000 if s in SLOW else 4000}",
                f"--screenshot={png}", url,
            ], check=True, capture_output=True, timeout=120)
            print(png)
    finally:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
