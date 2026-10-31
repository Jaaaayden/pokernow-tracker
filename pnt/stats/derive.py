"""Per-hand stat derivation. Mirrors SPEC.md function-for-function.

Everything here runs at **read time** over raw actions. Nothing is precomputed and
no counter is stored, so changing a definition in SPEC.md means editing this file
and re-running a query -- not migrating a database and not losing history.

The single organizing idea is that a stat is a pair of booleans per
(hand, player): *did they have the opportunity* and *did they do it*. Rates are
then just `sum(did) / sum(opportunity)`, and every awkward edge case reduces to a
question about when the opportunity flag gets set.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from fractions import Fraction

from ..logfmt.events import (
    FLOP,
    POST_ANTE,
    POST_BOMB,
    POST_MISSING_SB,
    POST_STRADDLE,
    PREFLOP,
    RIVER,
    TURN,
)
from ..logfmt.parser import position_name, position_slots
from .cards import board_at

POSTFLOP_STREETS = (FLOP, TURN, RIVER)
PREV_STREET = {FLOP: PREFLOP, TURN: FLOP, RIVER: TURN}

def pct(num: int, den: int) -> float | None:
    """`num` as a percent of `den` to one decimal; None on an empty denominator.
    Every rate and share the stats print goes through this one rounding."""
    return round(100.0 * num / den, 1) if den else None


VOLUNTARY_COMMIT = {"call", "bet", "raise"}
AGGRESSIVE = {"bet", "raise"}

#: The seats an open from first in is a steal from. Heads-up the button is the
#: small blind, and its open is a steal too. See SPEC.md, "Steals and blind defense".
STEAL_SEATS = frozenset({"CO", "BTN", "SB", "BTN/SB"})
#: The seats that defend against a steal.
BLIND_SEATS = frozenset({"SB", "BB"})
#: Forced posts that are not a live blind: they go to the pot without being the
#: player's bet, so the player who made one still comes in cold.
DEAD_POST_KINDS = frozenset({POST_ANTE, POST_MISSING_SB})

#: Bet-size buckets, smallest first: under a fifth of the pot, a fifth to half,
#: half to three-quarters, three-quarters to pot, over pot. See SPEC.md, "Bet
#: size buckets".
SIZE_BUCKETS = ("blocker", "small", "medium", "large", "overbet")


def size_bucket(amount: int, pot: int) -> str:
    """The size bucket of a bet of `amount` chips into a pot of `pot` chips.

    PokerNow's 1/2, 3/4 and pot buttons each *start* a bucket, and they are most
    of the bets in real logs. Bets are whole chips, so a 3/4 click into a pot of
    30 comes out as 22 or 23: a bet reaches an edge when it is at least the edge
    rounded down to a chip. Overbet is strictly more than the pot.

    A blocker is under a fifth of the pot, with the edge rounded down the same
    way: `pot // 5` in integers, never `0.2 * pot`, whose float can land a chip
    either side of the same edge.
    """
    if amount > pot:
        return "overbet"
    if amount >= math.floor(0.75 * pot):
        return "large"
    if amount >= math.floor(0.5 * pot):
        return "medium"
    if amount >= pot // 5:
        return "small"
    return "blocker"


#: The letter each voluntary postflop action writes into a `line`.
LINE_CODE = {"check": "x", "bet": "b", "call": "c", "raise": "r", "fold": "f"}

#: What a player can do facing a bet, and what a bet can get back. Exclusive.
ANSWERS = ("fold", "call", "raise")


@dataclass(slots=True)
class HandAction:
    seq: int
    street: str
    pn_id: str
    kind: str
    amount: int
    is_forced: bool
    all_in: bool
    #: The raw cumulative "raises to N" figure. Sizing facts read this, because
    #: "opened to 4bb" is about the total, not the increment over the blind.
    amount_to: int | None = None
    #: What a forced post was: sb, bb, ante, missed_bb, missing_sb or straddle.
    #: None for a voluntary action, and for a post whose kind is not known.
    post_kind: str | None = None
    #: The `ord` of the log line it came from (epoch ms * 100 + seq). None on rows
    #: imported before `actions.ord` existed: untimed, never zero seconds.
    ord: int | None = None


@dataclass(slots=True)
class HandPlayerRow:
    pn_id: str
    seat: int
    seats_from_button: int | None
    contributed: int
    collected: int
    folded: bool
    hole_cards: str | None = None
    #: Signed 7-2 side-bet result. Part of `net`, but never of the pot.
    bounty: int = 0
    #: Signed Nit Game penalty. Part of `net`, but never of the pot.
    nit: int = 0
    #: Chips in front of them when the hand started, from the `Player stacks:`
    #: line. None on rows imported before the column was filled: unknown, not 0.
    starting_stack: int | None = None


@dataclass(slots=True)
class HandRow:
    hand_id: int
    game_id: str
    hand_number: int
    n_dealt_in: int
    dead_button: bool
    blinds_irregular: bool
    went_to_showdown: bool
    saw_flop: bool
    #: False when the log stops mid-hand. The actions up to that point are real; the
    #: chips are not all there, so no money figure may be drawn from this hand.
    complete: bool
    bb: int | None
    ts: str | None
    players: dict[str, HandPlayerRow]
    actions: list[HandAction]
    #: First run of the board. Run-it-twice hands classify on run one only.
    board: tuple[str, ...] = ()
    #: How many boards were dealt: 2 when the players ran it twice.
    run_count: int = 1
    #: The `ord` of each street's run-one board line, by street; empty on hands
    #: imported before it was stored. See SPEC.md, "Timing tells".
    board_ords: dict[str, int] = field(default_factory=dict)
    #: Decision limit plus a full time bank, in ms, when the hand began. None when
    #: unknown or the clock was off.
    clock_ms: int | None = None
    #: The game as the hand-start line names it. None on hands imported before it
    #: was stored, which were all Hold'em. See `is_holdem`.
    variant: str | None = None
    #: Double Board: two boards dealt on every street, not a run-it-twice.
    double_board: bool = False
    #: Double Board's second board, as dealt; empty on every other hand.
    board2: tuple[str, ...] = ()


def is_holdem(hand: HandRow) -> bool:
    """Whether the hand's play can be read: every rate, range, node and review
    rule here assumes two hole cards. Any other game (PLO) counts for money only,
    except in the hand lists (`derive(every_game=True)`)."""
    return hand.variant is None or "hold" in hand.variant.lower()


def is_analysed(hand: HandRow) -> bool:
    """Whether the hand's play is read at all: Hold'em on one board. PLO's cards
    and a Double Board's second board are past what any rate, range or review
    rule here understands, so those hands count for money and are listed, no more."""
    return is_holdem(hand) and not hand.double_board


#: `is_analysed` in SQL, over `hands` aliased `h`, for readers that skip `derive`.
ANALYSED_SQL = "((h.variant IS NULL OR h.variant LIKE '%hold%') AND NOT h.double_board)"


def is_bomb_pot(hand: HandRow) -> bool:
    """Everyone bought in with a bomb-pot bet: no blinds, no preflop decision."""
    return any(a.is_forced and a.post_kind == POST_BOMB for a in hand.actions)


@dataclass(slots=True)
class Facts:
    """One row per (hand, player). Booleans throughout; aggregation sums them."""

    hand_id: int
    pn_id: str
    n_dealt_in: int
    seats_from_button: int | None
    dead_button: bool
    blinds_irregular: bool = False
    #: The hand's position slots, a dead small blind's included: what
    #: `position_name` sizes the table by. 0 reads as `n_dealt_in`.
    position_slots: int = 0
    game_id: str = ""
    hand_number: int = 0
    ts: str | None = None

    vpip_opp: bool = False
    vpip: bool = False
    pfr_opp: bool = False
    pfr: bool = False
    three_bet_opp: bool = False
    three_bet: bool = False
    fold_to_3bet_opp: bool = False
    fold_to_3bet: bool = False

    cbet_opp: dict[str, bool] = field(default_factory=dict)
    cbet: dict[str, bool] = field(default_factory=dict)
    #: Facing a c-bet before anyone raises it. Also the opportunity for `raise_cbet`.
    fold_to_cbet_opp: dict[str, bool] = field(default_factory=dict)
    fold_to_cbet: dict[str, bool] = field(default_factory=dict)
    raise_cbet: dict[str, bool] = field(default_factory=dict)
    #: Leading into the previous street's aggressor before they act.
    donk_opp: dict[str, bool] = field(default_factory=dict)
    donk: dict[str, bool] = field(default_factory=dict)
    #: Who c-bet at them, on each street where they faced one. Keys match
    #: `fold_to_cbet_opp` exactly -- this is the *who* behind that opportunity.
    faced_cbet_by: dict[str, str] = field(default_factory=dict)
    #: The previous street's aggressor they had the chance to lead into. Keys
    #: match `donk_opp`.
    donk_into: dict[str, str] = field(default_factory=dict)
    #: Acted while a bet or raise had already been made on the street -- c-bet or
    #: not, and including the bettor facing a raise over their own bet. The three
    #: below say what they did there. See SPEC.md, "Facing a bet".
    faced_bet: dict[str, bool] = field(default_factory=dict)
    folded_to_bet: dict[str, bool] = field(default_factory=dict)
    called_bet: dict[str, bool] = field(default_factory=dict)
    raised_bet: dict[str, bool] = field(default_factory=dict)
    #: Made the street's last bet or raise -- the aggressor SPEC.md defines. The
    #: preflop equivalent is `pfa`.
    aggressor: dict[str, bool] = field(default_factory=dict)
    #: Their check closed a street that checked through, with at least two players
    #: acting on it. The player who declined to bet when nobody else did either.
    check_back: dict[str, bool] = field(default_factory=dict)
    #: At their *first* decision facing a bet on the street -- same keys as
    #: `faced_bet` -- the size of the latest bet or raise in front of them, and
    #: what they did there, exactly one of ANSWERS. See SPEC.md, "Facing a bet".
    #: That latest bet or raise, measured as its maker's `bet_pot` or `raise_pot`.
    faced_bet_pot: dict[str, float] = field(default_factory=dict)
    #: The SIZE_BUCKETS bucket of that same bet or raise.
    faced_bet_size: dict[str, str] = field(default_factory=dict)
    faced_bet_action: dict[str, str] = field(default_factory=dict)

    aggressive: dict[str, int] = field(default_factory=dict)
    agg_denom: dict[str, int] = field(default_factory=dict)

    saw_flop: bool = False
    wtsd_opp: bool = False
    wtsd: bool = False
    wsd: bool = False

    net: int = 0
    #: The final pot in chips: every chip that stayed in the middle, uncalled bets
    #: and bounties excluded. Same value for everyone in the hand; short on an
    #: incomplete hand, like `net`.
    pot: int = 0
    bb_size: int | None = None
    #: Carried from the hand so aggregation can drop this row from bb/100 without
    #: dropping it from everything else -- folding, betting and showing down all
    #: happened, and only the ledger is short.
    complete: bool = True

    # --- line and sizing facts (SPEC.md, "Lines and sizing") -----------------
    #: Made the level-2 raise.
    opener: bool = False
    #: Was the last preflop aggressor -- the player who "owns" the flop c-bet.
    pfa: bool = False
    #: Highest preflop bet level the hand reached: 1 limped/walk, 2 single-raised,
    #: 3 three-bet pot, 4+ four-bet pot. Same value for everyone in the hand.
    #: 0 when there was no preflop betting to read: a bomb pot, or PLO's money-only facts.
    pot_level: int = 1
    #: Size of the hand's open raise in big blinds, for everyone in the hand.
    open_bb: float | None = None
    #: This player's own last preflop raise-to, in big blinds.
    pf_raise_bb: float | None = None
    #: What this player did at each preflop bet level they acted at: level ->
    #: fold | check | call | raise. Level 1 is an unopened pot, 2 is facing the
    #: open, 3 facing a 3-bet, and so on. A player acts at most once per level,
    #: so this is the whole preflop decision path. See SPEC.md, "Decision points".
    pf_faced: dict[int, str] = field(default_factory=dict)
    #: This player's first `bet` on each postflop street as a fraction of the pot
    #: it was made into. 1.0 is a pot-sized bet; above it is an overbet.
    bet_pot: dict[str, float] = field(default_factory=dict)
    #: The SIZE_BUCKETS bucket of that same first bet.
    bet_size: dict[str, str] = field(default_factory=dict)
    #: The bucket of the c-bet this player faced, on streets where they faced one.
    faced_cbet_size: dict[str, str] = field(default_factory=dict)
    #: This player's first `raise` on each postflop street: the chips it added
    #: beyond a call, over the pot once they had called -- so a pot-sized raise is
    #: 1.0. See SPEC.md, "Raise sizes".
    raise_pot: dict[str, float] = field(default_factory=dict)
    #: The SIZE_BUCKETS bucket of that same raise.
    raise_size: dict[str, str] = field(default_factory=dict)
    #: What this player's first `bet` on the street got back: `raise` if anyone
    #: raised over it, else `call` if anyone called, else `fold`. No key when
    #: nobody answered it. See SPEC.md, "What a bet got".
    bet_answer: dict[str, str] = field(default_factory=dict)
    #: Their own voluntary actions on each postflop street, in order, one letter
    #: each from LINE_CODE: `xc` is check-call, `bf` bet then folded to a raise.
    #: No key for a street they did not act on. See SPEC.md, "Action lines".
    line: dict[str, str] = field(default_factory=dict)
    #: Checked, then raised a bet behind them: the line starts `xr`.
    check_raise: dict[str, bool] = field(default_factory=dict)
    #: Checked, then had to act again: the line starts `x` and goes on. The
    #: opportunity for `check_raise`, and the hands `xc` and `xf` come from too.
    check_raise_opp: dict[str, bool] = field(default_factory=dict)
    #: The street of their voluntary fold, preflop included; None if they never folded.
    fold_street: str | None = None
    #: Every voluntary action on each postflop street, everyone's, in order and
    #: from this player's seat: their own in capitals (`X B C R F`), the others'
    #: in lower case. `xBrF` is: checked to, bet, check-raised, folded. A key for
    #: every street they were still in when it began, even with nothing of their
    #: own on it. See SPEC.md, "The whole street".
    seq: dict[str, str] = field(default_factory=dict)
    #: They made the street's bet, and a player who had checked to them raised.
    faced_check_raise: dict[str, bool] = field(default_factory=dict)
    #: What they did next: one of ANSWERS. No key when they never acted again.
    faced_check_raise_action: dict[str, str] = field(default_factory=dict)
    #: Chips in the middle when each postflop street was dealt, for the streets
    #: that were: forced posts included, capped at `pot`. Same for everyone.
    pot_at: dict[str, int] = field(default_factory=dict)
    #: Went all-in with a bet or raise, per street, preflop included. An all-in
    #: call is not a jam. See SPEC.md, "Jams".
    jam: dict[str, bool] = field(default_factory=dict)
    #: Acted while the street's latest bet or raise was someone else's jam.
    faced_jam: dict[str, bool] = field(default_factory=dict)
    #: Called that jam.
    called_jam: dict[str, bool] = field(default_factory=dict)

    # --- who they were up against, and whether they closed the action --------
    #: The other players still in at this player's last action, in postflop
    #: acting order. See SPEC.md, "Who the action was against".
    opponents: tuple[str, ...] = ()
    #: This player's place in postflop acting order among `opponents` plus
    #: themselves; 0 acts first. None when the order cannot be established.
    pos_order: int | None = None
    #: How many players that order covers, this player included.
    pos_players: int | None = None
    #: True when they act last of them -- in position. Always
    #: `pos_order == pos_players - 1`; stored so filters and tests read one flag.
    in_position: bool | None = None

    #: Two Hold'em hole cards, or None: unknown, or not Hold'em. Every card reader
    #: here (hand classes, ranges, strength) knows two, so PLO's never reach it.
    hole_cards: str | None = None
    #: The cards to *show*, any game: what the hand lists print. Nothing reads them.
    cards: str | None = None
    board: tuple[str, ...] = ()

    # --- foundation facts (SPEC.md: "Steals and blind defense", "Squeeze and
    # cold-call", "Delayed c-bet, probe and float", "Stack depth", "Straddles",
    # "Won when saw flop"). Hand-level ones carry the same value for everyone.
    #: Someone posted a straddle. Same for everyone in the hand.
    straddled: bool = False
    #: Players who did not fold preflop, on a hand that dealt a flop; None when
    #: no flop was dealt. Same for everyone in the hand.
    players_flop: int | None = None
    #: At their first voluntary preflop action, every earlier one was a fold.
    first_in: bool = False
    #: The hand's open was a steal: made first in from a steal seat, on a hand
    #: whose seat labels can be trusted. Same for everyone in the hand.
    steal_open: bool = False
    #: Calls of the open made before this player's first decision facing it. None
    #: when they never faced an open.
    callers_before: int | None = None
    #: Posted a live blind: small blind, big blind, straddle, or a returning
    #: player's missed big blind -- chips in front of them before they acted.
    #: Antes and dead small blinds are not live.
    posted_blind: bool = False
    #: Every chip of their own forced posts, in chips like `net`: blinds, straddle,
    #: missed blinds and antes, dead posts included -- what folding every hand
    #: would have cost them. See SPEC.md, "bb/100 against folding".
    posted: int = 0
    #: Their starting stack against the deepest other stack dealt in, in big
    #: blinds: min(own, max(others)) / bb. None when any of those is unknown.
    eff_bb: float | None = None
    #: The stack-to-pot ratio on the flop: the effective stack among the flop
    #: players, less this player's preflop chips, over `pot_at["flop"]`. None
    #: when they did not see the flop or a stack is unknown.
    spr_flop: float | None = None
    #: Collected chips from the pot. A 7-2 bounty is not the pot.
    won_pot: bool = False
    #: The signed 7-2 side-bet result, from `HandPlayerRow.bounty`: positive when
    #: paid to them, negative when they paid, 0 when none. Already part of `net`.
    bounty: int = 0
    #: The signed Nit Game penalty, from `HandPlayerRow.nit`. Already part of `net`.
    nit: int = 0
    #: Hold'em, by the hand-start line: the `holdem` / `plo` filters.
    holdem: bool = True
    #: Hold'em on one board (`is_analysed`). False on PLO and Double Board, whose
    #: facts carry money only unless derived with `every_game` (see `derive`).
    analysed: bool = True
    #: A bomb pot: everyone posted the bomb, nobody had a preflop decision.
    bomb_pot: bool = False
    #: Double Board was dealt (`HandRow.double_board`), and its second board.
    double_board: bool = False
    board2: tuple[str, ...] = ()
    #: As the preflop aggressor, after every earlier postflop street checked
    #: through: first to bet on this street (turn and river).
    delayed_cbet_opp: dict[str, bool] = field(default_factory=dict)
    delayed_cbet: dict[str, bool] = field(default_factory=dict)
    #: Not the preflop aggressor, after the aggressor's check closed the previous
    #: street: acted with no bet yet, before the aggressor (turn and river).
    probe_opp: dict[str, bool] = field(default_factory=dict)
    probe: dict[str, bool] = field(default_factory=dict)
    #: The preflop aggressor facing that probe bet, before anyone raised it.
    fold_to_probe_opp: dict[str, bool] = field(default_factory=dict)
    fold_to_probe: dict[str, bool] = field(default_factory=dict)
    #: Called on the previous street (preflop: called a raise), and the previous
    #: street's aggressor checked to them: acted with no bet yet. `float_bet` is
    #: the bet, named so it does not shadow the builtin.
    float_opp: dict[str, bool] = field(default_factory=dict)
    float_bet: dict[str, bool] = field(default_factory=dict)

    # --- session dynamics (SPEC.md, "Session dynamics"). Context across hands,
    # which `derive` cannot see: `tilt.annotate` fills these over one player's
    # hands in one game, wherever a player's hands are listed. A row nobody
    # annotated keeps the defaults, and no session term matches it.
    #: Hands since their last hand in this game that lost `tilt.BIG_LOSS_BB` or
    #: more: 1 is the hand straight after it. None before any such loss.
    hands_since_loss: int | None = None
    #: The size of that loss in big blinds, positive. None with the above.
    loss_bb: float | None = None
    #: Their own hand count in this game, from 1. 0 on a row never annotated.
    session_hand: int = 0
    #: Hours since their first timestamped hand in this game. None when this
    #: hand has no timestamp.
    session_hour: float | None = None

    # --- timing tells (SPEC.md, "Timing tells"). Their first voluntary decision
    # on each street, preflop included. No key where it cannot be timed: no
    # decision there, or a hand imported before `actions.ord` was stored, which
    # is untimed and never zero seconds.
    #: Milliseconds from the previous post, action or board line in the hand.
    think_ms: dict[str, int] = field(default_factory=dict)
    #: That decision's bucket, one of TEMPO_BUCKETS.
    tempo: dict[str, str] = field(default_factory=dict)

    # --- read from the facts above, never stored -----------------------------
    @property
    def position_label(self) -> str | None:
        """Their seat's name, or None where it cannot be trusted: no seat, a dead
        button or a dead blind. The rule `positional_report` follows."""
        if self.seats_from_button is None or self.dead_button or self.blinds_irregular:
            return None
        return position_name(self.seats_from_button, self.n_dealt_in, self.position_slots)

    @property
    def posted_bb(self) -> Fraction | None:
        """`posted` in this hand's big blinds, exact; None when the blind is unknown."""
        return Fraction(self.posted, self.bb_size) if self.bb_size else None

    @property
    def steal_opp(self) -> bool:
        return self.first_in and self.position_label in STEAL_SEATS

    @property
    def steal(self) -> bool:
        return self.steal_opp and self.opener

    @property
    def fold_to_steal_opp(self) -> bool:
        return (
            self.steal_open
            and self.callers_before == 0
            and self.position_label in BLIND_SEATS
        )

    @property
    def fold_to_steal(self) -> bool:
        return self.fold_to_steal_opp and self.pf_faced.get(2) == "fold"

    @property
    def three_bet_vs_steal(self) -> bool:
        return self.fold_to_steal_opp and self.pf_faced.get(2) == "raise"

    @property
    def squeeze_opp(self) -> bool:
        return (self.callers_before or 0) >= 1

    @property
    def squeeze(self) -> bool:
        return self.squeeze_opp and self.pf_faced.get(2) == "raise"

    @property
    def cold_call_opp(self) -> bool:
        return 2 in self.pf_faced and 1 not in self.pf_faced and not self.posted_blind

    @property
    def cold_call(self) -> bool:
        return self.cold_call_opp and self.pf_faced.get(2) == "call"

    @property
    def limp(self) -> bool:
        return self.pf_faced.get(1) == "call"

    @property
    def limp_raise(self) -> bool:
        return self.limp and self.pf_faced.get(2) == "raise"

    @property
    def limp_call(self) -> bool:
        return self.limp and self.pf_faced.get(2) == "call"

    @property
    def limp_fold(self) -> bool:
        return self.limp and self.pf_faced.get(2) == "fold"

    @property
    def jam_pf(self) -> bool:
        return bool(self.jam.get(PREFLOP))

    @property
    def jam_pf_opp(self) -> bool:
        return self.vpip_opp

    @property
    def wwsf(self) -> bool:
        return self.saw_flop and self.won_pot

    @property
    def wwsf_nsd(self) -> bool:
        return self.wwsf and not self.wtsd


