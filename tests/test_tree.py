"""The spot tree: every node's filter finds exactly its hands, children never hold
more than their parent passed on, and the roots are seats. See SPEC.md, "Spot tree"."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from pnt.db.conn import connect
from pnt.ingest.importer import import_csv
from pnt.stats.derive import derive
from pnt.stats.filters import parse_filter
from pnt.stats.live import hand_row
from pnt.stats.nodes import nodes_for
from pnt.stats.queries import display_names, facts_for, identities_of, load_hands, report
from pnt.stats.tree import _rank, build_tree, decision_terms, seat_of, spot_term, tree_for
from tests.conftest import ALL_LOGS, EDGE_LOGS, local_client


def _players(conn):
    return [r["player"] for r in report(conn)]


def _nodes(tree: dict) -> Iterator[dict]:
    stack = list(tree["roots"])
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n["children"])


def _tree(conn, player, spot="", **kw):
    """The tree over one player's hands in a spot, and those hands' Facts."""
    pred = parse_filter(spot, display_names(conn)) if spot else None
    facts = [f for f in facts_for(conn, player) if pred is None or pred(f)]
    hands = load_hands(conn, None, identities_of(conn, player))
    return build_tree(hands, facts, player, **kw), facts


def _count(facts, filt: str) -> int:
    pred = parse_filter(filt)
    return sum(1 for f in facts if pred(f))


def _assert_exact(tree: dict, facts) -> int:
    checked = 0
    for n in _nodes(tree):
        assert _count(facts, n["filter"]) == n["hands"], (n["id"], n["filter"])
        for d in n["decisions"]:
            assert _count(facts, d["filter"]) == d["hands"], (n["id"], d["filter"])
        checked += 1
    return checked


@pytest.mark.parametrize("texture", [False, True])
def test_node_filters_find_exactly_their_hands(db, texture):
    checked = 0
    for player in _players(db):
        tree, facts = _tree(db, player, texture=texture)
        checked += _assert_exact(tree, facts)
    assert checked > 200


@pytest.mark.parametrize("spot", ["srp", "position=BB", "saw_flop,vs=HSJ"])
def test_node_filters_are_exact_inside_a_spot(db, spot):
    """A node's filter, added to the spot's, finds exactly its hands."""
    for player in _players(db):
        tree, facts = _tree(db, player, spot)
        _assert_exact(tree, facts)
        assert tree["hands"] == len(facts)


def test_node_filters_are_exact_on_the_edge_logs(tmp_path):
    conn = connect(tmp_path / "e.sqlite")
    for path in EDGE_LOGS:
        import_csv(conn, path)
    for player in _players(conn):
        tree, facts = _tree(conn, player)
        _assert_exact(tree, facts)
    conn.close()


def test_children_hold_at_most_what_their_parent_passed_on(db):
    """Children partition the hands that went on; a full-depth tree loses none."""
    for player in _players(db):
        full, _ = _tree(db, player, max_depth=20)
        for n in _nodes(full):
            assert sum(c["hands"] for c in n["children"]) + n["ended"] == n["hands"], n["id"]
            # Each decision's hands are its children's plus the ones that ended on it.
            for d in n["decisions"]:
                assert sum(c["hands"] for c in n["children"] if c["via"] == d["decision"]) <= d["hands"]
        cut, _ = _tree(db, player, max_depth=2, min_hands=3)
        for n in _nodes(cut):
            assert n["hands"] >= 3
            assert sum(c["hands"] for c in n["children"]) + n["ended"] <= n["hands"], n["id"]
        # Two decisions below the seat, and nothing under the second.
        assert all(not g["children"] for r in cut["roots"] for c in r["children"] for g in c["children"])


def test_roots_are_seats(db):
    for player in _players(db):
        tree, facts = _tree(db, player)
        ids = [r["id"] for r in tree["roots"]]
        assert len(ids) == len(set(ids))
        for r in tree["roots"]:
            assert r["filter"] == f"position={r['id']}"
            assert r["hands"] == sum(1 for f in facts if seat_of(f) == r["id"])
            assert r["decisions"] == [] and r["kind"] is None
            # Below a seat, the first decision is always preflop.
            assert all(c["street"] == "preflop" for c in r["children"])
        assert sum(r["hands"] for r in tree["roots"]) + tree["unplaced"] == tree["hands"] == len(facts)


def test_a_limper_is_ruled_out_of_facing_the_open_first(db):
    """`position=X,faced_open` alone would also find the limper who then faced a
    raise; the node of facing it first says it was not unopened to them."""
    found = 0
    for player in _players(db):
        tree, _ = _tree(db, player)
        for r in tree["roots"]:
            for c in r["children"]:
                if c["id"] == f"{r['id']}/pf2":
                    limped = any(
                        g["id"] == f"{r['id']}/pf1/call.pf2" for k in r["children"] for g in k["children"]
                    )
                    if limped:
                        assert "!unopened" in c["filter"].split(","), c["filter"]
                        found += 1
    assert found


def test_tracked_keys_come_in_a_fixed_order(parsed_every):
    """The negations rest on it: a key's place in a path is the same in every hand."""
    checked = 0
    for result in parsed_every.values():
        for parsed in result.hands:
            for path in nodes_for(hand_row(parsed)).values():
                keys = [n.key for n in path if n.key is not None]
                assert keys == sorted(keys, key=_rank), keys
                checked += len(keys)
    assert checked > 1000


def test_decision_terms_imply_the_spot(parsed_every):
    """A decision's terms find only hands that reached its spot."""
    keys = {("preflop", lvl) for lvl in range(1, 6)}
    for result in parsed_every.values():
        for parsed in result.hands:
            row = hand_row(parsed)
            for path in nodes_for(row).values():
                keys.update(n.key for n in path if n.key is not None)
    facts = [f for result in parsed_every.values() for p in result.hands for f in derive(hand_row(p))]
    for key in keys:
        spot = parse_filter(spot_term(key))
        for d in ("fold", "check", "call", "raise", "bet"):
            try:
                pred = parse_filter(",".join(decision_terms(key, d)))
            except (KeyError, ValueError):
                continue
            assert all(spot(f) for f in facts if pred(f)), (key, d)


def test_tree_for_matches_build_tree_and_caches(db):
    a = tree_for(db, "Chris")
    b, _ = _tree(db, "Chris")
    assert a["roots"] == b["roots"]
    assert tree_for(db, "Chris") is a
    with pytest.raises(ValueError):
        tree_for(db, "ghost")


def test_tree_endpoint(tmp_path, monkeypatch):
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

    body = client.get("/players/HSJ/tree").json()
    assert body["player"] == "HSJ" and body["filter"] is None and body["roots"]
    assert body["depth"] == 6 and body["texture"] is False
    root = body["roots"][0]
    assert {"id", "label", "edge", "hands", "decisions", "filter", "children"} <= set(root)
    spot = client.get("/players/HSJ/tree", params={"filter": "srp", "depth": 2, "texture": True}).json()
    assert spot["filter"] == "srp" and spot["hands"] <= body["hands"]
    assert client.get("/players/HSJ/tree", params={"filter": "nonsense"}).status_code == 400
    assert client.get("/players/HSJ/tree", params={"depth": 0}).status_code == 422
    assert client.get("/players/ghost/tree").status_code == 404
