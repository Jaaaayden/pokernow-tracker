# Guide

The full reference for every feature. The [README](../README.md) is the tour; this
is the manual. Exact stat definitions and thresholds are in
[`SPEC.md`](../pnt/stats/SPEC.md), and internals are in
[architecture.md](architecture.md).

- [Getting hands in](#getting-hands-in)
- [Players and aliases](#players-and-aliases)
- [Stats](#stats)
- [Filters](#filters)
- [Range charts and sizing](#range-charts-and-sizing)
- [The live HUD](#the-live-hud)
- [Tags](#tags)
- [Hand review](#hand-review)
- [All-in EV](#all-in-ev)
- [Biggest pots](#biggest-pots)
- [Publishing logs](#publishing-logs)

## Getting hands in

### The log folder

Keep every PokerNow export in one folder, and a bare `pnt import` picks up whatever
is new. That folder is `~/Downloads/pokernow-logs` unless you say otherwise. To use
a different one, pass paths, a folder or a glob straight to `pnt import`, pass
`--log-dir`, or set `PNT_LOG_DIR` to change it for good. `pnt where` prints the one
in effect.

Re-importing is free because duplicate entries are ignored, so the habit is "drop
the export in the folder, run `pnt import`".

**Live capture keeps the folder current by itself.** Each time the server
re-derives a captured game, it writes the game's `poker_now_log_<id>.csv` there,
merged with any copy already in the folder. The folder is therefore a running
record of every game you capture, and enough to rebuild the database from scratch
with `pnt import`. Set `PNT_SAVE_LOGS=0` to turn this off. The server reads
`PNT_LOG_DIR` when it starts, so run `pnt service restart` after changing either
variable.

### Sync: the database follows the folder

**Delete a game's CSV and the game leaves the database**, along with its hands, its
raw lines, and any player that no other game knew about. Merges and renames stay.
Drop a new export in and it is imported. The running server checks every few
seconds (`PNT_SYNC_SECONDS`, `0` to turn it off). Without the server, `pnt sync`
does the same thing once.

The rules that keep this safe:

- Only games that have had a file in the log folder are ever removed. Games
  imported from somewhere else, like a Downloads copy or the bundled sample, stay
  where they are. `pnt sync` counts them, and `--prune-untracked` removes them.
- Deleting the whole folder removes nothing, because a missing folder looks the
  same as an unplugged drive.
- Deletion works on whole files. Lines cut out of a CSV that stays in the folder
  stay in the database.
- Logs with no hand in them are ignored. These come from joining a table and leaving
  before the first deal. Live capture and `pnt backfill` no longer write them, and
  sync and `pnt import` skip them. `pnt sync --remove-empty` deletes the ones already
  in the folder, along with any game they put in the database.

### Backfill: old games by link

For games played before live capture, `pnt backfill` fetches them by link, so you
don't have to click "download full log" on each one. Pass links or game IDs, or
`-f` a text file with one per line. Each game lands in the log folder as
`poker_now_log_<id>.csv`, and games already there are skipped. Requests are paced
because PokerNow rate-limits, so expect about a minute per few thousand lines. Then
run `pnt import`, or pass `--import`.

Without cookies the file has every action and showdown but not **your own unshown
hole cards**, because PokerNow sends those only to you. Copy the `npt` and `apt`
cookies from DevTools (Application → Cookies → `https://www.pokernow.com`) into
`PNT_COOKIE` as `npt=...; apt=...` (`$env:PNT_COOKIE = "npt=...; apt=..."` in
PowerShell). Both are needed: `npt` alone gets nothing more than no cookie at all.
With both, the file is identical to a manual export. They are your login, so don't
put them in a file or a commit. `--refresh` re-fetches games already in the folder
and adds only the lines they lack, so a game fetched without the cookie can be
topped up later.

### The bundled sample

With no exports of its own to import, `pnt setup` loads the
[bundled sample corpus](../pnt/logs): 5,257 real hands, so the first page you open
is not an empty one. Those are someone else's games, and setup says so.
`--no-sample` leaves the database empty for live capture to fill.

`pnt import` falls back to the same corpus when the log folder is empty. The
fallback triggers on *empty*, never on *small*: with one log of your own in the
folder, that log is the only thing imported. Explicit paths and `--log-dir` never
reach the fallback, because being handed a different folder than the one you named
would be worse than the error it replaces.

## Players and aliases

Commands that take a player take an **alias**: a canonical name from
`pnt alias list`, not a PokerNow ID and not a seat. An alias starts as the display
name first seen for an ID, and can be renamed (`pnt alias rename`) or merged.

PokerNow IDs are stable per browser and survive renames and quit/rejoin. The same
human on a second device gets a different ID.

```bash
pnt alias list
pnt alias merge "onlybluffs" "genericpoker"   # one person, two devices
pnt alias split "FQN9hzhzP_" --alias henry    # the inverse: undo a merge, or part two people
pnt alias export                              # save the table to pnt/logs/aliases.csv
pnt alias import                              # and put it back on a fresh database
```

The alias table is the one thing in the database that a re-import cannot rebuild,
so it is kept in the repo as [pnt/logs/aliases.csv](../pnt/logs/aliases.csv). Run
`pnt alias export` after merging and commit the file. `pnt alias import` is safe to
repeat: IDs the database has not seen yet are skipped, so import new logs and run it
again.

The same thing, with the evidence in front of you, is the players page at
[http://127.0.0.1:52000/players](http://127.0.0.1:52000/players). It lists every
person, the PokerNow IDs behind them, and every name each ID has shown. That last
column is the point, because the names are how you recognise someone:

```
harry    <-  bread, Woolball (har), wool, wool ball, fish
charles  <-  chugnuts, The Great Wall, straight teeth, SplayWash
```

No string comparison finds those. Only someone who was at the table knows, so the
page lays out the IDs, names, hand counts and dates, shows exactly what a merge
will move *before* you confirm it, and then offers an undo.

### Player notes

Some reads never show up in a stat: "tanks then jams with air", "tilts after a
lost all-in", "this is Sam's brother". Write them on the player. Open their row on
the players page and type in the box under **rename**; Ctrl+Enter or clicking away
saves it, and an emptied box deletes it. The same note is under the Player dropdown
on the chart page, with a ✎ to edit it where it stands, and a 📝 beside the name on
the stats table holds it as a tooltip. At the table it is under the player's name on
their HUD card, and the ✎ beside the name edits it there. In the compact table, the
name is underlined and the note is its tooltip.

```bash
pnt player-note henry "tanks then jams with air; never folds a river"
pnt player-note henry           # read it back
pnt player-note henry --clear
pnt player-note                 # every note so far
```

The note belongs to the person, not to one PokerNow ID, so it follows them to a
second device once you merge the two. A merge keeps both notes: the merged-away
player's note is added to the end of the other's, after a `— ` line. Nothing else
loses one either: `pnt alias import` hands the note of a player it empties to
whoever now holds the IDs, and deleting a game never removes a player you have
written about. Notes are not in `aliases.csv`, so they stay in the database.
`POST /players/{alias}/note` writes one, and `/players` and `/hud/{game}` return them.

### 7-2 bounties

Above the list, the players page keeps the 7-2 side bet's leaderboard: for each
player who has won or paid a bounty, how many and how many chips (and big blinds)
each way, ranked by net. A side bet only moves chips, so the net column sums to
zero. The won and paid counts open those hands on the chart (`bounty_won`,
`bounty_paid`); click anywhere else on a row to open that player's card. Under it
is how often you were dealt 72 and how often you played it; the two counts open
those hands on the chart. That
part is yours only, from the logs you downloaded: anyone else's folded 72 is never
seen.

```bash
pnt bounties                # the leaderboard and your 72s
pnt bounties --game <id>    # one game
```

`GET /bounties` returns the same. See SPEC.md, "7-2 bounty ledger".

### Head to head

`vs=henry` shows how you play against henry; head to head shows how much money
went between you. Open a player's row on the players page for a **Head to head**
table: one row per opponent, biggest money either way first, with the hands you
played together, the big blinds you won from them (negative is lost), showdowns,
and each side's failed bluffs into the other. The last column opens your chart
on `vs=<opponent>` and says how many hands it will find. **Who pays whom**, above
the list, is the same for every pair at once: each cell is what the row's player
won from the column's, so the cell across the diagonal is the same number negated.

A hand with one winner settles exactly: every loser's chips went to them. With
several winners (a chop, a side pot) each loser's loss is split by what each
winner won, and a hand whose log stopped early moves no money. Bounties go to
whoever they were paid to. SPEC.md, "Head to head", has the rule in full.

```bash
pnt h2h            # the pool's matrix
pnt h2h henry      # henry against each opponent
```

## Stats

`pnt stats` lists every player, most hands first. It prints `--`, not `0`, when a
denominator is empty: a player with no 3-bet opportunities has an *unknown* 3-bet
percentage, and showing `0%` would be a claim the data does not support.

Every player at once is also at
[http://127.0.0.1:52000/stats](http://127.0.0.1:52000/stats). A browser gets a
sortable table, while the HUD, curl and your scripts get the same figures as JSON
from the same URL (the page alone is at `/stats.html`). Pick a street to swap the
postflop columns, toggle the c-bet size mix, and click a player to open their range
chart in the spot you are looking at.

A VPIP of 45% on 20 hands and one on 2,000 look the same in a table, so hover a rate
for its sample and its 95% range (a Wilson interval): `20 preflop decisions · ± 20.0
pts (95%: 25.8–65.8%)`. The `±` column beside bb/100 does the same for the win rate,
from how much each hand's result swings; a +5 ± 30 is not yet a winner. The range is
only a display: the tags keep their own minimum samples.

**SQZ** and **CC** are squeeze and cold-call. A squeeze is a 3-bet after an open and
at least one caller; a player whose SQZ sits near zero lets you open and flat in
front of them without being squeezed off the hand. A cold call is a call of an open
with nothing in yet (no blind posted, no limp); a high CC means a capped range,
since the strong hands would have 3-bet. Hover a cell for the number of chances
behind it. The range chart's **Squeeze** and **Cold call** chips put the same hands
on the chart, and **Got squeezed** is the other side: they called an open and then
faced a 3-bet.

Three columns cover the blind war, which is most of a short-handed home game.
**ATS** (attempt to steal) is how often they open when it folds to them in the
cutoff, button or small blind. **F2S** (fold to steal) and **3BvS** (3-bet vs
steal) are how a player in the small or big blind answers a steal nobody called. A
blind who folds a lot is one to open wider against, and a wide stealer is one to
defend and 3-bet against. Hands with a dead button or a dead blind are left out of
all three, since the seats are guesses there. The range chart's **Steal**, **Fold
to steal** and **3-bet vs steal** chips show the hands behind each column.

**Split by** puts sub-rows under each player. *Players on the flop* gives one row
for heads-up flops (`players_flop=2`) and one for three or more (`players_flop>=3`),
so a 60% c-bet that drops to 20% three-way stands out: their multiway bet is value.
Each sub-row is the spot with that term added, and clicking it opens that chart. The
two rows' postflop counts sum to the player's whole row. *Min hands* applies to the
whole row, so check the sample on hover before trusting a thin sub-row.

*Straddle* splits each player into the hands someone straddled (`straddled`) and
the rest (`!straddled`). The two rows' hands sum to the whole row's. Straddles are
common in home games and push everyone's opens wider, so watch who loosens up
(VPIP and PFR climb) and who keeps folding. A straddle is not a raise: a straddled
pot nobody raised is still limped (level 1), so `3bet_pot` and the other pot types
mean the same thing in both rows.

*Stack depth* splits each player by effective stack at the deal (their stack
against the deepest other one): under 40bb, 40–100, 100–200 and 200+. With rebuys
and uneven stacks, the same player at 30bb and at 250bb is a different opponent,
and the **JAM** column (all-in preflop bets and raises, over their preflop
decisions) is where it shows first. A hand with an unknown stack is in no bucket,
so the four rows can add up to fewer hands than the whole row. The range chart's
**Depth** chips write the same buckets into the spot, and **SPR ≤3** narrows it to
flops where one or two bets get the stacks in.

The postflop columns also cover the bets a missed c-bet leaves open. **DCB** is the
preflop raiser's delayed c-bet, **Probe** is betting into a preflop raiser who checked
the last street back, **F2P** is that raiser folding to the probe, and **Float** is
betting when the aggressor you called checks to you. DCB, Probe and F2P need a
checked-through street first, so they appear on the turn and river only. A player
who never probes lets you check back the flop for free. One who always floats makes
your turn check-raise a weapon. The chart's Lines view has **probe** and **float**
chips for the turn.

The front door, [http://127.0.0.1:52000](http://127.0.0.1:52000), shows what is in
the database (hands, players, log lines and parse misses), links to every page, and
lists the handful of commands worth knowing when something looks wrong.

### Positional breakdown

```bash
pnt positions genericpoker
```

```
position                 Hands  VPIP   PFR  3Bet F3Bet CBetF FCBetF   AFf  WTSD  W$SD bb/100
--------------------------------------------------------------------------------------------
BB                         207  69.7  28.4  23.2   100  74.2     40  76.9  41.7  63.6 163.24
BTN/SB                     200  82.5  57.5  12.5  46.2  48.8      0  58.4  46.2    60 309.35
BTN                          8   100  62.5     0    50  66.7      0  83.3   100  66.7 4252.5
SB                           6   100  33.3    40    --   100     --  71.4    40     0 -23.33
```

Table sizes are **pooled** by default. `--split-by-size` separates 3-handed from
heads-up instead. Hands with a dead button or a dead blind are left out of this view
only, because the position label is best-effort there. They still count in
`pnt stats`.

### Tilt and session dynamics

The chart's **Tilt** view (also at `GET /players/{alias}/tilt`) answers "is he a
different player after losing a big pot?" It compares the 20 hands after any hand
that lost 40 bb or more in the same session against all their hands and against
every other hand, and then splits their hands by hours since they sat down. Each
row shows VPIP, PFR, postflop aggression and bb/100 with their sample sizes.
Clicking a row adds its filter to the spot. The same context works as filter terms
anywhere: `post_loss`, `hands_since_loss<=5`, `session_hand>=100` and
`session_hour>=3`. In the HUD, a **tilt?** chip sits on any player whose next hand
falls inside that 20-hand window. Clicking it opens their Tilt view, so you can
check whether a loss has ever changed how they play.

## Filters

The Holdem Manager approach: pick a spot, then look at behaviour inside it. Every
command and page that takes `--filter` (or has a **Spot** box) accepts the same
terms, comma-separated.

```bash
pnt stats --filter 3bet                    # only hands where they 3-bet preflop
pnt stats --filter "cbet_flop,position=BTN"
pnt stats --filter "faced_cbet_flop,players>=3"
```

**The hand.** `pot>=500` keeps hands whose final pot reached 500 chips (`pot_bb>=50`
says it in big blinds). `vs=henry` keeps hands played against henry, meaning he was
still in when the player last acted (the same "vs" that a hand row prints).
`vs!=henry` keeps the rest. `pnt stats --filter vs=henry` is everyone's figures in
hands against him.

**The line.** `opener`, `pfa` (preflop aggressor), the pot types `limped` / `srp` /
`3bet_pot` / `4bet_pot`, and comparisons on `open_bb`, `raise_bb` (the player's own
preflop raise-to) and `bet_flop` / `bet_turn` / `bet_river` (first bet on that
street as a fraction of the pot).

**Preflop decision points**, one per bet level: `unopened` (had a decision with no
raise in front), `faced_open`, `faced_3bet_any`, `faced_4bet` and `faced_5bet`. What
they did there is `limp`, `called_open`, `4bet` or `5bet`, or spelled out as
`faced_open=fold` or `faced_4bet=call`. `faced_3bet` stays the opener's alone, since
fold-to-3-bet is defined on it. `limp` is the action, and `limped` is the pot. So
"what does he call a 3-bet with after cold-calling" is
`called_open,faced_3bet_any=call`. This is the vocabulary the live HUD uses to name
the spot a hand is in as it is played.

**Steals, squeezes and cold calls.** `first_in` is everyone before them folded.
`steal` is an open from first in from the CO, button or small blind (`steal_opp` the
chance), and `fold_to_steal` / `3bet_vs_steal` are a blind's answer to one nobody
called (`fold_to_steal_opp`). `squeeze` is a 3-bet over an open and at least one
caller (`squeeze_opp`, or `callers_before>=1` for the count), and `cold_call` a call
of an open with no blind and no limp in (`cold_call_opp`). Hands with a dead button
or a dead blind have no steal, since their seats are guesses.

**The table and the stacks.** `straddled` keeps the hands someone straddled,
`players_flop>=3` the multiway flops, `eff_bb<40` the short-stacked hands (effective
stack in big blinds at the deal) and `spr_flop<=3` the low stack-to-pot flops. A
hand with an unknown stack matches no `eff_bb` comparison. `won_pot` is "collected
from the pot" (a 7-2 bounty is not the pot), and `fold_street=turn` keeps the hands
they folded on that street.

**Bet sizes** come in five buckets: `blocker` (under ⅕ pot), `small` (⅕ to ½),
`medium` (½ to ¾), `large` (¾ to pot) and `overbet` (more than pot). PokerNow's ½,
¾ and pot buttons each start one. Use them as `cbet_flop=medium`, `cbet_turn=overbet`,
`bet_river=large`, or `faced_cbet_flop=small` for the c-bet a player was facing.
`faced_bet_river=large` is any bet or raise they faced, `raised_bet_flop=medium`
their own raise (measured after the call, so a pot raise is 1.0), and
`bet_answer_river=fold` what their bet got: `fold`, `call` or `raise`.

**Postflop responses** are flags per street: `folded_to_cbet_turn`,
`raised_cbet_flop`, and `donk_flop` (betting into the previous street's aggressor
before they act). Facing any bet, not only a c-bet, is a term too:
`faced_bet_river`, `folded_to_bet_river`, `called_bet_flop`, `raised_bet_turn`.
`aggressor_river` is the player who made the street's last bet or raise, and
`check_back_turn` (or `check_back` for any street) is the player whose check closed
a street that checked through. `check_raise_flop` is a check-raise, and
`check_raise_flop_opp` checked and then faced a bet.

**After a missed c-bet.** `delayed_cbet_turn` is the preflop raiser betting the turn
after the flop checked through; `probe_turn` is the other player betting into a
preflop raiser who checked the flop back, and `folded_to_probe_turn` the raiser
folding to it; `float_turn` is calling a bet on the flop and then betting the turn
when that bettor checks. Each has an `_opp` form (`probe_turn_opp`) and an
any-street form (`probe`). Delayed c-bets and probes are turn and river only; a
float can be on the flop, after calling a preflop raise.

**Action lines** spell out what a player did on a street, one letter per action:
`x` check, `b` bet, `c` call, `r` raise, `f` fold. `line_flop=xc` is a flop
check-call, `*` matches any run (`line_turn=x*` checked first), and `-` is "did not
act". `line=xc//b*` reads flop/turn/river at once, an empty street matching
anything, and `?` matches exactly one action. Put `!` in front of any flag to
negate it: `!cbet_flop`.

**The whole street**, everyone's actions in order, is `action_flop=`: the player's
own actions in capitals, everyone else's in lower case. Unlike `line_`, **case
matters**. `action_flop=xBrF` is "they checked to me, I bet, they check-raised, I
folded"; `action_flop=*Br*` is any flop where I bet and got raised. `action=` takes
flop/turn/river like `line=`. `faced_check_raise_flop` is "I bet and got
check-raised", and `faced_check_raise_flop=fold` what I did about it.

**All-ins**, preflop included: `jam` / `jam_river` (bet or raised all in),
`faced_jam` / `faced_jam_turn`, and `called_jam` / `called_jam_preflop`.

**The holding:** `hand=72` (both suits), `hand=72o`, `hand!=AA`, or `hand_pct>=60`
for a bottom-40% hand by the standard strength ranking. `made_flop=set` is what the
shown hand had made on the flop's board, `made_turn>=two_pair` that or better, and
`made=flush` the final board; `pair` and `draw` name the whole group, and `|` joins
several (`made_flop=set|full_house|flush`). So `made_flop>=set,line_flop=x*` is the
sets that checked the flop.

**Board texture:** `flop=ace_high`, `flop=monotone`, `flop=paired`,
`flop=connected`, `river!=flush_possible`, `board=twotone` and so on. The full tag
list is in [`SPEC.md`](../pnt/stats/SPEC.md).

## Range charts and sizing

What a player *had* in a spot, from the hands where their cards were shown:

```bash
pnt range henry --filter "opener,open_bb>=4,srp"          # the 13x13 chart
pnt range henry --filter "pfa,srp,cbet_flop,cbet_turn,bet_river=overbet" --by made
```

```
henry  filter: pfa,srp,cbet_flop,cbet_turn,bet_river=overbet
hands in this spot: 12   cards known: 7   coverage: 58.3%

class                       n    pct  won   net bb
--------------------------------------------------
straight                    2   28.6    2     61.5
two_pair                    2   28.6    1    -34.0
top_pair                    1   14.3    1     15.1
middle_pair                 2   28.6    1     20.9
```

Every row is one kind of hand, strongest first, with no `pair` row above them adding
them up again: a pair is the pair it was (`overpair`, `top_pair`, `pocket_pair`
for one under a board card, `middle_pair`, `bottom_pair`, `board_pair` when the
hole cards add nothing), three of a kind is a `set` or `trips` off a paired board,
and an unpaired hand is the draw it had on the flop or turn (`combo_draw`,
`flush_draw`, `open_ender`, `gutshot`) or `high_card`, so the high-card row is real
air. These are the names `made=` takes, and on the chart a row lists its hands as
the spot plus `made=<row>`.

**Coverage is the number to read first.** Cards are known only at showdown, so a
chart of "hands they 3-bet" is really "hands they 3-bet and showed down". The
bluffs that folded out are the missing part. To check someone's river bluffing,
open the chart on `jam_river` under **Made hands**, see how much is air, and then
read coverage: a jam everyone folds to is never shown, so the bluffs that worked
are missing.

What they bet each size *with* is its own view:

```bash
pnt sizing henry --street flop --kind cbet         # c-bets by size, plus the checks
pnt sizing henry --street turn --kind faced_cbet   # fold / call / raise per size faced
```

Every raise-size figure comes with its median, mode, mean, min and max, computed
over every hand in the spot (sizing needs no showdown).

### The chart page

[http://127.0.0.1:52000/chart](http://127.0.0.1:52000/chart) has the same views.
The 13x13 chart can be coloured three ways:

- by net won;
- by how often the player raised with each hand when they had a preflop decision
  (0–100%);
- by how their habitual raise size with that hand compares to their usual size in
  the spot. "Habitual" is the mean when a hand's sizes agree and the median when
  they do not, so a single tilt jam cannot repaint a cell.

The made-hand view shows what the shown hands had by the end, and **Sizing** shows
the sizing view. Under the tiles, the page shows how often the player c-bets, folds
to one, raises one and leads in the selected spot. The **Jammed** and **Called
jam** chip rows write the all-in terms for you, and the **Pot ≥** box and **vs**
picker write `pot>=` and `vs=`.

**Is their limp a trap?** The **Limped** chip row splits a player's limps that
someone raised behind into **limp-reraise**, **limp-call** and **limp-fold**
(`limp,faced_open=raise|call|fold`). Pick limp-reraise on the Preflop or Made
view to see the hands they showed doing it, and add **top 15% shown**
(`hand_pct<=15`) to count how many were big hands. A limp-reraise that everyone
folded to is never shown, so the shown range leans strong. The same rate is the
**LRR** column on `/stats`: limp-reraises over every limp that faced a raise.
When at least five of a player's limp-reraises were shown and 60% or more of them
were top-15% hands, they get the **LIMP-RERAISE = STRONG** tag.

**Results** opens with the red line and the blue line: the player's net big blinds
over time, split into the hands they took to showdown (blue) and every other hand
(red), which add up to the grey total. A blue line that climbs while the red one
falls is someone who wins with cards and gives pots up without them: bluff them
off hands and value-bet them thinly. A rising red line is someone who takes pots
away, so call them down lighter. A dashed line pays each all-in showdown by
equity, and the bars underneath are each session's result. Hover for any hand's
running totals, click a point to open that hand, click a bar to list that
session, or click red or blue in the legend to narrow the spot to those hands.
`pnt winnings <alias>` prints the totals and the session table, and
`GET /players/{alias}/winnings` returns every point.

**Results** ends with the give-up funnel. Of the flops the player saw, it shows how
many turns, rivers and showdowns they were still in for, and how many showdowns
they won. Under that is **WWSF** (won when saw flop: how often a flop ended with
them collecting the pot) and the part of it they won without a showdown. A second
table shows where they folded after the flop, each street split by whether they
had been the previous street's aggressor, as a share of the hands that reached
that street the same way. Low WWSF beside a normal WTSD means they give up too
much: keep betting at them. Click any row to narrow the spot to its hands and list
them. WWSF and W$WSF are also columns on the stats page, and
`GET /players/{alias}/funnel` returns the tables as JSON.

**Straddled pots.** The **Pot** chip row ends with **straddled**, which goes with
any pot type rather than replacing it (`srp,straddled` is a single-raised
straddled pot). Results follows the spot, so to see where a player wins and loses
in straddled pots, add `straddled` to the Spot: the red and blue lines and the
pot type, seat and position tables then cover those hands only. Swap it for
`!straddled` to compare against the rest.

Every cell, bar and size opens the list of hands behind it, and clicking a hand
replays it. Each row names who the hand was against and whether the player closed
the action (`IP` or `OOP` rather than a seat, since every postflop stat here is
measured against an aggressor and not against a seat). Hover a row for the full
opponent list and who c-bet on which street. Each row also shows the pot, and the
list can be ordered newest first or biggest first.

Player, spot, view, colour mode and theme all live in the URL
(`/chart?player=henry&filter=opener,srp&color=size&theme=dark`), which is how the
HUD embeds it. `GET /players/{alias}/range` returns the same data as JSON, with all
169 cells present in chart order.

## The live HUD

Open a PokerNow game and click the extension's toolbar icon (pin it first) to open
Chrome's side panel. It sits beside the game, so nothing covers the table. It only
opens on PokerNow game tabs, and it hides when you switch to another tab and comes
back when you return. Its ⏸ pauses capture, and its ⚙ holds the server URL, poll
interval and capture status.

**Floating instead.** Set ⚙ → HUD to *Floating on the page* to get the same HUD as a
box on the game page. Drag it by its header and resize it from its bottom-right
corner. It remembers where you left it. Its ✕ hides it, and the toolbar icon brings
it back. Switch back to *Side panel* in the same place.

**Flag the last hand.** Once a hand has finished, the header shows **🚩 #N** for
it, greyed out. Press it to flag that hand for the chart's **Manual review** tab
(see [Flagging hands yourself](#flagging-hands-yourself)), and it turns red. Press it
again to unflag. It always names the last hand to *finish*, so during a hand it
points at the one before.

**Compact.** The – in the header strips the HUD down to one table: a row per player
with the same figures (this session, then lifetime in grey) and no tags, spot lines
or chart. When floating, the box shrinks to fit the table. The + brings everything
back. Either mode stays as you left it.

**Player cards.** Everyone dealt into the latest hand gets a card with VPIP, PFR,
3-bet, fold to 3-bet, c-bet and WTSD, each shown twice: this session first, with
lifetime in grey beside it. A session figure turns blue when it sits 10 or more
points from lifetime on at least 10 chances, and lifetime falls outside the
session's 95% range, so a gap a small sample could give by chance stays white.
Hover a figure for both samples and each one's range: `45% of 20, ± 20.0 pts (95%:
25.8–65.8%)` says tonight's 45% could be anything from a quarter to two thirds.

**Tags** sit under each name: the archetype filled in, the exploits outlined and the
fun ones dashed. Hover a chip for the evidence and the count. Click it to open the
chart on exactly those hands, in the view that shows them.

**The embedded chart.** Click a card to embed that player's range chart under the
cards, opened on all their hands. It takes the panel's width (drag the panel's edge
to widen it), and its height is dragged at the corner and remembered. The spot,
board texture and view are the chart page's own controls. *open ↗* carries whatever
you have picked out into a full tab.

**Following the hand.** For every player, the HUD works out the spot they are in as
a filter: `position=BTN,unopened`, then `position=BTN,opener,faced_3bet` once they
open and get 3-bet, then
`position=BTN,opener,faced_3bet=call,3bet_pot,faced_cbet_flop=small` on the flop.
Each decision is added to the path, so the next spot branches from it.

The card shows the spot and what that player has *shown up with* there. After the
decision they just made, their shown hands split into value, marginal, draw and air
(`facing flop c-bet → call · value 50% · draw 25% · air 25% (4 of 9 shown)`), read
on the board as it stood when they acted, so a flop call that rivered a flush counts
as the draw it was. While they are still to act, the line has one such row per
decision, and the hover gives how often they fold, call or raise, with the counts.
The player to act is marked.

- **Widening.** A spot with no history is widened one step at a time until one has
  data: first to any position, then to the spot they arrived from (a 5-bet with
  nothing behind it shows their 4-bet spot), then to the pot type. A postflop spot
  starts out among flops with as many players (heads-up, or three or more), and its
  first step after any size is *any number of players*. A flop three or more saw
  puts "3-way" (or 4-way, and so on) at the start of the line. A widened spot
  is shown with **≈**, and the hover says how. A spot answered from earlier on the
  path reads *arrived with …*: the range they brought here, on this street's board,
  never a borrowed fold/call/raise mix. When even the widest spot is empty, the
  card says *no data*.
- **After a missed c-bet.** Delayed c-bet, probe, facing-a-probe and float spots
  are tracked spots of their own (`probe_turn_opp`, `float_river_opp`), so the card
  answers them from those hands. A player with no history of one at all is shown
  their last tracked spot instead, as *arrived with …*.
- **Board narrowing.** Postflop, each step is first tried narrowed to boards like
  this one (highest card, paired or not), and kept that way while at least *min
  shown hands to narrow by board* survive it (5 by default, in the settings).
- **Minimum sample.** The settings' *min hands for a live spot* raises the sample a
  spot needs before it is shown rather than widened.
- **Follow.** With **follow** on (the default), the embedded chart shows the player
  to act, in their spot, and moves with the action. Click a card to look at that
  player until the action next moves. The chart bar's 📌 keeps it on one player
  while the action goes round, and a second press lets it follow again. Editing the
  spot or the player inside the chart switches follow off, so nothing is snatched
  away mid-look.

The definitions are in [`SPEC.md`](../pnt/stats/SPEC.md), "Decision points
(nodes)" and "What they showed up with".

## Tags

```bash
pnt tags                                   # everyone, most hands first
pnt tags henry                             # one player, with why each tag fired
pnt tags henry --filter "players>=4"       # judged on the hands in a spot
```

Each player gets one archetype (FISH, MANIAC, STATION, LAG or NIT), judged against
what ordinary VPIP and PFR look like at each table size, since 70% VPIP is normal
heads-up and loose six-handed. A player none of those fit is named for what their
exploit tags say they do (STICKY, FIT OR FOLD, TRAPPER or GAMBLER), or TAG, PASSIVE
or BALANCED when nothing stands out. After the archetype come the exploit tags the
data supports, and then the fun ones.

- **A tag has to hold up without any one session.** If taking one game out makes it
  stop firing, that night made it, so it is listed apart as "one session only"
  instead of being treated as a habit.
- **Every tag carries its count** and the spot filter that puts those hands on the
  chart. A rule with too small a sample stays silent rather than guessing.
- **Tags built from shown hands say so**, because a river bet that reached showdown
  is one that got called; the bluffs that worked are the hands missing.

The rules, the thresholds, and the hands these games play on purpose (7-2, 9-2 and
K-2o are never evidence of a bad call) are in [`SPEC.md`](../pnt/stats/SPEC.md),
"Tags". `GET /players/{alias}/tags` returns the same as JSON.

### Your leaks

```bash
pnt leaks                # your top three leaks, then every stat outside its band
pnt leaks --all          # every stat judged, inside its band or not
```

The same rules turned on yourself. The stats page opens with a **Your leaks** panel
whenever a log names its owner (the logs you download yourself do). Twelve of your
stats (VPIP, PFR, 3-bet, ATS, fold to steal, flop c-bet, fold to flop c-bet, WTSD,
W$SD, WWSF, turn probe and flop float) are held against a band of ordinary values,
and your fold share against each size of bet is held against that size's
break-even. Each one outside its band gets a cost in bb per 100 hands and a
confidence, which is how much of its 95% range sits outside the band, and the top
three by cost × confidence are listed. Click one to open those hands on your chart.
The cost is where the money went, not proof of what a different decision would
have won. The bands are judgement values, not what this pool's winners do: a pool
of nine has too few winners to measure. The rules are in
[`SPEC.md`](../pnt/stats/SPEC.md), "Leak finder", and `GET /leaks` returns the same
as JSON.

### Shows without being asked

```bash
pnt shows henry          # what henry turned over after a fold or an uncontested pot
```

Some players show the bluff that got a fold, to needle the table; others show only
the big hand they laid down, to look tight. Open a player on the
[players page](http://127.0.0.1:52000/players.html) and **Shows without being asked**
lists every hand they showed after it ended, split by how it ended for them (folded,
won uncontested, or mucked at showdown), with each two-card show read as strong,
marginal, a draw or air on the board as it stood when they folded. One-card shows are
counted only. Click a hand's number to replay it on the chart. Any chart link can
do the same with `hand=<id>`, as in `/chart?player=henry&hand=1234`. These are the
hands they *chose* to show you, so read them as what they want you to believe, not
as their range. The range charts leave them out for the same
reason. A player whose shows are mostly air or draws gets a **SHOWS BLUFFS** fun tag,
and one whose shows are mostly strong gets **SHOWS NUTS**.

## Hand review

```bash
pnt review jayden                          # newest first, every flag
pnt review jayden --kind missed_bluff      # one flag
pnt review jayden --filter "3bet_pot"      # any spot filter the other pages take
pnt review jayden --unreviewed             # only what you have not marked off yet
pnt review jayden --noted                  # only the ones you wrote something about
```

Review flags three kinds of mistake and three kinds of bad beat:

| Flag | Meaning |
|---|---|
| **Missed bluff** | A pot of 15bb or more checked down on the river with every hand shown a board pair or worse. Nobody bet, and nobody had anything. |
| **Missed value** | Two hands good enough to play for stacks (top two pair or a set; top pair top kicker in a 3-bet pot at 100bb) on a board with no flush or straight available, where less than half the stacks went in. |
| **Failed bluff** | A postflop bet or raise with air that got called or raised in a hand you lost. The row carries the size, who called, their archetype and their showdown rate, so the evidence for "was it the sizing or the target" is right beside it. |
| **Suckout** | All in at 60% or better, and lost. |
| **Preflop cooler** | QQ+ or AK all in behind. |
| **Postflop cooler** | A stacks hand already behind on a dry board. |

A flag is a prompt to open the replay, not a verdict. Every flag that reads the
villain's cards can only see showdowns, so mucked hands are counted and reported
rather than dropped, and the page shows that coverage beside the count.

The page is the chart's **Hand review** and **Bad beats** views
([http://127.0.0.1:52000/chart?by=review](http://127.0.0.1:52000/chart?by=review)).
It has the same rows, ordered by recency, biggest pot or biggest swing, with each
flag switchable on or off and a click replaying any hand.
`GET /players/{alias}/review` returns the JSON. SPEC.md, "Hand review", pins every
rule and threshold.

### Marks and notes

A flag says a hand is worth a look. A mark says you have looked at it, and a note
says what you found. Each row on the page has a check box and a pencil. The pencil
opens a box under the row; Ctrl+Enter or clicking away saves it, and an emptied box
deletes the note. **Hide reviewed** clears the hands you are done with out of the
list. Both are also on the command line:

```bash
pnt reviewed pgl41zM3_CKphpnKM1DMIosUT 161    # mark it
pnt reviewed pgl41zM3_CKphpnKM1DMIosUT 161 --undo
pnt reviewed                                  # everything marked so far

pnt note pgl41zM3_CKphpnKM1DMIosUT 161 "turn barrel had no fold equity"
pnt note pgl41zM3_CKphpnKM1DMIosUT 161        # read it back
pnt note pgl41zM3_CKphpnKM1DMIosUT 161 --clear
pnt note                                      # every note so far
```

Notes show up under the flag's own line in `pnt review` and under the row on the
page, so the next time the hand comes up, your own reading of it is beside the
machine's. Marks and notes are on the *hand*, so a hand with two flags, or one that
shows up on two players' reviews, is marked once and noted once. They survive
`pnt rebuild`.

The two are independent. Unticking a hand you want to look at again keeps what you
wrote about it, and a note on a hand you have *not* finished with ("check the turn
sizing here") is the ordinary case. Writing a note again replaces it.

There are two kinds of note. A hand note, the ✎ beside a row, is about one hand: it
is on every list of the chart, including the hands you open by clicking a cell. A
[player note](#player-notes) is about the person, and sits under their name on the
players page, the chart and the HUD.

### Flagging hands yourself

The rules only catch some mistakes. When a hand feels wrong at the table, press
**🚩** in the HUD right after it ends. The chart's **Manual review** view
([http://127.0.0.1:52000/chart?by=manual](http://127.0.0.1:52000/chart?by=manual))
lists every hand you flagged that the player on show was dealt into, newest first,
with the same check box and pencil as the other lists. A hand flagged from the HUD
shows up a few seconds later, once the tracker has rebuilt it. Every row on the
other lists has a 🚩 too, so you can flag a hand from Hand review or Session as
well. Clear the 🚩 to take a hand off the list.

A flag is the third thing on a hand, next to the mark and the note, and it is
independent of both. Marking a flagged hand reviewed keeps it on Manual review, so
**Hide reviewed** works there the same way it does on the other lists. Flags
survive `pnt rebuild`. `GET /players/{alias}/flagged` returns the list,
`GET /flagged` returns every flag, and
`POST /games/{game_id}/hands/{hand_number}/flag` sets or clears one.

### Going through a whole session

Flags only catch some mistakes. The chart's **Session** view
([http://127.0.0.1:52000/chart?by=session](http://127.0.0.1:52000/chart?by=session))
lists every hand of one game in the order it was dealt, starting with your latest
game. Pick another game from the dropdown. A flagged hand carries its flag on the
row, and every row has the same check box and pencil, so you can mark or write on
any hand, flagged or not. **Played only** (on by default) leaves out the hands that
were just a preflop fold. The Spot box still applies, so `vs=luis` lists only that
session's hands against him. `GET /players/{alias}/games` lists a player's games,
and `GET /players/{alias}/hands?game=<id>` lists the rows with their marks and notes.

**Side by side** puts the list on the left and the replay on the right, where it
stays in place while you scroll. It works in Hand review, Manual review, Bad beats and Session. The
↑ and ↓ keys step to the previous and next hand and replay it. The setting is kept
in this browser. Below 900px wide, including the extension's side panel, the replay
goes back under the list.

## All-in EV

```bash
pnt allin                                  # every player: actual, adjusted, diff, in bb
pnt allin --filter "3bet_pot" --min-hands 10
```

Every all-in showdown where every live hand was shown gets a row per player. It
records their equity when the betting stopped (on the board dealt by the last
voluntary action), what that equity was worth across the main and side pots, and
what they actually collected.

- **Adjusted** is the net had every pot been paid out by equity.
- **Actual** is the same net every other page prints.
- **Diff** is the deck's contribution. Within a hand it sums to zero.

Hands where a live player mucked are counted as skipped rather than guessed at.
Preflop equities are sampled and marked `~`.

The page is [http://127.0.0.1:52000/allin](http://127.0.0.1:52000/allin). It shows
the same table in bb or chips, split by the street the betting stopped on. Clicking
a player draws their cumulative actual and adjusted lines over every all-in hand and
lists those hands with cards, board, equity and pot (newest, biggest pot or biggest
swing first), and any of them can be replayed. The Spot box takes every filter the
other pages do. `GET /allin` and `GET /players/{alias}/allin` return the JSON.

## Biggest pots

```bash
pnt pots                                   # over 2,000 chips in the last 7 days
pnt pots --days 1                          # just today
pnt pots --all --min-pot 10000             # the biggest ever recorded
pnt pots --player jayden                   # only hands you were dealt into
```

The pot is what actually sat in the middle. Uncalled bets are not in it (an
over-shove nobody matched never sat there), and neither are 7-2 bounties, so it is
the same figure every other page prints. A **chopped** pot names no winner rather
than crowning whoever came out a blind ahead. A hand whose log stopped mid-way is
listed and labelled, never silently dropped.

The page is [http://127.0.0.1:52000/pots.html](http://127.0.0.1:52000/pots.html).
It has a 24h / 7 day / 30 day / all switch, an adjustable threshold, and a chips/bb
toggle. Each row is drawn as a bar against the biggest pot in the window, so the
outliers stand out, and clicking a row replays the hand. `GET /pots` returns the
JSON. SPEC.md, "Biggest pots", pins the window and the pot.

## Publishing logs

A raw export names your hole cards on **every hand you were dealt into**: what you
folded, what you three-bet light with, what you checked back on the river.
Opponents get one line per showdown, but you get one line per hand:

```
"Your hand is 4♦, 9♥",2026-07-23T08:18:05.455Z,178479468545503
```

`pnt redact` writes copies you can publish. [`pnt/logs/`](../pnt/logs) is its
output, and it is what ships in the wheel:

```bash
pnt redact --out pnt/logs        # every log in the log folder -> the bundled corpus
pnt redact --audit pnt/logs      # verify what you are about to commit; exits 1 on a leak
```

It keeps your cards only on hands where you showed them anyway. Every stat is
unchanged, since hole cards feed range charts and not action frequencies. What you
lose is your own range coverage, which drops to the level you give an opponent:

```
pnt range wooooo          # original: cards known: 222   coverage: 100%
pnt range wooooo          # redacted: cards known:  54   coverage: 24.3%
```

Your originals are never written to. How the matching works, and why it cannot
leak, is in [architecture.md](architecture.md#redaction).
