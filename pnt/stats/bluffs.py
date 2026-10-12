"""Bluff:value: what a player bet and raised with, by the kind of aggression.

Every bet, check-raise, raise and jam is a claim to have it. The shown ones say
how often the claim was true. Each category here is one kind of aggression on
one street, and its shown hands are read **on the board as it stood when the
action was made** (`tags.strength_at`), so a flop flush draw that got there on
the river was a semi-bluff when it bet the flop:

- ``value``       strong: top pair or an overpair, or better
- ``marginal``    medium or weak: middle, bottom, board or a low pocket pair --
                  neither a bluff nor a hand betting to be called by worse
- ``semi_bluff``  no pair, but a flush or straight draw, on the flop or turn
- ``bluff``       high card; on the river a draw has missed, so it is a bluff too

The ratio is ``value : (bluff + semi_bluff)``. A marginal hand is left out of
both sides: it is betting for thin value or protection, and the ratio is the one
an opponent needs when deciding whether to call.

**Read coverage first, and read it against the folded-through share.** Cards are
known when a hand is shown, and a bet everyone folds to is never shown -- that is
exactly the bluff that worked. So the shown hands lean to value, more so the more
of a category got folds. A bet category carries its ``folded`` share, from
`bet_answer`, as that context. The player whose logs these are sees their own
cards on every hand, so their own rows are close to complete.

Every category is a filter (`filter`), and so is every class within it
(`class_filters`): clicking a row reproduces exactly its hands. The one roll-up
across streets, ``all aggression``, has no filter, since a filter cannot say
"bet or raised on any street"; it is listed by its hands instead.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from ..logfmt.events import RIVER
from .derive import POSTFLOP_STREETS, SIZE_BUCKETS, Facts
from .queries import facts_by_player
from .tags import strength_at

#: The four classes, strongest claim first: the order the stacked bar is drawn in.
CLASSES = ("value", "marginal", "semi_bluff", "bluff")

#: The categories on each street, in the order a street's rows are listed.
#: (kind, size bucket or None). `raise` is every raise facing a bet on the street,
#: check-raises included: a check-raise is the raise made from out of position,
#: and its own row splits it out.
STREET_KINDS: tuple[tuple[str, str | None], ...] = (
    *(("bet", s) for s in SIZE_BUCKETS),
    ("check_raise", None),
    ("raise", None),
    ("jam", None),
)

_SIZE_WORDS = {
    "blocker": "blocker bet, under 1/5 pot",
    "small": "small bet, 1/5 to 1/2",
    "medium": "medium bet, 1/2 to 3/4",
    "large": "large bet, 3/4 to pot",
    "overbet": "overbet",
}


def classify(strength: str | None, street: str) -> str | None:
    """A `strength_at` bucket as one of CLASSES, or None when it is unknown."""
    if strength is None:
        return None
    if strength == "strong":
        return "value"
    if strength in ("medium", "weak"):
        return "marginal"
    if strength == "draw":
        # `strength_at` already reads a river draw as air; a draw on the river is
        # a bluff either way.
        return "bluff" if street == RIVER else "semi_bluff"
    return "bluff"


def class_terms(street: str) -> dict[str, str | None]:
    """Each class as a `made_<street>` term, so a class within a row reproduces too.

    These are `strength_at`'s buckets written as MADE_LEAVES spans: strong is top
    pair and up (two pair and better rank above it), the marginal pairs are the
    four below it, and a draw is the `draw` group. On the river nothing is a
    semi-bluff, and a bluff is anything short of a pair -- the busted draws as
    well as high card. `test_every_class_reproduces` holds this to `strength_at`.
    """
    river = street == RIVER
    return {
        "value": f"made_{street}>=top_pair",
        "marginal": f"made_{street}=pocket_pair|middle_pair|bottom_pair|board_pair",
        "semi_bluff": None if river else f"made_{street}=draw",
        "bluff": f"made_{street}<pair" if river else f"made_{street}=high_card",
    }


def ratio_text(value: int, bluffs: int) -> str:
    """``value : bluffs`` as "2.1 : 1": "∞ : 1" for value with no bluff beside it,
    "0 : 1" for bluffs alone, and "--" when neither was shown."""
    if not value and not bluffs:
        return "--"
    if not bluffs:
        return "∞ : 1"
    return f"{value / bluffs:.1f} : 1"


def _pct(num: int, den: int) -> float | None:
    return round(100.0 * num / den, 1) if den else None


def _last_aggression(f: Facts) -> str | None:
    """The last postflop street they bet or raised on: what `all aggression` reads."""
    for s in reversed(POSTFLOP_STREETS):
        line = f.line.get(s, "")
        if "b" in line or "r" in line:
            return s
    return None


def categories() -> list[dict]:
    """Every category: key, street, kind, size, label, filter, and its membership
    test. The street a row's hands are read on is `street`, except for ``all
    aggression``, which reads each hand on its last aggressive street."""
    out: list[dict] = []

    def add(key, street, kind, size, label, term, test, read_on):
        out.append({
            "key": key, "street": street, "kind": kind, "size": size,
            "label": label, "filter": term, "test": test, "read_on": read_on,
        })

    for s in POSTFLOP_STREETS:
        for kind, size in STREET_KINDS:
            if kind == "bet":
                add(f"bet_{s}_{size}", s, kind, size, _SIZE_WORDS[size], f"bet_{s}={size}",
                    lambda f, s=s, z=size: f.bet_size.get(s) == z, s)
            elif kind == "check_raise":
                add(f"check_raise_{s}", s, kind, None, "check-raise", f"check_raise_{s}",
                    lambda f, s=s: bool(f.check_raise.get(s)), s)
            elif kind == "raise":
                add(f"raise_{s}", s, kind, None, "raise (check-raises too)", f"raised_bet_{s}",
                    lambda f, s=s: bool(f.raised_bet.get(s)), s)
            else:
                add(f"jam_{s}", s, kind, None, "jam (all-in bet or raise)", f"jam_{s}",
                    lambda f, s=s: bool(f.jam.get(s)), s)
    add("bet_river_all", RIVER, "all_bets", None, "every river bet", "bet_river",
        lambda f: RIVER in f.bet_pot, RIVER)
    add("aggression", "all", "aggression", None, "all aggression", None,
        lambda f: _last_aggression(f) is not None, None)
    return out


#: key -> category, for the pool's hand list.
CATEGORY = {c["key"]: c for c in categories()}


def members(facts: Iterable[Facts], key: str, cls: str | None = None) -> list[Facts]:
    """The rows of one category, or of one class within it. Raises on an unknown
    key or class, so a mistyped one is an error rather than an empty list."""
    if key not in CATEGORY:
        raise ValueError(f"unknown category {key!r}. Known: {', '.join(CATEGORY)}")
    if cls is not None and cls not in CLASSES:
        raise ValueError(f"unknown class {cls!r}. Known: {', '.join(CLASSES)}")
    cat = CATEGORY[key]
    rows = [f for f in facts if cat["test"](f)]
    if cls is None:
        return rows
    return [f for f in rows if _class_of(f, cat) == cls]


def _class_of(f: Facts, cat: dict) -> str | None:
    street = cat["read_on"] or _last_aggression(f)
    return classify(strength_at(f.hole_cards, f.board, street), street)


def _row(cat: dict, rows: list[Facts], ids: bool) -> dict:
    counts = dict.fromkeys(CLASSES, 0)
    by_class: dict[str, list[int]] = {c: [] for c in CLASSES}
    for f in rows:
        cls = _class_of(f, cat)
        if cls is not None:
            counts[cls] += 1
            by_class[cls].append(f.hand_id)
    known = sum(counts.values())
    bluffs = counts["bluff"] + counts["semi_bluff"]
    out = {
        "key": cat["key"],
        "street": cat["street"],
        "kind": cat["kind"],
        "size": cat["size"],
        "label": cat["label"],
        "filter": cat["filter"],
        "class_filters": (
            {c: (f"{cat['filter']},{t}" if t else None) for c, t in class_terms(cat["street"]).items()}
            if cat["filter"] else dict.fromkeys(CLASSES)
        ),
        "n": len(rows),
        "known": known,
        "coverage": _pct(known, len(rows)),
        "counts": counts,
        "pct": {c: _pct(v, known) for c, v in counts.items()},
        "ratio": round(counts["value"] / bluffs, 2) if bluffs else None,
        "ratio_text": ratio_text(counts["value"], bluffs),
        "folded": None,
        "folded_pct": None,
    }
    # What a bet got is known for bets only: a raise's answer is not a fact.
    if cat["kind"] in ("bet", "all_bets"):
        folded = sum(1 for f in rows if f.bet_answer.get(cat["street"]) == "fold")
        out["folded"] = folded
        out["folded_pct"] = _pct(folded, len(rows))
    if ids:
        out["hand_ids"] = [f.hand_id for f in rows]
        out["class_ids"] = by_class
    return out


def bluff_value(facts: Iterable[Facts], ids: bool = True) -> dict:
    """Every category's shown-hand split and ratio over an already-filtered set.

    `facts` is one player's rows, or every player's for the pool: each row is one
    (hand, player), so a pool category counts a hand once per player who took the
    line in it. `ids` adds each row's `hand_ids` and per-class `class_ids`, which
    one player's drill-down lists by; a pool's hand ids name nobody and are left out.
    """
    facts = list(facts)
    rows = [_row(cat, [f for f in facts if cat["test"](f)], ids) for cat in categories()]
    return {
        "hands": len(facts),
        "classes": list(CLASSES),
        "streets": [*POSTFLOP_STREETS, "all"],
        "rows": rows,
    }


def pool_facts(conn: sqlite3.Connection, game_id: str | None = None) -> list[Facts]:
    """Every player's rows over every hand: the pool a pool category counts.

    Derived afresh on each call, like a filtered `report()`: the Facts of the
    whole database are too big to keep, and the pool is asked for by a person
    clicking, not on a timer.
    """
    grouped, _, _ = facts_by_player(conn, game_id)
    return [f for rows in grouped.values() for f in rows]
