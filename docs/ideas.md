# Ideas

The record of the analysis backlog: every idea on it in October 2026 is built.
[`guide.md`](guide.md) says how to use each one, and
[`SPEC.md`](../pnt/stats/SPEC.md) pins what it counts. Anything built from here
on gets its numerator, denominator and exclusions pinned in SPEC.md first.

The ideas were filtered for what this data can support: a small home game among
friends, where the cards are known only at showdown and for the hero, and every log
line carries a millisecond timestamp.

## Built first

Per-street action lines, check-raises, `fold_street` and the other Foundation
facts; the Slowplays view; flat made-hand categories; the Lines view;
Value:bluff; Fold equity by size; player notes; and Results by pot type.

## October 2026 batch 1: all 16 built

| # | Idea | Where it lives |
|---|---|---|
| 1 | Red line / blue line | Top of the chart's **Results** view, `/players/{alias}/winnings`, `pnt winnings` |
| 2 | WWSF and the give-up funnel | WWSF and W$WSF columns on `/stats`; funnel tables under Results, `/players/{alias}/funnel` |
| 3 | Multiway vs heads-up | **Split by** on `/stats`; `players_flop`; a players step in the HUD's spot search |
| 4 | Steal and blind defense | ATS, F2S and 3BvS columns; chart chips; FOLDS TO STEAL and STEALS WIDE tags |
| 5 | Head-to-head money flow | The **Head to head** table on each player's row on `/players`, `/players/{alias}/h2h`, `pnt h2h` |
| 6 | Voluntary-show tells | The chart's **Shows** tab, `pnt shows` (the SHOWS BLUFFS / SHOWS NUTS tags were dropped) |
| 7 | Confidence intervals | ± ranges on hover in `/stats`, the chart and the HUD; the ± column beside bb/100 |
| 8 | Delayed c-bet, probe and float | DCB, Probe, F2P and Float columns; HUD spots; Lines chips |
| 9 | Tilt and session dynamics | The chart's **Tilt** view, `post_loss` and the session terms, the HUD's "tilt?" chip, TILTS |
| 10 | Stack-depth splits | Stack depth in **Split by**, the JAM column, Depth and SPR chips |
| 11 | Squeeze and cold-call | SQZ and CC columns, chart chips, NEVER SQUEEZES |
| 12 | Limp-reraise | LRR column, Limped chips, LIMP-RERAISE = STRONG |
| 13 | Straddled pots | Straddle in **Split by**, the straddled chip on the Pot row |
| 14 | Leak finder for the hero | `pnt leaks` (experimental; the **Tendencies** page replaced its panel) |
| 15 | Timing tells | The chart's **Timing** view, `tempo_<street>=` and `think_<street>`, the HUD clock, `pnt timing` (needs one `pnt rebuild`) |
| 16 | 7-2 bounty ledger | The chart's **7-2** tab, `/bounties`, `pnt bounties` |

## October 2026 batch 2

The HUD and group-by batch. [`guide.md`](guide.md) says how to use each one.

| Item | Where it lives |
|---|---|
| HUD stats picker | **HUD stats** on the front page, `GET`/`PUT /settings/hud`, `/hud`'s columns |
| Filter OR, brackets and value lists | Every Spot box and `--filter`: `3bet_pot\|4bet_pot`, `(…)\|(…)`, `!( )`, `position=BTN\|CO` |
| Chip counts | The number on each chart chip, `POST /counts` |
| Group by | The panel beside the chart's Preflop and Made grids, `GET /groupby` |
| Position split and vs fold | *Position* in **Split by** on `/stats`, the **vs fold** column, `bb_per_100_vs_fold` |
| Saved spots | The ★ beside a Spot box, pinned ones in the chart's Common row, `/spots` |
| Hand tags | The **+ tag** box on rows and replays, `tag=` (also on the CLI), the chart's **Tagged** view, `/tags`, `/tagged` |
| Spot tree | The chart's **Tree** view, `GET /players/{alias}/tree` |
| Holdings by street | **Made hands › Streets** on the chart, `GET /players/{alias}/streets` |
| Estimated ranges (R1 frequency range, R2 board breakdown) | The chart preflop view's **Estimate** toggle and **Board** panel, `GET /players/{alias}/estimate` and `/board` |

## Considered and left out

| Idea | Why not |
|---|---|
| **MDF vs fold rate**, **break-even fold %** | Built into Fold equity by size |
| **Range research by line and size** | Built as the Lines view and Value:bluff |
| **Equity realization, EV graphs for non-all-in hands** | Needs the villain's cards on hands that did not reach showdown |
| **Auto-notes** (HM3/H2N rules that write notes) | Tags already do this with counts and filters. Player notes exist now; a possible next step is "pin a tag into the note" |
| **Rabbit-hunt analysis** (`Undealt cards:`) | Nothing to act on. The cards reached nobody |
| **Table-size segmented HUD profiles** | Already possible with `players=N`, and pooled by design (architecture.md, "Table sizes are pooled by default") |
