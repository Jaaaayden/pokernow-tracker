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

**Bet sizes** come in four buckets: `small` (under ½ pot), `medium` (½ to ¾),
`large` (¾ to pot) and `overbet` (more than pot). PokerNow's ½, ¾ and pot buttons
each start one. Use them as `cbet_flop=medium`, `cbet_turn=overbet`,
`bet_river=large`, or `faced_cbet_flop=small` for the c-bet a player was facing.

**Postflop responses** are flags per street: `folded_to_cbet_turn`,
`raised_cbet_flop`, and `donk_flop` (betting into the previous street's aggressor
before they act). Facing any bet, not only a c-bet, is a term too:
`faced_bet_river`, `folded_to_bet_river`, `called_bet_flop`, `raised_bet_turn`.
`aggressor_river` is the player who made the street's last bet or raise, and
`check_back_turn` (or `check_back` for any street) is the player whose check closed
a street that checked through.

**All-ins**, preflop included: `jam` / `jam_river` (bet or raised all in),
`faced_jam` / `faced_jam_turn`, and `called_jam` / `called_jam_preflop`.

**The holding:** `hand=72` (both suits), `hand=72o`, `hand!=AA`, or `hand_pct>=60`
for a bottom-40% hand by the standard strength ranking.

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
pair                        3   42.9    2     36.0
  middle_pair               2   28.6    1     20.9
  top_pair                  1   14.3    1     15.1
```

Unpaired hands split into `draw` (a flush or straight draw on the flop or turn, with
the kind of draw as the detail) and `high_card`, so the high-card row is real air.

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
Chrome's side panel. It sits beside the game, so nothing covers the table, and it
shows whichever tab is active in that window. Its ⏸ pauses capture, and its ⚙
holds the server URL, poll interval and capture status.

**Player cards.** Everyone dealt into the latest hand gets a card with VPIP, PFR,
3-bet, fold to 3-bet, c-bet and WTSD, each shown twice: this session first, with
lifetime in grey beside it. A session figure turns blue when it sits 10 or more
points from lifetime on at least 10 chances. Hover a figure for both samples.

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
  nothing behind it shows their 4-bet spot), then to the pot type. A widened spot
  is shown with **≈**, and the hover says how. A spot answered from earlier on the
  path reads *arrived with …*: the range they brought here, on this street's board,
  never a borrowed fold/call/raise mix. When even the widest spot is empty, the
  card says *no data*.
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
