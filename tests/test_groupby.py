"""Group by: every cell reproduces from its own term. See SPEC.md, "Group by"."""

from __future__ import annotations

import pytest

from pnt.stats.filters import parse_filter
from pnt.stats.groupby import DIMENSIONS, MADE_BUCKETS, catalog, check_dims, group
from pnt.stats.queries import aggregate, display_names, facts_by_player, facts_for
from pnt.stats.results import SEAT_ORDER
from tests.conftest import ALL_LOGS, EDGE_LOGS, local_client

#: Every dimension a term can reproduce; `player` names its cell instead.
TERMED = [d for d in DIMENSIONS if d != "player"]
#: The ones a hand can sit in several buckets of.
MULTI = {"vs"}
BASES = ("", "vpip", "srp,saw_flop")


@pytest.fixture(scope="module")
def pool(tmp_path_factory):
    """Every fixture log, as the pool: (facts, whose, names, conn)."""
    from pnt.db.conn import connect
    from pnt.ingest.importer import import_csv

    conn = connect(tmp_path_factory.mktemp("groupby") / "g.sqlite")
    for path in ALL_LOGS + EDGE_LOGS:
        import_csv(conn, path)
    grouped, aliases, _ = facts_by_player(conn)
    whose = {f.pn_id: aliases[pid] for pid, rows in grouped.items() for f in rows}
    facts = [f for rows in grouped.values() for f in rows]
    yield facts, whose, display_names(conn), conn
    conn.close()


def _grouped(pool, dims, base="", **kw):
    facts, whose, names, _ = pool
    pred = parse_filter(base, names) if base else None
    spot = [f for f in facts if pred is None or pred(f)]
    return spot, group(spot, dims, names=names, player_of=lambda f: whose[f.pn_id], **kw)


def _check(cell: dict, got: list) -> None:
    mine = aggregate(got)
    assert cell["hands"] == len(got), cell["term"]
    assert cell["shown"] == sum(1 for f in got if f.hole_cards), cell["term"]
    assert cell["net_bb"] == mine.get("net_bb"), cell["term"]
    assert cell["bb_per_100_vs_fold"] == mine.get("bb_per_100_vs_fold"), cell["term"]


@pytest.mark.parametrize("dim", TERMED)
@pytest.mark.parametrize("base", BASES)
def test_every_cell_reproduces_from_its_term(pool, dim, base):
    facts, _, names, _ = pool
    spot, out = _grouped(pool, [dim], base)
    assert out["cells"], f"{dim} has cells on the fixtures"
    for cell in out["cells"]:
        assert cell["term"], cell["keys"]
        pred = parse_filter(",".join(t for t in (base, cell["term"]) if t), names)
        _check(cell, [f for f in facts if pred(f)])
    if dim not in MULTI:
        assert sum(c["hands"] for c in out["cells"]) == out["total"]["hands"] - out["skipped"]
    assert out["total"]["hands"] == len(spot)


def test_vs_counts_a_hand_once_per_opponent(pool):
    spot, out = _grouped(pool, ["vs"])
    assert sum(c["hands"] for c in out["cells"]) > out["total"]["hands"] - out["skipped"]
    assert out["skipped"] == sum(1 for f in spot if not f.opponents)
    # HSJ and hsj are two players, but `vs=` compares casefolded: one bucket.
    labels = [c["keys"][0].casefold() for c in out["cells"]]
    assert len(labels) == len(set(labels))


def test_one_players_vs_cells_reproduce(pool):
    _, _, names, conn = pool
    mine = facts_for(conn, "genericpoker")
    out = group(mine, ["vs"], names=names)
    assert out["cells"]
    for cell in out["cells"]:
        pred = parse_filter(cell["term"], names)
        _check(cell, [f for f in mine if pred(f)])


def test_player_cells_name_their_player(pool):
    spot, out = _grouped(pool, ["player"])
    _, whose, _, _ = pool
    assert out["skipped"] == 0
    assert sum(c["hands"] for c in out["cells"]) == len(spot)
    for cell in out["cells"]:
        assert cell["term"] == "" and cell["player"] == cell["keys"][0]
        _check(cell, [f for f in spot if whose[f.pn_id] == cell["player"]])
    hands = [c["hands"] for c in out["cells"]]
    assert hands == sorted(hands, reverse=True), "no order: most hands first"


@pytest.mark.parametrize("dims", [("position", "pot_type"), ("pot_type", "depth"), ("flop_texture", "made_flop"),
                                  ("player", "position"), ("vs", "pot_type")])
