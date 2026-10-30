"""Hand filters -- "show me only the hands where X happened".

This is the Holdem-Manager capability: pick a spot, then look at what a player
does inside it. `3bet,position=BTN` means *of the hands where this player 3-bet
from the button*, what are their stats?

Filters are predicates over the derived `Facts` row for one (hand, player), which
is why they compose freely and why adding a new one is three lines. That only
works because actions are stored raw with street and sequence -- a pre-aggregated
schema cannot answer "hands that reached this point" at all.

A *line* is just a longer filter. "Opened 4bb+ in a single-raised pot, c-bet the
flop half pot, c-bet the turn, overbet the river" is::

    opener,open_bb>=4,srp,cbet_flop=medium,cbet_turn,bet_river=overbet

The pot and the opposition are terms too: `pot>=500` keeps the hands whose final
pot reached 500 chips, and `vs=henry` the hands played against henry.
`jam_river` keeps river all-ins, the spot for reading bluffs against value, and
`called_jam` the other side of it. `line_flop=xr` is a flop check-raise, and
`made_flop>=set,line_flop=x*` the sets that checked the flop -- a slowplay.
`action_flop=*Br*` is the whole street, everyone's actions: they bet and someone
raised them, their own letters in capitals. `since:2026-10-20` and `last:7d` keep
the recent hands, so any spot can be read for how they play lately.

A comma is and, `|` or, and `|` binds tighter: `3bet_pot|4bet_pot` is either pot,
`position=BTN|CO` either seat, and `(srp,position=BTN)|3bet_pot` groups with
brackets (SPEC.md, "Filter language").
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache

from ..logfmt.parser import position_name
from .cards import (
    ALL_CLASSES,
    MADE_GROUPS,
    MADE_LEAVES,
    RANK_VALUE,
    TEXTURE_TAGS,
    board_texture,
    hand_class,
    hand_pct,
    leaf_of,
    made_at,
    street_of_board,
)
from .derive import ANSWERS, LINE_CODE, SIZE_BUCKETS, TEMPO_BUCKETS, Facts
from .strength import TIERS, hs_of, tier_of
from .tilt import BIG_LOSS_BB, WINDOW, post_loss

Predicate = Callable[[Facts], bool]
#: What `tag=` reads its hands from: one tag as written -> the (game_id,
#: hand_number) of every hand carrying it. A tag is your data, not the log's, so
#: the caller with a connection passes one (`db.userdata.tag_resolver`).
TagResolver = Callable[[str], Collection[tuple[str, int]]]

STREETS = ("flop", "turn", "river")

#: name -> predicate. Flags only; parameterised filters are handled in `parse_filter`.
FLAGS: dict[str, Predicate] = {
    "vpip": lambda f: f.vpip,
    "no_vpip": lambda f: f.vpip_opp and not f.vpip,
    "acted_preflop": lambda f: f.vpip_opp,
    "pfr": lambda f: f.pfr,
    "3bet": lambda f: f.three_bet,
    "3bet_opp": lambda f: f.three_bet_opp,
    "faced_3bet": lambda f: f.fold_to_3bet_opp,
    "folded_to_3bet": lambda f: f.fold_to_3bet,
    "saw_flop": lambda f: f.saw_flop,
    "cbet_flop": lambda f: bool(f.cbet.get("flop")),
    "cbet_turn": lambda f: bool(f.cbet.get("turn")),
    "cbet_river": lambda f: bool(f.cbet.get("river")),
    "wtsd": lambda f: f.wtsd,
    "won_sd": lambda f: f.wsd,
    "won": lambda f: f.net > 0,
    "lost": lambda f: f.net < 0,
    "dead_button": lambda f: f.dead_button,
    # --- line terms
    "opener": lambda f: f.opener,
    "pfa": lambda f: f.pfa,
    "limped": lambda f: f.pot_level == 1,  # never a bomb pot, PLO or Double Board: those are 0
    "srp": lambda f: f.pot_level == 2,
    "3bet_pot": lambda f: f.pot_level == 3,
    "4bet_pot": lambda f: f.pot_level >= 4,
    "bet_flop": lambda f: "flop" in f.bet_pot,
    "bet_turn": lambda f: "turn" in f.bet_pot,
    "bet_river": lambda f: "river" in f.bet_pot,
    "cards_known": lambda f: f.hole_cards is not None,
    # --- preflop decision points (SPEC.md, "Decision points"). Each is a bet
    # level the player acted at, or what they did there. `faced_3bet` above stays
    # the opener's alone because Fold to 3-Bet is defined on it; `faced_3bet_any`
    # is the cold-caller's and the squeezed player's as well. `limp` is the action
    # where `limped` is the pot type.
    "unopened": lambda f: 1 in f.pf_faced,
    "limp": lambda f: f.pf_faced.get(1) == "call",
    "faced_open": lambda f: 2 in f.pf_faced,
    "called_open": lambda f: f.pf_faced.get(2) == "call",
    "faced_3bet_any": lambda f: 3 in f.pf_faced,
    "4bet": lambda f: f.pf_faced.get(3) == "raise",
    "faced_4bet": lambda f: 4 in f.pf_faced,
    "5bet": lambda f: f.pf_faced.get(4) == "raise",
    "faced_5bet": lambda f: 5 in f.pf_faced,
    # --- steals, squeezes and cold calls (SPEC.md, "Steals and blind defense",
    # "Squeeze and cold-call"). Each reads the `Facts` property of the same name.
    "first_in": lambda f: f.first_in,
    "steal_opp": lambda f: f.steal_opp,
    "steal": lambda f: f.steal,
    "fold_to_steal_opp": lambda f: f.fold_to_steal_opp,
    "fold_to_steal": lambda f: f.fold_to_steal,
    "3bet_vs_steal": lambda f: f.three_bet_vs_steal,
    "squeeze_opp": lambda f: f.squeeze_opp,
    "squeeze": lambda f: f.squeeze,
    "cold_call_opp": lambda f: f.cold_call_opp,
    "cold_call": lambda f: f.cold_call,
    "straddled": lambda f: f.straddled,
    "won_pot": lambda f: f.won_pot,
    # The 7-2 side bet, settled outside the pot.
    "bounty_won": lambda f: f.bounty > 0,
    "bounty_paid": lambda f: f.bounty < 0,
    # The Nit Game penalty, the same way.
    "nit_won": lambda f: f.nit > 0,
    "nit_paid": lambda f: f.nit < 0,
    # The game. PLO and Double Board are counted in the hand lists and Results
    # only (SPEC.md, "Money"), so `plo` and `double_board` find nothing elsewhere.
    "holdem": lambda f: f.holdem,
    "plo": lambda f: not f.holdem,
    "bomb_pot": lambda f: f.bomb_pot,
    "double_board": lambda f: f.double_board,
}

# Responses to a c-bet, and leads into the previous street's aggressor, per street.
for _s in STREETS:
    FLAGS[f"cbet_{_s}_opp"] = lambda f, s=_s: bool(f.cbet_opp.get(s))
    FLAGS[f"faced_cbet_{_s}"] = lambda f, s=_s: bool(f.fold_to_cbet_opp.get(s))
    FLAGS[f"folded_to_cbet_{_s}"] = lambda f, s=_s: bool(f.fold_to_cbet.get(s))
    FLAGS[f"called_cbet_{_s}"] = lambda f, s=_s: bool(
        f.fold_to_cbet_opp.get(s) and not f.fold_to_cbet.get(s) and not f.raise_cbet.get(s)
    )
    FLAGS[f"raised_cbet_{_s}"] = lambda f, s=_s: bool(f.raise_cbet.get(s))
    FLAGS[f"donk_{_s}"] = lambda f, s=_s: bool(f.donk.get(s))
    FLAGS[f"donk_{_s}_opp"] = lambda f, s=_s: bool(f.donk_opp.get(s))
    # Facing any bet or raise on the street, whoever made it, and what they did;
    # the street's last aggressor; and closing a street that checked through.
    FLAGS[f"faced_bet_{_s}"] = lambda f, s=_s: bool(f.faced_bet.get(s))
    FLAGS[f"folded_to_bet_{_s}"] = lambda f, s=_s: bool(f.folded_to_bet.get(s))
    FLAGS[f"called_bet_{_s}"] = lambda f, s=_s: bool(f.called_bet.get(s))
    FLAGS[f"raised_bet_{_s}"] = lambda f, s=_s: bool(f.raised_bet.get(s))
    FLAGS[f"aggressor_{_s}"] = lambda f, s=_s: bool(f.aggressor.get(s))
    FLAGS[f"check_back_{_s}"] = lambda f, s=_s: bool(f.check_back.get(s))
    # Checked, then raised a bet behind them; and checked, then had to act again.
    FLAGS[f"check_raise_{_s}"] = lambda f, s=_s: bool(f.check_raise.get(s))
    FLAGS[f"check_raise_{_s}_opp"] = lambda f, s=_s: bool(f.check_raise_opp.get(s))
    # The other side of it: they bet, and a player who had checked to them raised.
    FLAGS[f"faced_check_raise_{_s}"] = lambda f, s=_s: bool(f.faced_check_raise.get(s))
FLAGS["check_back"] = lambda f: any(f.check_back.values())
FLAGS["check_raise"] = lambda f: any(f.check_raise.values())
FLAGS["faced_check_raise"] = lambda f: any(f.faced_check_raise.values())

# After a missed c-bet (SPEC.md, "Delayed c-bet, probe and float"): term name ->
# (Facts field, streets). Delayed c-bets and probes need a postflop street before
# them; a float can follow the preflop raise. Each also has an any-street form.
_MISSED_CBET = {
    "delayed_cbet": ("delayed_cbet", STREETS[1:]),
    "delayed_cbet_opp": ("delayed_cbet_opp", STREETS[1:]),
    "probe": ("probe", STREETS[1:]),
    "probe_opp": ("probe_opp", STREETS[1:]),
    "faced_probe": ("fold_to_probe_opp", STREETS[1:]),
    "folded_to_probe": ("fold_to_probe", STREETS[1:]),
    "float": ("float_bet", STREETS),
    "float_opp": ("float_opp", STREETS),
}
for _name, (_attr, _streets) in _MISSED_CBET.items():
    FLAGS[_name] = lambda f, a=_attr: any(getattr(f, a).values())
    for _s in _streets:
        # `probe_turn_opp`, like `cbet_turn_opp`: the street before the suffix.
        _term = f"{_name[:-4]}_{_s}_opp" if _name.endswith("_opp") else f"{_name}_{_s}"
        FLAGS[_term] = lambda f, a=_attr, s=_s: bool(getattr(f, a).get(s))

# Jams: all-in bets and raises, and the players who faced and called them. Preflop
# counts here, unlike the postflop terms above.
for _name in ("jam", "faced_jam", "called_jam"):
    FLAGS[_name] = lambda f, n=_name: any(getattr(f, n).values())
    for _s in ("preflop", *STREETS):
        FLAGS[f"{_name}_{_s}"] = lambda f, n=_name, s=_s: bool(getattr(f, n).get(s))

#: name -> getter for the comparison terms (`open_bb>=4`, `bet_river>=1`).
#: A getter returning None means "not applicable on this hand", and every
#: comparison against None is False -- a hand with no open has no open size.
NUMERIC: dict[str, Callable[[Facts], float | None]] = {
    "players": lambda f: f.n_dealt_in,
    "open_bb": lambda f: f.open_bb,
    "raise_bb": lambda f: f.pf_raise_bb,
    "bet_flop": lambda f: f.bet_pot.get("flop"),
    "bet_turn": lambda f: f.bet_pot.get("turn"),
    "bet_river": lambda f: f.bet_pot.get("river"),
    #: The final pot, in chips and in this hand's big blinds. `pot_bb` sits after
    #: `pot` on purpose: the prefix match tries `pot` first, finds no operator
    #: behind `_bb`, and moves on.
    "pot": lambda f: f.pot,
    "pot_bb": lambda f: f.pot / f.bb_size if f.bb_size else None,
    #: Where the holding sits in `cards.HAND_RANKING`: 0.6 is AA, 100 is 32o, so
    #: `hand_pct>=60` is a bottom-40% hand. None when the cards were not shown.
    "hand_pct": lambda f: hand_pct(hand_class(f.hole_cards)) if f.hole_cards else None,
    #: After `players` on purpose, as `pot_bb` after `pot`.
    "players_flop": lambda f: f.players_flop,
    #: Stack depth: effective stack in bb, and the flop's stack-to-pot ratio.
    "eff_bb": lambda f: f.eff_bb,
    "spr_flop": lambda f: f.spr_flop,
    #: Calls of the open in front of their first decision facing it.
    "callers_before": lambda f: f.callers_before,
}
for _s in STREETS:
    #: The bet or raise they first faced on the street, and their own first raise,
    #: as pot fractions: `faced_bet_river>=1` is facing a pot-sized bet or bigger.
    NUMERIC[f"faced_bet_{_s}"] = lambda f, s=_s: f.faced_bet_pot.get(s)
    NUMERIC[f"raised_bet_{_s}"] = lambda f, s=_s: f.raise_pot.get(s)

# --- session dynamics (SPEC.md, "Session dynamics"): context across a player's
# hands in one game, filled by `tilt.annotate` before any filter runs. A row
# nobody annotated has no session context, and every one of these is False on it.
FLAGS["post_loss"] = post_loss
NUMERIC["hands_since_loss"] = lambda f: f.hands_since_loss
NUMERIC["session_hand"] = lambda f: f.session_hand or None
NUMERIC["session_hour"] = lambda f: f.session_hour

#: name -> getter for the size-bucket terms (`cbet_flop=medium`, `bet_river=overbet`,
#: `faced_cbet_turn=large`). The buckets are SIZE_BUCKETS; see SPEC.md.
SIZED: dict[str, Callable[[Facts], str | None]] = {}
for _s in STREETS:
    SIZED[f"cbet_{_s}"] = lambda f, s=_s: f.bet_size.get(s) if f.cbet.get(s) else None
    SIZED[f"bet_{_s}"] = lambda f, s=_s: f.bet_size.get(s)
    SIZED[f"faced_cbet_{_s}"] = lambda f, s=_s: f.faced_cbet_size.get(s)
    SIZED[f"faced_bet_{_s}"] = lambda f, s=_s: f.faced_bet_size.get(s)
    SIZED[f"raised_bet_{_s}"] = lambda f, s=_s: f.raise_size.get(s)

#: name -> getter for the answer terms (`bet_answer_river=fold`): what their first
#: bet on the street got back, one of ANSWERS, or None when they made no bet there
#: or nobody answered it.
ANSWERED: dict[str, Callable[[Facts], str | None]] = {
    f"bet_answer_{_s}": (lambda f, s=_s: f.bet_answer.get(s)) for _s in STREETS
}
#: And what they did when their bet was check-raised (`faced_check_raise_flop=fold`).
for _s in STREETS:
    ANSWERED[f"faced_check_raise_{_s}"] = lambda f, s=_s: f.faced_check_raise_action.get(s)

#: The action-line terms: `line_flop=xc` for one street, `line=xc/x/b*` for the hand.
LINE_TERMS = ("line", *(f"line_{s}" for s in STREETS))

#: The whole-street terms, read from `seq` and case-sensitive: `action_flop=xBr*`
#: (checked to, bet, check-raised), `action=*Bc//` for the hand.
ACTION_TERMS = ("action", *(f"action_{s}" for s in STREETS))

#: The made-hand terms: `made_flop>=set` on the board as it stood on that street,
#: `made=flush` on the final board.
MADE_TERMS = ("made", *(f"made_{s}" for s in STREETS))

#: The strength-tier terms: `tier_river=strong`, `tier_flop>=strong` -- what the
#: hand was worth on the board as it stood (`strength.tier_at`), strongest first.
TIER_TERMS = tuple(f"tier_{s}" for s in STREETS)

#: What a player can do at a preflop decision point.
DECISIONS = ("fold", "check", "call", "raise")

#: name -> getter for the decision terms (`faced_open=call`, `faced_3bet=fold`):
#: the decision this player made at that point, or None when they never reached
#: it. `faced_3bet` is the opener's decision only, exactly like the flag.
DECIDED: dict[str, Callable[[Facts], str | None]] = {
    "unopened": lambda f: f.pf_faced.get(1),
    "faced_open": lambda f: f.pf_faced.get(2),
    "faced_3bet": lambda f: f.pf_faced.get(3) if f.fold_to_3bet_opp else None,
    "faced_3bet_any": lambda f: f.pf_faced.get(3),
    "faced_4bet": lambda f: f.pf_faced.get(4),
    "faced_5bet": lambda f: f.pf_faced.get(5),
}

#: Board texture terms: `flop=ace_high`, `turn=paired`, `river!=flush_possible`.
#: `board` is the whole board as dealt (three to five cards). Each street needs
#: that many cards on the board; a hand that ended earlier never matches.
_BOARD_CARDS = {"flop": 3, "turn": 4, "river": 5, "board": 3}
#: Cached by board: a texture term re-reads every row's board, and `/counts` asks
#: a dozen texture chips over the same rows at once. `Facts.board` is a tuple.
_texture_of = lru_cache(maxsize=1 << 16)(board_texture)


def _texture_term(term: str) -> Predicate | None:
    for street, need in _BOARD_CARDS.items():
        for op in ("!=", "="):
            prefix = f"{street}{op}"
            if not term.startswith(prefix):
                continue
            tag = term[len(prefix):]
            if tag not in TEXTURE_TAGS:
                raise ValueError(
                    f"unknown board texture {tag!r}. Known: {', '.join(TEXTURE_TAGS)}"
                )
            want = op == "="

            def pred(f: Facts, street=street, need=need, tag=tag, want=want) -> bool:
                if len(f.board) < need:
                    return False
                cards = f.board if street == "board" else f.board[:need]
                return (tag in _texture_of(tuple(cards))) == want

            return pred
    return None


def _decision_term(term: str) -> Predicate | None:
    name, sep, decision = term.partition("=")
    if not sep or name not in DECIDED:
        return None
    if decision not in DECISIONS:
        raise ValueError(f"unknown decision {decision!r}. Known: {', '.join(DECISIONS)}")
    return lambda f, g=DECIDED[name], d=decision: g(f) == d


def _answer_term(term: str) -> Predicate | None:
    name, sep, answer = term.partition("=")
    if not sep or name not in ANSWERED:
        return None
    if answer not in ANSWERS:
        raise ValueError(f"unknown answer {answer!r}. Known: {', '.join(ANSWERS)}")
    return lambda f, g=ANSWERED[name], want=answer: g(f) == want


def _street_matcher(pattern: str, kind: str) -> Callable[[str | None], bool]:
    """One street's pattern as a test on that street's `line` or `seq` (None: no key).

    The action letters are themselves, ``*`` any run of them, none included, and
    ``?`` exactly one; the whole pattern must match. ``x*`` is every line that
    starts with a check, ``*r`` every line that ends on a raise. ``-`` alone is
    "no key": did not act, for a line; not in the hand on that street, for an
    action sequence. Any other pattern needs the key, so ``*`` is "acted at all"
    for a line and "was in it" for a sequence.

    A line pattern forgives case, since a line is only ever one player's letters.
    An action pattern does not: capitals are the player's own actions and lower
    case everyone else's, so ``xB`` and ``Xb`` are opposite seats of one street.
    """
    pattern = pattern.strip()
    codes = "".join(LINE_CODE.values())
    if kind == "line":
        pattern = pattern.lower()
    else:
        codes += codes.upper()
    if pattern == "-":
        return lambda value: value is None
    if not pattern or any(ch not in codes + "*?" for ch in pattern):
        hint = (
            "x b c r f" if kind == "line"
            else "X B C R F for their own actions, x b c r f for everyone else's"
        )
        missing = "did not act" if kind == "line" else "not in the hand on that street"
        start = "xc*" if kind == "line" else "xB*"
        raise ValueError(
            f"bad {kind} pattern {pattern!r}: use {hint}. Two wildcards: * stands for any "
            f"number of actions, none included ({start} is {start[:-1]} then anything), and ? "
            f"for exactly one ({start[:-1]}? is {start[:-1]} then one more action). - on its own "
            f"means {missing}"
        )
    rx = re.compile("".join({"*": ".*", "?": "."}.get(ch, ch) for ch in pattern))
    return lambda value: value is not None and rx.fullmatch(value) is not None


def _street_term(term: str, kind: str) -> Predicate | None:
    """`line_flop=xc` / `line_flop!=x*`, and `line=xc/x/b*` across the streets; the
    same for `action_` and `action=`, read from `seq` instead of `line`.

    The hand form takes up to three patterns, flop/turn/river; an empty one, or
    one left off the end, matches anything, no key included. `!=` is exactly the
    opposite of `=`, so `line_river!=b*` keeps the hands that never reached the
    river as well -- `-` is a value a line can have.
    """
    names, attr = (LINE_TERMS, "line") if kind == "line" else (ACTION_TERMS, "seq")
    for name in names:
        for op in ("!=", "="):
            prefix = f"{name}{op}"
            if not term.startswith(prefix):
                continue
            value = term[len(prefix):]
            if name == kind:
                parts = value.split("/")
                if len(parts) > len(STREETS) or not any(p.strip() for p in parts):
                    example = "xc//b*" if kind == "line" else "*Bc//xB*"
                    raise ValueError(
                        f"bad {kind} {value!r}: give up to three street patterns as "
                        f"flop/turn/river, e.g. {example}"
                    )
                tests = [(s, _street_matcher(p, kind)) for s, p in zip(STREETS, parts) if p.strip()]
            else:
                tests = [(name.removeprefix(f"{kind}_"), _street_matcher(value, kind))]
            want = op == "="

            def pred(f: Facts, tests=tests, want=want, attr=attr) -> bool:
                got = getattr(f, attr)
                return all(t(got.get(s)) for s, t in tests) == want

            return pred
    return None


#: The order streets are dealt in, for "folded before this street".
_STREET_ORDER = {"preflop": 0, **{s: i + 1 for i, s in enumerate(STREETS)}}
_LEAF_RANK = {leaf: i for i, leaf in enumerate(MADE_LEAVES)}


def _made_names(name: str) -> tuple[str, ...]:
    """The leaves a made-hand name covers. A leaf wins over a class of the same
    name, so ``trips`` is one hole card to a paired board and never a set."""
    if name in _LEAF_RANK:
        return (name,)
    if name in MADE_GROUPS:
        return MADE_GROUPS[name]
    raise ValueError(
        f"unknown made hand {name!r}. Known: {', '.join(MADE_LEAVES)}, or a group: "
        f"{', '.join(g for g in MADE_GROUPS if g not in _LEAF_RANK)}"
    )


def _made_leaf(f: Facts, street: str | None) -> str | None:
    """Their made hand on `street`'s board as a MADE_LEAVES leaf, or None when it
    cannot be said: cards unknown, the board never got there, or they had folded
    on an earlier street. `street` None is the final board, read the way the
    made-hand range view reads it: every known holding, folded or not."""
    if f.hole_cards is None:
        return None
    if street is None:
        street = street_of_board(f.board)
    elif f.fold_street is not None and _STREET_ORDER[f.fold_street] < _STREET_ORDER[street]:
        return None
    mh = made_at(f.hole_cards, f.board, street)
    return None if mh is None else leaf_of(mh)


def _made_test(op: str, names: list[str]) -> Callable[[int], bool]:
    """The test on a leaf's rank that `op` and `names` make. `names` is one name,
    or several for `=` and `!=`, each a leaf or a group's span."""
    spans = [[_LEAF_RANK[x] for x in _made_names(n)] for n in names]
    members = frozenset(r for s in spans for r in range(min(s), max(s) + 1))
    lo, hi = min(spans[0]), max(spans[0])  # the strongest member, and the weakest
    return {
        "=": lambda r: r in members,
        "!=": lambda r: r not in members,
        ">=": lambda r: r <= hi,
        ">": lambda r: r < lo,
        "<=": lambda r: r >= lo,
        "<": lambda r: r > hi,
    }[op]


