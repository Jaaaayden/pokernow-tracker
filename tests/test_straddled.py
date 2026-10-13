"""Straddled pots: the stats page's "Split by straddle" and the chart's chip.
The fact and the term are F2's; see SPEC.md, "Straddles"."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from pnt.stats.derive import derive
from pnt.stats.filters import parse_filter
from pnt.stats.queries import facts_for, load_hands, report
from tests.conftest import STRADDLE, STRADDLE_GAME
from tests.test_multiway import _by_player, _splits

CHART_HTML = Path(__file__).parent.parent / "pnt/server/static/chart.html"
SPLIT = ("straddled", "!straddled")


def _with_straddles(db):
    from pnt.ingest.importer import import_csv

    import_csv(db, STRADDLE)
    return db


def _is_straddle(a) -> bool:
    return a.is_forced and a.post_kind == "straddle"


def test_the_straddle_split_is_on_the_page():
    assert _splits()["straddle"] == [("straddled", "straddled"), ("not straddled", "!straddled")]


def test_the_chart_has_a_straddled_pot_chip():
    page = CHART_HTML.read_text(encoding="utf-8")
    start = page.index("const POT_TYPES = [")
    block = page[start:page.index("];", start)]
    assert '["straddled", "straddled", "toggle"]' in block
    parse_filter("srp,straddled")


def test_straddled_and_the_rest_sum_to_the_hands(db):
    """The two groups partition every hand, so each player's sub-rows sum to their row."""
    _with_straddles(db)
    whole = _by_player(report(db))
    parts = [_by_player(report(db, predicate=parse_filter(t))) for t in SPLIT]
    assert any(parts[0].values()) and any(parts[1].values()), "both groups have hands"
    for name, row in whole.items():
        got = [p.get(name, {"hands": 0, "_opp": {}}) for p in parts]
        assert sum(g["hands"] for g in got) == row["hands"], name
        assert "vpip" in row["_opp"]
        for key in row["_opp"]:  # a Tally is a sum, so every sample size splits too
            assert sum(g["_opp"].get(key, 0) for g in got) == row["_opp"][key], (name, key)


def test_a_straddled_row_reproduces_from_its_filter(db):
    _with_straddles(db)
    pred = parse_filter("straddled")
    rows = report(db, predicate=pred)
    assert rows
    for row in rows:
        assert sum(1 for f in facts_for(db, row["player"]) if pred(f)) == row["hands"]


def test_a_straddle_hand_is_straddled_and_keeps_its_pot_level(db):
    """Every fixture straddle hand has the fact, and taking the straddle post out
    changes no one's pot level: the straddle is level 1's amount, not a raise."""
    _with_straddles(db)
    hands = [h for h in load_hands(db, STRADDLE_GAME) if any(_is_straddle(a) for a in h.actions)]
    assert hands, "the straddle log has straddles"
    for h in hands:
        facts = {f.pn_id: f for f in derive(h)}
        assert all(f.straddled for f in facts.values()), h.hand_number
        assert parse_filter("straddled")(next(iter(facts.values())))
        plain = {f.pn_id: f for f in derive(replace(h, actions=[a for a in h.actions if not _is_straddle(a)]))}
        assert not any(f.straddled for f in plain.values())
        for pid, f in facts.items():
            assert f.pot_level == plain[pid].pot_level, (h.hand_number, pid)


def test_the_chip_and_the_split_find_the_same_hands_as_the_post(db):
    """`straddled` is exactly the hands with a straddle post in them."""
    _with_straddles(db)
    hands = load_hands(db, STRADDLE_GAME)
    for h in hands:
        posted = any(_is_straddle(a) for a in h.actions)
        assert all(parse_filter("straddled")(f) == posted for f in derive(h)), h.hand_number
        assert all(parse_filter("!straddled")(f) != posted for f in derive(h)), h.hand_number