def test_two_dimensions_cross(pool, dims):
    facts, whose, names, _ = pool
    spot, out = _grouped(pool, list(dims))
    assert out["cells"] and all(len(c["keys"]) == 2 for c in out["cells"])
    for cell in out["cells"]:
        pred = parse_filter(cell["term"], names) if cell["term"] else (lambda f: True)
        got = [f for f in facts if pred(f)]
        if "player" in dims:
            got = [f for f in got if whose[f.pn_id] == cell["player"]]
        _check(cell, got)
    if "vs" not in dims:
        assert sum(c["hands"] for c in out["cells"]) == len(spot) - out["skipped"]


def test_rows_follow_the_dimension_order(pool):
    _, out = _grouped(pool, ["position"])
    seats = [c["keys"][0] for c in out["cells"]]
    assert seats == sorted(seats, key=SEAT_ORDER.index)
    _, out = _grouped(pool, ["made_flop"])
    order = list(MADE_BUCKETS)
    made = [c["keys"][0] for c in out["cells"]]
    assert made == sorted(made, key=order.index)
    assert out["by"][0]["order"] == order


def test_a_minimum_hides_cells_and_counts_them(pool):
    _, everything = _grouped(pool, ["line_flop"])
    _, some = _grouped(pool, ["line_flop"], min_hands=20)
    kept = [c for c in everything["cells"] if c["hands"] >= 20]
    assert [c["keys"] for c in some["cells"]] == [c["keys"] for c in kept]
    assert some["hidden"]["cells"] == len(everything["cells"]) - len(kept)
    assert some["hidden"]["hands"] == sum(c["hands"] for c in everything["cells"]) - sum(c["hands"] for c in kept)


def test_a_cell_carries_its_rates_and_mix(pool):
    _, out = _grouped(pool, ["pot_type"], street="turn")
    cell = next(c for c in out["cells"] if c["keys"] == ["srp"])
    assert set(cell["rates"]) == {"vpip", "pfr", "3bet", "cbet_turn", "fold_to_cbet_turn", "af_turn",
                                  "wtsd", "wsd", "_opp"}
    assert set(cell["rates"]["_opp"]) == set(cell["rates"]) - {"_opp"}
    mix = cell["mix"]
    assert mix["street"] == "turn" and mix["known"] == sum(g["n"] for g in mix["groups"]) > 0
    assert mix["known"] <= cell["shown"]
    assert abs(sum(g["pct"] for g in mix["groups"]) - 100) < 0.5


def test_bad_dimensions_are_refused():
    for dims in ([], ["position", "pot_type", "depth"], ["position", "position"], ["nope"]):
        with pytest.raises(ValueError):
            check_dims(dims)
    with pytest.raises(ValueError, match="pool"):
        check_dims(["player"], pool=False)
    with pytest.raises(ValueError, match="street"):
        group([], ["position"], street="preflop")
    assert "player" in {d["name"] for d in catalog()} - {d["name"] for d in catalog(pool=False)}


# --------------------------------------------------------------- the route ---


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PNT_DB", str(tmp_path / "api.sqlite"))
    import importlib

    from pnt.server import app as app_module

    importlib.reload(app_module)

    from pnt.db.conn import connect
    from pnt.ingest.importer import import_csv

    conn = connect(tmp_path / "api.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    conn.close()
    return local_client(app_module.app)


def test_the_route_serves_pool_and_player(client):
    pool = client.get("/groupby", params={"by": "position,pot_type", "filter": "vpip", "min": 2})
    assert pool.status_code == 200, pool.text
    body = pool.json()
    assert body["player"] is None and {"name": "player", "label": "Player"} in body["dims"]
    assert [d["name"] for d in body["by"]] == ["position", "pot_type"]
    assert body["cells"] and all(c["hands"] >= 2 for c in body["cells"])
    stats = client.get("/stats", params={"filter": "vpip"}).json()
    assert body["total"]["hands"] == sum(r["hands"] for r in stats)

    one = client.get("/groupby", params={"by": "vs", "player": "genericpoker"}).json()
    assert one["player"] == "genericpoker" and "player" not in {d["name"] for d in one["dims"]}
    chart = client.get("/players/genericpoker/stats").json()
    assert one["total"]["hands"] == chart["hands"]

    by_player = client.get("/groupby", params={"by": "player"}).json()
    assert {c["player"] for c in by_player["cells"]} == {r["player"] for r in client.get("/stats").json()}


def test_the_route_refuses_what_it_cannot_group(client):
    assert client.get("/groupby", params={"by": "nope"}).status_code == 400
    assert client.get("/groupby", params={"by": "player", "player": "genericpoker"}).status_code == 400
    assert client.get("/groupby", params={"by": "position", "filter": "nonsense"}).status_code == 400
    assert client.get("/groupby", params={"by": "position", "player": "nobody"}).status_code == 404
    assert client.get("/groupby", params={"by": "position", "street": "preflop"}).status_code == 422
