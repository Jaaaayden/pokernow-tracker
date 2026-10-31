"""The chart's views as the pre-release review left them: one money colour pair,
the stats page's labels, the hero by default, and no fixed prose dressed as data."""

from __future__ import annotations

import re
from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "pnt" / "server" / "static" / "chart.html").read_text("utf-8")


def test_a_bare_chart_opens_on_the_hero():
    assert '"/settings/hero"' in HTML


def test_money_is_green_and_red_on_the_grid():
    """The net ramp is the --up/--down pair every other money figure takes."""
    net = HTML[HTML.index('if (state.color === "net")'):HTML.index('if (state.color === "pfr")')]
    assert "ramp(GREEN)" in net and "ramp(BLUE)" not in net


def test_tilt_says_afq_as_the_stats_page_does():
    assert '["AFq", ' in HTML and '["AF", ' not in HTML


def test_timing_prints_no_fixed_findings():
    assert "Slow calls" not in HTML and "long think before a river bet" not in HTML


def test_every_toggle_has_a_pressed_style():
    rule = re.search(r":is\(([^)]*)\)\[aria-pressed=true\]", HTML)
    assert rule and all(x in rule.group(1) for x in ("#esttoggle", "#tabletoggle", "#ttexture", "#hands-split"))


def test_list_views_can_show_every_row():
    assert "function moreRows(" in HTML and HTML.count("moreRows(list, rows.length)") == 3


def test_no_unused_chip_setters():
    for name in ("setLimpTerms", "toggleTerm", "setGroupTerm", "setDepthTerms", "setJamTerm"):
        assert name not in HTML, name


def test_no_value_reads_as_an_em_dash():
    assert '"—"' not in HTML
