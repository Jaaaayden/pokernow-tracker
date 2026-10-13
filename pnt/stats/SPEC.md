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

### Straddles

| Fact | Definition |
|---|---|
| **straddled** | Some player made a forced post whose `post_kind` is `straddle`. Same value for everyone in the hand |

Filter: `straddled`, and `!straddled` for the rest.

Judgement calls:

1. **A straddle changes no level.** The level-1 amount already includes it (see
   above), so the open, the 3-bet and every other level read the same on a
   straddled hand; `open_bb` is still the open over the hand's big blind, not
   over the straddle. A straddled pot is a pot type of its own only through this
   flag.
2. **The straddler has a live blind.** Their post is their bet for the street,
   exactly as a big blind's is, so they are never a cold caller ("Squeeze and
   cold-call") -- but the straddle seat is not a blind for blind defense, which
   reads the seat names.
3. **Only the log's word counts.** A post is a straddle when PokerNow's line says
   `posts a straddle`; a post of twice the big blind under another name is not
   one, and a post with no recorded kind is not one either.

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

### Decision points

Every preflop decision is taken at some bet level, and the level *is* the spot:
"facing an open" is level 2, "facing a 3-bet" is level 3. One fact carries the
whole path.

| Fact | Definition |
|---|---|
| **pf_faced[level]** | What the player did the (only) time they acted at that level: `fold`, `check`, `call` or `raise`. Recorded before the action resolves, like every opportunity. A preflop `bet` is a `raise`. |

A player acts at most once per level — action only comes back to them after a
raise, which is the next level — so the dict is the player's decision path.

| Level | Spot term | Acting there is… | Raising there is… | Calling there is… |
|---|---|---|---|---|
| 1 | `unopened` | had a decision with no raise in front | `opener` | `limp` |
| 2 | `faced_open` | same hands as `3bet_opp` | `3bet` | `called_open` |
| 3 | `faced_3bet_any` | opener or not | `4bet` | `faced_3bet_any=call` |
| 4 | `faced_4bet` | | `5bet` | `faced_4bet=call` |
| 5 | `faced_5bet` | | `faced_5bet=raise` | `faced_5bet=call` |

`<spot>=<decision>` filters on the decision itself, so `faced_open=fold` is the
hands where they folded to an open. `faced_3bet` and `faced_3bet=<decision>` stay
**the opener's alone**, because Fold to 3-Bet is defined on them; `faced_3bet_any`
covers the cold-caller and the squeezed player too. `limp` is the action and
`limped` the pot type.

Two more rates read the path directly:

| Stat | Numerator | Denominator | Filter (num / den) |
|---|---|---|---|
| **Limp-reraise** (`limp_reraise`) | `pf_faced[1] == call` and `pf_faced[2] == raise` (`limp_raise`) | Limped, then acted facing the open: `limp_raise` + `limp_call` + `limp_fold` | `limp,faced_open=raise` / `limp,faced_open` |
| **Preflop jam** (`jam_pf`) | `jam[preflop]`: an all-in bet or raise | Had a preflop decision (`vpip_opp`, kept as `jam_pf_opp`) | `jam_preflop` / `acted_preflop` |

A limper whose next decision is facing a 3-bet -- someone opened and someone else
3-bet before it came back -- never faced the open, so is in neither side of
limp-reraise. An all-in call is not a jam ("Jams").

### Steals and blind defense

A steal is an open from late position that nobody in front of it wanted: the
blinds are the only players left to beat.

| Fact | Definition |
|---|---|
| **first_in** | At the player's first voluntary preflop action, every earlier voluntary preflop action was a fold. Forced posts do not count, so the player after a straddle can be first in |
| **steal_open** | The hand's opener was `first_in`, from a steal seat (CO, BTN or SB; heads-up the BTN/SB), on a hand that is neither `dead_button` nor `blinds_irregular`. Same value for everyone in the hand |
| **callers_before** | Calls made at level 2 before this player's first decision at level 2. None when they never acted facing the open |
| **position_label** | The seat's name (`position_name`), or None on a dead-button or dead-blind hand -- the rule `positional_report` follows. A property, not stored |

| Stat | Numerator | Denominator | Exclusions |
|---|---|---|---|
| **ATS** (`ats`) | Opened (`opener`) | `steal_opp`: `first_in` from a steal seat | Dead button; dead blind |
| **Fold to steal** (`fold_to_steal`) | `pf_faced[2] == fold` | `fold_to_steal_opp`: in the SB or BB, the hand's open was a steal, and `callers_before == 0` | Same, through `steal_open` |
| **3-bet vs steal** (`3bet_vs_steal`) | `pf_faced[2] == raise` (`three_bet_vs_steal`) | Same as fold to steal | Same |

Filters: `first_in`, `steal_opp`, `steal`, `fold_to_steal_opp`, `fold_to_steal`,
`3bet_vs_steal`, and `callers_before>=N`.

Judgement calls:

1. **Seats are trusted only where `positional_report` trusts them.** The
   `position=` filter drops a dead button but labels a dead-blind hand; a steal
   is a claim about the seat, so both are out, as in "Known judgement calls" 4.
   `first_in` is a claim about the action alone and stays set on them.
2. **A steal folded to the blinds needs no caller in between.** One call and the
   blind is facing an open and a caller, which is the squeeze spot, so
   `callers_before == 0` is part of the defense opportunity.
3. **Heads-up, the button's open is a steal**, and the big blind defends it. The
   small blind folding first in from the BTN/SB is a steal opportunity it
   declined.
4. **A blind's 3-bet of a steal is a 3-bet**, and counts in the 3-bet stat as
   well: 3-bet vs steal is a slice of it, not a separate action.

### Squeeze and cold-call

| Fact | Definition |
|---|---|
| **posted_blind** | The player made a forced preflop post that is a live blind: small blind, big blind, straddle, or a returning player's missed big blind. Antes and dead small blinds are not live (`post_kind` `ante` and `missing_sb`), and a post of no recorded kind counts as live |

| Stat | Numerator | Denominator | Exclusions |
|---|---|---|---|
| **Squeeze** (`squeeze`) | `pf_faced[2] == raise` | `squeeze_opp`: acted facing the open with `callers_before >= 1` | — |
| **Cold-call** (`cold_call`) | `pf_faced[2] == call` | `cold_call_opp`: acted facing the open, with no live blind posted and no earlier decision this hand (`1 not in pf_faced`) | Blinds and straddlers; limpers |

Filters: `squeeze_opp`, `squeeze`, `cold_call_opp`, `cold_call`.

Judgement calls:

1. **"Not in the blinds" is read from the posts, not the seat names.** A player
   with a live blind in front of them is not coming in cold, whatever seat the
   label gives them: the straddler and the returning player's missed big blind
   are out with the small and big blinds. Reading posts also keeps the dead-button
   hands, whose labels are guesses.
2. **A limper is never a cold caller.** Their limp is their first decision, so
   the call of the open that follows is `limp_call`.
3. **A squeeze is a 3-bet.** Every `squeeze` is a `three_bet`, and the limper who
   re-raises an open and its callers is both a squeeze and a limp-reraise.
4. **The callers are callers of the open.** Limps before the open are level 1 and
   do not count toward `callers_before`; a limper who then calls the open does.

---

## Postflop

**Aggressor**: the last player to `bet` or `raise` on a street. If a street checks
through it has **no** aggressor, so there is no c-bet opportunity on the next
street. (A bet on a street after one that checked through is a delayed c-bet or a
probe, not a continuation bet -- see "Delayed c-bet, probe and float". Some
trackers carry the aggressor forward; this one does not.)

| Stat | Numerator | Denominator | Exclusions |
|---|---|---|---|
| **C-Bet** (flop/turn/river) | Player bets | Player was the previous street's aggressor, saw this street, and acts while no bet has yet been made on it | Previous street checked through; player already all-in |
| **Fold to C-Bet** | Player folds | Player acts facing a c-bet, before anyone raises over it | — |
| **Raise C-Bet** (flop/turn/river) | Player raises | Same as Fold to C-Bet | — |
| **Call C-Bet** (flop/turn/river) | Player neither folds nor raises | Same as Fold to C-Bet | — |
| **Donk Bet** (flop/turn/river) | Player bets | Player acts while no bet has yet been made on this street, the previous street's aggressor has not acted on it yet, and that aggressor is not all-in | The aggressor themselves; previous street checked through |
| **Check-Raise** (flop/turn/river) | Player's line on the street starts `xr` | Player checked and then acted again on the street (`check_raise_opp`) — action only comes back after a bet | Player who never checked; a check that closed the street |
| **Aggression Frequency** (per street) | `bet` + `raise` | `bet` + `raise` + `call` + `fold` | `check` is excluded from **both** sides |

Aggression frequency deliberately excludes checks. Including them makes the stat
measure how often a player is out of position rather than how aggressive they are.

A donk bet (a *lead*) is a bet into the previous street's aggressor before they
get to act. Like the c-bet it needs an aggressor: a bet after a checked-through
street is a probe and counts as neither.

### Facing a bet, the aggressor, and checking back

The c-bet family above is defined against one bettor, the previous street's
aggressor. Three generic facts cover the rest, and the tags read them.

| Fact | Definition | Notes |
|---|---|---|
| **faced_bet[street]** | Acted while a bet or raise had already been made on the street | Whoever made it. A bettor who is raised and acts again *faced a bet*: their own bet is not the last word. `fold_to_cbet_opp` is a subset |
| **folded_to_bet / called_bet / raised_bet[street]** | What they did there | Not exclusive: a player who called and then raised over a re-raise has both |
| **aggressor[street]** | Made the street's last bet or raise | The aggressor of the Postflop section; `pfa` is the preflop one |
| **check_back[street]** | Their check was the last action of a street that checked through | Needs two or more players to have acted on the street: a lone check against a player already all in decided nothing |

A street with no bet ends on a check, and exactly one player's check closed it,
so a street has a `check_back` for at most one player and never alongside an
`aggressor`.

The three above are not exclusive, so they cannot be counted against each other.
Four more facts take the player's **first** decision facing a bet on the street —
same keys as `faced_bet` — and are:

| Fact | Definition | Notes |
|---|---|---|
| **faced_level[street]** | How many bets and raises the street had when they first faced one: 1 a bet, 2 a raise, 3 a re-raise | The bettor who is raised first faces level 2 |
| **faced_bet_pot[street]** | The latest bet or raise in front of them then, measured as its maker's own size: `bet_pot` for a bet, `raise_pot` for a raise | The size of what they faced, not the price of calling it -- the rule `faced_cbet_size` already follows |
| **faced_bet_size[street]** | The size bucket of that same bet or raise | |
| **faced_bet_action[street]** | `fold`, `call` or `raise`, exactly one | A caller who later re-raises a re-raise answered with `call` |

**Fold to bets by size** (`vs_bet_<street>_by_size` in the stat row) counts every
first decision facing a bet by `faced_bet_size`, with the fold, call and raise
rates of each bucket. It is the c-bet-only `vs_cbet_<street>_by_size` widened to
anyone's bet or raise. Keys are the five size buckets: `{faced, fold, call, raise}`.

Filters: `faced_bet_river`, `folded_to_bet_river`, `called_bet_<street>`,
`raised_bet_<street>`, `aggressor_<street>`, `check_back_<street>`, and
`check_back` for any street. `faced_bet_river=large` and `faced_bet_river>=1`
read `faced_bet_size` and `faced_bet_pot`.

### What a bet got

| Fact | Definition | Notes |
|---|---|---|
| **bet_answer[street]** | What the player's first `bet` on the street got from the players behind it: `raise` if anyone raised over it, else `call` if at least one player called, else `fold` | No key when nobody acted on it. Keys are a subset of `bet_size` |

The answers run until the first raise over the bet, and that raise is the answer
whatever came before it: called by one player and raised by the next is a raised
bet. `fold` means every player who acted on it folded. This is the reading
`review.failed_bluff` already gives its responders.

**What each bet size got** (`bet_<street>_answer_by_size`) is the bettor's side of
fold equity: per size bucket, `{n, fold, call, raise}`, where `n` is every first
bet of that size and the three rates are of `n`. Their `n`s add up to the bets
made on the street.

Filters: `bet_answer_river=fold`, `=call`, `=raise`.

### Delayed c-bet, probe and float

What happens on the streets after the aggressor gives one up. The c-bet and the
donk bet are both defined against the previous street's aggressor; these three
cover the bets the aggressor's check leaves open. "PFA" is the preflop aggressor
(`pfa`), who stays the same on every street.

| Stat | Numerator | Denominator (opportunity) | Streets |
|---|---|---|---|
| **Delayed c-bet** (`delayed_cbet_<s>`) | Player bets | The player is the PFA, every earlier postflop street was dealt and checked through, and they act on this street with no bet yet made on it | turn, river |
| **Probe** (`probe_<s>`) | Player bets | Not the PFA; the previous street checked through and the PFA's check closed it (`check_back` by the PFA); they act on this street with no bet yet, before the PFA has acted on it | turn, river |
| **Fold to probe** (`fold_to_probe_<s>`) | The PFA folds | The PFA acts facing a probe bet, before anyone raises over it | turn, river |
| **Float** (`float_<s>`) | Player bets | The player called on the previous street (`called_bet[prev]`; on the flop, put chips in preflop without being the PFA), the previous street's aggressor has acted on this street -- checked, since no bet is yet made -- and they act with no bet yet | flop, turn, river |

"Checked through" is the `check_back` rule: no bet or raise, and a check by one of
two or more players closed it. The fact for a float is `float_bet` (and
`float_opp`), so it does not shadow the builtin; the stat key and the filter are
`float_<s>`.

**The families are disjoint from the c-bet and the donk bet, and from each
other, on any one street.** A delayed c-bet and a probe follow a street that
checked through, which has no aggressor, so neither a c-bet nor a donk
opportunity exists there. A float needs an aggressor who has *already* acted on
the street, where a donk needs one who has *not* -- the float is "bet when the
c-bet was missed", the donk "bet before the c-bet could be made". And a float
needs an aggressor on the previous street, which the other two rule out. A bettor
whose opportunity is none of the five is betting into a street nobody owned.

