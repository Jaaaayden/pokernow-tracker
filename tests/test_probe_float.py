"""Delayed c-bet, probe and float on the pages and in the node table.

The facts and filter terms are Foundation's (`test_foundation2.py`); this file
holds the stats-page columns, the chart's Lines chips, and the decision points
that let the HUD answer these spots from the hands behind them. Pinned hands:

  HU #18   Chris calls gp's flop c-bet; gp checks the turn and Chris bets: a float.
  HU #63   Chris probes the river into gp, who checked the turn back; gp calls.
  HU #65   The same river probe, and gp folds to it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from pnt.stats.derive import Facts, derive
from pnt.stats.filters import parse_filter
from pnt.stats.live import hand_row
from pnt.stats.nodes import Node, candidates, decision_of, decisions, nodes_for, resolve
from pnt.stats.queries import load_hands, report
from tests.conftest import HU_GAME

CHRIS = "5NARaPRkSp"
GP = "gpP9uUffpu"
STATIC = Path(__file__).resolve().parents[1] / "pnt" / "server" / "static"
NEW_KINDS = ("delayed_cbet", "probe", "faced_probe", "float")
#: The stats page's new columns, and the streets each family exists on.
FAMILIES = {
    "delayed_cbet": ("turn", "river"),
    "probe": ("turn", "river"),
    "fold_to_probe": ("turn", "river"),
    "float": ("flop", "turn", "river"),
}


@pytest.fixture()
def hu(db):
    return {h.hand_number: h for h in load_hands(db, HU_GAME)}


def _block(page: str, start: str, end: str = "};") -> str:
    i = page.index(start)
    return page[i:page.index(end, i)]


# --- stats.html -------------------------------------------------------------


def test_stats_page_has_a_column_per_family_with_its_tip_and_sample_noun():
    page = (STATIC / "stats.html").read_text(encoding="utf-8")
    postflop = re.findall(r'\["(\w+)", "[^"]+", "(\w+)"', _block(page, "const POSTFLOP = [", "];"))
    assert all(k == opp for k, opp in postflop if k in FAMILIES), "each new rate's denominator is its own _opp key"
    keys = [k for k, _ in postflop]
    for key in FAMILIES:
        assert key in keys
    assert keys[-1] == "af", "AFq stays the last postflop column"
    tips = re.findall(r"^\s+(\w+): \(s\) =>", _block(page, "const HEAD_TIP = {"), re.MULTILINE)
    nouns = re.findall(r"(\w+): \"", _block(page, "const OPP_NOUN = {"))
    for key in FAMILIES:
        assert key in tips and key in nouns, key


def test_stats_page_shows_each_family_on_its_own_streets_only():
    """Run the page's own column list: DCB, Probe and F2P are turn and river only."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    page = (STATIC / "stats.html").read_text(encoding="utf-8")
    start, end = page.index("const LATER"), page.index("const POSTFLOP = [")
    src = page[start:end] + _block(page, "const POSTFLOP = [", "];") + "];"
    script = src + """
const shown = {};
for (const s of ["flop", "turn", "river"])
  shown[s] = POSTFLOP.filter(([, , , streets]) => !streets || streets.includes(s)).map(([k]) => k);
console.log(JSON.stringify(shown));"""
    shown = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout)
    for key, streets in FAMILIES.items():
        for s in ("flop", "turn", "river"):
            assert (key in shown[s]) == (s in streets), (key, s)


def test_every_new_column_is_a_rate_the_server_sends(db):
    """The page reads `<key>_<street>` and `_opp[<key>_<street>]`: both must exist
    on exactly the streets the page shows the column on."""
    row = report(db)[0]
    for key, streets in FAMILIES.items():
        for s in ("flop", "turn", "river"):
            assert (f"{key}_{s}" in row) == (s in streets), (key, s)
            assert (f"{key}_{s}" in row["_opp"]) == (s in streets), (key, s)


# --- chart.html -------------------------------------------------------------


