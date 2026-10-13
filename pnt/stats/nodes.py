"""Decision points: where a player stands in a hand, written as a filter.

Mirrors SPEC.md, "Decision points (nodes)". The walk here is `derive._preflop`
and `derive._postflop` with the *situation* pulled out at each action, so a node's
filter names exactly the facts those two functions would set on the finished hand.
That is what lets a hand still in progress be looked up in a player's history --
the node is a spot, and a spot is a filter.

Pure: `HandRow` in, nodes out; `resolve` takes the player's `Facts` rather than a
database. Nothing here is stored.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from ..logfmt.events import FLOP, PREFLOP, RIVER, TURN
from ..logfmt.parser import position_name
from .cards import board_at, board_texture, street_of_board
from .derive import POSTFLOP_STREETS, PREV_STREET, VOLUNTARY_COMMIT, Facts, HandRow, size_bucket
from .filters import DECISIONS, parse_filter
from .tags import strength_at

__all__ = [
    "Node", "candidates", "decision_of", "decisions", "nodes_for", "resolve",
    "showings", "street_of_board", "texture_terms",
]

#: Preflop bet level -> the situation term for a player acting there. Level 3
#: depends on whether they opened; above 5 nothing tracks the spot.
_LEVEL_SITUATION = {1: "unopened", 2: "faced_open", 4: "faced_4bet", 5: "faced_5bet"}
_LEVEL_LABEL = {1: "unopened", 2: "facing open", 3: "facing 3bet", 4: "facing 4bet", 5: "facing 5bet"}
_SITUATION_LEVEL = {
    "unopened": 1,
    "faced_open": 2,
    "faced_3bet": 3,
    "faced_3bet_any": 3,
    "faced_4bet": 4,
    "faced_5bet": 5,
}
#: The history term a raise or a call at each level earns. Anything not listed
#: is spelled `<situation>=<decision>`.
_RAISE_TERM = {1: "opener", 2: "3bet", 3: "4bet", 4: "5bet"}
_CALL_TERM = {1: "limp", 2: "called_open"}
#: `pot_level` -> pot type term; 4 and above is `4bet_pot`.
_POT_TERM = {1: "limped", 2: "srp", 3: "3bet_pot"}
_POT_DOWN = {"4bet_pot": "3bet_pot", "3bet_pot": "srp", "limped": "srp"}
_POT_LABEL = {"3bet_pot": "3-bet pots", "srp": "single-raised pots"}

_STREET_INDEX = {PREFLOP: 0, FLOP: 1, TURN: 2, RIVER: 3}


@dataclass(slots=True)
class Node:
    """One decision point on a player's path through a hand."""

    #: preflop | cbet | faced_cbet | donk | delayed_cbet | probe | faced_probe |
    #: float, or None when nothing tracks the spot.
    kind: str | None
    street: str
    #: The preflop bet level, for preflop nodes.
    level: int | None
    label: str
    position: str | None
    pf_history: list[str] = field(default_factory=list)
    pot: str | None = None
    post_history: list[str] = field(default_factory=list)
    situation: str | None = None
    #: None while the decision is still pending.
    decision: str | None = None

    @property
    def filter(self) -> str | None:
        if self.situation is None:
            return None
        parts = [self.position, *self.pf_history, self.pot, *self.post_history, self.situation]
        return ",".join(p for p in parts if p)

    @property
    def key(self) -> tuple[str, int | str] | None:
        """What `decisions()` counts at this node: the situation, not the history."""
        if self.kind is None:
            return None
        return ("preflop", self.level) if self.kind == "preflop" else (self.kind, self.street)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "street": self.street,
            "level": self.level,
            "label": self.label,
            "filter": self.filter,
            "decision": self.decision,
        }


@dataclass(slots=True)
class _Street:
    """The state `derive._postflop` keeps while walking one street."""

    bet_made: bool = False
    cbet_by: str | None = None
    cbet_size: str | None = None
    cbet_raised: bool = False
    aggressor: str | None = None
    last_aggression: str | None = None  # bet | raise
    acted: set[str] = field(default_factory=set)
    # After a missed c-bet (SPEC.md, "Delayed c-bet, probe and float"). `pfa` is
    # the preflop aggressor on every street; the two flags say whether this
    # street can hold a delayed c-bet or a probe at all; `floaters` are the
    # players who called on the street before (on the flop, put chips in preflop);
    # `called` collects this street's callers for the next one.
    pfa: str | None = None
    delayed_on: bool = False
    probe_on: bool = False
    probe_by: str | None = None
    probe_raised: bool = False
    floaters: set[str] = field(default_factory=set)
    called: set[str] = field(default_factory=set)


