"""Stack-depth splits: the stats page's "Split by: stack depth", the chart's depth
chips and the JAM column. See SPEC.md, "Stack-depth splits"."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pnt.stats.filters import parse_filter
from pnt.stats.queries import facts_for, report
from tests.test_multiway import _by_player, _splits

STATIC = Path(__file__).parent.parent / "pnt/server/static"
DEPTH = [
    ("<40bb", "eff_bb<40"),
    ("40–100bb", "eff_bb>=40,eff_bb<100"),
    ("100–200bb", "eff_bb>=100,eff_bb<200"),
    ("200bb+", "eff_bb>=200"),
]


def test_the_depth_split_is_the_four_buckets():
    assert _splits()["depth"] == DEPTH
    page = (STATIC / "stats.html").read_text(encoding="utf-8")
    assert 'SPLIT_NAMES.depth = "stack depth"' in page


def test_the_chart_chips_are_the_same_buckets():
    """The chart's Depth row writes the very terms the stats page splits by, so a
    sub-row and a chip open the same hands."""
    page = (STATIC / "chart.html").read_text(encoding="utf-8")
    block = page[page.index("const DEPTHS = ["):]
    block = block[:block.index("];")]
    assert re.findall(r'\["([^"]+)", "([^"]+)"\]', block) == DEPTH
    assert '"spr_flop<=3"' in page
    parse_filter("spr_flop<=3")


def test_jam_column_has_its_tip_and_sample_noun():
    page = (STATIC / "stats.html").read_text(encoding="utf-8")
    assert '["jam_pf", "JAM", "pct", "jam_pf"]' in page
    assert re.search(r"^\s*jam_pf: \[\"Preflop jam\",", page, re.MULTILINE)
    assert re.search(r'^\s*jam_pf: "preflop decisions",', page, re.MULTILINE)


def _depth_rows(db, spot: str | None) -> tuple[dict, list[dict]]:
    def rows(term: str | None) -> dict[str, dict]:
        filt = ",".join(t for t in (spot, term) if t)
        return _by_player(report(db, predicate=parse_filter(filt) if filt else None))

    return rows(None), [rows(term) for _, term in DEPTH]


@pytest.mark.parametrize("spot", [None, "srp", "position=BTN"])
def test_buckets_sum_to_the_hands_with_a_known_stack(db, spot):
    """The four buckets partition the hands whose effective stack is known: every
    count of a player's sub-rows sums to their whole row's."""
    whole, parts = _depth_rows(db, spot)
    assert sum(1 for p in parts if p) >= 3, "the corpus spans several depths"
    keys = ("vpip", "3bet", "jam_pf", "wtsd", "cbet_flop")
    for name, row in whole.items():
        got = [p.get(name, {"hands": 0, "_opp": {}}) for p in parts]
        known = sum(1 for f in facts_for(db, name)
                    if f.eff_bb is not None and (spot is None or parse_filter(spot)(f)))
        assert sum(g["hands"] for g in got) == known == row["hands"], (name, spot)
        for key in keys:
            assert sum(g["_opp"].get(key, 0) for g in got) == row["_opp"][key], (name, key)


def test_unknown_stack_hands_are_in_no_bucket(db):
    """Judgement call 26: one missing stack leaves eff_bb None for everyone in the
    hand, so the buckets sum to the known hands, not to all of them."""
    hand_id, = db.execute(
        "SELECT hand_id FROM hand_players GROUP BY hand_id HAVING count(*) >= 3 ORDER BY hand_id LIMIT 1"
    ).fetchone()
    pids = [r[0] for r in db.execute("SELECT pn_id FROM hand_players WHERE hand_id = ?", (hand_id,))]
    db.execute("UPDATE hand_players SET starting_stack = NULL WHERE hand_id = ? AND pn_id = ?",
               (hand_id, pids[0]))
    db.commit()

    whole, parts = _depth_rows(db, None)
    short = 0
    for name, row in whole.items():
        facts = facts_for(db, name)
        unknown = [f for f in facts if f.eff_bb is None]
        assert all(f.hand_id == hand_id for f in unknown)
        for f in unknown:
            assert not any(parse_filter(term)(f) for _, term in DEPTH)
        got = sum(p.get(name, {"hands": 0})["hands"] for p in parts)
        assert got == row["hands"] - len(unknown), name
        short += len(unknown)
    assert short == len(pids), "every player in that hand lost their depth"


def test_a_depth_sub_row_reproduces_from_its_filter(db):
    """A sub-row is `<spot>,<term>`: the same filter on one player's facts gives its n."""
    for _, term in DEPTH:
        filt = f"srp,{term}"
        pred = parse_filter(filt)
        for row in report(db, predicate=pred):
            assert sum(1 for f in facts_for(db, row["player"]) if pred(f)) == row["hands"], (term, row["player"])
