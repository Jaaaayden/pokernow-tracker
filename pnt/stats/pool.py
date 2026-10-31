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
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field

from ..db.conn import generation, path_of, reading
from .derive import Facts, derive
from .lru import LRU
from .queries import _game_stamps, identity_map, load_hands
from .tags import RuleBase, baseline, counts
from .tilt import annotate

#: Per (database, game): (that game's stamp, each PokerNow ID's `tags.counts`).
_GAME_COUNTS: LRU[tuple[str, str], tuple[int, dict[str, Counter]]] = LRU()
#: Per (database, game scope): (generation, the Pool). Capped: the HUD asks for
#: every table it sits at, and an evening's games would otherwise stay forever.
_POOL: LRU[tuple[str, str | None], tuple[int | None, Pool]] = LRU(32)


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
    # Sessions numbered over every hand, as a player's own are (`play_facts`); the
    # baseline counts the analysed ones only.
    played = {pn: [f for f in annotate(fs) if f.analysed] for pn, fs in by.items()}
    return {pn: counts(fs, own) for pn, fs in played.items() if fs}


def _game_counts(
    conn: sqlite3.Connection, game: str | None, own: frozenset, between: Callable[[], None] | None = None
) -> list[dict[str, Counter]]:
    """Per-game counts, derived only where a game's stamp moved. `between`, when
    given, runs before each game that has to be derived."""
    path = path_of(conn)
    stamps = _game_stamps(conn, game) if path else None
    if stamps is None:
        return [_counts_by_id((f for h in load_hands(conn, game) for f in derive(h)), own)]
    out = []
    for gid, stamp in stamps.items():
        hit = _GAME_COUNTS.get((path, gid))
        if hit is None or hit[0] != stamp:
            if between is not None:
                between()  # before the lock: a request may be waiting on it
            with _GAME_COUNTS.making((path, gid)):
                hit = _GAME_COUNTS.get((path, gid))
                if hit is None or hit[0] != stamp:
                    facts = [f for h in load_hands(conn, gid) for f in derive(h)]
                    hit = _GAME_COUNTS[path, gid] = (stamp, _counts_by_id(facts, own))
        out.append(hit[1])
    if game is None:
        _GAME_COUNTS.prune(lambda k: k[0] == path and k[1] not in stamps)
    return out


def pool_for(
    conn: sqlite3.Connection, game: str | None = None, *, between: Callable[[], None] | None = None
) -> Pool:
    """The pool over every hand, or one game's: each player's counts, by alias.

    `between` runs before each game derived afresh: the server's warm-up passes
    one that waits while requests are being answered, so a chart opened just
    after a start does not share the CPU with every hand in the database."""
    path, gen = path_of(conn), generation(conn)
    hit = _POOL.get((path, game))
    if hit is not None and gen is not None and hit[0] == gen:
        return hit[1]
    # The warm-up pauses inside, so it never holds the lock; its games are
    # still each derived once, under `_game_counts`' own.
    with _POOL.making((path, game)) if between is None else nullcontext():
        hit = _POOL.get((path, game))
        if hit is not None and gen is not None and hit[0] == gen:
            return hit[1]
        return _count_pool(conn, game, between, path, gen)


def _count_pool(
    conn: sqlite3.Connection, game: str | None, between: Callable[[], None] | None, path: str, gen: int | None
) -> Pool:
    """`pool_for`'s miss."""
    with reading(conn):
        own = owners(conn)
        ident = identity_map(conn)
        players: dict[str, Counter] = defaultdict(Counter)
        for per_game in _game_counts(conn, game, own, between):
            for pn, c in per_game.items():
                players[ident.get(pn, (-1, pn))[1]].update(c)
    pool = Pool(game or "lifetime", dict(players), own)
    if path:
        _POOL[path, game] = (gen, pool)
    return pool


def pool_from_facts(
    grouped: Mapping[str, list[Facts]], own: Collection[tuple[str, str]] = (), scope: str = "spot",
) -> Pool:
    """A pool over facts already in hand, by alias: a filtered spot's."""
    return Pool(scope, {a: counts(fs, own) for a, fs in grouped.items() if fs}, frozenset(own))


def clear() -> None:
    """Forget every remembered pool and count. For tests."""
    _GAME_COUNTS.clear()
    _POOL.clear()