Filters: `delayed_cbet_<s>`, `delayed_cbet_<s>_opp`, `probe_<s>`,
`probe_<s>_opp`, `faced_probe_<s>` (the fold-to-probe opportunity),
`folded_to_probe_<s>`, `float_<s>`, `float_<s>_opp`, and the any-street forms
`delayed_cbet`, `delayed_cbet_opp`, `probe`, `probe_opp`, `faced_probe`,
`folded_to_probe`, `float`, `float_opp`.

Judgement calls:

1. **A delayed c-bet needs every earlier street checked.** The PFA who c-bets the
   flop, checks the turn and bets the river is not delayed: the flop was bet.
   That river is the other player's probe spot when the PFA's check closed the
   turn, and the PFA's own river bet there is in none of the five.
2. **A probe is against the PFA, not the last aggressor.** The street before was
   checked through, so it has no aggressor; the player a probe tests is the one
   who owned the hand preflop and declined to keep betting.
3. **"In position" for a float is behind the aggressor.** The aggressor checked to
   them, so they act after the aggressor on the street; multiway they need not
   be last of everyone. This is the bet against a missed c-bet, and the c-bettor
   is the one it is against.
4. **A flop float is open to every non-PFA player who put chips in preflop.** On
   a raised pot that is everyone who called a raise, the big blind included.

### Who the action was against

Every postflop stat above is defined against an *aggressor*, so the aggressor's
identity is a fact about the spot and not merely a step in computing it. Three
facts carry it, and a drill-down names them.

| Fact | Definition |
|---|---|
| **faced_cbet_by[street]** | The player whose c-bet they were facing. Keys match `fold_to_cbet_opp` exactly |
| **donk_into[street]** | The previous street's aggressor they had the chance to lead into. Keys match `donk_opp` |
| **opponents** | The other players who had not folded **at the time of this player's last action**, in postflop acting order |

`opponents` is "still in when I last acted", not "saw the flop". The villain who
folds to your c-bet *is* someone you faced; a player who folded before you ever
acted is not.

A consequence worth stating, because it looks like a bug: **a villain can appear
in `faced_cbet_by` and not in `opponents`.** Three-handed, A c-bets, you call, B
raises and A folds — A c-bet at you, and A was gone before you were done with the
hand. The two facts answer different questions and are both right.

A player who never acted voluntarily — a walk, or all-in from a post — has no
last action to reckon from, so their `opponents` is whoever had not folded when
the hand ended. On a walk that is nobody.

### In position (IP / OOP)

Absolute seat labels (BTN, SB) say where a player sat. What the postflop stats
actually turn on is whether they **close the action**, so that is what is
recorded.

| Fact | Definition |
|---|---|
| **pos_order** | Their place in postflop acting order among `opponents` plus themselves; 0 acts first |
| **pos_players** | How many players that order covers, this player included |
| **in_position** | True when they act last of them. Always `pos_order == pos_players - 1` |

Two signals, in priority order:

1. **First-orbit acting order** — the distinct players in the order they first
   acted on the earliest postflop street that saw voluntary action. Used only
   when *every* player in the group acted there.
2. **Seat order** otherwise: small blind first, button last. Counted in **slots**
   (`seats_from_button - 1`, with the button wrapping to the end), never
   `(seats_from_button - 1) mod n_dealt_in` — a dead small blind pushes a player
   past `n_dealt_in - 1`, and the modulo then collides two players onto one rank.
   Hand #25 of `pgl1UViJ4` is three-handed with seats 0, 2 and 3, where
   `(0-1) mod 3` and `(3-1) mod 3` are both 2.

When neither applies, all three are `None`. **Unknown is not "out of position"**,
on the same principle that prints `--` for a rate with no opportunities.

Because the group is each player's own `opponents`, **two players in one hand can
both be in position** — each closed the action over the players still in when
*they* last acted. A row is always one player's view of the hand, so the two never
appear side by side, but the facts are per-player and not a single ranking of the
table.

The order signal is preferred because it is *raw*: it is who actually acted after
whom, which is true even on the hands where the position label is a guess. On the
6,255-hand database the two signals agree on 11,259 of 11,259 comparable rows on
regular hands; the only rows where they disagree are dead-button hands, and there
the acting order is the one that is right.

### Bet size buckets

Every postflop `bet` also gets a size bucket, measured the same way as `bet_pot`:
the bet over the pot it was made into.

| Bucket | Size | Rule |
|---|---|---|
| `blocker` | under ⅕ pot | `amount < pot // 5` |
| `small` | ⅕ to ½ pot | `amount ≥ pot // 5` and `amount < floor(½ × pot)` |
| `medium` | ½ to ¾ pot | `amount ≥ floor(½ × pot)` |
| `large` | ¾ pot to pot | `amount ≥ floor(¾ × pot)` and `amount ≤ pot` |
| `overbet` | more than pot | `amount > pot` |

**PokerNow's ½, ¾ and pot buttons each start a bucket.** They are most of the
bets: of 1,016 flop c-bets in a 5,836-hand database, 211 were exactly ½ pot, 62
exactly pot and 28 exactly ¾. With the edges placed anywhere else, the same
button click would land on either side of one. Bets are whole chips, so an edge
is rounded down to a chip: a ¾-pot click into a pot of 30 is 22 or 23 chips, and
both are `large`. A pot-sized bet is `large`, not an overbet.

**A blocker is a bet too small to ask much of anyone**: the one-blind poke into a
big pot, made to set the price of the river rather than to win it. No button
makes it, so its edge is a round number rather than a click, and it is taken in
integers, `pot // 5`, never `0.2 × pot` -- the float lands a chip either side of
the same edge on different pots.

The same buckets size a raise, measured by `raise_pot` (see "Raise sizes").

**Fold to C-Bet and Raise C-Bet by size** split both stats by the bucket of the
c-bet the player faced -- that c-bet's own size, not the price of calling it.

Filters use the bucket names: `cbet_flop=medium` (a flop c-bet of that size),
`bet_river=overbet` (their first river bet, c-bet or not) and
`faced_cbet_turn=large` (the c-bet they were facing).

### Fold equity by bet size

`fold_equity(facts)` (`stats/fold_equity.py`) puts the two sides of a bet next to
the price of bluffing at that size, per street and size bucket. It reads the facts
above and adds none.

**A pure bluff's break-even.** A bet of `s` pots with no equity wins the pot (1)
when everyone folds and loses `s` otherwise, so it breaks even when it is folded
to `s / (1 + s)` of the time: ⅓ for a half-pot bet, ½ for a pot-sized one. The
defender's side of the same number is the **minimum defense frequency**,
`MDF = 1 − s / (1 + s) = 1 / (1 + s)`: how often they must call or raise to keep
that bluff from profiting. ⅔ against a half-pot bet.

| Side | Rows | Per bucket | Against | Filter (`n`) |
|---|---|---|---|---|
| **Bettor** (`bets`) | Every first bet on the street, by `bet_size` | `n`; `fold`, `call`, `raise` from `bet_answer`; `unanswered` | `break_even`; `edge` = fold % − break-even, in points | `bet_<street>=SIZE`; the folded part `bet_<street>=SIZE,bet_answer_<street>=fold` |
| **Defender** (`faced`) | Every first decision facing a bet or raise, by `faced_bet_size` | `n`; `fold`, `call`, `raise` from `faced_bet_action` | `mdf`; `overfold` = fold % − (100 − MDF), in points | `faced_bet_<street>=SIZE` |
| **Defender, c-bets only** (`faced_cbet`) | Every c-bet faced before anyone raised it, by `faced_cbet_size` | as above, from `fold_to_cbet` / `raise_cbet` | as above | `faced_cbet_<street>=SIZE`; the folded part adds `folded_to_cbet_<street>` |

Each row also carries `mean_size` and `median_size` (pot fractions) and `judged`
(at least `MIN_SAMPLE` = 10 rows). A positive `edge` means a pure bluff of that
size, made as often as these bets were, would have shown a profit; a positive
`overfold` means the player gives up more than the bet demands, and bluffing them
at that size prints. The views highlight both only on a judged row: a 100% fold
rate off two bets says nothing about the next one.

Judgement calls:

1. **The break-even is the mean of each bet's own `s / (1 + s)`**, not the
   break-even of the mean `bet_pot`. A bluff of `s` pots folded to with probability
   p makes `p − (1 − p)s` pots, which over the pot *after* the bet, `1 + s`, is
   `p − s / (1 + s)`. So fold % minus this mean is a bluff's average result per
   bet, each measured against the pot it made, and the edge is zero exactly at
   break-even. The break-even of the mean size is the same zero with each bet
   measured against the pot *before* it, where one 200-pot jam into a limped pot
   risks as much as four hundred half-pot bets: on the 14,543-hand database it moved the
   flop overbet bucket's break-even from 70% to 84% by itself. Inside the four
   bounded buckets the two readings agree to a few tenths. MDF is averaged the
   same way, and `median_size` is the size a view prints, for the same reason.
2. **Rates are of every bet in the bucket**, the reading `bet_<street>_answer_by_size`
   already gives, so a page showing both never disagrees with itself. A bet nobody
   answered (a log that stops after it) counts in none of the three, and is
   reported as `unanswered`. The real database has none.
3. **The bettor's fold % is everyone folding; the defender's is one player's
   fold.** Multiway, a bet three players face is one bet and three decisions, so
   the pool's defender fold % runs above its bettor fold % at the same size, and
   should.
4. **The defender's rows include raises faced**, sized by `raise_pot`: a raise of
   `s` is a bet of `s` into the pot once called, which is the pot-limit
   convention ("Raise sizes") and the reading MDF takes of a raise. A player's
   first decision is the one counted, as in `vs_bet_<street>_by_size`. The
   defender's fold has no filter of its own: no term reads `faced_bet_action`, and
   `folded_to_bet_<street>` also matches a call followed by a fold to the raise.
5. **An unraised c-bet is the first bet a player faces on the street**, so its
   pot fraction is that player's `faced_bet_pot`, and the c-bet rows read it
   there: there is no `faced_cbet_pot`.
6. **No cards are needed**, so there is no coverage figure: every bet and every
   fold is in the log. The other bias still applies in reverse: this says how
   often a bet *won*, not what it was. Most bets that got called were not bluffs.

The **pool** (`GET /fold_equity`, `pnt foldeq --pool`) is the same computation over
every (hand, player) row in the database, so its counts are the players' counts
summed. Its rows carry filters but no hand ids, since one hand is several rows.
`GET /players/{alias}/fold_equity` adds each row's `hand_ids` and, where the
folded part has a filter, `fold_hand_ids`. Both take `filter=` and `game=`.

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

| Fact | Definition |
|---|---|
| **players_flop** | How many players saw the flop: the count of `saw_flop` rows in the hand. None when no flop was dealt. Same value for everyone in the hand |

Filter: `players_flop=2` (heads-up on the flop), `players_flop>=3` (multiway).

### Multiway vs heads-up

The stats page's **Split by: players on the flop** shows each player as two
sub-rows, one per group, under their row for the whole spot:

| Group | Term ANDed with the spot | Holds |
|---|---|---|
| **HU** | `players_flop=2` | Exactly two players saw the flop |
| **3+ way** | `players_flop>=3` | Three or more saw it |

Every rate keeps its own numerator and denominator; the split only narrows the
hands to one group, so a sub-row is reproduced by its filter (`<spot>,<term>`) on
the chart or `/stats`. Hands with no flop are in neither group. Because the two
terms partition the flop hands, each player's two sub-rows sum to their whole
row on every postflop count: flops seen (the WTSD denominator), c-bet chances,
c-bets faced and so on.

The HUD's closest-spot search carries the same group on every postflop node
(see "Decision points (nodes)").

Judgement calls:

1. **Two groups, not the exact count.** A 4-way flop is read with the 3-way
   ones: four-handed flops are too rare to stand alone, and what changes a
   c-bet is the second caller, not the third.
2. **`players_flop` is a hand fact, so `hands` in a sub-row counts the player's
   hands in that group, flop seen or not.** Their two `hands` sum to the hands a
   flop was dealt on, not to the flops they saw. The preflop columns of a
   sub-row likewise describe hands that went on to be heads-up or multiway:
   context for the postflop columns, not a preflop tendency of its own.
3. **The page's minimum hands applies to the whole row.** A player who clears it
   is shown with both groups, however few hands each holds; every rate there
   carries its sample size on hover.

### Won when saw flop

| Fact / Stat | Numerator | Denominator | Exclusions |
|---|---|---|---|
| **won_pot** (fact) | `collected > 0` -- the reading W$SD takes | — | A 7-2 bounty is **not** winning the pot: it is paid beside the pot, never from it |
| **WWSF** (`wwsf`) | `saw_flop` and `won_pot` | `saw_flop` (the WTSD denominator) | Hands ending preflop |
| **WWSF, no showdown** (`wwsf_nsd`) | `saw_flop`, `won_pot` and not `wtsd` | Same as WWSF | Same |

| Fact | Definition |
|---|---|
| **bounty** | The signed 7-2 side-bet result, `HandPlayerRow.bounty`: positive when paid to the player, negative when they paid, 0 when none. Part of `net`, never of the pot |

Filters: `bounty_won` (`bounty > 0`) and `bounty_paid` (`bounty < 0`).

Filters: `won_pot`; WWSF is `saw_flop,won_pot` over `saw_flop`, and the
no-showdown part adds `!wtsd`. `fold_street=<street>` keeps the hands folded on
that street, preflop included.

Judgement calls:

1. **Winning the pot is collecting from it, not finishing ahead.** A player who
   takes a side pot smaller than what they put in has `won_pot` and a negative
   net, and a player paid a bounty on a lost hand has neither `won_pot` nor a
   pot. This is W$SD's reading, so WWSF's showdown part and W$SD count the same
   hands.
2. **A chopped pot is won**, by everyone who collected, as in W$SD. So the WWSF
   of the players in one hand can sum past 100%.
3. **`wwsf_nsd` shares WWSF's denominator**, so the two stack: `wwsf_nsd ≤ wwsf`,
   and the difference is the share of flops won at showdown.

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
| **pot_at[street]** | Chips in the middle when that street was dealt | Forced posts included, capped at `pot`; only the streets that were dealt; identical for everyone in the hand. The flop's is what an SPR is measured against |
| **pot** | The final pot in chips: Σ `contributed` over everyone dealt in | Uncalled bets and bounties excluded; identical for everyone in the hand; short on an incomplete hand, like `net` |

