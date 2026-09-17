"""Card classification: the 169 preflop classes and made-hand strength at showdown.

Two questions a range chart asks of a known holding:

1. *Which of the 169 cells is it?*  ``QsQh`` -> ``QQ``, ``AsKs`` -> ``AKs``,
   ``KdAh`` -> ``AKo``.  Rank order is canonical (higher first) so a cell has one
   label regardless of the order PokerNow printed the cards in.
2. *What did it make on this board?*  Used for the line-composition view:
   "of the hands that took this line and were shown, how many were a pair or
   worse".  The class is the true best-five category (a pair on the board with
   no hole-card involvement is still a *pair*) and the ``detail`` says how the
   hole cards participate, which is what separates top pair from a board pair.
   An unpaired holding that had a flush or straight draw on the flop or turn is
   a *draw* rather than *high_card*, so the high-card row reads as real air.

Pure functions over card strings; nothing here touches the database.
"""

from __future__ import annotations

from collections import Counter
from typing import NamedTuple

from ..logfmt.events import FLOP, PREFLOP, RIVER, TURN

#: Board cards dealt by the end of each street.
DEALT = {PREFLOP: 0, FLOP: 3, TURN: 4, RIVER: 5}


def board_at(board: list[str] | tuple[str, ...], street: str) -> tuple[str, ...] | None:
    """The cards on the table when `street` was being played, or None when the
    board never got that far -- the hand ended earlier, or was cut mid-street.

    Run-it-twice second boards repeat this prefix, so the first run is the one
    every player saw when they acted.
    """
    need = DEALT[street]
    if len(board) < need:
        return None
    return tuple(board[:need])


def street_of_board(board: tuple[str, ...] | list[str]) -> str:
    """The betting street a board of this many cards is on."""
    n = len(board)
    if n >= 5:
        return RIVER
    if n == 4:
        return TURN
    if n >= 3:
        return FLOP
    return PREFLOP


#: High to low -- this is the row/column order of the standard 13x13 chart.
RANKS = "AKQJT98765432"
RANK_VALUE = {r: 14 - i for i, r in enumerate(RANKS)}  # A=14 ... 2=2
VALUE_RANK = {v: r for r, v in RANK_VALUE.items()}

#: Made-hand classes, strongest first. The order is the sort order in reports.
#: ``draw`` is never returned by `made_class`; `made_hand` produces it for a
#: high-card holding that had a draw on the flop or turn.
MADE_CLASSES = (
    "straight_flush",
    "quads",
    "full_house",
    "flush",
    "straight",
    "trips",
    "two_pair",
    "pair",
    "draw",
    "high_card",
)

#: Every ``detail`` a ``draw`` can carry, strongest first.
DRAW_DETAILS = ("combo_draw", "flush_draw", "open_ender", "gutshot")


class Card(NamedTuple):
    value: int  # 2..14
    suit: str  # c d h s


def parse_cards(text: str | list[str] | tuple[str, ...]) -> list[Card]:
    """``"QsQh"`` or ``["Qs", "Qh"]`` -> ``[Card(12,'s'), Card(12,'h')]``.

    PokerNow uses ``T`` for ten in exports, but ``10`` is accepted too.
    """
    if isinstance(text, str):
        text = text.replace(" ", "").replace(",", "")
        tokens: list[str] = []
        i = 0
        while i < len(text):
            if text[i:i + 2] == "10":
                tokens.append("T" + text[i + 2])
                i += 3
            else:
                tokens.append(text[i:i + 2])
                i += 2
    else:
        tokens = [t.replace("10", "T") for t in text]
    out = []
    for tok in tokens:
        if len(tok) != 2 or tok[0].upper() not in RANK_VALUE or tok[1].lower() not in "cdhs":
            raise ValueError(f"bad card token: {tok!r}")
        out.append(Card(RANK_VALUE[tok[0].upper()], tok[1].lower()))
    return out


# --------------------------------------------------------------- preflop ----


def hand_class(cards: str | list[str] | tuple[str, ...]) -> str:
    """Canonical 169-class label: ``AA``, ``AKs``, ``AKo``."""
    a, b = parse_cards(cards)
    hi, lo = (a, b) if a.value >= b.value else (b, a)
    if hi.value == lo.value:
        return VALUE_RANK[hi.value] * 2
    return f"{VALUE_RANK[hi.value]}{VALUE_RANK[lo.value]}{'s' if hi.suit == lo.suit else 'o'}"


def grid_labels() -> list[list[str]]:
    """The 13x13 chart, rows and columns from A down to 2.

    Diagonal is pairs, above it suited, below it offsuit -- the layout every
    tracker uses, so a chart built from this needs no re-mapping.
    """
    rows = []
    for i, r in enumerate(RANKS):
        row = []
        for j, c in enumerate(RANKS):
            if i == j:
                row.append(r * 2)
            elif j > i:
                row.append(f"{r}{c}s")
            else:
                row.append(f"{c}{r}o")
        rows.append(row)
    return rows


