"""The spot tree: one player's decision points, browsed the way a solver's tree is.

The root is their seat. Under it, each decision point they reached first; under
each of those, what they chose there and the next decision point the hands that
chose it reached; and so on down. Every node carries how often each choice was
made and the filter that finds exactly its hands.

The walk is `nodes.nodes_for`, so a decision point here is a live-HUD node. What
was decided at it is read back from the hand's `Facts` with `nodes.decision_of`,
the same reading a filter makes -- which is what lets each node's filter find
exactly the hands counted at it. Pure: hands and Facts in, a tree out; `tree_for`
is the database half. See SPEC.md, "Spot tree".
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from ..db.conn import generation, path_of, reading
from ..logfmt.events import FLOP, PREFLOP, STREET_INDEX
from ..logfmt.parser import position_name
from .derive import Facts, HandRow, pct
from .filters import DECISIONS
from .lru import LRU
from .nodes import Node, decision_of, decisions, label_of, nodes_for, players_term, texture_terms
from .queries import facts_cached, identities_of, load_hands
from .results import SEAT_ORDER

Key = tuple[str, int | str]

#: Preflop bet level -> the flag for having acted at it, which is also the name
#: its decision terms take (`faced_open=call`). Level 3 is `faced_3bet_any`, the
#: opener's and everyone else's alike: `faced_3bet` would be the opener's alone,
#: and the level is what `decision_of` reads.
_LEVEL_TERM = {1: "unopened", 2: "faced_open", 3: "faced_3bet_any", 4: "faced_4bet", 5: "faced_5bet"}

#: Postflop kind -> (the flag for reaching the spot on street S, the flag for
#: betting there). The two facing spots are answered fold / call / raise instead.
_SPOT = {
    "donk": ("donk_{s}_opp", "donk_{s}"),
    "cbet": ("cbet_{s}_opp", "cbet_{s}"),
    "delayed_cbet": ("delayed_cbet_{s}_opp", "delayed_cbet_{s}"),
    "probe": ("probe_{s}_opp", "probe_{s}"),
    "float": ("float_{s}_opp", "float_{s}"),
    "faced_cbet": ("faced_cbet_{s}", None),
    "faced_probe": ("faced_probe_{s}", None),
}

#: The order a player can meet the postflop spots in within one street: a lead
#: they check comes before the c-bet they then face. No other two share a street.
_KIND_ORDER = {k: i for i, k in enumerate(_SPOT)}

_POT_TERM = {0: "bomb_pot", 1: "limped", 2: "srp", 3: "3bet_pot"}
_POT_WORDS = {"limped": "limped", "srp": "SRP", "3bet_pot": "3-bet pot", "4bet_pot": "4-bet pot", "bomb_pot": "bomb pot"}
_RAISE_WORDS = {2: "raise", 3: "3-bet", 4: "4-bet", 5: "5-bet"}
_HIGH_WORDS = {
    "ace_high": "A-high", "king_high": "K-high", "queen_high": "Q-high",
    "jack_high": "J-high", "ten_high": "T-high", "low": "low",
}

DEFAULT_DEPTH = 6


def spot_term(key: Key) -> str:
    """The flag for having reached the spot `key` names."""
    kind, at = key
    if kind == "preflop":
        return _LEVEL_TERM[int(at)]
    return _SPOT[kind][0].format(s=at)


def decision_terms(key: Key, decision: str) -> list[str]:
    """The terms that say this decision was made at `key` -- exactly the hands
    `decision_of` reads it on. Each implies the spot."""
    kind, at = key
    if kind == "preflop":
        return [f"{_LEVEL_TERM[int(at)]}={decision}"]
    s = str(at)
    if kind == "faced_cbet":
        return [{"fold": f"folded_to_cbet_{s}", "call": f"called_cbet_{s}", "raise": f"raised_cbet_{s}"}[decision]]
    if kind == "faced_probe":
        # As nodes.py: the PFA had not acted on the street before the probe, so
        # their line there starts with the answer.
        return [f"folded_to_probe_{s}"] if decision == "fold" else [f"faced_probe_{s}", f"line_{s}={decision[0]}*"]
    opp, bet = (t.format(s=s) for t in _SPOT[kind])
    return [bet] if decision == "bet" else [opp, f"!{bet}"]


def _rank(key: Key) -> tuple[int, int]:
    kind, at = key
    if kind == "preflop":
        return (0, int(at))
    return (STREET_INDEX[str(at)], _KIND_ORDER[kind])


def seat_of(f: Facts) -> str | None:
    """The seat `position=` reads: none on a dead button or with no seat. A dead
    blind does not hide it from the filter, so it does not hide it here."""
    if f.seats_from_button is None or f.dead_button:
        return None
    return position_name(f.seats_from_button, f.n_dealt_in, f.position_slots)


def _extras(f: Facts, texture: bool) -> tuple[str, ...]:
    """What the first postflop node adds: the pot type, heads-up or multiway, and
    with `texture` the flop's high card and pairing. Hand-level, so the same for
    every node of the hand."""
    pot = _POT_TERM.get(f.pot_level, "4bet_pot")
    players = players_term(f.players_flop)
    tex = texture_terms(f.board, FLOP) if texture else []
    return tuple(t for t in (pot, players, *tex) if t)


def _steps(hand: HandRow, f: Facts) -> list[tuple[Node, Key, str]]:
    """This player's tracked decisions on the hand, in order: (node, key, what
    `decision_of` reads there). Untracked nodes are skipped."""
    out: list[tuple[Node, Key, str]] = []
    seen: set[Key] = set()
    for node in nodes_for(hand).get(f.pn_id, []):
        key = node.key
        if key is None or key in seen:
            continue
        d = decision_of(f, key)
        if d is None:
            continue
        seen.add(key)
        out.append((node, key, d))
    return out


# ----------------------------------------------------------------- edge words --

def _raises(lo: int, hi: int) -> str:
    """Villain raises from level `lo` to `hi`, in words."""
    if lo == hi:
        return f"villain {'raises' if lo == 2 else _RAISE_WORDS[lo] + 's'}"
    return ", ".join(_RAISE_WORDS.get(n, f"{n}-bet") for n in range(lo, hi + 1))


def _edge(prev: Node | None, prev_key: Key | None, via: str | None, node: Node, key: Key,
          extras: tuple[str, ...]) -> str:
    """What happened between the player's last tracked decision and this one."""
    parts: list[str] = []
    if key[0] == "preflop":
        level = int(key[1])
        if prev_key is None:
            parts.append("no raise yet" if level == 1 else
                         "villain opens" if level == 2 else
                         "open, " + ", ".join(_RAISE_WORDS[n] for n in range(3, level + 1)))
        else:
            first = int(prev_key[1]) + (2 if via == "raise" else 1)
            parts.append(_raises(first, level))
        return " · ".join(parts)

    street = node.street
    if extras:  # the first postflop node: how the pot got here
        if via == "raise" and prev_key is not None and prev_key[0] == "preflop":
            parts.append("called")
        pot = next((t for t in extras if t in _POT_WORDS), None)
        parts.append(f"{_POT_WORDS[pot]} {street}" if pot else street)
        if any(t.startswith("players_flop") for t in extras):
            parts.append("HU" if "players_flop=2" in extras else "multiway")
        board = [t.split("=", 1)[1] for t in extras if t.startswith("flop=")]
        if board:
            parts.append(" ".join(_HIGH_WORDS.get(w, w) for w in board))
    elif prev is None or prev.street != street:
        parts.append(street)
    before = {
        "faced_cbet": "villain c-bets",
        "faced_probe": "villain probes",
        "float": "villain checks",
        "delayed_cbet": "checked through",
        "probe": "PFA checked back",
    }.get(key[0])
    if before:
        parts.append(before)
    return " · ".join(p for p in parts if p)


