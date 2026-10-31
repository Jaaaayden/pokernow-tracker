"""Voluntary shows: the cards a player chose to turn over when nobody asked.

PokerNow logs a show *after* ``-- ending hand #N --``: a player who folded and
then showed, a winner who took the pot uncontested and showed, a showdown loser
who mucked and showed later. They are stored in `voluntary_shows` and read here
through the `v_voluntary_shows` view, and they never reach `hole_cards`, so no
range view includes them (SPEC.md, "Ranges").

**The bias runs the other way from showdowns, and that is the tell.** Showdown
cards are the hands that got called; these are the ones a player *chose* to
reveal. Some show the bluff that got a fold to needle the table, some show only
the hand they laid down to earn credit for a tight image. Neither is a sample of
their range. It is a sample of what they want you to believe, which is the thing
worth knowing.

Each two-card show is read **on the board as it stood** (`strength.strength_at`,
with `cards.made_at` for the made hand behind it):

- a fold on the street they folded, since that is the hand they gave up;
- anything else on the board the hand ended on.

The four classes are `strength_at`'s buckets with medium and weak folded into one:

- ``strong``    a value hand on that board (`strength.strength_at`); preflop, `PREFLOP_STRENGTH`'s strong
- ``marginal``  middle, bottom, board or a low pocket pair; preflop, medium
- ``draw``      no pair, but a flush or straight draw, on the flop or turn
- ``air``       high card, or a draw on the river; preflop, the weak hands

A one-card show is counted, not classified: one card says little about a hand.

Two rates read the two-card shows: how often they showed air (`air_pct`) and how
often a strong hand on that board (`strong_pct`). A fold they then showed is also held
against the hand that won, wherever the winner's cards are known (your own hands,
or the winner's own show): was the folded hand ahead on the board as it stood
(`vs`, and `folded_vs` for the count)? Preflop folds are not compared.

Shows are not `Facts`, so no spot filter reproduces a row. Each row is one hand,
and carries its `hand_id`, so it opens on its own. It also carries the hand's pot
type (`pot_level`, and `pot` as a word), read off the player's facts for that
hand: the same level the ``limped``/``srp``/``3bet_pot``/``4bet_pot`` terms read.
"""

from __future__ import annotations

import json
import sqlite3

from ..logfmt.events import PREFLOP, RIVER, STREET_ORDER
from .cards import (
    DEALT,
    DRAW_DETAILS,
    board_at,
    hand_class,
    leaf_of,
    made_at,
    parse_cards,
    street_of_board,
)
from .derive import ANALYSED_SQL, pct
from .equity import rank7
from .queries import display_names, facts_cached, identities_of
from .tags import strength_at

#: Strongest first: the order the table prints them in.
CLASSES = ("strong", "marginal", "draw", "air")

#: How the hand ended for the player who showed, in the order they are printed.
CONTEXTS = ("folded", "uncontested", "showdown")
CONTEXT_LABELS = {
    "folded": "folded, then showed",
    "uncontested": "won uncontested, then showed",
    "showdown": "reached showdown, showed after",
}


#: `Facts.pot_level` as a word, as the filter terms read it: 0 is a bomb pot.
POT_TYPES = ("limped", "srp", "3bet", "4bet", "other")


def pot_type(level: int | None) -> str:
    """``limped``, ``srp``, ``3bet``, ``4bet`` (and above) or ``other``."""
    if not level:
        return "other"
    return POT_TYPES[min(level, 4) - 1]

#: One bullet each. **text** is bold on the page.
NOTE = (
    "**Chosen reveals**, not a sample of their range: the hands they wanted you to see.",
    "Bluffs that got folds are never shown anywhere else; here, only the ones they chose.",
    "Showing air advertises bluffs; showing only strong hands sells a tight image.",
    "That choice is the tell, so the range views leave these out.",
)

_CLASS_OF = {"strong": "strong", "medium": "marginal", "weak": "marginal", "draw": "draw", "air": "air"}
#: Preflop `strength_at` reads `hand_pct` alone. Its weak is the bottom half of
#: the chart, the hands nobody plays for value: shown unasked, that is air.
_PREFLOP_CLASS_OF = {"strong": "strong", "medium": "marginal", "weak": "air"}