def _preflop_node(pid: str, level: int, opener: str | None, position: str | None,
                  history: list[str]) -> Node:
    if level == 3:
        situation = "faced_3bet" if pid == opener else "faced_3bet_any"
        label = "squeezed" if "called_open" in history else (
            "facing 3bet" if pid == opener else "facing 3bet (cold)")
    else:
        situation = _LEVEL_SITUATION.get(level)
        label = _LEVEL_LABEL.get(level, "facing 6bet+")
    return Node(
        kind="preflop" if situation else None,
        street=PREFLOP,
        level=level,
        label=label,
        position=position,
        pf_history=list(history),
        situation=situation,
    )


def _preflop_term(node: Node, decision: str) -> str | None:
    level = node.level or 0
    if decision == "raise":
        if level in _RAISE_TERM:
            return _RAISE_TERM[level]
        return f"{node.situation}=raise" if node.situation else None
    if decision == "call":
        if level in _CALL_TERM:
            return _CALL_TERM[level]
        return f"{node.situation}=call" if node.situation else None
    return None


def _missed_cbet_spot(pid: str, street: str, st: _Street,
                      prev_aggressor: str | None) -> tuple[str, str, str] | None:
    """(kind, situation, label) for the spots a missed c-bet leaves open, or None.

    The conditions are `derive._postflop`'s for the delayed c-bet, probe, float
    and fold-to-probe facts. SPEC.md shows the first three disjoint from the
    c-bet and the donk bet, and from each other, and facing a probe never shares
    a street with facing a c-bet, so the order here decides nothing.
    """
    if not st.bet_made:
        if st.delayed_on and pid == st.pfa:
            return "delayed_cbet", f"delayed_cbet_{street}_opp", f"{street} delayed c-bet spot"
        if st.probe_on and pid != st.pfa and st.pfa not in st.acted:
            return "probe", f"probe_{street}_opp", f"{street} probe spot"
        if (
            prev_aggressor is not None
            and pid != prev_aggressor
            and prev_aggressor in st.acted
            and pid in st.floaters
        ):
            return "float", f"float_{street}_opp", f"{street} float spot"
        return None
    if st.probe_by is not None and not st.probe_raised and pid == st.pfa:
        return "faced_probe", f"faced_probe_{street}", f"facing {street} probe"
    return None


def _postflop_node(pid: str, street: str, st: _Street, prev_aggressor: str | None,
                   all_in_at: dict[str, int], seq: int, position: str | None,
                   pf_history: list[str], pot: str, post_history: list[str]) -> Node:
    kind: str | None
    situation: str | None
    if not st.bet_made and pid == prev_aggressor:
        kind, situation, label = "cbet", f"cbet_{street}_opp", f"{street} c-bet spot"
    elif (
        not st.bet_made
        and prev_aggressor is not None
        and pid != prev_aggressor
        and prev_aggressor not in st.acted
        and all_in_at.get(prev_aggressor, seq) >= seq
    ):
        kind, situation, label = "donk", f"donk_{street}_opp", f"{street} lead spot"
    elif st.cbet_by is not None and not st.cbet_raised and pid != st.cbet_by:
        kind = "faced_cbet"
        situation = f"faced_cbet_{street}={st.cbet_size}" if st.cbet_size else f"faced_cbet_{street}"
        label = f"facing {street} c-bet" + (f" ({st.cbet_size})" if st.cbet_size else "")
    elif (missed := _missed_cbet_spot(pid, street, st, prev_aggressor)) is not None:
        kind, situation, label = missed
    else:
        kind, situation = None, None
        if not st.bet_made:
            label = f"first in ({street})" if not st.acted else f"checked to ({street})"
        else:
            label = f"facing {street} {st.last_aggression or 'bet'}"
    return Node(
        kind=kind,
        street=street,
        level=None,
        label=label,
        position=position,
        pf_history=list(pf_history),
        pot=pot,
        post_history=list(post_history),
        situation=situation,
    )


