"""The pool: every player's tag counts, so a player is judged against their table.

A fixed cut ("folds to a c-bet 50% or more") reads a different thing in every
game: a loose home game folds less than an online pool, and its fish look
normal next to each other. So each tag rule is held against the *pool* -- the
median of the other players' rates, each player once -- and fires on a player
who stands out from it (`tags._verdict`; SPEC.md, "Pool baseline").

The counts are `tags.counts`, per PokerNow ID per game, derived only where a
game's stamp moved (the same contract as `queries._tallies`), and summed per
player on read. The pool is remembered per derivation generation, so a HUD poll
costs a lookup and a new hand costs its own game.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, OrderedDict, defaultdict
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field

from ..db.conn import generation, path_of, reading
from .derive import Facts, derive
from .queries import _game_stamps, identity_map, load_hands
from .tags import RuleBase, baseline, counts
from .tilt import annotate

#: Per (database, game): (that game's stamp, each PokerNow ID's `tags.counts`).
_GAME_COUNTS: dict[tuple[str, str], tuple[int, dict[str, Counter]]] = {}
#: Per (database, game scope): (generation, the Pool).
_POOL: dict[tuple[str, str | None], tuple[int | None, Pool]] = {}
#: Pools built over a filtered spot, newest last: (database, generation, game, filter).
_SPOT_POOLS: OrderedDict[tuple, Pool] = OrderedDict()
_SPOT_MAX = 8


@dataclass
class Pool:
    """Every player's counts over one scope, and whose logs show every card."""

    scope: str
    players: dict[str, Counter]
    owners: frozenset[tuple[str, str]] = frozenset()
    _bases: dict[str | None, dict[str, RuleBase]] = field(default_factory=dict, repr=False)

    def baseline(self, exclude: str | None = None) -> dict[str, RuleBase]:
        """Each measure's baseline without `exclude`, remembered per player."""
        hit = self._bases.get(exclude)
        if hit is None:
            hit = self._bases[exclude] = baseline(self.players, exclude)
        return hit


def owners(conn: sqlite3.Connection) -> frozenset[tuple[str, str]]:
    """(game, PokerNow ID) of each log's owner, whose every card is shown there."""
    return frozenset(
        (g, h) for g, h in conn.execute("SELECT game_id, hero_pn_id FROM games WHERE hero_pn_id IS NOT NULL")
    )


def _counts_by_id(facts: Iterable[Facts], own: Collection[tuple[str, str]]) -> dict[str, Counter]:
    by: dict[str, list[Facts]] = defaultdict(list)
    for f in facts:
        by[f.pn_id].append(f)
    return {pn: counts(annotate(fs), own) for pn, fs in by.items()}


def _game_counts(conn: sqlite3.Connection, game: str | None, own: frozenset) -> list[dict[str, Counter]]:
    """Per-game counts, derived only where a game's stamp moved."""
    path = path_of(conn)
    stamps = _game_stamps(conn, game) if path else None
    if stamps is None:
        return [_counts_by_id((f for h in load_hands(conn, game) for f in derive(h)), own)]
    out = []
    for gid, stamp in stamps.items():
        hit = _GAME_COUNTS.get((path, gid))
        if hit is None or hit[0] != stamp:
            facts = [f for h in load_hands(conn, gid) for f in derive(h)]
            hit = _GAME_COUNTS[path, gid] = (stamp, _counts_by_id(facts, own))
        out.append(hit[1])
    if game is None:
        for key in [k for k in _GAME_COUNTS if k[0] == path and k[1] not in stamps]:
            del _GAME_COUNTS[key]
    return out


def pool_for(conn: sqlite3.Connection, game: str | None = None) -> Pool:
    """The pool over every hand, or one game's: each player's counts, by alias."""
    path, gen = path_of(conn), generation(conn)
    hit = _POOL.get((path, game))
    if hit is not None and gen is not None and hit[0] == gen:
        return hit[1]
    with reading(conn):
        own = owners(conn)
        ident = identity_map(conn)
        players: dict[str, Counter] = defaultdict(Counter)
        for per_game in _game_counts(conn, game, own):
            for pn, c in per_game.items():
                players[ident.get(pn, (-1, pn))[1]].update(c)
    pool = Pool(game or "lifetime", dict(players), own)
    if path:
        _POOL[path, game] = (gen, pool)
    return pool


def pool_from_facts(
    grouped: Mapping[str, list[Facts]], own: Collection[tuple[str, str]] = (), scope: str = "spot",
    key: tuple | None = None,
) -> Pool:
    """A pool over facts already in hand, by alias: a filtered spot's. `key`, when
    given, remembers the last few so a spot's tags do not re-count every player."""
    if key is not None and key in _SPOT_POOLS:
        _SPOT_POOLS.move_to_end(key)
        return _SPOT_POOLS[key]
    pool = Pool(scope, {a: counts(fs, own) for a, fs in grouped.items() if fs}, frozenset(own))
    if key is not None:
        _SPOT_POOLS[key] = pool
        while len(_SPOT_POOLS) > _SPOT_MAX:
            _SPOT_POOLS.popitem(last=False)
    return pool


def clear() -> None:
    """Forget every remembered pool and count. For tests."""
    _GAME_COUNTS.clear()
    _POOL.clear()
    _SPOT_POOLS.clear()