def classify(hole: str, board: tuple[str, ...], street: str) -> tuple[str, str]:
    """``(class, made)`` for two shown cards on `street` of `board`.

    `made` is the `MADE_LEAVES` entry by `cards.made_at` postflop, and the hand
    class (``72o``) preflop. The class is `strength_at`'s bucket, as CLASSES.
    """
    s = strength_at(hole, board, street)
    if street == PREFLOP:
        return _PREFLOP_CLASS_OF[s], hand_class(hole)
    return _CLASS_OF[s], leaf_of(made_at(hole, board, street))


def context_of(folded: bool, went_to_showdown: bool) -> str:
    """Which of CONTEXTS a show belongs to. A fold wins over a showdown others reached."""
    if folded:
        return "folded"
    return "showdown" if went_to_showdown else "uncontested"


def _read_on(context: str, fold_street: str | None, board: tuple[str, ...]) -> str:
    """The street a show is read on: the fold's, else the last the board reached.

    Never past the board: a hand cut off mid-street has no later cards to read.
    """
    last = street_of_board(board)
    if context != "folded" or fold_street not in DEALT:
        return last
    return min(fold_street, last, key=STREET_ORDER.index)


def _first_run(board_json: str | None) -> tuple[str, ...]:
    runs = json.loads(board_json) if board_json else []
    return tuple(runs[0]) if runs else ()


def _empty_classes() -> dict[str, int]:
    return {c: 0 for c in CLASSES}


def _winners(conn: sqlite3.Connection, hand_ids: list[str]) -> dict[str, list[tuple[str, str | None]]]:
    """hand_id -> [(pn_id, two known cards or None)] of everyone who collected,
    most first. Cards are the hole cards on record, else the winner's own show."""
    out: dict[str, list[tuple[str, str | None]]] = {}
    if not hand_ids:
        return out
    q = ",".join("?" * len(hand_ids))
    shown = {(r["hand_id"], r["pn_id"]): r["cards"] for r in conn.execute(
        f"SELECT hand_id, pn_id, cards FROM voluntary_shows WHERE hand_id IN ({q}) AND length(cards) = 4", hand_ids)}
    for r in conn.execute(
        f"SELECT hand_id, pn_id, hole_cards FROM hand_players WHERE hand_id IN ({q}) AND collected > 0"
        f" ORDER BY collected DESC", hand_ids,
    ):
        cards = r["hole_cards"] if r["hole_cards"] and len(r["hole_cards"]) == 4 else shown.get((r["hand_id"], r["pn_id"]))
        out.setdefault(r["hand_id"], []).append((r["pn_id"], cards))
    return out


def versus(hole: str, winner: str, board: tuple[str, ...]) -> str:
    """``ahead``, ``chop`` or ``behind``: `hole` against `winner` on `board`."""
    b = parse_cards(list(board))
    mine, theirs = rank7(parse_cards(hole) + b), rank7(parse_cards(winner) + b)
    return "ahead" if mine > theirs else "chop" if mine == theirs else "behind"