def _preflop(hand: HandRow, facts: dict[str, Facts]) -> str | None:
    """Walk preflop voluntary actions, tracking bet level. Returns the last aggressor."""
    level = 1  # the blinds
    aggressor: str | None = None
    opener: str | None = None  # who made it level 2
    open_bb: float | None = None
    folds_only = True  # every voluntary preflop action so far was a fold
    open_calls = 0  # calls made at level 2 so far

    for a in hand.actions:
        if a.street != PREFLOP or a.is_forced:
            continue
        f = facts.get(a.pn_id)
        if f is None:
            continue

        # --- opportunity is recorded BEFORE the action resolves.
        # Rule 3 of SPEC.md: acting *is* the opportunity.
        if not f.vpip_opp:
            f.first_in = folds_only
        f.vpip_opp = True
        f.pfr_opp = True
        if level == 2 and a.pn_id != aggressor:
            f.three_bet_opp = True
        if level == 3 and a.pn_id == opener:
            f.fold_to_3bet_opp = True
        if level == 2 and 2 not in f.pf_faced:
            f.callers_before = open_calls
        # A preflop `bet` (no blind in front of it) takes the level exactly as a
        # raise does, so it is recorded as one.
        f.pf_faced.setdefault(level, "raise" if a.kind == "bet" else a.kind)
        if a.kind != "fold":
            folds_only = False
        if level == 2 and a.kind == "call":
            open_calls += 1

        # --- then apply it
        if a.kind == "fold":
            if level == 3 and a.pn_id == opener:
                f.fold_to_3bet = True
        elif a.kind in VOLUNTARY_COMMIT:
            f.vpip = True
            if a.kind == "raise" or a.kind == "bet":
                f.pfr = True
                level += 1
                if hand.bb and a.amount_to is not None:
                    f.pf_raise_bb = round(a.amount_to / hand.bb, 2)
                if level == 2:
                    opener = a.pn_id
                    f.opener = True
                    open_bb = f.pf_raise_bb
                elif level == 3:
                    f.three_bet = True
                aggressor = a.pn_id

    # A steal is an open from first in, from a steal seat, on a hand whose seat
    # labels can be trusted -- `position_label` is None on the rest.
    o = facts.get(opener) if opener is not None else None
    steal_open = o is not None and o.first_in and o.position_label in STEAL_SEATS
    for f in facts.values():
        f.pot_level = 0 if f.bomb_pot or not f.analysed else level
        f.open_bb = open_bb
        f.steal_open = steal_open
    if aggressor is not None and aggressor in facts:
        facts[aggressor].pfa = True
    return aggressor