def _postflop_term(node: Node, decision: str, amount: int, pot_before: int) -> str | None:
    s = node.street
    if node.kind == "cbet" and decision == "bet":
        return f"cbet_{s}={size_bucket(amount, pot_before)}" if pot_before > 0 else f"cbet_{s}"
    if node.kind == "faced_cbet":
        if decision == "call":
            return f"called_cbet_{s}"
        if decision == "raise":
            return f"raised_cbet_{s}"
    if node.kind == "donk" and decision == "bet":
        return f"donk_{s}"
    if node.kind in ("delayed_cbet", "probe", "float") and decision == "bet":
        return f"{node.kind}_{s}"
    if node.kind == "faced_probe" and decision in ("call", "raise"):
        # No fact names the answer to a probe beyond the fold, but the preflop
        # aggressor had not acted on the street before it, so their line there
        # starts with the answer.
        return f"faced_probe_{s},line_{s}={decision[0]}*"
    return None


def nodes_for(hand: HandRow, pending_for: str | None = None) -> dict[str, list[Node]]:
    """Each dealt-in player's path through the hand, one node per decision.

    With `pending_for`, that player also gets a trailing node with no decision:
    the spot they are in right now, on the street the board says the hand is on.
    """
    paths: dict[str, list[Node]] = {pid: [] for pid in hand.players}
    labelled = not hand.dead_button and not hand.blinds_irregular
    position = {
        pid: (
            f"position={position_name(p.seats_from_button, hand.n_dealt_in)}"
            if labelled and p.seats_from_button is not None
            else None
        )
        for pid, p in hand.players.items()
    }
    pf_history: dict[str, list[str]] = {pid: [] for pid in hand.players}
    post_history: dict[str, list[str]] = {pid: [] for pid in hand.players}
    current = street_of_board(hand.board)

    # --- preflop: the bet-level walk of derive._preflop
    level = 1
    aggressor: str | None = None
    opener: str | None = None
    for a in hand.actions:
        if a.street != PREFLOP or a.is_forced or a.pn_id not in paths:
            continue
        decision = "raise" if a.kind == "bet" else a.kind
        node = _preflop_node(a.pn_id, level, opener, position[a.pn_id], pf_history[a.pn_id])
        node.decision = decision
        paths[a.pn_id].append(node)
        term = _preflop_term(node, decision)
        if term:
            pf_history[a.pn_id].append(term)
        if decision == "raise":
            level += 1
            if level == 2:
                opener = a.pn_id
            aggressor = a.pn_id
    if pending_for in paths and current == PREFLOP:
        paths[pending_for].append(
            _preflop_node(pending_for, level, opener, position[pending_for], pf_history[pending_for])
        )
    pot = _POT_TERM.get(level, "4bet_pot")

    # --- postflop: the street walk of derive._postflop
    pot_before: dict[int, int] = {}
    all_in_at: dict[str, int] = {}
    running = 0
    for a in hand.actions:
        pot_before[a.seq] = running
        running += a.amount
        if a.all_in:
            all_in_at.setdefault(a.pn_id, a.seq)
    next_seq = max((a.seq for a in hand.actions), default=0) + 1

    prev_aggressor = aggressor
    # derive._postflop's missed-c-bet state: who closed each checked-through
    # street, and whether every postflop street so far checked through.
    pfa = aggressor
    closed_by: dict[str, str] = {}
    unbet = True
    vpip = {
        a.pn_id for a in hand.actions
        if a.street == PREFLOP and not a.is_forced and a.kind in VOLUNTARY_COMMIT
    }
    called_prev: set[str] = vpip
    for street in POSTFLOP_STREETS:
        if _STREET_INDEX[street] > _STREET_INDEX[current]:
            break
        st = _Street(
            pfa=pfa,
            delayed_on=street != FLOP and unbet and pfa is not None,
            probe_on=pfa is not None and closed_by.get(PREV_STREET[street]) == pfa,
            floaters=called_prev,
        )
        street_actions = [a for a in hand.actions if a.street == street and not a.is_forced]
        for a in street_actions:
            pid = a.pn_id
            if pid in paths:
                node = _postflop_node(
                    pid, street, st, prev_aggressor, all_in_at, a.seq,
                    position[pid], pf_history[pid], pot, post_history[pid],
                )
                node.decision = a.kind
                paths[pid].append(node)
                term = _postflop_term(node, a.kind, a.amount, pot_before[a.seq])
                if term:
                    post_history[pid].extend(term.split(","))
            if a.kind == "bet":
                before = pot_before[a.seq]
                size = size_bucket(a.amount, before) if before > 0 else None
                if not st.bet_made and pid == prev_aggressor:
                    st.cbet_by, st.cbet_size = pid, size
                if not st.bet_made and st.probe_on and pid != pfa and pfa not in st.acted:
                    st.probe_by = pid
                st.bet_made, st.aggressor, st.last_aggression = True, pid, "bet"
            elif a.kind == "raise":
                if st.cbet_by is not None:
                    st.cbet_raised = True
                if st.probe_by is not None:
                    st.probe_raised = True
                st.bet_made, st.aggressor, st.last_aggression = True, pid, "raise"
            elif a.kind == "call" and st.bet_made:
                st.called.add(pid)
            st.acted.add(pid)
        if pending_for in paths and street == current:
            paths[pending_for].append(
                _postflop_node(
                    pending_for, street, st, prev_aggressor, all_in_at, next_seq,
                    position[pending_for], pf_history[pending_for], pot, post_history[pending_for],
                )
            )
        prev_aggressor = st.aggressor
        called_prev = st.called
        # A street with no bet that a check closed, two or more having acted, was
        # checked through -- `check_back`. Anything else, an empty street included,
        # ends the run of checked-through streets a delayed c-bet needs.
        last = street_actions[-1] if street_actions else None
        if last is not None and st.aggressor is None and last.kind == "check" and len(st.acted) >= 2:
            closed_by[street] = last.pn_id
        else:
            unbet = False

    return paths


