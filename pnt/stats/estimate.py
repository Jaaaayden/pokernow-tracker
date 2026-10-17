"""Estimated ranges: what a player holds at a preflop spot from how often they take
the action there, and what that range makes on a board.

Shown hands miss the bluffs that got folds. A frequency does not: "opens 62% from
the button" is a range of about 62% of hands, whoever got to see them. So the
estimate fills the top X% of `cards.HAND_RANKING` (capped for a call: the hands
they would have raised come off the top), then lets the hands they did show pull
their class, and its family, into it. The board breakdown classifies every combo
of that range on a board and cuts it at the player's next-action frequency.

Both are estimates and linear by assumption; the notes say so. Pure: Facts in,
dicts out. See SPEC.md, "Estimated ranges".
"""

from __future__ import annotations

import bisect
import itertools
import random
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .cards import (
    HAND_RANKING,
    MADE_LEAVES,
    RANK_VALUE,
    TEXTURE_TAGS,
    VALUE_RANK,
    Card,
    board_texture,
    hand_class,
    leaf_of,
    made_at,
    made_hand,
    parse_cards,
    street_of_board,
)
from .derive import Facts
from .filters import DECIDED, TagResolver, parse_filter
from .lru import LRU
from .nodes import texture_terms
from .queries import wilson
from .streets import GROUP_OF, GROUPS
from .strength import _board as _strength_table

#: Every two-card holding.
COMBOS = 1326
#: Shown hands at the node before calibration moves anything, and shown hands
#: taking the next action before a typed board's texture narrows the spot.
MIN_SHOWN = 5
#: Random flops a flop texture term is averaged over.
SAMPLE_FLOPS = 30

_INDEX = {label: i for i, label in enumerate(HAND_RANKING)}


def combos_of(label: str) -> int:
    """6 for a pair, 4 suited, 12 offsuit."""
    return 6 if len(label) == 2 else 4 if label[2] == "s" else 12


# ------------------------------------------------------------------ the node --

#: Situation -> preflop bet level (SPEC.md, "Decision points").
_LEVEL = {"unopened": 1, "faced_open": 2, "faced_3bet": 3, "faced_3bet_any": 3, "faced_4bet": 4, "faced_5bet": 5}
_DECISION_TERM = re.compile(rf"^({'|'.join(_LEVEL)})=(\w+)$")
#: History terms that name a decision: term -> (situation, decision, a term the
#: spot gains). `steal` is an open from a steal seat first in, and so on.
_ALIASES: dict[str, tuple[str, str, str | None]] = {
    "opener": ("unopened", "raise", None),
    "steal": ("unopened", "raise", "steal_opp"),
    "limp": ("unopened", "call", None),
    "3bet": ("faced_open", "raise", None),
    "squeeze": ("faced_open", "raise", "squeeze_opp"),
    "3bet_vs_steal": ("faced_open", "raise", "fold_to_steal_opp"),
    "called_open": ("faced_open", "call", None),
    "cold_call": ("faced_open", "call", "cold_call_opp"),
    "4bet": ("faced_3bet_any", "raise", None),
    "5bet": ("faced_4bet", "raise", None),
}
#: Flags settled before a preflop decision: they say where the player sat and
#: what was in front of them, not what they did.
_BEFORE_FLAGS = frozenset({
    "first_in", "steal_opp", "squeeze_opp", "cold_call_opp", "3bet_opp", "fold_to_steal_opp", "acted_preflop",
    "straddled", "dead_button", "post_loss",
})
_BEFORE_NUMERIC = re.compile(r"^(players|eff_bb|callers_before|session_hand|session_hour|hands_since_loss)(>=|<=|!=|=|>|<)")
_OPEN_BB = re.compile(r"^open_bb(>=|<=|!=|=|>|<)")
_FLOP_TEXTURE = re.compile(rf"^flop(!=|=)({'|'.join(TEXTURE_TAGS)})$")

_LABEL = {
    (1, "raise"): "open", (1, "call"): "limp", (2, "raise"): "3-bet", (2, "call"): "call vs open",
    (3, "raise"): "4-bet", (3, "call"): "call vs 3-bet", (4, "raise"): "5-bet", (4, "call"): "call vs 4-bet",
    (5, "raise"): "6-bet", (5, "call"): "call vs 5-bet",
}
_SPOT_WORDS = {1: "unopened", 2: "facing an open", 3: "facing a 3-bet", 4: "facing a 4-bet", 5: "facing a 5-bet"}

