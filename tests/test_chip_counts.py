"""The chart's chip counts: every chip's preview, the spot a click would leave,
is a filter the server can count.

The previews come from the page's own chip lists and `next…` helpers, run in
node over a few box states (empty, each chip pressed, a mixed spot), so a chip
whose click writes something the parser refuses shows up here rather than as a
chip with no badge.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from pnt.db.conn import connect
from pnt.ingest.importer import import_csv
from pnt.stats.filters import count_terms, parse_filter
from pnt.stats.queries import display_names, facts_by_player
from tests.conftest import ALL_LOGS

CHART = Path(__file__).resolve().parents[1] / "pnt" / "server" / "static" / "chart.html"
REPLAY = CHART.with_name("replay.js")
NODE = shutil.which("node")

CONSTS = ["PRESETS", "LINES", "SIZES", "POT_TYPES", "DEPTHS", "SPR_LOW", "EFF_TERM", "ACTION_ROWS",
          "JAM_ROWS", "JAM_STREETS", "jamPattern", "LIMP_ROW", "LIMP_STRONG", "TEXTURES", "terms"]
HELPERS = ["nextLimpTerms", "nextToggle", "nextGroupTerm", "nextOneOf", "nextDepthTerms", "nextJamTerm"]

# Each More filters row as the page builds it: the same chips, the same helper.
HARNESS = r"""
const chips = [];
for (const [, f] of PRESETS) chips.push(() => (box.trim() === f ? "" : f));
for (const [, key] of LINES)
  for (const term of [key, ...SIZES.map(([, b]) => `${key}=${b}`)]) chips.push(() => nextGroupTerm(key, term));
const group = POT_TYPES.filter(([, , k]) => k !== "toggle").map(([, t]) => t);
for (const [, term, kind] of POT_TYPES) chips.push(() => (kind === "toggle" ? nextToggle(term) : nextOneOf(group, term)));
for (const [, pair] of DEPTHS) chips.push(() => nextDepthTerms(pair));
chips.push(() => nextToggle(SPR_LOW[1]));
for (const [, , cs] of ACTION_ROWS) for (const c of cs) if (c) chips.push(() => nextToggle(c[1]));
for (const [, key] of JAM_ROWS)
  for (const term of [key, ...JAM_STREETS.map(s => `${key}_${s}`)]) chips.push(() => nextJamTerm(key, term));
for (const [, pair] of LIMP_ROW) chips.push(() => nextLimpTerms(pair));
chips.push(() => nextToggle(LIMP_STRONG[1]));
for (const t of TEXTURES) if (t) chips.push(() => nextToggle(`flop=${t[1]}`));
const previews = (state) => { box = state; return chips.map(c => c()); };
const pressed = previews("");
const states = ["", ...pressed, "3bet_pot,cbet_flop=small,eff_bb>=40,eff_bb<100,jam_flop,limp,faced_open=call,flop=paired"];
const out = {};
for (const s of states) out[s] = previews(s);
console.log(JSON.stringify(out));
"""


def _block(page: str, name: str) -> str:
    start = page.index(f"const {name} = ")
    return page[start:page.index(";", start) + 1]


def _function(page: str, name: str) -> str:
    start = page.index(f"function {name}(")
    return page[start:page.index("\n  }\n", start) + 4]


@pytest.fixture(scope="module")
def previews() -> dict[str, list[str]]:
    """Box state -> every chip's preview from it."""
    if NODE is None:
        pytest.skip("node is not installed")
    page = CHART.read_text("utf-8")
    script = "\n".join([
        'let box = "";',
        "const $ = () => ({ get value() { return box; } });",
        # SIZES takes its words from replay.js's SIZE_LABEL.
        _block(REPLAY.read_text("utf-8"), "SIZE_LABEL").strip(),
        *(_block(page, n) for n in CONSTS),
        *(_function(page, n) for n in HELPERS),
        HARNESS,
    ])
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True, encoding="utf-8")
    return json.loads(out.stdout)


def test_every_chip_wires_its_click_through_the_preview():
    """A chip's click writes the same `next…` its badge counts, so the two agree;
    no row is left on a click handler of its own."""
    page = CHART.read_text("utf-8")
    build = page[page.index("for (const [name, f] of PRESETS)"):page.index("for (const [text, sel] of COMMON)")]
    assert not re.search(r'addEventListener\("click"', build)
    for helper in HELPERS:
        assert re.search(rf"chipClick\(b, \(\) => .*{helper}\(", build), helper
    # The Common row's stand-ins count as the chip they click.
    assert "chipNext.set(b, chipNext.get(real))" in page


def test_every_chip_preview_parses(previews):
    for state, spots in previews.items():
        parse_filter(state)
        for spot in spots:
            parse_filter(spot)


def test_a_pressed_chip_previews_its_removal(previews):
    # Pressed alone, every chip's click empties the box, the presets included.
    for spot in previews[""]:
        assert "" in previews[spot], spot
    # Picking within a row replaces that row's term and keeps the rest.
    assert "srp" in previews["3bet_pot"]
    assert "cbet_flop=large" in previews["cbet_flop=small"]
    assert "eff_bb>=200" in previews["eff_bb>=40,eff_bb<100"]
    assert "jam_river" in previews["jam_flop"]


def test_the_counts_take_every_preview(previews, tmp_path):
    """POST /counts' counter over the fixtures: no preview counts null, and the
    empty spot a pressed chip leaves counts every hand."""
    conn = connect(tmp_path / "t.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    grouped, _, _ = facts_by_player(conn)
    names = display_names(conn)
    conn.close()
    facts = max(grouped.values(), key=len)
    spots = sorted({s for ss in previews.values() for s in ss})
    out = count_terms(facts, "", spots, names)
    assert not [s for s in spots if out["counts"][s] is None]
    assert out["counts"][""] == out["base"] == len(facts)
    assert all(0 <= n <= out["base"] for n in out["counts"].values())