`pot>=500` and `pot_bb>=50` filter on it in chips and in that hand's big blinds.
`vs=<name>` filters on `opponents` (see "Who the action was against"): the named
player was still in when this player last acted. `vs!=` negates, and names match
case-insensitively against the display name or the raw PokerNow ID.

**Sizes use the raw `raises to N` figure**, not the incremental amount: "opened
to 4bb" is about the total. Bet fractions use the incremental amount over the pot
*before* the bet, forced posts included.

**Only a `bet` gets a `bet_pot` entry.** A raise over someone else's bet is a
different decision and is not folded into it. A filter for "bet the river" is
therefore `bet_river` (the flag) or `bet_river>=1` (the size).

**Comparisons against a missing value are false, not errors.** `open_bb<3` does not
match a limped pot; the hand has no open size, so it cannot satisfy any claim
about one.

**`!` negates a flag, and only a flag.** `!cbet_flop` is every hand `cbet_flop`
does not match, the hands that never saw a flop included; add `cbet_flop_opp` for
"could have c-bet and did not". A comparison, a size or a texture has its own `!=`
where negating it means something, and `!pot>=500` could be read two ways, so it
is refused.

### Action lines

A line is what one player did on one street, written as one letter per voluntary
action: `x` check, `b` bet, `c` call, `r` raise, `f` fold. `xc` is a check-call,
`bf` a bet that folded to a raise, `xrc` a check-raise that called the re-raise.

| Fact | Definition | Notes |
|---|---|---|
| **line[street]** | The player's own voluntary actions on that postflop street, in order | No key for a street they did not act on: not reached, already folded, or already all in. Never empty when present; only the last letter can be `f` |
| **check_raise[street]** | The line starts `xr` | |
| **check_raise_opp[street]** | The line starts `x` and has a second letter | Action only comes back to a player who checked when someone bet behind them, so this is "checked and faced a bet" |
| **fold_street** | The street of their voluntary fold, preflop included | `None` when they never folded |

Lines are filters: `line_flop=xc` is exactly that line; `*` stands for any run of
letters, none included, and `?` for exactly one, so `line_flop=x*` is every flop
that started with a check, `line_river=*r` every river that ended on a raise and
`line_turn=?c` a call as their second action. `-` alone is "did not act".
Any other pattern needs them to have acted, so `line_turn=*` is "acted on the
turn". `line_flop!=x*` is the exact opposite of `line_flop=x*`, so it keeps the
hands that never saw a flop as well.

`line=F/T/R` reads the three streets at once, flop first. An empty segment, or one
left off the end, matches anything, not acting included: `line=xc//b*` is a flop
check-call, any turn, and a river they bet. `check_raise`, `check_raise_<street>`
and `check_raise_<street>_opp` are flags. A row from `hand_list` carries `line`.
Line patterns forgive case.

### The whole street

A line is one player's half of the street. The other half, what was done *to*
them, is the sequence: every voluntary action on the street, everyone's, in order,
written from one seat.

| Fact | Definition | Notes |
|---|---|---|
| **seq[street]** | Every voluntary action on that postflop street in order, the player's own as `X B C R F` and everyone else's as `x b c r f` | A key for every street the player was still in when it began -- see below. Dropping the lower-case letters and lower-casing the rest gives `line[street]`, or `""` where there is no line |
| **faced_check_raise[street]** | They made the street's bet, and a player who had checked earlier on the street raised | The other side of `check_raise`. A raise over someone else's raise counts, if the raiser had checked: `xBrr` is a check-reraise, and still at them |
| **faced_check_raise_action[street]** | Their next action after it: `fold`, `call` or `raise` | No key when they never acted again (all in on the bet). The check-raiser sat in front of them, so nothing of theirs comes between |

Heads-up villain checks, hero bets, villain check-raises, hero folds: hero's row is
`xBrF` and villain's `XbRf`, each the other with the case swapped. Three-handed it
reads the same way, `xxBcr` from the bettor's seat.

**Which streets get a key.** A street was played when its board was dealt, or when
anyone acted on it (a log cut mid-street), and two or more players were still in
when it began. Every player still in gets a key, whether or not they act on it:

- A player **all in** from an earlier street has a row of the others' lower-case
  letters alone, and no `line`. They were in the pot, and what the others did is
  what happened to it.
- A street **nobody acted on** -- the run-out after an all-in -- is `""` for
  everyone still in, not a missing key. They were in it and nothing happened.
- A player who **folded on an earlier street**, or a hand that ended before the
  street was dealt, has no key. That is what `-` matches.

Sequences are filters, and **case-sensitive**, unlike lines: a capital is this
player's action, lower case anyone else's, so `action_flop=xB*` (they bet after a
check) and `action_flop=Xb*` (they checked and someone bet) are two seats of one
street. `*` is any run, `?` exactly one action, `-` alone "not in the hand on that
street". `action_flop=*Br*` is "they bet and were raised", at any point and by
anyone; `action_flop=xBrF` is the heads-up check-raise they folded to, and
`action_flop=?B*` a bet with exactly one player checking in front. `action_flop=*` is "in the hand on the
flop", the run-out included. `action=F/T/R` reads the three streets at once with
the same segment rules as `line=`, and `!=` is the exact complement, as for lines.

`faced_check_raise_<street>` and `faced_check_raise` are flags, and
`faced_check_raise_flop=fold` (`call`, `raise`) is the answer. A row from
`hand_list` carries `seq`.

### Raise sizes

| Fact | Definition | Notes |
|---|---|---|
| **raise_pot[street]** | The player's first `raise` on the street: `(amount − to_call) / (pot_before + to_call)` | `amount` is the raise's own chips, `to_call` what calling would have cost them, `pot_before` the pot before the raise, forced posts included. A pot-sized raise is 1.0 |
| **raise_size[street]** | The size bucket of the same chips over the same pot | Keys match `raised_bet` exactly |

This is the pot-limit convention: the raise is measured after the call, against
the pot that call would have made. It is also what PokerNow's ½, ¾ and pot raise
buttons measure -- see judgement call 28 for how that was checked.

Filters: `raised_bet_flop=large` and `raised_bet_flop>=1`.

### Jams

A jam is an all-in `bet` or `raise`, the `(all in)` PokerNow writes on the line.
All three facts are kept per street, preflop included.

| Fact | Definition | Notes |
|---|---|---|
| **jam[street]** | Made a voluntary `bet` or `raise` flagged all in | An all-in *call* is not a jam; neither is an all-in forced post (rule 1) |
| **faced_jam[street]** | Acted while the street's latest bet or raise was someone else's jam | A raise over it with chips behind ends it; a second jam over the top becomes the one faced. Raising earlier in the street does not exempt a player |
| **called_jam[street]** | Faced a jam and called | Includes a call that puts the caller all in |

Filters: `jam`, `faced_jam` and `called_jam` for any street, and `jam_river`,
`called_jam_preflop` and so on for one.

**A jam range is short of bluffs.** A jam everyone folds to is never shown, and
that is exactly where a bluff succeeds. A `jam_river` range that is all value can
mean the player only jams value, or only that their bluffs got through. Read coverage
before reading balance. `called_jam` ranges suffer much less from this, since a
called jam almost always reaches showdown.

### Stack depth

