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

from ..logfmt.events import FLOP, PREFLOP, RIVER, TURN
from .cards import board_at

POSTFLOP_STREETS = (FLOP, TURN, RIVER)
PREV_STREET = {FLOP: PREFLOP, TURN: FLOP, RIVER: TURN}

VOLUNTARY_COMMIT = {"call", "bet", "raise"}
AGGRESSIVE = {"bet", "raise"}

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


@dataclass(slots=True)
class Facts:
    """One row per (hand, player). Booleans throughout; aggregation sums them."""

    hand_id: int
    pn_id: str
    n_dealt_in: int
    seats_from_button: int | None
    dead_button: bool
    blinds_irregular: bool = False
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
    #: `faced_bet` -- how many bets and raises were in front of them (1 a bet, 2 a
    #: raise, 3 a re-raise), the size of the latest one, and what they did there,
    #: exactly one of ANSWERS. See SPEC.md, "Facing a bet".
    faced_level: dict[str, int] = field(default_factory=dict)
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

    hole_cards: str | None = None
    board: tuple[str, ...] = ()


def _preflop(hand: HandRow, facts: dict[str, Facts]) -> str | None:
    """Walk preflop voluntary actions, tracking bet level. Returns the last aggressor."""
    level = 1  # the blinds
    aggressor: str | None = None
    opener: str | None = None  # who made it level 2
    open_bb: float | None = None

    for a in hand.actions:
        if a.street != PREFLOP or a.is_forced:
            continue
        f = facts.get(a.pn_id)
        if f is None:
            continue

        # --- opportunity is recorded BEFORE the action resolves.
        # Rule 3 of SPEC.md: acting *is* the opportunity.
        f.vpip_opp = True
        f.pfr_opp = True
        if level == 2 and a.pn_id != aggressor:
            f.three_bet_opp = True
        if level == 3 and a.pn_id == opener:
            f.fold_to_3bet_opp = True
        # A preflop `bet` (no blind in front of it) takes the level exactly as a
        # raise does, so it is recorded as one.
        f.pf_faced.setdefault(level, "raise" if a.kind == "bet" else a.kind)

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

    for f in facts.values():
        f.pot_level = level
        f.open_bb = open_bb
    if aggressor is not None and aggressor in facts:
        facts[aggressor].pfa = True
    return aggressor


def _postflop(hand: HandRow, facts: dict[str, Facts], preflop_aggressor: str | None) -> None:
    prev_aggressor = preflop_aggressor

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
            continue

        bet_made = False
        cbet_by: str | None = None
        cbet_size: str | None = None
        cbet_raised = False
        street_aggressor: str | None = None
        acted: set[str] = set()
        # The street's betting as it stands: bets and raises so far (1 a bet, 2 a
        # raise), each player's chips in on this street, the most anyone has in,
        # and the size of the latest bet or raise as (pot fraction, bucket).
        level = 0
        put: dict[str, int] = {}
        top = 0
        latest: tuple[float, str] | None = None

        for i, a in enumerate(street_actions):
            # --- shared street state first, for every actor: a player missing
            # from `facts` still moves the level and the price for everyone else.
            faced = (level, latest)
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
                level += 1
                latest = sized
            put[a.pn_id] = put.get(a.pn_id, 0) + a.amount
            top = max(top, put[a.pn_id])

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
                if street not in f.faced_level and a.kind in ANSWERS:
                    f.faced_level[street] = faced[0]
                    f.faced_bet_action[street] = a.kind
                    if faced[1] is not None:
                        f.faced_bet_pot[street], f.faced_bet_size[street] = faced[1]

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
                bet_made = True
                street_aggressor = a.pn_id
            elif a.kind == "raise":
                if cbet_by is not None:
                    cbet_raised = True
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
        if street_aggressor is None and last.kind == "check" and len(acted) >= 2 and last.pn_id in facts:
            facts[last.pn_id].check_back[street] = True

        prev_aggressor = street_aggressor


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
    slots = max([hand.n_dealt_in, *((p.seats_from_button or 0) + 1 for p in hand.players.values())])
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


def derive(hand: HandRow) -> list[Facts]:
    """Produce one Facts row per dealt-in player."""
    pot = sum(p.contributed for p in hand.players.values())
    streets_pot = pot_at(hand, pot)
    facts = {
        pid: Facts(
            hand_id=hand.hand_id,
            pn_id=pid,
            n_dealt_in=hand.n_dealt_in,
            seats_from_button=p.seats_from_button,
            dead_button=hand.dead_button,
            blinds_irregular=hand.blinds_irregular,
            game_id=hand.game_id,
            hand_number=hand.hand_number,
            ts=hand.ts,
            net=p.collected - p.contributed + p.bounty,
            pot=pot,
            bb_size=hand.bb,
            complete=hand.complete,
            hole_cards=p.hole_cards,
            board=hand.board,
            pot_at=dict(streets_pot),
        )
        for pid, p in hand.players.items()
    }

    aggressor = _preflop(hand, facts)
    _postflop(hand, facts, aggressor)
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
        # `shows` lines -- see SPEC.md).
        f.wtsd = f.saw_flop and hand.went_to_showdown and not p.folded
        f.wsd = f.wtsd and p.collected > 0

    return list(facts.values())