NO_NODE = "pick one preflop spot, e.g. position=BTN,unopened=raise"


class NoNode(ValueError):
    """The filter does not name one preflop decision to estimate from."""


@dataclass(slots=True)
class Node:
    """The preflop decision a spot filter names, and the spot it was made in."""

    situation: str
    decision: str
    #: The terms the spot keeps: settled before the decision.
    context: list[str] = field(default_factory=list)
    #: The terms left out of the spot, as written.
    ignored: list[str] = field(default_factory=list)

    @property
    def level(self) -> int:
        return _LEVEL[self.situation]

    @property
    def method(self) -> str:
        return "capped" if self.decision == "call" and self.level >= 2 else "linear"

    @property
    def spot_filter(self) -> str:
        return ",".join([*self.context, self.situation])

    @property
    def filter(self) -> str:
        return ",".join([*self.context, f"{self.situation}={self.decision}"])

    def as_dict(self) -> dict:
        return {
            "situation": self.situation, "decision": self.decision, "level": self.level,
            "label": _LABEL[self.level, self.decision], "spot": _SPOT_WORDS[self.level],
            "method": self.method, "filter": self.filter, "spot_filter": self.spot_filter,
            "context": list(self.context), "ignored": list(self.ignored),
        }


def segments(expr: str | None) -> list[str]:
    """`expr` split at its top-level commas, a bracket kept whole."""
    out, depth, cur = [], 0, []
    for ch in expr or "":
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur).strip())
    return [s for s in out if s]


def _decision_of(seg: str) -> tuple[str, str, str | None] | None:
    m = _DECISION_TERM.match(seg)
    if m:
        return m.group(1), m.group(2), None
    return _ALIASES.get(seg)


def resolve_node(expr: str | None) -> Node:
    """The preflop decision `expr` names, or NoNode. The highest level named is the
    node; a lower one is history and stays in the spot."""
    segs = segments(expr)
    named = [(s, d) for s in segs if (d := _decision_of(s)) is not None]
    if not named:
        raise NoNode(NO_NODE)
    top = max(_LEVEL[d[0]] for _, d in named)
    at = [d for _, d in named if _LEVEL[d[0]] == top]
    decisions = {d[1] for d in at}
    if len(decisions) > 1:
        raise NoNode(f"two decisions at one spot ({', '.join(sorted(decisions))}): pick one, e.g. faced_open=call")
    decision = decisions.pop()
    situation = "faced_3bet" if any(d[0] == "faced_3bet" for d in at) else at[0][0]
    if decision not in ("raise", "call"):
        raise NoNode(f"an estimate needs a raise or a call, not a {decision}: e.g. {situation}=raise")
    node = Node(situation, decision)
    for s in segs:
        d = _decision_of(s)
        if d is not None:
            if _LEVEL[d[0]] < top:
                node.context.append(s)
            if d[2] and d[2] not in node.context:
                node.context.append(d[2])
        elif _before(s, top):
            node.context.append(s)
        else:
            node.ignored.append(s)
    return node


def _before(seg: str, level: int) -> bool:
    """A term settled before a decision at `level`: kept in the spot."""
    flag = seg.removeprefix("!")
    if flag in _LEVEL:
        return _LEVEL[flag] <= level
    if flag in _BEFORE_FLAGS:
        return True
    if seg.startswith("position=") and "(" not in seg:
        return True
    if _BEFORE_NUMERIC.match(seg):
        return True
    return level >= 2 and bool(_OPEN_BB.match(seg))


# --------------------------------------------------------- R1: the estimate --


def top_fill(pct: float) -> dict[str, float]:
    """The top `pct`% of combos along `HAND_RANKING`: class -> the share of its
    combos taken, the boundary class partly."""
    left = max(0.0, min(100.0, pct)) / 100 * COMBOS
    out: dict[str, float] = {}
    for label in HAND_RANKING:
        if left <= 1e-9:
            break
        c = combos_of(label)
        take = min(c, left)
        out[label] = take / c
        left -= take
    return out


