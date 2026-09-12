# Stat definitions

This file is the artifact. `derive.py` mirrors it function-for-function; if the two
ever disagree, this file is right and the code is a bug.

Trackers disagree with each other precisely here, and it is where quiet bugs live —
a stat with a wrong denominator looks completely plausible forever. So every stat
below pins three things: **numerator**, **denominator (opportunity)**, **exclusions**.

---

## Two global rules

### 1. Forced money never counts as voluntary

Every chip movement carries `is_forced`. Blinds, antes, straddles, dead posts and
missed-blind entry posts are all forced. Forced money counts toward *denominators*
and **never** toward a VPIP numerator.

Without this, a player who joins and leaves constantly accumulates a permanently
inflated VPIP that never washes out — they are posting blinds every time they sit
down, and every post would read as "voluntarily put money in".

### 2. Denominators use the dealt-in roster

Opportunity is computed over the roster from the `Player stacks:` line, never over
"players who acted" and never over "players currently seated and active". A player
sitting out is still at the table.

Getting this wrong is invisible: every rate comes out quietly too low for exactly
the players who sit out most.

### 3. Opportunity means *they got to act*

Across all preflop stats, a player has an opportunity on a hand if and only if they
took a preflop action. This is the operational form of "action reached them", and
it disposes of three edge cases at once:

- **BB walk** (everyone folds to the big blind): the BB never acts, so the hand is
  excluded from their VPIP/PFR denominator. They were never offered a choice.
- **All-in from a forced post**: no action, no opportunity.
- **Hand ends before action reaches them**: no action, no opportunity.

---

## Bet levels

The 3-bet family is defined against a bet-level counter rather than by counting
raises, because "3-bet" means *the third level of betting*, and the blinds are the
first.

| Level | Meaning |
|---|---|
| 1 | The blinds |
| 2 | Open raise |
| 3 | 3-bet |
| 4 | 4-bet |

A straddle is a forced post, so it raises the *amount* of level 1 without creating
level 2.

---

## Preflop

| Stat | Numerator | Denominator | Exclusions |
|---|---|---|---|
| **VPIP** | ≥1 voluntary preflop `call`, `bet` or `raise` | Hands where the player took any preflop action | Forced posts; BB walks; all-in from a post |
| **PFR** | ≥1 voluntary preflop `raise` | Same as VPIP | Same |
| **3-Bet** | The player's raise takes the level 2 → 3 | Player acts while level == 2 and is not the current aggressor | Hands with no open raise |
| **Fold to 3-Bet** | Player folds | Player made the level-2 open, someone made it 3, action returned to them | Player never opened |

A BB who checks their option has an opportunity and does not have VPIP. An SB who
completes to the BB *does* have VPIP — completing is voluntary.

---

## Postflop

**Aggressor**: the last player to `bet` or `raise` on a street. If a street checks
through it has **no** aggressor, so there is no c-bet opportunity on the next
street. (A bet on a street that checked through is a probe or float, not a
continuation bet. Some trackers carry the aggressor forward; this one does not.)

| Stat | Numerator | Denominator | Exclusions |
|---|---|---|---|
| **C-Bet** (flop/turn/river) | Player bets | Player was the previous street's aggressor, saw this street, and acts while no bet has yet been made on it | Previous street checked through; player already all-in |
| **Fold to C-Bet** | Player folds | Player acts facing a c-bet, before anyone raises over it | — |
| **Raise C-Bet** (flop/turn/river) | Player raises | Same as Fold to C-Bet | — |
| **Donk Bet** (flop/turn/river) | Player bets | Player acts while no bet has yet been made on this street, the previous street's aggressor has not acted on it yet, and that aggressor is not all-in | The aggressor themselves; previous street checked through |
| **Aggression Frequency** (per street) | `bet` + `raise` | `bet` + `raise` + `call` + `fold` | `check` is excluded from **both** sides |

Aggression frequency deliberately excludes checks. Including them makes the stat
measure how often a player is out of position rather than how aggressive they are.

A donk bet (a *lead*) is a bet into the previous street's aggressor before they
get to act. Like the c-bet it needs an aggressor: a bet after a checked-through
street is a probe and counts as neither.

### Bet size buckets

Every postflop `bet` also gets a size bucket, measured the same way as `bet_pot`:
the bet over the pot it was made into.

| Bucket | Size | Rule |
|---|---|---|
| `small` | under ½ pot | `amount < floor(½ × pot)` |
| `medium` | ½ to ¾ pot | `amount ≥ floor(½ × pot)` |
| `large` | ¾ pot to pot | `amount ≥ floor(¾ × pot)` and `amount ≤ pot` |
| `overbet` | more than pot | `amount > pot` |

**PokerNow's ½, ¾ and pot buttons each start a bucket.** They are most of the
bets: of 1,016 flop c-bets in a 5,836-hand database, 211 were exactly ½ pot, 62
exactly pot and 28 exactly ¾. With the edges placed anywhere else, the same
button click would land on either side of one. Bets are whole chips, so an edge
is rounded down to a chip: a ¾-pot click into a pot of 30 is 22 or 23 chips, and
both are `large`. A pot-sized bet is `large`, not an overbet.