# ---------------------------------------------------------------------- trie --

@dataclass(slots=True)
class _T:
    id: str
    label: str
    edge: str | None
    kind: str | None
    street: str
    key: Key | None
    via: str | None
    #: The terms every hand here satisfies, short of the spot itself: the seat,
    #: each earlier decision, and from the first postflop node the pot.
    terms: list[str]
    hands: list[Facts] = field(default_factory=list)
    #: Hands with no tracked decision after this one.
    ended: int = 0
    #: Keys some other hand skipped on its way to matching these terms.
    neg: set[Key] = field(default_factory=set)
    children: dict[tuple, _T] = field(default_factory=dict)


def _key_id(key: Key) -> str:
    return f"pf{key[1]}" if key[0] == "preflop" else f"{key[0]}_{key[1]}"


def _child(parent: _T, prev: Node | None, via: str | None, node: Node, key: Key,
           extras: tuple[str, ...]) -> _T:
    terms = list(parent.terms)
    if parent.key is not None and via is not None:
        terms += decision_terms(parent.key, via)
    terms += extras
    seg = _key_id(key) if via is None else f"{via}.{_key_id(key)}"
    if extras:
        seg += "~" + "~".join(extras)
    label = node.label if key[0] == "preflop" else label_of(key)
    return _T(
        id=f"{parent.id}/{seg}",
        label=label,
        edge=_edge(prev, parent.key, via, node, key, extras),
        kind=key[0],
        street=node.street,
        key=key,
        via=via,
        terms=terms,
    )