def capped_fill(top_pct: float, slice_pct: float) -> dict[str, float]:
    """The top `top_pct`% less the top `slice_pct`%, class by class."""
    top, cut = top_fill(top_pct), top_fill(slice_pct)
    return {k: w - cut.get(k, 0.0) for k, w in top.items() if w - cut.get(k, 0.0) > 1e-9}


def combo_count(cells: Mapping[str, float]) -> float:
    return sum(w * combos_of(k) for k, w in cells.items())


def _family(label: str) -> str | None:
    """The family a class moves with (SPEC.md, "Estimated ranges"), or None."""
    hi, lo = RANK_VALUE[label[0]], RANK_VALUE[label[1]]
    if hi == lo:
        return "small pairs" if hi <= 6 else None
    if label[2] == "s":
        if hi == 14:
            return "suited aces" if lo <= 9 else None
        gap = hi - lo - 1
        if gap == 0 and hi <= 11:
            return "suited connectors"
        if gap in (1, 2) and lo < 10:
            return "suited gappers"
        return None
    if lo >= 10:
        return "offsuit broadways"
    return "offsuit aces" if hi == 14 else None


#: Family -> its classes, strongest first.
FAMILIES: dict[str, list[str]] = {}
for _label in HAND_RANKING:
    if (_f := _family(_label)) is not None:
        FAMILIES.setdefault(_f, []).append(_label)


#: Pseudo-hands of the linear guess behind each class's score in `calibrate`: the
#: shown hands outweigh it only once a class has been expected a few times.
PRIOR_HANDS = 3.0


def calibrate(cells: dict[str, float], shown: Counter) -> tuple[dict, list[dict], list[dict], bool]:
    """Refill the range by how often each class was shown, shrunk toward the
    linear guess `cells`. -> (cells, rows, demoted, calibrated).

    A class's score is (shown + PRIOR_HANDS × guess) / (expected + PRIOR_HANDS),
    where expected is how often it would be shown if it were always in the range
    (the shown hands spread over the range's combos). The range's combos are then
    filled again, highest score first, the ranking breaking ties. Few shown hands
    leave the guess as it is; many make the range follow what was played, and a
    class shown once in a thousand hands (a 72o for the bounty) stays out.
    """
    base = dict(cells)
    total = combo_count(base)
    n = sum(shown.values())
    out = dict(base)
    if n >= MIN_SHOWN and total > 0:
        def score(label: str) -> float:
            expected = n * combos_of(label) / total
            return (shown.get(label, 0) + PRIOR_HANDS * base.get(label, 0.0)) / (expected + PRIOR_HANDS)

        out = {}
        left = total
        for label in sorted(HAND_RANKING, key=lambda k: (-score(k), _INDEX[k])):
            if left <= 1e-9:
                break
            c = combos_of(label)
            take = min(c, left)
            out[label] = take / c
            left -= take
    out = {k: w for k, w in out.items() if w > 1e-9}
    lost = {k: (w - out.get(k, 0.0)) * combos_of(k) for k, w in base.items()}
    demoted = [{"class": k, "combos": round(v, 2)}
               for k, v in sorted(lost.items(), key=lambda kv: -_INDEX[kv[0]]) if v > 1e-6]
    moved_by = {k: [k] for k in out if base.get(k, 0) <= 1e-9}
    rows = [
        {
            "class": k,
            "shown": n,
            "outside": base.get(k, 0) <= 1e-9,
            "family": _family(k),
            "moved": moved_by.get(k, []),
        }
        for k, n in sorted(shown.items(), key=lambda kv: (-kv[1], _INDEX[kv[0]]))
    ]
    return out, rows, demoted, bool(moved_by or demoted)


