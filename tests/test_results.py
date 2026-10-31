"""Results by pot type: each table partitions the hands, each row reproduces from
its filter, and the money is bb/100's money. See SPEC.md, "Results by pot type"."""

from __future__ import annotations

import json
from collections import Counter
from fractions import Fraction

import pytest
from typer.testing import CliRunner

from pnt import cli
from pnt.db.conn import connect
from pnt.ingest.importer import import_csv
from pnt.stats.derive import Facts
from pnt.stats.filters import parse_filter
from pnt.stats.queries import display_names, facts_for, report, tally
from pnt.stats.results import WORST_MIN_HANDS, _mark_worst, results_breakdown
from tests.conftest import ALL_LOGS, TRUNCATED, local_client


def _rows(table: dict) -> list[dict]:
    return [x for r in table["rows"] for x in (r, *r.get("roles", ()))]


def _bb(facts) -> Fraction:
    """The exact sum a row's net_bb is rounded from."""
    return tally(facts).bb_sum


def _bb_of(row: dict, facts, names) -> Fraction:
    pred = parse_filter(row["filter"], names)
    return _bb([f for f in facts if pred(f)])


def _players(db):
    return [r["player"] for r in report(db)]


def test_every_table_adds_up_to_the_total(db):
    """Hands and net, to the exact fraction: each table partitions the hands, and a
    pot type's role rows partition the pot type."""
    names = display_names(db)
    for player in _players(db):
        facts = facts_for(db, player)
        out = results_breakdown(facts)
        total = out["total"]
        assert total["hands"] == out["hands"] == len(facts)
        for table in out["tables"]:
            rows = table["rows"]
            assert sum(r["hands"] for r in rows) == total["hands"], (player, table["key"])
            assert sum(r["bb_hands"] for r in rows) == total["bb_hands"], (player, table["key"])
            # Exact: every row's hands, found again, carry the total's fraction.
            pieces = []
            for r in rows:
                if r["filter"] is None:
                    ids = set(r["hand_ids"])
                    rest = [f for f in facts if f.saw_flop]  # the only rows without a filter
                    mine = [f for f in rest if f.hand_id in ids and f.in_position is (r["key"] == "ip")]
                else:
                    pred = parse_filter(r["filter"], names)
                    mine = [f for f in facts if pred(f)]
                assert len(mine) == r["hands"]
                pieces.append(_bb(mine))
                if r.get("roles"):
                    assert all(x["filter"].startswith(r["filter"] + ",") for x in r["roles"])
                    assert sum(x["hands"] for x in r["roles"]) == r["hands"]
                    assert sum(_bb_of(x, facts, names) for x in r["roles"]) == pieces[-1]
            assert sum(pieces) == _bb(facts), (player, table["key"])
            rounded = sum(r["net_bb"] or 0 for r in rows)
            assert rounded == pytest.approx(total["net_bb"] or 0, abs=0.01 * len(rows))


def test_every_row_reproduces_from_its_filter(db):
    """Clicking a row adds its filter to the spot: that filter has to find exactly
    the row's hands, inside the spot as well as on all hands."""
    names = display_names(db)
    checked = Counter()
    for player in _players(db):
        all_facts = facts_for(db, player)
        for spot in ("", "vpip", "position=BB"):
            pred = parse_filter(spot, names) if spot else None
            facts = [f for f in all_facts if pred is None or pred(f)]
            out = results_breakdown(facts)
            for table in out["tables"]:
                for r in _rows(table):
                    if r["filter"] is None:
                        # No term says it yet (in position): the ids carry it.
                        assert table["key"] == "ip" and r["key"] in ("ip", "oop", "unknown")
                        assert len(r["hand_ids"]) == r["hands"]
                        continue
                    combined = ",".join(t for t in (spot, r["filter"]) if t)
                    p = parse_filter(combined, names)
                    assert sum(1 for f in all_facts if p(f)) == r["hands"], (player, spot, r)
                    checked[table["key"]] += 1
    assert checked["pot"] and checked["position"] and checked["ip"]


