"""Hand strength: how good a holding is on the board as it stood, as a share.

`cards.made_hand` says what a holding *is* -- top pair, a board pair, a flush
draw -- by what the hole cards add. That decides whether a player has a hand of
their own at all: a draw, or air (high card, a pair that is only the board's, a
draw that missed by the river). It cannot say how *good* that hand is on this
board: queens on K-K-7 are an "underpair" and beat 91% of holdings, as often as
top pair does on K-7-2; aces on a four-flush, four-straight river are an
"overpair" and beat 55%.

So a hand of their own is graded by **hand strength** (Billings, Papp, Schaeffer
and Szafron, "Opponent Modeling in Poker", AAAI-98): the share of every two-card
holding the rest of the deck can make that it beats on the board as it stood,
ties counting half. A board of `n` cards leaves `52 - n - 2` cards for the
opponent, so the flop counts C(47, 2) = 1,081 holdings and the river C(45, 2) =
990. `HS_CUTS` turns it into the tiers, per street, since the same pair beats
fewer holdings as more board cards arrive. See SPEC.md, "Hand strength".

Each value is exact: the board's holdings are ranked once (`equity.rank7`), and a
holding's wins and ties are read off the sorted ranks, less the holdings that
share a card with it. They are kept as integers (`2*wins + ties`, `n`) so a hand
that sits exactly on a cut lands on the same side every time, and are memoized in
the process and in `strength_cache` (`warm`, `prime`): the table is derived and
disposable, keyed by cards alone, and a change of cut never touches it.
"""

from __future__ import annotations

import bisect
import itertools
import sqlite3
import threading
from collections.abc import Callable, Iterable
from functools import lru_cache

from ..logfmt.events import FLOP, PREFLOP, RIVER, TURN
from .cards import VALUE_RANK, Card, board_at, hand_class, hand_pct, made_at, parse_cards

#: Preflop strength by `hand_pct`: at or under the first cutoff is `strong`, at
#: or under the second `medium`, the rest `weak`. 15 is roughly 77+, ATs+, KQs
#: and AJo+; 50 is where a chart's playable hands run out. Judgement values,
#: recorded in SPEC.md, "What they showed up with".
PREFLOP_STRENGTH: dict[str, float] = {"strong": 15, "medium": 50}

#: The hand-strength cuts for a hand of their own, per street: at or over
#: `strong` is strong, under `weak` is weak, between is medium, and strong at or
#: over `nutted` is the nutted tier. Calibrated on the shown hands of the
#: 15,052-hand database on 2026-10-14 (`scripts/calibrate_strength.py`; SPEC.md,
#: "Hand strength"): `strong` best separates top pair and overpairs from the
#: lower pairs on unpaired boards, `weak` middle pairs and underpairs from bottom
#: pair, and `nutted` keeps the nutted share where "a set or better" had it.
HS_CUTS: dict[str, dict[str, float]] = {
    FLOP: {"nutted": 0.99, "strong": 0.86, "weak": 0.73},
    TURN: {"nutted": 0.96, "strong": 0.83, "weak": 0.61},
    RIVER: {"nutted": 0.91, "strong": 0.77, "weak": 0.48},
}
for _street, _cut in HS_CUTS.items():
    assert _cut["nutted"] >= _cut["strong"] >= _cut["weak"], _street

#: The strength tiers, strongest first. Every shown hand with a street to judge it
#: on lands in exactly one.
TIERS = ("nutted", "strong", "marginal", "draw", "air")
_TIER_OF_STRENGTH = {"strong": "strong", "medium": "marginal", "weak": "marginal", "draw": "draw", "air": "air"}

#: The postflop streets, in order, and the board cards each has.
_STREET_ORDER = {PREFLOP: 0, FLOP: 1, TURN: 2, RIVER: 3}


# ------------------------------------------------------------ the measure ---


def _norm(cards: Iterable[Card]) -> tuple[str, ...]:
    """Cards as sorted ``Ah``-style strings: the order a key never depends on."""
    return tuple(sorted(f"{VALUE_RANK[c.value]}{c.suit}" for c in cards))


def cache_key(hole: str | Iterable[str], board: Iterable[str]) -> str:
    """``"AhKd|2c7s9d"``: hole and board each sorted, since neither order matters."""
    return "".join(_norm(parse_cards(hole))) + "|" + "".join(_norm(parse_cards(list(board))))


_DECK: tuple[Card, ...] = tuple(Card(v, s) for v in range(2, 15) for s in "cdhs")