# ------------------------------------------------------------- resolution --

#: (filter, notes on how it was widened, what `decisions()` should count).
Candidate = tuple[str, list[str], tuple[str, int | str]]


def _join(position: str | None, pf: list[str], pot: str | None, post: list[str], situation: str) -> str:
    return ",".join(p for p in [position, *pf, pot, *post, situation] if p)


def _parent(last: str, remaining: list[str]) -> tuple[str, int]:
    """The situation the last history term was decided at, and its level."""
    if last in ("opener", "limp"):
        return "unopened", 1
    if last in ("3bet", "called_open"):
        return "faced_open", 2
    if last == "4bet":
        return ("faced_3bet" if "opener" in remaining else "faced_3bet_any"), 3
    if last == "5bet":
        return "faced_4bet", 4
    situation = last.split("=", 1)[0]
    return situation, _SITUATION_LEVEL[situation]


def candidates(node: Node) -> list[Candidate]:
    """Every filter to try for a node, exact first, widened one step at a time.

    The order is SPEC.md's, "Finding the closest spot". Pure, so the ladder can be
    checked without a database.
    """
    if node.situation is None or node.kind is None:
        return []
    out: list[Candidate] = []
    seen: set[str] = set()

    def add(filt: str, notes: list[str], key: tuple[str, int | str]) -> None:
        if filt not in seen:
            seen.add(filt)
            out.append((filt, list(notes), key))

    if node.kind == "preflop":
        hist = list(node.pf_history)
        situation, level = node.situation, node.level or _SITUATION_LEVEL[node.situation]
        notes: list[str] = []
        while True:
            add(_join(node.position, hist, None, [], situation), notes, ("preflop", level))
            if node.position:
                add(_join(None, hist, None, [], situation), [*notes, "any position"], ("preflop", level))
            if not hist:
                return out
            last = hist.pop()
            situation, level = _parent(last, hist)
            notes = [*notes, f"their {_LEVEL_LABEL[level]} spot"]

    key = (node.kind, node.street)
    pos, pf, pot, post, situation = (
        node.position, list(node.pf_history), node.pot, list(node.post_history), node.situation,
    )
    notes = []
    add(_join(pos, pf, pot, post, situation), notes, key)
    if "=" in situation:
        situation = situation.split("=", 1)[0]
        notes = [*notes, "any size"]
        add(_join(pos, pf, pot, post, situation), notes, key)
    if post:
        post = []
        notes = [*notes, "any earlier street"]
        add(_join(pos, pf, pot, post, situation), notes, key)
    if pos:
        pos = None
        notes = [*notes, "any position"]
        add(_join(pos, pf, pot, post, situation), notes, key)
    if pf:
        pf = []
        notes = [*notes, "any preflop line"]
        add(_join(pos, pf, pot, post, situation), notes, key)
    while pot in _POT_DOWN:
        pot = _POT_DOWN[pot]
        notes = [*notes, _POT_LABEL[pot]]
        add(_join(pos, pf, pot, post, situation), notes, key)
    if pot:
        pot = None
        notes = [*notes, "any pot type"]
        add(_join(pos, pf, pot, post, situation), notes, key)
    return out