def _overmatch(root: _T, steps: list[tuple[Node, Key, str]], extras: tuple[str, ...]) -> None:
    """Mark, on every node whose terms this hand also satisfies without being one
    of its hands, the decision points it skipped to get there.

    A node's terms name each earlier decision and its own spot, and terms only
    ever add up: a button who limped and then faced a raise satisfies the terms
    of the button's "facing open" node too. Such a hand reaches the node by a
    subsequence of its own path, so every subsequence that lands on a node is
    walked, and what it skipped is what the node's filter must rule out.
    """

    def walk(t: _T, j: int, via: str | None, skipped: tuple[Key, ...], post: bool) -> None:
        for k in range(j, len(steps)):
            node, key, d = steps[k]
            first_post = node.street != PREFLOP and not post
            child = t.children.get((via, key, extras if first_post else ()))
            if child is None:
                continue
            here = skipped + tuple(s[1] for s in steps[j:k])
            child.neg.update(here)
            walk(child, k + 1, d, here, post or first_post)

    walk(root, 0, None, (), False)


def _order(key: Key | None) -> tuple[str, ...]:
    """The decisions at `key` in display order, for sorting its children."""
    if key is None:
        return ()
    return DECISIONS if key[0] == "preflop" else tuple(decisions([], key))


def _out(t: _T, min_hands: int) -> dict:
    neg = [f"!{spot_term(k)}" for k in sorted(t.neg, key=_rank)]
    base = [*t.terms, *neg]
    filt = ",".join([*base, spot_term(t.key)] if t.key is not None else base)
    rows = []
    if t.key is not None:
        counts = decisions(t.hands, t.key)
        n = len(t.hands)
        for d, c in counts.items():
            rows.append({
                "decision": d,
                "hands": c,
                "pct": pct(c, n),
                "filter": ",".join([*base, *decision_terms(t.key, d)]),
            })
    order = _order(t.key)
    kids = sorted(
        (c for c in t.children.values() if len(c.hands) >= min_hands),
        key=lambda c: (order.index(c.via) if c.via in order else len(order), -len(c.hands), c.id),
    )
    return {
        "id": t.id,
        "label": t.label,
        "edge": t.edge,
        "via": t.via,
        "kind": t.kind,
        "street": t.street,
        "hands": len(t.hands),
        "ended": t.ended,
        "decisions": rows,
        "filter": filt,
        "children": [_out(c, min_hands) for c in kids],
    }


