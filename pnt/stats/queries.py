"""Load hands from SQLite, derive facts, aggregate.

Deliberately reads raw rows and derives in Python rather than expressing the
opportunity logic as SQL window functions. The bet-level walk and the c-bet
opportunity rules are sequential reasoning about a hand; written in SQL they are
correct-looking and unreviewable, and SPEC.md could no longer be checked against
them by eye.

If this ever gets slow, the escape hatch is the one the design already allows:
materialize `Facts` into a rollup table marked derived and disposable, rebuilt
from `actions`. Never make it the source of truth.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping

from ..db.conn import generation, path_of
from ..logfmt.parser import position_name
from .derive import SIZE_BUCKETS, Facts, HandAction, HandPlayerRow, HandRow, derive


def load_hands(
    conn: sqlite3.Connection,
    game_id: str | None = None,
    pn_ids: Collection[str] | None = None,
    hand_ids: Collection[int] | None = None,
) -> list[HandRow]:
    """Load hands with their rosters and actions.

    `pn_ids` keeps only the hands those identities were dealt into -- and keeps
    each of those hands *whole*, every player and every action. That matters: a
    hand is derived as a unit (the bet-level walk needs everyone), so narrowing the
    roster would change the answer, while narrowing the set of hands does not.

    One player's figures therefore cost one player's hands, instead of the whole
    database re-derived and then discarded down to them.

    `hand_ids` narrows to exactly those hands, for a caller that has already picked
    its population in SQL (the all-in showdowns, say) and wants them whole.
    """
    clauses, params = [], []
    if game_id:
        clauses.append("h.game_id = ?")
        params.append(game_id)
    if pn_ids is not None:
        ids = list(pn_ids)
        if not ids:
            return []
        clauses.append(
            "h.hand_id IN (SELECT hand_id FROM hand_players WHERE pn_id IN"
            f" ({','.join('?' * len(ids))}))"
        )
        params.extend(ids)
    if hand_ids is not None:
        wanted = list(hand_ids)
        if not wanted:
            return []
        clauses.append(f"h.hand_id IN ({','.join('?' * len(wanted))})")
        params.extend(wanted)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params = tuple(params)

    hands: dict[int, HandRow] = {}
    for r in conn.execute(
        f"""SELECT h.hand_id, h.game_id, h.hand_number, h.n_dealt_in, h.dead_button,
                   h.blinds_irregular, h.went_to_showdown, h.board_json, h.ts,
                   h.complete, h.run_count, COALESCE(h.bb, g.bb) AS bb
            FROM hands h LEFT JOIN games g ON g.game_id = h.game_id
            {where} ORDER BY h.ord""",
        params,
    ):
        runs = json.loads(r["board_json"] or "[]")
        # "saw a flop" == the first run has at least three cards
        saw_flop = bool(runs) and len(runs[0]) >= 3
        hands[r["hand_id"]] = HandRow(
            hand_id=r["hand_id"],
            game_id=r["game_id"],
            hand_number=r["hand_number"],
            n_dealt_in=r["n_dealt_in"],
            dead_button=bool(r["dead_button"]),
            blinds_irregular=bool(r["blinds_irregular"]),
            went_to_showdown=bool(r["went_to_showdown"]),
            saw_flop=saw_flop,
            complete=bool(r["complete"]),
            bb=r["bb"],
            ts=r["ts"],
            players={},
            actions=[],
            board=tuple(runs[0]) if runs else (),
            run_count=r["run_count"] or 1,
        )

    for r in conn.execute(
        f"""SELECT hp.* FROM hand_players hp JOIN hands h ON h.hand_id = hp.hand_id {where}""",
        params,
    ):
        h = hands.get(r["hand_id"])
        if h is not None:
            h.players[r["pn_id"]] = HandPlayerRow(
                pn_id=r["pn_id"],
                seat=r["seat"],
                seats_from_button=r["seats_from_button"],
                contributed=r["contributed"],
                collected=r["collected"],
                bounty=r["bounty"],
                folded=bool(r["folded"]),
                hole_cards=r["hole_cards"],
                starting_stack=r["starting_stack"],
            )

    for r in conn.execute(
        f"""SELECT a.* FROM actions a JOIN hands h ON h.hand_id = a.hand_id {where}
            ORDER BY a.hand_id, a.seq""",
        params,
    ):
        h = hands.get(r["hand_id"])
        if h is not None:
            h.actions.append(
                HandAction(
                    seq=r["seq"],
                    street=r["street"],
                    pn_id=r["pn_id"],
                    kind=r["action_type"],
                    amount=r["amount"],
                    is_forced=bool(r["is_forced"]),
                    all_in=bool(r["all_in"]),
                    amount_to=r["amount_to"],
                )
            )

    return list(hands.values())


def identity_map(conn: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    """pn_id -> (player_id, alias). Collapses multi-device identities into one person."""
    return {
        r["pn_id"]: (r["player_id"], r["alias"])
        for r in conn.execute(
            "SELECT pi.pn_id, pi.player_id, p.alias FROM player_identities pi"
            " JOIN players p ON p.player_id = pi.player_id"
        )
    }


def display_names(conn: sqlite3.Connection) -> dict[str, str]:
    """pn_id -> the name a drill-down should print.

    The canonical alias when there is one, otherwise the last name PokerNow showed
    for that ID -- the same rule the replay endpoint uses. Resolved at read time,
    so a merge or a rename shows up on the next request without re-deriving.
    """
    return {
        r["pn_id"]: r["alias"] or r["last_seen_name"] or r["pn_id"]
        for r in conn.execute(
            "SELECT pi.pn_id, pi.last_seen_name, p.alias FROM player_identities pi"
            " LEFT JOIN players p ON p.player_id = pi.player_id"
        )
    }


def _rate(num: int, den: int) -> float | None:
    return round(100.0 * num / den, 1) if den else None


def aggregate(facts: Iterable[Facts]) -> dict:
    """Collapse Facts rows into the v1 stat set.

    Every rate returns None -- not 0 -- when the denominator is empty. A player
    with no 3-bet opportunities has an *unknown* 3-bet percentage, and showing 0%
    would be a claim we cannot support.
    """
    f = list(facts)
    if not f:
        return {"hands": 0}

    def s(attr: str) -> int:
        return sum(1 for x in f if getattr(x, attr))

    def sd(attr: str, street: str) -> int:
        return sum(1 for x in f if getattr(x, attr).get(street))

    def counter(attr: str, street: str) -> int:
        return sum(getattr(x, attr).get(street, 0) for x in f)

    net = sum(x.net for x in f)
    # Normalize each hand by *its own* big blind. Blind levels move within a game,
    # so dividing a whole session's net by one bb silently rescales history.
    scaled = [x.net / x.bb_size for x in f if x.bb_size and x.complete]

    out = {
        "hands": len(f),
        "vpip": _rate(s("vpip"), s("vpip_opp")),
        "pfr": _rate(s("pfr"), s("pfr_opp")),
        "3bet": _rate(s("three_bet"), s("three_bet_opp")),
        "fold_to_3bet": _rate(s("fold_to_3bet"), s("fold_to_3bet_opp")),
        "wtsd": _rate(s("wtsd"), s("wtsd_opp")),
        "wsd": _rate(s("wsd"), s("wtsd")),
        "net": net,
        "bb_per_100": round(100.0 * sum(scaled) / len(scaled), 2) if scaled else None,
        "_opp": {
            "vpip": s("vpip_opp"),
            "3bet": s("three_bet_opp"),
            "fold_to_3bet": s("fold_to_3bet_opp"),
            "wtsd": s("wtsd_opp"),
        },
    }
    for street in ("flop", "turn", "river"):
        faced_cbet = sd("fold_to_cbet_opp", street)
        out[f"cbet_{street}"] = _rate(sd("cbet", street), sd("cbet_opp", street))
        out[f"fold_to_cbet_{street}"] = _rate(sd("fold_to_cbet", street), faced_cbet)
        out[f"raise_cbet_{street}"] = _rate(sd("raise_cbet", street), faced_cbet)
        out[f"donk_{street}"] = _rate(sd("donk", street), sd("donk_opp", street))
        out[f"af_{street}"] = _rate(counter("aggressive", street), counter("agg_denom", street))

        # How big their c-bets are, and how they answer each size of c-bet.
        sizes = Counter(x.bet_size.get(street) for x in f if x.cbet.get(street))
        n_cbets = sum(sizes.values())
        out[f"cbet_{street}_sizes"] = {
            b: {"n": sizes[b], "pct": _rate(sizes[b], n_cbets)} for b in SIZE_BUCKETS
        }
        faced, folded, raised = Counter(), Counter(), Counter()
        for x in f:
            b = x.faced_cbet_size.get(street)
            if b is None:
                continue
            faced[b] += 1
            folded[b] += bool(x.fold_to_cbet.get(street))
            raised[b] += bool(x.raise_cbet.get(street))
        out[f"vs_cbet_{street}_by_size"] = {
            b: {
                "faced": faced[b],
                "fold": _rate(folded[b], faced[b]),
                "raise": _rate(raised[b], faced[b]),
            }
            for b in SIZE_BUCKETS
        }

        out["_opp"][f"cbet_{street}"] = sd("cbet_opp", street)
        out["_opp"][f"fold_to_cbet_{street}"] = faced_cbet
        out["_opp"][f"donk_{street}"] = sd("donk_opp", street)
        # Alone among these, an action count rather than a hand count: one hand can
        # contribute several. See SPEC.md, "Aggression Frequency".
        out["_opp"][f"af_{street}"] = counter("agg_denom", street)
    return out


def facts_by_player(
    conn: sqlite3.Connection,
    game_id: str | None = None,
    pn_ids: Collection[str] | None = None,
) -> tuple[dict[int, list[Facts]], dict[int, str], list[HandRow]]:
    """Derive every hand and group the resulting facts by canonical player."""
    hands = load_hands(conn, game_id, pn_ids)
    ident = identity_map(conn)
    grouped: dict[int, list[Facts]] = defaultdict(list)
    aliases: dict[int, str] = {}
    for hand in hands:
        for fact in derive(hand):
            pid, alias = ident.get(fact.pn_id, (-1, fact.pn_id))
            grouped[pid].append(fact)
            aliases[pid] = alias
    return grouped, aliases, hands


#: The last unfiltered `report()` per (database, game), with the generation it was
#: computed at. One entry each, holding a handful of small dicts -- deliberately not
#: a cache of `Facts`, which runs to tens of megabytes and would grow without bound.
#:
#: This is the answer to the HUD asking the same whole-database question every 30
#: seconds and after every hand. It cannot go stale: `bump_generation` runs inside
#: the same transaction as every change that would invalidate it, so a hit means the
#: derivation behind it is still exactly current.
_REPORT_CACHE: dict[tuple[str, str | None], tuple[int, list[dict]]] = {}


#: One player's `Facts`, per (database, alias, game), with the generation they
#: were derived at. This one *is* a cache of Facts, which `_REPORT_CACHE` refuses
#: to be -- but for a handful of players, not the whole database: the live view
#: asks for everyone at the table on every poll, and a table seats ten. Capped so
#: an evening across several tables cannot grow it without bound.
_FACTS_CACHE: dict[tuple[str, str, str | None], tuple[int, list[Facts]]] = {}
_FACTS_CACHE_MAX = 16


def clear_caches() -> None:
    """Forget every memoized result. For tests, and for anything that edits the
    database behind this module's back."""
    _REPORT_CACHE.clear()
    _FACTS_CACHE.clear()