#: The decisions `decisions()` reports at each kind of node, in display order.
_KIND_DECISIONS = {
    "cbet": ("bet", "check"),
    "faced_cbet": ("fold", "call", "raise"),
    "donk": ("bet", "check"),
    "delayed_cbet": ("bet", "check"),
    "probe": ("bet", "check"),
    "float": ("bet", "check"),
    "faced_probe": ("fold", "call", "raise"),
}

#: The bet-or-check spots after a missed c-bet: kind -> (opportunity, bet) facts.
_MISSED_CBET_FACTS = {
    "delayed_cbet": ("delayed_cbet_opp", "delayed_cbet"),
    "probe": ("probe_opp", "probe"),
    "float": ("float_opp", "float_bet"),
}


def decision_of(f: Facts, key: tuple[str, int | str]) -> str | None:
    """What this hand did at the spot `key` names, or None when it was never there.

    A player acts once per preflop level and is first in, or facing an unraised
    c-bet, at most once per street, so each key is one decision per hand: this
    is the per-hand inverse of `decisions()`.
    """
    kind, at = key
    if kind == "preflop":
        return f.pf_faced.get(int(at))
    s = str(at)
    if kind == "cbet":
        if f.cbet.get(s):
            return "bet"
        return "check" if f.cbet_opp.get(s) else None
    if kind == "faced_cbet":
        if f.fold_to_cbet.get(s):
            return "fold"
        if f.raise_cbet.get(s):
            return "raise"
        return "call" if f.fold_to_cbet_opp.get(s) else None
    if kind == "donk":
        if f.donk.get(s):
            return "bet"
        return "check" if f.donk_opp.get(s) else None
    if kind in _MISSED_CBET_FACTS:
        opp, bet = _MISSED_CBET_FACTS[kind]
        if getattr(f, bet).get(s):
            return "bet"
        return "check" if getattr(f, opp).get(s) else None
    if kind == "faced_probe":
        if not f.fold_to_probe_opp.get(s):
            return None
        # Facing the probe is the preflop aggressor's first decision facing a bet
        # on the street: nobody raised in front of them, or it would not count.
        return "fold" if f.fold_to_probe.get(s) else f.faced_bet_action.get(s)
    return None


def decisions(facts: Iterable[Facts], key: tuple[str, int | str]) -> dict[str, int]:
    """How the hands behind a spot were played at it: a count per decision.

    Preflop lists `check` only when someone checked -- the big blind's option is
    the one place it happens, and it would be noise everywhere else.
    """
    kind = key[0]
    c = Counter(d for d in (decision_of(f, key) for f in facts) if d)
    if kind == "preflop":
        return {d: c.get(d, 0) for d in DECISIONS if d != "check" or c.get("check")}
    if kind in _KIND_DECISIONS:
        return {d: c.get(d, 0) for d in _KIND_DECISIONS[kind]}
    return {}


#: What a shown hand can be worth at a decision, in the order the HUD lists them.
BUCKETS = ("strong", "medium", "weak", "draw", "air")


def showings(facts: Iterable[Facts], key: tuple[str, int | str], street: str) -> dict[str, dict[str, int]]:
    """What the hands behind a spot turned out to hold, per decision, read on the
    board as it stood on `street`.

    One entry per decision `decisions()` reports: `hands` that made it, `known`
    of them with cards known and a board long enough for `street`, and a count
    per `BUCKETS` over those. The known count is the honest denominator -- the
    hands that folded, and the bluffs that were never called, are not in it.
    """
    out = {d: {"hands": 0, "known": 0, **{b: 0 for b in BUCKETS}} for d in decisions([], key)}
    for f in facts:
        d = decision_of(f, key)
        if d is None:
            continue
        row = out.setdefault(d, {"hands": 0, "known": 0, **{b: 0 for b in BUCKETS}})
        row["hands"] += 1
        s = strength_at(f.hole_cards, f.board, street)
        if s is not None:
            row["known"] += 1
            row[s] += 1
    return out


def _shown(showings_: dict[str, dict[str, int]], decision: str | None = None) -> int:
    """Shown hands behind the split the HUD will print: the one decision once it
    is made, every decision's while it is pending."""
    if decision is not None:
        return showings_.get(decision, {}).get("known", 0)
    return sum(row["known"] for row in showings_.values())


def texture_terms(board: tuple[str, ...] | list[str], street: str) -> list[str]:
    """The filter terms that narrow a postflop spot to boards like this one, most
    telling first: the highest card, then whether the board is paired. Empty
    preflop, or when the board has not reached the street."""
    if street == PREFLOP:
        return []
    seen = board_at(board, street)
    if not seen:
        return []
    tags = board_texture(seen)
    high = next(t for t in tags if t.endswith("_high") or t == "low")
    return [f"{street}={high}", f"{street}={'paired' if 'paired' in tags else 'unpaired'}"]


