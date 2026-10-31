"""Group by: one spot's hands split by a dimension, or two crossed.

HM3's report grouping. `Holdem Manager` shows a player by position, by pot type,
by stack depth, and lets you cross two of them; this does the same over `Facts`.
Each **cell** is the hands in one bucket (or one pair of buckets) and carries the
filter term that finds exactly them, so `<spot>,<term>` reproduces it on any page
and a click on a row narrows the spot to it. `tests/test_groupby.py` holds every
dimension's terms to that.

One pass over the facts per request: each hand is bucketed once per dimension and
added to its cells' tallies. See SPEC.md, "Group by".
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from itertools import product

from ..logfmt.events import STREET_INDEX
from ..logfmt.parser import position_name
from .cards import (
    DRAW_DETAILS,
    HAND_RANKING,
    MADE_GROUPS,
    board_texture,
    hand_class,
    hand_pct,
    leaf_of,
    made_at,
)
from .derive import POSTFLOP_STREETS as STREETS
from .derive import SIZE_BUCKETS, TEMPO_BUCKETS, Facts
from .queries import Tally, rates
from .results import POT_TYPES, SEAT_ORDER
from .strength import TIERS, tier_of

Bucket = str
#: A dimension's buckets for one hand: one, several (`vs`), or None, not applicable.
Key = Bucket | list[Bucket] | None


@dataclass(frozen=True)
class Context:
    """What some dimensions need beyond the hand: who each pn_id is."""

    #: pn_id -> display name, as `queries.display_names`; what `vs=` matches on.
    names: Mapping[str, str] = field(default_factory=dict)
    #: The player a row belongs to, in pool mode; None for one player's hands.
    player_of: Callable[[Facts], str] | None = None


@dataclass(frozen=True)
class Dim:
    name: str
    label: str
    #: The hand's bucket(s), or None where the dimension does not apply.
    key: Callable[[Facts, Context], Key]
    #: The filter term that selects a bucket's hands, given the same spot.
    term: Callable[[Bucket], str]
    #: The buckets' display order; None sorts by hands, most first.
    order: tuple[str, ...] | None = None
    #: Only over the pool: a player's own hands have one player.
    pool_only: bool = False
    #: Folds a bucket to the identity two spellings share (`vs` compares casefolded).
    ident: Callable[[Bucket], Bucket] = lambda b: b


# --- the dimensions' keys -------------------------------------------------------


def _seat(f: Facts, _: Context) -> Key:
    """The `position=` filter's own label, as `results._seat`: a dead small blind
    keeps its label, so the cell reproduces from its term; a dead button has none."""
    if f.seats_from_button is None or f.dead_button:
        return None
    return position_name(f.seats_from_button, f.n_dealt_in, f.position_slots)


#: Results' pot types, under the names the pot-type flags have.
_POT_NAMES = ("walk", "limped", "srp", "3bet_pot", "4bet_pot", "bomb_pot", "double_board", "plo")
_POT_TERMS = {name: term for name, (_, _, term, _) in zip(_POT_NAMES, POT_TYPES, strict=True)}
_POT_TESTS = tuple(zip(_POT_NAMES, (test for *_, test in POT_TYPES), strict=True))


def _pot_type(f: Facts, _: Context) -> Key:
    return next((name for name, test in _POT_TESTS if test(f)), None)


def _players_flop(f: Facts, _: Context) -> Key:
    n = f.players_flop
    if n is None:
        return None
    return "3+" if n >= 3 else str(n)


#: The stats page's depth split (SPEC.md, "Stack-depth splits"): bucket -> term.
_DEPTH = {
    "<40": "eff_bb<40",
    "40-100": "eff_bb>=40,eff_bb<100",
    "100-200": "eff_bb>=100,eff_bb<200",
    "200+": "eff_bb>=200",
}


def _depth(f: Facts, _: Context) -> Key:
    d = f.eff_bb
    if d is None:
        return None
    return "<40" if d < 40 else "40-100" if d < 100 else "100-200" if d < 200 else "200+"


def _vs(f: Facts, ctx: Context) -> Key:
    return [ctx.names.get(p, p) for p in f.opponents] or None


def _player(f: Facts, ctx: Context) -> Key:
    if ctx.player_of is None:
        raise ValueError("group by player needs the pool: leave the player out")
    return ctx.player_of(f)


#: The made-hand buckets, strongest first: each `MADE_GROUPS` group, with `pair`
#: opened into its leaves (SPEC.md, "Group by", call 5). bucket -> its leaves.
MADE_BUCKETS: dict[str, tuple[str, ...]] = {}
for _g, _leaves in MADE_GROUPS.items():
    if _g == "pair":
        MADE_BUCKETS.update({leaf: (leaf,) for leaf in _leaves})
    else:
        MADE_BUCKETS[_g] = _leaves
_BUCKET_OF_LEAF = {leaf: b for b, leaves in MADE_BUCKETS.items() for leaf in leaves}

def made_leaf(f: Facts, street: str) -> str | None:
    """Their made hand on `street`'s board, gated as a `made_<street>` term gates
    it: cards known, the board got there, and they had not folded before it."""
    if f.hole_cards is None:
        return None
    if f.fold_street is not None and STREET_INDEX[f.fold_street] < STREET_INDEX[street]:
        return None
    mh = made_at(f.hole_cards, f.board, street)
    return None if mh is None else leaf_of(mh)


def _made_term(street: str) -> Callable[[Bucket], str]:
    def term(b: Bucket) -> str:
        # The `trips` group spelled out: as a filter name `trips` is the leaf alone.
        return f"made_{street}={'|'.join(MADE_BUCKETS[b]) if b == 'trips' else b}"

    return term


def _flop_texture(f: Facts, _: Context) -> Key:
    """One bucket per flop. A paired flop is never monotone, so narrowing twotone
    and rainbow to unpaired boards makes the four partition every flop."""
    if len(f.board) < 3:
        return None
    tags = board_texture(f.board[:3])
    return next(t for t in ("paired", "monotone", "twotone", "rainbow") if t in tags)


_TEXTURE_TERMS = {
    "paired": "flop=paired",
    "monotone": "flop=monotone",
    "twotone": "flop=twotone,flop=unpaired",
    "rainbow": "flop=rainbow,flop=unpaired",
}

#: `hand_pct` bands, upper bound in: bucket -> (low, high).
_RANK_BANDS = {"0-5": (0, 5), "5-15": (5, 15), "15-30": (15, 30), "30-50": (30, 50), "50-100": (50, 100)}


def _hand_rank(f: Facts, _: Context) -> Key:
    if not f.hole_cards:
        return None
    pct = hand_pct(hand_class(f.hole_cards))
    return next(b for b, (lo, hi) in _RANK_BANDS.items() if pct <= hi or hi == 100)


def _hand_rank_term(b: Bucket) -> str:
    lo, hi = _RANK_BANDS[b]
    if lo == 0:
        return f"hand_pct<={hi}"
    return f"hand_pct>{lo}" if hi == 100 else f"hand_pct>{lo},hand_pct<={hi}"


# --- the registry ---------------------------------------------------------------


def _registry() -> dict[str, Dim]:
    dims = [
        Dim("position", "Position", _seat, lambda b: f"position={b}", SEAT_ORDER),
        Dim("pot_type", "Pot type", _pot_type, _POT_TERMS.__getitem__, _POT_NAMES),
        Dim("players_flop", "Players on flop", _players_flop,
            lambda b: "players_flop>=3" if b == "3+" else f"players_flop={b}", ("1", "2", "3+")),
        Dim("depth", "Stack depth", _depth, _DEPTH.__getitem__, tuple(_DEPTH)),
        Dim("vs", "Opponent", _vs, lambda b: f"vs={b}", ident=str.casefold),
        Dim("player", "Player", _player, lambda b: "", pool_only=True),
    ]
    for s in STREETS:
        dims.append(Dim(f"line_{s}", f"Line {s}", lambda f, _, s=s: f.line.get(s), lambda b, s=s: f"line_{s}={b}"))
    for s in STREETS:
        dims.append(Dim(
            f"made_{s}", f"Made {s}", lambda f, _, s=s: _BUCKET_OF_LEAF.get(made_leaf(f, s)),
            _made_term(s), tuple(MADE_BUCKETS),
        ))
    for s in STREETS:
        dims.append(Dim(f"tier_{s}", f"Tier {s}", lambda f, _, s=s: tier_of(f, s), lambda b, s=s: f"tier_{s}={b}", TIERS))
    dims.append(Dim("flop_texture", "Flop texture", _flop_texture, _TEXTURE_TERMS.__getitem__, tuple(_TEXTURE_TERMS)))
    for s in STREETS:
        dims.append(Dim(
            f"cbet_size_{s}", f"C-bet size {s}",
            lambda f, _, s=s: f.bet_size.get(s) if f.cbet.get(s) else None,
            lambda b, s=s: f"cbet_{s}={b}", SIZE_BUCKETS,
        ))
    for s in STREETS:
        dims.append(Dim(
            f"bet_size_{s}", f"Bet size {s}", lambda f, _, s=s: f.bet_size.get(s),
            lambda b, s=s: f"bet_{s}={b}", SIZE_BUCKETS,
        ))
    for s in ("preflop", *STREETS):
        dims.append(Dim(
            f"tempo_{s}", f"Tempo {s}", lambda f, _, s=s: f.tempo.get(s),
            lambda b, s=s: f"tempo_{s}={b}", TEMPO_BUCKETS,
        ))
    dims.append(Dim(
        "hand_class", "Hand", lambda f, _: hand_class(f.hole_cards) if f.hole_cards else None,
        lambda b: f"hand={b}", HAND_RANKING,
    ))
    dims.append(Dim("hand_rank", "Hand rank", _hand_rank, _hand_rank_term, tuple(_RANK_BANDS)))
    return {d.name: d for d in dims}


#: Every dimension, in the order a page lists them.
DIMENSIONS: dict[str, Dim] = _registry()


def catalog(pool: bool = True) -> list[dict]:
    """The dimensions a page can offer: `{name, label}`, the pool-only ones only
    when `pool`."""
    return [{"name": d.name, "label": d.label} for d in DIMENSIONS.values() if pool or not d.pool_only]


def check_dims(names: Iterable[str], pool: bool = True) -> list[Dim]:
    """The named dimensions, or ValueError: one or two, known, distinct, and
    `player` only over the pool."""
    names = list(names)
    if not 1 <= len(names) <= 2:
        raise ValueError("group by one dimension, or two: by=position or by=position,pot_type")
    if len(set(names)) != len(names):
        raise ValueError(f"group by {names[0]!r} twice")
    out = []
    for n in names:
        d = DIMENSIONS.get(n)
        if d is None:
            raise ValueError(f"unknown dimension {n!r}. Known: {', '.join(DIMENSIONS)}")
        if d.pool_only and not pool:
            raise ValueError(f"group by {n!r} needs the pool: leave the player out")
        out.append(d)
    return out


# --- the cells ------------------------------------------------------------------

#: The made-hand mix a cell's bar shows: coarse groups of `MADE_LEAVES`, strongest
#: first. See SPEC.md, "Group by".
MIX_GROUPS: dict[str, tuple[str, ...]] = {
    "two_pair+": ("straight_flush", "quads", "full_house", "flush", "straight", "set", "trips", "two_pair"),
    "top_pair+": ("overpair", "top_pair"),
    "weak_pair": ("pocket_pair", "middle_pair", "bottom_pair", "board_pair"),
    "draw": DRAW_DETAILS,
    "air": ("high_card",),
}
_MIX_OF_LEAF = {leaf: g for g, leaves in MIX_GROUPS.items() for leaf in leaves}


def _rate_keys(street: str) -> tuple[str, ...]:
    return ("vpip", "pfr", "3bet", f"cbet_{street}", f"fold_to_cbet_{street}", f"af_{street}", "wtsd", "wsd")


class _Cell:
    __slots__ = ("mix", "shown", "tally")

    def __init__(self) -> None:
        self.tally = Tally()
        self.shown = 0
        self.mix: Counter = Counter()

    def add(self, f: Facts, mix: str | None) -> None:
        self.tally.add(f)
        if f.hole_cards is not None:
            self.shown += 1
        if mix is not None:
            self.mix[mix] += 1

    def out(self, street: str) -> dict:
        t = self.tally
        r = rates(t)
        c = t.counts
        # `rates` keeps no `_opp` for pfr and wsd: PFR's is VPIP's, W$SD's the showdowns.
        opp = {**r.get("_opp", {}), "pfr": c["pfr_opp"], "wsd": c["wtsd"]}
        keys = _rate_keys(street)
        known = sum(self.mix.values())
        return {
            "hands": t.hands,
            "shown": self.shown,
            # The hands behind every bb figure: complete, with a known big blind.
            "bb_hands": t.bb_n,
            "net_bb": r.get("net_bb"),
            "bb_per_100": r.get("bb_per_100"),
            "bb_per_100_ci": r.get("bb_per_100_ci"),
            "bb_per_100_vs_fold": r.get("bb_per_100_vs_fold"),
            "bb_per_100_vs_fold_ci": r.get("bb_per_100_vs_fold_ci"),
            "rates": {**{k: r.get(k) for k in keys}, "_opp": {k: opp.get(k, 0) for k in keys}},
            "mix": {
                "street": street,
                "known": known,
                "groups": [
                    {"group": g, "n": self.mix[g], "pct": round(100.0 * self.mix[g] / known, 1)}
                    for g in MIX_GROUPS if self.mix[g]
                ],
            },
        }


def _mix_of(f: Facts, street: str) -> str | None:
    leaf = made_leaf(f, street)
    return None if leaf is None else _MIX_OF_LEAF[leaf]


def _buckets(d: Dim, f: Facts, ctx: Context) -> list[Bucket]:
    """The hand's buckets on `d`, each once: [] where `d` does not apply."""
    k = d.key(f, ctx)
    if k is None:
        return []
    return list(dict.fromkeys(k)) if isinstance(k, list) else [k]


