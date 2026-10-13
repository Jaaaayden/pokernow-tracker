"""The 7-2 bounty ledger: who has collected the side bet, who has paid it.

Purely for fun. `hand_players.bounty` is the signed side-bet result of one hand,
already part of `net` and kept out of the pot (schema.sql), so the whole ledger
is one GROUP BY over it, resolved to people through `player_identities`. Nothing
is derived and nothing stored.

Per player:

``won``   hands with a positive bounty: the count, the chips, and the chips in
          that hand's big blinds.
``paid``  the same for hands with a negative bounty, as positive figures.
``net``   won minus paid, in chips and in bb.

Each row carries the filters that find its hands, ``bounty_won`` and
``bounty_paid``: run over every hand of the player, each finds exactly its count.

A side bet moves chips between players and creates none, so the ledger's net
sums to zero across everyone. The one way it cannot is a payment to or from a
player the log never dealt into that hand, which the parser drops on one side
(`parser._apply_bounty`); `balance` reports what is left so that would show.

The second half is hero's own: of the hands dealt while hero's cards are known --
the logs hero downloaded, where ``Your hand is`` names them on every hand -- how
many were 7-2 and how many of those hero played (put chips in voluntarily
preflop, the `vpip` rule of SPEC.md). Everyone else's 72 is seen only when it is
shown, and a folded 72 never is, so this is hero-only by necessity. Each figure
carries the filter that finds its hands, ``hand=72`` and ``hand=72,vpip`` -- or
None when one of their 72s was shown in a log they do not own, which the filter,
run over every hand of the player, would find as well.
See SPEC.md, "7-2 bounty ledger".
"""

from __future__ import annotations

import sqlite3

from .cards import hand_class
from .derive import VOLUNTARY_COMMIT
from .queries import display_names

#: The classes the hero half counts, as the `hand=72` filter names them.
SEVEN_DEUCE = frozenset({"72o", "72s"})
DEALT_FILTER = "hand=72"
PLAYED_FILTER = "hand=72,vpip"
#: The terms that find a leaderboard row's bounty hands, one each way.
WON_FILTER = "bounty_won"
PAID_FILTER = "bounty_paid"


def _bb(chips: float, n_known: int) -> float | None:
    return round(chips, 2) if n_known else None


def _bounty_rows(conn: sqlite3.Connection, game: str | None) -> list[sqlite3.Row]:
    """One row per (identity, hand) with a non-zero bounty."""
    where, params = "hp.bounty != 0", []
    if game:
        where += " AND h.game_id = ?"
        params.append(game)
    return conn.execute(
        "SELECT hp.pn_id, hp.bounty, COALESCE(h.bb, g.bb) AS bb, pi.player_id, p.alias"
        " FROM hand_players hp"
        " JOIN hands h ON h.hand_id = hp.hand_id"
        " LEFT JOIN games g ON g.game_id = h.game_id"
        " LEFT JOIN player_identities pi ON pi.pn_id = hp.pn_id"
        " LEFT JOIN players p ON p.player_id = pi.player_id"
        f" WHERE {where}",
        params,
    ).fetchall()


def _hands_dealt(conn: sqlite3.Connection, game: str | None) -> dict[object, int]:
    """Hands dealt in, per person: the `n` every bounty count is out of."""
    where, params = "", []
    if game:
        where, params = " WHERE h.game_id = ?", [game]
    out: dict[object, int] = {}
    for r in conn.execute(
        "SELECT COALESCE(CAST(pi.player_id AS TEXT), 'pn:' || hp.pn_id) AS who, COUNT(*) AS n"
        " FROM hand_players hp JOIN hands h ON h.hand_id = hp.hand_id"
        " LEFT JOIN player_identities pi ON pi.pn_id = hp.pn_id"
        f"{where} GROUP BY who",
        params,
    ):
        out[r["who"]] = r["n"]
    return out


def _who(row: sqlite3.Row) -> str:
    """The grouping key `_hands_dealt` uses: the person, or the bare id."""
    return str(row["player_id"]) if row["player_id"] is not None else "pn:" + row["pn_id"]


