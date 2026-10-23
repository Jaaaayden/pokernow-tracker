"""Event stream -> hands.

Two rules in here are the difference between a correct tracker and one that looks
correct:

**1. Bet amounts are cumulative per street.** ``bets N``, ``raises to N``,
``calls N`` *and the live forced posts* (``posts a straddle of N``, ``posts a
missed big blind of N``) all state the player's total commitment for that street,
not the chips they just pushed. Incremental =
``N - already_committed_this_street``.
Verified by reconstructing pot totals: hand #180 preflop is 60, not 75; hand #92
final pot is 480; hand #44's uncalled return is 605-50=555. Read ``calls N`` as
incremental and every pot, every net-won figure and every bb/100 is wrong while
still looking plausible. `tests/test_amounts.py` asserts this across every hand.

**2. The roster is the dealt-in roster.** It comes from the ``Player stacks:``
line, never from join/quit events and never from who happens to act. A player
sitting out is still at the table; getting this wrong is invisible, because every
rate is then quietly too low for exactly the players who sit out most.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from ..ingest.csv_source import RawEntry
from . import events as E
from .grammar import classify

#: Posts that go straight to the pot without counting as the player's bet for the
#: street. A dead small blind and an ante do not entitle you to call for less.
DEAD_POSTS = frozenset({E.POST_ANTE, E.POST_MISSING_SB})

#: Live posts that state a street TOTAL rather than fresh chips, exactly like
#: ``raises to N``. A straddle posted by the small blind is a raise to N with the
#: blind already inside it, and a returning player's missed big blind absorbs the
#: small blind they posted the same street. Adding either one on top of the blind
#: over-counts the pot by the blind -- silently, because the next cumulative
#: action re-derives from the street total and cancels the error. It only survives
#: when the player never acts again, which is why it hid in fold-around hands.
CUMULATIVE_POSTS = frozenset({E.POST_STRADDLE, E.POST_MISSED_BB})


@dataclass(slots=True)
class HandPlayer:
    pn_id: str
    name: str
    seat: int
    starting_stack: int
    seats_from_button: int | None = None
    hole_cards: tuple[str, ...] = ()
    committed: int = 0  # gross chips put in, including forced posts
    uncalled: int = 0  # returned to them when nobody called
    collected: int = 0
    #: Signed 7-2 side-bet result for this hand: positive when paid to this
    #: player, negative when paid out. Deliberately NOT folded into `collected`:
    #: this money never enters the pot, and the conservation law
    #: (total_contributed == total_collected) is the strongest correctness check
    #: this parser has. Mixing side-bet chips into it would break that check for
    #: every bounty hand and cost more than the stat is worth.
    bounty: int = 0
    #: Signed Nit Game penalty settled after this hand, kept apart from `bounty`
    #: for the same reason and so the 7-2 ledger stays the 7-2 ledger.
    nit: int = 0
    folded: bool = False

    @property
    def contributed(self) -> int:
        """Net chips this player actually risked into the pot."""
        return self.committed - self.uncalled

    @property
    def net(self) -> int:
        """Everything this player won or lost on the hand, side bets included."""
        return self.collected - self.contributed + self.bounty + self.nit


@dataclass(slots=True)
class ParsedAction:
    pn_id: str
    street: str
    seq: int
    kind: str  # fold | check | call | bet | raise | post
    amount: int  # INCREMENTAL chips committed by this action
    amount_to: int | None  # raw cumulative figure from the log, kept for audit
    is_forced: bool
    all_in: bool
    post_kind: str | None = None
    #: The `ord` of the log line this action came from (epoch ms * 100 + seq), so
    #: the action can be put back on the clock. See SPEC.md, "Timing tells".
    ord: int | None = None


@dataclass(slots=True)
class ParsedHand:
    game_id: str
    hand_number: int
    table_hand_id: str
    ts: str
    ord: int
    variant: str
    dealer_seat: int | None
    dead_button: bool
    n_dealt_in: int
    #: True when a blind position was dead, leaving a position slot no player
    #: occupies. Positions on these hands are best-effort -- exclude from splits.
    blinds_irregular: bool = False
    #: False when the log stops before ``-- ending hand #N --``. The export was
    #: taken mid-hand, so the chips are genuinely only half-recorded: contributed
    #: will not equal collected and no rate derived from it is trustworthy.
    #: Excluded from the conservation law rather than "fixed" -- the data is
    #: partial, not wrong, and a later re-import of the finished log repairs it.
    complete: bool = True
    bb: int | None = None  # from this hand's actual BB post, not the game's config
    players: dict[str, HandPlayer] = field(default_factory=dict)
    actions: list[ParsedAction] = field(default_factory=list)
    board_runs: list[list[str]] = field(default_factory=list)
    hero_cards: tuple[str, ...] = ()
    run_count: int = 1
    #: True when the table dealt Double Board: a ``(second board)`` line, not a
    #: run-it-twice ``(second run)``. Both land in `board_runs[1]`.
    double_board: bool = False
    #: Cards shown after ``-- ending hand #N --``, keyed by pn_id. Deliberately
    #: separate from `HandPlayer.hole_cards` -- see `_apply_voluntary_show`.
    voluntary_shows: dict[str, VoluntaryShow] = field(default_factory=dict)
    #: The `ord` of each betting street's board line, run one only: what the first
    #: decision on a street is timed from. See SPEC.md, "Timing tells".
    board_ords: dict[str, int] = field(default_factory=dict)
    #: The longest a decision could take when the hand began, in ms: the table's
    #: decision limit plus a full time bank, from its ``Game Config Changes`` lines.
    #: None when the table turned the clock off.
    clock_ms: int | None = None

    @property
    def went_to_showdown(self) -> bool:
        """Two or more players still live when the hand ended.

        Deliberately *not* inferred from ``shows`` lines: players voluntarily show
        cards after winning uncontested, and rabbit-hunt shows appear between hands.
        """
        return sum(1 for p in self.players.values() if not p.folded) >= 2

    @property
    def total_contributed(self) -> int:
        return sum(p.contributed for p in self.players.values())

    @property
    def total_collected(self) -> int:
        return sum(p.collected for p in self.players.values())

    def saw_street(self, street: str) -> bool:
        return len(self.board_runs) > 0 and len(self.board_runs[0]) >= {
            E.FLOP: 3,
            E.TURN: 4,
            E.RIVER: 5,
        }.get(street, 0)


@dataclass(slots=True)
class ParseResult:
    game_id: str
    hands: list[ParsedHand] = field(default_factory=list)
    misses: list[tuple[int, str, str]] = field(default_factory=list)  # (ord, entry, reason)
    blinds: dict[str, int] = field(default_factory=dict)  # latest sb/bb/ante seen
    #: Hands dropped for ending on another hand's end line: the log lost the lines
    #: between (see SPEC.md, "Money"). Each is the number the dropped hand started as.
    sewn: list[int] = field(default_factory=list)


def assign_positions(
    seats: list[int], dealer_seat: int | None, bb_seat: int | None
) -> tuple[dict[int, int], bool]:
    """Map seat number -> seats_from_button. Returns ``(positions, irregular)``.

    Seat numbers are physical table indices (1-10) and are frequently
    non-contiguous -- a real hand in these fixtures is seated ``#1 #2 #3 #10``. So
    positions are counted over *dealt-in players in seat order*, never over raw
    seat numbers.

    Anchoring, in order of preference:

    1. **The dealer** named in the hand-start line, reconciled against the big
       blind (below).
    2. **The big blind** alone, when the hand says ``(dead button)`` or the named
       dealer is not dealt in. The BB always posts, and sits at
       ``seats_from_button`` 1 heads-up, 2 otherwise.

    The reconciliation matters. When a player leaves, PokerNow can leave the small
    blind *dead* -- a position slot no dealt-in player occupies. Hand #25 of
    `pgl1UViJ4` is seated #1/#2/#10 with the button on seat 2 and a ``Dead Small
    Blind`` line, so seat 10 posts the *big* blind while sitting only one slot from
    the button. A plain 0..n-1 rotation cannot express that gap and mislabels every
    player after it. Detecting the offset from the BB and shifting is what keeps
    those hands honest; `irregular` is True whenever such a gap was found, so
    positional reports can exclude them.
    """
    seats = sorted(seats)
    n = len(seats)
    if n == 0:
        return {}, False
    expected_bb = 1 if n == 2 else 2

    if dealer_seat is not None and dealer_seat in seats:
        start = seats.index(dealer_seat)
        rotation = [seats[(start + k) % n] for k in range(n)]
        positions = {seat: k for k, seat in enumerate(rotation)}
        if bb_seat is not None and bb_seat in positions:
            offset = expected_bb - positions[bb_seat]
            if offset > 0:
                # A dead blind sits between the button and the big blind; everyone
                # from the button's left onward is one or more slots further round
                # than a naive rotation suggests.
                for seat in rotation[1:]:
                    positions[seat] += offset
                return positions, True
        return positions, False

    if bb_seat is not None and bb_seat in seats:
        start = seats.index(bb_seat)
        return (
            {seats[(start + k) % n]: (expected_bb + k) % n for k in range(n)},
            False,
        )

    return {}, False


# Standard position names by table size. Written out rather than computed because
# the convention is not arithmetic: 6-max is BTN/SB/BB/UTG/HJ/CO with no lojack,
# while 7-handed inserts LJ and keeps a single UTG.
_POSITION_TABLES: dict[int, tuple[str, ...]] = {
    2: ("BTN/SB", "BB"),
    3: ("BTN", "SB", "BB"),
    4: ("BTN", "SB", "BB", "CO"),
    5: ("BTN", "SB", "BB", "HJ", "CO"),
    6: ("BTN", "SB", "BB", "UTG", "HJ", "CO"),
    7: ("BTN", "SB", "BB", "UTG", "LJ", "HJ", "CO"),
    8: ("BTN", "SB", "BB", "UTG", "UTG+1", "LJ", "HJ", "CO"),
    9: ("BTN", "SB", "BB", "UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO"),
    10: ("BTN", "SB", "BB", "UTG", "UTG+1", "UTG+2", "UTG+3", "LJ", "HJ", "CO"),
}


def position_name(seats_from_button: int, n_dealt_in: int) -> str:
    """Label a position at *query* time.

    Never stored: seat 4 is UTG on one hand and the cutoff on the next once two
    players leave.
    """
    table = _POSITION_TABLES.get(max(2, min(n_dealt_in, 10)), _POSITION_TABLES[10])
    if 0 <= seats_from_button < len(table):
        return table[seats_from_button]
    # Only reachable on irregular-blind hands, where a dead slot pushes the last
    # player past the final seat. They act first preflop, so: UTG.
    return "UTG"


class _HandBuilder:
    """Accumulates one hand's worth of events."""

    def __init__(self, game_id: str, start: E.HandStart, ts: str):
        self.hand = ParsedHand(
            game_id=game_id,
            hand_number=start.hand_number,
            table_hand_id=start.table_hand_id,
            ts=ts,
            ord=start.ord,
            variant=start.variant,
            dealer_seat=None,
            dead_button=start.dead_button,
            n_dealt_in=0,
        )
        self._dealer_ref = start.dealer
        self.street = E.PREFLOP
        self._committed: dict[str, int] = {}  # this street only
        self._seq = 0
        self._bb_seat: int | None = None

    # -- event handlers ----------------------------------------------------
    def roster(self, ev: E.PlayerStacks) -> None:
        for seat, ref, stack in ev.seats:
            self.hand.players[ref.pn_id] = HandPlayer(
                pn_id=ref.pn_id, name=ref.name, seat=seat, starting_stack=stack
            )
            if self._dealer_ref is not None and ref.pn_id == self._dealer_ref.pn_id:
                self.hand.dealer_seat = seat
        self.hand.n_dealt_in = len(self.hand.players)

    def post(self, ev: E.Post) -> None:
        p = self.hand.players.get(ev.player.pn_id)
        if p is None:
            return
        if ev.kind == E.POST_BB:
            self._bb_seat = p.seat
            # Blind levels change mid-game, so the big blind is a property of the
            # hand, not of the game. Take the largest BB post in case someone also
            # posts a missed blind.
            self.hand.bb = max(self.hand.bb or 0, ev.amount)
        already = self._committed.get(p.pn_id, 0)
        if ev.kind in DEAD_POSTS:
            # Real chips into the pot, but they buy no part of the street's bet.
            incremental, amount_to = ev.amount, None
        elif ev.kind in CUMULATIVE_POSTS:
            incremental = max(0, ev.amount - already)
            self._committed[p.pn_id] = max(already, ev.amount)
            amount_to = ev.amount
        else:
            incremental, amount_to = ev.amount, None
            self._committed[p.pn_id] = already + ev.amount
        p.committed += incremental
        self._seq += 1
        self.hand.actions.append(
            ParsedAction(
                pn_id=p.pn_id,
                street=self.street,
                seq=self._seq,
                kind="post",
                amount=incremental,
                amount_to=amount_to,
                is_forced=True,
                all_in=ev.all_in,
                post_kind=ev.kind,
                ord=ev.ord,
            )
        )

    def action(self, ev: E.Action) -> None:
        p = self.hand.players.get(ev.player.pn_id)
        if p is None:
            return
        incremental = 0
        if ev.amount_to is not None:
            # THE RULE: amount_to is cumulative for this street.
            incremental = ev.amount_to - self._committed.get(p.pn_id, 0)
            self._committed[p.pn_id] = ev.amount_to
            p.committed += incremental
        if ev.kind == "fold":
            p.folded = True
        self._seq += 1
        self.hand.actions.append(
            ParsedAction(
                pn_id=p.pn_id,
                street=self.street,
                seq=self._seq,
                kind=ev.kind,
                amount=incremental,
                amount_to=ev.amount_to,
                is_forced=False,
                all_in=ev.all_in,
                ord=ev.ord,
            )
        )

    def street_dealt(self, ev: E.StreetDealt) -> None:
        while len(self.hand.board_runs) <= ev.run:
            self.hand.board_runs.append([])
        self.hand.board_runs[ev.run] = list(ev.board)
        self.hand.run_count = len(self.hand.board_runs)
        self.hand.double_board |= ev.double_board
        if ev.run == 0:
            # Only the first board advances the betting street. A run-it-twice second
            # run is dealt after all action is complete, and a Double Board second
            # board is logged straight after the first board's line for the same
            # street -- so in both cases resetting here again would be a no-op at
            # best and would wipe a live street's commitments at worst.
            self.street = ev.street
            self._committed.clear()
            self.hand.board_ords[ev.street] = ev.ord

    def uncalled(self, ev: E.UncalledReturn) -> None:
        p = self.hand.players.get(ev.player.pn_id)
        if p is not None:
            p.uncalled += ev.amount

    def collected(self, ev: E.Collected) -> None:
        p = self.hand.players.get(ev.player.pn_id)
        if p is not None:
            p.collected += ev.amount

    def shows(self, ev: E.Shows) -> None:
        p = self.hand.players.get(ev.player.pn_id)
        # Two cards in Hold'em, four or five in PLO; a single card is a partial show.
        if p is not None and len(ev.cards) >= 2:
            p.hole_cards = ev.cards

    def finish(
        self, *, complete: bool = True, table_bb: int | None = None, bomb_bbs: int | None = None
    ) -> ParsedHand:
        self.hand.complete = complete
        if self.hand.bb is None:
            self.hand.bb = bomb_pot_bb(self.hand, bomb_bbs, table_bb)
        positions, irregular = assign_positions(
            [p.seat for p in self.hand.players.values()],
            self.hand.dealer_seat,
            self._bb_seat,
        )
        self.hand.blinds_irregular = irregular
        for p in self.hand.players.values():
            p.seats_from_button = positions.get(p.seat)
        return self.hand