class _Board:
    """Every holding the rest of the deck makes on one board, ranked once."""

    __slots__ = ("index", "rank", "sorted", "with_card")

    def __init__(self, board: tuple[Card, ...]) -> None:
        from .equity import rank7  # equity imports cards; keep this module's imports light

        rest = [c for c in _DECK if c not in board]
        combos = list(itertools.combinations(rest, 2))
        raw = [rank7([a, b, *board]) for a, b in combos]
        dense = {r: i for i, r in enumerate(sorted(set(raw)))}
        self.rank = [dense[r] for r in raw]
        self.sorted = sorted(self.rank)
        self.index = {frozenset(c): i for i, c in enumerate(combos)}
        self.with_card: dict[Card, list[int]] = {c: [] for c in rest}
        for i, (a, b) in enumerate(combos):
            self.with_card[a].append(i)
            self.with_card[b].append(i)

    def count(self, hole: tuple[Card, Card]) -> tuple[int, int]:
        """(2*wins + ties, holdings) for `hole`, every holding sharing a card left out."""
        me = self.rank[self.index[frozenset(hole)]]
        lo, hi = bisect.bisect_left(self.sorted, me), bisect.bisect_right(self.sorted, me)
        wins, ties = lo, hi - lo
        for i in set(self.with_card[hole[0]]) | set(self.with_card[hole[1]]):
            r = self.rank[i]
            if r < me:
                wins -= 1
            elif r == me:
                ties -= 1
        return 2 * wins + ties, len(self.rank) - len(self.with_card[hole[0]]) - len(self.with_card[hole[1]]) + 1


@lru_cache(maxsize=512)
def _board(board: tuple[Card, ...]) -> _Board:
    return _Board(board)


#: Every value known to this process, by `cache_key`: (2*wins + ties, holdings).
#: Cards alone decide it, so one dict serves every database the process opens.
_HS: dict[str, tuple[int, int]] = {}
#: Values computed since the last `flush`, for `strength_cache`.
_PENDING: dict[str, tuple[int, int]] = {}
_LOCK = threading.Lock()
#: Database paths whose `strength_cache` this process has read.
_PRIMED: set[str] = set()


def counts(hole: str, board: Iterable[str]) -> tuple[int, int] | None:
    """(2*wins + ties, holdings) for a holding on a board of 3 to 5 cards, or None
    when it cannot be measured: too few board cards, or a card used twice."""
    h, b = parse_cards(hole), parse_cards(list(board))
    if len(h) != 2 or not 3 <= len(b) <= 5 or len({*h, *b}) != len(h) + len(b):
        return None
    key = "".join(_norm(h)) + "|" + "".join(_norm(b))
    hit = _HS.get(key)
    if hit is None:
        hit = _board(tuple(sorted(b))).count((h[0], h[1]))
        with _LOCK:
            _HS[key] = hit
            _PENDING[key] = hit
    return hit


def hand_strength(hole: str | None, board: Iterable[str]) -> float | None:
    """The share of holdings `hole` beats on `board`, ties half, 0 to 1; None when
    it cannot be measured."""
    if not hole:
        return None
    c = counts(hole, board)
    return None if c is None else c[0] / (2 * c[1])


def _at_least(c: tuple[int, int], cut: float) -> bool:
    """HS >= cut, decided in integers so a value on the cut is never a rounding call."""
    return c[0] * 100 >= round(cut * 100) * 2 * c[1]


# ------------------------------------------------------------- the grades ---


@lru_cache(maxsize=65536)
def _strength_on(hole: str, board: tuple[str, ...], street: str) -> str:
    """The postflop rule, memoized: the same shown hands are re-read every poll."""
    mh = made_at(hole, board, street)
    if mh.cls == "draw":
        return "air" if street == RIVER else "draw"
    if mh.cls == "high_card" or mh.detail == "board_pair":
        return "air"
    c = counts(hole, board)
    if c is None:  # a card used twice: say what the class says, without a measure
        return "medium"
    cut = HS_CUTS[street]
    if _at_least(c, cut["strong"]):
        return "strong"
    return "medium" if _at_least(c, cut["weak"]) else "weak"


def strength_at(hole_cards: str | None, board: tuple[str, ...] | list[str], street: str) -> str | None:
    """What a known holding was worth on `street`, or None when that is unknown.

    Postflop, on the board as it stood on that street. Whether there is a hand of
    their own at all is `cards.made_hand`'s; how good it is, their hand strength
    against `HS_CUTS`:

    ``strong``  a hand of their own at or over the strong cut: top pair or better
                on most boards, queens on K-K-7, never aces on a four-flush river
    ``medium``  between the cuts: most middle pairs and underpairs
    ``weak``    under the weak cut: most bottom pairs
    ``draw``    no pair of their own, but a flush or straight draw -- flop or turn
    ``air``     high card, or a board pair the hole cards add nothing to; on the
                river a draw has missed, so it is air too

    Preflop, by `hand_pct` against `PREFLOP_STRENGTH`: ``strong``, ``medium`` or
    ``weak``. Unknown when the cards are, or when the board never reached the
    street: a hand that ended on the flop says nothing about the turn.
    """
    if not hole_cards:
        return None
    if street == PREFLOP:
        pct = hand_pct(hand_class(hole_cards))
        for bucket, cutoff in PREFLOP_STRENGTH.items():
            if pct <= cutoff:
                return bucket
        return "weak"
    seen = board_at(board, street)
    if seen is None:
        return None
    return _strength_on(hole_cards, tuple(seen), street)