def _made_term(term: str) -> Predicate | None:
    """`made_flop=set`, `made_turn>=two_pair`, `made_river<pair`, `made=flush`.

    Comparisons read MADE_LEAVES, strongest first. Against a group they take its
    span: `>=pair` is a pair or better, `<pair` weaker than every pair -- the
    draws and high card. `!=` and every comparison need a known hand on that
    street, like every claim about a holding.

    `=` and `!=` take several names joined by `|`, any of them:
    `made_flop=set|flush|full_house` is the hands that flopped one of the three,
    which no single span says. A comparison takes one name, since "at least set
    or flush" is not a question.
    """
    for name in MADE_TERMS:
        if not term.startswith(name):
            continue
        rest = term[len(name):]
        op = next((o for o in ("!=", ">=", "<=", "=", ">", "<") if rest.startswith(o)), None)
        if op is None:
            continue
        names = [n.strip().lower() for n in rest[len(op):].split("|")]
        if len(names) > 1 and not all(names):
            raise ValueError(f"{term!r}: a | needs a made hand on each side (made_flop=set|flush)")
        if len(names) > 1 and op not in ("=", "!="):
            raise ValueError(
                f"{term!r}: only = and != take several made hands (made_flop=set|flush); "
                f"{op} compares against one"
            )
        test = _made_test(op, names)
        street = None if name == "made" else name.removeprefix("made_")

        def pred(f: Facts, street=street, test=test) -> bool:
            leaf = _made_leaf(f, street)
            return leaf is not None and test(_LEAF_RANK[leaf])

        return pred
    return None


