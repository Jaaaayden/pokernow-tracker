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

Each two-card show is read **on the board as it stood** (`tags.strength_at`,
with `cards.made_at` for the made hand behind it):

- a fold on the street they folded, since that is the hand they gave up;
- anything else on the board the hand ended on.

The four classes are `strength_at`'s buckets with medium and weak folded into one:

- ``strong``    top pair or an overpair, or better; preflop, `PREFLOP_STRENGTH`'s strong
- ``marginal``  middle, bottom, board or a low pocket pair; preflop, medium
- ``draw``      no pair, but a flush or straight draw, on the flop or turn
- ``air``       high card, or a draw on the river; preflop, the weak hands

A one-card show is counted, not classified: one card says little about a hand.

Shows are not `Facts`, so no spot filter reproduces a row. Each row is one hand,
and carries its `hand_id`, so it opens on its own.
"""

from __future__ import annotations

import json
import sqlite3

from ..logfmt.events import FLOP, PREFLOP, RIVER, TURN
from .cards import DEALT, DRAW_DETAILS, board_at, hand_class, leaf_of, made_at, street_of_board
from .queries import identities_of
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

_STREET_ORDER = (PREFLOP, FLOP, TURN, RIVER)

NOTE = (
    "Chosen reveals, not a sample of their range: these are the hands the player "
    "decided you should see. Everywhere else, bluffs that got folds are never shown; "
    "here they are, but only the ones the player wanted seen. Showing air after a fold "
    "or a pot nobody contested advertises bluffs; showing only strong hands sells a "
    "tight image. That choice is the tell, so the range views leave these rows out."
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
    return min(fold_street, last, key=_STREET_ORDER.index)


def _first_run(board_json: str | None) -> tuple[str, ...]:
    runs = json.loads(board_json) if board_json else []
    return tuple(runs[0]) if runs else ()


def _empty_classes() -> dict[str, int]:
    return {c: 0 for c in CLASSES}


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
        "rows": [],
        "note": NOTE,
    }
    if not ids:
        return out
    q = ",".join("?" * len(ids))
    game_args = [game] if game else []
    out["hands"] = conn.execute(
        f"SELECT count(*) FROM hand_players hp JOIN hands h ON h.hand_id = hp.hand_id"
        f" WHERE hp.pn_id IN ({q}){' AND h.game_id = ?' if game else ''}",
        [*ids, *game_args],
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT v.*,"
        f" (SELECT a.street FROM actions a WHERE a.hand_id = v.hand_id AND a.pn_id = v.pn_id"
        f"  AND a.action_type = 'fold' ORDER BY a.seq LIMIT 1) AS fold_street"
        f" FROM v_voluntary_shows v"
        f" WHERE v.pn_id IN ({q}){' AND v.game_id = ?' if game else ''}"
        f" ORDER BY v.ts DESC, v.hand_number DESC",
        [*ids, *game_args],
    ).fetchall()
    by_ctx = {c["key"]: c for c in out["contexts"]}
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
            "n_dealt_in": r["n_dealt_in"],
            "contributed": r["contributed"],
            "collected": r["collected"],
        })
    out["coverage"] = round(100.0 * out["two_card"] / out["shows"], 1) if out["shows"] else None
    return out
