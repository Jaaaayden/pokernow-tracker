"""The give-up funnel: its steps only narrow, every row reproduces from its filter,
and WWSF is a plain count of hands. See SPEC.md, "Give-up funnel"."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from pnt.db.conn import connect
from pnt.ingest.importer import import_csv
from pnt.stats.filters import parse_filter
from pnt.stats.funnel import funnel
from pnt.stats.queries import display_names, facts_for, rates, report, tally
from tests.conftest import ALL_LOGS, EDGE_LOGS, local_client


def _players(conn):
    return [r["player"] for r in report(conn)]


def _rows(out: dict) -> list[dict]:
    return [*out["steps"], *out["won"], *(x for r in out["folds"] for x in (r, *r["roles"]))]


def test_steps_never_widen(db):
    seen = 0
    for player in _players(db):
        out = funnel(facts_for(db, player))
        ns = [s["n"] for s in out["steps"]]
        assert ns == sorted(ns, reverse=True), (player, ns)
        assert ns[0] == out["flops"]
        seen += out["flops"]
    assert seen


def test_steps_never_widen_on_the_edge_logs(tmp_path):
    conn = connect(tmp_path / "e.sqlite")
    for path in EDGE_LOGS:
        import_csv(conn, path)
    for player in _players(conn):
        ns = [s["n"] for s in funnel(facts_for(conn, player))["steps"]]
        assert ns == sorted(ns, reverse=True), (player, ns)
    conn.close()


def test_every_row_reproduces_from_its_filter(db):
    """A row's filter, added to the spot, finds exactly its hands; a fold row's
    `of_filter` finds exactly what it is a share of."""
    names = display_names(db)
    checked = Counter()
    for player in _players(db):
        all_facts = facts_for(db, player)
        for spot in ("", "srp", "position=BB"):
            pred = parse_filter(spot, names) if spot else None
            facts = [f for f in all_facts if pred is None or pred(f)]
            out = funnel(facts)
            for r in _rows(out):
                for key, n in (("filter", "n"), ("of_filter", "of")):
                    if key not in r:
                        continue
                    combined = ",".join(t for t in (spot, r[key]) if t)
                    p = parse_filter(combined, names)
                    assert sum(1 for f in all_facts if p(f)) == r[n], (player, spot, r)
                    checked[key] += 1
    assert checked["filter"] and checked["of_filter"]


def test_fold_roles_partition_their_street(db):
    for player in _players(db):
        out = funnel(facts_for(db, player))
        steps = {s["key"]: s["n"] for s in out["steps"]}
        for r in out["folds"]:
            assert sum(x["n"] for x in r["roles"]) == r["n"]
            assert sum(x["of"] for x in r["roles"]) == r["of"] == steps[r["key"]]
            # Nobody folds a street they never reached.
            assert r["n"] <= r["of"]
        assert out["total_folds"] == sum(r["n"] for r in out["folds"])


def test_wwsf_agrees_with_rates(db):
    """The funnel's WWSF rows are `rates()`'s, and showdown wins plus the wins
    without one make up WWSF."""
    for player in _players(db):
        facts = facts_for(db, player)
        out = funnel(facts)
        won = {r["key"]: r for r in out["won"]}
        by_hand = sum(1 for f in facts if f.saw_flop and f.won_pot)
        assert won["wwsf"]["n"] == by_hand
        r = rates(tally(facts))
        if out["flops"]:
            assert won["wwsf"]["pct"] == pytest.approx(r["wwsf"], abs=0.051)
            assert won["wwsf_nsd"]["pct"] == pytest.approx(r["wwsf_nsd"], abs=0.051)
            assert r["_opp"]["wwsf"] == out["flops"]
        # The showdown wins and the wins without one are WWSF between them.
        steps = {s["key"]: s["n"] for s in out["steps"]}
        assert steps["won_sd"] + won["wwsf_nsd"]["n"] == won["wwsf"]["n"]


def test_wwsf_is_counted_from_the_raw_hands(db):
    """The same count with no `Facts` at all: the stored hands' own columns."""
    raw = set()
    for r in db.execute(
        """SELECT hp.hand_id, hp.pn_id, h.board_json FROM hand_players hp
           JOIN hands h ON h.hand_id = hp.hand_id
           WHERE hp.collected > 0 AND NOT EXISTS (
               SELECT 1 FROM actions a WHERE a.hand_id = hp.hand_id AND a.pn_id = hp.pn_id
               AND a.street = 'preflop' AND a.action_type = 'fold')"""
    ):
        runs = json.loads(r["board_json"] or "[]")
        if runs and len(runs[0]) >= 3:  # a flop was dealt
            raw.add((r["hand_id"], r["pn_id"]))
    total = 0
    for player in _players(db):
        facts = facts_for(db, player)
        n = len(raw & {(f.hand_id, f.pn_id) for f in facts})
        won = {r["key"]: r["n"] for r in funnel(facts)["won"]}
        assert won["wwsf"] == n, player
        total += n
    assert total == len(raw)


def test_funnel_endpoint(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import importlib

    monkeypatch.setenv("PNT_DB", str(tmp_path / "api.sqlite"))
    from pnt.server import app as app_module

    importlib.reload(app_module)
    conn = connect(tmp_path / "api.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    expect = funnel(facts_for(conn, "HSJ"))
    conn.close()
    client = local_client(app_module.app)

    body = client.get("/players/HSJ/funnel").json()
    assert body["player"] == "HSJ" and body["filter"] is None
    assert body["flops"] == expect["flops"] > 0
    assert [s["key"] for s in body["steps"]] == ["flop", "turn", "river", "showdown", "won_sd"]
    assert [r["key"] for r in body["folds"]] == ["flop", "turn", "river"]
    spot = client.get("/players/HSJ/funnel", params={"filter": "srp"}).json()
    assert spot["filter"] == "srp" and spot["flops"] <= body["flops"]
    assert client.get("/players/HSJ/funnel", params={"filter": "nonsense"}).status_code == 400
    assert client.get("/players/ghost/funnel").status_code == 404


def test_stats_and_chart_pages_have_it():
    static = Path(__file__).resolve().parents[1] / "pnt" / "server" / "static"
    stats = (static / "stats.html").read_text("utf-8")
    assert '["wwsf", "WWSF"' in stats and '["wwsf_nsd", "W$WSF"' in stats
    assert "wwsf: [" in stats and "wwsf_nsd: [" in stats
    chart = (static / "chart.html").read_text("utf-8")
    assert "function renderFunnel(" in chart and "/funnel?" in chart