def _tier_term(term: str) -> Predicate | None:
    """`tier_river=strong`, `tier_flop>=strong`, `tier_turn=draw|air`.

    The tiers read `strength.TIERS`, strongest first, so `>=strong` is strong or
    nutted. Like a `made_<street>` term, it needs the cards known, the board on
    that street, and no fold before it; `!=` included.
    """
    for name in TIER_TERMS:
        if not term.startswith(name):
            continue
        rest = term[len(name):]
        op = next((o for o in ("!=", ">=", "<=", "=", ">", "<") if rest.startswith(o)), None)
        if op is None:
            continue
        names = [n.strip().lower() for n in rest[len(op):].split("|")]
        if len(names) > 1 and op not in ("=", "!="):
            raise ValueError(f"{term!r}: only = and != take several tiers (tier_river=strong|nutted)")
        bad = [n for n in names if n not in TIERS]
        if bad:
            raise ValueError(f"unknown tier {bad[0]!r}. Known: {', '.join(TIERS)}")
        ranks = {TIERS.index(n) for n in names}
        r0 = TIERS.index(names[0])
        test = {
            "=": lambda r, ranks=ranks: r in ranks,
            "!=": lambda r, ranks=ranks: r not in ranks,
            ">=": lambda r, r0=r0: r <= r0,
            ">": lambda r, r0=r0: r < r0,
            "<=": lambda r, r0=r0: r >= r0,
            "<": lambda r, r0=r0: r > r0,
        }[op]
        street = name.removeprefix("tier_")

        def pred(f: Facts, street=street, test=test) -> bool:
            tier = tier_of(f, street)
            return tier is not None and test(TIERS.index(tier))

        return pred
    return None


