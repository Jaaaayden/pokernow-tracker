"""Head to head: the money between two players, and who pays whom across the pool.

`vs=alex` already restricts any stat to the hands played against one villain.
What it cannot say is how many chips went from you to alex. This module settles
every hand into transfers between pairs of players and sums them:

- **One winner** -- every heads-up finish, and most multiway ones -- is exact:
  each loser's net went to the winner, chip for chip, and a folder's dead money
  with it.
- **Several winners** (a chopped pot, a side pot won by someone else) needs a
  rule. Each loser's loss is split across the winners in proportion to what each
  *won* (their net gain from the pot). Judgement call 1 in SPEC.md, "Head to
  head": it is the only proportional split under which every player's transfers
  add back up to their net.
- **Bounties** (the 7-2 side bet) are settled the same way on their own ledger,
  which in practice always has one receiver, so they are exact.

A hand whose log stopped early, or whose chips do not balance, moves nothing:
its money is not all there. Those hands still count as hands played together.

Failed bluffs come from `review.failed_bluff`, run only on the hands that can
possibly qualify (a lost pot of the rule's size with a postflop bet or raise in
it), so the pool view does not derive every hand. They need the bluffer's cards,
so each carries the spots it was looked for in and how many of those showed them.

Nothing is stored. See SPEC.md, "Head to head".
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from fractions import Fraction
from itertools import combinations

from ..logfmt.events import PREFLOP
from .derive import AGGRESSIVE, HandRow, derive, is_analysed
from .queries import display_names, facts_cached, identities_of, identity_map, load_hands
from .review import THRESHOLDS as REVIEW_THRESHOLDS
from .review import Context, failed_bluff

#: What a failed bluff is looked for in, and why it might not be found.
FAILED_BLUFF_NOTE = (
    "A failed bluff is a postflop bet or raise with air that was called or raised, in a "
    f"pot of {REVIEW_THRESHOLDS['failed_bluff_pot_bb']}bb or more that the bluffer lost. "
    "It needs the bluffer's cards: shown at a showdown, or your own."
)

_FIELDS = ("hands", "counted", "showdowns", "flow_hands", "split_hands",
           "failed_bluffs", "bluff_spots", "bluff_known")


def _settle(ledger: Mapping[str, int], flows: dict[tuple[str, str], Fraction]) -> bool:
    """Book one balanced ledger as payer -> payee transfers. True when it had
    more than one winner, so the split was the proportional rule, not exact."""
    winners = {p: v for p, v in ledger.items() if v > 0}
    total = sum(winners.values())
    for loser, v in ledger.items():
        if v >= 0:
            continue
        for w, won in winners.items():
            key = (loser, w)
            flows[key] = flows.get(key, Fraction(0)) + Fraction(-v * won, total)
    return len(winners) > 1


Flows = dict[tuple[str, str], Fraction]


def _ledgers(hand: HandRow) -> tuple[Flows, Flows, bool] | None:
    """The pot's transfers and the bounties', kept apart, and whether either was
    split by the multiway rule. None when the money is not all there."""
    if not hand.complete:
        return None
    pot = {pid: p.collected - p.contributed for pid, p in hand.players.items()}
    bounty = {pid: p.bounty for pid, p in hand.players.items() if p.bounty}
    nit = {pid: p.nit for pid, p in hand.players.items() if p.nit}
    if sum(pot.values()) or sum(bounty.values()) or sum(nit.values()):
        return None
    pot_flows: Flows = {}
    bounty_flows: Flows = {}
    split = _settle(pot, pot_flows)
    # A bounty is its own ledger: settled with the pot, it would be spread over
    # the pot's winners instead of going to the player it was paid to.
    split |= _settle(bounty, bounty_flows)
    # So is a Nit penalty, though it is not a bounty: settled apart, then counted
    # with the pot. It has one payer, so the proportional rule is exact, not a split.
    nit_flows: Flows = {}
    _settle(nit, nit_flows)
    for k, chips in nit_flows.items():
        pot_flows[k] = pot_flows.get(k, Fraction(0)) + chips
    return pot_flows, bounty_flows, split


def hand_flows(hand: HandRow) -> tuple[Flows, bool] | None:
    """Every chip transfer in one hand, (payer, payee) -> chips, and whether any
    of it was split by the multiway rule.

    None when the money is not all there: the log stopped mid-hand, or the pot or
    the bounties do not net to zero. Each player's payments in, less their
    payments out, is exactly their net (`collected - contributed + bounty + nit`).
    """
    settled = _ledgers(hand)
    if settled is None:
        return None
    pot_flows, bounty_flows, split = settled
    flows = dict(pot_flows)
    for key, chips in bounty_flows.items():
        flows[key] = flows.get(key, Fraction(0)) + chips
    return flows, split


def answered_by(hand: HandRow, pid: str) -> set[str]:
    """Everyone who called or raised one of this player's postflop bets or raises,
    on the same street before anyone raised -- the walk `failed_bluff` makes."""
    acts = [a for a in hand.actions if not a.is_forced]
    out: set[str] = set()
    for i, a in enumerate(acts):
        if a.pn_id != pid or a.kind not in AGGRESSIVE or a.street == PREFLOP:
            continue
        for b in acts[i + 1:]:
            if b.street != a.street:
                break
            if b.pn_id == pid:
                continue
            if b.kind in AGGRESSIVE:
                out.add(b.pn_id)
                break
            if b.kind == "call":
                out.add(b.pn_id)
    return out


def _bluffs(hand: HandRow) -> list[tuple[str, set[str], set[str]]]:
    """(bluffer, everyone who answered their bets, who answered the failed bluff)
    for each player who lost a big enough pot after a postflop bet or raise that
    was answered. The third set is empty unless the cards show it was air."""
    bb = hand.bb
    if not hand.complete or not bb or not is_analysed(hand):
        return []
    if sum(p.contributed for p in hand.players.values()) / bb < REVIEW_THRESHOLDS["failed_bluff_pot_bb"]:
        return []
    out = []
    facts = None
    for pid, p in hand.players.items():
        if p.collected - p.contributed + p.bounty + p.nit >= 0:
            continue
        answered = answered_by(hand, pid)
        if not answered:
            continue
        caught: set[str] = set()
        if p.hole_cards:
            if facts is None:
                facts = {f.pn_id: f for f in derive(hand)}
            row = failed_bluff(hand, facts, pid, Context())
            if row is not None:
                caught = {v.pn_id for v in row.villains}
        out.append((pid, answered, caught))
    return out


def pair_flows(
    hands: Iterable[HandRow], key: Callable[[str], str] | None = None
) -> dict[tuple[str, str], dict]:
    """Per ordered pair (a, b): what passed between them over these hands.

    `key` folds a pn_id into the person it belongs to (default: the pn_id itself);
    a pair is two different people. Both orders are present, and each entry reads
    from a's side:

    - ``hands``          dealt in together
    - ``counted``        of those, hands whose money is all there (complete, balanced, a bb)
    - ``net_bb_a_to_b``  big blinds a paid b, net; exactly ``-net_bb_a_to_b`` of (b, a)
    - ``bounty_bb_a_to_b`` the part of that which was 7-2 bounties
    - ``flow_hands``     counted hands where any chips moved between them
    - ``split_hands``    of those, hands settled by the multiway rule rather than exactly
    - ``showdowns``      both still in at a showdown
    - ``failed_bluffs``  a's failed bluffs that b called or raised
    - ``bluff_spots``    a lost a big pot after b answered a's postflop bet or raise
    - ``bluff_known``    of those, a's cards were known, so the bluff could be judged

    Net figures are exact fractions; round them at the edge.
    """
    key = key or (lambda pid: pid)
    out: dict[tuple[str, str], dict] = {}

    def entry(a: str, b: str) -> dict:
        e = out.get((a, b))
        if e is None:
            e = out[a, b] = {f: 0 for f in _FIELDS}
            e["net_bb_a_to_b"] = Fraction(0)
            e["bounty_bb_a_to_b"] = Fraction(0)
        return e

    for hand in hands:
        who = {pid: key(pid) for pid in hand.players}
        people = sorted(set(who.values()))
        live = {who[pid] for pid, p in hand.players.items() if not p.folded}
        settled = _ledgers(hand) if hand.bb else None
        for a, b in combinations(people, 2):
            for x, y in ((a, b), (b, a)):
                e = entry(x, y)
                e["hands"] += 1
                if settled is not None:
                    e["counted"] += 1
                if hand.went_to_showdown and x in live and y in live:
                    e["showdowns"] += 1

        if settled is not None:
            pot_flows, bounty_flows, split = settled
            touched: set[tuple[str, str]] = set()
            for flows, fields in ((pot_flows, ("net_bb_a_to_b",)),
                                  (bounty_flows, ("net_bb_a_to_b", "bounty_bb_a_to_b"))):
                for (payer, payee), chips in flows.items():
                    a, b = who[payer], who[payee]
                    if a == b or not chips:
                        continue
                    bb = chips / hand.bb
                    for f in fields:
                        entry(a, b)[f] += bb
                        entry(b, a)[f] -= bb
                    touched |= {(a, b), (b, a)}
            for pair in touched:
                out[pair]["flow_hands"] += 1
                if split:
                    out[pair]["split_hands"] += 1

        for pid, answered, caught in _bluffs(hand):
            a = who[pid]
            known = bool(hand.players[pid].hole_cards)
            for b in {who[q] for q in answered if q in who} - {a}:
                e = entry(a, b)
                e["bluff_spots"] += 1
                e["bluff_known"] += known
            for b in {who[q] for q in caught if q in who} - {a}:
                entry(a, b)["failed_bluffs"] += 1
    return out


# ------------------------------------------------------------------ reports ---


def _people(conn: sqlite3.Connection) -> tuple[Callable[[str], str], dict[str, str]]:
    """pn_id -> person key (the alias; the raw pn_id when it has none), and each
    key's display name."""
    ident = identity_map(conn)
    names = display_names(conn)

    def key(pid: str) -> str:
        hit = ident.get(pid)
        return hit[1] if hit else pid

    labels = {key(pid): names.get(pid, pid) for pid in names}
    return key, labels