def estimate_range(facts: Iterable[Facts], node: Node) -> dict:
    """The estimated range at `node` over `facts`, the spot's hands (already
    narrowed by `node.context`). See SPEC.md, "Estimated ranges"."""
    getter = DECIDED[node.situation]
    at = [(f, d) for f in facts if (d := getter(f)) is not None]
    counts = Counter(d for _, d in at)
    n = len(at)
    count = counts[node.decision]
    out: dict = {
        "node": node.as_dict(), "method": node.method, "n": n, "count": count,
        "counts": {k: counts.get(k, 0) for k in ("fold", "check", "call", "raise")},
        "pct": None, "ci": None, "pm": None, "top_pct": None, "slice_pct": None,
        "combos": 0.0, "cells": {}, "shown": 0, "outside": 0, "calibrated": False,
        "calibration": [], "demoted": [], "min_shown": MIN_SHOWN,
    }
    if not n:
        return out
    pct = 100.0 * count / n
    lo, hi = wilson(count, n)
    if node.method == "capped":
        slice_pct = 100.0 * counts["raise"] / n
        top_pct = pct + slice_pct
        cells = capped_fill(top_pct, slice_pct)
    else:
        slice_pct, top_pct = 0.0, pct
        cells = top_fill(pct)
    shown = Counter(hand_class(f.hole_cards) for f, d in at if d == node.decision and f.hole_cards)
    cells, rows, demoted, moved = calibrate(cells, shown)
    out.update({
        "pct": round(pct, 1),
        "ci": [round(100 * lo, 1), round(100 * hi, 1)],
        "pm": round(100 * (hi - lo) / 2),
        "top_pct": round(top_pct, 1),
        "slice_pct": round(slice_pct, 1),
        "combos": round(combo_count(cells), 1),
        "cells": {k: round(w, 3) for k, w in sorted(cells.items(), key=lambda kv: _INDEX[kv[0]])},
        "shown": sum(shown.values()),
        "outside": sum(r["shown"] for r in rows if r["outside"]),
        "calibrated": moved,
        "calibration": rows,
        "demoted": demoted,
    })
    return out


def estimate(
    facts: Iterable[Facts], expr: str | None, names: Mapping[str, str] | None = None, tags: TagResolver | None = None
) -> dict:
    """`estimate_range` at the node `expr` names, over one player's `facts` (every
    hand of theirs; the spot is applied here). NoNode or ValueError on a bad filter."""
    node = resolve_node(expr)
    pred = parse_filter(node.spot_filter, names, tags)
    return estimate_range([f for f in facts if pred(f)], node)


# --------------------------------------------------- R2: the board breakdown --

_DECK: tuple[Card, ...] = tuple(Card(v, s) for v in range(14, 1, -1) for s in "cdhs")
_HOLDINGS: dict[str, list[tuple[Card, Card]]] = {}
for _a, _b in itertools.combinations(_DECK, 2):
    _HOLDINGS.setdefault(hand_class([f"{VALUE_RANK[_a.value]}{_a.suit}", f"{VALUE_RANK[_b.value]}{_b.suit}"]),
                         []).append((_a, _b))
assert sum(len(v) for v in _HOLDINGS.values()) == COMBOS

VALUE_GROUPS = ("strong", "trips", "two_pair", "top_pair", "weak_pair")
_LEAF_ORDER = {leaf: i for i, leaf in enumerate(MADE_LEAVES)}


def _text(c: Card) -> str:
    return f"{VALUE_RANK[c.value]}{c.suit}"


def parse_board(text: str) -> tuple[str, ...]:
    """``"Ah7c2d"`` -> ``("Ah", "7c", "2d")``; ValueError unless 3 to 5 distinct cards."""
    try:
        cards = parse_cards(text.strip())
    except ValueError as exc:
        raise ValueError(f"{exc}: a board is 3 to 5 cards, e.g. Ah7c2d") from exc
    if not 3 <= len(cards) <= 5:
        raise ValueError(f"a board is 3 to 5 cards, e.g. Ah7c2d, not {len(cards)}")
    if len(set(cards)) != len(cards):
        raise ValueError("a card is on the board twice")
    return tuple(_text(c) for c in cards)