def facts_cached(
    conn: sqlite3.Connection, alias: str, game_id: str | None = None
) -> list[Facts]:
    """`facts_for`, remembered until the derivation generation moves.

    The same contract as `report()`'s cache: keyed on the generation counter,
    which every write that changes a derived fact bumps inside its own
    transaction, so a hit is exactly current. Ingesting raw lines does not bump
    it, which is the point -- live capture writes every few seconds and the
    answer does not change until the hand is rebuilt. Callers must not mutate the
    rows they get back.
    """
    gen = generation(conn)
    path = path_of(conn)
    cacheable = gen is not None and bool(path)
    key = (path, alias, game_id)
    if cacheable:
        hit = _FACTS_CACHE.get(key)
        if hit is not None and hit[0] == gen:
            return hit[1]
    facts = facts_for(conn, alias, game_id)
    if cacheable:
        if key not in _FACTS_CACHE and len(_FACTS_CACHE) >= _FACTS_CACHE_MAX:
            _FACTS_CACHE.pop(next(iter(_FACTS_CACHE)))
        _FACTS_CACHE[key] = (gen, facts)
    return facts


def report(
    conn: sqlite3.Connection,
    game_id: str | None = None,
    min_hands: int = 1,
    predicate=None,
) -> list[dict]:
    """Stats per player, most hands first.

    `predicate` is an optional `Facts -> bool` filter -- this is the Holdem-Manager
    move: restrict to the hands where a player reached some specific spot, then
    report their behaviour within it.
    """
    # Only the unfiltered report is cached. A predicate is an arbitrary callable --
    # not something that can be used as a key -- and a spot query is asked once by a
    # person, where the unfiltered one is asked on a timer.
    # Cacheable only when the result can be keyed and invalidated with certainty:
    # a filterless query, against a database on disk (an in-memory one has no path
    # to tell it apart from another), whose generation counter can be read.
    gen = generation(conn) if predicate is None else None
    path = path_of(conn)
    cacheable = predicate is None and gen is not None and bool(path)
    key = (path, game_id)
    if cacheable:
        cached = _REPORT_CACHE.get(key)
        if cached is not None and cached[0] == gen:
            # Copied out so a caller that edits a row cannot corrupt the next reader.
            return [dict(r) for r in cached[1] if r["hands"] >= min_hands]

    grouped, aliases, _ = facts_by_player(conn, game_id)
    rows = []
    for pid, facts in grouped.items():
        if predicate is not None:
            facts = [f for f in facts if predicate(f)]
        if len(facts) < min_hands:
            continue
        rows.append({"player": aliases.get(pid, "?"), **aggregate(facts)})
    rows.sort(key=lambda r: -r["hands"])
    if cacheable:
        # Stored before min_hands is applied, so a stricter or looser threshold is
        # served from the same derivation.
        full = rows if min_hands <= 1 else _report_rows(grouped, aliases)
        _REPORT_CACHE[key] = (gen, full)
    return rows


