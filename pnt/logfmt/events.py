"""Typed events -- the intermediate representation between raw log lines and hands.

One dataclass per line *kind*. `grammar.py` produces these; `parser.py` consumes
them. Keeping this layer explicit is what lets an unrecognized line become a
recorded `Unknown` rather than a silent drop.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .tokens import PlayerRef

# --- Post kinds -------------------------------------------------------------
# Every one of these is forced money: it counts toward stat denominators and
# never toward a VPIP numerator. See pnt/stats/SPEC.md.
POST_SB = "sb"
POST_BB = "bb"
POST_ANTE = "ante"
POST_MISSED_BB = "missed_bb"
POST_MISSING_SB = "missing_sb"
POST_STRADDLE = "straddle"

FORCED_POST_KINDS = frozenset(
    {POST_SB, POST_BB, POST_ANTE, POST_MISSED_BB, POST_MISSING_SB, POST_STRADDLE}
)

# --- Streets ----------------------------------------------------------------
PREFLOP = "preflop"
FLOP = "flop"
TURN = "turn"
RIVER = "river"
STREET_ORDER = (PREFLOP, FLOP, TURN, RIVER)


@dataclass(frozen=True, slots=True)
class Event:
    """Base: every event carries its source ordering key and raw text."""

    ord: int
    raw: str


@dataclass(frozen=True, slots=True)
class HandStart(Event):
    hand_number: int
    table_hand_id: str
    variant: str
    dealer: PlayerRef | None  # None when the log says "(dead button)"
    dead_button: bool


@dataclass(frozen=True, slots=True)
class HandEnd(Event):
    hand_number: int


@dataclass(frozen=True, slots=True)
class PlayerStacks(Event):
    """The dealt-in roster. This -- not join/quit events -- defines who is in the hand."""

    seats: tuple[tuple[int, PlayerRef, int], ...]  # (seat, player, starting_stack)


@dataclass(frozen=True, slots=True)
class HeroCards(Event):
    cards: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Post(Event):
    player: PlayerRef
    kind: str
    amount: int


@dataclass(frozen=True, slots=True)
class Action(Event):
    """A voluntary action.

    `amount_to` is the raw figure from the log and is **cumulative for the
    street**, not incremental -- see parser.py. None for fold/check.
    """

    player: PlayerRef
    kind: str  # fold | check | call | bet | raise
    amount_to: int | None
    all_in: bool


@dataclass(frozen=True, slots=True)
class StreetDealt(Event):
    street: str
    run: int  # 0 = first run, 1 = second run (run-it-twice)
    board: tuple[str, ...]  # full board for this street on this run


@dataclass(frozen=True, slots=True)
class UncalledReturn(Event):
    player: PlayerRef
    amount: int


@dataclass(frozen=True, slots=True)
class Collected(Event):
    player: PlayerRef
    amount: int
    ranking: str | None  # e.g. "Two Pair, A's & 6's"; None for uncontested pots


@dataclass(frozen=True, slots=True)
class BountyPaid(Event):
    """A side-bet transfer between two players, outside the pot.

    Emitted only by the ``paid N ... to X`` line. The matching ``collected N from
    the 7-2 bounty`` line is a *summary* of those payments -- crediting both would
    pay the winner twice -- so it is classified as `Noise`.
    """

    payer: PlayerRef
    payee: PlayerRef
    amount: int
    kind: str = "7-2"


@dataclass(frozen=True, slots=True)
class Shows(Event):
    player: PlayerRef
    cards: tuple[str, ...]  # may be a single card (voluntary partial show)


@dataclass(frozen=True, slots=True)
class SeatChange(Event):
    """joined / quit / stand up / sit back / requested seat / admin approval."""

    player: PlayerRef
    kind: str
    stack: int | None


@dataclass(frozen=True, slots=True)
class AdminStackChange(Event):
    player: PlayerRef
    from_stack: int
    to_stack: int


@dataclass(frozen=True, slots=True)
class BlindChange(Event):
    which: str  # sb | bb | ante
    #: int for chip-denominated games, float when the table is configured in a
    #: decimal currency (e.g. "changed from 0.10 to 0.05"). Only ever a game-level
    #: fallback: a hand's big blind comes from that hand's actual BB post.
    from_amount: float
    to_amount: float


@dataclass(frozen=True, slots=True)
class Noise(Event):
    """Recognized but semantically inert: rabbit hunts, RIT prompts, config dumps."""

    kind: str


@dataclass(frozen=True, slots=True)
class Unknown(Event):
    """Unrecognized line. Never dropped -- these land in `parse_misses`."""

    reason: str = "no grammar rule matched"