def tier_at(hole: str | None, board: tuple[str, ...] | list[str], street: str) -> str | None:
    """The strength tier of a holding on `street`'s board, or None when unknown.

    ``nutted``    strong, and at or over the street's nutted cut
    ``strong``    the rest of `strength_at`'s strong
    ``marginal``  its medium and weak
    ``draw``      a flush or straight draw and no pair of their own, flop or turn
    ``air``       high card or a board pair, and on the river a draw that missed
    """
    s = strength_at(hole, board, street)
    if s is None or street == PREFLOP:
        return None
    if s == "strong":
        c = counts(hole, board_at(board, street))
        if c is not None and _at_least(c, HS_CUTS[street]["nutted"]):
            return "nutted"
    return _TIER_OF_STRENGTH[s]


def _known_on(f, street: str) -> bool:
    """Cards known, the board reached `street`, and they had not folded before it:
    the hands a `made_<street>` term can speak for."""
    if f.hole_cards is None or board_at(f.board, street) is None:
        return False
    return f.fold_street is None or _STREET_ORDER.get(f.fold_street, 9) >= _STREET_ORDER[street]


def tier_of(f, street: str) -> str | None:
    """A `Facts` row's tier on `street`, gated like a `made_<street>` term."""
    return tier_at(f.hole_cards, f.board, street) if _known_on(f, street) else None


def hs_of(f, street: str) -> float | None:
    """A `Facts` row's hand strength on `street`, gated like `tier_of`."""
    return hand_strength(f.hole_cards, board_at(f.board, street)) if _known_on(f, street) else None


# -------------------------------------------------------------- the cache ---


def prime(conn: sqlite3.Connection) -> None:
    """Read this database's `strength_cache` into the process, once per path."""
    from ..db.conn import path_of

    path = path_of(conn)
    if path in _PRIMED:
        return
    try:
        rows = conn.execute("SELECT key, num2, n FROM strength_cache").fetchall()
    except sqlite3.OperationalError:  # a database from before the table
        return
    with _LOCK:
        for key, num2, n in rows:
            _HS.setdefault(key, (num2, n))
        if path:
            _PRIMED.add(path)


def needed(conn: sqlite3.Connection, game_id: str | None = None) -> list[tuple[str, tuple[str, ...]]]:
    """Every (hole, board as it stood) a grade can ask about: each known holding --
    hole cards stored, and two-card voluntary shows -- with a pair of their own or
    better, on every street its board reached. Sorted by board, so `warm` builds
    each board's table once."""
    import json

    where, params = ("WHERE h.game_id = ?", (game_id,)) if game_id else ("", ())
    rows = conn.execute(
        "SELECT hp.hole_cards AS cards, h.board_json AS board FROM hand_players hp"
        f" JOIN hands h ON h.hand_id = hp.hand_id {where}"
        f"{' AND' if where else ' WHERE'} hp.hole_cards IS NOT NULL"
        " UNION SELECT vs.cards AS cards, h.board_json AS board FROM voluntary_shows vs"
        f" JOIN hands h ON h.hand_id = vs.hand_id {where}",
        params * 2,
    ).fetchall()
    out = set()
    for r in rows:
        cards, runs = r["cards"], json.loads(r["board"] or "[]")
        if not cards or not runs or len(parse_cards(cards)) != 2:
            continue
        board = tuple(runs[0])
        for street in (FLOP, TURN, RIVER):
            seen = board_at(board, street)
            if seen is None:
                break
            mh = made_at(cards, tuple(seen), street)
            if mh is not None and mh.cls not in ("draw", "high_card") and mh.detail != "board_pair":
                out.add((cards, tuple(seen)))
    return sorted(out, key=lambda x: (tuple(sorted(x[1])), x[0]))


def flush(conn: sqlite3.Connection) -> int:
    """Write the values computed since the last flush to `strength_cache`."""
    from ..db.conn import writing

    with _LOCK:
        pending = dict(_PENDING)
        _PENDING.clear()
    if not pending:
        return 0
    try:
        with writing(conn):
            conn.executemany(
                "INSERT OR IGNORE INTO strength_cache (key, num2, n) VALUES (?, ?, ?)",
                [(k, a, b) for k, (a, b) in pending.items()],
            )
    except sqlite3.OperationalError:  # read-only, or no table: keep them for later
        with _LOCK:
            _PENDING.update(pending)
        return 0
    return len(pending)


def warm(
    conn: sqlite3.Connection, game_id: str | None = None, progress: Callable[[int, int], None] | None = None
) -> int:
    """Measure every holding `needed` names and store them: what `pnt import`,
    `pnt rebuild` and the server's start run, so reads never wait on a board.
    Returns how many values this database's table gained."""
    prime(conn)
    jobs = needed(conn, game_id)
    for i, (cards, board) in enumerate(jobs):
        key = cache_key(cards, board)
        hit = _HS.get(key)
        if hit is None:
            counts(cards, board)
        else:
            with _LOCK:
                _PENDING.setdefault(key, hit)  # known here, maybe not to this file
        if progress and i % 500 == 0:
            progress(i, len(jobs))
    return flush(conn)