def _report_rows(grouped: dict[int, list[Facts]], aliases: dict[int, str]) -> list[dict]:
    rows = [{"player": aliases.get(pid, "?"), **aggregate(facts)} for pid, facts in grouped.items()]
    rows.sort(key=lambda r: -r["hands"])
    return rows


def identities_of(conn: sqlite3.Connection, alias: str) -> list[str]:
    """Every PokerNow ID merged under one alias. Raises on an alias that does not exist."""
    rows = conn.execute(
        "SELECT pi.pn_id FROM players p JOIN player_identities pi"
        " ON pi.player_id = p.player_id WHERE p.alias = ?",
        (alias,),
    ).fetchall()
    if not rows:
        if not conn.execute("SELECT 1 FROM players WHERE alias = ?", (alias,)).fetchone():
            raise ValueError(f"unknown alias: {alias!r}")
        return []  # a real player, with no identities yet
    return [r["pn_id"] for r in rows]


def facts_for(
    conn: sqlite3.Connection, alias: str, game_id: str | None = None
) -> list[Facts]:
    """Every Facts row for one canonical player, across all merged identities.

    The alias is resolved against the identity tables rather than against a full
    derivation, so only that player's hands are read and derived. On a database
    where one player has a fraction of the hands, that is the same fraction of the
    work -- and an unknown alias now costs a single indexed lookup rather than the
    whole history.
    """
    ids = identities_of(conn, alias)
    if not ids:
        return []
    wanted = set(ids)
    return [
        f for hand in load_hands(conn, game_id, ids) for f in derive(hand) if f.pn_id in wanted
    ]