ALL_CLASSES: tuple[str, ...] = tuple(label for row in grid_labels() for label in row)

#: The 169 classes strongest first, by all-in equity against one random hand --
#: the ordering every "starting hand ranking" chart reproduces (Wikipedia's
#: "Texas hold 'em starting hands", heads-up vs random). It is a *reference*, not
#: a claim about how any spot should be played: a tag that says "called a 3-bet
#: with a bottom-40% hand" is measured against this list, and SPEC.md says so.
HAND_RANKING: tuple[str, ...] = (
    "AA", "KK", "QQ", "JJ", "TT", "99", "88", "AKs", "77", "AQs", "AJs", "AKo", "ATs",
    "AQo", "66", "AJo", "KQs", "55", "A9s", "KJs", "ATo", "A8s", "KTs", "KQo", "44",
    "A7s", "A9o", "KJo", "QJs", "A5s", "A6s", "A8o", "KTo", "QTs", "33", "A4s", "K9s",
    "A7o", "JTs", "A3s", "QJo", "K8s", "A5o", "K9o", "QTo", "A6o", "A2s", "K7s", "Q9s",
    "JTo", "A4o", "K6s", "22", "A3o", "K5s", "J9s", "Q8s", "K8o", "Q9o", "T9s", "K4s",
    "A2o", "K7o", "J8s", "K3s", "Q7s", "K6o", "T8s", "Q6s", "J9o", "K2s", "K5o", "Q5s",
    "T9o", "J7s", "98s", "Q8o", "K4o", "Q4s", "J8o", "T7s", "K3o", "Q3s", "97s", "J6s",
    "T8o", "Q7o", "K2o", "87s", "J5s", "Q2s", "Q6o", "96s", "T6s", "J4s", "98o", "J7o",
    "Q5o", "86s", "T7o", "J3s", "76s", "95s", "Q4o", "T5s", "J2s", "97o", "85s", "Q3o",
    "T4s", "65s", "87o", "J6o", "75s", "T3s", "94s", "Q2o", "96o", "J5o", "84s", "T2s",
    "54s", "74s", "86o", "64s", "J4o", "93s", "T6o", "76o", "53s", "92s", "J3o", "83s",
    "95o", "63s", "73s", "85o", "T5o", "J2o", "43s", "82s", "65o", "75o", "52s", "T4o",
    "62s", "54o", "72s", "T3o", "42s", "94o", "84o", "64o", "74o", "32s", "T2o", "93o",
    "53o", "63o", "73o", "92o", "43o", "83o", "52o", "82o", "62o", "42o", "72o", "32o",
)

_HAND_PCT: dict[str, float] = {
    label: round((i + 1) / len(HAND_RANKING) * 100, 1) for i, label in enumerate(HAND_RANKING)
}


def hand_pct(label: str) -> float:
    """Where a class sits in HAND_RANKING, as a percentile: 0.6 is AA, 100 is 32o.

    `hand_pct("72o") > 90` reads as "a bottom-10% hand". Takes a `hand_class()` label.
    """
    return _HAND_PCT[label]


# --------------------------------------------------------------- showdown ---


class MadeHand(NamedTuple):
    cls: str  # one of MADE_CLASSES, or "no_board" when fewer than three board cards
    detail: str | None  # how the hole cards take part, or which draw; see `made_hand`


def _straight_high(values: set[int]) -> int | None:
    """Highest card of the best straight in `values`, wheel included; None if none."""
    vs = set(values)
    if 14 in vs:
        vs.add(1)  # the ace plays low in A-2-3-4-5
    best = None
    for high in range(5, 15):
        if all(v in vs for v in range(high - 4, high + 1)):
            best = high
    return best


def _straight_outs(hole: list[Card], board: list[Card]) -> int:
    """Ranks that would complete a straight using at least one hole card.

    Only meaningful for an unpaired, unmade holding: with no straight present,
    every straight in ``values | {r}`` contains ``r``, so the count is exactly
    the number of distinct out ranks. A four-straight on the board that the
    hole cards do not take part in is not the player's draw and is not counted.
    """
    board_values = {c.value for c in board}
    values = board_values | {c.value for c in hole}
    hole_only = {c.value for c in hole} - board_values
    if 14 in hole_only:
        hole_only.add(1)  # the ace plays low in A-2-3-4-5
    outs = 0
    for r in range(2, 15):
        if r in values:
            continue
        vs = values | {r}
        if 14 in vs:
            vs.add(1)
        for high in range(5, 15):
            window = set(range(high - 4, high + 1))
            if window <= vs and window & hole_only:
                outs += 1
                break
    return outs