def vs_filters(names: Mapping[str, str], key: Callable[[str], str]) -> dict[str, str | None]:
    """Person key -> the chart filter for the hands played against them.

    `vs=` matches a display name or a raw id, ignoring case. That names a person
    when nobody else answers to the same text; `kai` and `KAI` are two people one
    term cannot tell apart. Then a person with a single PokerNow ID is named by
    the ID, and one with several gets None -- the filter language has no OR. A
    comma would split the term in two, so a name with one is not used either.
    """
    owners: dict[str, set[str]] = {}
    ids: dict[str, list[str]] = {}
    for pid, name in names.items():
        k = key(pid)
        ids.setdefault(k, []).append(pid)
        for token in (pid, name):
            owners.setdefault(token.strip().casefold(), set()).add(k)
    out: dict[str, str | None] = {}
    for k, pids in ids.items():
        out[k] = None
        for candidate in (k, *(pids if len(pids) == 1 else ())):
            if candidate.strip() and "," not in candidate and owners.get(candidate.strip().casefold()) == {k}:
                out[k] = f"vs={candidate}"
                break
    return out


def _vs_counts(facts: Iterable, names: Mapping[str, str]) -> Counter:
    """casefolded name -> how many of these facts `vs=<name>` keeps. Mirrors
    `filters._player_term`: a name matches an opponent's display name or raw id."""
    counts: Counter = Counter()
    for f in facts:
        tokens = set()
        for pid in f.opponents:
            tokens.add(pid.casefold())
            tokens.add(names.get(pid, pid).casefold())
        counts.update(tokens)
    return counts