def test_in_position_rows_are_the_hands_that_saw_a_flop(db):
    facts = facts_for(db, "HSJ")
    rows = {r["key"]: r for r in results_breakdown(facts)["tables"][2]["rows"]}
    assert sorted(rows["ip"]["hand_ids"]) == sorted(f.hand_id for f in facts if f.saw_flop and f.in_position)
    assert sorted(rows["oop"]["hand_ids"]) == sorted(
        f.hand_id for f in facts if f.saw_flop and f.in_position is False
    )
    assert rows["no_flop"]["saw_flop_pct"] in (0, None)
    assert rows["ip"]["saw_flop_pct"] == 100


def test_walks_are_the_big_blind_folded_to(db):
    """A walk is a hand nobody raised in which this player never had a decision:
    on these logs, always the big blind, always winning the small blind."""
    for player in _players(db):
        out = results_breakdown(facts_for(db, player))
        walk = out["tables"][0]["rows"][0]
        assert walk["key"] == "walk" and "roles" not in walk
        if walk["hands"]:
            # No preflop decision, so no VPIP: "–", as on the stat rows.
            assert walk["vpip_pct"] is None and walk["vpip_hands"] == 0
            assert walk["saw_flop_pct"] == 0
            assert walk["net_bb"] > 0


def test_vpip_is_the_stat_rows_vpip(db):
    """One label, one number: Results' VPIP is the stats page's and the HUD's."""
    for row in report(db):
        total = results_breakdown(facts_for(db, row["player"]))["total"]
        assert total["vpip_pct"] == row["vpip"], row["player"]


def _fact(hand_id: int, net: int, *, complete: bool = True, **kw) -> Facts:
    return Facts(
        hand_id=hand_id, pn_id="p", n_dealt_in=2, seats_from_button=0, dead_button=False,
        net=net, bb_size=10, complete=complete, vpip_opp=True, **kw,
    )


def test_incomplete_hands_count_as_hands_and_in_no_bb_figure():
    facts = [
        _fact(1, 25, pot_level=2, pfa=True),
        _fact(2, -400, complete=False, pot_level=2, pfa=True),  # a log that stopped mid-hand
        _fact(3, -10, pot_level=2),
    ]
    out = results_breakdown(facts)
    assert (out["total"]["hands"], out["total"]["bb_hands"], out["total"]["net_bb"]) == (3, 2, 1.5)
    srp = out["tables"][0]["rows"][2]
    pfa, other = srp["roles"]
    assert (pfa["hands"], pfa["bb_hands"], pfa["net_bb"], pfa["bb_per_100"]) == (2, 1, 2.5, 250.0)
    assert (other["hands"], other["bb_hands"], other["net_bb"]) == (1, 1, -1.0)
    # A row of incomplete hands alone has hands and no money at all: unknown, not zero.
    only = results_breakdown([_fact(9, -400, complete=False, pot_level=3)])["tables"][0]["rows"][3]
    assert (only["hands"], only["bb_hands"], only["net_bb"], only["bb_per_100"]) == (1, 0, None, None)


def test_incomplete_hands_on_a_truncated_log(tmp_path):
    conn = connect(tmp_path / "t.sqlite")
    import_csv(conn, TRUNCATED)
    seen = 0
    for player in _players(conn):
        facts = facts_for(conn, player)
        broken = [f for f in facts if not f.complete]
        out = results_breakdown(facts)
        assert out["total"]["bb_hands"] == len(facts) - len(broken)
        assert out["total"]["net_bb"] == round(float(_bb([f for f in facts if f.complete])), 2)
        seen += len(broken)
    assert seen, "the truncated log stops mid-hand"


#: HSJ, worked from the three core logs. Their worst pot is single-raised; the
#: caller side of it loses faster still, but on fewer hands than the minimum.
HSJ_POTS = {
    "walk": (6, 2.4, 40.0),
    "limped": (67, -99.8, -148.96),
    "srp": (129, -760.4, -589.46),
    "srp:pfa": (70, -204.8, -292.57),
    "srp:other": (59, -555.6, -941.69),
    "3bet": (28, -36.6, -130.71),
    "3bet:pfa": (9, -186.4, -2071.11),
    "3bet:other": (19, 149.8, 788.42),
    "4bet": (1, 0.0, 0.0),
    "4bet:pfa": (1, 0.0, 0.0),
    "4bet:other": (0, None, None),
}


