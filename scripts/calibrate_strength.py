"""Calibrate `strength.HS_CUTS` on a database's shown hands. Read-only: the
database is opened `mode=ro`, so not even a migration can write to it.

    python scripts/calibrate_strength.py [pokernow.sqlite]

For every shown holding with a pair of its own or better, on every street its
board reached, prints hand strength by made hand, then the cuts:

- strong: the cut that best separates top pair and overpairs from the lower pairs
  (underpair, middle, bottom) on unpaired boards, where those names can be trusted
  -- the most Youden's J, true-positive rate minus false-positive rate;
- weak: the same, middle pairs and underpairs against bottom pair;
- nutted: the cut that keeps the nutted share where "a set or better" had it, since
  two pair and sets overlap too much in hand strength for J to split them.

Then the tier shares the printed cuts give, against the leaf rules they replace.
Paste the table into SPEC.md, "Hand strength", and the cuts into `HS_CUTS`.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

from pnt.stats.cards import MADE_LEAVES, board_at, board_texture, leaf_of, made_at
from pnt.stats.strength import counts

STREETS = ("flop", "turn", "river")
_RANK = {leaf: i for i, leaf in enumerate(MADE_LEAVES)}
TOP = {"top_pair", "overpair"}
LOWER = {"pocket_pair", "middle_pair", "bottom_pair"}
MIDDLE = {"pocket_pair", "middle_pair"}


def youden(pos: list[float], neg: list[float]) -> tuple[float, float]:
    """(cut, J): the cut maximizing TPR - FPR for `x >= cut` as the positive call."""
    best = (0.0, -1.0)
    for cut in sorted({round(v, 2) for v in pos + neg}):
        tpr = sum(v >= cut for v in pos) / len(pos)
        fpr = sum(v >= cut for v in neg) / len(neg)
        if tpr - fpr > best[1]:
            best = (cut, tpr - fpr)
    return best


def main(path: str) -> None:
    # Not pnt.db.conn.connect, which runs the schema and migrations on what it opens.
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT hp.hole_cards AS cards, h.board_json AS board FROM hand_players hp"
        " JOIN hands h ON h.hand_id = hp.hand_id WHERE hp.hole_cards IS NOT NULL"
    ).fetchall()
    seen: set[tuple[str, tuple[str, ...]]] = set()
    by: dict[str, list[tuple[str, float, bool]]] = defaultdict(list)  # street -> (leaf, hs, unpaired)
    for r in rows:
        runs = json.loads(r["board"] or "[]")
        if not runs:
            continue
        for street in STREETS:
            board = board_at(tuple(runs[0]), street)
            if board is None:
                break
            if (r["cards"], board) in seen:
                continue
            seen.add((r["cards"], board))
            mh = made_at(r["cards"], board, street)
            if mh is None or mh.cls in ("draw", "high_card") or mh.detail == "board_pair":
                continue
            c = counts(r["cards"], board)
            if c is None:
                continue
            by[street].append((leaf_of(mh), c[0] / (2 * c[1]), "unpaired" in board_texture(board)))

    print(f"{len(seen)} distinct shown holdings on a street\n")
    cuts = {}
    for street in STREETS:
        rows_s = by[street]
        print(f"== {street}: {len(rows_s)} with a pair of their own or better")
        leaves = defaultdict(list)
        for leaf, hs, _ in rows_s:
            leaves[leaf].append(hs)
        for leaf in sorted(leaves, key=_RANK.get):
            v = leaves[leaf]
            q = statistics.quantiles(v, n=10) if len(v) >= 10 else [min(v), max(v)]
            print(f"   {leaf:14} n={len(v):5}  p10 {q[0]:.2f}  median {statistics.median(v):.2f}  p90 {q[-1]:.2f}")
        strong, j_s = youden([h for l, h, u in rows_s if u and l in TOP], [h for l, h, u in rows_s if u and l in LOWER])
        weak, j_w = youden([h for l, h, u in rows_s if u and l in MIDDLE],
                           [h for l, h, u in rows_s if u and l == "bottom_pair"])
        share = sum(_RANK[l] <= _RANK["set"] for l, _, _ in rows_s) / len(rows_s)
        ordered = sorted((h for _, h, _ in rows_s), reverse=True)
        nutted = round(ordered[max(0, round(share * len(ordered)) - 1)], 2)
        nj = youden([h for l, h, _ in rows_s if _RANK[l] <= _RANK["set"]],
                    [h for l, h, _ in rows_s if _RANK[l] > _RANK["set"]])
        cuts[street] = {"nutted": nutted, "strong": strong, "weak": weak}
        print(f"   strong {strong:.2f} (J {j_s:.2f})  weak {weak:.2f} (J {j_w:.2f})  "
              f"nutted {nutted:.2f} (set+ share {share:.1%}; Youden would say {nj[0]:.2f}, J {nj[1]:.2f})")
        old = Counter("nutted" if _RANK[l] <= _RANK["set"] else "strong" if _RANK[l] <= _RANK["top_pair"]
                      else "marginal" for l, _, _ in rows_s)
        new = Counter("nutted" if h >= nutted and h >= strong else "strong" if h >= strong else "marginal"
                      for _, h, _ in rows_s)
        print("   tiers  " + "  ".join(f"{t}: {old[t] / len(rows_s):.0%} -> {new[t] / len(rows_s):.0%}"
                                        for t in ("nutted", "strong", "marginal")) + "\n")
    print("HS_CUTS =", json.dumps(cuts))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "pokernow.sqlite")