def _bb(x: Fraction) -> float:
    return round(float(x), 2)


def _row(e: dict) -> dict:
    """An entry made printable: net figures as rounded floats, read from a's side
    as what a WON from b, so positive is up."""
    d = {f: e[f] for f in _FIELDS}
    d["net_bb"] = _bb(-e["net_bb_a_to_b"])
    d["bounty_bb"] = _bb(-e["bounty_bb_a_to_b"])
    return d


def _skipped(hands: list[HandRow]) -> dict:
    out = {"incomplete": 0, "unbalanced": 0, "no_bb": 0}
    for h in hands:
        if not h.complete:
            out["incomplete"] += 1
        elif not h.bb:
            out["no_bb"] += 1
        elif hand_flows(h) is None:
            out["unbalanced"] += 1
    return out


def player_h2h(conn: sqlite3.Connection, alias: str, game_id: str | None = None) -> dict:
    """One row per opponent of one player, biggest money either way first.

    `net_bb` is what the player won from that opponent (negative: lost to them).
    Each row carries `filter`, the chart spot for the hands against them (see
    `vs_filters`; None when no one term names only them), and `n`, how many of
    the player's hands that filter keeps -- `vs=` is "still in when I last
    acted", narrower than `hands` dealt in together. Raises ValueError on an
    unknown alias.
    """
    ids = identities_of(conn, alias)
    hands = load_hands(conn, game_id, ids) if ids else []
    key, labels = _people(conn)
    flows = pair_flows(hands, key)
    names = display_names(conn)
    filters = vs_filters(names, key)
    vs = _vs_counts(facts_cached(conn, alias, game_id), names) if ids else Counter()

    rows = []
    for (a, b), e in flows.items():
        if a != alias:
            continue
        name = labels.get(b, b)
        flt = filters.get(b)
        rows.append({
            "opponent": name,
            **_row(e),
            "failed_bluffs_against": flows[b, a]["failed_bluffs"],
            "bluff_spots_against": flows[b, a]["bluff_spots"],
            "bluff_known_against": flows[b, a]["bluff_known"],
            "filter": flt,
            "n": vs[flt[len("vs="):].strip().casefold()] if flt else None,
        })
    rows.sort(key=lambda r: (-abs(r["net_bb"]), -r["hands"], r["opponent"]))
    return {
        "player": alias,
        "game": game_id,
        "hands": len(hands),
        "net_bb": _bb(sum((-e["net_bb_a_to_b"] for (a, _), e in flows.items() if a == alias), Fraction(0))),
        "skipped": _skipped(hands),
        "rows": rows,
        "failed_bluff_note": FAILED_BLUFF_NOTE,
    }


