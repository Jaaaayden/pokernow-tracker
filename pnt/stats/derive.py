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

from dataclasses import dataclass, field

from ..logfmt.events import FLOP, PREFLOP, RIVER, TURN

POSTFLOP_STREETS = (FLOP, TURN, RIVER)
PREV_STREET = {FLOP: PREFLOP, TURN: FLOP, RIVER: TURN}

VOLUNTARY_COMMIT = {"call", "bet", "raise"}
AGGRESSIVE = {"bet", "raise"}


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
    bb: int | None
    ts: str | None
    players: dict[str, HandPlayerRow]
    actions: list[HandAction]
    #: First run of the board. Run-it-twice hands classify on run one only.
    board: tuple[str, ...] = ()


@dataclass(slots=True)
class Facts:
    """One row per (hand, player). Booleans throughout; aggregation sums them."""

    hand_id: int
    pn_id: str
    n_dealt_in: int
    seats_from_button: int | None
    dead_button: bool
    blinds_irregular: bool = False

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
    fold_to_cbet_opp: dict[str, bool] = field(default_factory=dict)
    fold_to_cbet: dict[str, bool] = field(default_factory=dict)

    aggressive: dict[str, int] = field(default_factory=dict)
    agg_denom: dict[str, int] = field(default_factory=dict)

    saw_flop: bool = False
    wtsd_opp: bool = False
    wtsd: bool = False
    wsd: bool = False

    net: int = 0
    bb_size: int | None = None

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
    #: This player's first `bet` on each postflop street as a fraction of the pot
    #: it was made into. 1.0 is a pot-sized bet; above it is an overbet.
    bet_pot: dict[str, float] = field(default_factory=dict)

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
    running = 0
    for a in hand.actions:
        pot_before[a.seq] = running
        running += a.amount

    for street in POSTFLOP_STREETS:
        street_actions = [a for a in hand.actions if a.street == street and not a.is_forced]
        if not street_actions:
            prev_aggressor = None
            continue

        bet_made = False
        cbet_by: str | None = None
        cbet_raised = False
        street_aggressor: str | None = None

        for a in street_actions:
            f = facts.get(a.pn_id)
            if f is None:
                continue

            # c-bet opportunity: previous street's aggressor, first-in on this one
            if not bet_made and a.pn_id == prev_aggressor:
                f.cbet_opp[street] = True
                if a.kind == "bet":
                    f.cbet[street] = True

            # facing a c-bet, before anyone raises over it
            if cbet_by is not None and not cbet_raised and a.pn_id != cbet_by:
                f.fold_to_cbet_opp[street] = True
                if a.kind == "fold":
                    f.fold_to_cbet[street] = True

            # aggression frequency: checks excluded from both sides
            if a.kind in AGGRESSIVE:
                f.aggressive[street] = f.aggressive.get(street, 0) + 1
                f.agg_denom[street] = f.agg_denom.get(street, 0) + 1
            elif a.kind in ("call", "fold"):
                f.agg_denom[street] = f.agg_denom.get(street, 0) + 1

            if a.kind == "bet":
                if not bet_made and a.pn_id == prev_aggressor:
                    cbet_by = a.pn_id
                if street not in f.bet_pot and pot_before[a.seq] > 0:
                    f.bet_pot[street] = round(a.amount / pot_before[a.seq], 3)
                bet_made = True
                street_aggressor = a.pn_id
            elif a.kind == "raise":
                if cbet_by is not None:
                    cbet_raised = True
                bet_made = True
                street_aggressor = a.pn_id

        prev_aggressor = street_aggressor


def derive(hand: HandRow) -> list[Facts]:
    """Produce one Facts row per dealt-in player."""
    facts = {
        pid: Facts(
            hand_id=hand.hand_id,
            pn_id=pid,
            n_dealt_in=hand.n_dealt_in,
            seats_from_button=p.seats_from_button,
            dead_button=hand.dead_button,
            blinds_irregular=hand.blinds_irregular,
            net=p.collected - p.contributed + p.bounty,
            bb_size=hand.bb,
            hole_cards=p.hole_cards,
            board=hand.board,
        )
        for pid, p in hand.players.items()
    }

    aggressor = _preflop(hand, facts)
    _postflop(hand, facts, aggressor)

    folded_preflop = {
        a.pn_id for a in hand.actions if a.street == PREFLOP and a.kind == "fold"
    }
    for pid, p in hand.players.items():
        f = facts[pid]
        f.saw_flop = hand.saw_flop and pid not in folded_preflop
        f.wtsd_opp = f.saw_flop
        # Showdown = two or more players still live at the end (never inferred from
        # `shows` lines -- see SPEC.md).
        f.wtsd = f.saw_flop and hand.went_to_showdown and not p.folded
        f.wsd = f.wtsd and p.collected > 0

    return list(facts.values())
