"""Limp-reraise on the pages: the /stats LRR column, the chart's limp chips.

The rate itself is F2's (SPEC.md, "Decision points"); these hold the pages to it.
The column must be exactly the hands its filter finds, and every chip a working
spot.
"""

from __future__ import annotations

import re
from pathlib import Path

from pnt.stats.filters import parse_filter
from pnt.stats.queries import display_names, facts_for, report

STATIC = Path(__file__).parent.parent / "pnt/server/static"


def _block(page: str, name: str) -> str:
    start = page.index(f"const {name} = ")
    return page[start:page.index(";", start)]


def test_the_lrr_column_is_the_hands_its_filter_finds(db):
    names = display_names(db)
    num, den = parse_filter("limp,faced_open=raise", names), parse_filter("limp,faced_open", names)
    faced = 0
    for row in report(db):
        facts = facts_for(db, row["player"])
        hits = sum(1 for f in facts if num(f))
        n = sum(1 for f in facts if den(f))
        assert row["_opp"]["limp_reraise"] == n, row["player"]
        assert row["limp_reraise"] == (round(100.0 * hits / n, 1) if n else None), row["player"]
        faced += n
    assert faced, "the fixtures have limps that faced a raise"


def test_the_lrr_column_is_on_the_stats_page():
    page = (STATIC / "stats.html").read_text(encoding="utf-8")
    assert '["limp_reraise", "LRR", "pct", "limp_reraise"]' in _block(page, "BASE")
    # The sample-size tooltip and the key both need it named.
    assert "limp_reraise:" in _block(page, "OPP_NOUN")
    assert "limp_reraise: [" in page[page.index("const BASE_TIP = "):]


def test_every_limp_chip_is_a_working_filter():
    page = (STATIC / "chart.html").read_text(encoding="utf-8")
    pair = re.compile(r'\["([^"]+)", "([^" ]+)", "[^"]+"\]')
    chips = dict(pair.findall(_block(page, "LIMP_ROW")))
    assert chips == {
        "limp-reraise": "limp,faced_open=raise",
        "limp-call": "limp,faced_open=call",
        "limp-fold": "limp,faced_open=fold",
    }
    strong = pair.findall(_block(page, "LIMP_STRONG"))
    assert [t for _, t in strong] == ["hand_pct<=15"]
    for term in [*chips.values(), "hand_pct<=15", "limp,faced_open=raise,hand_pct<=15"]:
        parse_filter(term)


def test_the_three_limp_chips_split_every_limp_that_faced_a_raise(db):
    names = display_names(db)
    preds = [parse_filter(f"limp,faced_open={d}", names) for d in ("raise", "call", "fold")]
    den = parse_filter("limp,faced_open", names)
    for row in report(db):
        facts = facts_for(db, row["player"])
        assert sum(1 for f in facts for p in preds if p(f)) == sum(1 for f in facts if den(f))
