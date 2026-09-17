"""Player tags: one archetype, the exploits the data supports, and the fun ones.

A tag is a claim about a player that you can act on at the table -- "never bluffs
the river", "folds to 3-bets" -- backed by a count and a spot filter that puts the
hands behind it on the chart. SPEC.md, "Tags", pins every rule: what is counted,
over what, the threshold, and the smallest sample that may fire it. This module
mirrors that section the way `derive.py` mirrors the stat tables.

Three things every rule respects:

- **Too small a sample is unknown, not false.** A rule under its minimum is silent,
  on the same principle that prints `--` for a rate with no opportunities.
- **Shown-hand evidence is biased toward showdowns.** A player's river bets that
  reached showdown are the ones that got called; the bluffs that worked are not in
  the sample. Every tip built from shown hands says "shown".
- **Nothing is stored.** Tags are computed from `Facts` at read time, like every
  other figure, so a threshold change is an edit here and in SPEC.md, not a migration.

`THRESHOLDS` holds every cutoff in one place. The values were calibrated against
the 8,036-hand database on 2026-09-14: the first draft fired for the whole table on
some rules and could never fire on others.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from functools import lru_cache

from ..logfmt.events import PREFLOP, RIVER
from .cards import board_at, hand_class, hand_pct, made_hand, street_of_board
from .derive import POSTFLOP_STREETS, Facts

#: Hands these games play on purpose (the 7-2 bounty and its cousins). They are
#: fun-tag material and never evidence of a bad call: a 72o that called a 3-bet
#: was a bet on the side game, not a misread of the spot.
TROLL_HANDS: frozenset[str] = frozenset({"72o", "72s", "92o", "92s", "K2o"})

#: What ordinary VPIP and PFR look like at each table size, in points. Heads-up,
#: 70% VPIP is normal; six-handed it is loose. A player's looseness is measured
#: hand by hand against the baseline for *that hand's* table, so a player who
#: alternates between heads-up and a full table is judged fairly on both. These
#: are judgement values, recorded in SPEC.md so they can be argued with.
BASELINES: dict[int, tuple[float, float]] = {
    2: (68, 50),
    3: (52, 36),
    4: (44, 30),
    5: (38, 26),
    6: (30, 21),
}
DEFAULT_BASELINE: tuple[float, float] = (24, 17)  # seven-handed and up

#: An archetype needs this many preflop decisions behind it.
MIN_ARCHETYPE_OPPS = 100
#: A REG with this many hands and no exploit tag is shown as BALANCED.
BALANCED_HANDS = 500

#: Every cutoff, by tag id: `pct` is compared against the rule's rate, `n` is the
#: smallest sample the rule may fire on, `hits` a minimum count where the rule is
#: a count rather than a rate. Mirrored as the table in SPEC.md.
THRESHOLDS: dict[str, dict[str, float]] = {
    "no_bluff": {"pct": 7, "n": 10},
    "bluffs_river": {"pct": 20, "n": 8},
    "calls_down_light": {"pct": 20, "n": 8},
    "folds_river": {"pct": 50, "n": 15},
    "folds_to_3bet": {"pct": 50, "n": 15},
    # The light-hand rules need a count *and* a share: the hero's cards are known
    # on every hand, so a bare count would fire on anyone with enough hands.
    "inelastic_vs_3bet": {"pct": 15, "n": 15, "hits": 3, "light_pct": 15, "hand_pct": 60},
    "fourbets_light": {"hits": 3, "light_pct": 15, "hand_pct": 50},
    "jams_light": {"hits": 3, "light_pct": 15, "hand_pct": 50},
    "limper": {"pct": 45, "n": 30},
    "jam_happy": {"pct": 3, "hits": 5, "post_pct": 8, "post_hits": 8},
    "folds_to_cbet": {"pct": 50, "n": 25},
    "sticky_vs_cbet": {"pct": 30, "n": 25},
    "auto_cbet": {"pct": 60, "n": 25},
    "rarely_cbets": {"pct": 35, "n": 25},
    "checks_back_weak": {"pct": 35, "n": 10},
    "traps": {"pct": 60, "n": 8},
    "donks": {"pct": 35, "n": 15},
    "size_tell": {"gap": 40, "n": 6},
    "fun": {"hits": 2},
    # archetypes
    "maniac": {"pfr_excess": 15, "af_flop": 60, "jam_pct": 3, "3bet": 18},
    "station": {"fold_to_cbet": 30, "fold_to_cbet_n": 25, "wtsd": 38, "wtsd_n": 40, "pfr_ratio": 0.5},
    "fish": {"vpip_excess": 12, "pfr_ratio": 0.45},
    "lag": {"vpip_excess": 8, "pfr_ratio": 0.6},
    "nit": {"vpip_excess": -12},
}

#: The fun tags: a display name and the classes it covers.
FUN_HANDS: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("7-2", "72", frozenset({"72o", "72s"})),
    ("9-2", "92", frozenset({"92o", "92s"})),
    ("K-2o", "K2o", frozenset({"K2o"})),
)

_STRONG_PAIRS = {"overpair", "top_pair"}
_WEAK_PAIRS = {"bottom_pair", "board_pair"}

#: Preflop strength by `hand_pct`: at or under the first cutoff is `strong`, at
#: or under the second `medium`, the rest `weak`. 15 is roughly 77+, ATs+, KQs
#: and AJo+; 50 is where a chart's playable hands run out. Judgement values,
#: recorded in SPEC.md, "What they showed up with".
PREFLOP_STRENGTH: dict[str, float] = {"strong": 15, "medium": 50}


@lru_cache(maxsize=65536)
def _strength_on(hole: str, board: tuple[str, ...], street: str) -> str:
    """The postflop rule, memoized: the same shown hands are re-read every poll."""
    mh = made_hand(hole, board)
    if mh.cls == "draw":
        return "air" if street == RIVER else "draw"
    if mh.cls == "high_card":
        return "air"
    if mh.cls != "pair":
        return "strong"
    if mh.detail in _STRONG_PAIRS:
        return "strong"
    if mh.detail in _WEAK_PAIRS:
        return "weak"
    return "medium"


def strength_at(hole_cards: str | None, board: tuple[str, ...] | list[str], street: str) -> str | None:
    """What a known holding was worth on `street`, or None when that is unknown.

    Postflop, on the board as it stood on that street:

    ``strong``  two pair or better, or top pair / an overpair
    ``medium``  middle pair, or a pocket pair below the top card
    ``weak``    bottom pair, or a pair that is all on the board
    ``draw``    no pair, but a flush or straight draw -- on the flop or turn
    ``air``     high card; on the river a draw has missed, so it is air too

    Preflop, by `hand_pct` against `PREFLOP_STRENGTH`: ``strong``, ``medium`` or
    ``weak``. Unknown when the cards are, or when the board never reached the
    street: a hand that ended on the flop says nothing about the turn.
    """
    if not hole_cards:
        return None
    if street == PREFLOP:
        pct = hand_pct(hand_class(hole_cards))
        for bucket, cutoff in PREFLOP_STRENGTH.items():
            if pct <= cutoff:
                return bucket
        return "weak"
    seen = board_at(board, street)
    if seen is None:
        return None
    return _strength_on(hole_cards, seen, street)


def strength(f: Facts) -> str | None:
    """What a shown hand had made by the end, in four words, or None when unknown.

    ``strong``  two pair or better, or top pair / an overpair
    ``medium``  middle pair, or a pocket pair below the top card
    ``weak``    bottom pair, or a pair that is all on the board
    ``air``     high card, or a draw that missed

    `strength_at` on the final board, with a draw that never got there read as
    air: this is what the tags judge.
    """
    if not f.hole_cards or len(f.board) < 3:
        return None
    s = strength_at(f.hole_cards, f.board, street_of_board(f.board))
    return "air" if s == "draw" else s


def _pct(hits: int, n: int) -> float | None:
    return round(100.0 * hits / n, 1) if n else None


def _tag(
    id: str,
    label: str,
    kind: str,
    tip: str,
    n: int,
    hits: int,
    filter: str,
    by: str = "preflop",
    **extra,
) -> dict:
    return {
        "id": id,
        "label": label,
        "kind": kind,
        "tip": tip,
        "n": n,
        "hits": hits,
        "pct": _pct(hits, n),
        "filter": filter,
        "by": by,
        **extra,
    }


def _count(facts: Iterable[Facts], pred: Callable[[Facts], bool]) -> int:
    return sum(1 for f in facts if pred(f))


def _share(facts: list[Facts], pool: Callable[[Facts], bool], hit: set[str]) -> tuple[int, int]:
    """(shown hands in the pool, how many of them made a hand in `hit`)."""
    n = hits = 0
    for f in facts:
        if not pool(f):
            continue
        s = strength(f)
        if s is None:
            continue
        n += 1
        hits += s in hit
    return n, hits


def _baseline(n_dealt_in: int) -> tuple[float, float]:
    return BASELINES.get(n_dealt_in, DEFAULT_BASELINE)


def _is_light(f: Facts, cutoff: float) -> bool:
    """A shown hand at or below the cutoff of the ranking, troll hands excepted."""
    if not f.hole_cards:
        return False
    cls = hand_class(f.hole_cards)
    return cls not in TROLL_HANDS and hand_pct(cls) >= cutoff


# --------------------------------------------------------------- profile ----


def profile(facts: list[Facts]) -> dict:
    """The measurements every rule reads, so `pnt tags` can show why a tag did or
    did not fire. Rates are None when their denominator is empty."""
    vpip_opp = [f for f in facts if f.vpip_opp]
    vpip = sum(1 for f in vpip_opp if f.vpip)
    pfr = sum(1 for f in vpip_opp if f.pfr)
    vpip_excess = pfr_excess = None
    if vpip_opp:
        vpip_excess = sum((100 if f.vpip else 0) - _baseline(f.n_dealt_in)[0] for f in vpip_opp) / len(vpip_opp)
        pfr_excess = sum((100 if f.pfr else 0) - _baseline(f.n_dealt_in)[1] for f in vpip_opp) / len(vpip_opp)

    def rate(num: Callable[[Facts], bool], den: Callable[[Facts], bool]) -> dict:
        n = _count(facts, den)
        hits = _count(facts, lambda f: den(f) and num(f))
        return {"hits": hits, "n": n, "pct": _pct(hits, n)}

    def shown(pool: Callable[[Facts], bool], hit: set[str]) -> dict:
        n, hits = _share(facts, pool, hit)
        return {"hits": hits, "n": n, "pct": _pct(hits, n)}

    agg = sum(f.aggressive.get("flop", 0) for f in facts)
    agg_den = sum(f.agg_denom.get("flop", 0) for f in facts)
    three_opp = _count(facts, lambda f: f.three_bet_opp)
    pre_jams = _count(facts, lambda f: bool(f.jam.get("preflop")))
    post_jams = _count(facts, lambda f: any(f.jam.get(s) for s in POSTFLOP_STREETS))
    saw_flop = _count(facts, lambda f: f.saw_flop)
    t = THRESHOLDS

    return {
        "hands": len(facts),
        "vpip_opp": len(vpip_opp),
        "vpip": _pct(vpip, len(vpip_opp)),
        "pfr": _pct(pfr, len(vpip_opp)),
        "vpip_excess": round(vpip_excess, 1) if vpip_excess is not None else None,
        "pfr_excess": round(pfr_excess, 1) if pfr_excess is not None else None,
        "pfr_ratio": round(pfr / vpip, 2) if vpip else None,
        "tables": dict(sorted(Counter(f.n_dealt_in for f in vpip_opp).items())),
        "af_flop": {"hits": agg, "n": agg_den, "pct": _pct(agg, agg_den)},
        "3bet": rate(lambda f: f.three_bet, lambda f: f.three_bet_opp) if three_opp else {"hits": 0, "n": 0, "pct": None},
        "wtsd": rate(lambda f: f.wtsd, lambda f: f.wtsd_opp),
        "rules": {
            "no_bluff": shown(lambda f: bool(f.aggressor.get("river")) and f.wtsd, {"air"}),
            "calls_down_light": shown(lambda f: bool(f.called_bet.get("river")) and f.wtsd, {"air", "weak"}),
            "folds_river": rate(lambda f: bool(f.folded_to_bet.get("river")), lambda f: bool(f.faced_bet.get("river"))),
            "folds_to_3bet": rate(lambda f: f.fold_to_3bet, lambda f: f.fold_to_3bet_opp),
            "called_3bet_light": rate(
                lambda f: _is_light(f, t["inelastic_vs_3bet"]["hand_pct"]),
                lambda f: f.hole_cards is not None and "call" in (f.pf_faced.get(3), f.pf_faced.get(4)),
            ),
            "fourbets_light": rate(
                lambda f: _is_light(f, t["fourbets_light"]["hand_pct"]),
                lambda f: f.hole_cards is not None and "raise" in (f.pf_faced.get(3), f.pf_faced.get(4)),
            ),
            "jams_light": rate(
                lambda f: _is_light(f, t["jams_light"]["hand_pct"]),
                lambda f: f.hole_cards is not None and bool(f.jam.get("preflop")),
            ),
            "limper": rate(lambda f: f.pf_faced.get(1) == "call", lambda f: 1 in f.pf_faced),
            "jam_preflop": {"hits": pre_jams, "n": len(vpip_opp), "pct": _pct(pre_jams, len(vpip_opp))},
            "jam_postflop": {"hits": post_jams, "n": saw_flop, "pct": _pct(post_jams, saw_flop)},
            "folds_to_cbet": rate(lambda f: bool(f.fold_to_cbet.get("flop")), lambda f: bool(f.fold_to_cbet_opp.get("flop"))),
            "cbet_flop": rate(lambda f: bool(f.cbet.get("flop")), lambda f: bool(f.cbet_opp.get("flop"))),
            "checks_back": shown(lambda f: any(f.check_back.values()) and f.wtsd, {"strong"}),
            "donks": rate(lambda f: bool(f.donk.get("flop")), lambda f: bool(f.donk_opp.get("flop"))),
            "size_tell": _size_tell_measure(facts),
        },
    }


def _size_tell_measure(facts: list[Facts]) -> dict:
    """Strong share of shown first bets, small (under 3/4 pot) against big."""
    small = big = Counter()
    streets: Counter[str] = Counter()
    for f in facts:
        if not f.wtsd:
            continue
        s = strength(f)
        if s is None:
            continue
        for street, bucket in f.bet_size.items():
            side = small if bucket in ("small", "medium") else big
            side["n"] += 1
            side["hits"] += s == "strong"
            streets[street] += 1
    return {
        "small": {"hits": small["hits"], "n": small["n"], "pct": _pct(small["hits"], small["n"])},
        "big": {"hits": big["hits"], "n": big["n"], "pct": _pct(big["hits"], big["n"])},
        "street": streets.most_common(1)[0][0] if streets else None,
    }


# --------------------------------------------------------------- exploits ---


def _exploits(p: dict) -> list[dict]:
    """The exploit tags the profile supports, in display order: river reads first,
    then preflop, then the c-bet game, then lines, then the sizing tell."""
    t = THRESHOLDS
    r = p["rules"]
    out: list[dict] = []

    def rate_tag(id: str, label: str, m: dict, above: bool, tip: str, filter: str, by: str = "preflop") -> None:
        th = t[id]
        if m["n"] < th["n"] or m["pct"] is None:
            return
        if (m["pct"] >= th["pct"]) if above else (m["pct"] <= th["pct"]):
            out.append(_tag(id, label, "exploit", tip.format(**m), m["n"], m["hits"], filter, by))

    m = r["no_bluff"]
    rate_tag("no_bluff", "NO BLUFF", m, False,
             "Bet or raised the river and showed air {pct}% of the time ({hits} of {n} shown). "
             "Fold to their river bets without a strong hand.", "aggressor_river,wtsd", "made")
    rate_tag("bluffs_river", "BLUFFS RIVER", m, True,
             "Bet or raised the river and showed air {pct}% of the time ({hits} of {n} shown). "
             "Call down.", "aggressor_river,wtsd", "made")
    rate_tag("calls_down_light", "CALLS DOWN LIGHT", r["calls_down_light"], True,
             "Called a river bet and showed a weak hand {pct}% of the time ({hits} of {n} shown). "
             "Value bet thin; do not bluff the river.", "called_bet_river,wtsd", "made")
    rate_tag("folds_river", "FOLDS RIVER", r["folds_river"], True,
             "Folded to a river bet {pct}% of the time ({hits} of {n}). Bet the river.",
             "faced_bet_river")
    rate_tag("folds_to_3bet", "FOLDS TO 3BET", r["folds_to_3bet"], True,
             "Opened and folded to a 3-bet {pct}% of the time ({hits} of {n}). 3-bet them light.",
             "faced_3bet")

    # Inelastic to a 3-bet: rarely folds one, or has shown up calling with junk.
    th = t["inelastic_vs_3bet"]
    f3, light = r["folds_to_3bet"], r["called_3bet_light"]
    reasons = []
    if f3["n"] >= th["n"] and f3["pct"] is not None and f3["pct"] <= th["pct"]:
        reasons.append(f"opened and folded to a 3-bet only {f3['pct']}% of the time ({f3['hits']} of {f3['n']})")
    if light["hits"] >= th["hits"] and light["pct"] >= th["light_pct"]:
        reasons.append(
            f"called a 3-bet or 4-bet with a bottom-{100 - th['hand_pct']:.0f}% hand "
            f"{light['hits']} of {light['n']} shown times"
        )
    if reasons:
        m = f3 if reasons[0].startswith("opened") else light
        out.append(_tag("inelastic_vs_3bet", "INELASTIC VS 3BET", "exploit",
                        "; ".join(reasons).capitalize() + ". 3-bet for value only, and bigger.",
                        m["n"], m["hits"], "faced_3bet_any=call"))

    for id, label, verb, advice, filter in (
        ("fourbets_light", "4BETS LIGHT", "4-bet", "Call their 4-bets wider; 5-bet for value.", "4bet"),
        ("jams_light", "JAMS LIGHT", "jammed preflop", "Call their jams wider.", "jam_preflop"),
    ):
        m = r[id]
        if m["hits"] >= t[id]["hits"] and m["pct"] >= t[id]["light_pct"]:
            out.append(_tag(id, label, "exploit",
                            f"{verb.capitalize()} with a bottom-half hand {m['hits']} of {m['n']} shown times. {advice}",
                            m["n"], m["hits"], filter))

    rate_tag("limper", "LIMPER", r["limper"], True,
             "Limped {pct}% of unopened pots ({hits} of {n}). Iso-raise.", "limp")

    th, pre, post = t["jam_happy"], r["jam_preflop"], r["jam_postflop"]
    if pre["hits"] >= th["hits"] and pre["pct"] is not None and pre["pct"] >= th["pct"]:
        out.append(_tag("jam_happy", "JAM HAPPY", "exploit",
                        f"Jammed preflop {pre['hits']} times in {pre['n']} preflop decisions ({pre['pct']}%). "
                        "Wait for a hand and let them jam into it.", pre["n"], pre["hits"], "jam"))
    elif post["hits"] >= th["post_hits"] and post["pct"] is not None and post["pct"] >= th["post_pct"]:
        out.append(_tag("jam_happy", "JAM HAPPY", "exploit",
                        f"Jammed after the flop in {post['hits']} of {post['n']} flops seen ({post['pct']}%). "
                        "Wait for a hand and let them jam into it.", post["n"], post["hits"], "jam"))

    m = r["folds_to_cbet"]
    rate_tag("folds_to_cbet", "FOLDS TO CBET", m, True,
             "Folded to a flop c-bet {pct}% of the time ({hits} of {n}). C-bet every flop.", "faced_cbet_flop")
    rate_tag("sticky_vs_cbet", "STICKY VS CBET", m, False,
             "Folded to a flop c-bet only {pct}% of the time ({hits} of {n}). C-bet for value, not as a bluff.",
             "faced_cbet_flop")
    m = r["cbet_flop"]
    rate_tag("auto_cbet", "AUTO CBET", m, True,
             "C-bet the flop {pct}% of the time ({hits} of {n}). Float and raise their c-bets.", "cbet_flop_opp")
    rate_tag("rarely_cbets", "RARELY CBETS", m, False,
             "C-bet the flop only {pct}% of the time ({hits} of {n}). Their flop bet is a hand; bet when they check.",
             "cbet_flop_opp")
    m = r["checks_back"]
    rate_tag("checks_back_weak", "CHECKS BACK WEAK", m, False,
             "Checked back and showed a strong hand only {pct}% of the time ({hits} of {n} shown). "
             "Lead the next street after they check back.", "check_back,wtsd", "made")
    rate_tag("traps", "TRAPS", m, True,
             "Checked back and showed a strong hand {pct}% of the time ({hits} of {n} shown). "
             "Do not lead into a check-back.", "check_back,wtsd", "made")
    rate_tag("donks", "DONKS", r["donks"], True,
             "Led into the aggressor on {pct}% of flops ({hits} of {n}). Expect leads; raise them.", "donk_flop")

    th, m = t["size_tell"], r["size_tell"]
    small, big = m["small"], m["big"]
    if small["n"] >= th["n"] and big["n"] >= th["n"]:
        gap = big["pct"] - small["pct"]
        if abs(gap) >= th["gap"]:
            strong = gap > 0
            out.append(_tag(
                "size_tell", "BIG = STRONG" if strong else "BIG = BLUFF", "exploit",
                f"Big bets showed a strong hand {big['pct']}% of the time against {small['pct']}% for "
                f"small ones ({big['n']} and {small['n']} shown). "
                + ("Fold to big bets; call small ones." if strong else "Call big bets; respect small ones."),
                big["n"] + small["n"], big["hits"] + small["hits"], "wtsd",
                "sizing", street=m["street"], kind="bet",
            ))
    return out


# ------------------------------------------------------------- archetype ----


def _archetype(p: dict, exploits: list[dict]) -> dict | None:
    """One overarching label, first rule that fits. Silent under MIN_ARCHETYPE_OPPS."""
    if p["vpip_opp"] < MIN_ARCHETYPE_OPPS:
        return None
    t = THRESHOLDS
    r = p["rules"]
    vx, px, ratio = p["vpip_excess"], p["pfr_excess"], p["pfr_ratio"] or 0.0
    af, jam, three = p["af_flop"]["pct"], r["jam_preflop"]["pct"], p["3bet"]["pct"]
    fold_cbet, wtsd = r["folds_to_cbet"], p["wtsd"]
    n = p["vpip_opp"]
    base = f"VPIP {vx:+.0f} and PFR {px:+.0f} points from the baseline for their table sizes, raising {ratio:.0%} of the hands they play ({n} preflop decisions)."

    def tag(id: str, label: str, tip: str) -> dict:
        return _tag(id, label, "archetype", tip, n, n, "acted_preflop")

    th = t["maniac"]
    if px >= th["pfr_excess"] and (
        (af is not None and af >= th["af_flop"])
        or (jam is not None and jam >= th["jam_pct"])
        or (three is not None and three >= th["3bet"])
    ):
        return tag("maniac", "MANIAC", base + " Raises and jams far more than the table. Let them bluff into a made hand.")
    th = t["station"]
    if (
        fold_cbet["n"] >= th["fold_to_cbet_n"] and fold_cbet["pct"] is not None and fold_cbet["pct"] <= th["fold_to_cbet"]
        and wtsd["n"] >= th["wtsd_n"] and wtsd["pct"] is not None and wtsd["pct"] >= th["wtsd"]
        and ratio < th["pfr_ratio"]
    ):
        return tag("station", "STATION", base + f" Folds to a flop c-bet {fold_cbet['pct']}% and reaches showdown {wtsd['pct']}% of the time. Value bet three streets; never bluff.")
    th = t["fish"]
    if vx >= th["vpip_excess"] and ratio < th["pfr_ratio"]:
        return tag("fish", "FISH", base + " Loose and passive. Value bet relentlessly.")
    th = t["lag"]
    if vx >= th["vpip_excess"] and ratio >= th["pfr_ratio"]:
        return tag("lag", "LAG", base + " Loose and aggressive. Tighten up, then let them barrel.")
    if vx <= t["nit"]["vpip_excess"]:
        return tag("nit", "NIT", base + " Tight. Steal their blinds; fold to their raises.")
    if p["hands"] >= BALANCED_HANDS and not exploits:
        return tag("balanced", "BALANCED", base + f" No exploit stands out over {p['hands']} hands. Play solid.")
    return tag("reg", "REG", base + " Solid overall; the exploit tags say where the gaps are.")


# ------------------------------------------------------------------ fun -----


def _fun(facts: list[Facts]) -> list[dict]:
    out = []
    for name, term, classes in FUN_HANDS:
        raised = [f for f in facts if f.hole_cards and f.pfr and hand_class(f.hole_cards) in classes]
        jams = [f for f in raised if f.jam.get("preflop")]
        if jams:
            out.append(_tag(
                f"fun_{term}", f"{name} ALL-IN-PRE ×{len(jams)}", "fun",
                f"Jammed preflop with {name} {len(jams)} times (shown), raised it {len(raised)} times in all.",
                len(jams), len(jams), f"hand={term},jam_preflop",
            ))
        elif len(raised) >= THRESHOLDS["fun"]["hits"]:
            out.append(_tag(
                f"fun_{term}", f"{name} RAISES ×{len(raised)}", "fun",
                f"Raised preflop with {name} {len(raised)} times (shown).",
                len(raised), len(raised), f"hand={term},pfr",
            ))
    return out


# --------------------------------------------------------------- entry ------


def tags_for(facts: Iterable[Facts]) -> dict:
    """Every tag the hands support: ``archetype`` (or None), the ordered ``tags``
    list with the archetype first and the fun ones last, and the ``profile`` the
    rules were judged on."""
    rows = list(facts)
    if not rows:
        return {"archetype": None, "tags": [], "profile": {"hands": 0}}
    p = profile(rows)
    exploits = _exploits(p)
    arch = _archetype(p, exploits)
    return {
        "archetype": arch,
        "tags": [*([arch] if arch else []), *exploits, *_fun(rows)],
        "profile": p,
    }