def test_one_player_pinned(db):
    out = results_breakdown(facts_for(db, "HSJ"))
    assert (out["total"]["hands"], out["total"]["net_bb"], out["total"]["bb_per_100"]) == (231, -894.4, -387.19)
    pots = {r["key"]: r for r in _rows(out["tables"][0])}
    assert {k: (r["hands"], r["net_bb"], r["bb_per_100"]) for k, r in pots.items()} == HSJ_POTS
    assert [k for k, r in pots.items() if r["worst"]] == ["srp"]
    assert pots["srp:other"]["bb_hands"] < WORST_MIN_HANDS
    seats = {r["key"]: (r["hands"], r["net_bb"]) for r in out["tables"][1]["rows"]}
    assert seats == {"BTN": (5, -6.0), "BTN/SB": (107, -108.6), "SB": (8, -234.0), "BB": (111, -545.8)}
    assert [r["key"] for r in out["tables"][1]["rows"] if r["worst"]] == ["BB"]
    ip = {r["key"]: (r["hands"], r["net_bb"]) for r in out["tables"][2]["rows"]}
    assert ip == {"ip": (73, -105.8), "oop": (89, -792.0), "no_flop": (69, 3.4)}
    assert out["tables"][2]["rows"][-1]["rank"] is False, "no flop is never up for worst"


def test_no_flop_is_never_the_worst():
    """A losing no-flop row on plenty of hands is passed over for a smaller loss."""
    rows = [
        {"key": "ip", "bb_hands": 200, "bb_per_100": -5.0, "worst": False},
        {"key": "no_flop", "bb_hands": 900, "bb_per_100": -40.0, "worst": False, "rank": False},
    ]
    _mark_worst(rows, WORST_MIN_HANDS)
    assert [r["key"] for r in rows if r["worst"]] == ["ip"]


def test_nothing_is_worst_when_nothing_is_losing(db):
    out = results_breakdown(facts_for(db, "genericpoker"))
    big = [r for t in out["tables"] for r in _rows(t) if r["bb_hands"] >= WORST_MIN_HANDS]
    assert big and all(r["bb_per_100"] >= 0 for r in big)
    assert not any(r["worst"] for t in out["tables"] for r in _rows(t))


def test_results_endpoint(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import importlib

    monkeypatch.setenv("PNT_DB", str(tmp_path / "api.sqlite"))
    from pnt.server import app as app_module

    importlib.reload(app_module)
    conn = connect(tmp_path / "api.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    conn.close()
    client = local_client(app_module.app)

    body = client.get("/players/HSJ/results").json()
    assert body["player"] == "HSJ" and body["filter"] is None
    assert body["hands"] == 231 and body["min_hands"] == WORST_MIN_HANDS
    assert [t["key"] for t in body["tables"]] == ["pot", "position", "ip"]
    spot = client.get("/players/HSJ/results", params={"filter": "position=BB"}).json()
    assert spot["filter"] == "position=BB" and spot["hands"] == 111
    assert [r["key"] for r in spot["tables"][1]["rows"]] == ["BB"]
    assert client.get("/players/HSJ/results", params={"filter": "nonsense"}).status_code == 400
    assert client.get("/players/ghost/results").status_code == 404


def test_cli_prints_the_tables_and_json(tmp_path):
    path = tmp_path / "r.sqlite"
    conn = connect(path)
    for p in ALL_LOGS:
        import_csv(conn, p)
    conn.close()
    result = CliRunner().invoke(cli.app, ["results", "HSJ", "--db", str(path)])
    assert result.exit_code == 0, result.output
    assert "By pot type" in result.output and "3bet_pot,!pfa" in result.output and "<- worst" in result.output
    result = CliRunner().invoke(cli.app, ["results", "HSJ", "--json", "--filter", "srp", "--db", str(path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["hands"] == 129
    result = CliRunner().invoke(cli.app, ["results", "nobody", "--db", str(path)])
    assert result.exit_code != 0 and "unknown alias" in result.output


def test_the_chart_has_the_view():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "pnt" / "server" / "static" / "chart.html").read_text("utf-8")
    assert 'data-view="results"' in html and '"results"];' in html
    assert "results: " in html and "function renderResults(" in html
    assert "/results?" in html
