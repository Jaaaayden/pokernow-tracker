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
raised them, their own letters in capitals.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping

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
from .derive import ANSWERS, LINE_CODE, SIZE_BUCKETS, Facts

Predicate = Callable[[Facts], bool]

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
    "limped": lambda f: f.pot_level == 1,
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
}
for _s in STREETS:
    #: The bet or raise they first faced on the street, and their own first raise,
    #: as pot fractions: `faced_bet_river>=1` is facing a pot-sized bet or bigger.
    NUMERIC[f"faced_bet_{_s}"] = lambda f, s=_s: f.faced_bet_pot.get(s)
    NUMERIC[f"raised_bet_{_s}"] = lambda f, s=_s: f.raise_pot.get(s)

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
                return (tag in board_texture(cards)) == want

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
#: this to FLAGS, NUMERIC, SIZED, ANSWERED, LINE_TERMS, ACTION_TERMS and MADE_TERMS,
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
    )),
    ("Action line", (
        ("line_<street>=LINE", "line_flop=xc",
         ("their actions that street: x check, b bet, c call, r raise, f fold; x* checked then anything,"
          " x? checked then exactly one more (xc xf xr), - alone did not act")),
        ("line_<street>!=LINE", "line_turn!=x*", "any line but that one"),
        ("line=LINE", "line=xc//b*", "flop/turn/river; * any run, ? one action, - did not act, empty any"),
        ("action_<street>=SEQ", "action_flop=xBr*",
         ("the whole street, everyone in order: THEIRS IN CAPITALS, others lower case; xB* checked to them,"
          " they bet, then anything; xB? then exactly one more (xBc or xBf); - alone not in the hand; case matters")),
        ("action_<street>!=SEQ", "action_turn!=*Br*", "any street but that"),
        ("action=SEQ", "action=*Bc//", "flop/turn/river, as line=; - not in the hand that street"),
    )),
    ("Holding", (
        ("hand=CLASS", "hand=72", "the shown hand: 72 is both suits, 72o one, 77 the pair"),
        ("hand!=CLASS", "hand!=AA", "any shown hand but that one"),
        ("hand_pct>=N", "hand_pct>=60", "strength rank of the shown hand: 0.6 is AA, 100 is 32o"),
        ("made_<street>=MADE", "made_flop=set|flush", "what the shown hand had made on that street; | for any of several"),
        ("made_<street>>=MADE", "made_turn>=two_pair", "that or better; also !=, >, <=, <"),
        ("made=MADE", "made=flush", "made on the final board"),
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
        ("won", "won", "won chips"),
        ("lost", "lost", "lost chips"),
        ("cards_known", "cards_known", "hole cards were shown"),
        ("pot>=N", "pot>=500", "the final pot, in chips"),
        ("pot_bb>=N", "pot_bb>=50", "the final pot, in bb"),
    )),
    ("Opponent", (
        ("vs=NAME", "vs=henry", "played against that player"),
        ("vs!=NAME", "vs!=henry", "not against that player"),
    )),
    ("Not", (
        ("!FLAG", "!cbet_flop", "any flag above, negated"),
    )),
)


#: Flags no hand can match together. The `?` panel drops the earlier of two such
#: terms when the later one is typed, so `srp,...,3bet_pot` keeps `3bet_pot`.
EXCLUSIVE: tuple[tuple[str, ...], ...] = (
    ("limped", "srp", "3bet_pot", "4bet_pot"),
    ("won", "lost"),
    ("vpip", "no_vpip"),
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
    "board_pair": "pair on the board only",
    "combo_draw": "flush draw + straight draw",
    "open_ender": "two ranks make a straight",
    "gutshot": "one rank makes a straight",
    "pair": "any pair",
    "draw": "any draw",
}


def vocabulary() -> dict:
    """VOCABULARY plus the value lists it refers to, as the `/filters` payload."""
    positions: list[str] = []
    for n in range(2, 11):
        for seat in range(n):
            if (p := position_name(seat, n)) not in positions:
                positions.append(p)
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
        "sizes": {b: _SIZE_WORDS[b] for b in SIZE_BUCKETS},
        "textures": list(TEXTURE_TAGS),
        "exclusive": [list(g) for g in EXCLUSIVE],
        "texture_families": [list(g) for g in TEXTURE_FAMILIES],
    }


def parse_filter(expr: str, names: Mapping[str, str] | None = None) -> Predicate:
    """Compile a comma-separated filter expression into one predicate (AND).

    `names` maps pn_id -> display name and is only needed for `vs=`; pass
    `display_names(conn)` from queries.py.

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
      ``!cbet_flop``         any flag, negated
    """
    terms = [t.strip() for t in expr.split(",") if t.strip()]
    preds: list[Predicate] = []

    for term in terms:
        if term in FLAGS:
            preds.append(FLAGS[term])
            continue

        # `!flag`: plain flags only. A negated comparison or size has its own `!=`
        # where it means anything, and `!pot>=500` would read two ways.
        if term.startswith("!"):
            if term[1:] not in FLAGS:
                raise ValueError(f"only a flag can be negated with !: {term!r}")
            preds.append(lambda f, p=FLAGS[term[1:]]: not p(f))
            continue

        player = _player_term(term, names)
        if player is not None:
            preds.append(player)
            continue

        holding = _hand_term(term)
        if holding is not None:
            preds.append(holding)
            continue

        if term.startswith("position="):
            want = term.split("=", 1)[1].upper()
            preds.append(
                lambda f, w=want: f.seats_from_button is not None
                and not f.dead_button
                and position_name(f.seats_from_button, f.n_dealt_in).upper() == w
            )
            continue

        decided = _decision_term(term)
        if decided is not None:
            preds.append(decided)
            continue

        answered = _answer_term(term)
        if answered is not None:
            preds.append(answered)
            continue

        line = _street_term(term, "line")
        if line is not None:
            preds.append(line)
            continue

        action = _street_term(term, "action")
        if action is not None:
            preds.append(action)
            continue

        made = _made_term(term)
        if made is not None:
            preds.append(made)
            continue

        sized = _size_term(term)
        if sized is not None:
            preds.append(sized)
            continue

        numeric = _numeric_term(term)
        if numeric is not None:
            preds.append(numeric)
            continue

        texture = _texture_term(term)
        if texture is not None:
            preds.append(texture)
            continue

        raise ValueError(
            f"unknown filter term: {term!r}. "
            f"Known flags: {', '.join(sorted(FLAGS))}; "
            f"position=<POS>; vs=<player>; hand=<class>; a comparison on {', '.join(NUMERIC)}; "
            f"a size ({', '.join(SIZE_BUCKETS)}) on {', '.join(SIZED)}; "
            f"a decision ({', '.join(DECISIONS)}) on {', '.join(DECIDED)}; "
            f"an answer ({', '.join(ANSWERS)}) on {', '.join(ANSWERED)}; "
            f"line_<street>=PATTERN or line=flop/turn/river; action_<street>= or action= likewise; "
            f"made_<street>=HAND or made=HAND; "
            f"!flag; or flop=/turn=/river=/board= with a texture tag"
        )

    if not preds:
        return lambda f: True
    return lambda f: all(p(f) for p in preds)
