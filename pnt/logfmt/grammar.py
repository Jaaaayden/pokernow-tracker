"""Line grammar: one rule per PokerNow log line kind.

The rule table below was derived by exhaustively normalizing every entry across
the fixture logs (~3400 entries) into distinct "shapes", not by guessing. Any
line that matches nothing becomes an `Unknown` event and is recorded -- never
dropped. That is the mechanism that makes "a re-import repairs any gap" true.

Player names are matched **greedily** (`.+`). Names in real logs include ``all
in`` and ``500``; greedy matching with a fully anchored suffix backtracks from
the end of the line and so cannot be fooled by name content.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from contextvars import ContextVar
from decimal import Decimal

from . import events as E
from .tokens import PN_ID, PlayerRef, parse_cards, split_player_token

_P = r'"(?P<p>.+)"'

#: A second quoted name in the same line. `_P` cannot be reused -- two identically
#: named groups in one pattern is a regex error -- and its greedy `.+` would
#: otherwise swallow everything between the first and last quote.
_P2 = r'"[^"]*"'

#: An amount as the log prints it: whole chips, or a decimal on a Cents Mode table.
_AMT = r"\d+(?:\.\d+)?"

#: Whether the line being classified was logged in Cents Mode (see `classify`).
_CENTS: ContextVar[bool] = ContextVar("cents", default=False)


def _chips(text: str, cents: bool = False) -> int:
    """An amount in the table's own integer units, whatever the log printed.

    Cents Mode only changes the display: the table still counts in whole units
    and prints each one divided by 100 (the blind schedule `5/20` is logged as a
    small blind of ``0.05``). So a decimal amount, or any amount logged in Cents
    Mode, is multiplied back by 100. Every amount of a hand then shares one unit
    with the hands dealt outside Cents Mode, and stats in big blinds are unchanged.
    """
    if cents or "." in text or _CENTS.get():
        return int((Decimal(text) * 100).to_integral_value())
    return int(text)


def _player(m: re.Match, group: str = "p") -> PlayerRef:
    return split_player_token(m.group(group))


# --- Individual builders ----------------------------------------------------


def _hand_start(m: re.Match, ord_: int, raw: str) -> E.Event:
    who = m.group("who")
    dead = who == "dead button"
    dealer = None if dead else split_player_token(who[len('dealer: "') : -1])
    return E.HandStart(
        ord=ord_,
        raw=raw,
        hand_number=int(m.group("n")),
        table_hand_id=m.group("hid"),
        variant=m.group("variant").strip(),
        dealer=dealer,
        dead_button=dead,
    )


_SEAT_RE = re.compile(rf'#(\d+) "(.+)" \(({_AMT})\)')
#: Between two seats: ``(stack) | #seat``. Splitting on a bare ``|`` would cut a
#: name with a pipe in it, and lose the whole roster with it.
_SEAT_SPLIT = re.compile(r"(?<=\)) \| (?=#\d)")


def _player_stacks(m: re.Match, ord_: int, raw: str) -> E.Event:
    found = []
    for part in _SEAT_SPLIT.split(m.group("body")):
        # Each seat must be the whole field: a name containing '" (' or ') | #'
        # could otherwise desync the roster, so we verify rather than assume.
        seat = _SEAT_RE.fullmatch(part)
        if seat is None:
            return E.Unknown(ord=ord_, raw=raw, reason="player-stacks roster desync")
        found.append(seat.groups())
    # A stack printed with a decimal point is Cents Mode, so every stack is.
    cents = any("." in stack for _, _, stack in found)
    seats = tuple(
        (int(seat), split_player_token(tok), _chips(stack, cents)) for seat, tok, stack in found
    )
    return E.PlayerStacks(ord=ord_, raw=raw, seats=seats, cents=cents)


_POST_KINDS = {
    "small blind": E.POST_SB,
    "big blind": E.POST_BB,
    "ante": E.POST_ANTE,
    "missed big blind": E.POST_MISSED_BB,
    "missing small blind": E.POST_MISSING_SB,
    "straddle": E.POST_STRADDLE,
}

_STREETS = {"Flop": E.FLOP, "Turn": E.TURN, "River": E.RIVER}

_SEAT_KINDS = {
    "joined the game with a stack of": "joined",
    "quits the game with a stack of": "quit",
    "stand up with the stack of": "stand_up",
    "sit back with the stack of": "sit_back",
}


# --- Rule table -------------------------------------------------------------
# Order matters only where one pattern could shadow another; each is anchored,
# so in practice these are mutually exclusive. Specific-before-general anyway.

Rule = tuple[re.Pattern, Callable[[re.Match, int, str], E.Event]]

RULES: list[Rule] = [
    # -- hand structure
    (
        re.compile(
            r"^-- starting hand #(?P<n>\d+) \(id: (?P<hid>\S+)\)\s+"
            r'(?P<variant>.*?) \((?P<who>dealer: ".+"|dead button)\) --$'
        ),
        _hand_start,
    ),
    (
        re.compile(r"^-- ending hand #(?P<n>\d+) --$"),
        lambda m, o, r: E.HandEnd(ord=o, raw=r, hand_number=int(m.group("n"))),
    ),
    (re.compile(r"^Player stacks: (?P<body>.+)$"), _player_stacks),
    (
        re.compile(r"^Your hand is (?P<cards>.+)$"),
        lambda m, o, r: E.HeroCards(ord=o, raw=r, cards=tuple(parse_cards(m.group("cards")))),
    ),
    # -- forced posts (before generic actions)
    (
        # The `and go all in` suffix is the same one `calls`/`bets`/`raises to`
        # carry, and it appears here whenever a stack is shorter than the blind it
        # owes. Without it the line did not match at all, and an unmatched post is
        # not a missing label but a missing *blind*: the chips never enter the pot,
        # the hand records no big-blind post, and every net figure in it is wrong.
        # PokerNow writes "posts an ante"; the article is optional, so that line
        # and the "posts a ..." of every other post both match.
        re.compile(
            rf"^{_P} posts an? (?P<kind>small blind|big blind|ante|missed big blind|"
            rf"missing small blind|straddle) of (?P<amt>{_AMT})(?P<allin> and go all in)?$"
        ),
        lambda m, o, r: E.Post(
            ord=o,
            raw=r,
            player=_player(m),
            kind=_POST_KINDS[m.group("kind")],
            amount=_chips(m.group("amt")),
            all_in=bool(m.group("allin")),
        ),
    ),
    (
        # A bomb pot's buy-in: the first player "posts a bet", the rest "call" it,
        # all of it forced. Matched before the plain `calls N` rule below. A short
        # stack's "and go all in" can come with two spaces before the label.
        re.compile(
            rf"^{_P} (?:posts a bet of|calls) (?P<amt>{_AMT})(?P<allin> and go all in)?\s+"
            r"\(bomb pot bet\)$"
        ),
        lambda m, o, r: E.Post(
            ord=o,
            raw=r,
            player=_player(m),
            kind=E.POST_BOMB,
            amount=_chips(m.group("amt")),
            all_in=bool(m.group("allin")),
        ),
    ),
    # -- voluntary actions
    (
        re.compile(rf"^{_P} (?P<kind>folds|checks)$"),
        lambda m, o, r: E.Action(
            ord=o,
            raw=r,
            player=_player(m),
            kind={"folds": "fold", "checks": "check"}[m.group("kind")],
            amount_to=None,
            all_in=False,
        ),
    ),
    (
        re.compile(rf"^{_P} (?P<kind>calls|bets) (?P<amt>{_AMT})(?P<allin> and go all in)?$"),
        lambda m, o, r: E.Action(
            ord=o,
            raw=r,
            player=_player(m),
            kind={"calls": "call", "bets": "bet"}[m.group("kind")],
            amount_to=_chips(m.group("amt")),
            all_in=bool(m.group("allin")),
        ),
    ),
    (
        re.compile(rf"^{_P} raises to (?P<amt>{_AMT})(?P<allin> and go all in)?$"),
        lambda m, o, r: E.Action(
            ord=o,
            raw=r,
            player=_player(m),
            kind="raise",
            amount_to=_chips(m.group("amt")),
            all_in=bool(m.group("allin")),
        ),
    ),
    # -- board. A second board comes from run-it-twice ("second run", dealt once all
    # -- action is over) or from Double Board ("second board", dealt with the first
    # -- on every street, betting in between). Both are run 1 of the same pot.
    (
        re.compile(
            r"^(?P<street>Flop|Turn|River)(?P<run> \(second (?P<what>run|board)\))?:\s*(?P<rest>.+)$"
        ),
        lambda m, o, r: E.StreetDealt(
            ord=o,
            raw=r,
            street=_STREETS[m.group("street")],
            run=1 if m.group("run") else 0,
            board=tuple(parse_cards(m.group("rest"))),
            double_board=m.group("what") == "board",
        ),
    ),
    # -- pot resolution
    (
        re.compile(rf"^Uncalled bet of (?P<amt>{_AMT}) returned to {_P}$"),
        lambda m, o, r: E.UncalledReturn(
            ord=o, raw=r, player=_player(m), amount=_chips(m.group("amt"))
        ),
    ),
    (
        re.compile(rf"^{_P} collected (?P<amt>{_AMT}) from pot(?: with (?P<rank>.+))?$"),
        lambda m, o, r: E.Collected(
            ord=o,
            raw=r,
            player=_player(m),
            amount=_chips(m.group("amt")),
            ranking=m.group("rank"),
        ),
    ),
    # -- 7-2 bounty: a side bet settled between players, outside the pot.
    # The payment lines carry the money. The winner's "collected N from the 7-2
    # bounty" line that follows is the SUM of those payments, so it is recognized
    # as noise -- counting both would credit the winner twice.
    (
        re.compile(
            rf"^{_P} paid (?P<amt>{_AMT}) for the (?P<kind>.+?) bounty to "
            rf"\"(?P<p2>[^\"]*)\"(?P<more>(?:, \"[^\"]*\")*)$"
        ),
        lambda m, o, r: E.BountyPaid(
            ord=o,
            raw=r,
            payer=_player(m),
            payee=split_player_token(m.group("p2")),
            amount=_chips(m.group("amt")),
            kind=m.group("kind"),
            others=tuple(split_player_token(t) for t in re.findall(r'"([^"]*)"', m.group("more"))),
        ),
    ),
    (
        re.compile(rf"^{_P} collected (?P<amt>{_AMT}) from the (?P<kind>.+?) bounty$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="bounty_summary"),
    ),
    # -- Nit Game: each player escrows the prize per opponent from their stack;
    # -- the last to win a pot pays it to everyone else. Only the penalty moves
    # -- chips between players (see `E.NitPenalty`); the rest is bookkeeping.
    (
        re.compile(
            rf"^{_P} paid (?P<amt>{_AMT}) each to (?P<to>\"[^\"]*\"(?:, \"[^\"]*\")*) "
            r"as the Nit penalty\.$"
        ),
        lambda m, o, r: E.NitPenalty(
            ord=o,
            raw=r,
            payer=_player(m),
            payees=tuple(split_player_token(t) for t in re.findall(r'"([^"]*)"', m.group("to"))),
            amount=_chips(m.group("amt")),
        ),
    ),
    (
        re.compile(rf"^{_P} cleared the Nit\.$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="nit_cleared"),
    ),
    (
        re.compile(r"^Asking (?:interested|seated) players .*Nit Game.*\.$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="nit_prompt"),
    ),
    (
        re.compile(r"^Nit Game activated with \d+ players\.$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="nit_activated"),
    ),
    (
        # Too few confirmed: the round is called off, and its buy-ins moved nothing.
        re.compile(r"^Nit Game did not start because there were not enough confirmed players\.$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="nit_not_started"),
    ),
    (
        re.compile(rf"^The player {_P} confirmed the Nit Game using (?:their stack|an add-on)\.$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="nit_buy_in"),
    ),
    (
        # An escrow bought from off the table, not the stack: still the player's own
        # money, so like the buy-in it moves no net.
        re.compile(rf"^The player {_P} made an add-on of {_AMT} to their Nit Escrow\.$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="nit_add_on"),
    ),
    (
        re.compile(
            r"^\"[^\"]*\"(?:, \"[^\"]*\")* confirmed the Nit Game using their existing Nit Escrow\.$"
        ),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="nit_escrow_kept"),
    ),
    (
        re.compile(
            rf"^The player {_P} received {_AMT} chips of returned Nit escrow when leaving the table\.$"
        ),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="nit_escrow_returned"),
    ),
    (
        re.compile(rf"^{_P} shows a (?P<cards>.+)\.$"),
        lambda m, o, r: E.Shows(
            ord=o, raw=r, player=_player(m), cards=tuple(parse_cards(m.group("cards")))
        ),
    ),
    # -- seating / admin
    (
        re.compile(
            rf"^The player {_P} (?P<kind>joined the game with a stack of|"
            r"quits the game with a stack of|stand up with the stack of|"
            rf"sit back with the stack of) (?P<stack>{_AMT})\.$"
        ),
        lambda m, o, r: E.SeatChange(
            ord=o,
            raw=r,
            player=_player(m),
            kind=_SEAT_KINDS[m.group("kind")],
            stack=_chips(m.group("stack")),
        ),
    ),
    (
        re.compile(rf"^The player {_P} requested a seat\.$"),
        lambda m, o, r: E.SeatChange(
            ord=o, raw=r, player=_player(m), kind="requested_seat", stack=None
        ),
    ),
    (
        re.compile(
            rf"^The admin approved the player {_P} participation with a stack of (?P<stack>{_AMT})\.$"
        ),
        lambda m, o, r: E.SeatChange(
            ord=o, raw=r, player=_player(m), kind="admin_approved", stack=_chips(m.group("stack"))
        ),
    ),
    (
        re.compile(
            rf"^The admin updated the player {_P} stack from (?P<a>{_AMT}) to (?P<b>{_AMT})\.$"
        ),
        lambda m, o, r: E.AdminStackChange(
            ord=o, raw=r, player=_player(m), from_stack=_chips(m.group("a")), to_stack=_chips(m.group("b"))
        ),
    ),
    (
        re.compile(
            r"^The game's (?P<which>small blind|big blind|ante) was changed "
            rf"from (?P<a>{_AMT}) to (?P<b>{_AMT})\.$"
        ),
        lambda m, o, r: E.BlindChange(
            ord=o,
            raw=r,
            which={"small blind": "sb", "big blind": "bb", "ante": "ante"}[m.group("which")],
            from_amount=_chips(m.group("a")),
            to_amount=_chips(m.group("b")),
        ),
    ),
    # -- recognized noise (no effect on stats, but explicitly accounted for)
    (
        re.compile(r"^Undealt cards(?: \(second (?:run|board)\))?:.*$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="rabbit_hunt"),
    ),
    (
        re.compile(rf"^{_P} chooses to\s+(?:not\s+)?run it twice\.$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="rit_choice"),
    ),
    (
        re.compile(r"^All players in hand choose to run it twice\.$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="rit_agreed"),
    ),
    (
        re.compile(r"^Remaining players decide whether to run it twice\.$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="rit_prompt"),
    ),
    (
        re.compile(r"^WARNING: the admin queued the stack change.*$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="admin_queued_stack"),
    ),
    (
        re.compile(r"^Game Config Changes(\n.*)*$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="config_change"),
    ),
    (
        re.compile(r"^Dead Small Blind$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="dead_small_blind"),
    ),
    (
        re.compile(r"^Some players choose to not run it twice\.$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="rit_declined"),
    ),
    # A table whose settings force run-it-twice announces it instead of asking.
    # PokerNow's own text drops the "to"; accept it either way.
    (
        re.compile(r"^The board will run it twice due (?:to )?the game settings\.$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="rit_forced"),
    ),
    # -- table administration. None of it moves chips inside a hand: a rebuy shows
    # -- up as a larger stack on the next `Player stacks:` line, and net is
    # -- collected-minus-contributed per hand, so none of these touch a stat.
    (
        re.compile(rf"^The admin {_P} (?:enqueued|canceled) the game stop on next hand\.$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="admin_game_stop"),
    ),
    (
        re.compile(rf"^The admin {_P} enqueued the removal of the player {_P2}\.$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="admin_remove_player"),
    ),
    (
        re.compile(rf"^The admin {_P} rejected the seat request from the player {_P2}\.$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="admin_reject_seat"),
    ),
    (
        # Takes effect next hand, and that hand's `Player stacks:` line is the roster.
        re.compile(rf"^The admin {_P} forced the player {_P2} to away mode in the next hand\.$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="admin_force_away"),
    ),
    (
        re.compile(rf"^The player {_P} passed the room ownership to {_P2}\.$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="ownership_passed"),
    ),
    (
        # A player who logs in mid-session keeps their seat under their account's ID.
        # Lines after it carry the new ID; nothing here links the two identities.
        re.compile(
            rf"^The player {_P} changed the ID from {PN_ID} to {PN_ID} "
            r"because [^\n]+\.$"
        ),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="id_changed"),
    ),
    (
        re.compile(rf"^The player {_P} canceled the seat request\.$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="seat_request_canceled"),
    ),
    (
        re.compile(rf"^The player {_P} requested a rebuy of {_AMT}\.$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="rebuy_requested"),
    ),
    (
        re.compile(rf"^The player {_P} rebought\. New stack {_AMT}\.$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="rebuy_completed"),
    ),
    (
        re.compile(r"^Asking to busted players the rebuy decision\.$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="rebuy_prompt"),
    ),
    (
        re.compile(
            r"^Waiting for the game owner to approve or reject pending rebuy requests\.$"
        ),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="rebuy_waiting"),
    ),
]


def classify(entry: str, ord_: int, *, cents: bool = False) -> E.Event:
    """Turn one raw log entry into a typed event.

    Returns `Unknown` rather than raising, so one unfamiliar line cannot abort an
    import of 10,000 hands.

    `cents`: the table was in Cents Mode when this line was logged, so even an
    amount printed without a decimal point is in hundredths (see `_chips`). The
    parser knows the mode; a line read on its own does not need it unless it
    carries an amount.
    """
    if not cents:
        return _classify(entry, ord_)
    token = _CENTS.set(True)
    try:
        return _classify(entry, ord_)
    finally:
        _CENTS.reset(token)


def _classify(entry: str, ord_: int) -> E.Event:
    text = entry.strip()
    for pattern, build in RULES:
        m = pattern.match(text)
        if m:
            try:
                return build(m, ord_, entry)
            except ValueError as exc:
                return E.Unknown(ord=ord_, raw=entry, reason=str(exc))
    return E.Unknown(ord=ord_, raw=entry)
