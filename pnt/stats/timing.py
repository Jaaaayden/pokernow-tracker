"""Timing tells: does a snap call mean a draw, and does a long tank mean a bluff?

PokerNow logs every line to the millisecond, so each player's first decision on a
street carries how long it took (`Facts.think_ms`) and a tempo bucket
(`Facts.tempo`): pre-selected, snap, normal, tank, or a timeout. This module counts
what the hands in each bucket were, in the spots where timing was found to mean
something on this pool: flop and turn calls, river bets, and river calls.

What the pool's hands said (SPEC.md, "Timing tells"), and what the view leads
with: a slow call is stronger than a quick one -- more value, fewer draws -- and a
long think before a river bet means value, not a bluff, while a snapped river bet
is the weakest. River calls carry little timing signal. Per player the cells are
thin, so the pool comes first and every figure carries its n.

Every row carries the filter that finds exactly its `n` hands. The strength split
reads cards: shown cards for most players, which leans to value because a bluff
that got a fold is never shown, and every card of the hands in a log its owner
captured. The pool splits the two.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass

from ..logfmt.events import PREFLOP
from .derive import POSTFLOP_STREETS, TEMPO_BUCKETS, TEMPO_CUTS, Facts
from .lines import tier_at
from .queries import load_hands, play_facts, wilson

STREETS = (PREFLOP, *POSTFLOP_STREETS)
TIERS = ("nutted", "strong", "marginal", "draw", "air")

#: The spots, in the order the view shows them: (key, street, label, the letter
#: their first decision there starts with, the filter that finds it).
SPOTS: tuple[tuple[str, str, str, str, str], ...] = (
    ("flop_call", "flop", "Flop call", "c", "line_flop=c*"),
    ("turn_call", "turn", "Turn call", "c", "line_turn=c*"),
    ("river_bet", "river", "River bet", "b", "line_river=b*"),
    ("river_call", "river", "River call", "c", "line_river=c*"),
)

#: The buckets each kind of first decision can land in, against the pool's cuts
#: and against the player's own median. A bet cannot be pre-selected, and only a
#: fold or a check times out. A call after the flop always faces a bet, so its
#: quick end is the pre-selected window and the pool's cuts leave it no snap;
#: against the player's own median, a call between the two is a snap.
BUCKETS_OF: dict[tuple[str, str], tuple[str, ...]] = {
    ("b", "pool"): ("snap", "normal", "tank"),
    ("c", "pool"): ("pre", "normal", "tank"),
    ("b", "own"): ("snap", "normal", "tank"),
    ("c", "own"): ("pre", "snap", "normal", "tank"),
}

#: Known hands a player needs in every bucket of a spot for their own row there
#: to be worth reading. Under it, the pool's row is the better guide.
MIN_CELL = 30

#: The view's note, one bullet each. **text** is bold on the page.
NOTE = (
    "**Strength:** shown cards only, so it leans to value: a bluff that got a fold is never shown.",
    "**Your rows** use every hand (your log has all your cards), so they include your bluffs and read weaker.",
    ("**Think time:** from the board or the last action to theirs. **Pre:** under 1 s, a pre-selected button. "
     "Timeouts are left out."),
)


def _first(f: Facts, street: str) -> str | None:
    """The letter of their first decision on a postflop street."""
    line = f.line.get(street)
    return line[0] if line else None


def _cell(rows: list[Facts], street: str) -> dict:
    """What `rows` held on `street`: n, how many are known, and the tiers."""
    counts: Counter[str] = Counter()
    for f in rows:
        tier = tier_at(f.hole_cards, f.board, street)
        if tier:
            counts[tier] += 1
    known = sum(counts.values())
    value = counts["nutted"] + counts["strong"]
    ci = wilson(value, known)
    share = (lambda k: round(100 * k / known, 1)) if known else (lambda k: None)
    return {
        "n": len(rows),
        "known": known,
        "coverage": round(100 * known / len(rows), 1) if rows else None,
        "counts": {t: counts[t] for t in TIERS},
        "value": share(value),
        "value_ci": [round(100 * ci[0], 1), round(100 * ci[1], 1)] if ci else None,
        "draw": share(counts["draw"]),
        "air": share(counts["air"]),
    }


def _spots(
    facts: list[Facts],
    mode: str,
    bucket_for: Callable[[Facts, str], str | None],
    term_for: Callable[[str, str], str],
    *,
    owners: Collection[tuple[str, str]] | None = None,
    with_ids: bool = False,
    spots: Iterable[tuple[str, str, str, str, str]] = SPOTS,
) -> list[dict]:
    """A block per spot: its first decisions, how many were timed, and a row per
    bucket with what the hands were. `term_for(bucket, street)` is the filter term
    that finds the bucket, ANDed to the spot's own."""
    out = []
    for key, street, label, letter, base in spots:
        took = [f for f in facts if _first(f, street) == letter]
        groups: dict[str, list[Facts]] = defaultdict(list)
        timed = 0
        for f in took:
            b = bucket_for(f, street) if street in f.tempo else None
            if b is not None:
                timed += 1
                groups[b].append(f)
        rows = []
        for b in BUCKETS_OF[letter, mode]:
            members = groups.get(b, [])
            r = {"key": f"{key}:{b}", "bucket": b, "filter": f"{base},{term_for(b, street)}",
                 **_cell(members, street)}
            if owners is not None:
                r["own"] = _cell([f for f in members if (f.game_id, f.pn_id) in owners], street)
                r["others"] = _cell([f for f in members if (f.game_id, f.pn_id) not in owners], street)
            if with_ids:
                r["hand_ids"] = [f.hand_id for f in members]
            rows.append(r)
        out.append({"key": key, "street": street, "label": label, "filter": base,
                    "n": len(took), "timed": timed, "rows": rows})
    return out


