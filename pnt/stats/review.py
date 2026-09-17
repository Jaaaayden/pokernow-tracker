"""Hand review: the hands worth a second look, and the ones that were just bad luck.

Two groups of flags, one row per (hand, flag) for the player being reviewed:

**Mistakes** -- a prompt to open the replay, never a verdict.

- ``missed_bluff``  a bloated pot checked down on the river where every hand
  shown was a board pair or worse: someone could have bet and taken it.
- ``missed_value``  two hands good enough to play for stacks, on a board with no
  flush or straight, and less than half the stacks went in.
- ``failed_bluff``  a postflop bet or raise with air that was called or raised,
  and lost. The row carries the size and who called, with their profile, so the
  question "was it the sizing or the target" has its evidence beside it.

**Beats** -- bookkeeping.

- ``suckout``      all in ahead, lost.
- ``cooler_pre``   a premium preflop all-in that was behind, and lost.
- ``cooler_post``  a hand good enough to play for stacks that was behind a better
  one when the money went in, and lost.

Definitions and every threshold are in SPEC.md, "Hand review"; `THRESHOLDS` is
the other copy. Like the rest of `pnt/stats`, nothing here is stored: rows are
derived at read time from the hands, and all-in equities come from `allin.py`
and its cache.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..db.conn import writing
from ..logfmt.events import PREFLOP, RIVER
from .allin import AllInRow, allin_rows, decision_street
from .cards import (
    MADE_CLASSES,
    VALUE_RANK,
    MadeHand,
    board_at,
    board_texture,
    hand_class,
    made_class,
    made_hand,
    parse_cards,
    top_kicker,
    top_two_pair,
)
from .derive import AGGRESSIVE, POSTFLOP_STREETS, Facts, HandPlayerRow, HandRow, derive, size_bucket
from .equity import best_of
from .queries import display_names, facts_cached, hand_list, identities_of, identity_map, load_hands
from .tags import tags_for

#: Every cutoff the flags use. Mirrored as the table in SPEC.md, "Hand review".
THRESHOLDS: dict = {
    # A pot at least this many big blinds at the end is "bloated": worth a look
    # when it checks down, and big enough to count as a postflop cooler.
    "bloated_pot_bb": 15,
    # Missed value: less than this share of the two effective stacks went in.
    "stacks_in_share": 0.5,
    # A failed bluff is only worth reviewing in a pot at least this big.
    "failed_bluff_pot_bb": 15,
    # A suckout was at least this far ahead: a 55% flip that loses is a flip.
    "suckout_equity": 0.6,
    # A preflop cooler was at most this far ahead.
    "cooler_equity": 0.5,
    # A 3-bet pot this deep or shallower plays for stacks with TPTK; deeper, like
    # a single-raised pot, it wants top two pair or better.
    "deep_bb": 120,
    "premium": ("AA", "KK", "QQ", "AKs", "AKo"),
}

#: Each flag and the group it lists under: the Hand review tab or the Bad beats one.
KINDS: dict[str, str] = {
    "missed_bluff": "mistake",
    "missed_value": "mistake",
    "failed_bluff": "mistake",
    "suckout": "beat",
    "cooler_pre": "beat",
    "cooler_post": "beat",
}

KIND_LABELS: dict[str, str] = {
    "missed_bluff": "Missed bluff",
    "missed_value": "Missed value",
    "failed_bluff": "Failed bluff",
    "suckout": "Suckout",
    "cooler_pre": "Preflop cooler",
    "cooler_post": "Postflop cooler",
}


@dataclass(slots=True)
class Villain:
    """Someone the flag was about: a showdown opponent, or the player who called."""

    pn_id: str
    cards: str | None
    #: Their made hand on the board the flag is judged on; None when unknown.
    made: MadeHand | None = None
    #: failed_bluff only: what they did about the bluff, `called` or `raised`.
    answer: str | None = None
    #: failed_bluff only: they had bet or raised earlier in the hand, so their
    #: range still holds the strong hands. A heuristic, stated as one in SPEC.md.
    uncapped: bool | None = None
    archetype: str | None = None
    wtsd_pct: float | None = None
    folds_river_pct: float | None = None


@dataclass(slots=True)
class ReviewRow:
    hand_id: int
    pn_id: str
    kind: str
    why: str
    #: The street the flag is judged on: the river for a check-down, the bluff's
    #: street, or where the money went in.
    street: str
    #: The reviewed player's made hand on that street's board; None preflop.
    made: MadeHand | None
    villains: list[Villain]
    pot_bb: float | None
    #: The smallest starting stack among the player and the villains, in bb.
    eff_bb: float | None
    #: Stack-to-pot ratio on the flop among the same players; None without a flop.
    spr: float | None
    # failed_bluff
    bluff_kind: str | None = None
    bluff_size: str | None = None
    bluff_pot: float | None = None
    answer: str | None = None
    # beats with an all-in
    equity: float | None = None
    diff_bb: float | None = None
    method: str | None = None


@dataclass
class Context:
    """What the rules need beyond the hand itself."""

    names: Mapping[str, str] = field(default_factory=dict)
    #: (hand_id, pn_id) -> that player's all-in row, where the hand has one.
    allin: Mapping[tuple[int, str], AllInRow] = field(default_factory=dict)
    #: pn_id -> {"archetype", "wtsd_pct", "folds_river_pct"}; called lazily.
    villain_info: Callable[[str], dict] = lambda _pid: {}
    #: (hand_id, pn_id) pairs a rule could not judge because a stack was unknown.
    stack_unknown: set[tuple[int, str]] = field(default_factory=set)

    def name(self, pn_id: str) -> str:
        return self.names.get(pn_id, pn_id)


# ---------------------------------------------------------------- helpers ---


def _board(board: Collection[str] | str) -> tuple[str, ...]:
    """A board as a tuple of card strings, given that or one string of cards."""
    if isinstance(board, str):
        text = board.replace(" ", "")
        return tuple(text[i : i + 2] for i in range(0, len(text), 2))
    return tuple(board)


def is_air(mh: MadeHand) -> bool:
    """High card, a draw, or a pair that is entirely on the board.

    An underpair or bottom pair is a hand, not air: checking it down is a thin
    value question, not a missed bluff.
    """
    return mh.cls in ("high_card", "draw") or (mh.cls == "pair" and mh.detail == "board_pair")


def tier(pot_level: int, eff_bb: float | None) -> str:
    """What a hand must be to play for stacks in this pot.

    ``tptk_plus`` in a 4-bet pot, or a 3-bet pot no deeper than `deep_bb`;
    ``two_pair_plus`` everywhere else.
    """
    if pot_level >= 4:
        return "tptk_plus"
    if pot_level == 3 and eff_bb is not None and eff_bb <= THRESHOLDS["deep_bb"]:
        return "tptk_plus"
    return "two_pair_plus"


_TWO_PAIR = MADE_CLASSES.index("two_pair")


def both_cards_play(hole: str, board: Collection[str]) -> bool:
    """The hand's class needs both hole cards: either one alone makes less.

    A set, a full house from a pocket pair, top two pair. Not trips from one
    hole card on a paired board, and not a full house the board made with one
    card's help -- the hands everyone at the table can have a piece of.
    """
    board_cards = parse_cards(_board(board))
    h = parse_cards(hole)
    full = MADE_CLASSES.index(made_class(h + board_cards))
    return all(MADE_CLASSES.index(made_class([c, *board_cards])) > full for c in h)


def _overpair(hole: str, board: Collection[str]) -> bool:
    a, b = parse_cards(hole)
    return a.value == b.value and a.value > max(c.value for c in parse_cards(_board(board)))


def _tptk(hole: str, board: Collection[str]) -> bool:
    """Top pair top kicker, whether or not the board is also paired."""
    board_values = {c.value for c in parse_cards(_board(board))}
    a, b = parse_cards(hole)
    if a.value == b.value:
        return False
    top = max(board_values)
    best_kicker = max(v for v in range(2, 15) if v not in board_values)
    return {a.value, b.value} == {top, best_kicker}


def meets_tier(hole: str, board: Collection[str], tier_name: str) -> bool:
    """Does this holding play for stacks at this tier, on this board?

    ``two_pair_plus``: top two pair, or trips or better that needs both hole
    cards (a set; not one card to a paired board).
    ``tptk_plus``: any of those, an overpair, or top pair top kicker.
    """
    board = _board(board)
    cls = made_class(parse_cards(hole) + parse_cards(board))
    big = MADE_CLASSES.index(cls) < _TWO_PAIR and both_cards_play(hole, board)
    if big or top_two_pair(hole, board):
        return True
    return tier_name == "tptk_plus" and (_overpair(hole, board) or _tptk(hole, board))


def stacks_board(board: Collection[str]) -> bool:
    """No flush, no straight and no trips on the board: the hands are what they look like."""
    return not ({"flush_possible", "straight_possible", "trips"} & board_texture(_board(board)))


def dry_streets(board: Collection[str], last: str = RIVER):
    """(street, board as it stood) for each postflop street up to `last` whose
    board passes `stacks_board`, earliest first.

    A five-card river nearly always has three ranks inside a straight window or
    three of a suit, so a hand is judged on the street the board was still dry:
    that is where the chance to get the stacks in was.
    """
    for street in POSTFLOP_STREETS:
        seen = board_at(_board(board), street)
        if seen is None:
            return
        if stacks_board(seen):
            yield street, seen
        if street == last:
            return


def describe(hole: str | None, board: Collection[str]) -> str:
    """A made hand in a few words, for a `why` line."""
    if not hole:
        return "unknown cards"
    board = _board(board)
    if len(board) < 3:
        return hand_class(hole)
    mh = made_hand(hole, board)
    if mh.cls == "high_card":
        return f"{VALUE_RANK[max(c.value for c in parse_cards(hole))]}-high"
    if mh.cls == "draw":
        draw = (mh.detail or "draw").replace("_", " ")
        return f"missed {draw}" if len(board) == 5 else draw
    if mh.cls == "pair":
        if mh.detail == "top_pair" and top_kicker(hole, board):
            return "top pair top kicker"
        return {"pocket_pair": "underpair"}.get(mh.detail or "", (mh.detail or "pair").replace("_", " "))
    if mh.cls == "two_pair":
        if top_two_pair(hole, board):
            return "top two pair"
        if _overpair(hole, board):
            return "overpair"
        return "top pair top kicker" if _tptk(hole, board) else "two pair"
    if mh.cls == "trips":
        return "set" if mh.detail == "set" else "trips"
    return mh.cls.replace("_", " ")


def _live(hand: HandRow) -> list[HandPlayerRow]:
    return [p for p in sorted(hand.players.values(), key=lambda p: p.seat) if not p.folded]


def _showdown_known(hand: HandRow, pid: str) -> bool:
    """A complete showdown the player was in, with every live hand known."""
    me = hand.players.get(pid)
    if me is None or me.folded or not hand.complete or not hand.went_to_showdown:
        return False
    return all(p.hole_cards for p in _live(hand))


def _net(p: HandPlayerRow) -> int:
    return p.collected - p.contributed + p.bounty


def _bb(chips: float | None, bb: int | None, nd: int = 1) -> float | None:
    return round(chips / bb, nd) if chips is not None and bb else None


def _eff(hand: HandRow, pids: Collection[str]) -> int | None:
    stacks = [hand.players[q].starting_stack for q in pids]
    if not stacks or any(s is None for s in stacks):
        return None
    return min(stacks)


def _spr(hand: HandRow, f: Facts, pids: Collection[str]) -> float | None:
    """Effective stack behind on the flop over the pot on the flop."""
    flop_pot = f.pot_at.get("flop")
    if not flop_pot:
        return None
    behind = []
    for q in pids:
        p = hand.players[q]
        if p.starting_stack is None:
            return None
        put = sum(a.amount for a in hand.actions if a.street == PREFLOP and a.pn_id == q)
        behind.append(p.starting_stack - put)
    return round(max(min(behind), 0) / flop_pot, 1) if behind else None


def _river_checked_through(hand: HandRow) -> bool:
    river = [a for a in hand.actions if a.street == RIVER and not a.is_forced]
    return len({a.pn_id for a in river}) >= 2 and all(a.kind == "check" for a in river)


def _row(hand: HandRow, f: Facts, pid: str, kind: str, why: str, street: str,
         made: MadeHand | None, villains: list[Villain], **extra) -> ReviewRow:
    pids = [pid, *(v.pn_id for v in villains if v.pn_id in hand.players)]
    return ReviewRow(
        hand_id=hand.hand_id,
        pn_id=pid,
        kind=kind,
        why=why,
        street=street,
        made=made,
        villains=villains,
        pot_bb=_bb(f.pot, hand.bb),
        eff_bb=_bb(_eff(hand, pids), hand.bb),
        spr=_spr(hand, f, pids),
        **extra,
    )


def _vs(ctx: Context, villains: list[Villain], board: Collection[str]) -> str:
    return ", ".join(f"{describe(v.cards, board)} ({ctx.name(v.pn_id)})" for v in villains)


# ------------------------------------------------------------------ rules ---


def missed_bluff(hand: HandRow, facts: Mapping[str, Facts], pid: str, ctx: Context) -> ReviewRow | None:
    """A bloated pot checked down on the river, every shown hand a board pair or worse."""
    if not _showdown_known(hand, pid) or len(hand.board) < 5 or not hand.bb:
        return None
    if not _river_checked_through(hand):
        return None
    f = facts[pid]
    if f.pot / hand.bb < THRESHOLDS["bloated_pot_bb"]:
        return None
    live = _live(hand)
    made = {p.pn_id: made_hand(p.hole_cards, hand.board) for p in live}
    if not all(is_air(m) for m in made.values()):
        return None
    villains = [Villain(p.pn_id, p.hole_cards, made[p.pn_id]) for p in live if p.pn_id != pid]
    me = hand.players[pid]
    why = (
        f"{f.pot / hand.bb:.0f}bb pot checked down on the river: "
        f"{describe(me.hole_cards, hand.board)} vs {_vs(ctx, villains, hand.board)}"
    )
    return _row(hand, f, pid, "missed_bluff", why, RIVER, made[pid], villains)


def missed_value(hand: HandRow, facts: Mapping[str, Facts], pid: str, ctx: Context) -> ReviewRow | None:
    """Two stacks hands on a dry board, and less than half the stacks went in.

    Judged on the earliest street the board was dry and both hands already
    qualified (`dry_streets`).
    """
    if not _showdown_known(hand, pid) or len(hand.board) < 5 or not hand.bb:
        return None
    if any(a.all_in for a in hand.actions):
        return None
    f = facts[pid]
    me = hand.players[pid]
    for v in _live(hand):
        if v.pn_id == pid:
            continue
        eff = _eff(hand, [pid, v.pn_id])
        if eff is None:
            ctx.stack_unknown.add((hand.hand_id, pid))
            continue
        if f.pot >= THRESHOLDS["stacks_in_share"] * 2 * eff:
            continue
        t = tier(f.pot_level, eff / hand.bb)
        for street, seen in dry_streets(hand.board):
            if not (meets_tier(me.hole_cards, seen, t) and meets_tier(v.hole_cards, seen, t)):
                continue
            villain = Villain(v.pn_id, v.hole_cards, made_hand(v.hole_cards, seen))
            why = (
                f"{describe(me.hole_cards, seen)} vs {_vs(ctx, [villain], seen)} on the {street}, "
                f"{eff / hand.bb:.0f}bb deep: only a {f.pot / hand.bb:.0f}bb pot went in"
            )
            return _row(hand, f, pid, "missed_value", why, street, made_hand(me.hole_cards, seen), [villain])
    return None


def failed_bluff(hand: HandRow, facts: Mapping[str, Facts], pid: str, ctx: Context) -> ReviewRow | None:
    """A postflop bet or raise with air that was called or raised, in a hand they lost.

    Judged at the last such bet in the hand: a flop bluff that was called and
    then barrelled on the turn is one row, on the turn. A draw on the flop or
    turn is a semi-bluff with outs, not air; a draw that bets the river has
    missed, and is.
    """
    me = hand.players.get(pid)
    if me is None or not hand.complete or not me.hole_cards or _net(me) >= 0 or not hand.bb:
        return None
    if facts[pid].pot / hand.bb < THRESHOLDS["failed_bluff_pot_bb"]:
        return None
    acts = [a for a in hand.actions if not a.is_forced]
    found = None
    for i, a in enumerate(acts):
        if a.pn_id != pid or a.kind not in AGGRESSIVE or a.street == PREFLOP:
            continue
        seen = board_at(hand.board, a.street)
        if not seen:
            continue
        mh = made_hand(me.hole_cards, seen)
        if not is_air(mh) or (mh.cls == "draw" and a.street != RIVER):
            continue
        responders: list[tuple[str, str]] = []
        for b in acts[i + 1 :]:
            if b.street != a.street:
                break
            if b.pn_id == pid:
                continue
            if b.kind in AGGRESSIVE:
                responders.append((b.pn_id, "raised"))
                break
            if b.kind == "call":
                responders.append((b.pn_id, "called"))
        if responders:
            found = (a, seen, mh, responders)
    if found is None:
        return None

    a, seen, mh, responders = found
    pot_before = sum(x.amount for x in hand.actions if x.seq < a.seq)
    answer = "raised" if any(r == "raised" for _, r in responders) else "called"
    villains = []
    for q, r in responders:
        cards = hand.players[q].hole_cards if q in hand.players else None
        info = ctx.villain_info(q) or {}
        villains.append(Villain(
            pn_id=q,
            cards=cards,
            made=made_hand(cards, seen) if cards else None,
            answer=r,
            uncapped=any(x.pn_id == q and x.kind in AGGRESSIVE and x.seq < a.seq for x in acts),
            archetype=info.get("archetype"),
            wtsd_pct=info.get("wtsd_pct"),
            folds_river_pct=info.get("folds_river_pct"),
        ))

    size = f"{a.amount / pot_before:.0%} pot" if pot_before else f"{a.amount} chips"
    who = []
    for v in villains:
        bits = [b for b in (v.archetype, f"WTSD {v.wtsd_pct:.0f}%" if v.wtsd_pct is not None else None) if b]
        held = f" with {describe(v.cards, seen)}" if v.cards else ""
        who.append(f"{v.answer} by {ctx.name(v.pn_id)}{' (' + ', '.join(bits) + ')' if bits else ''}{held}")
    lost = _bb(-_net(me), hand.bb, 0)
    why = (
        f"{'Bet' if a.kind == 'bet' else 'Raised'} the {a.street} {size} with {describe(me.hole_cards, seen)}, "
        f"{'; '.join(who)}{f', lost {lost:.0f}bb' if lost is not None else ', lost'}"
    )
    return _row(
        hand, facts[pid], pid, "failed_bluff", why, a.street, mh, villains,
        bluff_kind=a.kind,
        bluff_size=size_bucket(a.amount, pot_before) if a.kind == "bet" and pot_before > 0 else None,
        bluff_pot=round(a.amount / pot_before, 2) if pot_before else None,
        answer=answer,
    )


def _allin_villains(r: AllInRow) -> list[Villain]:
    board = tuple(r.board)
    return [Villain(q, c, made_hand(c, board) if len(board) >= 3 else None) for q, c in r.villains]


def _beat_row(hand, facts, pid, kind, why, r: AllInRow, villains) -> ReviewRow:
    board = tuple(r.board)
    return _row(
        hand, facts[pid], pid, kind, why, r.street,
        made_hand(r.hole_cards, board) if len(board) >= 3 else None, villains,
        equity=r.equity, diff_bb=_bb(r.diff, r.bb, 2), method=r.method,
    )


def suckout(hand: HandRow, facts: Mapping[str, Facts], pid: str, ctx: Context) -> ReviewRow | None:
    """All in ahead, and lost."""
    r = ctx.allin.get((hand.hand_id, pid))
    if r is None or not (r.equity >= THRESHOLDS["suckout_equity"] and r.actual < 0):
        return None
    villains = _allin_villains(r)
    lost = _bb(-r.actual, r.bb, 0)
    why = (
        f"{r.equity:.0%} to win when it went in {'preflop' if r.street == PREFLOP else 'on the ' + r.street}: "
        f"{describe(r.hole_cards, r.board)} vs {_vs(ctx, villains, r.board)}"
        f"{f', lost {lost:.0f}bb' if lost is not None else ''}"
    )
    return _beat_row(hand, facts, pid, "suckout", why, r, villains)


def cooler_pre(hand: HandRow, facts: Mapping[str, Facts], pid: str, ctx: Context) -> ReviewRow | None:
    """A premium all in preflop and behind, and lost."""
    r = ctx.allin.get((hand.hand_id, pid))
    if r is None or r.street != PREFLOP or r.actual >= 0 or r.equity > THRESHOLDS["cooler_equity"]:
        return None
    if hand_class(r.hole_cards) not in THRESHOLDS["premium"]:
        return None
    villains = _allin_villains(r)
    lost = _bb(-r.actual, r.bb, 0)
    why = (
        f"{hand_class(r.hole_cards)} ran into {_vs(ctx, villains, ())} preflop: "
        f"{r.equity:.0%} equity{f', lost {lost:.0f}bb' if lost is not None else ''}"
    )
    return _beat_row(hand, facts, pid, "cooler_pre", why, r, villains)


def cooler_post(hand: HandRow, facts: Mapping[str, Facts], pid: str, ctx: Context) -> ReviewRow | None:
    """A stacks hand already behind a better one on a dry board, and lost.

    Judged on the earliest dry street (`dry_streets`) up to where the betting
    stopped. Without an all-in the pot must be bloated.
    """
    if not _showdown_known(hand, pid) or len(hand.board) < 5 or not hand.bb:
        return None
    me = hand.players[pid]
    if _net(me) >= 0:
        return None
    all_in = any(a.all_in for a in hand.actions)
    f = facts[pid]
    if not all_in and f.pot / hand.bb < THRESHOLDS["bloated_pot_bb"]:
        return None
    last = decision_street(hand)
    if last == PREFLOP:
        return None
    live = _live(hand)
    winners = best_of({p.pn_id: p.hole_cards for p in live}, hand.board)
    if pid in winners:
        return None
    for v in live:
        if v.pn_id == pid or v.pn_id not in winners:
            continue
        eff = _eff(hand, [pid, v.pn_id])
        if eff is None:
            ctx.stack_unknown.add((hand.hand_id, pid))
            continue
        t = tier(f.pot_level, eff / hand.bb)
        for street, seen in dry_streets(hand.board, last):
            if not meets_tier(me.hole_cards, seen, t):
                continue
            if best_of({pid: me.hole_cards, v.pn_id: v.hole_cards}, seen) != (v.pn_id,):
                continue  # not ahead yet: a later card did it, which is not a cooler
            r = ctx.allin.get((hand.hand_id, pid))
            villains = [Villain(v.pn_id, v.hole_cards, made_hand(v.hole_cards, seen))]
            where = f"all in on the {r.street}" if r else f"a {f.pot / hand.bb:.0f}bb pot"
            odds = f", {r.equity:.0%} to win" if r and r.street != RIVER else ""
            why = (
                f"{describe(me.hole_cards, seen)} into {_vs(ctx, villains, seen)} on the {street}, "
                f"{where}{odds}, lost {-_net(me) / hand.bb:.0f}bb"
            )
            return _row(
                hand, f, pid, "cooler_post", why, street, made_hand(me.hole_cards, seen), villains,
                equity=r.equity if r else None,
                diff_bb=_bb(r.diff, r.bb, 2) if r else None,
                method=r.method if r else None,
            )
    return None


MISTAKE_RULES = (missed_bluff, missed_value, failed_bluff)
#: Tried in order; a hand is at most one beat for a player.
BEAT_RULES = (suckout, cooler_pre, cooler_post)


def review_hand(hand: HandRow, facts: Mapping[str, Facts], pid: str, ctx: Context) -> list[ReviewRow]:
    """Every flag one hand earns for one player."""
    rows = [r for rule in MISTAKE_RULES if (r := rule(hand, facts, pid, ctx)) is not None]
    for rule in BEAT_RULES:
        r = rule(hand, facts, pid, ctx)
        if r is not None:
            rows.append(r)
            break
    return rows


# ------------------------------------------------------------------ marks ---
#
# A flag says a hand is worth a look; a mark says you have taken it. The two are
# opposites in every way that matters here: a flag is derived at read time from
# the cards and recomputed on every request, a mark is yours, entered by hand and
# stored. See schema.sql for why it is keyed on (game_id, hand_number) rather
# than on a hand_id that a rebuild moves.
#
# The mark is on the HAND, not on (hand, flag) or (hand, player). You reviewed a
# hand or you did not; a hand that earns two flags, or shows up on two players'
# reviews, is still the one replay you either watched or did not.


def reviewed_marks(conn: sqlite3.Connection, game_id: str | None = None) -> dict[tuple[str, int], str]:
    """(game_id, hand_number) -> when it was marked, for one game or all of them."""
    sql = "SELECT game_id, hand_number, reviewed_at FROM hand_reviews"
    args: tuple = ()
    if game_id is not None:
        sql += " WHERE game_id = ?"
        args = (game_id,)
    return {(r["game_id"], r["hand_number"]): r["reviewed_at"] for r in conn.execute(sql, args)}


def is_reviewed(conn: sqlite3.Connection, game_id: str, hand_number: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM hand_reviews WHERE game_id = ? AND hand_number = ?",
        (game_id, hand_number),
    ).fetchone() is not None


def mark_reviewed(
    conn: sqlite3.Connection, game_id: str, hand_number: int, reviewed: bool = True
) -> str | None:
    """Mark one hand reviewed, or clear the mark. Returns the timestamp, or None.

    Idempotent both ways: marking a marked hand keeps the original timestamp, so
    the mark says when you first looked, and clearing an unmarked one is a no-op.
    Raises ValueError when marking a hand the database does not have -- a mark is
    only useful next to the replay it points at. Clearing does not check, so a
    mark left over from a game since deleted can still be swept up.
    """
    if reviewed and conn.execute(
        "SELECT 1 FROM hands WHERE game_id = ? AND hand_number = ?", (game_id, hand_number)
    ).fetchone() is None:
        raise ValueError(f"no hand #{hand_number} in game {game_id}")
    with writing(conn):
        if not reviewed:
            conn.execute(
                "DELETE FROM hand_reviews WHERE game_id = ? AND hand_number = ?",
                (game_id, hand_number),
            )
            return None
        conn.execute(
            "INSERT INTO hand_reviews (game_id, hand_number, reviewed_at) VALUES (?, ?, ?)"
            " ON CONFLICT(game_id, hand_number) DO NOTHING",
            (game_id, hand_number, datetime.now(UTC).isoformat(timespec="seconds")),
        )
    return conn.execute(
        "SELECT reviewed_at FROM hand_reviews WHERE game_id = ? AND hand_number = ?",
        (game_id, hand_number),
    ).fetchone()[0]


# ------------------------------------------------------------------- read ---


def _villain_info(conn: sqlite3.Connection) -> Callable[[str], dict]:
    ident = identity_map(conn)
    memo: dict[str, dict] = {}

    def info(pn_id: str) -> dict:
        alias = ident.get(pn_id, (None, None))[1]
        if alias is None:
            return {}
        if alias not in memo:
            t = tags_for(facts_cached(conn, alias))
            p = t["profile"]
            memo[alias] = {
                "archetype": t["archetype"]["label"] if t["archetype"] else None,
                "wtsd_pct": p.get("wtsd", {}).get("pct"),
                "folds_river_pct": p.get("rules", {}).get("folds_river", {}).get("pct"),
            }
        return memo[alias]

    return info


def review_rows(
    conn: sqlite3.Connection, alias: str, game_id: str | None = None
) -> tuple[list[ReviewRow], set[tuple[int, str]], dict[int, dict[str, Facts]], dict[int, HandRow]]:
    """Every flag on one player's hands, newest first.

    Also returns the (hand, player) pairs a rule skipped for an unknown stack, and
    the facts and hands keyed by hand id, so a caller can filter rows on the spot
    DSL and count the population they came from. Raises ValueError on an unknown
    alias.
    """
    ids = set(identities_of(conn, alias))
    hands = load_hands(conn, game_id, ids) if ids else []
    facts_by_hand = {h.hand_id: {f.pn_id: f for f in derive(h)} for h in hands}
    allin_ids = [
        h.hand_id for h in hands
        if h.complete and h.went_to_showdown and any(a.all_in for a in h.actions)
    ]
    allin, _, _ = allin_rows(conn, game_id, allin_ids)
    ctx = Context(
        names=display_names(conn),
        allin={(r.hand_id, r.pn_id): r for r in allin if r.pn_id in ids},
        villain_info=_villain_info(conn),
    )

    rows: list[ReviewRow] = []
    for hand in hands:
        facts = facts_by_hand[hand.hand_id]
        for pid in ids & hand.players.keys():
            rows.extend(review_hand(hand, facts, pid, ctx))

    order = {h.hand_id: (h.ts or "", h.hand_id) for h in hands}
    rows.sort(key=lambda r: order[r.hand_id], reverse=True)
    return rows, ctx.stack_unknown, facts_by_hand, {h.hand_id: h for h in hands}


def _made_dict(mh: MadeHand | None, hole: str | None, board: Collection[str]) -> dict | None:
    if mh is None:
        return None
    return {"cls": mh.cls, "detail": mh.detail, "label": describe(hole, board)}


def review_hand_list(
    conn: sqlite3.Connection,
    alias: str,
    game_id: str | None = None,
    predicate: Callable[[Facts], bool] | None = None,
) -> dict:
    """One player's flagged hands, newest first, with the population they came from.

    Rows carry every field of `queries.hand_list` so the page renders them with
    the same code, plus the flag and whether the hand has been marked reviewed.
    Raises ValueError on an unknown alias.
    """
    rows, stack_unknown, facts_by_hand, hands = review_rows(conn, alias, game_id)
    ids = set(identities_of(conn, alias))
    names = display_names(conn)
    marks = reviewed_marks(conn, game_id)

    def keep(f: Facts) -> bool:
        return predicate is None or predicate(f)

    mine = [
        (hands[hid], f) for hid, fs in facts_by_hand.items() for pid, f in fs.items()
        if pid in ids and keep(f)
    ]
    examined = [(h, f) for h, f in mine if h.complete]
    showdowns = [(h, f) for h, f in examined if f.wtsd]
    known = [(h, f) for h, f in showdowns if all(p.hole_cards for p in _live(h))]
    skipped = {
        "cards_unknown": len(showdowns) - len(known),
        "stack_unknown": sum(1 for hid, pid in stack_unknown if keep(facts_by_hand[hid][pid])),
    }

    out = []
    counts = dict.fromkeys(KINDS, 0)
    for r in rows:
        f = facts_by_hand[r.hand_id][r.pn_id]
        if not keep(f):
            continue
        counts[r.kind] += 1
        hand = hands[r.hand_id]
        board = board_at(hand.board, r.street) or hand.board
        d = hand_list([f], names)[0]
        d.update(
            {
                "reviewed": (hand.game_id, hand.hand_number) in marks,
                "reviewed_at": marks.get((hand.game_id, hand.hand_number)),
                "kind": r.kind,
                "label": KIND_LABELS[r.kind],
                "group": KINDS[r.kind],
                "why": r.why,
                "street": r.street,
                "run_count": hand.run_count,
                "made": _made_dict(r.made, f.hole_cards, board),
                "villains": [
                    {
                        "player": names.get(v.pn_id, v.pn_id),
                        "cards": v.cards,
                        "made": _made_dict(v.made, v.cards, board),
                        "answer": v.answer,
                        "uncapped": v.uncapped,
                        "archetype": v.archetype,
                        "wtsd_pct": v.wtsd_pct,
                        "folds_river_pct": v.folds_river_pct,
                    }
                    for v in r.villains
                ],
                "pot_bb": r.pot_bb,
                "eff_bb": r.eff_bb,
                "spr": r.spr,
                "pot_level": f.pot_level,
                "bluff_kind": r.bluff_kind,
                "bluff_size": r.bluff_size,
                "bluff_pot": r.bluff_pot,
                "answer": r.answer,
                "equity": r.equity,
                "diff_bb": r.diff_bb,
                "method": r.method,
            }
        )
        out.append(d)

    return {
        "player": alias,
        "examined": len(examined),
        "showdowns": len(showdowns),
        "known_showdowns": len(known),
        "counts": counts,
        # Listed rows already marked. Counted over rows, not hands, so it can be
        # read straight against the flag counts beside it: one hand carrying two
        # flags is two rows on the page and two here.
        "reviewed": sum(1 for d in out if d["reviewed"]),
        "skipped": skipped,
        "thresholds": {k: list(v) if isinstance(v, tuple) else v for k, v in THRESHOLDS.items()},
        "hands": out,
    }