def group(
    facts: Iterable[Facts],
    dims: list[str],
    *,
    street: str = "flop",
    min_hands: int = 0,
    names: Mapping[str, str] | None = None,
    player_of: Callable[[Facts], str] | None = None,
) -> dict:
    """Split `facts` by one or two dimensions. See SPEC.md, "Group by".

    `names` is pn_id -> display name, for `vs`; `player_of` names each row's
    player, and is what allows `player` (pool mode). A hand a dimension does not
    apply to is counted in `skipped` and goes in no cell; cells under `min_hands`
    are left out and counted in `hidden`. Raises ValueError on a bad dimension or
    street.
    """
    if street not in STREETS:
        raise ValueError(f"unknown street {street!r}. Known: {', '.join(STREETS)}")
    chosen = check_dims(dims, pool=player_of is not None)
    ctx = Context(names=names or {}, player_of=player_of)

    total = _Cell()
    cells: dict[tuple[Bucket, ...], _Cell] = {}
    labels: list[dict[Bucket, Bucket]] = [{} for _ in chosen]
    skipped = 0
    for f in facts:
        mix = _mix_of(f, street)
        total.add(f, mix)
        per_dim = []
        for i, d in enumerate(chosen):
            raw = _buckets(d, f, ctx)
            # Several spellings of one bucket (`vs`: Alex, alex) are one cell,
            # shown under the first spelling met.
            idents = []
            for b in raw:
                k = d.ident(b)
                labels[i].setdefault(k, b)
                idents.append(k)
            per_dim.append(list(dict.fromkeys(idents)))
        if not all(per_dim):
            skipped += 1
            continue
        for keys in product(*per_dim):
            cell = cells.get(keys)
            if cell is None:
                cell = cells[keys] = _Cell()
            cell.add(f, mix)

    # Each dimension's own hand count per bucket, for the dimensions with no order.
    marginal: list[Counter] = [Counter() for _ in chosen]
    for keys, cell in cells.items():
        for i, k in enumerate(keys):
            marginal[i][k] += cell.tally.hands

    def sort_key(item: tuple[tuple[Bucket, ...], _Cell]) -> tuple:
        out: list = []
        for i, (d, k) in enumerate(zip(chosen, item[0], strict=True)):
            label = labels[i][k]
            if d.order is not None:
                pos = d.order.index(label) if label in d.order else len(d.order)
                out.append((pos, 0, label))
            else:
                out.append((0, -marginal[i][k], label))
        return tuple(out)

    rows = []
    hidden_cells = hidden_hands = 0
    for keys, cell in sorted(cells.items(), key=sort_key):
        if cell.tally.hands < min_hands:
            hidden_cells += 1
            hidden_hands += cell.tally.hands
            continue
        shown = [labels[i][k] for i, k in enumerate(keys)]
        terms = [d.term(b) for d, b in zip(chosen, shown, strict=True)]
        row = {"keys": shown, "term": ",".join(t for t in terms if t), **cell.out(street)}
        if any(d.name == "player" for d in chosen):
            row["player"] = shown[[d.name for d in chosen].index("player")]
        rows.append(row)

    return {
        "by": [{"name": d.name, "label": d.label, "order": list(d.order) if d.order else None} for d in chosen],
        "street": street,
        "min": min_hands,
        "total": {"keys": [], "term": "", **total.out(street)},
        "skipped": skipped,
        "hidden": {"cells": hidden_cells, "hands": hidden_hands},
        "cells": rows,
    }