def _size_term(term: str) -> Predicate | None:
    name, sep, bucket = term.partition("=")
    if not sep or name not in SIZED:
        return None
    if bucket not in SIZE_BUCKETS:
        try:
            float(bucket)
        except ValueError:
            raise ValueError(
                f"unknown bet size {bucket!r}. Known: {', '.join(SIZE_BUCKETS)}"
            ) from None
        return None  # `bet_river=1` is a numeric comparison
    return lambda f, g=SIZED[name], b=bucket: g(f) == b


_OPS: dict[str, Callable[[float, float], bool]] = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "=": lambda a, b: a == b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
}


def _player_term(term: str, names: Mapping[str, str] | None) -> Predicate | None:
    """`vs=<name>` / `vs!=<name>`: the hand was played against that player.

    "Against" is `Facts.opponents` -- the players still in at this player's last
    action (SPEC.md, "Who the action was against"), the same list a hand row prints
    as `vs`. `names` maps pn_id -> display name; a raw pn_id matches as well, so a
    caller without the map can still name an identity. With the map, a name nobody
    goes by is rejected: an empty chart is the wrong answer to a typo.
    """
    for op in ("!=", "="):
        prefix = f"vs{op}"
        if not term.startswith(prefix):
            continue
        want = term[len(prefix):].strip().casefold()
        if not want:
            raise ValueError("vs= needs a player name")
        if names is not None and want not in {n.casefold() for n in names.values()} | {
            i.casefold() for i in names
        }:
            raise ValueError(f"unknown player {term[len(prefix):].strip()!r}")
        lookup = names or {}
        positive = op == "="

        def pred(f: Facts, want=want, lookup=lookup, positive=positive) -> bool:
            hit = any(
                want in (pid.casefold(), lookup.get(pid, pid).casefold()) for pid in f.opponents
            )
            return hit == positive

        return pred
    return None


def hand_classes(value: str) -> frozenset[str]:
    """The 169-classes a `hand=` value names: ``72o`` one class, ``72`` both suits,
    ``77`` the pair. Rank order and case are forgiven (``27o`` is ``72o``)."""
    text = value.strip()
    ranks, suffix = text[:2].upper(), text[2:].lower()
    if len(ranks) != 2 or any(r not in RANK_VALUE for r in ranks) or suffix not in ("", "s", "o"):
        raise ValueError(f"unknown hand {value!r}: use a class like AKs, T9o or 77, or two ranks like 72")
    hi, lo = sorted(ranks, key=lambda r: -RANK_VALUE[r])
    if hi == lo:
        if suffix:
            raise ValueError(f"unknown hand {value!r}: a pair is neither suited nor offsuit")
        return frozenset({hi + lo})
    labels = {f"{hi}{lo}{suffix}"} if suffix else {f"{hi}{lo}s", f"{hi}{lo}o"}
    assert labels <= set(ALL_CLASSES)
    return frozenset(labels)


def _hand_term(term: str) -> Predicate | None:
    """`hand=72o` / `hand=72` / `hand!=AKs`: the holding, where it was shown."""
    for op in ("!=", "="):
        prefix = f"hand{op}"
        if not term.startswith(prefix):
            continue
        wanted = hand_classes(term[len(prefix):])
        positive = op == "="

        def pred(f: Facts, wanted=wanted, positive=positive) -> bool:
            return f.hole_cards is not None and (hand_class(f.hole_cards) in wanted) == positive

        return pred
    return None


def _tag_term(term: str, tags: TagResolver | None) -> Predicate | None:
    """`tag=river-bluff` / `tag!=cooler`: a hand you tagged (SPEC.md, "Hand tags").

    The hand, not the seat: every player's row of a tagged hand matches. A tag no
    hand carries matches nothing, as an empty spot should; without a resolver
    there is nothing to read, and that raises.
    """
    for op in ("!=", "="):
        prefix = f"tag{op}"
        if not term.startswith(prefix):
            continue
        want = term[len(prefix):].strip()
        if not want:
            raise ValueError("tag= needs a tag")
        if tags is None:
            raise ValueError(f"{term!r}: hand tags are only read where the tracker's database is (the pages, /stats)")
        keys = frozenset(tags(want))
        positive = op == "="

        def pred(f: Facts, keys=keys, positive=positive) -> bool:
            return ((f.game_id, f.hand_number) in keys) == positive

        return pred
    return None


#: Today on this machine's calendar, which `last:` counts back from. A name of
#: its own so a test can move the clock.
_today = date.today
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DAYS = re.compile(r"(\d+)d", re.IGNORECASE)


def _day_start(day: date) -> str:
    """Local midnight at the start of `day`, written as the parser writes `hands.ts`
    (ISO-8601 UTC, to the millisecond, with the `Z`), so a hand compares as text,
    the way `pots.cutoff` does."""
    try:
        at = datetime.combine(day, time()).astimezone(UTC)
    except (OverflowError, OSError):  # Windows has no local time before 1970
        at = datetime.combine(day, time(), UTC)
    return f"{at.strftime('%Y-%m-%dT%H:%M:%S')}.000Z"


def _date_term(term: str) -> Predicate | None:
    """`since:2026-10-20`, `before:2026-10-20`, `last:7d` (SPEC.md, "Dates").

    Days are this machine's calendar days: `since:` keeps the hands from that
    local midnight on, `before:` the hands before it, and `last:7d` today and the
    six days before. A hand with no timestamp matches none of them.
    """
    key, sep, value = term.partition(":")
    if not sep or key not in ("since", "before", "last"):
        return None
    value = value.strip()
    if key == "last":
        m = _DAYS.fullmatch(value)
        if m is None or int(m.group(1)) < 1:
            raise ValueError(f"bad span {value!r} for last: use a number of days, e.g. last:7d")
        try:
            cut = _day_start(_today() - timedelta(days=int(m.group(1)) - 1))
        except OverflowError:  # back past the year 1
            raise ValueError(f"span {value!r} for last: reaches back past any date") from None
    else:
        try:
            if not _DATE.fullmatch(value):
                raise ValueError
            cut = _day_start(date.fromisoformat(value))
        except ValueError:
            raise ValueError(f"bad date {value!r} for {key}: use YYYY-MM-DD, e.g. {key}:2026-10-20") from None
    if key == "before":
        return lambda f, cut=cut: f.ts is not None and f.ts < cut
    return lambda f, cut=cut: f.ts is not None and f.ts >= cut


def date_key(expr: str | None) -> str | None:
    """What a cache keyed by filter text must add for `expr` to stay current:
    today's date when a `last:` term counts back from it, else None. `since:`
    and `before:` name their day and need nothing."""
    if not expr or not re.search(r"(^|[,|(\s])last:", expr):
        return None
    return _today().isoformat()


def _numeric_term(term: str) -> Predicate | None:
    for name, getter in NUMERIC.items():
        if not term.startswith(name):
            continue
        rest = term[len(name):]
        for op in (">=", "<=", "=", ">", "<"):  # two-char ops first
            if rest.startswith(op):
                try:
                    n = float(rest[len(op):])
                except ValueError:
                    return None
                cmp = _OPS[op]

                def pred(f: Facts, g=getter, n=n, cmp=cmp) -> bool:
                    v = g(f)
                    return v is not None and cmp(v, n)

                return pred
    return None