def _postflop(hand: HandRow, facts: dict[str, Facts], preflop_aggressor: str | None) -> None:
    prev_aggressor = preflop_aggressor
    # The preflop aggressor stays the delayed c-better and the one probed into on
    # every street; `prev_aggressor` moves. A street that checked through records
    # who closed it, and `unbet` stays True while every postflop street so far was
    # dealt and checked through. See SPEC.md, "Delayed c-bet, probe and float".
    pfa = preflop_aggressor
    closed_by: dict[str, str] = {}
    unbet = True

    # Pot size *before* each action, forced posts included, so a bet can be
    # expressed as a fraction of what it was made into.
    pot_before: dict[int, int] = {}
    # When each player first went all-in: nobody can lead into a player who has
    # no chips left to act with.
    all_in_at: dict[str, int] = {}
    running = 0
    for a in hand.actions:
        pot_before[a.seq] = running
        running += a.amount
        if a.all_in:
            all_in_at.setdefault(a.pn_id, a.seq)

    for street in POSTFLOP_STREETS:
        street_actions = [a for a in hand.actions if a.street == street and not a.is_forced]
        if not street_actions:
            prev_aggressor = None
            unbet = False
            continue

        prev = PREV_STREET[street]
        delayed_on = street != FLOP and unbet and pfa is not None
        probe_on = street != FLOP and pfa is not None and closed_by.get(prev) == pfa
        probe_by: str | None = None
        probe_raised = False

        bet_made = False
        cbet_by: str | None = None
        cbet_size: str | None = None
        cbet_raised = False
        street_aggressor: str | None = None
        acted: set[str] = set()
        # The street's betting as it stands: each player's chips in on this
        # street, the most anyone has in, and the size of the latest bet or raise
        # as (pot fraction, bucket).
        put: dict[str, int] = {}
        top = 0
        latest: tuple[float, str] | None = None
        # Each actor's line so far, and who made the street's one bet: what a
        # check-raise is read from.
        lines: dict[str, str] = {}
        bettor: str | None = None

        for i, a in enumerate(street_actions):
            # --- shared street state first, for every actor: a player missing
            # from `facts` still moves the price for everyone else.
            faced = latest
            pot = pot_before[a.seq]
            to_call = max(0, top - put.get(a.pn_id, 0))
            sized: tuple[float, str] | None = None
            if a.kind == "bet" and pot > 0:
                sized = (round(a.amount / pot, 3), size_bucket(a.amount, pot))
            elif a.kind == "raise" and pot + to_call > 0:
                # What the raise adds beyond a call, over the pot once called: the
                # convention PokerNow's raise buttons follow. See SPEC.md, "Raise sizes".
                extra = a.amount - to_call
                sized = (round(extra / (pot + to_call), 3), size_bucket(extra, pot + to_call))
            if a.kind in AGGRESSIVE:
                latest = sized
            put[a.pn_id] = put.get(a.pn_id, 0) + a.amount
            top = max(top, put[a.pn_id])

            # A check-raise against the street's bettor, and the bettor's answer:
            # their next action. The check-raiser checked in front of them, so it
            # always comes before the bettor acts again.
            prior = lines.get(a.pn_id, "")
            lines[a.pn_id] = prior + LINE_CODE.get(a.kind, "")
            if a.kind == "bet":
                bettor = a.pn_id
            b = facts.get(bettor) if bettor is not None else None
            if b is not None and a.pn_id == bettor:
                if (street in b.faced_check_raise and street not in b.faced_check_raise_action
                        and a.kind in ANSWERS):
                    b.faced_check_raise_action[street] = a.kind
            elif b is not None and a.kind == "raise" and prior == "x":
                b.faced_check_raise[street] = True

            f = facts.get(a.pn_id)
            if f is None:
                acted.add(a.pn_id)
                continue

            if a.kind in LINE_CODE:
                f.line[street] = f.line.get(street, "") + LINE_CODE[a.kind]

            # facing a bet or raise, whoever made it -- the c-bet family below is
            # the subset where it was the previous street's aggressor's c-bet
            if bet_made:
                f.faced_bet[street] = True
                if a.kind == "fold":
                    f.folded_to_bet[street] = True
                elif a.kind == "call":
                    f.called_bet[street] = True
                elif a.kind == "raise":
                    f.raised_bet[street] = True
                # The first time only: a bettor who is raised and calls has
                # answered a raise, and that is their decision facing a bet.
                if street not in f.faced_bet_action and a.kind in ANSWERS:
                    f.faced_bet_action[street] = a.kind
                    if faced is not None:
                        f.faced_bet_pot[street], f.faced_bet_size[street] = faced

            if a.kind == "raise" and street not in f.raise_pot and sized is not None:
                f.raise_pot[street], f.raise_size[street] = sized
            if a.kind == "bet" and sized is not None and street not in f.bet_answer:
                answer = _answer(a.pn_id, street_actions[i + 1:])
                if answer is not None:
                    f.bet_answer[street] = answer

            # c-bet opportunity: previous street's aggressor, first-in on this one
            if not bet_made and a.pn_id == prev_aggressor:
                f.cbet_opp[street] = True
                if a.kind == "bet":
                    f.cbet[street] = True

            # donk opportunity: first-in ahead of the previous street's aggressor,
            # who has yet to act on this street and still has chips to act with
            if (
                not bet_made
                and prev_aggressor is not None
                and a.pn_id != prev_aggressor
                and prev_aggressor not in acted
                and all_in_at.get(prev_aggressor, a.seq) >= a.seq
            ):
                f.donk_opp[street] = True
                f.donk_into[street] = prev_aggressor
                if a.kind == "bet":
                    f.donk[street] = True

            # facing a c-bet, before anyone raises over it
            if cbet_by is not None and not cbet_raised and a.pn_id != cbet_by:
                f.fold_to_cbet_opp[street] = True
                f.faced_cbet_by[street] = cbet_by
                if cbet_size is not None:
                    f.faced_cbet_size[street] = cbet_size
                if a.kind == "fold":
                    f.fold_to_cbet[street] = True
                elif a.kind == "raise":
                    f.raise_cbet[street] = True

            # The streets after a missed c-bet. None of these can share a street
            # with a c-bet or a donk opportunity: delayed c-bets and probes follow
            # a street that checked through, which has no aggressor, and a float
            # comes after the aggressor has acted, where a donk comes before.
            if not bet_made:
                if delayed_on and a.pn_id == pfa:
                    f.delayed_cbet_opp[street] = True
                    if a.kind == "bet":
                        f.delayed_cbet[street] = True
                if probe_on and a.pn_id != pfa and pfa not in acted:
                    f.probe_opp[street] = True
                    if a.kind == "bet":
                        f.probe[street] = True
                called_prev = f.vpip if street == FLOP else bool(f.called_bet.get(prev))
                if (
                    prev_aggressor is not None
                    and a.pn_id != prev_aggressor
                    and prev_aggressor in acted
                    and called_prev
                ):
                    f.float_opp[street] = True
                    if a.kind == "bet":
                        f.float_bet[street] = True

            # the preflop aggressor facing a probe, before anyone raises over it
            if probe_by is not None and not probe_raised and a.pn_id == pfa:
                f.fold_to_probe_opp[street] = True
                if a.kind == "fold":
                    f.fold_to_probe[street] = True

            # aggression frequency: checks excluded from both sides
            if a.kind in AGGRESSIVE:
                f.aggressive[street] = f.aggressive.get(street, 0) + 1
                f.agg_denom[street] = f.agg_denom.get(street, 0) + 1
            elif a.kind in ("call", "fold"):
                f.agg_denom[street] = f.agg_denom.get(street, 0) + 1

            if a.kind == "bet":
                if not bet_made and a.pn_id == prev_aggressor:
                    cbet_by = a.pn_id
                    cbet_size = sized[1] if sized is not None else None
                if street not in f.bet_pot and sized is not None:
                    f.bet_pot[street], f.bet_size[street] = sized
                if not bet_made and f.probe.get(street):
                    probe_by = a.pn_id
                bet_made = True
                street_aggressor = a.pn_id
            elif a.kind == "raise":
                if cbet_by is not None:
                    cbet_raised = True
                if probe_by is not None:
                    probe_raised = True
                bet_made = True
                street_aggressor = a.pn_id

            acted.add(a.pn_id)

        for f in facts.values():
            line = f.line.get(street, "")
            if line.startswith("x") and len(line) >= 2:
                f.check_raise_opp[street] = True
                if line.startswith("xr"):
                    f.check_raise[street] = True

        if street_aggressor in facts:
            facts[street_aggressor].aggressor[street] = True
        # A street with no bet ends on a check, and that check closed it. One
        # player checking alone -- everyone else all in -- decided nothing.
        last = street_actions[-1]
        if street_aggressor is None and last.kind == "check" and len(acted) >= 2:
            closed_by[street] = last.pn_id
            if last.pn_id in facts:
                facts[last.pn_id].check_back[street] = True
        else:
            unbet = False

        prev_aggressor = street_aggressor


