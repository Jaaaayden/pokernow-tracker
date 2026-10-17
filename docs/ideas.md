# Ideas

A backlog of analysis this tracker could add, ranked by value per effort. Each entry
says what it answers at the table, which existing facts and filters it would read,
what is missing, how big it is, and where the idea comes from. Nothing here is
scheduled. The point is to make choosing the next feature a short conversation.

Most of the ideas are standard features of the desktop trackers (Holdem Manager 3,
PokerTracker 4 and Hand2Note) and the solver-study sites. They are filtered for what
this data can actually support: a small home game among friends, where the cards are
known only at showdown and for the hero, and every log line carries a millisecond
timestamp.

Stat definitions belong in [`SPEC.md`](../pnt/stats/SPEC.md). Anything built from
this list gets its numerator, denominator and exclusions pinned there first, the
same as every other stat.

## Status: all 16 built (October 2026)

Every idea in the ranking below has been built. The write-ups stay as the record
of why each one exists; [`guide.md`](guide.md) says how to use them and SPEC.md
pins what they count.

| # | Idea | Where it lives |
|---|---|---|
| 1 | Red line / blue line | Top of the chart's **Results** view, `/players/{alias}/winnings`, `pnt winnings` |
| 2 | WWSF and the give-up funnel | WWSF and W$WSF columns on `/stats`; funnel tables under Results, `/players/{alias}/funnel` |
| 3 | Multiway vs heads-up | **Split by** on `/stats`; `players_flop`; a players step in the HUD's spot search |
| 4 | Steal and blind defense | ATS, F2S and 3BvS columns; chart chips; FOLDS TO STEAL and STEALS WIDE tags |
| 5 | Head-to-head money flow | The **Head to head** table on each player's row on `/players`, `/h2h`, `pnt h2h` |
| 6 | Voluntary-show tells | The chart's **Shows** tab, `pnt shows` (the SHOWS BLUFFS / SHOWS NUTS tags were dropped) |
| 7 | Confidence intervals | ± ranges on hover in `/stats`, the chart and the HUD; the ± column beside bb/100 |
| 8 | Delayed c-bet, probe and float | DCB, Probe, F2P and Float columns; HUD spots; Lines chips |
| 9 | Tilt and session dynamics | The chart's **Tilt** view, `post_loss` and the session terms, the HUD's "tilt?" chip, TILTS |
| 10 | Stack-depth splits | Stack depth in **Split by**, the JAM column, Depth and SPR chips |
| 11 | Squeeze and cold-call | SQZ and CC columns, chart chips, NEVER SQUEEZES |
| 12 | Limp-reraise | LRR column, Limped chips, LIMP-RERAISE = STRONG |
| 13 | Straddled pots | Straddle in **Split by**, the straddled chip on the Pot row |
| 14 | Leak finder for the hero | `/leaks`, `pnt leaks` (experimental; the **Tendencies** page replaced its panel) |
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

## Already planned

These features are already in the current feature batch, so they are left out of the
ranking below:

| Planned | Covers |
|---|---|
| **Foundation** | Per-street action lines (`line_<s>=`, `line=F/T/R`), check-raise detection, `fold_street`, sizes of bets and raises faced, `raise_size`, `bet_answer`, the `blocker` size bucket, per-street made hand (`made_<s>=`), and `!flag` negation |
| **Slowplay report** | Strength by street against what they did on that street |
| **Flattened categories** | Made-hand composition as one flat list of leaves |
| **Action-line explorer** | Strength and net bb per `f/t/r` line. This is our version of Hand2Note's range research |
| **Value:bluff** | Value, bluff and semi-bluff shares by bet bucket, check-raise, raise and jam |
| **Fold equity by size** | The bettor side, with the break-even fold % (alpha), and the defender side against **MDF** |
| **Player notes** | `players.notes`, kept through merges, shown on `/players`, `/stats`, `/chart` and the HUD |
| **Results by pot type** | bb/100 by walk, limped, SRP, 3-bet and 4-bet+, split by PFA vs caller, position and IP/OOP |

"F" below means the Foundation branch. Sizes are **S** (a day or less: one fact or
one view), **M** (a few days: new facts, an endpoint and a view) and **L** (a schema
change plus a rebuild, or several moving parts).

## Ranking