#: Every term a filter understands, grouped for the pages' `?` panel. Each entry is
#: (term as written, an example to drop into the box, what it keeps). `<street>`
#: stands for flop, turn or river. `test_filter_vocabulary_covers_every_term` holds
#: this to FLAGS, NUMERIC, SIZED, ANSWERED, LINE_TERMS, ACTION_TERMS, MADE_TERMS and TIER_TERMS,
#: so a new term cannot ship undocumented.
VOCABULARY: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    ("Preflop", (
        ("vpip", "vpip", "called, bet or raised preflop"),
        ("no_vpip", "no_vpip", "had a decision, put nothing in"),
        ("acted_preflop", "acted_preflop", "had a preflop decision"),
        ("pfr", "pfr", "raised preflop"),
        ("opener", "opener", "made the first raise"),
        ("pfa", "pfa", "made the last preflop raise"),
        ("3bet", "3bet", "re-raised an open"),
        ("3bet_opp", "3bet_opp", "could have 3-bet"),
        ("faced_3bet", "faced_3bet", "opened and got 3-bet"),
        ("folded_to_3bet", "folded_to_3bet", "opened, got 3-bet, folded"),
        ("open_bb>=N", "open_bb>=4", "the open raise, in bb"),
        ("raise_bb>=N", "raise_bb<=2.5", "their own raise-to, in bb"),
    )),
    ("Preflop decisions", (
        ("unopened", "unopened", "had a decision with no raise in front"),
        ("limp", "limp", "called in an unopened pot"),
        ("faced_open", "faced_open", "acted facing an open"),
        ("called_open", "called_open", "called an open"),
        ("faced_3bet_any", "faced_3bet_any", "faced a 3-bet, opener or not"),
        ("4bet", "4bet", "re-raised a 3-bet"),
        ("faced_4bet", "faced_4bet", "acted facing a 4-bet"),
        ("5bet", "5bet", "re-raised a 4-bet"),
        ("faced_5bet", "faced_5bet", "acted facing a 5-bet"),
        ("<spot>=DECISION", "faced_open=call", "what they did there: fold, check, call or raise"),
        ("first_in", "first_in", "everyone before them folded"),
        ("steal_opp", "steal_opp", "first in from CO, BTN or SB"),
        ("steal", "steal", "opened first in from CO, BTN or SB"),
        ("fold_to_steal_opp", "fold_to_steal_opp", "in a blind, facing a steal nobody called"),
        ("fold_to_steal", "fold_to_steal", "folded a blind to a steal"),
        ("3bet_vs_steal", "3bet_vs_steal", "3-bet a steal from a blind"),
        ("callers_before>=N", "callers_before>=1", "calls of the open before they acted on it"),
        ("squeeze_opp", "squeeze_opp", "faced an open and at least one caller"),
        ("squeeze", "squeeze", "3-bet an open and its callers"),
        ("cold_call_opp", "cold_call_opp", "faced an open with nothing in: no blind, no limp"),
        ("cold_call", "cold_call", "called an open cold"),
    )),
    ("Pot type", (
        ("limped", "limped", "nobody raised preflop"),
        ("srp", "srp", "single-raised pot"),
        ("3bet_pot", "3bet_pot", "the pot was 3-bet"),
        ("4bet_pot", "4bet_pot", "the pot was 4-bet or more"),
    )),
    ("Table", (
        ("position=POS", "position=BTN", "their seat, any table size"),
        ("players=N", "players>=5", "how many were dealt in"),
        ("dead_button", "dead_button", "the button was dead"),
        ("straddled", "straddled", "someone straddled"),
        ("players_flop=N", "players_flop>=3", "how many saw the flop"),
        ("eff_bb<N", "eff_bb<40", "effective stack at the start, in bb"),
        ("spr_flop<=N", "spr_flop<=3", "stack-to-pot ratio on the flop"),
    )),
    ("Postflop", (
        ("saw_flop", "saw_flop", "saw the flop"),
        ("cbet_<street>", "cbet_flop", "c-bet as last street's aggressor"),
        ("cbet_<street>_opp", "cbet_flop_opp", "could have c-bet that street"),
        ("faced_cbet_<street>", "faced_cbet_flop", "faced a c-bet"),
        ("folded_to_cbet_<street>", "folded_to_cbet_flop", "folded to a c-bet"),
        ("called_cbet_<street>", "called_cbet_flop", "called a c-bet"),
        ("raised_cbet_<street>", "raised_cbet_flop", "raised a c-bet"),
        ("donk_<street>", "donk_flop", "led into last street's aggressor"),
        ("donk_<street>_opp", "donk_flop_opp", "could have led"),
        ("bet_<street>", "bet_turn", "bet the street"),
        ("faced_bet_<street>", "faced_bet_river", "faced a bet or raise, anyone's"),
        ("folded_to_bet_<street>", "folded_to_bet_river", "folded to a bet or raise"),
        ("called_bet_<street>", "called_bet_river", "called a bet or raise"),
        ("raised_bet_<street>", "raised_bet_flop", "raised a bet or raise"),
        ("aggressor_<street>", "aggressor_river", "made the street's last bet or raise"),
        ("check_back_<street>", "check_back_turn", "their check closed a street that checked through"),
        ("check_back", "check_back", "checked back any street"),
        ("check_raise_<street>", "check_raise_flop", "checked, then raised a bet"),
        ("check_raise_<street>_opp", "check_raise_flop_opp", "checked, then had to act again"),
        ("check_raise", "check_raise", "check-raised any street"),
        ("faced_check_raise_<street>", "faced_check_raise_flop", "bet, and got check-raised"),
        ("faced_check_raise", "faced_check_raise", "got check-raised, any street"),
        ("faced_check_raise_<street>=ANSWER", "faced_check_raise_flop=fold", "what they did next: fold, call or raise"),
        ("delayed_cbet_<street>", "delayed_cbet_turn", "preflop raiser bet after every street before checked through (turn, river)"),
        ("delayed_cbet_<street>_opp", "delayed_cbet_turn_opp", "could have made that delayed c-bet"),
        ("delayed_cbet", "delayed_cbet", "delayed c-bet any street"),
        ("delayed_cbet_opp", "delayed_cbet_opp", "could have delayed c-bet, any street"),
        ("probe_<street>", "probe_turn", "bet into the preflop raiser who checked back the street before (turn, river)"),
        ("probe_<street>_opp", "probe_turn_opp", "could have probed"),
        ("probe", "probe", "probed any street"),
        ("probe_opp", "probe_opp", "could have probed, any street"),
        ("faced_probe_<street>", "faced_probe_turn", "preflop raiser facing a probe"),
        ("folded_to_probe_<street>", "folded_to_probe_turn", "folded to a probe"),
        ("faced_probe", "faced_probe", "faced a probe, any street"),
        ("folded_to_probe", "folded_to_probe", "folded to a probe, any street"),
        ("float_<street>", "float_turn", "called last street, then bet when its aggressor checked"),
        ("float_<street>_opp", "float_turn_opp", "could have floated"),
        ("float", "float", "floated any street"),
        ("float_opp", "float_opp", "could have floated, any street"),
    )),
    ("Action line", (
        ("line_<street>=LINE", "line_flop=xc",
         ("their own actions that street, e.g. line_flop=xc check-call · x* check, then anything"
          " · x? check, then one more · - didn't act")),
        ("line_<street>!=LINE", "line_turn!=x*", "any other line, e.g. line_turn!=x*"),
        ("line=LINE", "line=xc/b*/", "flop/turn/river, blank is any, e.g. line=xc/b*/ check-called the flop, bet the turn"),
        ("action_<street>=SEQ", "action_flop=xB*",
         ("everyone's actions in order, THEIRS IN CAPS, e.g. action_flop=xB* checked to them, they bet"
          " · bC called a bet · xBr* bet and got raised")),
        ("action_<street>!=SEQ", "action_turn!=*Br*", "any other sequence, e.g. action_turn!=*Br*"),
        ("action=SEQ", "action=*Bc//", "flop/turn/river, as line=, e.g. action=*Bc// their flop bet got called"),
    )),
    ("Holding", (
        ("hand=CLASS", "hand=72", "the shown hand: 72 is both suits, 72o one, 77 the pair"),
        ("hand!=CLASS", "hand!=AA", "any shown hand but that one"),
        ("hand_pct>=N", "hand_pct>=60", "strength rank of the shown hand: 0.6 is AA, 100 is 32o"),
        ("made_<street>=MADE", "made_flop=set|flush", "what the shown hand had made on that street; | for any of several"),
        ("made_<street>>=MADE", "made_turn>=two_pair", "that or better; also !=, >, <=, <"),
        ("made=MADE", "made=flush", "made on the final board"),
        ("tier_<street>=TIER", "tier_river=strong|nutted", "what it was worth on that board: nutted, strong, marginal, draw, air"),
        ("hs_<street>>=N", "hs_flop>=0.9", "share of holdings it beat on that street's board, 0 to 1"),
    )),
    ("Bet size", (
        ("cbet_<street>=SIZE", "cbet_flop=medium", "c-bet that size"),
        ("bet_<street>=SIZE", "bet_river=overbet", "first bet was that size"),
        ("faced_cbet_<street>=SIZE", "faced_cbet_flop=small", "faced a c-bet that size"),
        ("faced_bet_<street>=SIZE", "faced_bet_river=large", "first bet or raise they faced was that size"),
        ("raised_bet_<street>=SIZE", "raised_bet_flop=medium", "their first raise was that size"),
        ("bet_<street>>=N", "bet_river>=1", "first bet, as a pot fraction"),
        ("faced_bet_<street>>=N", "faced_bet_river>=1", "the bet or raise they faced, as a pot fraction"),
        ("raised_bet_<street>>=N", "raised_bet_flop>=1", "their raise, as a pot fraction once called"),
        ("bet_answer_<street>=ANSWER", "bet_answer_river=fold", "what their first bet got: fold, call or raise"),
    )),
    ("All-in", (
        ("jam", "jam", "bet or raised all-in, any street"),
        ("jam_preflop", "jam_preflop", "jammed preflop"),
        ("jam_<street>", "jam_river", "jammed that street"),
        ("faced_jam", "faced_jam", "acted facing someone's jam"),
        ("faced_jam_preflop", "faced_jam_preflop", "faced a preflop jam"),
        ("faced_jam_<street>", "faced_jam_river", "faced a jam on that street"),
        ("called_jam", "called_jam", "called a jam, any street"),
        ("called_jam_preflop", "called_jam_preflop", "called a preflop jam"),
        ("called_jam_<street>", "called_jam_river", "called a jam on that street"),
    )),
    ("Board", (
        ("flop=TEXTURE", "flop=monotone", "the flop has that texture"),
        ("turn=TEXTURE", "turn=paired", "the board through the turn"),
        ("river=TEXTURE", "river=flush_possible", "the board through the river"),
        ("board=TEXTURE", "board=twotone", "the whole board as dealt"),
        ("flop!=TEXTURE", "flop!=paired", "any of these, negated"),
    )),
    ("Result", (
        ("wtsd", "wtsd", "went to showdown"),
        ("won_sd", "won_sd", "won at showdown"),
        ("won_pot", "won_pot", "collected from the pot; a bounty is not the pot"),
        ("fold_street=STREET", "fold_street=turn", "the street they folded on: preflop, flop, turn or river"),
        ("bounty_won", "bounty_won", "was paid the 7-2 bounty"),
        ("bounty_paid", "bounty_paid", "paid the 7-2 bounty"),
        ("nit_won", "nit_won", "was paid the Nit penalty"),
        ("nit_paid", "nit_paid", "paid the Nit penalty: last to win a pot"),
        ("won", "won", "won chips"),
        ("lost", "lost", "lost chips"),
        ("cards_known", "cards_known", "hole cards were shown"),
        ("pot>=N", "pot>=500", "the final pot, in chips"),
        ("pot_bb>=N", "pot_bb>=50", "the final pot, in bb"),
    )),
    ("Game", (
        ("holdem", "holdem", "Hold'em"),
        ("plo", "plo", "PLO: hand lists and Results only"),
        ("bomb_pot", "bomb_pot", "a bomb pot: no blinds, everyone posted"),
        ("double_board", "double_board", "Double Board was dealt: hand lists and Results only"),
    )),
    ("Opponent", (
        ("vs=NAME", "vs=player 3", "played against that player"),
        ("vs!=NAME", "vs!=player 3", "not against that player"),
    )),
    ("Your tags", (
        ("tag=TAG", "tag=river-bluff", "hands you tagged, e.g. tag=cooler|study · tag=flagged the 🚩 ones"),
    )),
    ("Date", (
        ("since:DATE", "since:2026-10-20", "dealt that day or later, your local calendar"),
        ("before:DATE", "before:2026-10-20", "dealt before that day"),
        ("last:DAYS", "last:7d", "the last N days, today included: last:1d is today"),
    )),
    ("Session", (
        ("post_loss", "post_loss", f"the {WINDOW} hands after losing {BIG_LOSS_BB} bb or more in one hand, same session"),
        ("hands_since_loss<=N", "hands_since_loss<=5", "their hands since that big loss: 1 is the next hand"),
        ("session_hand>=N", "session_hand>=100", "their hand count this session, from 1"),
        ("session_hour>=N", "session_hour>=3", "hours since their first hand this session"),
    )),
    ("Timing", (
        ("think_preflop>=N", "think_preflop>=10", "seconds their first preflop decision took"),
        ("think_<street>>=N", "think_river>=5", "seconds their first decision on that street took, from the board or the last action"),
        ("tempo_preflop=TEMPO", "tempo_preflop=pre", "that preflop decision's tempo: pre, snap, normal, tank or timeout"),
        ("tempo_<street>=TEMPO", "tempo_river=tank", "that street's tempo; != for any other timed one"),
    )),
    ("Not", (
        ("!FLAG", "!cbet_flop", "any flag above, negated"),
    )),
    ("Combine", (
        ("A|B", "3bet_pot|4bet_pot", "either; | binds before , so srp,a|b is srp and (a or b)"),
        ("KEY=A|B", "position=BTN|CO", "any of these values; != is none of them"),
        ("(A,B)|C", "(srp,position=BTN)|(3bet_pot)", "brackets group terms"),
        ("!(A)", "!(limped)", "everything the bracket does not match"),
    )),
)


