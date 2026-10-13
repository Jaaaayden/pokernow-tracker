"""Squeeze and cold-call on the pages: the stats table's SQZ and CC columns and the
range chart's two preflop chips.

The facts and rates are the foundation's (`test_foundation2.py` pins them); this
holds the pages to them: every column names a rate the server sends, with its
sample-size noun and its key entry, and every per-player figure reproduces from
the chip's filter.
"""

from __future__ import annotations

import re
from pathlib import Path

from pnt.stats.filters import parse_filter
from pnt.stats.queries import display_names, facts_for, report

STATIC = Path(__file__).parent.parent / "pnt/server/static"


def _block(page: str, start: str, end: str) -> str:
    i = page.index(start)
    return page[i:page.index(end, i)]


def test_stats_table_has_squeeze_and_cold_call_columns(db):
    page = (STATIC / "stats.html").read_text(encoding="utf-8")
    base = _block(page, "const BASE = [", "];")
    assert '["squeeze", "SQZ", "pct", "squeeze"]' in base
    assert '["cold_call", "CC", "pct", "cold_call"]' in base
    # The key below the table and the header tooltip come from BASE_TIP; the
    # sample-size tooltip on every cell from OPP_NOUN.
    tips = _block(page, "const BASE_TIP = {", "\n  };")
    nouns = _block(page, "const OPP_NOUN = {", "\n  };")
    for key in ("squeeze", "cold_call"):
        assert re.search(rf"\b{key}: \[", tips), key
        assert re.search(rf"\b{key}: \"", nouns), key
    # And the server sends both, each with its denominator under `_opp`.
    row = report(db)[0]
    assert {"squeeze", "cold_call"} <= set(row) and {"squeeze", "cold_call"} <= set(row["_opp"])


def test_chart_has_squeeze_and_cold_call_chips():
    page = (STATIC / "chart.html").read_text(encoding="utf-8")
    presets = _block(page, "const PRESETS = [", "];")
    terms = dict(re.findall(r'\["([^"]+)", "([^"]+)"\]', presets))
    assert terms["Squeeze"] == "squeeze" and terms["Cold call"] == "cold_call"
    # The chip that was already called "Squeezed" is the other side of the spot.
    assert terms["Got squeezed"] == "called_open,faced_3bet_any"
    for term in ("squeeze", "cold_call"):
        parse_filter(term)


def test_every_players_squeeze_and_cold_call_reproduce_from_the_chips(db):
    """The stats row's rate and its `_opp` are the hands the chip's filter finds."""
    names = display_names(db)
    seen = {"squeeze": 0, "cold_call": 0}
    for row in report(db):
        facts = facts_for(db, row["player"])
        for key in seen:
            opp = sum(1 for f in facts if parse_filter(f"{key}_opp", names)(f))
            hits = sum(1 for f in facts if parse_filter(key, names)(f))
            assert row["_opp"][key] == opp, (row["player"], key)
            if opp:
                assert row[key] == round(100.0 * hits / opp, 1), (row["player"], key)
            else:
                assert row[key] is None, "no chances is unknown, not 0%"
            seen[key] += opp
    # The three core logs hold squeeze chances but no squeeze; the pool-wide
    # count of real squeezes is `test_foundation2`'s, over every fixture log.
    assert all(seen.values()), f"the fixtures have both spots: {seen}"
