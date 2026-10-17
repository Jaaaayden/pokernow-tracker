"""Holdings by street: the shown matrix adds up to the shown hands that reached the
river, the owner's funnel paths add up to their flops, and every cell and path
reproduces from its filter. See SPEC.md, "Holdings by street"."""

from __future__ import annotations

import pytest

from pnt.db.conn import connect
from pnt.ingest.importer import import_csv
from pnt.stats.cards import MADE_LEAVES
from pnt.stats.filters import parse_filter
from pnt.stats.pool import owners
from pnt.stats.queries import facts_for, identity_map, report
from pnt.stats.streets import GROUP_OF, GROUPS, PATHS, made_by_street
from tests.conftest import ALL_LOGS, EDGE_LOGS, local_client


def _players(conn):
    return [r["player"] for r in report(conn)]


def _count(facts, filt: str) -> int:
    pred = parse_filter(filt)
    return sum(1 for f in facts if pred(f))


def _owners_aliases(conn) -> set[str]:
    ident = identity_map(conn)
    return {ident[pn][1] for _, pn in owners(conn) if pn in ident}


def test_groups_cover_every_leaf_once():
    assert sorted(GROUP_OF) == sorted(MADE_LEAVES)
    assert sum(len(leaves) for *_, leaves in GROUPS) == len(MADE_LEAVES)


def _check_matrix(m: dict, facts, rows: str) -> None:
    assert sum(r["hands"] for r in m["rows"]) == m["hands"] == sum(t["hands"] for t in m["totals"])
    for r in m["rows"]:
        assert sum(c["hands"] for c in r["cells"]) == r["hands"]
        assert _count(facts, r["filter"]) == r["hands"], r["filter"]
        for c in r["cells"]:
            assert _count(facts, c["filter"]) == c["hands"], c["filter"]
            assert c["filter"].startswith(f"made_{rows}")
    for t in m["totals"]:
        assert _count(facts, t["filter"]) == t["hands"], t["filter"]


@pytest.mark.parametrize("spot", ["", "srp", "position=BB"])
def test_shown_matrix_adds_up_and_reproduces(db, spot):
    shown = 0
    for player in _players(db):
        pred = parse_filter(spot) if spot else None
        facts = [f for f in facts_for(db, player) if pred is None or pred(f)]
        m = made_by_street(facts, owners(db))["shown_matrix"]
        # Every hand with cards known that reached the river, and nothing else.
        assert m["hands"] == _count(facts, "made_river>=high_card")
        _check_matrix(m, facts, "flop")
        for r in m["rows"]:
            assert _count(facts, r["saw_flop_filter"]) == r["saw_flop"] >= r["hands"]
        assert sum(r["saw_flop"] for r in m["rows"]) == m["saw_flop"]
        shown += m["hands"]
    assert shown


def test_funnel_paths_sum_to_the_owners_flops(db):
    own = owners(db)
    heroes = _owners_aliases(db)
    assert heroes
    for player in _players(db):
        facts = facts_for(db, player)
        out = made_by_street(facts, own)
        mine = [f for f in facts if (f.game_id, f.pn_id) in own]
        if player not in heroes:
            assert out["hero_funnel"] is None
            continue
        hf = out["hero_funnel"]
        # Every card of their own logs is known, so every flop they saw has a group.
        assert hf["flops"] == sum(1 for f in mine if f.saw_flop) > 0
        assert sum(r["hands"] for r in hf["rows"]) == hf["flops"]
        for r in hf["rows"]:
            assert [p["key"] for p in r["paths"]] == [k for k, *_ in PATHS] + ["other"]
            assert sum(p["hands"] for p in r["paths"]) == r["hands"]
            assert _count(mine, r["filter"]) == r["hands"]
            for p in r["paths"]:
                if p["filter"] is not None:
                    assert _count(mine, p["filter"]) == p["hands"], p["filter"]
        _check_matrix(hf["turn_river"], mine, "turn")
        assert hf["turn_river"]["hands"] == _count(mine, "made_river>=high_card")


def test_funnel_paths_are_exclusive_on_the_edge_logs(tmp_path):
    """Each hand ends exactly one way: no path's filter shares a hand with another's,
    and only a cut-short log falls through to `other`."""
    conn = connect(tmp_path / "e.sqlite")
    for path in [*ALL_LOGS, *EDGE_LOGS]:
        import_csv(conn, path)
    own = owners(conn)
    for player in _owners_aliases(conn):
        mine = [f for f in facts_for(conn, player) if (f.game_id, f.pn_id) in own and f.saw_flop]
        preds = [parse_filter(terms) for _, _, terms in PATHS]
        for f in mine:
            hits = sum(p(f) for p in preds)
            assert hits <= 1, f.hand_id
            if hits == 0:
                assert not f.complete, f.hand_id
    conn.close()


def test_streets_endpoint(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import importlib

    monkeypatch.setenv("PNT_DB", str(tmp_path / "api.sqlite"))
    from pnt.server import app as app_module

    importlib.reload(app_module)
    conn = connect(tmp_path / "api.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    hero = min(_owners_aliases(conn))
    other = next(p for p in _players(conn) if p not in _owners_aliases(conn))
    conn.close()
    client = local_client(app_module.app)

    body = client.get(f"/players/{hero}/streets").json()
    assert set(body) >= {"player", "filter", "shown_matrix", "hero_funnel", "groups", "notes"}
    assert [g["key"] for g in body["groups"]] == [k for k, *_ in GROUPS]
    assert body["hero_funnel"]["flops"] > 0
    assert client.get(f"/players/{other}/streets").json()["hero_funnel"] is None
    spot = client.get(f"/players/{hero}/streets", params={"filter": "srp"}).json()
    assert spot["shown_matrix"]["hands"] <= body["shown_matrix"]["hands"]
    assert client.get(f"/players/{hero}/streets", params={"filter": "nonsense"}).status_code == 400
    assert client.get("/players/ghost/streets").status_code == 404
