"""Multiway vs heads-up: the stats page's "Split by" and the node step that
widens over players on the flop. See SPEC.md, "Multiway vs heads-up"."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pnt.stats.derive import Facts, derive
from pnt.stats.filters import parse_filter
from pnt.stats.live import hand_row
from pnt.stats.nodes import Node, candidates, nodes_for, players_term, resolve
from pnt.stats.queries import load_hands, report
from tests.conftest import MULTIWAY_GAME

STATS_HTML = Path(__file__).parent.parent / "pnt/server/static/stats.html"
SPLIT = ("players_flop=2", "players_flop>=3")


def _splits() -> dict[str, list[tuple[str, str]] | None]:
    """stats.html's SPLITS, read off the page: key -> [(label, term)] or None."""
    page = STATS_HTML.read_text(encoding="utf-8")
    start = page.index("const SPLITS = {")
    block = page[start:page.index("};", start)]
    out: dict[str, list[tuple[str, str]] | None] = {}
    for key, body in re.findall(r"^\s*(\w+): (null|\[.*\]),?$", block, re.MULTILINE):
        out[key] = None if body == "null" else re.findall(r'\["([^"]+)", "([^"]+)"\]', body)
    return out


def test_every_split_term_is_a_working_filter():
    """A split whose term the server rejects would only ever show an error."""
    splits = _splits()
    assert splits["none"] is None
    assert splits["multiway"] == [("HU", "players_flop=2"), ("3+ way", "players_flop>=3")]
    for groups in splits.values():
        for _, term in groups or []:
            parse_filter(term)
            parse_filter(f"srp,{term}")  # ANDed with a spot, as the page sends it


def _by_player(rows: list[dict]) -> dict[str, dict]:
    return {r["player"]: r for r in rows}


@pytest.mark.parametrize("spot", [None, "srp", "position=BTN"])
def test_heads_up_and_multiway_sum_to_the_flops_seen(db, spot):
    """The two groups partition the flop hands, so every postflop count of a
    player's sub-rows sums to their whole row's."""
    def rows(term: str | None) -> dict[str, dict]:
        filt = ",".join(t for t in (spot, term) if t)
        return _by_player(report(db, predicate=parse_filter(filt) if filt else None))

    whole, flop_dealt = rows(None), rows("players_flop>=1")
    parts = [rows(t) for t in SPLIT]
    assert any(parts[1].values()), "the corpus has multiway flops"
    keys = ("wtsd", "cbet_flop", "fold_to_cbet_flop", "donk_flop", "cbet_turn", "fold_to_cbet_river")
    for name, row in whole.items():
        got = [p.get(name, {"hands": 0, "_opp": {}}) for p in parts]
        for key in keys:
            assert sum(g["_opp"].get(key, 0) for g in got) == row["_opp"][key], (name, key)
        # hands is per hand, not per flop seen: the two sum to the hands a flop was dealt on.
        assert sum(g["hands"] for g in got) == flop_dealt.get(name, {"hands": 0})["hands"], name


def test_a_split_row_reproduces_from_its_filter(db):
    """A sub-row is `<spot>,<term>`: the same filter on one player's facts gives its n."""
    from pnt.stats.queries import facts_for

    for row in report(db, predicate=parse_filter("srp,players_flop>=3"), min_hands=5):
        pred = parse_filter("srp,players_flop>=3")
        assert sum(1 for f in facts_for(db, row["player"]) if pred(f)) == row["hands"]


# --- the node step -----------------------------------------------------------


def test_players_term_groups_the_count():
    assert players_term(None) is None
    assert players_term(2) == "players_flop=2"
    assert players_term(3) == players_term(6) == "players_flop>=3"