def _apply_bounty(hand: ParsedHand, ev: E.BountyPaid) -> None:
    """Book a side-bet transfer against a hand.

    Takes a `ParsedHand` rather than a builder because these lines arrive *after*
    ``-- ending hand #N --``: by then the hand is finished and the builder is gone.
    Players not dealt into the hand are ignored, the same way every other handler
    treats an unknown pn_id. Several winners split the amount; an odd chip goes to
    the first.
    """
    payer = hand.players.get(ev.payer.pn_id)
    if payer is not None:
        payer.bounty -= ev.amount
    winners = (ev.payee, *ev.others)
    share, odd = divmod(ev.amount, len(winners))
    for i, ref in enumerate(winners):
        payee = hand.players.get(ref.pn_id)
        if payee is not None:
            payee.bounty += share + (odd if i == 0 else 0)


def _apply_nit(hand: ParsedHand, ev: E.NitPenalty) -> None:
    """Book a Nit Game penalty against the hand whose winner cleared the last
    other Nit: the payer pays `amount` to each payee. Players not dealt into the
    hand are skipped, the payer included, as `_apply_bounty` skips them -- the
    money each player present moved is still theirs, and the Nit board's
    `balance` shows what the absent side would have been."""
    payer = hand.players.get(ev.payer.pn_id)
    if payer is not None:
        payer.nit -= ev.amount * len(ev.payees)
    for ref in ev.payees:
        p = hand.players.get(ref.pn_id)
        if p is not None:
            p.nit += ev.amount


