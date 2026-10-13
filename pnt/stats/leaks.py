"""The hero's leaks: the stats of their own game furthest out of line, and the cost.

The tags turn a player's numbers into exploits for use against them. This turns
the hero's numbers on themselves: each of twelve stats is held against a **band**
of ordinary values, and a stat outside its band is a candidate leak. Beside them,
the fold-equity tables (`fold_equity.py`) give the hero's defense by the size of
the bet faced, held against that size's break-even fold share: over-folding to a
size is a leak any bluff can take advantage of.

Every candidate carries:

- its value, sample and **Wilson interval** (`queries.wilson`), and how much of
  that interval sits outside the band, the **confidence**: 1 when the whole
  95% range is outside, near 0 when the band is mostly inside it;
- the **filter** that finds its opportunities (`n`) and the **leak filter** that
  finds the hands on the wrong side of it (`leak_n`), both on the hero's chart;
- a **cost** in bb per 100 of the hero's hands: the hands beyond the band's edge,
  each valued at what the leak filter's hands made against the hero's average
  hand. For an over-fold, what a pure bluff of that size wins off the hero beyond
  the band's edge, which is the fold-equity table's own measure.

The leaks rank by cost × confidence, and the top three are what the stats page
shows. The hero is `games.hero_pn_id`, the owner of the downloaded logs.

The bands are judgement values, the same kind `tags.THRESHOLDS` holds, and reuse
its cutoffs where a tag already draws the line. The other way, measuring against
the pool's winning players, needs more winners than a pool of nine has: two or
three players win over enough hands to judge, so "what winners do" would be one
or two people's habits. See SPEC.md, "Leak finder".
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterable

from .derive import Facts
from .filters import parse_filter
from .fold_equity import MIN_SAMPLE, break_even, fold_equity
from .queries import facts_cached, identity_map, rates, tally, wilson
from .tags import BASELINES, DEFAULT_BASELINE, MIN_ARCHETYPE_OPPS, THRESHOLDS

#: A rate leak is only judged on this many opportunities: the smallest sample
#: most exploit tags fire on. VPIP and PFR, measured on every preflop decision,
#: take the archetype's 100.
MIN_N = 25

#: How far either side of the table-size baseline VPIP and PFR may sit, in points:
#: VPIP's are the NIT and LAG cutoffs, PFR's upper the MANIAC one. PFR's lower is
#: a judgement value.
VPIP_BAND = (THRESHOLDS["nit"]["vpip_excess"], THRESHOLDS["lag"]["vpip_excess"])
PFR_BAND = (-8.0, THRESHOLDS["maniac"]["pfr_excess"])

#: The rate leaks: (key, label, what it counts, opportunity filter, hit term, band
#: low, band high). Bands in percent;
#: VPIP and PFR's are offsets from the baseline (see `_band`). Where a tag draws
#: the line, the band uses its cutoff; the rest are judgement values, recorded in
#: SPEC.md.
RATES: tuple[tuple[str, str, str, str, str, float, float], ...] = (
    ("vpip", "VPIP", "put chips in voluntarily preflop", "acted_preflop", "vpip", *VPIP_BAND),
    ("pfr", "PFR", "raised preflop", "acted_preflop", "pfr", *PFR_BAND),
    # 18: MANIAC's 3-bet cutoff.
    ("3bet", "3-bet", "re-raised an open", "3bet_opp", "3bet", 6, THRESHOLDS["maniac"]["3bet"]),
    # 55: STEALS WIDE.
    ("ats", "ATS", "opened from a steal seat when it folded to them", "steal_opp", "steal",
     30, THRESHOLDS["steals_wide"]["pct"]),
    # 35: FOLDS TO STEAL.
    ("fold_to_steal", "Fold to steal", "folded a blind to a steal", "fold_to_steal_opp", "fold_to_steal",
     15, THRESHOLDS["folds_to_steal"]["pct"]),
    # 35 and 60: RARELY CBETS and AUTO CBET.
    ("cbet_flop", "Flop c-bet", "c-bet the flop", "cbet_flop_opp", "cbet_flop",
     THRESHOLDS["rarely_cbets"]["pct"], THRESHOLDS["auto_cbet"]["pct"]),
    # 30 and 50: STICKY VS CBET and FOLDS TO CBET.
    ("fold_to_cbet_flop", "Fold to flop c-bet", "folded to a flop c-bet", "faced_cbet_flop",
     "folded_to_cbet_flop", THRESHOLDS["sticky_vs_cbet"]["pct"], THRESHOLDS["folds_to_cbet"]["pct"]),
    # 38: STATION's WTSD.
    ("wtsd", "WTSD", "went to showdown after seeing the flop", "saw_flop", "wtsd",
     25, THRESHOLDS["station"]["wtsd"]),
    ("wsd", "W$SD", "won money at showdown", "wtsd", "won_sd", 46, 58),
    ("wwsf", "WWSF", "won the pot after seeing the flop", "saw_flop", "won_pot", 40, 55),
    ("probe_turn", "Turn probe", "bet the turn into a raiser who checked the flop back",
     "probe_turn_opp", "probe_turn", 20, 50),
    ("float_flop", "Flop float", "bet the flop when the preflop raiser checked to them",
     "float_flop_opp", "float_flop", 20, 55),
)

#: The fold-equity band, in points either side of a size's break-even fold share:
#: folding more than `OVERFOLD` over it hands every bluff that size a profit;
#: folding more than `UNDERFOLD` under it is calling a lot more than the bet
#: demands. Judgement values. Under-folding is wider because a defender with
#: equity should fold less than a pure bluff's break-even.
OVERFOLD = 5.0
UNDERFOLD = 25.0

#: How many leaks the page and the CLI lead with.
TOP = 3

NOTE = (
    "Bands are judgement values, the kind tags use, and reuse a tag's cutoff where one exists. "
    "They are not what the pool's winners do: a pool of nine has two or three players who win "
    "over enough hands to judge, so a winners' baseline would be one or two people's habits. "
    "A leak's cost compares the hands its filter finds with your average hand, and only for the "
    "hands beyond the band's edge: an association, not a proof that playing those hands "
    "differently would have won that much. Folded hands always lose what was already in, and "
    "the weakest hands are the ones folded, so a fold-side cost runs high. An over-fold's cost is "
    "what a pure bluff of that size wins off you, from the fold-equity tables."
)


def _pct(num: int, den: int) -> float | None:
    return round(100.0 * num / den, 1) if den else None


def _round(x: float | None, nd: int = 2) -> float | None:
    return round(x, nd) if x is not None else None


def hero_of(conn: sqlite3.Connection, game: str | None = None) -> str | None:
    """The hero's alias: the player whose identity owns the most logs
    (`games.hero_pn_id`), in one game when `game` is given. None when no log
    names a hero, or the hero has no alias yet."""
    where, params = "WHERE hero_pn_id IS NOT NULL", ()
    if game:
        where, params = where + " AND game_id = ?", (game,)
    ident = identity_map(conn)
    logs: Counter = Counter()
    for r in conn.execute(f"SELECT hero_pn_id, COUNT(*) AS n FROM games {where} GROUP BY hero_pn_id", params):
        hit = ident.get(r["hero_pn_id"])
        if hit is not None:
            logs[hit[1]] += r["n"]
    if not logs:
        return None
    # Most logs, then the alias, so a tie always names the same person.
    return min(logs, key=lambda a: (-logs[a], a.casefold()))


def _band(key: str, lo: float, hi: float, facts: list[Facts]) -> tuple[float, float] | None:
    """The band in percent. VPIP and PFR's are offsets from the mean baseline over
    the hero's own preflop decisions, each at its own table size (`tags.BASELINES`)."""
    if key not in ("vpip", "pfr"):
        return (float(lo), float(hi))
    base = [BASELINES.get(f.n_dealt_in, DEFAULT_BASELINE)[key == "pfr"] for f in facts if f.vpip_opp]
    if not base:
        return None
    mid = sum(base) / len(base)
    return (round(max(0.0, mid + lo), 1), round(min(100.0, mid + hi), 1))


