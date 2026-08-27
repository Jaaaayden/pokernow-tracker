"""Hand filters -- "show me only the hands where X happened".

This is the Holdem-Manager capability: pick a spot, then look at what a player
does inside it. `3bet,position=BTN` means *of the hands where this player 3-bet
from the button*, what are their stats?

Filters are predicates over the derived `Facts` row for one (hand, player), which
is why they compose freely and why adding a new one is three lines. That only
works because actions are stored raw with street and sequence -- a pre-aggregated
schema cannot answer "hands that reached this point" at all.
"""

from __future__ import annotations

from collections.abc import Callable

from ..logfmt.parser import position_name
from .derive import Facts

Predicate = Callable[[Facts], bool]

#: name -> predicate. Flags only; parameterised filters are handled in `parse_filter`.
FLAGS: dict[str, Predicate] = {
    "vpip": lambda f: f.vpip,
    "no_vpip": lambda f: f.vpip_opp and not f.vpip,
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
}


def parse_filter(expr: str) -> Predicate:
    """Compile a comma-separated filter expression into one predicate (AND).

    Supported terms:
      ``3bet``               a flag from FLAGS
      ``position=BTN``       positional, pooled across table sizes
      ``players=6``          exact table size (n_dealt_in)
      ``players>=5``         table-size comparison
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

        for op in (">=", "<=", "=", ">", "<"):
            prefix = f"players{op}"
            if term.startswith(prefix):
                n = int(term[len(prefix) :])
                cmp = {
                    "=": lambda a, b: a == b,
                    ">": lambda a, b: a > b,
                    "<": lambda a, b: a < b,
                    ">=": lambda a, b: a >= b,
                    "<=": lambda a, b: a <= b,
                }[op]
                preds.append(lambda f, n=n, cmp=cmp: cmp(f.n_dealt_in, n))
                break
        else:
            raise ValueError(
                f"unknown filter term: {term!r}. "
                f"Known flags: {', '.join(sorted(FLAGS))}; "
                f"or position=<POS>, players=<n>"
            )

    if not preds:
        return lambda f: True
    return lambda f: all(p(f) for p in preds)