@dataclass(slots=True)
class VoluntaryShow:
    """Cards one player showed after a hand ended."""

    pn_id: str
    cards: list[str]  # in the order shown; a partial show leaves a single card
    ord: int  # of the latest show line


def _apply_voluntary_show(hand: ParsedHand, ev: E.Shows) -> None:
    """Record cards shown after a hand ended, apart from its showdown cards.

    Never written to `hole_cards`. Showdown cards are the hands that got there;
    these are the ones a player *chose* to reveal -- the bluff they are proud of,
    the fold they want credit for -- and that bias runs the other way. Merged,
    every range view would carry it silently; kept apart, a view can opt in.

    PokerNow lets a player show one card and then the other as two lines, so shows
    accumulate per player rather than replacing each other.
    """
    if ev.player.pn_id not in hand.players:
        return
    show = hand.voluntary_shows.setdefault(
        ev.player.pn_id, VoluntaryShow(pn_id=ev.player.pn_id, cards=[], ord=ev.ord)
    )
    for card in ev.cards:
        if card not in show.cards:
            show.cards.append(card)
    show.ord = ev.ord


#: PokerNow's default decision clock: 20 s to act, plus a 10 s time bank.
DEFAULT_DECISION_S, DEFAULT_TIME_BANK_S = 20, 10
_CLOCK_SETTINGS = {
    "limit": re.compile(r"^\* Decision Limit Time: .*» (\S+)\s*$", re.MULTILINE),
    "bank": re.compile(r"^\* Max Time Bank: .*» (\S+)\s*$", re.MULTILINE),
}