def confidence(ci: tuple[float, float] | None, band: tuple[float, float]) -> float:
    """How much of a Wilson interval (fractions) lies outside the band (percent):
    1 when it all does, 0 when none does. The share, not the distance, so a wide
    interval from a small sample is never more than partly out."""
    if ci is None:
        return 0.0
    lo, hi = 100.0 * ci[0], 100.0 * ci[1]
    width = hi - lo
    outside = max(0.0, min(hi, band[0]) - lo) + max(0.0, hi - max(lo, band[1]))
    if width <= 0:
        return 0.0
    return round(min(1.0, outside / width), 3)


def _side(value: float | None, band: tuple[float, float] | None) -> str | None:
    if value is None or band is None:
        return None
    if value > band[1]:
        return "high"
    if value < band[0]:
        return "low"
    return None


def _score(cost: float | None, conf: float) -> float:
    return round(max(0.0, -cost) * conf, 3) if cost is not None else 0.0


def _rate_leak(
    spec: tuple, facts: list[Facts], overall: dict, total_bb: int
) -> dict:
    key, label, counts, opp, hit, lo, hi = spec
    opps = [f for f in facts if parse_filter(opp)(f)]
    hit_pred = parse_filter(hit)
    hits = sum(1 for f in opps if hit_pred(f))
    n = len(opps)
    value = _pct(hits, n)
    band = _band(key, lo, hi, facts)
    side = _side(value, band)
    ci = wilson(hits, n)
    min_n = MIN_ARCHETYPE_OPPS if key in ("vpip", "pfr") else MIN_N
    leak_filter = (f"{opp},{hit}" if side == "high" else f"{opp},!{hit}") if side else None
    row = {
        "key": key,
        "family": key,
        "kind": "rate",
        "label": label,
        "counts": counts,
        "value": value,
        "hits": hits,
        "n": n,
        "band": list(band) if band else None,
        "ci": [round(100 * ci[0], 1), round(100 * ci[1], 1)] if ci else None,
        "side": side,
        "judged": n >= min_n,
        "min_n": min_n,
        "filter": opp,
        "leak_filter": leak_filter,
        "leak_n": None,
        "confidence": confidence(ci, band) if side else 0.0,
        "excess": None,
        "bb_per_100": None,
        "bb_hands": None,
        "cost_bb": None,
        "cost_bb_per_100": None,
        "view": None,
        "street": None,
    }
    if side:
        pred = parse_filter(leak_filter)
        mine = [f for f in opps if pred(f)]
        r = rates(tally(mine)) if mine else {}
        edge = band[1] if side == "high" else band[0]
        excess = abs(value - edge) / 100.0 * n
        row["leak_n"] = len(mine)
        row["excess"] = round(excess, 1)
        row["bb_per_100"] = r.get("bb_per_100")
        row["bb_hands"] = tally(mine).bb_n if mine else 0
        if row["bb_per_100"] is not None and overall.get("bb_per_100") is not None and total_bb:
            # Each hand beyond the edge, valued at what the leak's hands made over the
            # hero's average hand, in bb; then over every hand, per 100.
            cost_bb = excess * (row["bb_per_100"] - overall["bb_per_100"]) / 100.0
            row["cost_bb"] = round(cost_bb, 1)
            row["cost_bb_per_100"] = round(100.0 * cost_bb / total_bb, 2)
    row["score"] = _score(row["cost_bb_per_100"], row["confidence"]) if row["judged"] else 0.0
    row["tip"] = _rate_tip(row)
    return row


