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
from collections import Counter, defaultdict

from .leaks import hero_of
from .pool import pool_for
from .queries import facts_cached, report
from .tags import SHOWN_RULES, _exploits, profile, tags_for

#: An opponent counts toward the pool's tendencies from this many hands: under it,
#: a tag is too often one night's luck.
MIN_HANDS = 200

KINDS = ("archetype", "exploit")

#: Every measure the tags read, by street, for one player's view: (rule, what it
#: counts, the filter that opens those hands, the chart view). A measure that
#: fired no tag still says where the player sits against the pool. The filters
#: are the tags' own where a tag reads the measure.
MEASURES: tuple[tuple[str, tuple[tuple[str, str, str, str], ...]], ...] = (
    ("preflop", (
        ("limper", "limps an unopened pot", "limp", "preflop"),
        ("steals_wide", "opens a steal spot (CO, BTN, SB)", "steal_opp", "preflop"),
        ("folds_to_steal", "folds a blind to a steal", "fold_to_steal_opp", "preflop"),
        ("3bet", "3-bets an open", "3bet_opp", "preflop"),
        ("squeeze", "squeezes an open and a call", "squeeze_opp", "preflop"),
        ("folds_to_3bet", "folds an open to a 3-bet", "faced_3bet", "preflop"),
        ("jam_preflop", "jams preflop", "jam_preflop", "preflop"),
    )),
    ("flop", (
        ("cbet_flop", "c-bets the flop", "cbet_flop_opp", "preflop"),
        ("folds_to_cbet", "folds to a flop c-bet", "faced_cbet_flop", "preflop"),
        ("donks", "leads into the preflop raiser", "donk_flop", "preflop"),
        ("af_flop", "bets or raises, of flop actions", "saw_flop", "preflop"),
        ("jam_postflop", "jams after the flop", "jam", "preflop"),
    )),
    ("river", (
        ("folds_river", "folds to a river bet", "faced_bet_river", "preflop"),
        ("wtsd", "goes to showdown, of flops seen", "saw_flop", "preflop"),
        ("tilts", "VPIP after a big loss", "post_loss,acted_preflop", "preflop"),
    )),
    ("shown hands", (
        ("no_bluff", "river bets shown as air", "aggressor_river,wtsd", "made"),
        ("calls_down_light", "river calls shown weak", "called_bet_river,wtsd", "made"),
        ("checks_back", "check-backs shown strong", "check_back,wtsd", "made"),
        ("slowplays", "sets or better checked or called on the flop", "made_flop>=set,line_flop=*", "strength"),
        ("called_3bet_light", "3-bets called with a bottom-40% hand", "faced_3bet_any=call", "preflop"),
        ("fourbets_light", "4-bets with a bottom-half hand", "4bet", "preflop"),
        ("jams_light", "jams with a bottom-half hand", "jam_preflop", "preflop"),
        ("limp_reraise_strong", "limp-reraises with a top-15% hand", "limp,faced_open=raise", "preflop"),
    )),
)


def advice(tip: str) -> str:
    """The tip after its first sentence, which gives the figures: what the tag
    means and what to do about it."""
    head, _, rest = tip.strip().partition(". ")
    return rest or head


def _tags(conn: sqlite3.Connection, alias: str, game: str | None, pool=None) -> list[dict]:
    out = tags_for(facts_cached(conn, alias, game), pool, alias)
    return [{**t, "advice": advice(t["tip"])} for t in out["tags"] if t["kind"] in KINDS]


def table(pool, regulars: list[str]) -> list[dict]:
    """What the regulars do together, on the fixed cuts: the table's own habits.
    Judged against itself the pool is never an outlier, so this is where a
    whole-table tendency -- everyone folds to c-bets -- shows."""
    total: Counter = Counter()
    for alias in regulars:
        total.update(pool.players.get(alias, Counter()))
    if not total:
        return []
    return [{**{k: t[k] for k in ("id", "label", "pct", "hits", "n", "filter", "by")}, "advice": advice(t["tip"])}
            for t in _exploits(profile(total))]


def tendencies(conn: sqlite3.Connection, game: str | None = None, min_hands: int = MIN_HANDS) -> dict:
    """The hero's tags, and per tag the regular opponents it fires on, most first.
    A regular is anyone but the hero with `min_hands` hands or more."""
    hero = hero_of(conn, game)
    regulars = [r["player"] for r in report(conn, game, min_hands=min_hands) if r["player"] != hero]
    pool_ = pool_for(conn, game)
    by_tag: dict[str, dict] = {}
    on: dict[str, list[dict]] = defaultdict(list)
    for alias in regulars:
        for t in _tags(conn, alias, game, pool_):
            by_tag.setdefault(t["id"], {k: t[k] for k in ("id", "label", "kind", "advice")})
            on[t["id"]].append({"player": alias, **{k: t[k] for k in ("pct", "hits", "n", "filter", "by")}})
    pool = [{**by_tag[i], "count": len(on[i]), "players": sorted(on[i], key=lambda p: -p["n"])} for i in by_tag]
    pool.sort(key=lambda t: (-t["count"], t["kind"] != "archetype", t["label"]))
    return {
        "hero": hero,
        "game": game,
        "you": _tags(conn, hero, game, pool_) if hero else [],
        "regulars": len(regulars),
        "min_hands": min_hands,
        "pool": pool,
        "table": table(pool_, regulars),
    }


def player_tendencies(conn: sqlite3.Connection, alias: str, game: str | None = None,
                      shared: dict[str, int] | None = None) -> dict:
    """One player's tendencies: their tags (`tags`, archetype first; `streaky`
    ones one session carries), and every measure the tags read beside the pool's
    rate without them (`measures`, by street). `shared` is how many regulars
    each tag fires on, from `tendencies`. Raises ValueError on an unknown alias."""
    pool_ = pool_for(conn, game)
    out = tags_for(facts_cached(conn, alias, game), pool_, alias)
    shared = shared or {}
    p, base = out["profile"], pool_.baseline(alias)

    def tag(t: dict) -> dict:
        return {**t, "advice": advice(t["tip"]), "shared": shared.get(t["id"], 0)}

    def measure(rule: str, label: str, filter: str, by: str) -> dict:
        m = p.get(rule) or p.get("rules", {}).get(rule) or {"hits": 0, "n": 0, "pct": None, "own_n": 0}
        b = base.get(rule)
        return {
            "id": rule, "label": label, "filter": filter, "by": by,
            **{k: m[k] for k in ("hits", "n", "pct")},
            # Shown hands from their own log, every card face up: not comparable
            # with the pool, which leaves those rows out (SPEC.md, "Pool baseline").
            "own_log": rule in SHOWN_RULES and m["n"] > 0 and 2 * m.get("own_n", 0) >= m["n"],
            "pool_pct": b.pct if b else None,
            "pool_spread": list(b.spread) if b else None,
        }

    return {
        "player": alias,
        "hands": p["hands"],
        "vpip": p.get("vpip"),
        "pfr": p.get("pfr"),
        "pool": out["pool"],
        "tags": [tag(t) for t in out["tags"] if t["kind"] in KINDS],
        "streaky": [tag(t) for t in out["streaky"]],
        "measures": [{"street": street, "rows": [measure(*m) for m in rows]} for street, rows in MEASURES],
    }