def _streets(facts: list[Facts]) -> list[dict]:
    """How long their first decisions took on each street, and the buckets."""
    out = []
    for s in STREETS:
        times = [f.think_ms[s] for f in facts if s in f.think_ms and f.tempo[s] != "timeout"]
        buckets = Counter(f.tempo[s] for f in facts if s in f.tempo)
        timed = sum(buckets.values())
        q = statistics.quantiles(times, n=4) if len(times) >= 2 else None
        out.append({
            "street": s,
            "timed": timed,
            "filter": f"think_{s}>=0",
            "median_ms": round(statistics.median(times)) if times else None,
            "p25_ms": round(q[0]) if q else None,
            "p75_ms": round(q[2]) if q else None,
            "buckets": [{"bucket": b, "n": buckets[b], "filter": f"tempo_{s}={b}",
                         "pct": round(100 * buckets[b] / timed, 1) if timed else None}
                        for b in TEMPO_BUCKETS],
        })
    return out


def _coverage(facts: list[Facts]) -> dict:
    """First voluntary decisions with a time, of all of them. Short on hands
    imported before the clock was stored, until `pnt rebuild` times them."""
    decided = timed = 0
    for f in facts:
        streets = {s for s in POSTFLOP_STREETS if f.line.get(s)}
        if f.pf_faced:
            streets.add(PREFLOP)
        decided += len(streets)
        timed += len(streets & f.think_ms.keys())
    return {"decisions": decided, "timed": timed, "rebuild": timed < decided}


def medians(facts: Iterable[Facts]) -> dict[str, float]:
    """A player's own median first-decision time per street, in ms: what the
    relative buckets are cut from. Pre-selected clicks and timeouts are left out,
    since neither is a decision being thought about."""
    times: dict[str, list[int]] = defaultdict(list)
    for f in facts:
        for s, ms in f.think_ms.items():
            if f.tempo[s] not in ("pre", "timeout"):
                times[s].append(ms)
    return {s: statistics.median(v) for s, v in times.items() if v}


def relative_cuts(med: Mapping[str, float]) -> dict[str, tuple[int, int]]:
    """(snap under, tank over) in whole ms per street, from the player's medians."""
    return {s: (round(TEMPO_CUTS["rel_snap"] * m), round(TEMPO_CUTS["rel_tank"] * m)) for s, m in med.items()}


def _secs(ms: int) -> str:
    return f"{ms / 1000:g}"


def _relative(facts: list[Facts], cuts: Mapping[str, tuple[int, int]], with_ids: bool) -> list[dict]:
    """The spots again, bucketed against the player's own median instead of the
    pool's cuts: snap under half of it, tank over twice it. Pre-selected clicks
    keep their own label, and timeouts stay out."""
    def bucket(f: Facts, s: str) -> str | None:
        t = f.tempo.get(s)
        if t is None or s not in cuts or t in ("pre", "timeout"):
            return t
        lo, hi = cuts[s]
        ms = f.think_ms[s]
        return "snap" if ms < lo else "tank" if ms > hi else "normal"

    def term(b: str, s: str) -> str:
        if b == "pre":
            return f"tempo_{s}=pre"
        lo, hi = cuts[s]
        rest = f"tempo_{s}!=pre,tempo_{s}!=timeout"
        if b == "snap":
            return f"think_{s}<{_secs(lo)},{rest}"
        if b == "tank":
            return f"think_{s}>{_secs(hi)},{rest}"
        return f"think_{s}>={_secs(lo)},think_{s}<={_secs(hi)},{rest}"

    # Only the streets they have a median on: a player with no timed hands there
    # has no cuts to bucket against or to write a filter from.
    return _spots(facts, "own", bucket, term, with_ids=with_ids, spots=[sp for sp in SPOTS if sp[1] in cuts])