def _rate_tip(r: dict) -> str:
    if r["value"] is None:
        return f"{r['label']}: never had the chance."
    band = f"{r['band'][0]:g}-{r['band'][1]:g}%" if r["band"] else "no band"
    head = f"{r['label']} {r['value']}% ({r['hits']} of {r['n']}), band {band}"
    if not r["side"]:
        return head + ": inside it."
    word = "too often" if r["side"] == "high" else "too rarely"
    return f"{head}: {r['counts']} {word}."


def _defense_leaks(facts: list[Facts], total_bb: int, overall: dict) -> list[dict]:
    """One candidate per street and size the hero faced a bet at, from the fold-equity
    defender's table: fold share against `[break-even - UNDERFOLD, break-even + OVERFOLD]`."""
    out = []
    fe = fold_equity(facts)
    for street, data in fe["streets"].items():
        for row in data["faced"]:
            n, size = row["n"], row["size"]
            if not n or row["break_even"] is None:
                continue
            be = row["break_even"]
            band = (round(max(0.0, be - UNDERFOLD), 1), round(min(100.0, be + OVERFOLD), 1))
            value = _pct(row["folds"], n)
            side = _side(value, band)
            ci = wilson(row["folds"], n)
            leak = {
                "key": f"faced_bet_{street}_{size}",
                # Every size on one street, one side of the band, is one habit.
                "family": f"faced_bet_{street}_{side}",
                "kind": "fold_equity",
                "label": f"Fold to {size} {street} bets",
                "counts": f"folded to a {size} {street} bet or raise",
                "value": value,
                "hits": row["folds"],
                "n": n,
                "band": list(band),
                "break_even": be,
                "ci": [round(100 * ci[0], 1), round(100 * ci[1], 1)] if ci else None,
                "side": side,
                "judged": n >= MIN_SAMPLE,
                "min_n": MIN_SAMPLE,
                "filter": row["filter"],
                # No term reads the first decision facing a bet (fold_equity.py), so
                # the folds have no exact filter: the row's own one is the link.
                "leak_filter": None,
                "leak_n": row["folds"] if side == "high" else (n - row["folds"]) if side else None,
                "confidence": confidence(ci, band) if side else 0.0,
                "excess": None,
                "bb_per_100": None,
                "bb_hands": None,
                "cost_bb": None,
                "cost_bb_per_100": None,
                "view": "sizing",
                "street": street,
            }
            if side:
                edge = band[1] if side == "high" else band[0]
                leak["excess"] = round(abs(value - edge) / 100.0 * n, 1)
                mine = [f for f in facts if f.faced_bet_size.get(street) == size]
                if side == "high" and total_bb:
                    # What a pure bluff wins off them beyond the band's edge: per bet of
                    # s pots into P, (fold share - break-even_s - OVERFOLD) * (1 + s) * P,
                    # P the pot when the street was dealt, in that hand's big blinds.
                    p = row["folds"] / n
                    won = sum(
                        (p - break_even(f.faced_bet_pot[street]) - OVERFOLD / 100.0)
                        * (1 + f.faced_bet_pot[street]) * f.pot_at.get(street, 0) / f.bb_size
                        for f in mine if f.bb_size
                    )
                    leak["cost_bb"] = round(-max(0.0, won), 1)
                    leak["cost_bb_per_100"] = round(100.0 * leak["cost_bb"] / total_bb, 2)
                elif side == "low":
                    cont = [f for f in mine if f.faced_bet_action.get(street) in ("call", "raise")]
                    r = rates(tally(cont)) if cont else {}
                    leak["bb_per_100"] = r.get("bb_per_100")
                    leak["bb_hands"] = tally(cont).bb_n if cont else 0
                    if leak["bb_per_100"] is not None and overall.get("bb_per_100") is not None and total_bb:
                        cost_bb = leak["excess"] * (leak["bb_per_100"] - overall["bb_per_100"]) / 100.0
                        leak["cost_bb"] = round(cost_bb, 1)
                        leak["cost_bb_per_100"] = round(100.0 * cost_bb / total_bb, 2)
            leak["score"] = _score(leak["cost_bb_per_100"], leak["confidence"]) if leak["judged"] else 0.0
            word = {"high": "folds more than", "low": "folds well under", None: "folds about"}[side]
            leak["tip"] = (
                f"Folded to {size} {street} bets {value}% of the time ({row['folds']} of {n}), "
                f"break-even {be}%: {word} a pure bluff that size needs."
            )
            out.append(leak)
    return out


