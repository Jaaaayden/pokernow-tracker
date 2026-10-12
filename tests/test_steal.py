"""Steal and blind defense on the pages: the /stats columns, the chart chips, and
the dead-button and dead-blind hands every positional figure leaves out.

The facts themselves are pinned in `test_foundation2.py`; this file holds the
parts built on them (SPEC.md, "Steals and blind defense").
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from pnt.stats.derive import derive
from pnt.stats.filters import parse_filter
from pnt.stats.queries import facts_for, report
from tests.conftest import ALL_LOGS, local_client
from tests.test_lines import _hand

STATIC = Path(__file__).parent.parent / "pnt/server/static"
#: The three blind-war columns: rate key -> the `_opp` key its sample is under.
COLUMNS = {"ats": "ats", "fold_to_steal": "fold_to_steal", "3bet_vs_steal": "3bet_vs_steal"}


def _steal_hand():
    """Four-handed, bb 10: the cutoff folds, the button opens, both blinds fold."""
    return _hand(["S", "B", "C", "D"], [
        ("preflop", "S", "post", 5), ("preflop", "B", "post", 10),
        ("preflop", "C", "fold", 0), ("preflop", "D", "raise", 25, 25),
        ("preflop", "S", "fold", 0), ("preflop", "B", "fold", 0),
    ])


@pytest.fixture()
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    monkeypatch.setenv("PNT_DB", str(tmp_path / "steal.sqlite"))
    import importlib

    from pnt.server import app as app_module

    importlib.reload(app_module)

    from pnt.db.conn import connect
    from pnt.ingest.importer import import_csv

    conn = connect(tmp_path / "steal.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    conn.close()
    return local_client(app_module.app)


def test_stats_exposes_the_three_columns_with_their_samples(client):
    rows = client.get("/stats").json()
    assert rows
    for row in rows:
        for key, opp in COLUMNS.items():
            assert key in row and opp in row["_opp"], key
            n = row["_opp"][opp]
            assert (row[key] is None) == (n == 0), (row["player"], key)
            if n:
                assert 0 <= row[key] <= 100
        # Fold to steal and 3-bet vs steal are two answers to the same steals.
        assert row["_opp"]["fold_to_steal"] == row["_opp"]["3bet_vs_steal"]
    assert any(r["_opp"]["ats"] for r in rows), "the fixtures have steal chances"
    assert any(r["_opp"]["fold_to_steal"] for r in rows), "and steals faced"


def test_every_column_reproduces_from_its_filter(db):
    """A column's count is the hands its filter finds: `steal_opp` and `steal` for
    ATS, `fold_to_steal_opp` with `fold_to_steal` and `3bet_vs_steal` for defense."""
    terms = {
        "ats": ("steal", "steal_opp"),
        "fold_to_steal": ("fold_to_steal", "fold_to_steal_opp"),
        "3bet_vs_steal": ("3bet_vs_steal", "fold_to_steal_opp"),
    }
    for row in report(db):
        facts = facts_for(db, row["player"])
        for key, (num, den) in terms.items():
            hits = sum(1 for f in facts if parse_filter(num)(f))
            n = sum(1 for f in facts if parse_filter(den)(f))
            assert row["_opp"][key] == n, (row["player"], key)
            assert row[key] == (round(100.0 * hits / n, 1) if n else None), (row["player"], key)


def test_stats_page_has_the_columns_with_tips_and_sample_nouns():
    page = (STATIC / "stats.html").read_text(encoding="utf-8")

    def block(name: str) -> str:
        start = page.index(f"const {name} = ")
        return page[start:page.index("\n  };" if name != "BASE" else "\n  ];", start)]

    base = block("BASE")
    for key, opp in COLUMNS.items():
        assert re.search(rf'\["{re.escape(key)}", "[^"]+", "pct", "{re.escape(opp)}"\]', base), key
        assert re.search(rf'^\s+("{re.escape(key)}"|{re.escape(key)}):', block("BASE_TIP"), re.M), key
        assert re.search(rf'("{re.escape(opp)}"|\b{re.escape(opp)}): "', block("OPP_NOUN")), opp


def test_chart_has_the_steal_chips():
    page = (STATIC / "chart.html").read_text(encoding="utf-8")
    start = page.index("const PRESETS = [")
    presets = page[start:page.index("];", start)]
    terms = re.findall(r'\["[^"]+", "([^"]+)"\]', presets)
    for term in ("steal", "fold_to_steal", "3bet_vs_steal"):
        assert term in terms, term
        parse_filter(term)


@pytest.mark.parametrize("flag", ["dead_button", "blinds_irregular"])
def test_dead_button_and_dead_blind_hands_are_left_out(flag):
    """The seat names are guesses on these hands, so no steal is counted on them,
    by the facts or by any of the filters the columns and chips use -- even though
    `position=` itself keeps a dead-blind hand."""
    h = _steal_hand()
    live = {f.pn_id: f for f in derive(h)}
    terms = ("steal_opp", "steal", "fold_to_steal_opp", "fold_to_steal", "3bet_vs_steal")
    assert any(parse_filter(t)(f) for t in terms for f in live.values())
    setattr(h, flag, True)
    dead = list(derive(h))
    for term in terms:
        assert not any(parse_filter(term)(f) for f in dead), (flag, term)
    for f in dead:
        assert not (f.steal_opp or f.steal or f.fold_to_steal_opp or f.fold_to_steal or f.three_bet_vs_steal)


# ------------------------------------------------------------------ tags ----


def _tag(rows, id: str):
    from pnt.stats.tags import tags_for

    return next((t for t in tags_for(rows)["tags"] if t["id"] == id), None)


def _blind(n: int, answers: dict[str, int]) -> list:
    """`n` copies of the big blind facing the button's steal, answered `answers`
    times each way (call / raise), the rest folds."""
    b = {f.pn_id: f for f in derive(_steal_hand())}["B"]
    out = []
    for answer, k in [*answers.items(), ("fold", n - sum(answers.values()))]:
        out += [replace(b, hand_id=len(out) + i, pf_faced={**b.pf_faced, 2: answer}) for i in range(k)]
    return out


def test_folds_to_steal_fires_at_its_threshold_and_not_under_its_sample():
    from pnt.stats.tags import THRESHOLDS

    assert THRESHOLDS["folds_to_steal"] == {"pct": 35, "n": 30}
    # 11 folds of 30 is 36.7%: over the line.
    t = _tag(_blind(30, {"call": 19}), "folds_to_steal")
    assert t and (t["n"], t["hits"], t["kind"], t["filter"]) == (30, 11, "exploit", "fold_to_steal_opp")
    assert t["label"] == "FOLDS TO STEAL" and "11 of 30" in t["tip"]
    assert _tag(_blind(30, {"call": 20}), "folds_to_steal") is None, "10 of 30 is 33.3%, under the line"
    assert _tag(_blind(29, {"call": 10}), "folds_to_steal") is None, "29 steals faced is under the sample"
    assert all(parse_filter("fold_to_steal_opp")(f) for f in _blind(30, {}))


def test_folds_to_steal_ignores_dead_button_hands():
    """Thirty dead-button folds are no evidence: the seat names are guesses."""
    rows = [replace(f, dead_button=True) for f in _blind(30, {})]
    assert not any(f.fold_to_steal_opp for f in rows)
    assert _tag(rows, "folds_to_steal") is None