class _Table:
    """One board's holdings, read a class at a time: class -> [(leaf, hand
    strength)] for each of its combos the board leaves. Range-independent, so
    every estimate on the board shares what it has read."""

    __slots__ = ("board", "by_card", "classes", "every", "hs", "leaves", "relevant", "used")

    def __init__(self, board: tuple[str, ...]) -> None:
        cards = parse_cards(list(board))
        self.board = list(board)
        self.used = set(cards)
        self.hs = _strength_table(tuple(sorted(cards)))
        # Only a suit with two board cards can make a flush or a flush draw with
        # the hole cards, so a holding's class reads its other suits as one: one
        # `made_hand` per rank pair and relevant suits, not per combo.
        self.relevant = {s for s, k in Counter(c.suit for c in cards).items() if k >= 2}
        self.leaves: dict[tuple, str] = {}
        # `strength._Board.count` for every holding at once: the holdings that
        # share a card with it are read off one sorted list per card, not walked.
        self.by_card = {c: sorted(self.hs.rank[i] for i in idx) for c, idx in self.hs.with_card.items()}
        self.every = len(self.hs.rank)
        self.classes: dict[str, list[tuple[str, float]]] = {}

    def __getitem__(self, label: str) -> list[tuple[str, float]]:
        hit = self.classes.get(label)
        if hit is not None:
            return hit
        hs, rel, rows = self.hs, self.relevant, []
        for a, b in _HOLDINGS[label]:
            if a in self.used or b in self.used:
                continue
            k = (a.value, a.suit if a.suit in rel else "", b.value, b.suit if b.suit in rel else "")
            leaf = self.leaves.get(k)
            if leaf is None:
                leaf = self.leaves[k] = leaf_of(made_hand([_text(a), _text(b)], self.board))
            me = hs.rank[hs.index[frozenset((a, b))]]
            ra, rb = self.by_card[a], self.by_card[b]
            lo, hi = bisect.bisect_left(hs.sorted, me), bisect.bisect_right(hs.sorted, me)
            la, lb = bisect.bisect_left(ra, me), bisect.bisect_left(rb, me)
            wins = lo - la - lb
            # The holding itself is on both cards' lists: its tie comes off once.
            ties = hi - lo - (bisect.bisect_right(ra, me) - la) - (bisect.bisect_right(rb, me) - lb) + 1
            n = self.every - len(ra) - len(rb) + 1
            rows.append((leaf, (2 * wins + ties) / (2 * n)))
        self.classes[label] = rows
        return rows


_TABLES: LRU[tuple[str, ...], _Table] = LRU(64)


def _table(board: tuple[str, ...]) -> _Table:
    # The flop's order never matters; the turn's does, since draws are read on it.
    key = (*sorted(board[:3]), *board[3:])
    hit = _TABLES.get(key)
    if hit is None:
        hit = _TABLES[key] = _Table(tuple(board))
    return hit


def _mix(weights: Mapping[str, float]) -> list[dict]:
    """Leaf weights as the groups, strongest first: combos, pct and leaves."""
    total = sum(weights.values())
    out = []
    for key, label, _, leaves in GROUPS:
        c = sum(weights.get(x, 0.0) for x in leaves)
        out.append({
            "group": key, "label": label, "combos": round(c, 2),
            "pct": round(100 * c / total, 1) if total else None,
            "leaves": [
                {"leaf": x, "combos": round(weights[x], 2), "pct": round(100 * weights[x] / total, 1)}
                for x in leaves if weights.get(x, 0) > 1e-9
            ],
        })
    return out


def _share(weights: Mapping[str, float], groups: Iterable[str]) -> float | None:
    total = sum(weights.values())
    want = set(groups)
    return sum(w for x, w in weights.items() if GROUP_OF[x] in want) / total if total else None


def board_breakdown(
    range_cells: Mapping[str, float], board: Iterable[str], *, next_freq: float | None = None,
    street: str | None = None,
) -> dict:
    """The range's combos on `board`, card removal applied, by made-hand group.

    With `next_freq` (a percentage), also `linear_cut`: the strongest that share of
    the combos by hand strength, the leaf the cut falls in and the cut range's mix.
    `street` is the board's own by default.
    """
    board = tuple(board)
    street = street or street_of_board(board)
    table = _table(board)
    rows: list[tuple[float, float, str]] = []
    leaves: Counter = Counter()
    for label, w in range_cells.items():
        if w <= 0:
            continue
        for leaf, hs in table[label]:
            rows.append((hs, w, leaf))
            leaves[leaf] += w
    total = sum(leaves.values())
    value = _share(leaves, VALUE_GROUPS)
    out = {
        "board": list(board), "street": street, "combos": round(total, 1),
        "categories": _mix(leaves), "value_pct": None if value is None else round(100 * value, 1),
        "linear_cut": None,
    }
    if next_freq is None or not total:
        return out
    rows.sort(key=lambda r: (-r[0], _LEAF_ORDER[r[2]]))
    want = max(0.0, min(100.0, next_freq)) / 100 * total
    bet: Counter = Counter()
    got = 0.0
    at = None
    for hs, w, leaf in rows:
        if got >= want - 1e-9:
            break
        take = min(w, want - got)
        bet[leaf] += take
        got += take
        at = (leaf, hs)
    bluff = _share(bet, ("draw", "air"))
    out["linear_cut"] = {
        "pct": round(next_freq, 1),
        "at": at[0] if at else None,
        "group": GROUP_OF[at[0]] if at else None,
        "hs": round(at[1], 3) if at else None,
        "combos": round(got, 1),
        "mix": _mix(bet),
        "bluff_pct": None if bluff is None else round(100 * bluff, 1),
    }
    return out


