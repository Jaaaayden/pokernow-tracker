"""Who won each pot of a hand, with what, for the foot of the replay.

The log names the winning hand on each ``collected`` line, but the database keeps
only the chips, so the hand is read again here from the cards it stores: the pots
are `equity.side_pots`, each board's winners `equity.rank7` (`rank_omaha` when a
player holds more than two cards), and each hand's name `equity.rank_name`.

Run it twice and Double Board split every pot evenly between the boards, and a
chop splits a board's share evenly between the tied hands; an odd chip goes to
the first of them in seat order. The result is checked against what each player
actually collected. When the two disagree by more than those odd chips -- a
showdown hand mucked unseen, chips PokerNow moved that the pots cannot explain
-- the pots are dropped and the winners are read off `collected` instead, still
named with their hand where their cards are known.

Nothing here touches the database; the replay endpoint in ``app.py`` calls it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .cards import Card, parse_cards
from .equity import rank7, rank_name, rank_omaha, side_pots


def _rank(hole: list[Card], board: list[Card]) -> tuple[int, ...]:
    return rank_omaha(hole, board) if len(hole) > 2 else rank7(hole + board)


def summary(players: Sequence[Mapping], runs: Sequence[Sequence[str]]) -> dict | None:
    """The showdown of one hand, from its `hand_players` rows and `board_json` runs.

    ``{"uncontested": {pn_id, won} | None, "pots": [...]}``; each pot is
    ``{"label", "chips", "boards": [{"run", "board", "winners", "losers"}]}``, a
    winner ``{pn_id, hand, won}`` and a loser ``{pn_id, hand, lost}``, chips
    throughout, ``hand`` None for cards never shown. ``label`` is ``pot``, or
    ``main pot`` and ``side pot``/``side pot 2``... when the stacks made more than
    one. ``uncontested`` is set instead of pots when everyone else folded: who
    was left, and the pot they took. None when nobody collected anything (a log
    cut off mid-hand) or two hands were left with the board unfinished.
    """
    if not any(p["collected"] for p in players):
        return None
    live = [p for p in players if not p["folded"]]
    if len(live) < 2:
        top = max(players, key=lambda p: p["collected"])
        return {"uncontested": {"pn_id": top["pn_id"], "won": top["collected"]}, "pots": []}
    boards = [list(r) for r in runs if len(r) == 5]
    if not boards:
        return None

    holes = {p["pn_id"]: parse_cards(p["hole_cards"]) for p in live if p["hole_cards"]}
    named = [{pid: _rank(h, parse_cards(b)) for pid, h in holes.items()} for b in boards]
    contributed = {p["pn_id"]: p["contributed"] for p in players}
    pots = side_pots(contributed, [p["pn_id"] for p in live])

    won: dict[str, int] = {}
    out = []
    for k, (chips, elig) in enumerate(pots):
        # A layer's stake: what each eligible player put in above the last pot's
        # level, up to the next pot's (all the rest of it, in the top pot).
        floor = max((contributed[p["pn_id"]] for p in live if p["pn_id"] not in elig), default=0)
        nxt = pots[k + 1][1] if k + 1 < len(pots) else ()
        cap = max((contributed[q] for q in elig if q not in nxt), default=0) if nxt else None
        results = []
        for run, (board, ranks) in enumerate(zip(boards, named)):
            share = chips // len(boards) + (run < chips % len(boards))
            mine = {q: ranks[q] for q in elig if q in ranks}
            if not mine:
                return _by_collected(players, boards, named)
            top = max(mine.values())
            winners = [q for q in elig if mine.get(q) == top]
            each, odd = divmod(share, len(winners))
            row = {"run": run, "board": board, "winners": [], "losers": []}
            for i, q in enumerate(winners):
                amount = each + (i < odd)
                won[q] = won.get(q, 0) + amount
                row["winners"].append({"pn_id": q, "hand": rank_name(top), "won": amount})
            for q in elig:
                if q in winners:
                    continue
                stake = min(contributed[q], cap if cap is not None else contributed[q]) - floor
                row["losers"].append({
                    "pn_id": q, "hand": rank_name(mine[q]) if q in mine else None,
                    "lost": stake // len(boards),
                })
            results.append(row)
        label = "pot" if len(pots) == 1 else "main pot" if not out else "side pot" if len(out) == 1 else f"side pot {len(out)}"
        out.append({"label": label, "chips": chips, "boards": results})

    slack = len(pots) * len(boards)
    if any(abs(won.get(p["pn_id"], 0) - p["collected"]) > slack for p in players):
        return _by_collected(players, boards, named)
    return {"uncontested": None, "pots": out}


def _by_collected(players: Sequence[Mapping], boards: list[list[str]], named: list[dict]) -> dict:
    """The fallback: one pot, its winners whoever collected, on the first board."""
    ranks = named[0]
    row = {"run": 0, "board": boards[0], "winners": [], "losers": []}
    for p in players:
        pid = p["pn_id"]
        hand = rank_name(ranks[pid]) if pid in ranks else None
        if p["collected"]:
            row["winners"].append({"pn_id": pid, "hand": hand, "won": p["collected"]})
        elif not p["folded"]:
            row["losers"].append({"pn_id": pid, "hand": hand, "lost": p["contributed"]})
    chips = sum(p["collected"] for p in players)
    return {"uncontested": None, "pots": [{"label": "pot", "chips": chips, "boards": [row]}]}