def voluntary_shows(conn: sqlite3.Connection, alias: str, game: str | None = None) -> dict:
    """Every hand one player showed without being asked, classified and split by
    how the hand ended for them. Raises ValueError on an unknown alias.

    ``hands`` is every hand they were dealt into (the base the shows are drawn
    from), ``shows`` every show, ``two_card`` the classified ones and
    ``one_card`` the rest; ``coverage`` is ``two_card / shows``. ``classes``
    counts the two-card shows, and ``contexts`` splits the same counts by
    CONTEXTS, so each context's classes sum to its ``two_card`` and those sum to
    the total. ``rows`` is one per show, newest first, with the board as it stood.
    """
    ids = identities_of(conn, alias)
    out: dict = {
        "player": alias,
        "game": game,
        "hands": 0,
        "shows": 0,
        "two_card": 0,
        "one_card": 0,
        "coverage": None,
        "classes": _empty_classes(),
        "contexts": [
            {"key": k, "label": CONTEXT_LABELS[k], "shows": 0, "two_card": 0, "one_card": 0,
             "classes": _empty_classes()}
            for k in CONTEXTS
        ],
        "air_pct": None,
        "strong_pct": None,
        "folded_vs": {"known": 0, "ahead": 0, "chop": 0, "behind": 0, "ahead_pct": None},
        "rows": [],
        "note": list(NOTE),
    }
    if not ids:
        return out
    q = ",".join("?" * len(ids))
    game_args = [game] if game else []
    out["hands"] = conn.execute(
        f"SELECT count(*) FROM hand_players hp JOIN hands h ON h.hand_id = hp.hand_id"
        f" WHERE hp.pn_id IN ({q}) AND {ANALYSED_SQL}{' AND h.game_id = ?' if game else ''}",
        [*ids, *game_args],
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT v.*,"
        f" (SELECT a.street FROM actions a WHERE a.hand_id = v.hand_id AND a.pn_id = v.pn_id"
        f"  AND a.action_type = 'fold' ORDER BY a.seq LIMIT 1) AS fold_street"
        # Analysed hands only: a PLO show is four or five cards, a Double Board two boards.
        f" FROM v_voluntary_shows v JOIN hands h ON h.hand_id = v.hand_id"
        f" WHERE v.pn_id IN ({q}) AND {ANALYSED_SQL}{' AND v.game_id = ?' if game else ''}"
        f" ORDER BY v.ts DESC, v.hand_number DESC",
        [*ids, *game_args],
    ).fetchall()
    by_ctx = {c["key"]: c for c in out["contexts"]}
    winners = _winners(conn, [r["hand_id"] for r in rows if r["folded"] and r["n_cards"] == 2])
    owners = {g: h for g, h in conn.execute("SELECT game_id, hero_pn_id FROM games WHERE hero_pn_id IS NOT NULL")}
    names = display_names(conn)
    # Every show is from an analysed hand they were in, so each has its facts.
    levels = {f.hand_id: f.pot_level for f in facts_cached(conn, alias, game)}
    fv = out["folded_vs"]
    for r in rows:
        board = _first_run(r["board_json"])
        ctx = context_of(bool(r["folded"]), bool(r["went_to_showdown"]))
        street = _read_on(ctx, r["fold_street"], board)
        seen = board_at(board, street) or ()
        two = r["n_cards"] == 2
        cls = made = None
        if two:
            cls, made = classify(r["cards"], board, street)
        c = by_ctx[ctx]
        c["shows"] += 1
        out["shows"] += 1
        if two:
            c["two_card"] += 1
            c["classes"][cls] += 1
            out["two_card"] += 1
            out["classes"][cls] += 1
        else:
            c["one_card"] += 1
            out["one_card"] += 1
        out["rows"].append({
            "hand_id": r["hand_id"],
            "game_id": r["game_id"],
            "hand_number": r["hand_number"],
            "ts": r["ts"],
            "pn_id": r["pn_id"],
            "cards": r["cards"],
            "n_cards": r["n_cards"],
            "context": ctx,
            "street": street,
            "board": list(seen),
            "class": cls,
            "made": made,
            # A draw still drawing when the river was dealt: it missed.
            "missed": street == RIVER and made in DRAW_DETAILS,
            "vs": _vs(r, two, ctx, seen, winners, owners, names),
            "n_dealt_in": r["n_dealt_in"],
            "contributed": r["contributed"],
            "collected": r["collected"],
            "pot_level": levels.get(r["hand_id"]),
            "pot": pot_type(levels.get(r["hand_id"])),
        })
    out["coverage"] = round(100.0 * out["two_card"] / out["shows"], 1) if out["shows"] else None
    out["air_pct"] = pct(out["classes"]["air"], out["two_card"])
    out["strong_pct"] = pct(out["classes"]["strong"], out["two_card"])
    for row in out["rows"]:
        if row["vs"]:
            fv["known"] += 1
            fv[row["vs"]["result"]] += 1
    fv["ahead_pct"] = pct(fv["ahead"], fv["known"])
    return out


def _vs(r, two: bool, ctx: str, seen: tuple[str, ...], winners, owners, names) -> dict | None:
    """A shown fold against the hand that won, on the board as it stood, when the
    winner's two cards are known; else None. The biggest winner whose cards are
    known stands for the pot."""
    if not two or ctx != "folded" or len(seen) < 3:
        return None
    known = [(pn, c) for pn, c in winners.get(r["hand_id"], []) if c and pn != r["pn_id"]]
    if not known:
        return None
    pn, cards = known[0]
    return {"player": names.get(pn, pn), "cards": cards, "you": owners.get(r["game_id"]) == pn,
            "result": versus(r["cards"], cards, seen)}