def _average(parts: list[dict]) -> dict:
    """Breakdowns over several boards, each weighed alike."""
    if len(parts) == 1:
        return parts[0]
    k = len(parts)

    def avg_mix(mixes: list[list[dict]]) -> list[dict]:
        out = []
        for i, row in enumerate(mixes[0]):
            rows = [m[i] for m in mixes]
            pcts = [r["pct"] for r in rows if r["pct"] is not None]
            leaves: Counter = Counter()
            for r in rows:
                for x in r["leaves"]:
                    leaves[x["leaf"]] += x["pct"] / k
            out.append({
                "group": row["group"], "label": row["label"],
                "combos": round(sum(r["combos"] for r in rows) / k, 2),
                "pct": round(sum(pcts) / k, 1) if pcts else None,
                "leaves": [{"leaf": x, "pct": round(leaves[x], 1)} for x in MADE_LEAVES if leaves.get(x, 0) > 0.05],
            })
        return out

    values = [p["value_pct"] for p in parts if p["value_pct"] is not None]
    out = {
        "board": None, "street": parts[0]["street"], "combos": round(sum(p["combos"] for p in parts) / k, 1),
        "categories": avg_mix([p["categories"] for p in parts]),
        "value_pct": round(sum(values) / len(values), 1) if values else None,
        "linear_cut": None,
    }
    cuts = [p["linear_cut"] for p in parts if p["linear_cut"]]
    if cuts:
        at = Counter(c["at"] for c in cuts if c["at"]).most_common(1)
        bluffs = [c["bluff_pct"] for c in cuts if c["bluff_pct"] is not None]
        out["linear_cut"] = {
            "pct": cuts[0]["pct"],
            "at": at[0][0] if at else None,
            "group": GROUP_OF[at[0][0]] if at else None,
            "hs": round(sum(c["hs"] for c in cuts if c["hs"] is not None) / len(cuts), 3),
            "combos": round(sum(c["combos"] for c in cuts) / len(cuts), 1),
            "mix": avg_mix([c["mix"] for c in cuts]),
            "bluff_pct": round(sum(bluffs) / len(bluffs), 1) if bluffs else None,
        }
    return out


def flop_terms(expr: str | None) -> list[tuple[str, bool]]:
    """The spot's top-level flop texture terms: (tag, wanted)."""
    out = []
    for s in segments(expr):
        m = _FLOP_TEXTURE.match(s)
        if m:
            out.append((m.group(2), m.group(1) == "="))
    return out


def sample_flops(terms: list[tuple[str, bool]], n: int = SAMPLE_FLOPS, tries: int = 20000) -> list[tuple[str, ...]]:
    """Up to `n` distinct random flops with every texture term, seeded by the terms."""
    rng = random.Random(",".join(f"{t}{'=' if w else '!='}" for t, w in sorted(terms)))
    deck = [_text(c) for c in _DECK]
    out: list[tuple[str, ...]] = []
    seen: set[frozenset] = set()
    for _ in range(tries):
        if len(out) >= n:
            break
        flop = tuple(rng.sample(deck, 3))
        key = frozenset(flop)
        if key in seen:
            continue
        tags = board_texture(flop)
        if all((t in tags) == w for t, w in terms):
            seen.add(key)
            out.append(flop)
    return out