def _flush_draw(hole: list[Card], board: list[Card]) -> bool:
    """Exactly four to a suit, at least one of them a hole card."""
    by_suit = Counter(c.suit for c in hole + board)
    hole_suits = {c.suit for c in hole}
    return any(n == 4 and s in hole_suits for s, n in by_suit.items())


def draw_detail(hole: list[Card], board: list[Card]) -> str | None:
    """The strongest draw an unpaired holding has on this board, or None.

    - ``combo_draw``  a flush draw plus any straight draw
    - ``flush_draw``  four to a suit including a hole card
    - ``open_ender``  two ranks complete a straight (open-ended or double gutshot)
    - ``gutshot``     one rank completes a straight

    Meant for a holding whose best five is high card; on a made hand the
    straight-out count is not meaningful.
    """
    fd = _flush_draw(hole, board)
    outs = _straight_outs(hole, board)
    if fd and outs >= 1:
        return "combo_draw"
    if fd:
        return "flush_draw"
    if outs >= 2:
        return "open_ender"
    if outs == 1:
        return "gutshot"
    return None


def made_class(cards: list[Card]) -> str:
    """Best-five category of any 5..7 cards."""
    by_suit: dict[str, set[int]] = {}
    for c in cards:
        by_suit.setdefault(c.suit, set()).add(c.value)
    flush_suit = next((s for s, vs in by_suit.items() if len(vs) >= 5), None)
    if flush_suit and _straight_high(by_suit[flush_suit]) is not None:
        return "straight_flush"

    counts = sorted(Counter(c.value for c in cards).values(), reverse=True)
    if counts[0] == 4:
        return "quads"
    if counts[0] == 3 and len(counts) > 1 and counts[1] >= 2:
        return "full_house"
    if flush_suit:
        return "flush"
    if _straight_high({c.value for c in cards}) is not None:
        return "straight"
    if counts[0] == 3:
        return "trips"
    if counts[0] == 2 and counts[1] == 2:
        return "two_pair"
    if counts[0] == 2:
        return "pair"
    return "high_card"


def made_hand(hole: str | list[str] | tuple[str, ...], board: list[str] | tuple[str, ...]) -> MadeHand:
    """Classify a known holding on a board.

    ``detail`` for a **pair** says which pair it is -- this is the difference
    between a value hand and air, and the class alone cannot tell them apart:

    - ``overpair``     pocket pair above every board card
    - ``pocket_pair``  pocket pair with at least one board card above it
    - ``top_pair``     a hole card pairs the highest board rank
    - ``middle_pair``  a hole card pairs a board rank that is neither highest nor lowest
    - ``bottom_pair``  a hole card pairs the lowest board rank
    - ``board_pair``   the pair is entirely on the board; the hole cards add nothing

    For **trips**: ``set`` (pocket pair + one board card) or ``trips`` (one hole
    card + a board pair).

    A holding that is only **high_card** on the final board becomes a **draw**
    when it had one on the flop or the turn, with ``detail`` naming the draw
    (see `draw_detail`): ``combo_draw``, ``flush_draw``, ``open_ender`` or
    ``gutshot``. Draws are read on the turn board (or the flop, when the hand
    ended there); since the final hand is unmade, the turn's draws include the
    flop's. A busted draw on the river is still a draw -- that is what the
    player was betting with. A draw must use a hole card; four to a flush or a
    four-straight sitting on the board alone does not count. Any pair or better
    keeps its class even with a draw.

    Every other class has ``detail = None``.
    """
    h = parse_cards(hole)
    b = parse_cards(board)
    if len(b) < 3:
        return MadeHand("no_board", None)

    cls = made_class(h + b)
    if cls == "high_card":
        d = draw_detail(h, b[:4])
        return MadeHand("draw", d) if d else MadeHand("high_card", None)
    if cls not in ("pair", "trips"):
        return MadeHand(cls, None)

    board_values = sorted({c.value for c in b}, reverse=True)
    hv = [c.value for c in h]
    pocket = hv[0] == hv[1]

    if cls == "trips":
        return MadeHand(cls, "set" if pocket else "trips")

    if pocket:
        return MadeHand(cls, "overpair" if hv[0] > board_values[0] else "pocket_pair")

    paired = [v for v in hv if v in board_values]
    if not paired:
        return MadeHand(cls, "board_pair")
    v = paired[0]
    if v == board_values[0]:
        return MadeHand(cls, "top_pair")
    if v == board_values[-1]:
        return MadeHand(cls, "bottom_pair")
    return MadeHand(cls, "middle_pair")


# ------------------------------------------------------------------ boards ---

#: Every tag `board_texture` can produce, for documentation and validation.
TEXTURE_TAGS: tuple[str, ...] = (
    # highest card
    "ace_high", "king_high", "queen_high", "jack_high", "ten_high", "low",
    # suits
    "monotone", "twotone", "rainbow", "flush_possible", "straight_possible",
    # pairing
    "paired", "unpaired", "double_paired", "trips",
    # connectivity
    "connected", "three_connected", "disconnected",
    # rank mix
    "all_broadway", "no_broadway",
)