def test_multiway_nodes_carry_their_group(db):
    """Every postflop node names its hand's group, and the term holds on the
    finished hand's facts (test_nodes' invariant covers every term in general)."""
    seen = set()
    for hand in load_hands(db, MULTIWAY_GAME):
        facts = {f.pn_id: f for f in derive(hand)}
        for pid, path in nodes_for(hand).items():
            for node in path:
                if node.street == "preflop":
                    assert node.players is None
                    continue
                assert node.players_flop == facts[pid].players_flop
                if node.filter:
                    assert node.players in node.filter.split(",")
                    seen.add(node.players)
    assert seen == set(SPLIT)


def _node(**kw) -> Node:
    base = {"kind": "faced_cbet", "street": "flop", "level": None, "label": "", "position": "position=BTN",
            "pf_history": ["called_open"], "pot": "srp", "post_history": [], "situation": "faced_cbet_flop=small",
            "players_flop": 3}
    return Node(**{**base, **kw})


def test_any_number_of_players_comes_after_any_size():
    ladder = candidates(_node())
    assert [c[0] for c in ladder[:4]] == [
        "position=BTN,called_open,srp,players_flop>=3,faced_cbet_flop=small",
        "position=BTN,called_open,srp,players_flop>=3,faced_cbet_flop",
        "position=BTN,called_open,srp,faced_cbet_flop",
        "called_open,srp,faced_cbet_flop",
    ]
    assert ladder[2][1] == ["any size", "any number of players"]
    # No size to drop: the players step is the first widening.
    unsized = candidates(_node(kind="cbet", situation="cbet_flop_opp", pf_history=["opener"]))
    assert unsized[1] == ("position=BTN,opener,srp,cbet_flop_opp", ["any number of players"], ("cbet", "flop"))
    # Preflop nodes never carry it.
    pre = Node(kind="preflop", street="preflop", level=2, label="", position=None, situation="faced_open")
    assert all("players_flop" not in c[0] for c in candidates(pre))


def _fact(players_flop: int, **kw) -> Facts:
    return Facts(hand_id=0, pn_id="x", n_dealt_in=6, dead_button=False, seats_from_button=0,
                 pf_faced={2: "call"}, pot_level=2, saw_flop=True, players_flop=players_flop,
                 fold_to_cbet_opp={"flop": True}, faced_cbet_size={"flop": "small"}, **kw)


def test_resolve_widens_over_players_when_the_group_is_thin():
    """Heads-up history only: a multiway c-bet faced is answered from it, marked."""
    facts = [_fact(2, fold_to_cbet={"flop": True}), _fact(2)]
    assert parse_filter("position=BTN,called_open,srp,players_flop=2,faced_cbet_flop=small")(facts[0])
    got = resolve(facts, [_node()])
    assert got["filter"] == "position=BTN,called_open,srp,faced_cbet_flop"
    assert got["relaxed"] == ["any size", "any number of players"] and not got["exact"]
    assert got["hands"] == 2 and got["decisions"] == {"fold": 1, "call": 1, "raise": 0}
    # With a multiway hand behind it, the exact spot answers.
    exact = resolve([*facts, _fact(3)], [_node()])
    assert exact["exact"] and exact["hands"] == 1


def test_the_live_hand_counts_players_as_derive_will(parsed_all):
    """A hand cut on the flop gives the pending node the count the finished hand gets."""
    from dataclasses import replace

    checked = 0
    for parsed in parsed_all[MULTIWAY_GAME].hands:
        hand = hand_row(parsed)
        if not parsed.complete or len(hand.board) < 3:
            continue
        flop_at = next((i for i, a in enumerate(hand.actions) if a.street == "flop"), None)
        if flop_at is None:
            continue
        cut = replace(hand, actions=hand.actions[:flop_at], board=hand.board[:3], complete=False)
        who = hand.actions[flop_at].pn_id
        pending = nodes_for(cut, pending_for=who)[who][-1]
        final = {f.pn_id: f for f in derive(hand)}[who]
        assert pending.players_flop == final.players_flop, parsed.hand_number
        checked += 1
    assert checked > 10