def next_action(facts: Iterable[Facts], node: Node, street: str) -> dict | None:
    """The player's action after the node on `street`, over the spot's hands: its
    rate, sample and interval, and the shown hands that took it. None after a limp."""
    facts = list(facts)
    if node.decision == "raise":
        opp = [f for f in facts if f.cbet_opp.get(street)]
        took = [f for f in opp if f.cbet.get(street)]
        label = f"c-bet {street}"
    elif node.level >= 2:
        opp = [f for f in facts if f.fold_to_cbet_opp.get(street)]
        took = [f for f in opp if not f.fold_to_cbet.get(street)]
        label = f"continue vs {street} c-bet"
    else:
        return None
    ci = wilson(len(took), len(opp))
    leaves: Counter = Counter()
    for f in took:
        if f.hole_cards:
            mh = made_at(f.hole_cards, f.board, street)
            if mh is not None:
                leaves[leaf_of(mh)] += 1
    bluff = _share(leaves, ("draw", "air"))
    return {
        "label": label, "n": len(opp), "count": len(took),
        "pct": round(100 * len(took) / len(opp), 1) if opp else None,
        "ci": [round(100 * ci[0], 1), round(100 * ci[1], 1)] if ci else None,
        "shown": sum(leaves.values()),
        "shown_mix": {"n": sum(leaves.values()), "mix": _mix(leaves),
                      "bluff_pct": None if bluff is None else round(100 * bluff, 1)},
    }


def implied_bluffs(value_pct: float | None, bet_pct: float | None, shown_bluff_pct: float | None,
                   combos: float) -> dict | None:
    """The bluffs the showdowns do not show: `floor = 1 - V/X` less the shown share."""
    if value_pct is None or not bet_pct:
        return None
    v, x = value_pct / 100, bet_pct / 100
    floor = max(0.0, 1 - v / x)
    gap = None if shown_bluff_pct is None else max(0.0, floor - shown_bluff_pct / 100)
    return {
        "pct": None if gap is None else round(100 * gap, 1),
        "floor_pct": round(100 * floor, 1),
        "shown_pct": shown_bluff_pct,
        "value_pct": value_pct,
        "bet_pct": bet_pct,
        "bet_combos": round(x * combos, 1),
        "value_combos": round(v * combos, 1),
    }


NOTES = (
    "An estimate: the top X% by hand ranking, cut linearly.",
    "Implied unseen = at least 1 − value/bet share are draws or air, less the shown share.",
)


def board_report(
    facts: Iterable[Facts],
    spot: Iterable[Facts],
    expr: str | None,
    board: str | None,
    names: Mapping[str, str] | None = None,
    tags: TagResolver | None = None,
) -> dict:
    """The estimate at `expr`'s node, broken down on `board` or on flops sampled
    from the spot's flop texture. `facts` are every hand of the player's, `spot`
    the ones `expr` finds. NoNode, or ValueError with the reason, when it cannot."""
    facts = list(facts)
    est = estimate(facts, expr, names, tags)
    if not est["cells"]:
        raise ValueError(f"no hands at this spot to estimate from ({est['node']['spot_filter']})")
    node = resolve_node(expr)
    terms = flop_terms(expr)
    if board:
        boards = [parse_board(board)]
    elif terms:
        boards = sample_flops(terms)
        if not boards:
            raise ValueError("no flop has every flop texture in the spot")
    else:
        raise ValueError("type a board, e.g. Ah7c2d, or add a flop texture to the spot, e.g. flop=ace_high")
    street = street_of_board(boards[0])
    spot = list(spot)
    texture: list[str] = []
    nxt = next_action(spot, node, street)
    if board and nxt is not None:
        tex = texture_terms(boards[0], street)
        for k in range(len(tex), 0, -1):
            pred = parse_filter(",".join(tex[:k]))
            narrowed = next_action([f for f in spot if pred(f)], node, street)
            if narrowed is not None and narrowed["shown"] >= MIN_SHOWN:
                nxt, texture = narrowed, tex[:k]
                break
    freq = nxt["pct"] if nxt else None
    out = _average([board_breakdown(est["cells"], b, next_freq=freq, street=street) for b in boards])
    shown_bluff = nxt["shown_mix"]["bluff_pct"] if nxt else None
    return {
        **out,
        "boards": [list(b) for b in boards],
        "sampled": not board,
        "flop_terms": [f"flop{'=' if w else '!='}{t}" for t, w in terms],
        "texture": texture,
        "estimate": {k: est[k] for k in ("node", "method", "n", "count", "pct", "pm", "combos", "calibrated")},
        "next": nxt,
        "implied_unshown_bluffs": implied_bluffs(out["value_pct"], freq, shown_bluff, out["combos"]),
        "notes": list(NOTES),
    }