#: Flags no hand can match together. The `?` panel drops the earlier of two such
#: terms when the later one is typed, so `srp,...,3bet_pot` keeps `3bet_pot`.
EXCLUSIVE: tuple[tuple[str, ...], ...] = (
    ("limped", "srp", "3bet_pot", "4bet_pot"),
    ("won", "lost"),
    ("vpip", "no_vpip"),
    ("steal", "fold_to_steal_opp"),
    ("fold_to_steal", "3bet_vs_steal"),
    ("squeeze", "cold_call"),
    ("bounty_won", "bounty_paid"),
    ("nit_won", "nit_paid"),
    ("holdem", "plo"),
)

#: Texture tags no one board carries two of. The rest overlap on purpose (see
#: `cards.board_texture`), so `flop=monotone,flop=ace_high` is a real spot.
TEXTURE_FAMILIES: tuple[tuple[str, ...], ...] = (
    ("ace_high", "king_high", "queen_high", "jack_high", "ten_high", "low"),
    ("monotone", "twotone", "rainbow"),
)


_SIZE_WORDS = {
    "blocker": "under ⅕ pot",
    "small": "⅕ to ½ pot",
    "medium": "½ to ¾ pot",
    "large": "¾ pot to pot",
    "overbet": "more than pot",
}


#: Every name a `made_` term takes: the leaves, then the groups that are not leaves
#: themselves. `trips` is both, and the leaf wins.
MADE_NAMES: tuple[str, ...] = (*MADE_LEAVES, *(g for g in MADE_GROUPS if g not in MADE_LEAVES))

_MADE_WORDS = {
    "set": "pocket pair + a board card",
    "trips": "one hole card + a board pair",
    "overpair": "pocket pair over the board",
    "pocket_pair": "underpair",
    "two_pair": "both hole cards pair the board",
    "board_pair": "the hole cards add nothing to a paired board",
    "combo_draw": "flush draw + straight draw",
    "open_ender": "two ranks make a straight",
    "gutshot": "one rank makes a straight",
    "pair": "any pair",
    "draw": "any draw",
}


_TIER_WORDS = {
    "nutted": "beats nearly every holding on that board",
    "strong": "a value hand on that board",
    "marginal": "a pair of their own that beats some",
    "draw": "no pair of their own; a flush or straight draw",
    "air": "nothing of their own",
}


#: Every seat name `position_name` gives two to ten handed: what `position=` takes.
POSITIONS: tuple[str, ...] = tuple(
    dict.fromkeys(position_name(seat, n) for n in range(2, 11) for seat in range(n))
)
_POSITION_KEYS = frozenset(p.upper() for p in POSITIONS)


