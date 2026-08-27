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

from . import events as E
from .tokens import PlayerRef, parse_cards, split_player_token

_P = r'"(?P<p>.+)"'


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


_SEAT_RE = re.compile(r'#(\d+) "(.+?)" \((\d+)\)')


def _player_stacks(m: re.Match, ord_: int, raw: str) -> E.Event:
    body = m.group("body")
    found = _SEAT_RE.findall(body)
    # Sanity: one seat entry per '|'-separated field. A name containing '" (' would
    # desync this, so we verify rather than assume.
    if len(found) != body.count("|") + 1:
        return E.Unknown(ord=ord_, raw=raw, reason="player-stacks roster desync")
    seats = tuple(
        (int(seat), split_player_token(tok), int(stack)) for seat, tok, stack in found
    )
    return E.PlayerStacks(ord=ord_, raw=raw, seats=seats)


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
        re.compile(
            rf"^{_P} posts a (?P<kind>small blind|big blind|ante|missed big blind|"
            r"missing small blind|straddle) of (?P<amt>\d+)$"
        ),
        lambda m, o, r: E.Post(
            ord=o,
            raw=r,
            player=_player(m),
            kind=_POST_KINDS[m.group("kind")],
            amount=int(m.group("amt")),
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
        re.compile(rf"^{_P} (?P<kind>calls|bets) (?P<amt>\d+)(?P<allin> and go all in)?$"),
        lambda m, o, r: E.Action(
            ord=o,
            raw=r,
            player=_player(m),
            kind={"calls": "call", "bets": "bet"}[m.group("kind")],
            amount_to=int(m.group("amt")),
            all_in=bool(m.group("allin")),
        ),
    ),
    (
        re.compile(rf"^{_P} raises to (?P<amt>\d+)(?P<allin> and go all in)?$"),
        lambda m, o, r: E.Action(
            ord=o,
            raw=r,
            player=_player(m),
            kind="raise",
            amount_to=int(m.group("amt")),
            all_in=bool(m.group("allin")),
        ),
    ),
    # -- board
    (
        re.compile(
            r"^(?P<street>Flop|Turn|River)(?P<run> \(second run\))?:\s*(?P<rest>.+)$"
        ),
        lambda m, o, r: E.StreetDealt(
            ord=o,
            raw=r,
            street=_STREETS[m.group("street")],
            run=1 if m.group("run") else 0,
            board=tuple(parse_cards(m.group("rest"))),
        ),
    ),
    # -- pot resolution
    (
        re.compile(rf"^Uncalled bet of (?P<amt>\d+) returned to {_P}$"),
        lambda m, o, r: E.UncalledReturn(
            ord=o, raw=r, player=_player(m), amount=int(m.group("amt"))
        ),
    ),
    (
        re.compile(rf"^{_P} collected (?P<amt>\d+) from pot(?: with (?P<rank>.+))?$"),
        lambda m, o, r: E.Collected(
            ord=o,
            raw=r,
            player=_player(m),
            amount=int(m.group("amt")),
            ranking=m.group("rank"),
        ),
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
            r"sit back with the stack of) (?P<stack>\d+)\.$"
        ),
        lambda m, o, r: E.SeatChange(
            ord=o,
            raw=r,
            player=_player(m),
            kind=_SEAT_KINDS[m.group("kind")],
            stack=int(m.group("stack")),
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
            rf"^The admin approved the player {_P} participation with a stack of (?P<stack>\d+)\.$"
        ),
        lambda m, o, r: E.SeatChange(
            ord=o, raw=r, player=_player(m), kind="admin_approved", stack=int(m.group("stack"))
        ),
    ),
    (
        re.compile(
            rf"^The admin updated the player {_P} stack from (?P<a>\d+) to (?P<b>\d+)\.$"
        ),
        lambda m, o, r: E.AdminStackChange(
            ord=o, raw=r, player=_player(m), from_stack=int(m.group("a")), to_stack=int(m.group("b"))
        ),
    ),
    (
        re.compile(
            r"^The game's (?P<which>small blind|big blind|ante) was changed "
            r"from (?P<a>\d+) to (?P<b>\d+)\.$"
        ),
        lambda m, o, r: E.BlindChange(
            ord=o,
            raw=r,
            which={"small blind": "sb", "big blind": "bb", "ante": "ante"}[m.group("which")],
            from_amount=int(m.group("a")),
            to_amount=int(m.group("b")),
        ),
    ),
    # -- recognized noise (no effect on stats, but explicitly accounted for)
    (
        re.compile(r"^Undealt cards:.*$"),
        lambda m, o, r: E.Noise(ord=o, raw=r, kind="rabbit_hunt"),
    ),
    (
        re.compile(rf"^{_P} chooses to\s+run it twice\.$"),
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
]


def classify(entry: str, ord_: int) -> E.Event:
    """Turn one raw log entry into a typed event.

    Returns `Unknown` rather than raising, so one unfamiliar line cannot abort an
    import of 10,000 hands.
    """
    text = entry.strip()
    for pattern, build in RULES:
        m = pattern.match(text)
        if m:
            try:
                return build(m, ord_, entry)
            except ValueError as exc:
                return E.Unknown(ord=ord_, raw=entry, reason=str(exc))
    return E.Unknown(ord=ord_, raw=entry)