| Fact | Definition | None when |
|---|---|---|
| **eff_bb** | `min(own, max(other dealt-in stacks)) / bb`, from the `Player stacks:` line: the most this player could win or lose against the deepest other stack at the table | Any of those stacks is unknown, or the big blind is |
| **spr_flop** | Among the players who saw the flop, each one's stack behind = starting stack − their preflop chips (forced posts included), floored at 0; then `min(own behind, max(the others' behind)) / pot_at["flop"]` | They did not see the flop, a flop player's stack is unknown, or the flop pot is 0 |

Both are rounded to two places. Filters: `eff_bb<40`, `eff_bb>=150`,
`spr_flop<=3`.

Judgement calls:

1. **An unknown stack is unknown, not zero** -- "Hand review" judgement call 26,
   applied here. One missing stack at the table leaves `eff_bb` None for
   everyone, since the deepest other stack cannot be named; such a hand matches
   no `eff_bb` comparison, so a depth split adds up to the hands with known
   stacks, not to all of them.
2. **Effective stack is against the deepest other stack, before any action.** It
   says how deep the hand could play from the deal, which is the question a depth
   split asks of every hand alike, a preflop fold included. It is not the stack
   against the players who stayed in; `spr_flop` is.
3. **SPR is per player, against the flop players.** `review._spr` measures the
   SPR between a flagged player and the villains of one flag; this one is each
   flop player's own, against the deepest other stack behind on the flop. A
   player all in preflop has 0 behind and an SPR of 0, and so does everyone who
   saw the flop against them alone.

### Board texture

`flop=<tag>`, `turn=<tag>`, `river=<tag>` and `board=<tag>` (the board as dealt,
three to five cards) filter on the community cards; `!=` negates. A street needs
that many cards dealt, so a hand that ended preflop matches no texture term,
positively or negatively.

| Group | Tags | Rule |
|---|---|---|
| highest card | `ace_high` `king_high` `queen_high` `jack_high` `ten_high` `low` | exactly one; `low` is 9-high or below |
| suits | `monotone` `twotone` `rainbow` `flush_possible` | all one suit / exactly two suits present / no suit repeated / three or more of one suit |
| straights | `straight_possible` | three ranks inside one five-rank window, the ace playing high and low, so two hole cards can make a straight |
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
  pair) and trips (set vs trips). A holding that is only high card becomes a
  `draw` if it had one on the flop or turn, with a `detail` of `combo_draw`
  (flush plus straight draw), `flush_draw`, `open_ender` (any two-rank straight
  draw, double gutshots included) or `gutshot`. Draws are read on the turn board,
  or the flop when the hand ended there, so a draw that missed on the river still
  counts. A draw must use a hole card; a four-flush or four-straight sitting on
  the board alone does not. Any pair or better keeps its class even with a draw.
  `draw` sorts between `pair` and `high_card`.

  The view's rows are flat: one per `cards.MADE_LEAVES` leaf, strongest first,
  each with the class it belongs to as its `group`. A pair is `top_pair` or
  `board_pair`, never also a `pair` row above them counting the same hands, so
  the rows partition the known hands. Each row is exactly the hands
  `made=<leaf>` finds in the same spot (see "Made hand by street"), which is the
  filter a click on it lists; a hand that ended before the flop is a `no_board`
  row, last, with no made hand for a filter to name.

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

### Holdings

`cards.HAND_RANKING` orders the 169 classes strongest first by all-in equity
against one random hand, the ordering every starting-hand chart reproduces.
`hand_pct` is a class's place in it as a percentile: 0.6 is AA, 100 is 32o, so
"a bottom-40% hand" is `hand_pct>=60`. It is a reference for the tags, not a
claim about any spot: a 3-bet call that is bad six-handed can be fine heads-up,
and the tag that reads it says which count it rests on.

Filters: `hand=72` (both suits), `hand=72o`, `hand=77`, `hand!=AA`, and
`hand_pct>=60`. A holding term matches only where the cards are known.

### Made hand by street

`made_flop=set` reads what the holding had made **on the board as it stood on
that street** (`cards.made_at`), so a flop call that rivered a flush is a draw on
the flop and a flush on the river. `made=flush` reads the final board, exactly as
the `made` range view does.

The names are `cards.MADE_LEAVES`, one flat list strongest first: each class,
with `pair`, `trips` and `draw` opened up into their details.

    straight_flush quads full_house flush straight set trips two_pair
    overpair top_pair pocket_pair middle_pair bottom_pair board_pair
    combo_draw flush_draw open_ender gutshot high_card

`pair` and `draw` also name their whole group (`cards.MADE_GROUPS`). **A leaf wins
over a class of the same name**, so `made_flop=trips` is one hole card to a paired
board and never a set; a set is `made_flop=set`.

| Operator | Matches |
|---|---|
| `=` / `!=` | that leaf, or any leaf of that group / anything else |
| `>=` / `>` | at least as strong as the name's weakest leaf / stronger than its strongest |
| `<=` / `<` | no stronger than its strongest / weaker than its weakest |

So `made_turn>=two_pair` is two pair or better, `made_flop>=pair` any pair or
better, and `made_river<pair` the draws and air.

**`=` and `!=` take several names joined by `|`**, matching any of them:
`made_flop=set|full_house|flush` is the hands that flopped one of the three, which
no single span says, since `two_pair` sits between them and the pairs. A
comparison takes one name: "at least a set or a flush" is not a question.

A `made_<street>` term matches only when the cards are known, the board reached
that street, and the player had not folded on an earlier one. Every operator,
`!=` included, makes a claim about a holding, so an unknown one matches none of
them. `made=` keeps every known holding with a board, folded or not, because that
is what the range view counts and a row there must reproduce as a filter.

On the river a draw that missed is still its draw, as on the final board. That is
`made_hand`'s reading, and `made_river=flush_draw` is the busted flush draws.

`flop=trips` is not a holding. It is the board texture, three of a rank on the
flop, and stays one.

### Strength by line

The action-line explorer (`lines.line_breakdown`, `GET /players/{alias}/lines`,
`GET /lines` for the pool, `pnt lines`, the chart's **Lines** view) groups a spot's
hands by how they were played after the flop, and splits each group by what the
hands in it were when shown.

**The group key** is one segment per street, flop/turn/river joined with `/`, or
one street alone when the caller asks for one:

| Mode | Segment | Filter that finds the group |
|---|---|---|
| `own` | `line[street]`, or `-` with no line | `line=xc/x/-`, or `line_flop=xc` for one street |
| `action` | `seq[street]`, `-` with no key, `.` for `""` | `action=XbC/Xx/-`, or `action_flop=XbC` |

`.` is a street they were in on which nobody acted, the run-out after an all-in.
An empty segment in `action=` means "anything", so `.` is written as two terms
instead: `action_turn=*` (in the hand on the turn) and `action_turn!=?*` (with not
one action on it). Every hand lands in exactly one group -- the hands with no
action after the flop are `-/-/-` -- so the groups add up to the spot.

**Strength** is read on the board of the **last street of the key they acted
on**: a segment that is not `-` or `.`, and in `action` mode has a capital in it.
That street is fixed by the key, so a whole group is read on one street, and the
tiers are:

| Tier | Holding on that street | `made_<street>` terms |
|---|---|---|
| **nutted** | `NUTTED_FROM` (a set) or better by `cards.made_at`: set, straight, flush, full house, quads, straight flush | `>=set` |
| **strong** | the rest of `tags.strength_at`'s strong: trips, two pair, an overpair, top pair | `<set,>=top_pair` |
| **marginal** | its medium and weak: an underpair, middle, bottom or board pair | `<top_pair,>=board_pair` |
| **draw** | a flush or straight draw and no pair, flop and turn only | `=draw` |
| **air** | high card; on the river a draw that missed | `=high_card`, on the river `<pair` |

Each tier is an unbroken run of `MADE_LEAVES`, so it is a filter too: a tier's
filter is the group's plus its terms on that street.

| Field | Meaning |
|---|---|
| `n` | Hands in the group |
| `shown` | Of them, cards known |
| `known` | Of those, with a street to read them on; a group with no action after the flop has none |
| `coverage` | `known / n` |
| `tiers` | `{tier: {n, pct, filter}}`, `pct` of `known`; the counts add up to `known` |
| `draw_heavy` | The draw share, said outright. Always 0 on a group read on the river |
| `net_bb`, `bb_per_100` | By the bb/100 rule: complete hands with a known big blind (see "Money") |
| `filter`, `hand_ids` | The terms that find exactly the group inside the same spot, and its hands. The pool form leaves the ids out |

Groups under `min` hands (3 unless asked) are left out of the rows and counted in
`hidden`, groups and hands. Rows are sorted by `n`.

**The tree** (`lines.line_tree`, `tree` in the answer when all three streets are
read) holds the same hands as nested rows, so that every way of checking the flop
sits together: a node per flop line, opening into the turn lines under it, and
those into the river. A node is every hand whose line *starts* that way, with its
own filter (`line_flop=x`, `line=x/b`) and its own strength split, read on the last
street of the node they acted on -- "checked the flop" is what they had on the
flop. Its `children` are the ways on with `min` hands or more and `hidden` the
rest, and the two add up to the node. A line that stopped there (they folded, or
were all in) has no children. The leaves are the groups above, unchanged.
`test_the_tree_adds_up_and_reproduces` holds every node to its filter.

**The usual bias, sharpened.** A line that wins uncontested is never shown, and a
line is often *why* a hand won uncontested: `b/b/-` where the turn bet got a fold
is in the group, but its cards are not in the split. Bluffs that got a fold are
never shown, so a betting line reads stronger than it is. The hero's cards are
known on every hand, which is why the hero's coverage is far higher than anyone
else's.

35. **Nutted starts at a set.** `NUTTED_FROM = "set"`, in `lines.py`. A set is
    hidden and near the top of most boards; trips made with one hole card on a
    paired board are not, since anyone holding the case card or a full house has
    it beat, so trips stay strong. Two pair is strong for the same reason a flush
    is not: one is beaten by every better two pair, the other by very little.
    `NUTTED_FROM` is the one place to move it.
36. **Strength is read where they last acted, not on the final board.** A flop
    check-raise that folded the turn is judged on the turn board, where the fold
    happened; a line that ended on the flop says nothing about a river nobody saw
    them play. The cost is that a group read on the river can never be
    draw-heavy: a draw there has missed, and is air.
### Bluff:value

`bluffs.bluff_value` splits a player's aggression into categories and reads what
the shown hands in each had, **on the board of the street the action was made
on** (`tags.strength_at`). Each category is a filter, so it reproduces as a spot:

| Category | Filter | Per street |
|---|---|---|
| Size | none | every bet or raise of one size: the union of the three below it |
| · bet | `bet_<street>=<size>` | their first bet on the street, by its bucket |
| · raise | `raised_bet_<street>=<size>,!check_raise_<street>` | their first raise, sized by `raise_pot`, not a check-raise |
| · check-raise | `check_raise_<street>,raised_bet_<street>=<size>` | the check-raise, which is their first raise |
| Every check-raise | `check_raise_<street>` | the sized check-raises added together |
| Every raise | `raised_bet_<street>` | every raise facing a bet, check-raises included |
| Every jam | `jam_<street>` | an all-in bet or raise |
| Every river bet | `bet_river` | the five river bet rows added together |
| All aggression | none | every hand they bet or raised in, read on the **last** street they did |

**Size above action.** A street's rows open with one per size, and each opens into
the bet, the raise and the check-raise made at that size (`parent` names the size
row): the size is what the table sees first, and the action is what it means. A
size row has no filter, since a filter cannot say "a bet or a raise", and is
listed by its hands; it is the union of its children rather than their sum,
because a bet and a re-raise of one size on one street is one hand.
`test_sizes_sit_above_the_actions` holds the sized rows to the summaries.

| Class | `strength_at` | As a filter (flop shown) |
|---|---|---|
| `value` | strong | `made_flop>=top_pair` |
| `marginal` | medium or weak | `made_flop=pocket_pair\|middle_pair\|bottom_pair\|board_pair` |
| `semi_bluff` | draw, flop or turn | `made_flop=draw` |
| `bluff` | air; on the river a missed draw too | `made_flop=high_card`; `made_river<pair` on the river |

Every row reports `n` (every hand in it), `known` (cards shown), coverage, the four
counts, and **value : bluff** as `value / (bluff + semi_bluff)`, printed "2.1 : 1".
Marginal hands are on neither side: they bet for thin value or protection, and the
ratio is the one a caller needs. Value with no bluff beside it prints "∞ : 1";
nothing shown prints "--".

**A bet everyone folds to is never shown**, and that is the bluff that worked, so
the shown ratio leans to value -- more so the more of a row got folds. Bet rows
carry `folded`, the bets whose `bet_answer` was `fold`, as that context. Raise,
check-raise and jam rows carry none: what a raise got is not a fact. The player
whose logs these are knows their own cards on every hand, so their own rows are
close to complete; everyone else's are their showdowns.

**The pool** is the same table over every player's rows. Each row of `Facts` is
one hand and one player, so a pool category counts a hand once for each player
who took the line in it, and a pool row is the sum of the players' rows.

All aggression has no filter because a filter is an AND of terms and cannot say
"bet or raised on some street". Its hands are listed by category key instead
(`/bluffs/hands?category=aggression`).

### Strength by street (slowplays)

`made_flop=set|full_house|flush,line_flop=x*` asks one slowplay question.
`slowplay.slowplay(facts, street, floor)` asks all of them at once: what the player
did on one street with each made hand from `straight_flush` down to `floor`
(`set` by default; any leaf, `two_pair` the usual wider one).

A **row** is one leaf: the hands where the cards are known, `made_<street>` is
that leaf, and they acted on the street, so its filter is
`made_<street>=<leaf>,line_<street>=*`. A `total` row covers every leaf at once,
`made_<street>>=<floor>`. A player who made the hand but was already all in from
an earlier street had nothing to decide; they are counted as the row's `all_in`
(`line_<street>=-`) and in no column.

A **column** is what they did, read from `line[street]`. The columns partition
every line, so the cells of a row add up to its `n`, and a cell's filter is the
row's made-hand term plus the column's terms:

| Column | Line | Filter terms | Slowplay |
|---|---|---|---|
| **bet** | starts `b` | `line_<s>=b*` | |
| **raised** | starts `r`: raised the first bet they faced | `line_<s>=r*` | |
| **check-raised** | starts `xr` | `line_<s>=xr*` | |
| **check-called** | starts `xc` | `line_<s>=xc*` | yes |
| **called** | starts `c`: called the first bet they faced | `line_<s>=c*` | yes |
| **checked, no bet** | exactly `x`, and their check did not close the street | `line_<s>=x,!check_back_<s>` | yes |
| **checked back** | exactly `x`, and their check closed it | `line_<s>=x,check_back_<s>` | yes |
| **check-folded** | exactly `xf` | `line_<s>=xf` | |
| **folded** | exactly `f` | `line_<s>=f` | |
| **other** | any other line | `line_<s>=*` and `!=` each pattern above | |

A line is the player's own letters only, and its first letter is their first
decision. Only a check is followed by a decision of a different kind: action comes
back to a player who checked only when someone bet, so the second letter is `c`,
`r` or `f`. **other** is therefore `xb…` or `xx…`, which a complete log never
writes; it exists so the columns add up even if one ever does. A bet keeps the
whole line that follows it -- `bc` (bet, called a raise) and `bf` are bets -- as
does a call: `cr` is a call.

**`slowplay_pct`** is check-called + called + checked + checked back, over `n`.
A check-raise is a trap too, but not a slow one: it puts money in on the street,
so it has its own column and is left out of the share.

**Checked, no bet** and **checked back** both read `x`, the street checked through.
`check_back` tells them apart (see "Facing a bet, the aggressor, and checking
back"): the check that closed the street is the in-position check-back; every other
`x` is a check from first or a middle seat that nobody behind bet into. A lone check
against a player already all in decided nothing and is never a check-back, so it
lands in **checked, no bet**.

**Coverage** is `known / hands`: `hands` is every hand they were in on the street
(`action_<street>=*`), `known` those whose holding can be read there
(`made_<street>>=high_card`). **Shown hands only, and the bias runs one way**: a
strong hand that bet and got folds to is never shown, while one that checked or
called usually reaches showdown. An opponent's slowplay share reads high for that
reason. The hero's cards are known on every hand, so the hero's does not.

`GET /players/{alias}/slowplay?street=flop&floor=set&filter=` returns the table,
every cell with `n`, `pct` of its row, `filter` and `hand_ids`.
`GET /slowplay` (same parameters, plus `min`) pools everyone's hands and adds
`players`, each player's total row, highest slowplay share first. `pnt slowplay
[alias] --street flop --floor set` prints both; the chart's **Strength by street**
view draws the player's.

The **SLOWPLAYS** tag (see "Exploit tags") is this table's flop `total` row at the
default floor: `n` is the row's `n`, its hits the slowplays, and its filter the
row's. Calibrated on the 13,300-hand database on 2026-10-11, where the pool sits at
41% and the hero, every hand known, at 34%: 55% over 15 hands fires for one player
of the seven with a sample and finds one more who is streaky.

### Voluntary shows

`shows.voluntary_shows(conn, alias, game)` reads one player's rows of
`v_voluntary_shows`: every hand they turned cards over in after it ended, with
nobody asking. **The bias is the reverse of every other shown-hand figure, and it is
the point.** Elsewhere the bluffs that got folds are never shown; here they appear
only when the player chose to show them. A player who shows air after a fold or an
uncontested pot is advertising bluffs; one who shows only strong hands is selling a
tight image. The figure is what they want you to see, never a sample of a range, and
the endpoint, the CLI and the page all say so (`shows.NOTE`).

| Field | Counts |
|---|---|
| `hands` | every hand they were dealt into (`hand_players`), the base the shows come from |
| `shows` | every row of `v_voluntary_shows` for their identities |
| `two_card` | shows of both cards: classified |
| `one_card` | shows of one card: counted, never classified |
| `coverage` | `two_card / shows` |
| `classes` | the two-card shows by class, summing to `two_card` |
| `contexts` | the same counts split by how the hand ended for them, summing to the totals |

| Context | Rule |
|---|---|
| **folded, then showed** | `folded = 1`, whether or not others reached showdown |
| **won uncontested, then showed** | `folded = 0` and `went_to_showdown = 0` |
| **reached showdown, showed after** | `folded = 0` and `went_to_showdown = 1`: a muck turned over later |

A two-card show is read **on the board as it stood** (`tags.strength_at`, with
`cards.made_at` for the made hand printed beside it): a fold on the street of the
player's fold action, anything else on the board the hand ended on, and never past
the board's first run.

| Class | Postflop (`strength_at`) | Preflop (`strength_at`, by `hand_pct`) |
|---|---|---|
| `strong` | strong: top pair or an overpair, or better | strong (at or under 15) |
| `marginal` | medium or weak: middle, bottom, board or a low pocket pair | medium (at or under 50) |
| `draw` | draw, on the flop or turn | -- |
| `air` | air: high card; on the river a missed draw (`missed`) too | weak (past 50) |

No exclusions: a show from a dead-button or irregular-blinds hand is still a show,
since nothing here is positional. Shows are not `Facts`, so no spot filter
reproduces a row; each row carries its `hand_id` instead, and
`test_fixture_counts_match_the_view` holds every count to the view.
`GET /players/{alias}/shows?game=` returns it, `pnt shows <alias>` prints it, and
the players page lists it as **Shows without being asked**.

Judgement calls:

1. **A fold is read where it was folded.** The tell is what they gave up and then
   chose to show; the river that came after they left says nothing about that.
2. **Preflop weak is air.** A bottom-half hand shown after a preflop fold or a
   walk is the "look what I had" show, and nobody plays it for value.
3. **The showdown muck is its own context.** The idea asked for two (folded and
   uncontested), but a loser who mucks and then shows is a third choice, and
   folding it into either would mislabel it.

---

## Decision points (nodes)

A *node* is where a player stands in a hand, written as a filter, so the hand in
progress can be looked up in the player's history the way any spot is.
`nodes.py` mirrors this section; the live HUD reads it through `/live`.

Each player's path through a hand is one node per voluntary action, plus a
pending one for the player to act. A node's filter is built from six parts, in
this order:

    position, *preflop history, pot type, players, *postflop history, situation

| Part | Content |
|---|---|
| **position** | `position=<POS>`; withheld on dead-button and irregular-blind hands, as every positional view does |
| **preflop history** | The terms this player's earlier preflop decisions earned (table below) |
| **pot type** | `limped` / `srp` / `3bet_pot` / `4bet_pot` from `pot_level`, from the first postflop node on |
| **players** | `players_flop=2` or `players_flop>=3` from `players_flop` (everyone who did not fold preflop), on postflop nodes; the groups of "Multiway vs heads-up" |
| **postflop history** | The terms their earlier postflop decisions earned |
| **situation** | The term for this decision point, or none when nothing tracks it |

**Preflop** (L is the bet level when they act):

| L | Situation | Decision → history term |
|---|---|---|
| 1 | `unopened` | raise → `opener`; call → `limp`; check → nothing; fold ends the path |
| 2 | `faced_open` | raise → `3bet`; call → `called_open` |
| 3 | opener: `faced_3bet`, else `faced_3bet_any` | raise → `4bet`; call → `faced_3bet=call` / `faced_3bet_any=call` |
| 4 | `faced_4bet` | raise → `5bet`; call → `faced_4bet=call` |
| 5 | `faced_5bet` | raise → `faced_5bet=raise`; call → `faced_5bet=call` |
| 6+ | none | nothing |

**Postflop** (S is the street; the conditions are the Postflop table's):

| Condition | Situation | Decision → history term |
|---|---|---|
| Previous street's aggressor, first in | `cbet_S_opp` | bet → `cbet_S=<size>` (`cbet_S` when the pot is empty); check → nothing |
| Facing a c-bet before anyone raises it | `faced_cbet_S=<size>` (`faced_cbet_S` when unsized) | call → `called_cbet_S`; raise → `raised_cbet_S`; fold ends the path |
| Could lead into the previous street's aggressor | `donk_S_opp` | bet → `donk_S`; check → nothing |
| Anything else | none | nothing |

So a button who opens, calls a 3-bet and then faces a small c-bet walks
`position=BTN,unopened` → `position=BTN,opener,faced_3bet` →
`position=BTN,opener,faced_3bet=call,3bet_pot,players_flop=2,faced_cbet_flop=small`.

Every node with a filter and a decision satisfies its own filter on the finished
hand's `Facts`, and so does every history term it adds. That is the test that
keeps this table and `derive.py` in agreement.

### Finding the closest spot

A node with too few hands behind it (`min_hands`, 1 by default) is widened one
step at a time, most specific detail first, until one has enough. The answer
carries `exact: false` and a note per step. When even the widest form is empty
the answer is *no data*, and a chart following the action shows that player on
all their hands.

Preflop node: (1) as is; (2) without `position=`; (3) the **parent node** — drop
the situation and turn the last history term back into the situation it was
decided at (`opener`/`limp` → `unopened`, `3bet`/`called_open` → `faced_open`,
`4bet` → `faced_3bet` or `faced_3bet_any`, `5bet` → `faced_4bet`, `X=d` → `X`),
with position; (4) the same without; repeat 3–4 up the path to the bare
situation. A player facing a 5-bet with no history of it is shown their 4-bet
spot: the range they arrived with.

Postflop node: (1) as is; (2) any size (`faced_cbet_S=small` → `faced_cbet_S`);
(3) any number of players (without `players_flop`); (4) without postflop history;
(5) without `position=`; (6) without preflop history; (7) the pot one level down
(`4bet_pot` → `3bet_pot` → `srp`, `limped` → `srp`); (8) without a pot type.

A node nothing tracks (facing a bet in a limped pot, say) is answered from the
nearest earlier node on the path that has a filter, marked as such.

The decision mix reported beside a resolved spot is counted at the *resolved*
situation, not the asked one: a player shown their 4-bet spot is shown what they
did there.

**Boards like this one.** Before each rung is taken as it stands, it is tried
narrowed to the live board's texture: the highest-card tag and `paired` /
`unpaired`, both first and then the highest card alone, as `<street>=<tag>` terms
on the live node's street. A narrowed rung is kept only when it still has
`min_hands` hands **and** at least `min_known` shown hands (5 by default) behind
the split the HUD will print -- the decision's own once it is made, every
decision's while it is pending; the texture is there to sharpen what they showed up with, and a split over two
hands sharpens nothing. The narrowing is part of the answer, not of the node: the
reported `filter` carries it (so a chart following the spot narrows too) and
`texture` lists the terms used, while `exact` still speaks of the node alone.
Preflop nodes are never narrowed.

### Spots after a missed c-bet

The bets the "Delayed c-bet, probe and float" section defines are nodes too, so
the HUD answers them from the hands behind them rather than from an earlier node.
Their conditions are that section's, read before the action as the table above
reads its own; none can share a node with a c-bet, lead or facing-a-c-bet spot.

| Condition | Kind | Situation | Decision → history term |
|---|---|---|---|
| The PFA, every earlier postflop street checked through, no bet yet (turn, river) | `delayed_cbet` | `delayed_cbet_S_opp` | bet → `delayed_cbet_S`; check → nothing |
| Not the PFA, the PFA's check closed the street before, no bet yet, PFA yet to act (turn, river) | `probe` | `probe_S_opp` | bet → `probe_S`; check → nothing |
| The PFA facing that probe before anyone raises it (turn, river) | `faced_probe` | `faced_probe_S` | call → `faced_probe_S,line_S=c*`; raise → `faced_probe_S,line_S=r*`; fold ends the path |
| Called on the street before (flop: put chips in preflop), its aggressor has acted, no bet yet | `float` | `float_S_opp` | bet → `float_S`; check → nothing |

The decision mix is bet / check for the first, second and fourth, and fold / call /
raise facing a probe, where call and raise are `faced_bet_action` (the PFA had not
acted on the street before the probe, so facing it is their first decision facing a
bet there).

These nodes widen along the postflop ladder of "Finding the closest spot" like any
other.

Judgement calls:

1. **The answer to a probe is read from the line.** No fact records a call or a
   raise of a probe the way `called_cbet_S` / `raised_cbet_S` do, so the history
   carries `line_S=c*` / `line_S=r*` beside `faced_probe_S`: the PFA's line on the
   street starts with their answer, and the pair reproduces exactly.
2. **No history at all still has an answer.** Before these spots were tracked, the
   HUD answered them from the nearest earlier tracked node on the path. When even
   the widest form of one of these four is empty, it still does -- marked `arrived`,
   with the note "no *spot* hands; showing *earlier spot*" -- rather than turn a
   player with a thin history into *no data*. The c-bet, lead and facing-a-c-bet
   spots keep their *no data*.

### What they showed up with

Beside the decision mix, every resolved spot reports `showings`: per decision, the
hands that made it, how many had cards known, and what those were worth **on the
board as it stood on the live node's street**. A flop call that rivered a flush was
a draw when the call was made, and that is what it is counted as.

| Bucket | Postflop, on that street's board | Preflop, by `hand_pct` |
|---|---|---|
| `strong` | two pair or better, or top pair / an overpair | ≤ 15 |
| `medium` | middle pair, or a pocket pair under the top card | ≤ 50 |
| `weak` | bottom pair, or a pair entirely on the board | the rest |
| `draw` | no pair, but a flush or straight draw -- flop and turn only | — |
| `air` | high card; on the river a draw has missed | — |

The postflop rule is the exploit tags' `strength`, and the preflop cutoffs are
judgement values (`tags.PREFLOP_STRENGTH`). The HUD shows `strong` as *value*,
`medium` and `weak` together as *marginal*, and the two others by name.

A hand is *known* for a street only when its cards are known **and** its board
reached that street: a hand that ended on the flop says nothing about the turn,
and drops out of a turn spot's count. `known / hands` is printed with every split,
and the "Ranges" coverage caveat applies twice over -- a called flop bet that
reached showdown is the one that kept going.

Which decision's split the HUD puts on the line is the answering spot's
`decision`: the live node's own once made, or one row per decision while they are
to act. When the answer comes from earlier on the path -- an untracked node
answered by its nearest tracked ancestor, or a preflop spot widened up to a parent
node -- the answer is flagged `arrived`, the decision is the one they made *there*
this hand, and the line reads "arrived with": those hands are the range they
brought to this street, read on this street's board. That replaces the ancestor's
fold/call/raise mix, which under the live node's label read as a claim about a
spot it was never counted at.

---

## Tags

A tag is a claim about a player you can act on at the table, backed by a count
and a spot filter that puts the hands behind it on the chart. `tags.py` mirrors
this section; the HUD shows them as chips, `GET /players/{alias}/tags` and
`pnt tags` print them. Every threshold lives in `tags.THRESHOLDS`, and this table
is the other copy.

Three rules hold throughout:

- **Too small a sample is unknown, not false.** A rule under its minimum is
  silent, on the principle that prints `--` for a rate with no opportunities.
- **Shown-hand evidence is biased toward showdowns.** A river bet that reached
  showdown got called; the bluffs that worked are the hands missing. Every tip
  built from shown hands says "shown". The hero's cards are known on *every*
  hand, so the hero's shown evidence is complete where an opponent's is not --
  which is why the light-hand rules need a share as well as a count.
- **Lifetime only.** Tags are judged over every hand on record; a spot filter
  narrows the hands they are judged on, never the rules.
- **A habit outlasts any one night.** With 3 or more sessions (`MIN_SESSIONS`),
  every exploit tag that fires is judged again on the hands of all sessions but
  one, for each session in turn. One that stops firing without some session is
  **streaky**: it is dropped from `tags`, does not name the archetype, and is
  returned under `streaky` with `carried_by` (that session's `game_id` and
  hands; the biggest when several each carry it). Pooling, not a per-session
  average, is still what a tag is judged on: an average counts a 12-hand game
  as much as a 400-hand one, and most games are too small to judge a rule alone.
  The sums behind the profile add (`counts`), so this costs one pass over the
  hands.

### Baselines

Looseness is measured hand by hand against what is ordinary at that hand's table
size, so a player seen heads-up and six-handed is judged fairly on both. These
are judgement values.

| Dealt in | VPIP | PFR |
|---|---|---|
| 2 | 68 | 50 |
| 3 | 52 | 36 |
| 4 | 44 | 30 |
| 5 | 38 | 26 |
| 6 | 30 | 21 |
| 7+ | 24 | 17 |

```
vpip_excess = mean over VPIP opportunities of (100·vpip − baseline VPIP for that hand's table)
pfr_excess  = the same for PFR
pfr_ratio   = PFR count / VPIP count
```

### Archetype

One per player, first rule that fits, and none under 100 VPIP opportunities.
Exploit tags are judged first because BALANCED depends on them.

| Archetype | Rule |
|---|---|
| **MANIAC** | pfr_excess ≥ +15 and (flop AF ≥ 60, or preflop jams ≥ 3% of opportunities, or 3-bet ≥ 18%) |
| **STATION** | fold to flop c-bet ≤ 30% (n ≥ 25) and WTSD ≥ 38% (n ≥ 40) and pfr_ratio < 0.5 |
| **FISH** | vpip_excess ≥ +12 and pfr_ratio < 0.45 |
| **LAG** | vpip_excess ≥ +8 and pfr_ratio ≥ 0.6 |
| **NIT** | vpip_excess ≤ −12 |
| **BALANCED** | hands ≥ 500 and no exploit tag fired |
| *style* | otherwise, the style below with the most exploit tags; a tie goes to the one whose tags rest on the larger total `n` |
| **TAG** / **PASSIVE** | otherwise, with no exploit tag: pfr_ratio ≥ 0.5, else PASSIVE |

| Style | Exploit tags in the family |
|---|---|
| **STICKY** | CALLS DOWN LIGHT, STICKY VS CBET, INELASTIC VS 3BET |
| **FIT OR FOLD** | FOLDS RIVER, FOLDS TO CBET, FOLDS TO 3BET, NO BLUFF, CHECKS BACK WEAK |
| **TRAPPER** | TRAPS, SLOWPLAYS, RARELY CBETS |
| **GAMBLER** | JAM HAPPY, JAMS LIGHT, 4BETS LIGHT, BLUFFS RIVER, DONKS, AUTO CBET |

Only tags that are not streaky count toward a style.

### Exploit tags

"Shown" populations count hands with cards known and a board. **strong** is two
pair or better, or a pair that is top pair or an overpair; **weak** is bottom
pair or a pair entirely on the board; **air** is high card or a busted draw.
**Light** means `hand_pct` at or past the cutoff and not a troll hand
(`TROLL_HANDS` = 72, 92 and K2o, which these games play on purpose).

| Tag | Numerator | Denominator | Fires | Min n | Filter |
|---|---|---|---|---|---|
| **NO BLUFF** | air | `aggressor[river]` and WTSD, shown | ≤ 7% | 10 | `aggressor_river,wtsd` (made) |
| **BLUFFS RIVER** | air | same | ≥ 20% | 8 | same |
| **CALLS DOWN LIGHT** | air or weak | `called_bet[river]` and WTSD, shown | ≥ 20% | 8 | `called_bet_river,wtsd` (made) |
| **FOLDS RIVER** | `folded_to_bet[river]` | `faced_bet[river]` | ≥ 50% | 15 | `faced_bet_river` |
| **FOLDS TO 3BET** | Fold to 3-Bet | its opportunity | ≥ 25% | 15 | `faced_3bet` |
| **INELASTIC VS 3BET** | Fold to 3-Bet ≤ 15% (n ≥ 15), *or* light (cutoff 60) calls of a 3-bet or 4-bet ≥ 3 and ≥ 15% of shown ones; never when FOLDS TO 3BET fires, since the fold rate is the direct read | | | | `faced_3bet_any=call` |
| **4BETS LIGHT** | light (cutoff 50) 4-bets or 5-bets | shown 4-bets and 5-bets | ≥ 3 and ≥ 15% | | `4bet` |
| **JAMS LIGHT** | light (cutoff 50) preflop jams | shown preflop jams | ≥ 3 and ≥ 15% | | `jam_preflop` |
| **LIMPER** | `limp` | `unopened` | ≥ 45% | 30 | `limp` |
| **NEVER SQUEEZES** | Squeeze | its opportunity (`squeeze_opp`) | ≤ 3% | 25 | `squeeze_opp` |
| **FOLDS TO STEAL** | Fold to Steal | its opportunity (a blind facing a steal nobody called; no dead button or dead blind) | ≥ 35% | 30 | `fold_to_steal_opp` |
| **STEALS WIDE** | ATS (`steal`) | `steal_opp` (first in from CO, BTN or SB; no dead button or dead blind) | ≥ 55% | 30 | `steal_opp` |
| **JAM HAPPY** | preflop jams ≥ 5 and ≥ 3% of VPIP opportunities, else postflop jams ≥ 8 and ≥ 8% of flops seen | | | | `jam` |
| **FOLDS TO CBET** | Fold to C-Bet (flop) | its opportunity | ≥ 50% | 25 | `faced_cbet_flop` |
| **STICKY VS CBET** | same | same | ≤ 30% | 25 | same |
| **AUTO CBET** | C-Bet (flop) | its opportunity | ≥ 60% | 25 | `cbet_flop_opp` |
| **RARELY CBETS** | same | same | ≤ 35% | 25 | same |
| **CHECKS BACK WEAK** | strong | `check_back` any street and WTSD, shown | ≤ 35% | 10 | `check_back,wtsd` (made) |
| **TRAPS** | strong | same | ≥ 60% | 8 | same |
| **SLOWPLAYS** | checked or called (the slowplay columns of "Strength by street") | a set or better on the flop, shown, that they acted on | ≥ 55% | 15 | `made_flop>=set,line_flop=*` (strength, flop) |
| **DONKS** | Donk Bet (flop) | its opportunity | ≥ 35% | 15 | `donk_flop` |
| **BIG = STRONG** / **BIG = BLUFF** | strong share of shown first bets, `large`+`overbet` against `blocker`+`small`+`medium`, any street | | gap ≥ 40 points | 6 each side | `wtsd` (sizing, `bet`, the street with most bets) |

Each tag reports `n` (the denominator it was judged on), `hits`, `pct`, and the
filter and chart view that reproduce it. The rate tags' `n` is exactly the hands
their filter finds; the shown ones' `n` is those hands with cards known;
`test_every_tag_reproduces_on_the_chart` holds them to it.

### Fun tags

For each of 7-2, 9-2 and K-2o: the shown hands in that class the player raised
preflop with. **7-2 ALL-IN-PRE ×n** when at least one was a preflop jam
(`hand=72,jam_preflop`), else **7-2 RAISES ×n** from two raises (`hand=72,pfr`).

From voluntary shows (`shows.show_tags`, see "Voluntary shows"), over the two-card
shows:

| Tag | Numerator | Denominator | Fires | Filter |
|---|---|---|---|---|
| **SHOWS BLUFFS ×n** | air or draw | two-card shows | ≥ 3 and > 50% | none: `hand_ids` (by `shows`) |
| **SHOWS NUTS ×n** | strong | two-card shows | ≥ 3 and > 50% | none: `hand_ids` (by `shows`) |

`THRESHOLDS["shows"]` holds both cutoffs. More than half, so the two never fire
together. They come after the other fun tags on the HUD and on
`GET /players/{alias}/tags` with no filter, since a spot cannot narrow a show.
Calibrated on the 14,720-hand database on 2026-10-12: SHOWS BLUFFS fires for two of
the nine players who show at all (53% and 56% air or draw), SHOWS NUTS for none.

### Judgement calls

16. **Thresholds are calibrated, not derived.** Set against the 8,036-hand
    database on 2026-09-14 so that each rule separates a few players from the
    rest; the first draft fired on the whole table for some rules and could
    never fire for others. A different pool wants different numbers, and
    `THRESHOLDS` is the one place to change them.
17. **Troll hands are never evidence.** A 72o that called a 3-bet was a bet on
    the side game, not a misread of the spot, so it is excluded from the light-hand
    counts and celebrated in the fun tags instead.
18. **Baselines are judgement values**, chosen from what ordinary play looks like
    at each table size rather than measured from this pool -- the pool is nine
    friends, and measuring it would make the loosest table read as normal.

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

`net_bb` in a stat row is the numerator of that same fraction: Σ net / big blind
over the same complete hands. It is the figure a "bb won" column prints, and it
moves only where bb/100 does.

On the 6,255-hand database this was developed against, excluding the two truncated
hands moves the whole-table ledger from −3.29 bb to −0.43 bb of 10,731 bb gross,
and every one of the 6,253 complete hands conserves chips exactly.

### Confidence intervals

```
rate      Wilson score interval, z = 1.96 (95%):
            p      = num / den
            centre = (p + z²/2n) / (1 + z²/n)
            half   = z · √(p(1 − p)/n + z²/4n²) / (1 + z²/n)
            (lo, hi) = (centre − half, centre + half), clipped to [0, 1]
bb/100    ± 1.96 · sd / √n · 100
            sd = √((Σx² − (Σx)²/n) / (n − 1)),  x = each hand's net / its big blind
```

`queries.wilson(num, den)` returns `(lo, hi)` as fractions, or None when `den`
is 0. `rates()["bb_per_100_ci"]` is the bb/100 half-width, over the same complete
hands with a known big blind as bb/100 itself (`n` = `Tally.bb_n`), and None under
two of them. `Tally.bb_sq` keeps Σx² as an exact fraction, so it adds across
games like `bb_sum`.

### Intervals on the pages

Display only: no tag, threshold or minimum sample reads an interval.

| Where | What it shows |
|---|---|
| `/stats` page, a rate's hover | its `_opp` sample, then `± h pts (95%: lo–hi%)`, `h = (hi − lo)/2` |
| `/stats` page, `±` column | `bb_per_100_ci`; bb/100's hover is `bb/100 ± ci` |
| Chart tiles | slowplayed share over `total.n`; Results' bb/100 over `bb_hands` (`/results` rows carry `bb_per_100_ci`) |
| HUD figure hover | the session's and the lifetime's range, each over its own `_opp` |

The pages and `spot.js` each carry a copy of `wilson(p, n)` taking the rate as a
fraction; a test holds every copy to `queries.wilson`.

Judgement calls:
1. The pages rebuild `num` from the rounded percentage (`p = pct / 100`). The
   rounding moves `p` by at most 0.05 points, far inside any interval.
2. Coverage tiles get no interval. Coverage counts these exact hands; it is not an
   estimate of anything larger.
3. The HUD's blue drift mark also needs the lifetime figure outside the session's
   interval (`DRIFT_NEEDS_CI` in `sidepanel.js`), on top of the 10-point gap and 10
   chances. Lifetime includes the session, so this is conservative. On the six
   fixture logs it keeps 12 of 19 marks.
4. Aggression frequency counts actions, not hands, so its interval treats each
   action as its own trial. It reads a little narrow for that.

### All-in EV

What a player's all-ins were *worth* beside what they *took*. `equity.py` computes
the equities and `allin.py` turns them into rows; the page and `pnt allin` read those.

```
expected  = Σ over pots of  pot × P(this player wins the pot among its eligible players)
adjusted  = expected  − contributed + bounty
actual    = collected − contributed + bounty        (the `net` above, unchanged)
diff      = actual − adjusted                        positive ran above expectation
equity    = expected / pot                           share of the whole pot
```

| Term | Definition |
|---|---|
| **Population** | Complete hands that reached showdown with at least one all-in action, where every unfolded player's cards are known. One row per (hand, unfolded player). A hand with a mucked live hand is *counted* as `skipped.cards_unknown`, never silently dropped. Folded players have no row: they have no claim on the pot |
| **Decision point** | The street of the last voluntary action -- where the betting stopped. The board is the first run's cards dealt by then: 0, 3, 4 or 5. A run-it-twice second board repeats that prefix, so the first run is what everyone saw when they committed |
| **Pots** | Layered from `contributed` (which already excludes uncalled returns): the distinct amounts the live players put in are the layers, each open to the live players who reached it. Every chip is in some pot, a folded player's included; Σ pots = `pot` by construction. A layer only one player reached is folded into the pot below it -- see judgement call 15 |
| **Method** | `exact` when two or fewer cards are to come (990 boards after a flop all-in, 44 after a turn one, 1 on the river); `sampled` preflop, 50,000 deals from a generator seeded by the cards and pots, so the same hand always gets the same answer and its error stays under a quarter of a percent |

Because `actual` is the same `net` every other view prints, the two lines on the page
differ only where the deck did something: **within one hand the `diff`s sum to zero.**
A river all-in has no cards to come, so its equity is 0, ½ or 1 and its `diff` is 0;
those rows stay in the population so `actual` covers every all-in showdown, and the
by-street split shows where the variance actually was.

Equities depend on cards and pot layout alone -- never on a hand id or an identity --
so they are memoized in `equity_cache`, the one derived table in the schema. It is
disposable: drop it and the next request refills it, with identical numbers.

### Head to head

The money between two players: who has paid whom, and how much. `h2h.py` mirrors
this section; `GET /players/{alias}/h2h` (one row per opponent), `GET /h2h` (the
pool's matrix), `pnt h2h` and the players page read it.

Each hand is settled into transfers, **payer → payee**, on two ledgers kept apart:
the pot (`collected − contributed`) and the 7-2 bounty (`bounty`).

```
winners  = players whose ledger is positive; won_w = their gain
loser l pays winner w:   −ledger_l × won_w / Σ won
```

With **one winner** -- every hand that ended heads-up with a single winner, and
most multiway ones -- that is exact: each loser's net went to the winner, a
folder's dead money with it. With **several winners** (a chop, a side pot won by
someone else) it is the rule in judgement call 1. Either way **every player's
transfers in less their transfers out is exactly their `net`**, so a player's
row against the whole table sums to their net, and the matrix is antisymmetric:
what a won from b is exactly what b lost to a.

| Field (per pair, from a's side) | Definition |
|---|---|
| **hands** | Hands both were dealt into, every hand |
| **counted** | Of those, hands whose money is all there: complete, both ledgers net to zero, a known big blind. Only these move money |
| **net_bb** | Σ over counted hands of the chips b paid a, net, over that hand's big blind. Positive is up |
| **bounty_bb** | The part of `net_bb` that was bounties |
| **flow_hands** / **split_hands** | Counted hands where chips moved between them; of those, the ones settled by the multiway rule rather than exactly |
| **showdowns** | Both still in when the hand went to showdown |
| **failed_bluffs** | a's `failed_bluff` flags (see "Hand review") that b called or raised |
| **bluff_spots** / **bluff_known** | Hands where a lost a pot of at least `failed_bluff_pot_bb` after b called or raised a's postflop bet or raise; of those, the ones where a's cards were known, so the flag could be judged. `failed_bluffs ≤ bluff_known ≤ bluff_spots` |

Each opponent row of `/players/{alias}/h2h` also carries the chart filter for
the hands against them and **`n`**, how many of the player's hands it keeps. The
filter is `vs=<name>`, which is "still in when I last acted", so `n` is at most
`hands` and usually smaller.

Judgement calls:

1. **Several winners split each loss by what each won, not by what each
   collected.** A side pot is why: all in for 20, A wins the 60 main pot and B
   the 60 side pot from C. Both collected 60, but A won 40 and B 10; split by
   collected, C would pay each 25 and A's transfers would no longer add up to
   A's net. Split by gain, C pays A 40 and B 10. In a plain chop the two rules
   agree.
2. **A hand whose money is not all there moves nothing.** An incomplete hand, or
   one whose pot or bounties do not net to zero (two of 14,716 complete hands on
   the database this was built on), is a hand played together and nothing more,
   the rule in "Money". The response counts them under `skipped`.
3. **Bounties go to the player they were paid to.** They are their own ledger:
   settled with the pot, a bounty would be spread across the pot's winners. The
   log names payer and payee, but the hand keeps only each player's sum, so a
   hand with two bounty receivers would fall back to the proportional rule; none
   has occurred.
4. **Two IDs of one person in the same hand are one person**, and money between
   them is not a pair. Folding happens through the identity map at read time, so
   a merge or a split re-answers on the next request.
5. **The chart filter names the opponent only when it names nobody else.**
   `vs=` matches a display name or a raw ID, ignoring case, so `hsj` and `HSJ`
   are one term for two people. Then a player with one PokerNow ID is named by
   that ID (`vs=<pn_id>`); one with several, sharing a name, gets `filter: null`,
   since a filter cannot say "this ID or that one". A name with a comma would
   split the term, and is treated the same way.
6. **Failed bluffs need the bluffer's cards**: shown at a showdown, or your own.
   Your failed bluffs are therefore always judged and everyone else's only when
   shown, which is what `bluff_known / bluff_spots` is there to say.

### Results by pot type

Where one player's big blinds were won and lost, inside whatever spot is on the
chart. `results.py` mirrors this section; `GET /players/{alias}/results`, `pnt
results` and the chart's **Results** view read it.

Three tables over the same hands. Each **partitions** them, so its rows add up
to the spot's total: the same hand count, and the same net to the exact fraction.

| Table | Rows | Filter each row adds |
|---|---|---|
| **Pot type** | walk, limped, single-raised, 3-bet pot, 4-bet pot+ | `limped,!acted_preflop` · `limped,acted_preflop` · `srp` · `3bet_pot` · `4bet_pot` |
| ↳ **role**, raised pots only | PFA, caller / other | `<pot>,pfa` · `<pot>,!pfa` |
| **Position** | each seat `position=` names, in preflop acting order, blinds last; then dead button | `position=<seat>` · `dead_button` |
| **In position after the flop** | in position, out of position, no flop | none · none · `!saw_flop` |

Every row is a `queries.Tally`, read out by `rates`, so the money is the money
bb/100 is made of everywhere: each complete hand's net over its own big blind.
**`net_bb`** is that sum; **`bb_per_100`** is it over **`bb_hands`**, the
complete hands with a known big blind. **`hands`** counts every hand. An
incomplete hand is therefore in a row's `hands` and in none of its bb figures --
the rule in "Money", unchanged. The other columns: `vpip_pct` and `saw_flop_pct`
are shares of the row's hands, so "lost it playing" can be told from "lost the
blind folding"; `wtsd` and `wsd` are the stat rows' own.

**The worst row.** In each table, the lowest bb/100 among the rows standing on
at least **100 hands with a known result** (`WORST_MIN_HANDS`) is marked
`worst` -- and only when it is losing. Role rows are candidates alongside their
pot type: the worst place to be is usually one side of a pot. The **no flop**
row is never a candidate: it pays the blinds and nothing else, so it is losing
for everyone and would be marked in almost every table.

Judgement calls:

1. **A walk is the big blind folded to, and only from the big blind's seat.**
   `pot_level` 1 is every pot nobody raised; a walk is the part of it where this
   player never had a preflop decision, which on a complete hand is only ever
   the big blind (or a straddler) everyone folded to. It is a per-seat row on
   purpose: the players who folded around to someone else's walk made a real
   decision -- the small blind paid for it -- so they stay in **limped**, which
   is "nobody raised", as the `limped` term has always meant. Two rare hands
   also land in walk: a blind all-in from its post, who never acted, and a hand
   whose log stops before the player's first action. Both are counted rather
   than guessed at; the second is already out of every bb figure.
2. **A limper, and the big blind checking its option, are "caller / other".** A
   limped pot has no preflop aggressor, so it is not split by role at all:
   everyone in it would be "other". In a raised pot "other" is everyone but the
   last raiser -- the cold-caller, the blind who defended, the opener who called
   the 3-bet, and the player who folded preflop. `vpip_pct` and `saw_flop_pct`
   say how much of the row is each.
3. **Seats follow the `position=` filter, not `positional_report`.** A row must
   reproduce from its filter, and the filter labels a hand with a dead small
   blind (`blinds_irregular`) where `pnt positions` drops it; here it keeps the
   label it filters on. A dead button gets no label from either, and is its own
   row.
4. **In position is read only on hands that saw a flop.** `in_position` is set
   on a preflop fold as well, from the seats, but a fold has no position to
   play, so those hands -- and walks, whose `in_position` is None -- are the
   **no flop** row instead of half of in position. A hand that saw a flop with
   the order unknown gets a row of its own when one occurs.
5. **No filter term says in or out of position yet**, so those two rows carry
   `filter: null` and their `hand_ids`; the chart lists those hands without
   touching the spot. An `ip` flag would make them clickable like the rest.

### 7-2 bounty ledger

Who has collected and paid the 7-2 side bet. `bounty.py` mirrors this section;
`GET /bounties`, `pnt bounties` and the leaderboard on the players page read it.
SQL over `hand_players.bounty`, the signed side-bet result already in `net`,
resolved to people through `player_identities`. Nothing is derived.

| Figure | Numerator | Denominator / coverage |
|---|---|---|
| **won** | hands with `bounty > 0`: count, Σ chips, Σ chips / that hand's big blind | `hands`: every hand the player was dealt into |
| **paid** | hands with `bounty < 0`, the same three, as positive figures | the same |
| **net** | won − paid, in chips and in bb | `bb_known`: bounty hands with a big blind on record |
| **dealt 72** | hero's hands whose cards are `72o` or `72s` (`hand=72`) | `known / hands`: hero's hands with cards known, of hero's hands |
| **played 72** | of those, the ones with `vpip` (`hand=72,vpip`) | dealt 72 |

**hero** is whoever owns a log: the player behind `games.hero_pn_id`, over that
game's hands only. A database holding several people's logs has one hero row each.

The ledger lists only players with at least one bounty hand, ranked by net chips.
Across them, **net sums to zero**: a side bet moves chips and creates none.
`balance` reports the sum anyway, because a payment to or from a player not dealt
into the hand is booked on one side only (`parser._apply_bounty`).

Judgement calls:

1. **Incomplete hands are not excluded.** The bounty is settled after `-- ending
   hand --`, so a log that stops mid-hand has no bounty lines for that hand: there
   is nothing to exclude, and the bb/100 rule in "Money" never comes up.
2. **A bounty hand with no big blind on record adds its chips but no bb figure**,
   and `bb_known` says how many hands the bb figures stand on.
3. **Hero's 72 counts use only the logs hero owns.** There `Your hand is` names
   hero's cards on every hand; in someone else's log hero's cards are known only
   when shown, and a shown 72 is nearly always a played one. The filters run over
   every hand of the player, so a 72 they showed in a log they do not own would
   join the count. Where one has, that figure's filter (`dealt_filter`,
   `played_filter`) is null and the page shows the count unlinked.
4. **Each row's counts carry the filter that finds their hands**: `won_filter`
   is `bounty_won` (`bounty > 0`) and `paid_filter` is `bounty_paid`
   (`bounty < 0`), so run over every hand of the player through
   `/players/{alias}/hands` each lists exactly its count. The page links the two
   counts to the chart on them; the rest of the row still opens the player's
   card. A bounty paid to or by a PokerNow ID with no player row has no alias to
   open, so its filters are null.

### Red and blue lines

One player's winnings as running totals in hand order, split on whether they went
to showdown. `winnings.py` mirrors this section; `GET /players/{alias}/winnings`,
`pnt winnings` and the graph at the top of the chart's **Results** view read it.

```
x          = net / big_blind               each complete hand with a known big blind
total[i]   = Σ x over the first i such hands, oldest first
blue[i]    = Σ x over those hands with wtsd
red[i]     = Σ x over those hands with not wtsd
red + blue = total                          at every hand
```

| Line | Hands | Filter |
|---|---|---|
| **Total** (grey) | every complete hand with a known big blind | none |
| **Blue** (showdown) | `wtsd`: saw the flop, the hand went to showdown, and they had not folded | `wtsd` |
| **Red** (no showdown) | everything else: preflop folds, walks, pots won with a bet, pots given up | `!wtsd` |
| **All-in adjusted** (dashed, optional) | the total, with each all-in showdown's `adjusted` (see "All-in EV") in place of its net | none |
| **Session bars** | one per `game_id`, oldest first: Σ x over that game's hands, plus its red and blue parts | none -- `hand_ids` |

The money is bb/100's money: each sum is the exact fraction `Tally.bb_sum` is, so
the last point of `total` is `rates()["net_bb"]` for the same hands, to the cent.
**`hands`** counts every hand in the spot and **`bb_hands`** the ones in the
lines; each line carries both, so `known / n` is always on show. Every point is
rounded from its own exact sum, so `red + blue` can sit a cent off `total`.

Judgement calls:

1. **Incomplete hands are in no line**, by the rule in "Money": a hand whose log
   stops early has no result, and one with no known big blind has no size. Both
   still count among the hands, and on their side of the red / blue split, so
   each line's filter finds its `hands` exactly.
2. **Red is "not `wtsd`", not "won without showdown".** It is every hand this
   player did not take to showdown, won or lost: the pots they took with a bet,
   the ones they folded, and the blinds. A preflop all-in that ran out the board
   is blue, since `wtsd` counts the board being dealt as seeing the flop.
3. **The adjusted line only moves on all-in showdowns with every live hand
   known.** Its population is "All-in EV"'s, narrowed to this player's hands in
   the spot. An all-in showdown with a mucked live hand has no equity to pay by,
   so it keeps its actual net and is counted as `adjusted.skipped`. The graph
   draws the line only when at least one all-in was adjusted.
4. **No filter term names a game**, so a session bar carries its `hand_ids`
   within the spot and the chart lists those hands without touching the spot.

### Give-up funnel

Where one player's flops went, inside whatever spot is on the chart. `funnel.py`
mirrors this section; `GET /players/{alias}/funnel` and the last tables of the
chart's **Results** view read it. WWSF and W$WSF themselves are defined in "Won
when saw flop"; the stats page shows them as columns.

| Row | Counts | Filter (`n`) | Share of |
|---|---|---|---|
| **saw the flop** | `saw_flop` | `saw_flop` | — |
| **reached the turn** | had a turn `seq` key: still in when the turn began, all-in included | `saw_flop,action_turn=*` | flops seen, and the row above |
| **reached the river** | the same on the river | `saw_flop,action_river=*` | the same |
| **went to showdown** | `wtsd` | `saw_flop,wtsd` | the same |
| **won at showdown** | `wsd` | `saw_flop,won_sd` | the same |
| **won the pot** (WWSF) | `saw_flop` and `won_pot` | `saw_flop,won_pot` | flops seen |
| ↳ **without showdown** (W$WSF) | and not `wtsd` | `saw_flop,won_pot,!wtsd` | flops seen |
| **folded the `<street>`** | `fold_street == <street>`, flop, turn, river | `saw_flop,fold_street=<street>` | `of`: hands that reached it, `saw_flop[,action_<street>=*]` |
| ↳ **as the previous street's aggressor** | and `pfa` on the flop, `aggressor[prev]` on the turn and river | `…,pfa` / `…,aggressor_<prev>` | `of`: hands that reached it so |
| ↳ **not the aggressor** | and not that | `…,!pfa` / `…,!aggressor_<prev>` | `of`: the same |

Every count is over every hand in the spot, so each row's `filter`, added to the
spot, finds exactly its `n`, and a fold row's `of_filter` its `of`. A fold row's
`pct` is `n / of`; its `share` is `n` over every fold after the flop.
**`complete`** says how many of the flops seen come from a log that ran to the
end, the funnel's `known / n`.

Judgement calls:

1. **"Reached" is the whole-street sequence's key**, which every player still in
   when the street began has, an all-in player included (call 33 in "Known
   judgement calls"). A player all in on the flop reached the turn and the river:
   they were in the pot when those cards came.
2. **The steps are not forced nested; they nest by derivation.** Showdown is the
   stat rows' own `wtsd`, so the funnel's last two steps are WTSD and W$SD exactly.
   A showdown always reached the river, since a board that ran out keys every
   street, and a test holds the steps non-increasing on every fixture log.
3. **Won at showdown is the funnel's last step; WWSF is beside it, not in it.**
   WWSF counts pots won without a showdown too, so it can exceed the showdowns.
   Showdown wins plus the wins without one are WWSF exactly.
4. **Not the aggressor is everyone else who reached the street**, not only the
   callers of the last bet: a street that checked through had no aggressor, and
   in a limped pot nobody is the preflop aggressor, so all their folds are here.
5. **A hand cut short counts as far as its log got.** It reached no further, and
   its fold, if any, is counted; leaving it out would break the filter
   reproduction, since no term says "complete". `complete` shows how many there are.
6. **Preflop folds are not in the fold table.** It is about flops seen; preflop
   folds are VPIP's question.

---

## Hand review

A *flag* is one hand this player should look at again, with the reason written
out and the replay a click away. `review.py` mirrors this section, `THRESHOLDS`
is the other copy of the numbers below, and `GET /players/{alias}/review`,
`pnt review` and the chart's **Hand review** and **Bad beats** views read it.

One row per (hand, flag). Mistakes and beats are two groups because they are two
questions: the first is about the decision, the second only about the deck.

| Group | Flags |
|---|---|
| **mistake** | `missed_bluff`, `missed_value`, `failed_bluff` |
| **beat** | `suckout`, `cooler_pre`, `cooler_post` |

**A flag is a prompt, not a verdict.** Nothing here knows the ranges, the table
or the reads. It knows two hole cards on a board and where the chips went, and
the row exists so a person can open the replay and judge.

### What the flags cannot see

Villains' cards are known only at showdown, so every flag that reads them --
missed bluffs, missed value, postflop coolers -- is drawn from showdowns alone,
and the hands where a live hand was mucked are *counted* in `skipped`, never
silently dropped. The reviewed player's own cards are complete only for the
hero; for anyone else `failed_bluff` is the bluffs that got shown, which is the
sample most biased toward the ones that failed. `examined`, `showdowns` and
`known_showdowns` ride on every answer, so the coverage is beside the count.

### Thresholds

| Name | Value | Where it is used |
|---|---|---|
| `bloated_pot_bb` | 15 | A checked-down pot worth looking at; the floor for a postflop cooler with no all-in |
| `failed_bluff_pot_bb` | 15 | The pot a called bluff has to have been in |
| `stacks_in_share` | 0.5 | Missed value: the final pot was under this share of the two effective stacks |
| `suckout_equity` | 0.6 | At least this far ahead when the money went in |
| `cooler_equity` | 0.5 | At most this far ahead, for a preflop cooler |
| `deep_bb` | 120 | A 3-bet pot this deep or shallower plays for stacks with TPTK |
| `premium` | QQ+, AKs, AKo | The preflop hands a cooler is measured on |

### Three definitions the flags share

**Air** is high card, a draw, or a pair that is entirely on the board. An
underpair or bottom pair is a hand: checking it down is a thin-value question,
not a missed bluff.

**A stacks hand** is one worth playing 100bb for, by tier:

| Tier | Where | What counts |
|---|---|---|
| `tptk_plus` | 4-bet pots, and 3-bet pots no deeper than `deep_bb` | An overpair, top pair top kicker, top two pair, or a set or better that needs both hole cards |
| `two_pair_plus` | everywhere else -- limped, single-raised, and deeper 3-bet pots | Top two pair, or a set or better that needs both hole cards |

"Needs both hole cards" is the test that keeps the board's own hands out: a set
qualifies, one hole card to a paired board (trips, or a full house the board
mostly made) does not. Everyone at the table can have that piece.

**A dry board** has no `flush_possible`, no `straight_possible` and no `trips`
among its texture tags, so the hands are what they look like. A five-card river
almost always fails that test, so a hand is judged on the **earliest street the
board was still dry** -- which is where the chance to get the stacks in was.

### The flags

| Flag | Population | Fires when | Notes |
|---|---|---|---|
| **missed_bluff** | Complete showdowns with a river dealt and every live hand known | The river checked through with two or more players acting on it; the final pot is at least `bloated_pot_bb`; **every** live hand is air | Flagged for each live player: they all passed up the same bet |
| **missed_value** | The same, with no all-in in the hand | On the earliest dry street, this player **and** some villain both hold a stacks hand at the tier for that pot and effective stack, and the final pot came to less than `stacks_in_share` x 2 x the effective stack | Both sides get the flag on their own review |
| **failed_bluff** | Complete hands this player's cards are known in, with a final pot of at least `failed_bluff_pot_bb` | They bet or raised postflop with air on that street's board, at least one player called or raised it, and they lost the hand | One row per hand, at the **last** such bet. A draw is a semi-bluff on the flop and turn; on the river it has missed, so it is air |
| **suckout** | Their all-in showdown rows (`allin.py`) | Equity at least `suckout_equity` where the betting stopped, and they lost chips | A 55% flip that loses is a flip, not a beat, and a chop is neither |
| **cooler_pre** | The same, all in preflop | A `premium` holding with equity at most `cooler_equity`, and they lost chips | |
| **cooler_post** | Complete showdowns with every live hand known, that they lost | On the earliest dry street up to where the betting stopped: they held a stacks hand, a villain was **already ahead** there, and that villain won. With no all-in the pot must reach `bloated_pot_bb` | A villain who was behind there and got there later is the deck's doing, not a cooler |

Each row also carries the effective stack in big blinds, the flop SPR, the pot
type, and -- for a failed bluff -- the size of the bet and who answered it, with
their archetype, their WTSD and whether they had bet or raised earlier in the
hand. That last one is a heuristic for an uncapped range, and is labelled as one.

### Marking a hand reviewed, and writing down what went wrong

A flag says a hand is worth a look. A **mark** says you have taken it, and a
**note** says what you found -- the mistake, the read, the question to settle
later. Both are entered by hand, and they are the only things on these pages that
are *stored* rather than derived on every request.

| | Sets it | Clears it | On the page | In a row |
|---|---|---|---|---|
| Mark | `pnt reviewed <game_id> <hand_number>` | `--undo` | the ✓ beside the row | `reviewed`, `reviewed_at` |
| Note | `pnt note <game_id> <hand_number> "<text>"` | `--clear` | the ✎ beside the row | `note`, `noted_at` |

`POST /hands/{hand_id}/reviewed` and `POST /hands/{hand_id}/note` are the same two
writes for the page; `pnt note` with no arguments lists every note, and
`pnt review --noted` lists only the hands that have one.

**Both are on the hand, not on the flag or the player.** You watched a replay or
you did not: a hand carrying two flags, or showing up on two players' reviews, is
marked once, noted once, and reads that way everywhere.

**Both are keyed on `(game_id, hand_number)` -- the log's own name for the hand --
never on `hand_id`.** `hand_id` is a rowid the importer hands out, and a rebuild
deletes a game's hands before re-inserting them, so every id in that game moves; a
mark keyed on one would come back pointing at a different hand. For the same
reason neither table has a foreign key to `hands`: a cascade would erase them on
the rebuild that was supposed to preserve them.

Alone among the layer-2 tables, marks and notes are never thrown away -- a
rebuild, a merge or a rename leaves them untouched -- because no log can
re-derive a judgement you made. They are the same kind of row as an identity merge
or a player note.

**The mark and the note are independent**, which is why they are two tables and
not one column beside the other. Clearing the mark on a hand you want to come back
to must not delete what you typed about it, and a note on a hand still to be
reviewed -- "check the turn sizing here" -- is the ordinary case, written before
the second look rather than after. Writing a note again replaces it: a note is a
document, not a log, and one that strips to nothing is a deletion. `noted_at` is
therefore when it was *last* written, where `reviewed_at` is when the hand was
*first* marked.

**A manual flag is the third judgement** (`hand_flags`): "look at this one later",
set with the HUD's 🚩 for the hand that just ended, or with the 🚩 beside any list
row. It is keyed, kept and made idempotent like the mark (`flagged_at` is when it was
*first* set), and is independent of both the mark and the note. The Manual review
tab lists the flagged hands; `GET /players/{alias}/flagged` serves it, and
`POST /games/{game_id}/hands/{hand_number}/flag` sets or clears one. Two things
differ from the mark:

- **It is addressed by `(game_id, hand_number)`, not `hand_id`.** The HUD flags a
  hand seconds after its ending line arrives, often before the rebuild has derived
  it, so there is no `hand_id` yet.
- **It is checked against the raw log.** A flag is accepted for any hand with a
  `-- starting hand #N` line in `raw_entries`, derived or not. The Manual review
  list still shows only derived hands, so a fresh flag appears there once the
  rebuild lands.

The HUD finds "the previous hand" in `/live`'s `previous` field: the newest
`-- ending hand #N --` line in the raw log, with whether it is flagged. It is
present between hands as well as during one.

`pnt review --unreviewed` lists only what is left, and the chart's **Hide
reviewed** button does the same for the Hand review and Bad beats views. Notes are
never hidden: they are the one thing on the row you wrote yourself.

**A player note is the same judgement about a person** ("tanks then jams with
air"). It lives on the canonical player (`players.notes`, `notes_at`), not on a
PokerNow ID, so it follows them across devices; the rules are a hand note's --
stripped, replaced on every write, deleted when emptied. `pnt player-note <alias>
"<text>"` and `POST /players/{alias}/note` write it; `/players` and each `/hud`
seat carry it, and the players page, the stats table, the chart and the HUD card
show it. Three operations delete player rows, and none may take a note with them:
a **merge** appends the source's note to the target's after a `— ` line, an
**alias re-sync** that empties a player hands its note to whoever now holds its
IDs, and **deleting a game** never removes a player with a note. No statistic
reads it, so writing one invalidates no cache.

### Reproducing a flag on the chart

Unlike a tag, a flag cannot be turned into a filter that finds exactly its hands:
the spot language does not read the villain's cards. Each one is a **subset** of
the hands its widest filter lists, and a test holds it to that:

| Flag | Filter that contains it |
|---|---|
| `missed_bluff` | `wtsd,pot_bb>=15` |
| `missed_value` | `wtsd` |
| `failed_bluff` | `lost,pot_bb>=15` |
| `suckout` | `wtsd,lost,jam` |
| `cooler_pre`, `cooler_post` | `wtsd,lost` |

### Judgement calls

19. **Both sides of a missed-value hand are flagged.** The row says the money
    did not go in, not whose fault that was.
20. **One beat per hand**, tested `suckout`, then `cooler_pre`, then
    `cooler_post`. A hand listed twice would read as two beats.
21. **A chop is never a beat.** Every beat needs chips actually lost.
22. **`suckout_equity` is 0.6, not 0.5.** Being 53% and losing is variance in the
    ordinary sense, and calling it a bad beat makes the list useless.
23. **Missed value and coolers are judged on the earliest dry street, not the
    river.** Almost every river board has a straight or a flush available, so the
    river would answer "no hand was ever safe" and flag nothing at all.
24. **One hole card to a paired board is not a stacks hand.** Trips with a
    kicker, or a full house the board mostly made, is a hand everyone can hold a
    piece of -- and the pool's own hands say so: the first draft of this section
    flagged trips against trips as missed value.
25. **A failed bluff is judged at its last called street, and preflop bluffs are
    out of scope.** A three-barrel bluff is one row, on the river; a light 3-bet
    that got called is a preflop range question, which the tags already answer.
26. **An unknown starting stack is unknown, not zero.** Effective stacks and SPR
    are `None`, the hand is skipped for the flags that need them, and `skipped`
    counts it. Re-importing the log repairs it.
27. **A bluff a villain raised reads as `raised`**, from this player's last
    aggressive action on that street alone. A bet, a raise over it and a re-raise
    back is one row, not three.

---

## Biggest pots

The one view that does not start with a player. `pots.py` mirrors this section,
and `GET /pots`, `pnt pots` and the **Biggest pots** page read it.

Two knobs, and the defaults are the whole idea: **pots over 2,000 chips in the
last 7 days**, biggest first.

| Name | Default | What it means |
|---|---|---|
| `days` | 7 | How far back the window reaches, from the clock at request time. None (`--all`, `all_time=1`) is all of history |
| `min_pot` | 2000 | Chips a pot must reach to be listed |
| `limit` | 50 | How many pots one answer carries |

**The pot is `SUM(contributed)` over the hand's dealt-in players** -- every chip
that stayed in the middle. Uncalled bets are not in it (the over-shove nobody
matched never sat there) and neither are 7-2 bounties (they are a side bet, not
the pot). That is `derive`'s `Facts.pot` exactly, evaluated in SQL so that finding
the biggest pots in 9,000 hands does not cost 9,000 derivations: only the pots
that clear the bar are loaded and derived, for the seat-by-seat figures.

**The window is a string comparison against `hands.ts`**, which the parser stores
as ISO-8601 UTC to the millisecond with a `Z`. The cutoff is rendered in that
exact format. SQLite's own `datetime('now', '-7 days')` cannot be used: it renders
`2026-09-09 07:45:01`, a space where every stored timestamp has a `T`. A space
sorts *before* `T`, so a hand dealt at `2026-09-09T00:12:00.000Z` -- seven hours
before that cutoff -- compares as later than it and leaks into the window. The
bug would be almost a day of stale hands at the boundary, and nowhere else.

**A capped list says so.** `over` counts every pot that cleared the bar and
`hands` every hand in the window, so a list of 50 out of 200 is visibly a slice
rather than quietly the whole of it.

**A chopped pot names no winner.** Where two or more players collected, "winner
+13" would describe a pot everyone mostly got back; the row says *chopped* and
lists who shared it. `winner` is otherwise the largest net, `loser` the smallest,
and both are null in a hand where nobody finished up.

**An incomplete hand is listed and labelled.** A log that stops mid-hand has only
some of its chips on record, so its pot is short; `complete` is false and the row
is marked, on the same principle as everywhere else -- counted and flagged, never
silently dropped.

`player` narrows to the hands one person was dealt into. It is the same list seen
from one seat, not a different question: the pot, the winner and the seats are
still the table's.

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
   only the *position label* is uncertain, not the actions. IP/OOP is likewise
   still computed on them — see call 11, which is the same distinction again.
5. **Antes and dead small blinds** contribute to the pot but not to a player's
   street commitment, so they do not reduce what that player must pay to call.
6. **Each bet-size edge belongs to the bucket above it, rounded down to a chip**,
   so every PokerNow ½, ¾ and pot click lands in the bucket it starts, and a
   pot-sized bet is `large` rather than an overbet. See "Bet size buckets".
7. **A donk bet needs a previous-street aggressor who has yet to act.** A lead
   after a checked-through street, or into an aggressor who is already all-in,
   is not counted as one.
8. **IP/OOP is one label for the whole hand, taken from the first postflop
   orbit.** It is not recomputed when a later fold changes who closes the action:
   a player who is out of position three-handed on the flop still reads OOP on
   the row even if the player behind them folds that flop. A per-street map would
   triple the payload of a drill-down row that has one line of space.
9. **Multiway, only the player who closes the action is IP.** Everyone else is
   OOP, including a player sandwiched in the middle. The exact place is carried
   as `pos_order` of `pos_players` rather than invented as a third label.
10. **`opponents` is "still in at my last action"**, so it can omit a player named
    in `faced_cbet_by`. See "Who the action was against".
11. **Acting *order* survives a dead button or a dead blind, even though the
    position *label* does not.** Call 4 bars those hands from positional splits
    and still prints no BTN/SB name for them. IP/OOP is a different claim — who
    acts after whom — and a BB-anchored rotation and a skipped slot both preserve
    it. The fixtures show why this is not merely permissible but necessary: on
    dead-button hand #172 of `pgl9BTQl8oZL` the flop order is FQN → d-4X → gpP,
    so gpP closes the action, while the seat labels say FQN is on the button and
    therefore last. The seat labels are the ones that are wrong, which is exactly
    what call 4 warns about.
12. **All-in EV is measured from the last voluntary action, for everyone in the
    hand.** When a short stack is all in preflop and two others keep betting to
    the turn, the short stack's equity is taken on the turn board too, not on the
    preflop one. What happened between was other people's decisions, and the one
    board every pot was decided on is the one where the betting stopped. Thirteen
    hands in the 8,036-hand database are like this.
13. **River all-ins stay in the all-in EV population with a `diff` of zero**, and
    bounties are in both `actual` and `adjusted`, so `diff` is pure variance.
14. **Preflop equity is sampled, not enumerated.** 1.7 million boards heads-up is
    fifteen seconds of pure Python per hand; 50,000 seeded deals is half a second
    and reproducible, with an error smaller than a single chip on any pot in the
    corpus. Every row says which it was.
15. **Chips only one player could win go with the pot below.** PokerNow sometimes
    lets a caller put in a few chips more than the all-in and returns nothing: in
    hand #73 of `pgl8vNV4WURe` the caller put in 915 against 910, and the winner
    collected all 1,945. Treated as a side pot of 5 that only the caller could win,
    the hand would show a 5-chip gap on a river all-in, where there is no gap to
    have. It is paid where PokerNow paid it. The one other river gap in the
    database is the odd chip of a chopped pot, which is real and half a chip.
28. **A raise is sized after the call, against the pot the call would make.**
    `raise_pot = (amount − to_call) / (pot_before + to_call)`. Checked against
    the 1,419 postflop raises that were not all in across the bundled logs
    (`pnt/logs` and the fixtures), with each convention asked how many raises
    land within a chip of ½, ¾ and pot exactly:

    | Convention | ½ | ¾ | pot |
    |---|---|---|---|
    | extra over pot after the call (this one) | 152 | 107 | 84 |
    | extra over the pot before the raise | 75 | 35 | 70 |
    | raise-to over the pot before the raise | 39 | 46 | 119 |

    Only the first has a spike at all three button sizes -- in 0.05-wide bins,
    184 raises at 0.50 against 67 and 31 either side, 118 at 0.75 against 28
    and 24, 86 at 1.00 against 9 and 6 -- so it is the one PokerNow's raise
    buttons use. The broad hump between 0.15 and 0.45 is min-raises, which
    land wherever the bet they raise puts them: a min-raise of a ½-pot bet is
    0.25, of a pot bet 0.33. Raise-to over the pot has its own spike at 1.00
    (raise to the size of the pot) but nothing at ½ or ¾.
29. **A blocker is under a fifth of the pot.** No button makes one, so the edge
    is a judgement value; a fifth keeps the one-blind poke into a big pot apart
    from the ⅓-pot bet, which is a real sizing and not a blocker.
30. **The facing-a-bet sizes are the first decision only.** `faced_level`,
    `faced_bet_pot`, `faced_bet_size` and `faced_bet_action` describe the first
    time the player faced a bet on the street, as `fold_to_cbet_opp` does for
    c-bets, so each is one answer per street and the by-size table counts each
    decision once. A bettor raised and re-raised again answered the raise.
31. **`made_<street>` stops at a fold; `made=` does not.** The per-street term
    is about the hand a player held while acting, and a player who folded the
    flop held nothing on the turn. The final-board term is the range view's
    question, which classifies every known holding on the board as dealt.
32. **`line_<street>!=` is the exact complement of `=`.** Not acting is a value,
    `-`, so a hand that never reached the street matches every `!=` pattern
    except `-`. Pair it with `line_<street>=*` to mean "acted, and not like this".
    `action_` reads the same way.
33. **An all-in player keeps a `seq` on the streets after their all-in**, of the
    others' letters alone, and an unplayed run-out is `""` rather than no key.
    The sequence answers "what happened on this street while they were in the
    pot", and an all-in player is in the pot; "-" is kept for the players who
    were not. A street with fewer than two players still in has no key for
    anyone: the hand was over before it, whatever the board record says.
34. **`faced_check_raise` belongs to the bettor alone.** Whoever raised the bet
    before the check-raise also had it come over the top of them, but their
    raise was already an answer to a bet, and "we bet, they check-raised" is
    the spot the term names. The raiser's view is in their `seq`: `xbR*r`.