def _sequences(hand: HandRow, facts: dict[str, Facts]) -> None:
    """Each street's voluntary actions, everyone's, written from each seat.

    A street was played when its board was dealt, or when anyone acted on it (a
    log cut mid-street), and at least two players were still in at its start: a
    hand everyone folded to is over, even if a board stands in the record. Every
    player still in gets a key, an all-in player included -- their row is the
    others' letters alone -- and a street nobody acted on, an all-in run-out, is
    an empty string rather than no key: they were in it and nothing happened.
    """
    folded = {a.pn_id for a in hand.actions if a.street == PREFLOP and a.kind == "fold"}
    for street in POSTFLOP_STREETS:
        acts = [
            a for a in hand.actions
            if a.street == street and not a.is_forced and a.kind in LINE_CODE
        ]
        live = [pid for pid in facts if pid not in folded]
        if (not acts and board_at(hand.board, street) is None) or len(live) < 2:
            break
        for pid in live:
            facts[pid].seq[street] = "".join(
                LINE_CODE[a.kind].upper() if a.pn_id == pid else LINE_CODE[a.kind] for a in acts
            )
        folded |= {a.pn_id for a in acts if a.kind == "fold"}


def _answer(bettor: str, after: list[HandAction]) -> str | None:
    """What a bet got from the players behind it, given the street's later actions.

    The answers run until the first raise over it, which is the answer whatever
    came before: a call and then a raise is a raised bet. Otherwise one call is
    enough to make it called, and it was folded to only when everyone who acted
    folded. None when nobody acted on it at all. The same reading as
    `review.failed_bluff`'s responders.
    """
    kinds: set[str] = set()
    for b in after:
        if b.pn_id == bettor:
            continue
        if b.kind in AGGRESSIVE:
            return "raise"
        kinds.add(b.kind)
    if "call" in kinds:
        return "call"
    if "fold" in kinds:
        return "fold"
    return None