**Fold to C-Bet and Raise C-Bet by size** split both stats by the bucket of the
c-bet the player faced -- that c-bet's own size, not the price of calling it.

Filters use the bucket names: `cbet_flop=medium` (a flop c-bet of that size),
`bet_river=overbet` (their first river bet, c-bet or not) and
`faced_cbet_turn=large` (the c-bet they were facing).

---

## Showdown

| Stat | Numerator | Denominator | Exclusions |
|---|---|---|---|
| **WTSD** | Player reached showdown without folding | Player saw the flop | Hands ending preflop |
| **W$SD** | Player collected > 0 | WTSD numerator | — |

**Showdown detection** is "two or more players had not folded when the hand ended",
*not* the presence of `shows` lines. Players voluntarily show after winning
uncontested, and rabbit-hunt shows appear between hands entirely — using `shows`
would inflate WTSD for exhibitionists.

**"Saw the flop"** means dealt in, did not fold preflop, and a flop was actually
dealt.

---

## Lines and sizing

These facts exist so a *line* -- "opened 4bb+, single-raised pot, c-bet flop and
turn, overbet river" -- is just a longer filter, and so a range chart can colour a
cell by how big the player usually raises with it.

| Fact | Definition | Notes |
|---|---|---|
| **opener** | Made the level-2 raise | Exactly one per hand at level ≥ 2 |
| **pfa** | Last preflop aggressor | The player who owns the flop c-bet |
| **pot_level** | Highest preflop level the hand reached | 1 limped/walk, 2 `srp`, 3 `3bet_pot`, 4+ `4bet_pot`; identical for everyone in the hand |
| **open_bb** | The open raise's `raises to N`, over this hand's big blind | Same value for everyone in the hand; `None` in a limped pot |
| **pf_raise_bb** | This player's own *last* preflop raise-to, in big blinds | An opener who 4-bets ends above their open |
| **bet_pot[street]** | This player's *first* `bet` on that street, over the pot it was made into | 1.0 is a pot-sized bet, above it is an overbet |

**Sizes use the raw `raises to N` figure**, not the incremental amount: "opened
to 4bb" is about the total. Bet fractions use the incremental amount over the pot
*before* the bet, forced posts included.

**Only a `bet` gets a `bet_pot` entry.** A raise over someone else's bet is a
different decision and is not folded into it. A filter for "bet the river" is
therefore `bet_river` (the flag) or `bet_river>=1` (the size).

**Comparisons against a missing value are false, not errors.** `open_bb<3` does not
match a limped pot; the hand has no open size, so it cannot satisfy any claim
about one.

### Board texture

`flop=<tag>`, `turn=<tag>`, `river=<tag>` and `board=<tag>` (the board as dealt,
three to five cards) filter on the community cards; `!=` negates. A street needs
that many cards dealt, so a hand that ended preflop matches no texture term,
positively or negatively.

| Group | Tags | Rule |
|---|---|---|
| highest card | `ace_high` `king_high` `queen_high` `jack_high` `ten_high` `low` | exactly one; `low` is 9-high or below |
| suits | `monotone` `twotone` `rainbow` `flush_possible` | all one suit / exactly two suits present / no suit repeated / three or more of one suit |
| pairing | `paired` `double_paired` `trips` `unpaired` | any rank repeated; two different pairs; three of a rank |
| connectivity | `connected` `three_connected` `disconnected` | two adjacent ranks (ace plays high and low); three in a row; neither |
| rank mix | `all_broadway` `no_broadway` | every card T or above / every card 9 or below |

Tags overlap on purpose (a monotone flop is also `flush_possible`; a trips board is
also `paired`) so a filter can be as broad or narrow as the question.

### Sizing statistics

Wherever a raise size is reported it comes with `n`, `min`, `max`, `mean`,
`median` and `mode` (on a tie, the mode nearest the median). A range view also
reports these over **every** hand in the spot, shown or not -- sizing needs no
showdown, so it is the one range figure with full coverage.

### The habitual size (`raise_typical`)

An all-in is logged as an ordinary `raises to N`, so one tilt jam enters a cell as
a 270bb "raise" beside a row of 3bb opens. Neither average survives that alone, so
which one is used depends on whether the cell's sizes agree. `tolerance` is the
spot's own median raise, floored at 1bb: a 3bb opener drifting between 2bb and 5bb
is sizing consistently, and that same spread would be noise in a 10bb game.

| Cell | Basis | Why |
|---|---|---|
| spread ≤ tolerance | `mean` | the sizes agree, so no outlier is present and the mean uses all of them |
| spread > tolerance, n ≥ 3 | `median` | an outlier is present; the median steps over it |
| spread > tolerance, n = 2 | `midpoint` | no third raise to break the tie, so their midpoint stands in |
| n = 1 | `single` | the only evidence there is; the cell shows its own sample count |

