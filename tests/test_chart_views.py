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


def test_red_and_green_are_money_only():
    """RED and GREEN ramps fill only the grid's net mode; every other two-ended
    scale (tiers, made mix, raise size) runs blue to ORANGE."""
    script = HTML[HTML.index("<script>\n"):]
    net = script[script.index('if (state.color === "net")'):script.index('if (state.color === "pfr")')]
    assert script.count("ramp(RED)") == net.count("ramp(RED)") == 2
    assert script.count("ramp(GREEN)") == net.count("ramp(GREEN)") == 2
    assert "RED[" not in script and "GREEN[" not in script
    # The CSS scales: value:bluff's bluff end and the tree's bet/raise are not the loss red.
    for loss in ("#b93a39", "#e66e6d", "#e34948", "#f6b6b5", "#ef9291"):
        for scale in ("--bv-semi_bluff: " + loss, "--bv-bluff: " + loss, "--tr-raise: " + loss):
            assert scale not in HTML, scale


def test_no_version_skew_shims():
    """One error for a route the server lacks (pntPage.OLD_SERVER); no per-field
    probes for an older server's answers."""
    assert "older than this page" not in HTML
    assert HTML.count("pntPage.OLD_SERVER") == 1
    assert 'by") === "manual"' not in HTML


def test_size_buckets_have_one_wording():
    """Chips, the strip and hand rows take SIZE_LABEL's words, as Sizing does."""
    assert "SHORT_SIZE" not in HTML and '"<⅕"' not in HTML
    assert "map(b => [SIZE_LABEL[b], b])" in HTML


def test_spotless_views_dim_the_spot():
    assert 'new Set(["shows", "bounties", "nit"])' in HTML and "syncSpotless(view);" in HTML