def _jams(hand: HandRow, facts: dict[str, Facts]) -> None:
    """Who jammed on each street, and who faced and called those jams.

    A jam stays the thing to answer until someone raises over it with chips behind;
    a second all-in over the top becomes the new jam. Forced posts never jam: an
    all-in blind is not a decision.
    """
    for street in (PREFLOP, *POSTFLOP_STREETS):
        jammer: str | None = None
        for a in hand.actions:
            if a.street != street or a.is_forced:
                continue
            f = facts.get(a.pn_id)
            if jammer is not None and a.pn_id != jammer and f is not None:
                f.faced_jam[street] = True
                if a.kind == "call":
                    f.called_jam[street] = True
            if a.kind in AGGRESSIVE:
                jammer = a.pn_id if a.all_in else None
                if a.all_in and f is not None:
                    f.jam[street] = True


def _seat_ranks(hand: HandRow) -> dict[str, int]:
    """Postflop acting order from the button: small blind first, button last.

    Deliberately *not* `(seats_from_button - 1) % n_dealt_in`. A dead small blind
    leaves a slot no dealt-in player occupies, so `seats_from_button` can exceed
    `n_dealt_in - 1` (SPEC.md judgement call 4), and the modulo then collides two
    players onto one rank -- hand #25 of `pgl1UViJ4` is three-handed with seats 0,
    2 and 3, where `(0-1) % 3` and `(3-1) % 3` are both 2. Counting slots keeps
    the order right on those hands, which is all this needs: the position *label*
    stays untrustworthy there, and is still withheld.
    """
    slots = position_slots((p.seats_from_button for p in hand.players.values()), hand.n_dealt_in)
    return {
        pid: (p.seats_from_button - 1 if p.seats_from_button >= 1 else slots - 1)
        for pid, p in hand.players.items()
        if p.seats_from_button is not None
    }