def _players(conn: sqlite3.Connection, game: str | None) -> list[dict]:
    names = display_names(conn)
    dealt = _hands_dealt(conn, game)
    acc: dict[str, dict] = {}
    for r in _bounty_rows(conn, game):
        key = _who(r)
        a = acc.setdefault(key, {
            "player": r["alias"] or names.get(r["pn_id"], r["pn_id"]),
            "won_n": 0, "won": 0, "won_bb": 0.0,
            "paid_n": 0, "paid": 0, "paid_bb": 0.0,
            "bb_known": 0, "bounty_hands": 0,
            # Only a person the chart can open gets filters; a bare id has no alias.
            "linked": r["alias"] is not None,
        })
        chips = r["bounty"]
        side = "won" if chips > 0 else "paid"
        a[side + "_n"] += 1
        a[side] += abs(chips)
        a["bounty_hands"] += 1
        if r["bb"]:
            a[side + "_bb"] += abs(chips) / r["bb"]
            a["bb_known"] += 1

    rows = []
    for key, a in acc.items():
        known = a["bb_known"]
        rows.append({
            "player": a["player"],
            "hands": dealt.get(key, 0),
            "won": {"count": a["won_n"], "chips": a["won"], "bb": _bb(a["won_bb"], known)},
            "paid": {"count": a["paid_n"], "chips": a["paid"], "bb": _bb(a["paid_bb"], known)},
            "net": {"chips": a["won"] - a["paid"], "bb": _bb(a["won_bb"] - a["paid_bb"], known)},
            # A hand with no big blind on record adds its chips but no bb figure.
            "bb_known": known,
            "bounty_hands": a["bounty_hands"],
            "won_filter": WON_FILTER if a["linked"] else None,
            "paid_filter": PAID_FILTER if a["linked"] else None,
        })
    rows.sort(key=lambda r: (-r["net"]["chips"], -r["won"]["count"], r["player"].casefold()))
    return rows


def _hero(conn: sqlite3.Connection, game: str | None) -> list[dict]:
    """Per person who owns a log: hands dealt, cards known, 72 dealt, 72 played.

    Only the hands of the logs they own (`games.hero_pn_id`), where their cards
    are known on every hand. Their shown hands in other logs are read too, but
    only to tell whether the filters still reproduce the counts: `hand=72` runs
    over every hand of the player, so a 72 they showed elsewhere would join it.
    """
    where, params = "", []
    if game:
        where, params = " AND h.game_id = ?", [game]
    commits = ",".join(f"'{k}'" for k in sorted(VOLUNTARY_COMMIT))
    acc: dict[str, dict] = {}
    elsewhere: dict[str, list[int]] = {}  # 72s dealt and played outside their own logs
    names = display_names(conn)
    for r in conn.execute(
        "SELECT hp.pn_id, hp.hole_cards, h.game_id, pi.player_id, p.alias,"
        "  COALESCE(g.hero_pn_id = hp.pn_id, 0) AS own,"
        "  EXISTS (SELECT 1 FROM actions a WHERE a.hand_id = hp.hand_id AND a.pn_id = hp.pn_id"
        f"   AND a.street = 'preflop' AND a.is_forced = 0 AND a.action_type IN ({commits})) AS vpip"
        " FROM hand_players hp"
        " JOIN hands h ON h.hand_id = hp.hand_id"
        " LEFT JOIN games g ON g.game_id = h.game_id"
        " LEFT JOIN player_identities pi ON pi.pn_id = hp.pn_id"
        " LEFT JOIN players p ON p.player_id = pi.player_id"
        f" WHERE (g.hero_pn_id = hp.pn_id OR hp.hole_cards IS NOT NULL){where}",
        params,
    ):
        cards = r["hole_cards"]
        known = bool(cards) and len(cards) == 4
        is72 = known and hand_class(cards) in SEVEN_DEUCE
        if not r["own"]:
            if is72:
                e = elsewhere.setdefault(_who(r), [0, 0])
                e[0] += 1
                e[1] += bool(r["vpip"])
            continue
        a = acc.setdefault(_who(r), {
            "player": r["alias"] or names.get(r["pn_id"], r["pn_id"]),
            "hands": 0, "known": 0, "dealt": 0, "played": 0, "games": set(),
        })
        a["hands"] += 1
        a["games"].add(r["game_id"])
        a["known"] += known
        if is72:
            a["dealt"] += 1
            a["played"] += bool(r["vpip"])

    out = []
    for key, a in acc.items():
        shown_dealt, shown_played = elsewhere.get(key, (0, 0))
        out.append({
            "player": a["player"],
            "hands": a["hands"],
            "known": a["known"],
            "dealt_72": a["dealt"],
            "played_72": a["played"],
            "played_pct": round(100.0 * a["played"] / a["dealt"], 1) if a["dealt"] else None,
            # None where the filter would also find a 72 shown in someone else's log.
            "dealt_filter": None if shown_dealt else DEALT_FILTER,
            "played_filter": None if shown_played else PLAYED_FILTER,
            "logs": len(a["games"]),
        })
    out.sort(key=lambda r: -r["hands"])
    return out


def ledger(conn: sqlite3.Connection, game: str | None = None) -> dict:
    """The bounty leaderboard and hero's 72 counts. See the module docstring."""
    players = _players(conn, game)
    balance = sum(r["net"]["chips"] for r in players)
    return {
        "game": game,
        "players": players,
        "total": {
            "won": sum(r["won"]["chips"] for r in players),
            "paid": sum(r["paid"]["chips"] for r in players),
            "bounties": sum(r["won"]["count"] for r in players),
        },
        # Zero unless a payment named someone the log did not deal in.
        "balance": balance,
        "hero": _hero(conn, game),
    }