def pool_h2h(conn: sqlite3.Connection, game_id: str | None = None) -> dict:
    """Who pays whom: every pair in the pool, as a matrix.

    `players` is ordered by hands played, most first. ``cells[a][b]`` reads from
    a's side: `net_bb` is what a won from b, so ``cells[a][b].net_bb ==
    -cells[b][a].net_bb``, and a row's `net_bb` sums to a's attributed net over
    the counted hands. No hand ids: a pool figure names nobody's hands.
    """
    hands = load_hands(conn, game_id)
    key, labels = _people(conn)
    flows = pair_flows(hands, key)
    played: Counter = Counter()
    for h in hands:
        played.update({key(pid) for pid in h.players})
    order = sorted(played, key=lambda k: (-played[k], labels.get(k, k)))
    # The matrix is keyed by name, so two people shown under one name (no alias,
    # the same last-seen name) would share a row: each takes its key beside it.
    shown = Counter(labels.get(k, k) for k in order)
    name = {k: labels.get(k, k) if shown[labels.get(k, k)] == 1 else f"{labels.get(k, k)} ({k})" for k in order}
    cells: dict[str, dict[str, dict]] = {name[k]: {} for k in order}
    totals: dict[str, Fraction] = {k: Fraction(0) for k in order}
    for (a, b), e in flows.items():
        cells[name[a]][name[b]] = _row(e)
        totals[a] -= e["net_bb_a_to_b"]
    return {
        "game": game_id,
        "hands": len(hands),
        "players": [
            {"player": name[k], "hands": played[k], "net_bb": _bb(totals[k])} for k in order
        ],
        "cells": cells,
        "skipped": _skipped(hands),
        "failed_bluff_note": FAILED_BLUFF_NOTE,
    }