def _table(hand: HandRow, facts: dict[str, Facts]) -> None:
    """Who each player was up against, and whether they closed the action."""
    fold_at: dict[str, int] = {}
    last_at: dict[str, int] = {}
    orbit: list[str] = []  # the first postflop orbit, in acting order
    first_street: str | None = None

    for a in hand.actions:
        if a.is_forced:
            continue
        last_at[a.pn_id] = a.seq
        if a.kind == "fold":
            fold_at.setdefault(a.pn_id, a.seq)
        if a.street in POSTFLOP_STREETS:
            if first_street is None:
                first_street = a.street
            if a.street == first_street and a.pn_id not in orbit:
                orbit.append(a.pn_id)

    seat_rank = _seat_ranks(hand)
    orbit_rank = {pid: i for i, pid in enumerate(orbit)}

    for pid, f in facts.items():
        # Opponents are those still in *at this player's last action*, not those
        # who saw the flop: the villain who folds to your c-bet is someone you
        # faced, and a player who folded before you acted is not. See SPEC.md.
        mine = last_at.get(pid)
        if mine is None:
            # They never acted voluntarily -- a walk, or all-in from a post. The
            # end-of-hand roster is the only statement available about who was in.
            others = [q for q in hand.players if q != pid and not hand.players[q].folded]
        else:
            others = [q for q in hand.players if q != pid and fold_at.get(q, mine + 1) > mine]
        others.sort(
            key=lambda q: (
                orbit_rank.get(q, len(orbit)),
                seat_rank.get(q, len(seat_rank)),
                hand.players[q].seat,
            )
        )
        f.opponents = tuple(others)

        group = [pid, *others]
        if len(group) < 2:
            continue
        # Acting order is the better signal where it exists, because it is raw:
        # it survives a dead button, which the seat labels do not. It needs every
        # player in the group to have actually acted, though -- one lone actor
        # with everyone else all-in would read as trivially last, and so as in
        # position, which means nothing.
        if all(q in orbit_rank for q in group):
            rank = orbit_rank
        elif all(q in seat_rank for q in group):
            rank = seat_rank
        else:
            continue  # stays None: unknown is not "out of position"
        ordered = sorted(group, key=lambda q: rank[q])
        f.pos_order = ordered.index(pid)
        f.pos_players = len(group)
        f.in_position = f.pos_order == len(group) - 1


