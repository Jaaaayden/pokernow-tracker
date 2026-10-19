"""Every rate the HUD can show, and which it shows unless told otherwise.

The HUD prints a short label per column and spells the stat out on hover, so
each entry carries both. `opp` is the `_opp` count under the rate -- the sample
the HUD's drift marking and "of n" text read -- and names a *different* key
where `rates()` shares one: PFR sits over VPIP's decisions, a raised c-bet over
the c-bets faced. None where `rates()` keeps no count: W$SD and bb/100.

`hands` always comes first on the HUD and is not a choice, so it is not here.
The choice itself is stored by the server as the `hud_columns` setting.
"""

from __future__ import annotations

GROUPS = ("Preflop", "Flop", "Turn", "River", "Showdown")

#: Today's seven after `hands`: the HUD before it could be changed.
DEFAULT = ["vpip", "pfr", "3bet", "fold_to_3bet", "cbet_flop", "wtsd", "af_flop"]
#: More than this and the compact table outgrows the panel.
MAX = 12

_PREV = {"flop": "preflop", "turn": "flop", "river": "turn"}
#: Flop columns keep the plain label; the later streets are marked t / r.
_MARK = {"flop": "", "turn": "t", "river": "r"}


def _entry(key: str, label: str, tip: str, opp: str | None, group: str) -> dict:
    return {"key": key, "label": label, "tip": tip, "opp": opp, "group": group}


def _street(s: str) -> list[dict]:
    p, m, group = _PREV[s], _MARK[s], s.capitalize()
    cbet_tip = (
        "Continuation bet: they raised preflop and then bet the flop first-in."
        if s == "flop"
        else f"Continuation bet: they were the {p} aggressor and bet the {s} first-in."
    )
    out = [
        _entry(f"cbet_{s}", f"{m}CBet", cbet_tip, f"cbet_{s}", group),
        _entry(f"fold_to_cbet_{s}", f"{m}FCB",
               f"Fold to c-bet: the {p} aggressor c-bet the {s} at them, and they folded.",
               f"fold_to_cbet_{s}", group),
        _entry(f"raise_cbet_{s}", f"{m}RCB",
               f"Raise c-bet: over the same {s} c-bets as FCB, how often they raised.",
               f"fold_to_cbet_{s}", group),
        _entry(f"donk_{s}", f"{m}Donk",
               f"Lead (donk): they bet the {s} into the {p} aggressor before that player acted.",
               f"donk_{s}", group),
        _entry(f"check_raise_{s}", f"{m}XR",
               f"Check-raise: they checked the {s}, faced a bet, and raised.",
               f"check_raise_{s}", group),
        _entry(f"af_{s}", f"{m}AFq",
               f"Aggression frequency (AFq; a percentage, not the AF ratio) on the {s}: of everything they did bar checking, how often "
               "it was a bet or a raise rather than a call or a fold.",
               f"af_{s}", group),
    ]
    if s != "flop":
        out += [
            _entry(f"delayed_cbet_{s}", f"{m}DCB",
                   f"Delayed c-bet: they raised preflop, every street before the {s} checked through, "
                   f"and they bet the {s} first-in.",
                   f"delayed_cbet_{s}", group),
            _entry(f"probe_{s}", f"{m}Prob",
                   f"Probe: the preflop raiser checked the {p} back, and they bet the {s} into them.",
                   f"probe_{s}", group),
            _entry(f"fold_to_probe_{s}", f"{m}F2P",
                   f"Fold to probe: they raised preflop, checked the {p} back, and folded to a {s} probe.",
                   f"fold_to_probe_{s}", group),
        ]
    float_tip = (
        "Float: they called the preflop raise, the raiser checked the flop to them, and they bet."
        if s == "flop"
        else f"Float: they called on the {p}, its aggressor checked the {s} to them, and they bet."
    )
    out.append(_entry(f"float_{s}", f"{m}Flt", float_tip, f"float_{s}", group))
    return out


CATALOG: list[dict] = [
    _entry("vpip", "VPIP", "Voluntarily Put $ In Pot: how often they call, bet or raise preflop. "
           "Blinds are forced and never count. High means loose.", "vpip", "Preflop"),
    _entry("pfr", "PFR", "Pre-Flop Raise: how often they raise preflop. Close to VPIP means aggressive; "
           "far below it means they call far more than they raise.", "vpip", "Preflop"),
    _entry("3bet", "3Bet", "How often they re-raise someone's open.", "3bet", "Preflop"),
    _entry("fold_to_3bet", "F3B", "Fold to Three-Bet: they opened, someone re-raised, and they folded.",
           "fold_to_3bet", "Preflop"),
    _entry("ats", "ATS", "Attempt To Steal: folded to them in the CO, BTN or SB, how often they raised.",
           "ats", "Preflop"),
    _entry("fold_to_steal", "F2S", "Fold to Steal: in a blind, facing a steal nobody called, how often they folded.",
           "fold_to_steal", "Preflop"),
    _entry("3bet_vs_steal", "3BvS", "Three-Bet vs Steal: over the same steals as F2S, how often they re-raised.",
           "3bet_vs_steal", "Preflop"),
    _entry("squeeze", "SQZ", "Squeeze: an open and a caller in front of them, how often they re-raised.",
           "squeeze", "Preflop"),
    _entry("cold_call", "CC", "Cold Call: they called an open with no blind or limp in. High means a capped range.",
           "cold_call", "Preflop"),
    _entry("limp_reraise", "LRR", "Limp-Reraise: they limped, someone raised, and they re-raised.",
           "limp_reraise", "Preflop"),
    _entry("jam_pf", "JAM", "Preflop jam: how often they bet or raised all-in, over the same decisions as VPIP.",
           "jam_pf", "Preflop"),
    *_street("flop"),
    *_street("turn"),
    *_street("river"),
    _entry("wtsd", "WTSD", "Went To Showdown: of the flops they saw, how often they were still there at the end.",
           "wtsd", "Showdown"),
    _entry("wsd", "W$SD", "Won $ at Showdown: of the showdowns they reached, how often they won money.",
           None, "Showdown"),
    _entry("wwsf", "WWSF", "Won When Saw Flop: of the flops they saw, how often they took the pot, "
           "showdown or not.", "wwsf", "Showdown"),
    _entry("bb_per_100", "bb100", "Big blinds won per 100 hands. Positive is profit; a log that stopped "
           "mid-hand leaves that hand out.", None, "Showdown"),
]

BY_KEY: dict[str, dict] = {c["key"]: c for c in CATALOG}


def chosen(stored: object) -> list[str]:
    """The columns to show: the stored list without any key no longer in the
    catalog, or DEFAULT when nothing usable is stored. An empty list stored is
    a choice -- `hands` alone -- and stays empty."""
    if not isinstance(stored, list):
        return list(DEFAULT)
    keys = [k for k in stored if isinstance(k, str) and k in BY_KEY]
    return keys if keys or not stored else list(DEFAULT)


def check(columns: list[str]) -> None:
    """Raise ValueError unless `columns` is a list a user may store."""
    unknown = [k for k in columns if k not in BY_KEY]
    if unknown:
        raise ValueError(f"not a HUD stat: {', '.join(unknown)}")
    if len(set(columns)) != len(columns):
        raise ValueError("a stat is listed twice")
    if len(columns) > MAX:
        raise ValueError(f"at most {MAX} stats")