BOMB_BBS = re.compile(r"^\* Bomb Pot BB Quantity: .*» (\S+)\s*$", re.MULTILINE)


def bomb_bbs_setting(raw: str) -> tuple[bool, int | None]:
    """A ``Game Config Changes`` line's `Bomb Pot BB Quantity`: (whether the line
    sets it, the new value). A value that is not a number (``off``) is None."""
    m = BOMB_BBS.search(raw)
    if m is None:
        return False, None
    return True, int(m.group(1)) if m.group(1).isdigit() and int(m.group(1)) else None


def bomb_pot_bb(hand: ParsedHand, bomb_bbs: int | None, table_bb: int | None) -> int | None:
    """A bomb pot's big blind, None on any other hand. It posts no big blind but
    is played at the table's current one, and every bb figure needs it: the bomb
    is `bomb_bbs` of them by the table's settings; failing that, `table_bb`, the
    last big blind seen."""
    bomb = max((a.amount for a in hand.actions if a.post_kind == E.POST_BOMB), default=0)
    if not bomb:
        return None
    if bomb_bbs and bomb % bomb_bbs == 0:
        return bomb // bomb_bbs
    return table_bb


def _apply_clock(clock: dict[str, int | None], raw: str) -> None:
    """Read a ``Game Config Changes`` line's clock settings into `clock`.

    The new value is the one after the ``»``. One that is not a number of seconds
    (a clock switched off) leaves the clock unknown, rather than guessed.
    """
    for key, rx in _CLOCK_SETTINGS.items():
        m = rx.search(raw)
        if m:
            value = m.group(1)
            clock[key] = int(value[:-1]) if value.endswith("s") and value[:-1].isdigit() else None