def test_lines_view_has_probe_and_float_chips_that_parse():
    page = (STATIC / "chart.html").read_text(encoding="utf-8")
    chips = dict(re.findall(r'\["([^"]+)", "([^" ]+)"', _block(page, "const LINE_CHIPS = [", "];")))
    assert chips["probe"] == "probe_turn"
    assert chips["float"] == "float_turn"
    for term in chips.values():
        parse_filter(term)


# --- nodes ------------------------------------------------------------------


def _walk(hand, pid):
    return [(n.kind, n.decision, n.filter) for n in nodes_for(hand)[pid]]


def test_a_river_probe_and_the_answer_to_it_are_nodes(hu):
    assert _walk(hu[63], CHRIS)[-1] == (
        "probe", "bet", "position=BB,called_open,srp,players_flop=2,donk_flop,probe_river_opp",
    )
    assert _walk(hu[63], GP)[-1] == ("faced_probe", "call", "position=BTN/SB,opener,srp,players_flop=2,faced_probe_river")
    assert _walk(hu[65], GP)[-1][:2] == ("faced_probe", "fold")


def test_answering_a_probe_earns_a_history_term_that_reproduces(parsed_all):
    """Call and raise of a probe write `faced_probe_S` and the line it starts."""
    seen = Counter()
    for result in parsed_all.values():
        for parsed in result.hands:
            row = hand_row(parsed)
            facts = {f.pn_id: f for f in derive(row)}
            for pid, path in nodes_for(row).items():
                for i, node in enumerate(path):
                    if node.kind != "faced_probe" or node.decision not in ("call", "raise"):
                        continue
                    if i + 1 == len(path):
                        continue
                    added = path[i + 1].post_history[len(node.post_history):]
                    s = node.street
                    assert added == [f"faced_probe_{s}", f"line_{s}={node.decision[0]}*"]
                    assert all(parse_filter(t)(facts[pid]) for t in added)
                    seen[node.decision] += 1
    assert seen["call"] and seen["raise"]


def test_node_spots_are_exactly_the_facts_opportunities(parsed_every):
    """Every delayed c-bet, probe, facing-a-probe and float opportunity derive()
    records is one node of that kind on that street, and nothing more."""
    opp = {"delayed_cbet": "delayed_cbet_opp", "probe": "probe_opp",
           "faced_probe": "fold_to_probe_opp", "float": "float_opp"}
    nodes, facts = Counter(), Counter()
    for result in parsed_every.values():
        for parsed in result.hands:
            row = hand_row(parsed)
            for f in derive(row):
                for kind, attr in opp.items():
                    for s, on in getattr(f, attr).items():
                        if on:
                            facts[kind, s, f.pn_id, row.hand_number, row.game_id] += 1
            for pid, path in nodes_for(row).items():
                for n in path:
                    if n.kind in NEW_KINDS:
                        nodes[n.kind, n.street, pid, row.hand_number, row.game_id] += 1
    assert nodes == facts
    assert {k[0] for k in nodes} == set(NEW_KINDS)


def test_a_pending_probe_spot_is_the_spot_before_the_bet(hu):
    """Cut #63 just before Chris's river probe: he is to act in his probe spot."""
    hand = hu[63]
    i = next(i for i, a in enumerate(hand.actions)
             if a.street == "river" and a.pn_id == CHRIS and not a.is_forced)
    cut = replace(hand, actions=hand.actions[:i], complete=False)
    pending = nodes_for(cut, pending_for=CHRIS)[CHRIS][-1]
    assert pending.decision is None
    assert (pending.kind, pending.label) == ("probe", "river probe spot")
    assert pending.filter == "position=BB,called_open,srp,players_flop=2,donk_flop,probe_river_opp"


def _chris(hu) -> list[Facts]:
    return [f for h in hu.values() for f in derive(h) if f.pn_id == CHRIS]