def hand_list(facts: Iterable[Facts], names: Mapping[str, str] | None = None) -> list[dict]:
    """One compact row per hand, newest first: what a drill-down lists before a replay.

    `names` resolves opponent pn_ids for display; without it they come through raw.
    """
    lookup = names or {}

    def name(pn_id: str) -> str:
        return lookup.get(pn_id, pn_id)

    def named(ids: Iterable[str]) -> list[str]:
        # Deliberately not deduped. Within one hand a pn_id is a seat, so two merged
        # identities under one alias are two seats and two opponents -- collapsing
        # them by name would drop a real one and make the count disagree with
        # `pos_players`. It happens: one player sat twice in hand 54 of pgl8vNV4WURe.
        return [name(i) for i in ids]

    rows = []
    for f in sorted(facts, key=lambda x: (x.ts or "", x.hand_id), reverse=True):
        # Same rule as positional_report: no label when the button or a blind is dead.
        labelled = f.seats_from_button is not None and not f.dead_button and not f.blinds_irregular
        rows.append(
            {
                "hand_id": f.hand_id,
                "game_id": f.game_id,
                "hand_number": f.hand_number,
                "ts": f.ts,
                "position": position_name(f.seats_from_button, f.n_dealt_in) if labelled else None,
                "players": f.n_dealt_in,
                "hole_cards": f.hole_cards,
                "board": list(f.board),
                "net_bb": round(f.net / f.bb_size, 1) if f.bb_size else None,
                # Chips, plus this hand's blind so a row can print the pot in bb too.
                "pot": f.pot,
                "bb": f.bb_size,
                "wtsd": f.wtsd,
                "bet_size": dict(f.bet_size),
                # Who the hand was against, and whether they closed the action.
                # `position` above is the absolute seat and stays in the payload;
                # the drill-down shows `ip` instead. See SPEC.md.
                "ip": f.in_position,
                "pos_order": f.pos_order,
                "pos_players": f.pos_players,
                "vs": named(f.opponents),
                "vs_cbet": {s: name(p) for s, p in f.faced_cbet_by.items()},
                "led_into": {s: name(p) for s, p in f.donk_into.items()},
            }
        )
    return rows


def positional_report(conn: sqlite3.Connection, alias: str, pool: bool = True) -> list[dict]:
    """Break one player's stats down by position.

    Table size is *pooled at query time* from raw `n_dealt_in`, never pre-bucketed
    in storage -- pooling later is always possible, unpooling is not.
    """
    buckets: dict[str, list[Facts]] = defaultdict(list)
    for f in facts_for(conn, alias):
        if f.seats_from_button is None or f.dead_button or f.blinds_irregular:
            # Positions are best-effort when the button or a blind is dead, so
            # they are left out of positional splits entirely. See SPEC.md.
            continue
        key = position_name(f.seats_from_button, f.n_dealt_in)
        if not pool:
            key = f"{key} ({f.n_dealt_in}-handed)"
        buckets[key].append(f)

    return sorted(
        ({"position": k, **aggregate(v)} for k, v in buckets.items()),
        key=lambda r: -r["hands"],
    )