def build_tree(
    hands: Iterable[HandRow],
    facts: Iterable[Facts],
    alias: str | None = None,
    *,
    max_depth: int = DEFAULT_DEPTH,
    min_hands: int = 1,
    texture: bool = False,
) -> dict:
    """One player's spot tree over `facts` (their rows, already narrowed to a
    spot), walking the `hands` those rows come from.

    `max_depth` is how many decisions deep a path goes below the seat;
    `min_hands` hides a node with fewer hands (and so everything under it);
    `texture` splits the first postflop node by the flop's high card and pairing.
    """
    # A list per hand: two of one player's merged identities can sit in one hand.
    by_hand: dict[int, list[Facts]] = {}
    for f in facts:
        by_hand.setdefault(f.hand_id, []).append(f)
    roots: dict[str, _T] = {}
    walked: list[tuple[_T, list[tuple[Node, Key, str]], tuple[str, ...]]] = []
    unplaced = 0
    seen = 0
    for hand, f in ((h, f) for h in hands for f in by_hand.get(h.hand_id, ())):
        seen += 1
        seat = seat_of(f)
        if seat is None:
            unplaced += 1
            continue
        root = roots.get(seat)
        if root is None:
            root = roots[seat] = _T(id=seat, label=seat, edge=None, kind=None, street=PREFLOP, key=None,
                                    via=None, terms=[f"position={seat}"])
        root.hands.append(f)
        steps = _steps(hand, f)
        extras = _extras(f, texture)
        cur, prev, via, post = root, None, None, False
        for node, key, d in steps[:max_depth]:
            first_post = node.street != PREFLOP and not post
            ex = extras if first_post else ()
            post = post or first_post
            ck = (via, key, ex)
            child = cur.children.get(ck)
            if child is None:
                child = cur.children[ck] = _child(cur, prev, via, node, key, ex)
            child.hands.append(f)
            cur, prev, via = child, node, d
        if len(steps) <= max_depth:
            cur.ended += 1
        walked.append((root, steps, extras))
    for root, steps, extras in walked:
        _overmatch(root, steps, extras)

    seats = sorted(roots.values(), key=lambda t: (SEAT_ORDER.index(t.id) if t.id in SEAT_ORDER else 99, t.id))
    return {
        "player": alias,
        "hands": seen,
        "unplaced": unplaced,
        "depth": max_depth,
        "texture": texture,
        "min_hands": min_hands,
        "roots": [_out(t, min_hands) for t in seats if len(t.hands) >= min_hands],
        "notes": NOTES,
    }


NOTES = (
    "Each node's filter, added to the spot's, finds exactly its hands.",
    "Untracked spots (a bet in a limped pot, say) are skipped: the next tracked one hangs off the last.",
    "A dead-button hand has no seat and is left out (unplaced).",
)


# ------------------------------------------------------------------- database --

#: Built trees per (database, alias, game, spot, depth, texture, min_hands), with
#: the generation they were built at. A tree costs a load of the player's hands
#: and a walk of each, about two seconds on a 14,000-hand player; the browser asks
#: for it again on every expand and reload.
_TREE_CACHE: LRU[tuple, tuple[int, dict]] = LRU(8)


def tree_for(
    conn: sqlite3.Connection,
    alias: str,
    game_id: str | None = None,
    predicate: Callable[[Facts], bool] | None = None,
    *,
    spot: str | None = None,
    max_depth: int = DEFAULT_DEPTH,
    min_hands: int = 1,
    texture: bool = False,
) -> dict:
    """`build_tree` over one player's hands in a game (or all), narrowed by
    `predicate`. `spot` is the filter text behind `predicate`, the cache key; a
    predicate without one is never cached. Raises ValueError on an unknown alias.
    """
    gen = generation(conn)
    path = path_of(conn)
    cacheable = gen is not None and bool(path) and (predicate is None or spot is not None)
    key = (path, alias, game_id, spot if predicate is not None else None, max_depth, texture, min_hands)
    hit = _TREE_CACHE.get(key) if cacheable else None
    if hit is not None and hit[0] == gen:
        return hit[1]
    with _TREE_CACHE.making(key):
        hit = _TREE_CACHE.get(key) if cacheable else None
        if hit is not None and hit[0] == gen:
            return hit[1]
        with reading(conn):
            facts = facts_cached(conn, alias, game_id)
            if predicate is not None:
                facts = [f for f in facts if predicate(f)]
            if predicate is not None and len(facts) <= 5000:
                hands = load_hands(conn, game_id, hand_ids=[f.hand_id for f in facts])
            else:
                hands = load_hands(conn, game_id, identities_of(conn, alias))
        out = build_tree(hands, facts, alias, max_depth=max_depth, min_hands=min_hands, texture=texture)
        if cacheable:
            _TREE_CACHE[key] = (gen, out)
        return out