A `midpoint` is deliberately a size the player never used. It summarises two
contradictory raises rather than claiming a habit, and the basis says so, because
"raised small once and large once" is still worth seeing on the chart. It is the
one basis that a single jam can still stretch: 6.7bb and 456bb reports 231bb.

The chart colours each cell by the distance from `raise_typical` to the spot's
median: the exceptions are the signal, and everything sized normally stays grey.

`mode` is `None` whenever no size repeats. Two raises of 3.5bb and 15bb have no
most-common size, and returning either one would report list order as a fact about
the player.

---

## Ranges

A range view is a filter, then a bucketing of the hands whose cards are known:

- `preflop`: the 169 starting-hand classes, laid out as the standard 13x13 chart.
- `made`: best-five strength on the final board (first run for run-it-twice),
  with a `detail` for pairs (overpair / top / middle / bottom / pocket / board
  pair) and trips (set vs trips).

**Coverage** (`known / hands`) is reported alongside and is the honest part.
Cards are known only when shown, and hands are shown when they reach showdown.
A "3-bet range" from this data is really "3-bet hands that reached showdown":
the bluffs that folded out are exactly the ones missing, and nothing here
corrects for that. Percentages inside a range view are of *known* hands.

Cards shown **after** the hand ended are *not* in `hole_cards`: a fold the player
wants credit for, an uncontested win, a muck revealed late. PokerNow logs these
past `-- ending hand #N --`, and they are stored separately in `voluntary_shows`
(with context in the `v_voluntary_shows` view). They carry the opposite bias --
players show the bluffs they are proud of -- so no range view includes them.
Across the fixture logs that is 157 show lines, 52 of them two-card shows from
hands that ended before the river.

---

## Money

```
contributed = Σ(incremental chips committed, including forced posts) − uncalled returns
net         = collected − contributed + bounty
bb/100      = (Σ net / big_blind) / hands × 100
              over complete hands with a known big blind only
```

`contributed` subtracts uncalled bets because chips returned to you were never at
risk. Verified against hand #187 of the sample log: Chris posts SB 5, raises to 30,
opponent folds, 20 is returned, he collects 20 — net **+10**, which is the
opponent's big blind and nothing else.

**Incomplete hands are excluded from bb/100, and only from bb/100.** A log that
stops mid-hand leaves that hand with chips in the pot and no `collected` line, so
everyone still in it reads as having lost everything they put in — a loss that
never happened, and one that can only ever bias downward. The hand is dropped from
both sides of the bb/100 fraction, on the same principle that prints `--` for a
rate with no opportunities: unknown is not zero.

Everything else about such a hand is real and still counts. The players folded,
bet, called and reached showdown exactly as recorded, so VPIP, PFR, 3-bet, c-bet
and WTSD all include it. Only the ledger is short.

`hands` therefore counts every hand, while the bb/100 denominator may be smaller.
Re-importing the finished export repairs the hand and it rejoins.

On the 6,255-hand database this was developed against, excluding the two truncated
hands moves the whole-table ledger from −3.29 bb to −0.43 bb of 10,731 bb gross,
and every one of the 6,253 complete hands conserves chips exactly.

---

## Known judgement calls

These are choices, not facts. They are listed so they can be revisited without
archaeology.

1. **BB walks are excluded from VPIP/PFR denominators.** Rule 3 above.
2. **A checked-through street clears the aggressor**, so no c-bet opportunity
   follows it.
3. **Run-it-twice counts as W$SD-won when total collected > 0**, even if the player
   lost one of the two runs. A player who wins one run and loses the other shows as
   a win here while being break-even in chips. Double Board hands split the pot the
   same way and follow the same rule; made-hand views read the first board only.
4. **Hands with a dead button or a dead blind are excluded from positional
   splits.** Two distinct cases, both real in the fixtures:

   - `dead_button = 1` — the hand-start line reads `(dead button)` and names no
     dealer at all. Positions are anchored on the big blind instead.
   - `blinds_irregular = 1` — a player left and the small blind went **dead**, so
     a position slot exists that no dealt-in player occupies. Hand #25 of
     `pgl1UViJ4` has the button on seat 2, a `Dead Small Blind` line, and seat 10
     posting the *big* blind one slot from the button. `seats_from_button` is
     shifted past the gap, which means it can exceed `n_dealt_in - 1` on these
     hands.

   Both are rare (1 each in 549 hands) and both are flagged rather than guessed
   at. `positional_report` drops them; aggregate stats still include them, since
   only the *position label* is uncertain, not the actions.
5. **Antes and dead small blinds** contribute to the pot but not to a player's
   street commitment, so they do not reduce what that player must pay to call.
6. **Each bet-size edge belongs to the bucket above it, rounded down to a chip**,
   so every PokerNow ½, ¾ and pot click lands in the bucket it starts, and a
   pot-sized bet is `large` rather than an overbet. See "Bet size buckets".
7. **A donk bet needs a previous-street aggressor who has yet to act.** A lead
   after a checked-through street, or into an aggressor who is already all-in,
   is not counted as one.