| # | Idea | Answers | Size | Data | Depends on |
|---|---|---|---|---|---|
| 1 | [Red line / blue line](#1-red-line--blue-line) | Do they win by betting, or only at showdown? | S | ✅ | none (sits well on the Results view) |
| 2 | [WWSF and the give-up funnel](#2-wwsf-and-the-give-up-funnel) | How often do they take a pot after the flop, and on which street do they quit? | S | ✅ | F (`fold_street`) for the funnel |
| 3 | [Multiway vs heads-up splits](#3-multiway-vs-heads-up-splits) | Does their c-bet or fold rate change with three or more on the flop? | S | ⚠️ one fact | none |
| 4 | [Steal and blind defense](#4-steal-and-blind-defense) | Can I steal their blind, and do they steal mine? | S–M | ⚠️ two facts | none |
| 5 | [Head-to-head money flow](#5-head-to-head-money-flow) | Who am I losing to, and who pays whom? | M | ✅ | none |
| 6 | [Voluntary-show tells](#6-voluntary-show-tells) | When they show without being asked, is it a bluff or the nuts? | S | ✅ | none |
| 7 | [Confidence intervals](#7-confidence-intervals) | Is this number real, or ten hands of noise? | S | ✅ | none |
| 8 | [Delayed c-bet, probe and float](#8-delayed-c-bet-probe-and-float) | What do they do when the aggressor gives up a street? | M | ✅ after F | F (`line`) |
| 9 | [Tilt and session dynamics](#9-tilt-and-session-dynamics) | Do they spew after a big loss, or late in the night? | M | ✅ | none |
| 10 | [Stack-depth splits](#10-stack-depth-splits) | Do they play differently short or deep? | M | ⚠️ one fact | none |
| 11 | [Squeeze and cold-call](#11-squeeze-and-cold-call) | Do they punish an open plus a caller? | S–M | ⚠️ one fact | none |
| 12 | [Limp-reraise](#12-limp-reraise) | Is their limp a trap? | S | ✅ | none |
| 13 | [Straddled pots](#13-straddled-pots) | How does the straddle change the game? | S | ⚠️ one fact | none |
| 14 | [Leak finder for the hero](#14-leak-finder-for-the-hero) | Which of my own numbers is out of line, and what does it cost? | M | ✅ | Results by pot type, fold equity |
| 15 | [Timing tells](#15-timing-tells) | Does a snap call or a long tank mean strength? | L | ❌ needs `actions.ord` | F (`made_at`) for the strength side |
| 16 | [7-2 bounty ledger](#16-7-2-bounty-ledger) | Who has won and paid the most 7-2 bounties? | S | ✅ | none |

✅ means every fact is already derived. ⚠️ means one or two small new `Facts` fields
are needed, computed from data that is already stored. ❌ means a schema change and
a `pnt rebuild` are needed.

---

## 1. Red line / blue line

**At the table:** a player whose blue line (showdown winnings) climbs while their red
line (non-showdown winnings) falls wins with cards and gives up pots without them.
Bluff them off hands, and value-bet them thinly. A rising red line belongs to the
player who takes pots away, so call them down lighter. This is the most-used leak
graph in HM3 and PT4.

**Data:**
- `Facts.net`, `wtsd`, `bb_size`, `ts` and `game_id`.
- Red line: cumulative `net / bb_size` over hands with `not wtsd`. Walks and
  preflop steals land here.
- Blue line: cumulative `net / bb_size` over hands with `wtsd`.
- The two lines sum to the total line.
- Incomplete hands are dropped from all three lines, by the bb/100 rule.
- Optional third line: the all-in-adjusted total (`allin.py`'s `adjusted`) in place
  of `actual` on all-in hands. This is PT4's "all-in EV" line, and the `/allin` page
  already draws it alone.
- Optional per-session bars from `game_id`: a session results table that
  `player_games` almost has already.

**Size:** S. One `queries` function returning the three cumulative series, and one
SVG chart on `/chart` that follows the Spot filter. The Results view is the natural
home, beside its bb/100 table.

**Source:** [Red line](https://www.thepokerbank.com/articles/software/red-line/),
[tracker comparison](https://www.blackrain79.com/2020/08/pokertracker-vs-holdem-manager.html)

## 2. WWSF and the give-up funnel

**At the table:**
- **WWSF** (won when saw flop) is how often a player ends up with the pot once they
  see a flop. Low WWSF with a normal WTSD means they give up too much: keep betting
  at them.
- **The funnel** shows the same thing street by street. Of the flops they saw, it
  shows how many turns, rivers and showdowns they reached. Of the hands they folded,
  it shows where they folded, as aggressor and as caller.

**Data:**
- WWSF is `saw_flop` and `net > 0` over `saw_flop`. The `won` term already means
  `net > 0`, bounty included, so pin in SPEC whether a 7-2 bounty counts as winning
  the pot. `collected > 0` is the purer reading, and it is on `HandPlayerRow`, not
  `Facts`.
- W$WSF is the same, restricted to the pots won without showdown. It is the red line
  of idea 1 as a rate.
- The funnel reads F's `fold_street`, split by `aggressor[s]` on the street before.
- It sits beside `wtsd` and `wsd`, which give the last two steps.

**Size:** S. Two `Tally` counters and a `/stats` column. The funnel is one small
view once F lands.

**Source:** [WWSF glossary](https://www.deucescracked.com/learn/poker-glossary/wwsf),
[HUD stats](https://www.deucescracked.com/poker/strategy/hud-stats)

## 3. Multiway vs heads-up splits

**At the table:** most players c-bet and fold differently into two or more callers.
A 60% flop c-bet that drops to 20% three-way means their three-way bet is value.

**Data:**
- Missing: a `players_flop` fact counting the players who saw the flop. It is the
  same for everyone in the hand, like `pot_level`.
- It is **not** `pos_players`, which counts this player's `opponents` at their last
  action and changes as players fold. `players=N` is the dealt-in count.
- Filter: `players_flop=2`, `players_flop>=3`, a `NUMERIC` comparison like `pot_bb`.
- Views: the c-bet, fold-to-c-bet and `faced_bet` rates in `rates()` split by
  `players_flop`. Hand2Note's contexts work like this, and the HUD's node widening
  could add it as a step.

**Size:** S. One fact, one filter term, a two-column split on `/stats`.

**Source:** [Hand2Note HUD review](https://www.blackrain79.com/2022/05/hand2note-poker-hud-review.html)

## 4. Steal and blind defense

**At the table:** these are the core blind-war stats.
- **ATS** (attempt to steal): open-raises from CO, BTN or SB when folded to.
- **Fold to steal**: SB and BB facing one.
- **3-bet vs steal**.

In a short-handed home game, most hands are blind-vs-button.

**Data:**
- ATS is already expressible, one seat at a time: `unopened=raise,position=BTN`,
  and the same for CO and SB. Only a named `steal` term and a `/stats` column are
  new.
- Missing: the opener's position, so a defender can tell a steal from an early-
  position open. That needs a fact `open_from` (the opener's `seats_from_button`), the
  same for everyone in the hand.
- Missing: whether anyone called between the open and the defender
  (`callers_before`). A steal is only a steal when it was folded to the blinds.
  This fact is shared with idea 11.
- Fold to steal: SB or BB with `faced_open=fold`, where `open_from` is a steal
  seat and `callers_before == 0`. 3-bet vs steal is `faced_open=raise` on the same
  hands.
- Withheld on dead-button and irregular-blind hands, as every positional view is.

**Size:** S–M. Two facts in `_preflop`, three filter terms, SPEC rows, and
`/stats` columns. A FOLDS TO STEAL / STEALS WIDE tag pair is a natural follow-up.

**Source:** [PT4 fold BB to steal](https://www.cardplayer.com/?p=1035172),
[essential HUD stats](https://pokerskill.com/c/strategy-guides/12-essential-poker-hud-stats-used-by-online-pros)

## 5. Head-to-head money flow

**At the table:** "who am I actually losing to" and "who pays whom" in a nine-person
pool. Stats against one villain already exist (`vs=henry` on any page). What is
missing is the money between two players, and the matrix.

**Data:**
- `Facts.opponents`, `net`, `bb_size` and `wtsd`.
- **Exact attribution** is possible when a hand ends with two players who did not
  fold. The loser's net, less what folded players left in the pot, went to the
  winner.
- **Multiway pots** need a rule, for example each loser's chips split across winners
  in proportion to what they collected. The rule is a judgement call and must be
  written down as one.
- Bounties are a side bet between known players, so they can be attributed exactly.
- A per-pair table: hands together, net bb each way, showdowns, and who
  `failed_bluff`'d into whom.

**Size:** M. A pairwise pass in `queries`, `GET /players/{alias}/h2h` (one row per
opponent), and a pool matrix view on `/stats` or `/players`.

**Source:** [tracker comparison](https://www.blackrain79.com/2020/08/pokertracker-vs-holdem-manager.html) (HM3 "vs player" reports)

## 6. Voluntary-show tells

**At the table:** some players show bluffs to tilt the table, and others show only
the nuts. When they show after a fold or an uncontested win, which is it? This one
belongs to a friends' game, since online regulars rarely show at all.

**Data:**
- Already stored and kept apart on purpose: the `voluntary_shows` table and the
  `v_voluntary_shows` view (`folded`, `went_to_showdown`, `board_cards`). Range views
  never include these rows.
- Classify each two-card show with `cards.made_hand` on the board as it stood, or
  `made_at` after F: air, draw, marginal or strong. Split it by "folded, then showed"
  vs "won uncontested, then showed".
- One-card shows are counted, not classified.
- The bias runs the other way from showdowns: these are the hands a player *chose*
  to reveal. That is exactly the tell, and the view should say so.

**Size:** S. One query over the view, a small table on `/players` or the chart, and
an optional SHOWS BLUFFS / SHOWS NUTS fun tag.

**Source:** none. It follows from `docs/findings.md` §7 (voluntary shows) and the
data this game produces.

## 7. Confidence intervals

**At the table:** a VPIP of 45% on 20 hands and one on 2,000 hands look the same
today. An interval says whether the HUD's blue "playing looser tonight" figure is a
real change.

**Data:**
- Every rate is already `num / den`. A Wilson interval needs nothing more.
- bb/100 needs the per-hand variance of `net / bb_size`. `Tally` adds a sum of
  squares beside `bb_sum`.
- The tags' minimum sample sizes stay as they are. The interval is a display, not a
  rule.

**Size:** S. A `±` on hover in `/stats` and the HUD, and a CI column on bb/100.

**Source:** [Wilson score interval](https://en.wikipedia.org/wiki/Binomial_proportion_confidence_interval#Wilson_score_interval)

## 8. Delayed c-bet, probe and float

**At the table:** SPEC already excludes these from c-bet and donk ("a bet on a street
that checked through is a probe or float"), so today they are invisible.
- **Delayed c-bet:** the PFA checks the flop and bets the turn.
- **Probe:** the non-PFA bets out of position into a PFA who checked back the
  previous street.
- **Float:** the in-position caller bets after the aggressor checks to them.
- **Fold to probe.**

Players who never probe can have their checked-back flops attacked. Players who
always float make the turn check-raise a weapon.

**Data:**
- `pfa`, `aggressor[s]`, `check_back[s]`, `called_bet[s]`, `in_position`, and F's
  `line[s]`.
- A probe opportunity is OOP, not PFA, the previous street had `check_back` by the
  PFA, and they act first on this street.
- New facts `delayed_cbet_opp/delayed_cbet`, `probe_opp/probe`,
  `float_opp/float[s]` and `fold_to_probe[s]` are set in `_postflop` from street
  state. They are not computable from one player's `line` alone.

**Size:** M. Six fact families, filter terms, SPEC rows and tests. The node table can
then track the spots the HUD currently answers from an ancestor.

**Source:** [delayed c-bet](https://www.pokerskill.com/poker-glossary/delayed-c-bet/),
[probe, blocker and float bets](https://www.pokerlistings.com/strategy/blocking-continuation-and-floater-bets),
[PokerStrategy forum on barrel stats](https://www.pokerstrategy.com/forum/pokertracker-1647/double-barreling-stats-317894/)

## 9. Tilt and session dynamics

**At the table:** "he just lost a 100bb pot, so the next twenty hands are a different
player". The HUD's session-vs-lifetime blue figures hint at this, but nothing
measures it directly.

**Data:**
- Order a player's `Facts` by `(game_id, hand_number)`. `ts` is there for the clock.
- **After a big loss:** the N hands after any hand with `net / bb_size ≤ −X`, or
  after a `review` beat (`suckout`, `cooler_*`). Compare VPIP, PFR, aggression and
  bb/100 against the player's own baseline, with the coverage beside them.
- **Through the night:** the same figures by hand index within the session, or by
  hours since their first hand of the night.
- These are context across hands, which a per-`Facts` predicate cannot see. Either a
  post-pass adds `since_loss_bb` / `hands_since_loss` / `session_hour` to each
  player's `Facts` at read time, or this stays a report with no filter term.
  Choosing between them is the main design decision.
- The streaky-tag check (judge again without each session) is the same machinery
  pointed at a different question.

**Size:** M. A post-pass, a report, an optional TILTS tag, and a HUD flag when a
player is inside their post-loss window right now.

**Source:** [tilt markers](https://blask.com/knowledge/tilt-player/),
[CTI tilt detection thesis](https://theses-dissertations.princeton.edu/entities/publication/eec882df-7da7-49d1-a130-4e59797e7e2b)

## 10. Stack-depth splits

**At the table:** with rebuys and uneven stacks, a player at 30bb and the same player
at 250bb are different opponents. Stack depth decides who jams, who sets-mines, and
who can be pushed off a hand.

**Data:**
- `hand_players.starting_stack` is stored. `review._eff` already computes the
  effective stack for a set of players, and `review._spr` the flop SPR, but only
  inside review rows.
- Missing: `eff_bb` on `Facts`. Measure it as this player's stack against the
  largest other stack dealt in, so the value is per player and never needs the
  villain to be known.
- Unknown starting stack means `None`, by judgement call 26, so `eff_bb<40` does not
  match it.
- Filter: `eff_bb<40`, `eff_bb>=150`, and `spr_flop<=3` once SPR moves to `Facts`
  as well.
- View: VPIP, PFR, 3-bet, jam rate and bb/100 by depth bucket (<40, 40–100,
  100–200, 200+).

**Size:** M. Two facts, two filter terms, a split table, and SPEC rows.

**Source:** [Hand2Note HUD review](https://www.blackrain79.com/2022/05/hand2note-poker-hud-review.html),
[effective stack](https://www.deucescracked.com/learn/poker-glossary/effective-stack)

## 11. Squeeze and cold-call

**At the table:**
- **Squeeze %:** 3-bets after an open and one or more callers. A player who never
  squeezes makes open-and-flat safe.
- **Cold-call %:** calls of an open when not in the blinds. A high cold-call % means
  a capped range.

**Data:**
- `pf_faced[2]` already records the decision facing an open. Cold-call is
  `called_open` from any seat but the blinds, which is expressible today one
  `position=` at a time and only needs naming.
- Squeeze needs `callers_before` (shared with idea 4): the count of calls at level 2
  before this player acted. `squeeze_opp` is `faced_open` with `callers_before ≥ 1`.

**Size:** S–M. Mostly free once idea 4's fact exists.

**Source:** [squeeze](https://www.deucescracked.com/learn/poker-glossary/squeeze),
[3-bet squeeze](https://www.blackrain79.com/2023/11/3-bet-squeeze.html)

## 12. Limp-reraise

**At the table:** in a limp-heavy game, a limp-reraise is usually aces or kings. One
number tells you whether a player's limp is a trap.

**Data:**
- Already expressible: `limp,faced_open=raise` (a limper acts again at level 2).
  `limp,faced_open=call` is limp-call and `limp,faced_open=fold` is limp-fold.
- Missing only: a `/stats` column, and a shown-range readout through the existing
  range chart with `hand_pct`.
- An optional LIMP-RERAISE = STRONG tag uses the light-hand machinery in reverse.

**Size:** S.

**Source:** [HUD stats](https://www.deucescracked.com/poker/strategy/hud-stats)

## 13. Straddled pots

**At the table:** straddles are common in home games and change everyone's opening
ranges. How does each player's VPIP and bb/100 move in a straddled pot?

**Data:**
- `actions.post_kind = 'straddle'` is parsed and stored. architecture.md's parser
  notes cover the amount rules.
- Missing: a `straddled` hand-level fact and a filter term.
- The level-1 amount already includes the straddle (SPEC "Bet levels"), so the
  3-bet family is unaffected.
- The pot-type rows of the planned Results view can then split on it.

**Size:** S.

**Source:** none. It follows from the forced-post handling in
[architecture.md](architecture.md#parser-what-to-know-before-you-touch-it).

## 14. Leak finder for the hero

**At the table:** before the next session, the three things in my own game most out
of line and what they cost in bb, instead of a wall of numbers.

**Data:**
- The hero is `games.hero_pn_id`, and the hero's cards are known on every hand.
  Shown-hand evidence is therefore complete for the hero, as SPEC's tag rules
  already note.
- Bands: reuse the `tags.THRESHOLDS` and baseline-by-table-size pattern, judged
  against the hero instead of used for exploits. Alternatively, measure against the
  pool's winning players, which a nine-person pool can barely support. Say so.
- Cost: the bb/100 of the hands each leak's filter selects, from the Results view,
  plus the fold-equity tables for over- and under-folding by size.
- Every leak links to its filter on `/chart`, the same as a tag does.

**Size:** M, once the Results and fold-equity views exist.

**Source:** [HM3 LeakTracker](https://www.vip-grinders.com/holdem-manager-3-review-new-features/),
[Leak Buster](https://www.pokerstrategy.com/forum/poker-tools-1361/official-support-thread-leak-buster-database-analysis-315064/)

## 15. Timing tells

**At the table:** does a snap call mean a draw, and does a long tank before a river
bet mean a bluff? PokerNow logs every line to the millisecond, so this is the one tell
an online home game gives away and the desktop trackers mostly cannot see.

**Data:**
- `raw_entries.at` and `ord` (`epoch_ms * 100 + seq`) carry the time of every line.
- **Missing:** `actions` stores `seq` but not the `ord` it came from, so an action
  cannot be joined back to its time. Add `actions.ord`, filled by the parser, which
  needs a schema migration and a `pnt rebuild` of every game.
- Time to act: the action's time minus the time of the previous line in the hand.
  The first action on a street is measured from the board line.
- Buckets: snap (under ~1.5 s), normal, and tank (over ~10 s). Calibrate them on the
  pool, the way `THRESHOLDS` were.
- Strength side: shown hands by bucket, through F's `made_at` and the HUD's
  value / marginal / draw / air split.
- Live: the HUD already sees `decision-current` move (`watch.js`), so a running
  clock on the player to act is cheap.

**Caveats to verify first:**
- PokerNow's pre-action buttons (check/fold, call any) produce instant actions that
  are not decisions.
- Players in a friends' game are often distracted, so slow does not always mean
  thinking.
- Network lag adds noise.

A first measurement over the fixtures should show whether the distribution
separates at all before any UI is built.

**Size:** L. A schema change, a parser change, a rebuild, a new fact
(`think_ms[s]`), filter terms (`think_<s>>=10`), and the strength view.

**Source:** [timing tells](https://poker.academy/blog/post/the-hidden-power-of-timing-tells),
[Hand2Note review](https://www.pokerlistings.com/poker-tools/trackers/hand2note)

## 16. 7-2 bounty ledger

**At the table:** bragging rights. Who has collected and paid the most 7-2 bounties,
and how often each player actually plays 72 when dealt it (hero only, since others'
folded 72s are unseen).

**Data:**
- `hand_players.bounty` is signed and already part of `net`.
- The fun tags already find shown 7-2 raises (`hand=72,pfr`, `hand=72,jam_preflop`).
- A per-player total and count is one query.

**Size:** S. Purely for fun, which is why it ranks last despite costing almost
nothing.

**Source:** none. It is this game's own side bet (`schema.sql`, `hand_players.bounty`).

---

## Considered and left out

| Idea | Why not |
|---|---|
| **MDF vs fold rate**, **break-even fold %** | Already in the Fold equity by size plan |
| **Range research by line and size** | Already in the action-line explorer and value:bluff plans |
| **Equity realization, EV graphs for non-all-in hands** | Needs the villain's cards on hands that did not reach showdown |
| **Auto-notes** (HM3/H2N rules that write notes) | Tags already do this with counts and filters. Revisit once player notes exist, as "pin a tag into the note" |
| **Rabbit-hunt analysis** (`Undealt cards:`) | Nothing to act on. The cards reached nobody |
| **Table-size segmented HUD profiles** | Already possible with `players=N`, and pooled by design (architecture.md, "Table sizes are pooled by default") |