def label_of(key: tuple[str, int | str]) -> str:
    kind, at = key
    if kind == "preflop":
        return _LEVEL_LABEL.get(int(at), "preflop")
    return {
        "cbet": f"{at} c-bet spot",
        "faced_cbet": f"facing {at} c-bet",
        "donk": f"{at} lead spot",
        "delayed_cbet": f"{at} delayed c-bet spot",
        "probe": f"{at} probe spot",
        "faced_probe": f"facing {at} probe",
        "float": f"{at} float spot",
    }[kind]


def _decision_at(path: list[Node], key: tuple[str, int | str]) -> str | None:
    """What this player decided at the node on `path` that `key` counts at: the
    live node's own decision, or the earlier one a widened spot leads back to."""
    for n in reversed(path):
        if n.key == key:
            return n.decision
    return None


def resolve(
    facts: Iterable[Facts],
    path: list[Node],
    min_hands: int = 1,
    board: tuple[str, ...] | list[str] = (),
    min_known: int = 5,
) -> dict | None:
    """The closest spot in `facts` to the last node of `path` with data behind it.

    Widens the node along `candidates()` until at least `min_hands` hands match;
    None when nothing does. A node nothing tracks is answered from the nearest
    earlier node on the path that has a filter, and so is a missed-c-bet spot
    (`_ANCESTOR_FALLBACK`) whose whole ladder is empty. Only completed hands count: the
    hand in progress must not be evidence about itself.

    With a `board`, each rung is first tried narrowed to boards like this one
    (`texture_terms`, both terms then the first alone), and kept that way only
    when it still has `min_hands` hands *and* `min_known` shown ones behind it:
    the texture is there to sharpen what they showed up with, and a split over
    two hands sharpens nothing.

    `showings` is read on the street of the *live* node, whichever node answers.
    `decision` is what this player did at the spot that answers -- the live
    node's own decision, or None while it is pending -- and `arrived` says the
    answer comes from an earlier node on the path: a player facing a 5-bet with
    no history of it is shown their 4-bet spot, the range they arrived with, and
    an untracked node shows what the hands that took its path had by now.
    """
    if not path:
        return None
    live = path[-1]
    street = live.street
    tries: list[tuple[Node, list[str]]] = []
    earlier = _nearest_tracked(path[:-1])
    if live.situation is not None:
        tries.append((live, []))
        if live.kind in _ANCESTOR_FALLBACK and earlier is not None:
            tries.append((earlier, [f"no {live.label} hands; showing {earlier.label}"]))
    elif earlier is not None:
        tries.append((earlier, [f"{live.label} is not a tracked spot; showing {earlier.label}"]))
    complete = [f for f in facts if f.complete]
    need = max(1, min_hands)
    terms = texture_terms(board, street)
    for node, notes in tries:
        for filt, relaxed, key in candidates(node):
            decided = _decision_at(path, key)
            attempts = [(f"{filt},{','.join(terms[:k])}", terms[:k]) for k in range(len(terms), 0, -1)]
            attempts.append((filt, []))
            for attempt, used in attempts:
                pred = parse_filter(attempt)
                hit = [f for f in complete if pred(f)]
                if len(hit) < need:
                    continue
                shown = showings(hit, key, street)
                if used and _shown(shown, decided) < min_known:
                    continue
                return {
                    "filter": attempt,
                    "hands": len(hit),
                    "known": sum(f.hole_cards is not None for f in hit),
                    "exact": not notes and not relaxed,
                    "relaxed": [*notes, *relaxed],
                    "decisions": decisions(hit, key),
                    "decision": decided,
                    "arrived": key != live.key,
                    "street": street,
                    "texture": used,
                    "showings": shown,
                    "kind": key[0],
                    "at": key[1],
                    "label": label_of(key),
                }
    return None


#: The spots the HUD answered from an earlier node before they were tracked
#: (SPEC.md, "Delayed c-bet, probe and float"). A player with no history of one,
#: even widened all the way, is still answered from that earlier node -- the
#: range they arrived with -- rather than with no data.
_ANCESTOR_FALLBACK = frozenset({"delayed_cbet", "probe", "faced_probe", "float"})


def _nearest_tracked(path: list[Node]) -> Node | None:
    """The last node on `path` that has a filter."""
    return next((n for n in reversed(path) if n.situation is not None), None)
