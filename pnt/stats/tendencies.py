"""Tendencies: the tags on the hero, and the tags most of their opponents share.

The leak finder (`leaks.py`) judges the hero against bands, and on a pool that
under-bluffs its fold bands mislead. This page asks a plainer question with the
rules the HUD already uses (`tags.py`): what would an opponent's HUD say about
you, and which exploits fire on most of the players you sit with? Every tag
carries its own filter, so each row opens its hands on the chart.

Fun tags (7-2 jams and the like) are left out: they are not tendencies.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from .leaks import hero_of
from .queries import facts_cached, report
from .tags import tags_for

#: An opponent counts toward the pool's tendencies from this many hands: under it,
#: a tag is too often one night's luck.
MIN_HANDS = 200

KINDS = ("archetype", "exploit")


def advice(tip: str) -> str:
    """The tip after its first sentence, which gives the figures: what the tag
    means and what to do about it."""
    head, _, rest = tip.strip().partition(". ")
    return rest or head


def _tags(conn: sqlite3.Connection, alias: str, game: str | None) -> list[dict]:
    out = tags_for(facts_cached(conn, alias, game))
    return [{**t, "advice": advice(t["tip"])} for t in out["tags"] if t["kind"] in KINDS]


def tendencies(conn: sqlite3.Connection, game: str | None = None, min_hands: int = MIN_HANDS) -> dict:
    """The hero's tags, and per tag the regular opponents it fires on, most first.
    A regular is anyone but the hero with `min_hands` hands or more."""
    hero = hero_of(conn, game)
    regulars = [r["player"] for r in report(conn, game, min_hands=min_hands) if r["player"] != hero]
    by_tag: dict[str, dict] = {}
    on: dict[str, list[dict]] = defaultdict(list)
    for alias in regulars:
        for t in _tags(conn, alias, game):
            by_tag.setdefault(t["id"], {k: t[k] for k in ("id", "label", "kind", "advice")})
            on[t["id"]].append({"player": alias, **{k: t[k] for k in ("pct", "hits", "n", "filter", "by")}})
    pool = [{**by_tag[i], "count": len(on[i]), "players": sorted(on[i], key=lambda p: -p["n"])} for i in by_tag]
    pool.sort(key=lambda t: (-t["count"], t["kind"] != "archetype", t["label"]))
    return {
        "hero": hero,
        "game": game,
        "you": _tags(conn, hero, game) if hero else [],
        "regulars": len(regulars),
        "min_hands": min_hands,
        "pool": pool,
    }