def parse(entries: Iterable[RawEntry], game_id: str) -> ParseResult:
    """Parse `order`-ascending raw entries into hands.

    Events occurring outside hand boundaries are handled at game level rather than
    assumed to belong to the hand in progress: blind-level changes update the game,
    while the 7-2 bounty and voluntary shows -- both logged after the hand-end
    line -- attach to the hand that just ended.
    """
    result = ParseResult(game_id=game_id)
    builder: _HandBuilder | None = None
    #: The most recently finished hand. The 7-2 bounty and voluntary shows arrive
    #: *between* hands, after the hand-end line, so they have no open builder.
    last: ParsedHand | None = None
    #: The table's decision clock as configured so far, in seconds.
    clock: dict[str, int | None] = {"limit": DEFAULT_DECISION_S, "bank": DEFAULT_TIME_BANK_S}
    #: The table's big blind as of now: the last hand's BB post, or a later
    #: chip-denominated blind change. What a hand with no BB post is played at.
    table_bb: int | None = None
    #: How many big blinds a bomb pot costs, from the table's settings.
    bomb_bbs: int | None = None

    for raw in entries:
        ev = classify(raw.entry, raw.ord)

        if isinstance(ev, E.Unknown):
            result.misses.append((raw.ord, raw.entry, ev.reason))
            continue

        if isinstance(ev, E.HandStart):
            if builder is not None:  # log truncated mid-hand; keep what we have
                last = builder.finish(complete=False, table_bb=table_bb, bomb_bbs=bomb_bbs)
                table_bb = last.bb or table_bb
                result.hands.append(last)
            builder = _HandBuilder(game_id, ev, raw.at)
            if clock["limit"] is not None:
                builder.hand.clock_ms = 1000 * (clock["limit"] + (clock["bank"] or 0))
            continue

        if isinstance(ev, E.Noise) and ev.kind == "config_change":
            # Takes effect from the next hand: a hand under way keeps its clock.
            _apply_clock(clock, ev.raw)
            sets, value = bomb_bbs_setting(ev.raw)
            if sets:
                bomb_bbs = value
            continue

        if isinstance(ev, E.BlindChange):
            result.blinds[ev.which] = ev.to_amount
            if ev.which == "bb" and isinstance(ev.to_amount, int):
                table_bb = ev.to_amount
            continue

        if isinstance(ev, E.BountyPaid):
            # Settled after the hand it belongs to, so `last` is the usual target;
            # `builder.hand` covers a layout that settles before the hand ends.
            target = builder.hand if builder is not None else last
            if target is not None:
                _apply_bounty(target, ev)
            continue

        if isinstance(ev, E.NitPenalty):
            target = builder.hand if builder is not None else last
            if target is not None:
                _apply_nit(target, ev)
            continue

        if isinstance(ev, E.Shows) and builder is None:
            # Shown after the hand-end line: a fold, an uncontested win, or a muck
            # revealed late. Shows inside the hand (showdown) go to `builder.shows`.
            if last is not None:
                _apply_voluntary_show(last, ev)
            continue

        if builder is None:
            if isinstance(ev, E.HandEnd):
                # An end with no hand open: the log lost this hand's start, and what
                # is settled after it is this hand's, not the last stored one's.
                last = None
            # Pre-game config, or between-hand chatter. Nothing to attach it to.
            continue

        if isinstance(ev, E.HandEnd) and ev.hand_number != builder.hand.hand_number:
            # Another hand's end: the lines between are missing, so this is one hand's
            # start sewn to a later one's finish. Neither hand is on record; drop it,
            # and whatever is settled after this end line with it. Its largest big
            # blind post is still the best word on the table's blind.
            result.sewn.append(builder.hand.hand_number)
            table_bb = builder.hand.bb or table_bb
            builder = None
            last = None
        elif isinstance(ev, E.HandEnd):
            last = builder.finish(table_bb=table_bb, bomb_bbs=bomb_bbs)
            table_bb = last.bb or table_bb
            result.hands.append(last)
            builder = None
        elif isinstance(ev, E.PlayerStacks):
            builder.roster(ev)
        elif isinstance(ev, E.HeroCards):
            builder.hand.hero_cards = ev.cards
        elif isinstance(ev, E.Post):
            builder.post(ev)
        elif isinstance(ev, E.Action):
            builder.action(ev)
        elif isinstance(ev, E.StreetDealt):
            builder.street_dealt(ev)
        elif isinstance(ev, E.UncalledReturn):
            builder.uncalled(ev)
        elif isinstance(ev, E.Collected):
            builder.collected(ev)
        elif isinstance(ev, E.Shows):
            builder.shows(ev)
        # SeatChange / AdminStackChange / Noise: recognized, no effect on hand math.

    if builder is not None:  # the export was taken while this hand was still live
        result.hands.append(builder.finish(complete=False, table_bb=table_bb, bomb_bbs=bomb_bbs))

    return result


def iter_hands(entries: Iterable[RawEntry], game_id: str) -> Iterator[ParsedHand]:
    yield from parse(entries, game_id).hands