def thin(facts: Iterable[Facts], who: Mapping[str, str]) -> dict:
    """Per spot, how many players have known hands in it, and which of them have
    `MIN_CELL` in every bucket: the players whose own row says more than the pool's."""
    facts = list(facts)
    out = {}
    for key, street, label, letter, _ in SPOTS:
        buckets = BUCKETS_OF[letter, "pool"]
        cells: dict[str, Counter[str]] = defaultdict(Counter)
        for f in facts:
            if _first(f, street) != letter or f.tempo.get(street) not in buckets:
                continue
            if tier_at(f.hole_cards, f.board, street):
                cells[who.get(f.pn_id, f.pn_id)][f.tempo[street]] += 1
        full = sorted(p for p, c in cells.items() if all(c[b] >= MIN_CELL for b in buckets))
        out[key] = {"label": label, "players": len(cells), "full": full}
    return out


def timing_report(
    facts: Iterable[Facts],
    *,
    baseline: Iterable[Facts] | None = None,
    owners: Collection[tuple[str, str]] | None = None,
    who: Mapping[str, str] | None = None,
    with_ids: bool = False,
) -> dict:
    """The timing tables for a set of facts: one player's in a spot, or the pool's.

    `baseline` is the player's whole record, for the relative buckets' medians, so
    a spot narrows the hands judged but never the player's own pace. `owners`
    holds the (game, PokerNow ID) pairs whose cards are all known, the logs'
    owners, and splits each row's strength into theirs and everyone else's shown
    hands. `who` names each PokerNow ID's player, for the thin-sample count.
    `with_ids` adds each row's hand ids, for one player's list.
    """
    facts = list(facts)
    out = {
        "hands": len(facts),
        "timed": _coverage(facts),
        "streets": _streets(facts),
        "spots": _spots(facts, "pool", lambda f, s: f.tempo.get(s), lambda b, s: f"tempo_{s}={b}",
                        owners=owners, with_ids=with_ids),
        "buckets": list(TEMPO_BUCKETS),
        "cuts": dict(TEMPO_CUTS),
        "min_cell": MIN_CELL,
        "note": list(NOTE),
    }
    if baseline is not None:
        med = medians(baseline)
        cuts = relative_cuts(med)
        out["relative"] = {
            "medians_ms": {s: round(m) for s, m in med.items()},
            "cuts_ms": {s: list(c) for s, c in cuts.items()},
            "spots": _relative(facts, cuts, with_ids),
        }
    if who is not None:
        out["thin"] = thin(facts, who)
    return out


@dataclass(slots=True)
class Timed:
    """What the report reads of one `Facts` row, and nothing else."""

    game_id: str
    pn_id: str
    hand_id: int
    line: dict[str, str]
    tempo: dict[str, str]
    think_ms: dict[str, int]
    pf_faced: dict[int, str]
    hole_cards: str | None
    board: tuple[str, ...]


def pool_rows(
    conn: sqlite3.Connection,
    game_id: str | None = None,
    predicate: Callable[[Facts], bool] | None = None,
) -> list[Timed]:
    """`queries.pool_facts` for the pool's report, one game at a time and kept
    to `Timed`. The whole database's Facts at once is about 400 MB, which the
    phone's 1 GB VM swaps on for minutes; a game's session facts never read
    another game's rows, so the answer is the same."""
    games = [game_id] if game_id else [g for (g,) in conn.execute("SELECT DISTINCT game_id FROM hands")]
    return [
        Timed(f.game_id, f.pn_id, f.hand_id, f.line, f.tempo, f.think_ms, f.pf_faced, f.hole_cards, f.board)
        for g in games
        for f in play_facts(load_hands(conn, g))
        if predicate is None or predicate(f)
    ]


def needs_rebuild(conn: sqlite3.Connection) -> bool:
    """Whether any stored voluntary action has no time: hands imported before
    `actions.ord` existed, which only `pnt rebuild` can time."""
    try:
        row = conn.execute("SELECT 1 FROM actions WHERE ord IS NULL AND is_forced = 0 LIMIT 1").fetchone()
    except sqlite3.OperationalError:  # older still: the column itself is missing
        return True
    return row is not None