def pot_at(hand: HandRow, pot: int) -> dict[str, int]:
    """Chips in the middle when each dealt postflop street began.

    The running total of every action on the earlier streets, forced posts
    included. Capped at the final `pot`, which already excludes uncalled bets: an
    over-shove the caller could not match never sat in the middle.
    """
    out: dict[str, int] = {}
    for street in POSTFLOP_STREETS:
        if board_at(hand.board, street) is None:
            break
        before = {PREFLOP, *POSTFLOP_STREETS[: POSTFLOP_STREETS.index(street)]}
        running = sum(a.amount for a in hand.actions if a.street in before)
        out[street] = min(running, pot)
    return out


def derive(hand: HandRow, every_game: bool = False) -> list[Facts]:
    """Produce one Facts row per dealt-in player.

    A hand not `is_analysed` -- PLO, or Double Board -- gets money only, no
    opportunity and no cards, so no rate, range or tag counts it. `every_game`
    derives its lines in full, for the hand lists (Session), which show it like
    any other. Either way `hole_cards` stays None on it and `pot_level` 0: only
    `cards` shows them.
    """
    holdem = is_holdem(hand)
    analysed = holdem and not hand.double_board
    bomb = is_bomb_pot(hand)
    pot = sum(p.contributed for p in hand.players.values())
    streets_pot = pot_at(hand, pot)
    slots = position_slots((p.seats_from_button for p in hand.players.values()), hand.n_dealt_in)
    facts = {
        pid: Facts(
            hand_id=hand.hand_id,
            pn_id=pid,
            n_dealt_in=hand.n_dealt_in,
            seats_from_button=p.seats_from_button,
            dead_button=hand.dead_button,
            blinds_irregular=hand.blinds_irregular,
            position_slots=slots,
            game_id=hand.game_id,
            hand_number=hand.hand_number,
            ts=hand.ts,
            net=p.collected - p.contributed + p.bounty + p.nit,
            bounty=p.bounty,
            nit=p.nit,
            holdem=holdem,
            analysed=analysed,
            bomb_pot=bomb,
            double_board=hand.double_board,
            board2=hand.board2,
            pot=pot,
            bb_size=hand.bb,
            complete=hand.complete,
            hole_cards=p.hole_cards if analysed else None,
            cards=p.hole_cards,
            board=hand.board,
            pot_at=dict(streets_pot),
            # No preflop betting: neither a walk nor a limped pot. `_preflop` keeps it.
            pot_level=0 if bomb or not analysed else 1,
        )
        for pid, p in hand.players.items()
    }
    if analysed or every_game:
        aggressor = _preflop(hand, facts)
        _postflop(hand, facts, aggressor)
        _sequences(hand, facts)
        _jams(hand, facts)
        _table(hand, facts)

    folded_preflop = {
        a.pn_id for a in hand.actions if a.street == PREFLOP and a.kind == "fold"
    }
    for a in hand.actions:
        if a.kind == "fold" and not a.is_forced and a.pn_id in facts:
            f = facts[a.pn_id]
            f.fold_street = f.fold_street or a.street
    for pid, p in hand.players.items():
        f = facts[pid]
        f.saw_flop = hand.saw_flop and pid not in folded_preflop
        f.wtsd_opp = f.saw_flop
        # Showdown = two or more players still live at the end (never inferred from
        # `shows` lines -- see SPEC.md). A log cut off before the river shows the
        # live players at no showdown: it just stopped.
        f.wtsd = (f.saw_flop and hand.went_to_showdown and not p.folded
                  and (hand.complete or len(hand.board) == 5))
        f.wsd = f.wtsd and p.collected > 0
        f.won_pot = p.collected > 0
    if not analysed and not every_game:
        # Money only: who got to the flop and the showdown, which Results' lines
        # split the money by, and no opportunity, so no rate counts the hand.
        return list(facts.values())

    _foundation(hand, facts)
    _timing(hand, facts)
    return list(facts.values())