_HIGH_TAG = {14: "ace_high", 13: "king_high", 12: "queen_high", 11: "jack_high", 10: "ten_high"}


def board_texture(board: list[str] | tuple[str, ...]) -> frozenset[str]:
    """Texture tags for a flop, turn or river board (three to five cards).

    Tags are descriptive and overlap on purpose -- a monotone flop is also
    ``flush_possible``, a trips board is also ``paired`` -- so a filter can be
    as broad or as narrow as the question. Fewer than three cards gives no tags.

    - highest card: exactly one of ``ace_high`` … ``ten_high`` or ``low`` (9-high or below)
    - suits: ``monotone`` (all one suit), ``rainbow`` (no suit repeated),
      ``twotone`` (exactly two suits present), ``flush_possible`` (three or more of a suit),
      ``straight_possible`` (three ranks inside one five-rank window, so two hole
      cards can make a straight)
    - pairing: ``paired`` (any rank repeated), ``double_paired``, ``trips``, else ``unpaired``
    - connectivity: ``connected`` (two ranks adjacent, ace plays high and low),
      ``three_connected`` (three in a row, e.g. 8-7-6), else ``disconnected``
    - rank mix: ``all_broadway`` (every card T or above), ``no_broadway``
    """
    cards = parse_cards(board)
    if len(cards) < 3:
        return frozenset()
    tags: set[str] = set()

    values = [c.value for c in cards]
    tags.add(_HIGH_TAG.get(max(values), "low"))

    suits = Counter(c.suit for c in cards)
    top = max(suits.values())
    if top == len(cards):
        tags.add("monotone")
    if top == 1:
        tags.add("rainbow")
    if len(suits) == 2:
        tags.add("twotone")
    if top >= 3:
        tags.add("flush_possible")
    if straight_possible(board):
        tags.add("straight_possible")

    ranks = Counter(values)
    if max(ranks.values()) >= 3:
        tags.update({"trips", "paired"})
    elif max(ranks.values()) == 2:
        tags.add("paired")
        if sum(1 for n in ranks.values() if n == 2) >= 2:
            tags.add("double_paired")
    else:
        tags.add("unpaired")

    distinct = set(values)
    if 14 in distinct:
        distinct.add(1)
    run = max_run = 1
    prev = None
    for v in sorted(distinct):
        run = run + 1 if prev is not None and v == prev + 1 else 1
        max_run = max(max_run, run)
        prev = v
    if max_run >= 3:
        tags.update({"three_connected", "connected"})
    elif max_run == 2:
        tags.add("connected")
    else:
        tags.add("disconnected")

    if all(v >= 10 for v in values):
        tags.add("all_broadway")
    if all(v < 10 for v in values):
        tags.add("no_broadway")
    return frozenset(tags)


def straight_possible(board: list[str] | tuple[str, ...]) -> bool:
    """Two hole cards could make a straight on this board.

    True when three or more distinct board ranks fit inside one five-rank window,
    the ace playing both high and low. Fewer than three board cards is False.
    """
    cards = parse_cards(board)
    if len(cards) < 3:
        return False
    values = {c.value for c in cards}
    if 14 in values:
        values.add(1)
    return any(len(values & set(range(low, low + 5))) >= 3 for low in range(1, 11))


# -------------------------------------------------------------- holdings ---


def top_kicker(hole: str | list[str] | tuple[str, ...], board: list[str] | tuple[str, ...]) -> bool:
    """Top pair with the best kicker there is: TPTK.

    The hole card that does not pair the board is the highest rank missing from
    the board, so no other top-pair holding outkicks it. ``AK`` on ``K72`` and on
    ``A72`` are both TPTK; ``AQ`` on ``A72`` is not. False for anything that is not
    top pair, pocket pairs included.
    """
    if made_hand(hole, board) != MadeHand("pair", "top_pair"):
        return False
    h = parse_cards(hole)
    board_values = {c.value for c in parse_cards(board)}
    kicker = next((c.value for c in h if c.value not in board_values), None)
    if kicker is None:
        return False
    best = max(v for v in range(2, 15) if v not in board_values)
    return kicker == best


def top_two_pair(hole: str | list[str] | tuple[str, ...], board: list[str] | tuple[str, ...]) -> bool:
    """Two pair made with both hole cards, pairing the board's two highest ranks."""
    if made_hand(hole, board).cls != "two_pair":
        return False
    hv = {c.value for c in parse_cards(hole)}
    if len(hv) != 2:
        return False
    board_values = sorted({c.value for c in parse_cards(board)}, reverse=True)
    return hv == set(board_values[:2])