def vocabulary() -> dict:
    """VOCABULARY plus the value lists it refers to, as the `/filters` payload."""
    positions = list(POSITIONS)
    return {
        "groups": [
            {"name": name, "terms": [{"term": t, "example": e, "desc": d} for t, e, d in terms]}
            for name, terms in VOCABULARY
        ],
        "streets": list(STREETS),
        "operators": list(_OPS),
        "positions": positions,
        "decisions": list(DECISIONS),
        "decided": list(DECIDED),
        "answers": list(ANSWERS),
        "flags": list(FLAGS),
        "made": {m: _MADE_WORDS.get(m, "") for m in MADE_NAMES},
        "tiers": dict(_TIER_WORDS),
        "sizes": {b: _SIZE_WORDS[b] for b in SIZE_BUCKETS},
        "textures": list(TEXTURE_TAGS),
        "exclusive": [list(g) for g in EXCLUSIVE],
        "texture_families": [list(g) for g in TEXTURE_FAMILIES],
        "tempos": list(TEMPO_BUCKETS),
    }


def parse_filter(
    expr: str, names: Mapping[str, str] | None = None, tags: TagResolver | None = None
) -> Predicate:
    """Compile a filter expression into one predicate (SPEC.md, "Filter language").

    Terms are joined by ``,`` (and) and ``|`` (or), ``|`` binding tighter;
    brackets group, and ``!( )`` negates a bracket. ``position=BTN|CO`` is a value
    list, ``3bet_pot|4bet_pot`` an OR of whole terms. The tree is compiled here,
    once, and a row only walks it.

    `names` maps pn_id -> display name and is only needed for `vs=`; pass
    `display_names(conn)` from queries.py. `tags` resolves a tag to its hands and
    is only needed for `tag=`, which raises without it.

    Supported terms:
      ``3bet``               a flag from FLAGS
      ``position=BTN``       positional, pooled across table sizes
      ``players=6``          exact table size (n_dealt_in)
      ``players>=5``         table-size comparison
      ``open_bb>=4``         the hand's open raise was at least 4bb
      ``raise_bb<=2.5``      this player's own preflop raise-to
      ``bet_river>=1``       this player's first river bet, as a fraction of the pot
      ``pot>=500``           the final pot was at least 500 chips; ``pot_bb>=50`` in blinds
      ``vs=henry``           henry was still in when this player last acted; ``vs!=`` negates
      ``jam_river``          bet or raised all-in on the river; also ``jam``, ``faced_jam_<street>``
                             and ``called_jam_<street>``, preflop included
      ``cbet_flop=medium``   a flop c-bet in that size bucket; also bet_<street>= and
                             faced_cbet_<street>=, with small/medium/large/overbet
      ``faced_open=call``    what they did at a preflop decision point; also unopened=,
                             faced_3bet= (opener only), faced_3bet_any=, faced_4bet=,
                             faced_5bet=, with fold/check/call/raise
      ``flop=ace_high``      board texture on the flop; also turn=, river=, board=, and !=
      ``hand=72``            the shown holding: both suits, or ``72o`` / ``72s`` / ``77``; ``hand!=``
      ``hand_pct>=60``       the shown holding's strength rank, 0.6 (AA) to 100 (32o)
      ``check_back_turn``    closed a street that checked through; ``faced_bet_river``,
                             ``folded_to_bet_river``, ``called_bet_<street>``,
                             ``raised_bet_<street>`` and ``aggressor_<street>`` likewise
      ``check_raise_flop``   checked, then raised; ``check_raise_flop_opp`` checked and acted again
      ``faced_bet_river=large``  the bet or raise they first faced, by size; also
                             ``raised_bet_<street>=`` for their own raise, and ``>=0.5`` forms
      ``bet_answer_river=fold``  what their first bet got: fold, call or raise
      ``line_flop=xc``       their own actions on the street: x b c r f, ``*`` any run,
                             ``?`` one action, ``-`` did not act; ``line=xc//b*`` flop/turn/river
      ``action_flop=xBr*``   the whole street in order, their own actions in capitals and
                             everyone else's in lower case -- case-sensitive, unlike ``line_``;
                             ``action=*Bc//`` flop/turn/river
      ``faced_check_raise_flop``  they bet and were check-raised; ``=fold`` what they did next
      ``made_flop>=set``     their made hand on that street's board; ``made=flush`` on the
                             final one; =, !=, >=, >, <=, < against a leaf or a group
      ``fold_street=turn``   the street of their voluntary fold, preflop included
      ``eff_bb<40``          effective stack in bb; also ``spr_flop``, ``players_flop`` and
                             ``callers_before`` comparisons
      ``steal``              opened first in from a steal seat; also the steal, squeeze,
                             cold-call, probe, delayed c-bet and float families
      ``think_river>=5``     seconds their first river decision took; ``tempo_river=tank``
                             its bucket (pre, snap, normal, tank, timeout), preflop included
      ``tag=river-bluff``    a hand you tagged; ``tag!=`` negates
      ``since:2026-10-20``   dealt on or after that local day; ``before:`` before it,
                             ``last:7d`` today and the six days before
      ``!cbet_flop``         any flag, negated
    """
    preds = _Parser(expr, names, tags).parse()
    if not preds:
        return lambda f: True
    return lambda f: all(p(f) for p in preds)


def count_terms(
    facts: Iterable[Facts],
    base: str,
    terms: Iterable[str],
    names: Mapping[str, str] | None = None,
    tags: TagResolver | None = None,
) -> dict:
    """How many rows match `base`, and how many match `(base),(term)` per term
    (SPEC.md, "Counting terms"). A bad base raises; a bad term counts None."""
    pred = parse_filter(base, names, tags)
    rows = [f for f in facts if pred(f)]
    counts: dict[str, int | None] = {}
    for term in terms:
        try:
            p = parse_filter(term, names, tags)
        except ValueError:
            counts[term] = None
            continue
        counts[term] = sum(1 for f in rows if p(f))
    return {"base": len(rows), "counts": counts}


def _compile_term(
    term: str, names: Mapping[str, str] | None, tags: TagResolver | None = None
) -> tuple[Predicate, str]:
    """One term as a predicate, with the family that read it: `numeric` for a
    comparison, `flag`, `not` and `date` for the bare forms, else the family a value list
    has to stay inside (`position=BTN|CO` is two `position` terms)."""
    if term in FLAGS:
        return FLAGS[term], "flag"

    # `!flag`: plain flags only. A negated comparison or size has its own `!=`
    # where it means anything, and `!pot>=500` would read two ways.
    if term.startswith("!"):
        if term[1:] not in FLAGS:
            raise ValueError(f"only a flag can be negated with !: {term!r}")
        return (lambda f, p=FLAGS[term[1:]]: not p(f)), "not"

    if term.startswith("fold_street="):
        street = term.split("=", 1)[1].strip().lower()
        if street not in _STREET_ORDER:
            raise ValueError(
                f"unknown street {street!r} for fold_street=. Known: {', '.join(_STREET_ORDER)}"
            )
        return (lambda f, s=street: f.fold_street == s), "fold_street"

    if term.startswith("position="):
        want = term.split("=", 1)[1].strip().upper()
        if want not in _POSITION_KEYS:
            raise ValueError(f"unknown position {want!r}. Known: {', '.join(POSITIONS)}")
        return (
            lambda f, w=want: f.seats_from_button is not None
            and not f.dead_button
            and position_name(f.seats_from_button, f.n_dealt_in, f.position_slots).upper() == w
        ), "position"

    # In the order the terms were always tried: the first family to claim a term
    # reads it, so `bet_river=small` is a size and `bet_river=1` a comparison.
    for family, read in (
        ("vs", lambda t: _player_term(t, names)),
        ("tag", lambda t: _tag_term(t, tags)),
        ("hand", _hand_term),
        ("date", _date_term),
        ("decision", _decision_term),
        ("answer", _answer_term),
        ("line", lambda t: _street_term(t, "line")),
        ("action", lambda t: _street_term(t, "action")),
        ("tempo", _tempo_term),
        ("tier", _tier_term),
        ("made", _made_term),
        ("size", _size_term),
        ("numeric", _numeric_term),
        ("texture", _texture_term),
    ):
        pred = read(term)
        if pred is not None:
            return pred, family

    raise ValueError(
        f"unknown filter term: {term!r}. "
        f"Known flags: {', '.join(sorted(FLAGS))}; "
        f"position=<POS>; fold_street=<street>; vs=<player>; tag=<tag>; hand=<class>; "
        f"since:YYYY-MM-DD, before:YYYY-MM-DD or last:Nd; "
        f"a comparison on {', '.join(NUMERIC)}; "
        f"a size ({', '.join(SIZE_BUCKETS)}) on {', '.join(SIZED)}; "
        f"a decision ({', '.join(DECISIONS)}) on {', '.join(DECIDED)}; "
        f"an answer ({', '.join(ANSWERS)}) on {', '.join(ANSWERED)}; "
        f"line_<street>=PATTERN or line=flop/turn/river; action_<street>= or action= likewise; "
        f"made_<street>=HAND or made=HAND; tier_<street>=TIER ({', '.join(TIERS)}); "
        f"tempo_<street>=TEMPO ({', '.join(TEMPO_BUCKETS)}), preflop included; "
        f"!flag, !(...); or flop=/turn=/river=/board= with a texture tag"
    )


