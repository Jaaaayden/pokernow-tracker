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
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from ..logfmt.parser import position_name
from .cards import TEXTURE_TAGS, board_texture
from .derive import SIZE_BUCKETS, Facts

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
    "cbet_flop_opp": lambda f: bool(f.cbet_opp.get("flop")),
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
}

# Responses to a c-bet, and leads into the previous street's aggressor, per street.
for _s in STREETS:
    FLAGS[f"faced_cbet_{_s}"] = lambda f, s=_s: bool(f.fold_to_cbet_opp.get(s))
    FLAGS[f"folded_to_cbet_{_s}"] = lambda f, s=_s: bool(f.fold_to_cbet.get(s))
    FLAGS[f"raised_cbet_{_s}"] = lambda f, s=_s: bool(f.raise_cbet.get(s))
    FLAGS[f"donk_{_s}"] = lambda f, s=_s: bool(f.donk.get(s))
    FLAGS[f"donk_{_s}_opp"] = lambda f, s=_s: bool(f.donk_opp.get(s))

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
}

#: name -> getter for the size-bucket terms (`cbet_flop=medium`, `bet_river=overbet`,
#: `faced_cbet_turn=large`). The buckets are SIZE_BUCKETS; see SPEC.md.
SIZED: dict[str, Callable[[Facts], str | None]] = {}
for _s in STREETS:
    SIZED[f"cbet_{_s}"] = lambda f, s=_s: f.bet_size.get(s) if f.cbet.get(s) else None
    SIZED[f"bet_{_s}"] = lambda f, s=_s: f.bet_size.get(s)
    SIZED[f"faced_cbet_{_s}"] = lambda f, s=_s: f.faced_cbet_size.get(s)

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
      ``cbet_flop=medium``   a flop c-bet in that size bucket; also bet_<street>= and
                             faced_cbet_<street>=, with small/medium/large/overbet
      ``flop=ace_high``      board texture on the flop; also turn=, river=, board=, and !=
    """
    terms = [t.strip() for t in expr.split(",") if t.strip()]
    preds: list[Predicate] = []

    for term in terms:
        if term in FLAGS:
            preds.append(FLAGS[term])
            continue

        player = _player_term(term, names)
        if player is not None:
            preds.append(player)
            continue

        if term.startswith("position="):
            want = term.split("=", 1)[1].upper()
            preds.append(
                lambda f, w=want: f.seats_from_button is not None
                and not f.dead_button
                and position_name(f.seats_from_button, f.n_dealt_in).upper() == w
            )
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
            f"position=<POS>; vs=<player>; a comparison on {', '.join(NUMERIC)}; "
            f"a size ({', '.join(SIZE_BUCKETS)}) on {', '.join(SIZED)}; "
            f"or flop=/turn=/river=/board= with a texture tag"
        )

    if not preds:
        return lambda f: True
    return lambda f: all(p(f) for p in preds)