def rank(candidates: Iterable[dict]) -> list[dict]:
    """The judged leaks outside their band, costliest-and-surest first. Ties keep
    the candidates' own order (the RATES order, then the fold-equity tables), so
    the ranking is the same on every call."""
    rows = [r for r in candidates if r["side"] and r["judged"]]
    return [r for _, r in sorted(enumerate(rows), key=lambda ir: (-ir[1]["score"], ir[0]))]


def top_leaks(ranked: list[dict], k: int = TOP) -> list[dict]:
    """The `k` costliest leaks, one per habit (`family`): over-folding to three sizes
    of river bet is one leak, led by its costliest size, with the others named in
    `also`. A leak with no visible cost is never in the top: it is out of line, but
    the hands it finds did no worse than the hero's average."""
    out: list[dict] = []
    seen: dict[str, dict] = {}
    for r in ranked:
        if r["score"] <= 0:
            continue
        lead = seen.get(r["family"])
        if lead is not None:
            lead["also"].append(r["label"])
            continue
        if len(out) < k:
            lead = {**r, "also": []}
            seen[r["family"]] = lead
            out.append(lead)
    return out


def leaks_for(facts: Iterable[Facts]) -> dict:
    """Every candidate leak in one player's hands, and the ranked ones. The engine
    behind `leaks`, for a caller that already has the hands."""
    facts = list(facts)
    t = tally(facts)
    overall = rates(t) if t.hands else {"hands": 0}
    total_bb = t.bb_n
    candidates = [_rate_leak(spec, facts, overall, total_bb) for spec in RATES]
    candidates += _defense_leaks(facts, total_bb, overall)
    ranked = rank(candidates)
    top = top_leaks(ranked)
    return {
        "hands": t.hands,
        # The hands behind every bb figure: complete, with a known big blind.
        "bb_hands": total_bb,
        "bb_per_100": overall.get("bb_per_100"),
        "bb_per_100_ci": overall.get("bb_per_100_ci"),
        "top": top,
        "ranked": ranked,
        "candidates": candidates,
        "note": NOTE,
    }


def leaks(conn: sqlite3.Connection, game: str | None = None) -> dict:
    """The hero's leaks, over every hand they played or one game's. No hero -- no
    log names its owner -- is an empty result, not an error."""
    hero = hero_of(conn, game)
    if hero is None:
        return {"hero": None, "game": game, "hands": 0, "bb_hands": 0, "bb_per_100": None,
                "bb_per_100_ci": None, "top": [], "ranked": [], "candidates": [], "note": NOTE}
    return {"hero": hero, "game": game, **leaks_for(facts_cached(conn, hero, game))}