#: What makes a `|` segment a term of its own rather than a value of the one
#: before it: `position=BTN|CO` is a list, `3bet_pot|position=CO` two terms.
_OPERATOR_CHARS = frozenset("=!<>")
_OPERATOR = re.compile(r"!=|>=|<=|=|>|<")


class _Values:
    """A `key=value` term and the values `|` adds to it (`position=BTN|CO`).

    `=` is any of them and `!=` none of them, each value compiled as the term it
    spells. `made_` and `tier_` join their names back into one term instead, so
    their own `|` reads exactly as it always has.
    """

    def __init__(
        self, term: str, pred: Predicate, family: str, names: Mapping[str, str] | None, tags: TagResolver | None
    ):
        m = _OPERATOR.search(term)
        assert m is not None, term
        self.key, self.op = term[: m.start()], m.group()
        self.family, self.names, self.tags = family, names, tags
        self.values = [term[m.end():]]
        self.preds = [pred]

    def take(self, segment: str) -> bool:
        """Add `segment` as one more value if it is one; False leaves it a term."""
        if self.family == "numeric":
            return False
        # Any word can be a tag, so a flag after a tag list stays the flag:
        # `tag=study|won` is either. `tag=study|tag=won` names the tag.
        if self.family == "tag" and segment.strip().lower() in FLAGS:
            return False
        if self.family in ("made", "tier"):
            if segment.strip().lower() not in (MADE_NAMES if self.family == "made" else TIERS):
                return False
            self.values.append(segment)
            return True
        try:
            pred, family = _compile_term(f"{self.key}{self.op}{segment}", self.names, self.tags)
        except ValueError:
            return False
        if family != self.family:
            return False
        self.values.append(segment)
        self.preds.append(pred)
        return True

    def refuse(self, segment: str, err: ValueError) -> ValueError:
        """Why `segment` is neither a value here nor a term (`err`, as a term)."""
        if self.family == "numeric":
            return ValueError(
                f"{self.key}{self.op}{self.values[0]}|{segment}: a comparison takes one number, "
                f"so write each as its own term (eff_bb<40|eff_bb>=200)"
            )
        try:
            _compile_term(f"{self.key}{self.op}{segment}", self.names, self.tags)
        except ValueError as own:
            return own
        return err

    def predicate(self) -> Predicate:
        if self.family in ("made", "tier"):
            return _compile_term(f"{self.key}{self.op}{'|'.join(self.values)}", self.names, self.tags)[0]
        preds = tuple(self.preds)
        if len(preds) == 1:
            return preds[0]
        if self.op == "!=":
            return lambda f: all(p(f) for p in preds)
        return lambda f: any(p(f) for p in preds)


class _Parser:
    """The filter grammar (SPEC.md, "Filter language"), by recursive descent::

        conj := disj ("," disj)*
        disj := unit ("|" unit)*
        unit := "(" conj ")" | "!(" conj ")" | term
    """

    def __init__(self, expr: str, names: Mapping[str, str] | None, tags: TagResolver | None = None):
        self.expr, self.names, self.tags = expr, names, tags
        self.tokens = list(self._tokens(expr))
        self.at = 0

    @staticmethod
    def _tokens(expr: str):
        """(kind, text) pairs: `,` `|` `(` `!(` `)`, or `term` and its stripped text."""
        i, n = 0, len(expr)
        while i < n:
            ch = expr[i]
            if ch.isspace():
                i += 1
            elif ch in ",|()":
                yield ch, ch
                i += 1
            elif ch == "!" and expr[i + 1:].lstrip().startswith("("):
                yield "!(", "!("
                i = expr.index("(", i) + 1
            else:
                j = i
                while j < n and expr[j] not in ",|()":
                    j += 1
                yield "term", expr[i:j].strip()
                i = j

    def _peek(self) -> str | None:
        return self.tokens[self.at][0] if self.at < len(self.tokens) else None

    def parse(self) -> list[Predicate]:
        preds = self._conj()
        if self._peek() is not None:  # only a `)` stops a conj early
            raise ValueError(f"{self.expr!r}: a ) with no ( before it")
        return preds

    def _conj(self) -> list[Predicate]:
        preds: list[Predicate] = []
        while (kind := self._peek()) not in (None, ")"):
            if kind == ",":  # an empty term: `3bet,` is `3bet`
                self.at += 1
                continue
            preds.append(self._disj())
            if self._peek() not in (None, ",", ")"):
                raise ValueError(f"{self.expr!r}: put a , or a | before a (")
        return preds

    def _bracket(self) -> Predicate:
        negate = self.tokens[self.at][0] == "!("
        self.at += 1
        inner = tuple(self._conj())
        if self._peek() != ")":
            raise ValueError(f"{self.expr!r}: a ( that is never closed")
        self.at += 1
        if not inner:
            raise ValueError(f"{self.expr!r}: empty brackets")
        if negate:
            return lambda f: not all(p(f) for p in inner)
        return inner[0] if len(inner) == 1 else (lambda f: all(p(f) for p in inner))

    def _disj(self) -> Predicate:
        alts: list[Predicate] = []
        values: _Values | None = None  # the key=value term a bare segment may extend
        while True:
            kind = self._peek()
            if kind in ("(", "!("):
                if values is not None:
                    alts.append(values.predicate())
                    values = None
                alts.append(self._bracket())
            elif kind == "term":
                segment = self.tokens[self.at][1]
                self.at += 1
                bare = not _OPERATOR_CHARS & set(segment)
                if not (values is not None and bare and values.take(segment)):
                    try:
                        pred, family = _compile_term(segment, self.names, self.tags)
                    except ValueError as err:
                        if values is not None and bare:
                            raise values.refuse(segment, err) from None
                        raise
                    if values is not None:
                        alts.append(values.predicate())
                    values = None
                    if family in ("flag", "not", "date"):
                        alts.append(pred)
                    else:
                        values = _Values(segment, pred, family, self.names, self.tags)
            else:
                if values is not None and values.family in ("made", "tier"):
                    # `made_flop=set|`: the made term says what is missing, as it always has.
                    values.values.append("")
                    values.predicate()
                raise ValueError(f"{self.expr!r}: a | needs a term on each side")
            if self._peek() != "|":
                break
            self.at += 1
        if values is not None:
            alts.append(values.predicate())
        if len(alts) == 1:
            return alts[0]
        preds = tuple(alts)
        return lambda f: any(p(f) for p in preds)


# --- timing tells (SPEC.md, "Timing tells"): how long their first decision on a
# street took, in seconds, and its tempo bucket. A decision that could not be
# timed -- a hand imported before the clock was stored -- has neither, and no
# term here matches it, `!=` included.
TEMPO_STREETS = ("preflop", *STREETS)

# --- hand strength (SPEC.md, "Hand strength"): the share of holdings theirs beat
# on the board as it stood, 0 to 1. Known cards only, gated like `tier_<street>`.
for _s in STREETS:
    NUMERIC[f"hs_{_s}"] = lambda f, s=_s: hs_of(f, s)
for _s in TEMPO_STREETS:
    NUMERIC[f"think_{_s}"] = lambda f, s=_s: f.think_ms[s] / 1000 if s in f.think_ms else None

#: The tempo terms: `tempo_river=tank`, `tempo_flop!=pre`.
TEMPO_TERMS = tuple(f"tempo_{s}" for s in TEMPO_STREETS)


def _tempo_term(term: str) -> Predicate | None:
    for name in TEMPO_TERMS:
        for op in ("!=", "="):
            prefix = f"{name}{op}"
            if not term.startswith(prefix):
                continue
            bucket = term[len(prefix):].strip().lower()
            if bucket not in TEMPO_BUCKETS:
                raise ValueError(f"unknown tempo {bucket!r}. Known: {', '.join(TEMPO_BUCKETS)}")
            street, want = name.removeprefix("tempo_"), op == "="

            def pred(f: Facts, street=street, bucket=bucket, want=want) -> bool:
                got = f.tempo.get(street)
                return got is not None and (got == bucket) == want

            return pred
    return None