def _foundation(hand: HandRow, facts: dict[str, Facts]) -> None:
    """Straddles, players on the flop, live blinds and stack depth."""
    straddled = any(a.is_forced and a.post_kind == POST_STRADDLE for a in hand.actions)
    flop_players = [pid for pid, f in facts.items() if f.saw_flop]
    players_flop = len(flop_players) if hand.saw_flop else None
    blinds = {
        a.pn_id for a in hand.actions
        if a.street == PREFLOP and a.is_forced and a.amount > 0 and a.post_kind not in DEAD_POST_KINDS
    }
    stacks = {pid: p.starting_stack for pid, p in hand.players.items()}
    preflop_put: dict[str, int] = {}
    # Every forced post, live or dead: folding loses a dead one just the same.
    posted: dict[str, int] = {}
    for a in hand.actions:
        if a.street == PREFLOP:
            preflop_put[a.pn_id] = preflop_put.get(a.pn_id, 0) + a.amount
        if a.is_forced:
            posted[a.pn_id] = posted.get(a.pn_id, 0) + a.amount
    behind = {
        pid: max(stacks[pid] - preflop_put.get(pid, 0), 0)
        for pid in flop_players if stacks[pid] is not None
    }
    flop_pot = next(iter(facts.values())).pot_at.get(FLOP) if facts else None

    for pid, f in facts.items():
        f.straddled = straddled
        f.players_flop = players_flop
        f.posted_blind = pid in blinds
        f.posted = posted.get(pid, 0)
        own = stacks[pid]
        others = [s for q, s in stacks.items() if q != pid]
        # Judgement call 26: an unknown stack is unknown, not zero.
        if hand.bb and own is not None and others and None not in others:
            f.eff_bb = round(min(own, max(others)) / hand.bb, 2)
        if f.saw_flop and flop_pot and len(behind) == len(flop_players) and len(flop_players) >= 2:
            rest = [b for q, b in behind.items() if q != pid]
            f.spr_flop = round(min(behind[pid], max(rest)) / flop_pot, 2)


# --- timing tells (SPEC.md, "Timing tells") ------------------------------------

#: How long a decision took, fastest first. `pre` is a check, call or fold that
#: came so soon after the action before it that it was pre-selected with
#: PokerNow's buttons; `timeout` is a fold or check the clock made for them.
TEMPO_BUCKETS = ("pre", "snap", "normal", "tank", "timeout")

#: The cuts, by action class, calibrated on the pool's 115,469 timed decisions
#: (14,720 hands, 2026-10-12). The times have no natural gap, so each cut is a
#: percentile choice, near the pool's p20 and p80 for its class. Mirrored in
#: SPEC.md; `tags.THRESHOLDS["tempo"]` is this same dict.
TEMPO_CUTS: dict[str, float] = {
    # A bet or raise: snap under 2 s, tank over 5 s.
    "aggressive_snap_ms": 2000,
    "aggressive_tank_ms": 5000,
    # A check, call or fold: snap up to 1 s, tank over 3.5 s.
    "passive_snap_ms": 1000,
    "passive_tank_ms": 3500,
    # Pre-selected: a check, call or fold at most this long after a voluntary
    # action. PokerNow logs a pre-selected action one server step (~0.85 s) after
    # the action that set it off, so these land in a spike just under 1 s.
    "pre_ms": 1000,
    # A fold or check from the full clock (limit plus bank) to this much past it.
    "timeout_ms": 1000,
    # The relative buckets: under half, and over twice, the player's own median.
    "rel_snap": 0.5,
    "rel_tank": 2.0,
}

#: The actions PokerNow's buttons can pre-select.
PASSIVE = frozenset({"check", "call", "fold"})


def tempo_of(kind: str, think_ms: int, after_action: bool, clock_ms: int | None) -> str:
    """The TEMPO_BUCKETS bucket of one decision of `kind` that took `think_ms`.

    `after_action` says the time runs from a voluntary action, not a post or a
    board line: only then can a check, call or fold have been pre-selected.
    """
    c = TEMPO_CUTS
    if kind in ("fold", "check") and clock_ms is not None and clock_ms <= think_ms < clock_ms + c["timeout_ms"]:
        return "timeout"
    if kind in PASSIVE:
        if after_action and think_ms <= c["pre_ms"]:
            return "pre"
        if think_ms <= c["passive_snap_ms"]:
            return "snap"
        return "tank" if think_ms > c["passive_tank_ms"] else "normal"
    if think_ms < c["aggressive_snap_ms"]:
        return "snap"
    return "tank" if think_ms > c["aggressive_tank_ms"] else "normal"


def _timing(hand: HandRow, facts: dict[str, Facts]) -> None:
    """Time each player's first voluntary decision on each street.

    The clock runs from the previous post, action or board line in the hand --
    never from chat (a join, a seat request) logged in between. The first decision
    on a postflop street is timed from that street's board line, the first preflop
    one from the last blind posted. A line's time is its `ord` // 100, in ms.
    """
    anchor: int | None = None
    after_action = False
    street: str | None = None
    decided: set[tuple[str, str]] = set()
    for a in hand.actions:
        if a.street != street:
            street = a.street
            if street != PREFLOP:
                anchor, after_action = hand.board_ords.get(street), False
        if not a.is_forced and (a.pn_id, street) not in decided:
            decided.add((a.pn_id, street))
            f = facts.get(a.pn_id)
            if f is not None and a.ord is not None and anchor is not None and a.ord >= anchor:
                think = a.ord // 100 - anchor // 100
                f.think_ms[street] = think
                f.tempo[street] = tempo_of(a.kind, think, after_action, hand.clock_ms)
        anchor, after_action = a.ord, not a.is_forced