def test_resolve_answers_a_probe_spot_from_the_probe_hands(hu):
    facts = _chris(hu)
    path = nodes_for(hu[63])[CHRIS]
    got = resolve(facts, path)
    assert (got["kind"], got["at"], got["label"]) == ("probe", "river", "river probe spot")
    assert not got["arrived"] and got["decision"] == "bet"
    # The answer's filter reproduces its hands, and the mix is counted there.
    pred = parse_filter(got["filter"])
    hit = [f for f in facts if f.complete and pred(f)]
    assert len(hit) == got["hands"] >= 1
    assert all(f.probe_opp.get("river") for f in hit)
    assert got["decisions"] == {
        "bet": sum(bool(f.probe.get("river")) for f in hit),
        "check": sum(not f.probe.get("river") for f in hit),
    }


def test_probe_ladder_widens_like_any_postflop_node():
    node = Node(kind="probe", street="river", level=None, label="river probe spot",
                position="position=BB", pf_history=["called_open"], pot="srp",
                post_history=["donk_flop"], situation="probe_river_opp")
    assert [c[0] for c in candidates(node)] == [
        "position=BB,called_open,srp,donk_flop,probe_river_opp",
        "position=BB,called_open,srp,probe_river_opp",
        "called_open,srp,probe_river_opp",
        "srp,probe_river_opp",
        "probe_river_opp",
    ]
    assert {c[2] for c in candidates(node)} == {("probe", "river")}


def _fact(**kw) -> Facts:
    kw.setdefault("seats_from_button", 3)
    return Facts(hand_id=0, pn_id="x", n_dealt_in=6, dead_button=False, **kw)


def test_decisions_at_the_new_kinds():
    f = _fact(delayed_cbet_opp={"turn": True}, delayed_cbet={"turn": True},
              probe_opp={"river": True},
              fold_to_probe_opp={"turn": True}, faced_bet_action={"turn": "raise"},
              float_opp={"flop": True}, float_bet={"flop": True})
    assert decisions([f], ("delayed_cbet", "turn")) == {"bet": 1, "check": 0}
    assert decisions([f], ("probe", "river")) == {"bet": 0, "check": 1}
    assert decisions([f], ("faced_probe", "turn")) == {"fold": 0, "call": 0, "raise": 1}
    assert decisions([f], ("float", "flop")) == {"bet": 1, "check": 0}
    assert decision_of(f, ("probe", "turn")) is None
    folded = _fact(fold_to_probe_opp={"river": True}, fold_to_probe={"river": True},
                   faced_bet_action={"river": "fold"})
    assert decision_of(folded, ("faced_probe", "river")) == "fold"


def test_a_probe_spot_with_no_history_is_answered_from_the_spot_before():
    """Before probes were tracked the HUD showed the nearest earlier spot; a
    player who has never been in one still gets that, marked as arrived."""
    facts = [_fact(pf_faced={2: "call"}), _fact(pf_faced={2: "call"})]
    earlier = Node(kind="preflop", street="preflop", level=2, label="facing open", position=None,
                   situation="faced_open", decision="call")
    probe = Node(kind="probe", street="turn", level=None, label="turn probe spot", position=None,
                 pf_history=["called_open"], pot="srp", situation="probe_turn_opp")
    got = resolve(facts, [earlier, probe])
    assert got["filter"] == "faced_open" and got["arrived"] and not got["exact"]
    assert got["relaxed"] == ["no turn probe spot hands; showing facing open"]
    assert got["decision"] == "call" and got["street"] == "turn"
    # With a probe of their own behind it, the probe spot answers itself.
    facts.append(_fact(pf_faced={2: "call"}, pot_level=2, probe_opp={"turn": True}))
    got = resolve(facts, [earlier, probe])
    assert got["kind"] == "probe" and not got["arrived"] and got["hands"] == 1
    # The older spots keep their "no data".
    cbet = Node(kind="cbet", street="turn", level=None, label="turn c-bet spot", position=None,
                situation="cbet_turn_opp")
    assert resolve(facts, [earlier, cbet]) is None
