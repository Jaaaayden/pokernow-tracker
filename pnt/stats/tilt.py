"""Tilt and session dynamics: how a player plays after a big loss, and as the night wears on.

"He just lost a 100bb pot, so the next twenty hands are a different player." No
single hand can say that: it is context *across* hands. So `annotate` makes one
pass over a player's hands, game by game in hand order, and writes four facts
onto each row -- hands since their last big loss and its size, their hand count
in the session, and the hours since their first hand of it. From there they are
ordinary facts: `post_loss`, `hands_since_loss<=5` and `session_hour>=3` are
filter terms like any other, and `tilt_report` is a handful of filtered tallies.

A session is a `game_id`. A loss in one game never carries into the next: a night's
sleep is the cure the window stands for.

`annotate` runs wherever a player's hands are listed (`queries.facts_for`,
`facts_cached`, `facts_by_player`, `fold_equity.pool_facts`), before any spot
filter, so the context is the player's whole session rather than the hands left
after filtering. See SPEC.md, "Session dynamics".
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from .derive import POSTFLOP_STREETS, Facts

#: A hand that lost this many big blinds or more opens a post-loss window.
#: Calibrated on the 14,720-hand database on 2026-10-12: 2.7% of complete hands
#: (1,121 of them), about one every 37 hands a player plays.
BIG_LOSS_BB = 40
#: The window: the hands after a big loss that count as "post-loss". Calibrated
#: on the same database: within each player, VPIP runs about 2 points over their
#: own other hands through the first 20 hands after a loss, and the lift fades
#: after that (W=30: +1.6, W=50: +1.2).
WINDOW = 20

#: The hour bands of the "through the night" table: (key, label, filter terms,
#: lower bound, upper bound). Each band's terms find exactly its hands.
HOUR_BANDS: tuple[tuple[str, str, str, float, float | None], ...] = (
    ("h0", "first hour", "session_hour<1", 0, 1),
    ("h1", "second hour", "session_hour>=1,session_hour<2", 1, 2),
    ("h2", "third hour", "session_hour>=2,session_hour<3", 2, 3),
    ("h3", "three hours in", "session_hour>=3", 3, None),
)


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def big_loss(f: Facts) -> float | None:
    """The loss on this hand in big blinds, positive, when it is a big one; else None.

    Only a complete hand with a known big blind has a result to judge, the rule
    bb/100 follows: a log that stops mid-hand reads as a loss that never happened.
    """
    if not f.complete or not f.bb_size:
        return None
    lost = -f.net / f.bb_size
    return lost if lost >= BIG_LOSS_BB else None


def post_loss(f: Facts) -> bool:
    """Inside the window after a big loss: one of the WINDOW hands that follow it."""
    return f.hands_since_loss is not None and f.hands_since_loss <= WINDOW


def annotate(facts: list[Facts]) -> list[Facts]:
    """Fill the session facts on every row, in place, and return the same list.

    Rows are grouped by PokerNow ID and game, and ordered by hand number within
    each. Grouping by ID rather than alias keeps one seat's run of hands one run
    even when a list holds several merged identities, and lets the same pass
    serve one player's list and the whole pool's. Idempotent, and the same for a
    game's rows whether or not other games' rows are in the list.
    """
    runs: dict[tuple[str, str], list[Facts]] = {}
    for f in facts:
        runs.setdefault((f.pn_id, f.game_id), []).append(f)
    for run in runs.values():
        run.sort(key=lambda f: (f.hand_number, f.hand_id))
        first: datetime | None = None
        last_at: int | None = None
        last_bb: float | None = None
        for i, f in enumerate(run, 1):
            f.session_hand = i
            at = _ts(f.ts)
            if first is None:
                first = at
            f.session_hour = (at - first).total_seconds() / 3600 if at is not None and first is not None else None
            f.hands_since_loss = i - last_at if last_at is not None else None
            f.loss_bb = last_bb if last_at is not None else None
            lost = big_loss(f)
            if lost is not None:
                last_at, last_bb = i, round(lost, 1)
    return facts


def next_hand(facts: Iterable[Facts], game_id: str) -> dict:
    """Whether the hand this player plays next in `game_id` falls inside a
    post-loss window, read from their latest annotated hand there.

    The latest hand counts as a loss of its own: straight after a 100bb pot the
    next hand is the first of its window. What the HUD shows as "tilt?".
    """
    latest = None
    for f in facts:
        if f.game_id == game_id and (latest is None or (f.hand_number, f.hand_id) > (latest.hand_number, latest.hand_id)):
            latest = f
    if latest is None:
        return {"post_loss": False, "since": None, "loss_bb": None}
    lost = big_loss(latest)
    if lost is not None:
        since, size = 1, round(lost, 1)
    elif latest.hands_since_loss is not None:
        since, size = latest.hands_since_loss + 1, latest.loss_bb
    else:
        return {"post_loss": False, "since": None, "loss_bb": None}
    if since > WINDOW:
        return {"post_loss": False, "since": None, "loss_bb": None}
    return {"post_loss": True, "since": since, "loss_bb": size}


def _pct(num: int, den: int) -> float | None:
    return round(100.0 * num / den, 1) if den else None


def _row(key: str, label: str, filt: str, facts: list[Facts]) -> dict:
    """One row: VPIP, PFR, postflop aggression and bb/100, each with its n."""
    from .queries import rates, tally  # queries imports this module

    t = tally(facts)
    c = t.counts
    r = rates(t) if t.hands else {}
    agg = sum(c["aggressive", s] for s in POSTFLOP_STREETS)
    den = sum(c["agg_denom", s] for s in POSTFLOP_STREETS)
    return {
        "key": key,
        "label": label,
        # The terms that find exactly these hands, added to the spot.
        "filter": filt,
        "hands": t.hands,
        "vpip": r.get("vpip"),
        "vpip_n": c["vpip_opp"],
        "pfr": r.get("pfr"),
        "pfr_n": c["pfr_opp"],
        # Aggression frequency over every postflop street together: bets and
        # raises over bets, raises, calls and folds. An action count, not hands.
        "af": _pct(agg, den),
        "af_n": den,
        "bb_hands": t.bb_n,
        "net_bb": r.get("net_bb"),
        "bb_per_100": r.get("bb_per_100"),
        "bb_per_100_ci": r.get("bb_per_100_ci"),
    }


def tilt_report(facts: Iterable[Facts]) -> dict:
    """One player's play after a big loss and through the night, in a spot.

    `facts` must already be annotated (every list `queries` hands out is). Two
    tables: all hands against the post-loss window and every other hand, and the
    session-hour bands. The bands cover only hands with a timestamp, so `clock`
    gives that coverage. Every row carries the filter that finds its hands.
    """
    rows = list(facts)
    window = [f for f in rows if post_loss(f)]
    rest = [f for f in rows if not post_loss(f)]
    loss_rows = [
        _row("all", "all hands", "", rows),
        _row("post_loss", f"next {WINDOW} hands after a loss", "post_loss", window),
        _row("other", "every other hand", "!post_loss", rest),
    ]
    hour_rows = []
    for key, label, filt, lo, hi in HOUR_BANDS:
        band = [
            f for f in rows
            if f.session_hour is not None and f.session_hour >= lo and (hi is None or f.session_hour < hi)
        ]
        hour_rows.append(_row(key, label, filt, band))
    base, post = loss_rows[0], loss_rows[1]
    gap = round(post["vpip"] - base["vpip"], 1) if post["vpip"] is not None and base["vpip"] is not None else None
    return {
        "hands": len(rows),
        "big_loss_bb": BIG_LOSS_BB,
        "window": WINDOW,
        # Hands in this list that were themselves a big loss: what opened the windows.
        "losses": sum(1 for f in rows if big_loss(f) is not None),
        "sessions": len({f.game_id for f in rows}),
        "vpip_gap": gap,
        "clock": {"known": sum(1 for f in rows if f.session_hour is not None), "n": len(rows)},
        "tables": [
            {"key": "loss", "title": f"After a loss of {BIG_LOSS_BB} bb or more", "rows": loss_rows},
            {"key": "hour", "title": "Through the night, by hours since their first hand", "rows": hour_rows},
        ],
    }
