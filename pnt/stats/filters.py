"""Hand filters -- "show me only the hands where X happened".

This is the Holdem-Manager capability: pick a spot, then look at what a player
does inside it. `3bet,position=BTN` means *of the hands where this player 3-bet
from the button*, what are their stats?

Filters are predicates over the derived `Facts` row for one (hand, player), which
is why they compose freely and why adding a new one is three lines. That only
works because actions are stored raw with street and sequence -- a pre-aggregated
schema cannot answer "hands that reached this point" at all.

A *line* is just a longer filter. "Opened 4bb+ in a single-raised pot, c-bet the
flop and turn, overbet the river" is::

    opener,open_bb>=4,srp,cbet_flop,cbet_turn,bet_river>=1
"""

from __future__ import annotations

from collections.abc import Callable

from ..logfmt.parser import position_name
from .cards import TEXTURE_TAGS, board_texture
from .derive import Facts

Predicate = Callable[[Facts], bool]

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
    "faced_cbet_flop": lambda f: bool(f.fold_to_cbet_opp.get("flop")),
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


_OPS: dict[str, Callable[[float, float], bool]] = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "=": lambda a, b: a == b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
}


def _numeric_term(term: str) -> Predicate | None:
    for name, getter in NUMERIC.items():
        if not term.startswith(name):
            continue
        rest = term[len(name):]
        for op in (">=", "<=", "=", ">", "<"):  # two-char ops first
            if rest.startswith(op):
                n = float(rest[len(op):])
                cmp = _OPS[op]

                def pred(f: Facts, g=getter, n=n, cmp=cmp) -> bool:
                    v = g(f)
                    return v is not None and cmp(v, n)

                return pred
    return None


def parse_filter(expr: str) -> Predicate:
    """Compile a comma-separated filter expression into one predicate (AND).

    Supported terms:
      ``3bet``               a flag from FLAGS
      ``position=BTN``       positional, pooled across table sizes
      ``players=6``          exact table size (n_dealt_in)
      ``players>=5``         table-size comparison
      ``open_bb>=4``         the hand's open raise was at least 4bb
      ``raise_bb<=2.5``      this player's own preflop raise-to
      ``bet_river>=1``       this player's first river bet, as a fraction of the pot
      ``flop=ace_high``      board texture on the flop; also turn=, river=, board=, and !=
    """
    terms = [t.strip() for t in expr.split(",") if t.strip()]
    preds: list[Predicate] = []

    for term in terms:
        if term in FLAGS:
            preds.append(FLAGS[term])
            continue

        if term.startswith("position="):
            want = term.split("=", 1)[1].upper()
            preds.append(
                lambda f, w=want: f.seats_from_button is not None
                and not f.dead_button
                and position_name(f.seats_from_button, f.n_dealt_in).upper() == w
            )
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
            f"position=<POS>; a comparison on {', '.join(NUMERIC)}; "
            f"or flop=/turn=/river=/board= with a texture tag"
        )

    if not preds:
        return lambda f: True
    return lambda f: all(p(f) for p in preds)
