# PokerNow Tracker

A persistent, queryable database of PokerNow hands keyed to stable player
identity, with per-player stats derived at read time. A live HUD is one consumer
of that database, not the product.

## Quick start

You need **Python 3.11 or newer**, **pipx**, and **Chrome**. If you have Python but
not pipx, `python -m pip install --user pipx` then `python -m pipx ensurepath`, and
open a new terminal so PATH takes effect. On Windows prefer the python.org
installer over the Microsoft Store build: the Store build sandboxes writes under
`%LOCALAPPDATA%`, which is the kind of thing that makes a background task look
like it started and then find none of its files.

```bash
pipx install git+https://github.com/Jaaaayden/pokernow-tracker
pnt setup                                    # database, logs, background server, extension path
```

`pnt setup` is the whole first run: it creates the database, imports any exports
it finds, installs the always-on server (Windows), and prints the folder to load
in Chrome. It is safe to re-run.

**That is the only command you need**, and there is nothing to start by hand
afterwards — on Windows the server is registered as a Task Scheduler job for your
user, so it comes back at every login and restarts itself if it crashes. The one
thing to remember: after you update the code, run `pnt service restart`, because a
running server keeps the old code. `pnt service status` says whether it is up and
which database it has open.

On macOS and Linux there is no equivalent step — `pnt setup` sets everything else
up and tells you to run `pnt serve` yourself, or to put it under launchd or systemd
if you want it always on.

Then open **<http://127.0.0.1:52000>**. That page says what is in the database and
links to everything else -- stats, range charts, and the players page below. The
rest of the commands are there when you want them:

```bash
pnt import                                   # every log in ~/Downloads/pokernow-logs
pnt import path/to/log.csv                   # or specific files, a folder, or a glob
pnt stats                                    # every player, most hands first
pnt alias list                               # the player names you can query
pnt positions genericpoker                   # one player, split by position
pnt where                                    # which database, log folder and extension
pnt extension                                # where to point Chrome's "Load unpacked"
pnt alias merge onlybluffs genericpoker      # or do it on the players page
pnt alias split FQN9hzhzP_ --alias henry     # undo a merge, or separate two people
pnt serve                                    # http://127.0.0.1:52000
pnt service install                          # or: the same server, always on (Windows)
```

`pipx` gives `pnt` its own virtualenv and puts it on PATH, so nothing has to be
activated. To work on the code instead, clone it and `pip install -e ".[dev]"` --
the server dependencies are part of the base install, not an extra.

Keep every PokerNow export in one folder and a bare `pnt import` picks up whatever
is new. That folder is `~/Downloads/pokernow-logs` unless you say otherwise — pass
paths, a folder or a glob straight to `pnt import`, pass `--log-dir`, or set
`PNT_LOG_DIR` to change it for good. `pnt where` prints the one in effect.

Re-importing is free: duplicate entries are ignored, so the habit is "drop the
export in the folder, run `pnt import`".

Commands that take a player take an **alias** — a canonical name from
`pnt alias list`, not a PokerNow ID and not a seat. Aliases start as the display
name first seen for an ID and can be renamed (`pnt alias rename`) or merged
(below).

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

Table sizes are **pooled** by default; `--split-by-size` separates 3-handed from
heads-up instead. Pooling is the default deliberately — storage keeps `n_dealt_in`
raw, so you can always split a pooled stat later and never unpool a fragmented one.
Position itself is computed at query time from `seats_from_button` and
`n_dealt_in`, never stored: seat 4 is UTG on one hand and the cutoff on the next
once two players leave.

Hands with a dead button or a dead blind are left out of this view only — those are
the two cases where the position *label* is best-effort. They still count in
`pnt stats`, since the actions are certain even when the label is not.

### Querying a spot

The Holdem-Manager move — pick a spot, then look at behaviour inside it:

```bash
pnt stats --filter 3bet                    # only hands where they 3-bet preflop
pnt stats --filter "cbet_flop,position=BTN"
pnt stats --filter "faced_cbet_flop,players>=3"
```

This works only because actions are stored raw with street and sequence. A
pre-aggregated schema cannot answer "hands that reached this point" at all.

### Ranges and lines

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

A line is just a longer filter. The new terms are `opener`, `pfa` (preflop
aggressor), `limped` / `srp` / `3bet_pot` / `4bet_pot`, and comparisons on
`open_bb`, `raise_bb` (the player's own preflop raise-to) and `bet_flop` /
`bet_turn` / `bet_river` (first bet on that street as a fraction of the pot).

Bet sizes also come in four buckets: `small` (under ½ pot), `medium` (½ to ¾),
`large` (¾ to pot) and `overbet` (more than pot). PokerNow's ½, ¾ and pot buttons
each start one. Use them as `cbet_flop=medium`, `cbet_turn=overbet`,
`bet_river=large`, or `faced_cbet_flop=small` for the c-bet a player was facing.
Postflop responses are flags per street: `folded_to_cbet_turn`,
`raised_cbet_flop` and `donk_flop` (betting into the previous street's aggressor
before they act).

What they bet each size *with* is its own view:

```bash
pnt sizing henry --street flop --kind cbet         # c-bets by size, plus the checks
pnt sizing henry --street turn --kind faced_cbet   # fold / call / raise per size faced
```

The chart page has the same view under **Sizing**. Every cell, bar and size on
the page opens the list of hands behind it, and clicking a hand replays it. Each
row in that list names who the hand was against and whether the player closed the
action — `IP` or `OOP` rather than a seat, since every postflop stat here is
measured against an aggressor and not against a seat. Hover a row for the full
opponent list and who c-bet on which street.

Board texture is a filter too: `flop=ace_high`, `flop=monotone`, `flop=paired`,
`flop=connected`, `river!=flush_possible`, `board=twotone` and so on — the full tag
list is in [`SPEC.md`](pnt/stats/SPEC.md). Every raise-size figure comes with
median, mode, mean, min and max, computed over every hand in the spot (sizing
needs no showdown), and the chart colours each hand by how far its size sits from
that player's usual size in the spot.

**Coverage is the number to read first.** Cards are known only at showdown, so a
chart of "hands they 3-bet" is really "hands they 3-bet and showed down"; the
bluffs that folded out are the missing part. `GET /players/{alias}/range` returns
the same data as JSON with every one of the 169 cells present, in chart order.

The same views as a page: `pnt serve`, then open
[http://127.0.0.1:52000/chart](http://127.0.0.1:52000/chart). The 13x13 chart can be
coloured by net won, by how often the player raised with each hand when they had
a preflop decision (0–100%), or by how their habitual raise size with it compares
to their usual size in the spot; the made-hand view shows what the shown hands had
by the end. "Habitual" is the mean when a hand's sizes agree and the median when
they do not, so a single tilt jam cannot repaint a cell — see
[`SPEC.md`](pnt/stats/SPEC.md).
Player, spot, view, colour mode and theme all live in the URL
(`/chart?player=henry&filter=opener,srp&color=size&theme=dark`), which is what the
HUD will embed once live capture exists. Under the tiles it also shows how often
that player c-bets, folds to one, raises one and leads, in whatever spot is
selected.

The front door is [http://127.0.0.1:52000](http://127.0.0.1:52000) — hands, players,
log lines and parse misses, links to every page, and the handful of commands worth
knowing when something looks wrong.

Every player at once is [http://127.0.0.1:52000/stats](http://127.0.0.1:52000/stats):
a browser gets a sortable table, while the HUD, curl and your scripts get the same
figures as JSON from the same URL (the page alone is at `/stats.html`). Pick a
street to swap the postflop columns, toggle the c-bet size mix, and click a player
to open their range chart in the spot you are looking at.

### Identity

PokerNow IDs are stable per browser and survive renames and quit/rejoin; the same
human on a second device gets a different ID.

```bash
pnt alias list
pnt alias merge "onlybluffs" "genericpoker"   # one person, two devices
pnt alias split "FQN9hzhzP_" --alias henry    # the inverse: undo a merge, or part two people
```

Merging is a single UPDATE, and nothing is recomputed — precisely because no
statistic is materialized.

The same thing with the evidence in front of you is
**[http://127.0.0.1:52000/players](http://127.0.0.1:52000/players)**: every person,
the PokerNow IDs behind them, and every name each ID has shown. That last column is
the point — the names are how you recognise someone, and they are the reason this
cannot be automated:

```
harry    <-  bread, Woolball (har), wool, wool ball, fish
charles  <-  chugnuts, The Great Wall, straight teeth, SplayWash
```

No string comparison finds those. Only someone who was at the table knows, so the
page lays out the IDs, names, hand counts and dates, shows exactly what a merge
will move *before* you confirm it, and then offers an undo.

That undo is why `POST /aliases/merge` returns the IDs it moved rather than a count
of them: the merge deletes the source player row, so that list is the only record
of what was behind it. `POST /aliases/split` takes it back. A merge followed by its
undo restores every number exactly, which `test_identity_api.py` asserts.

---

## What the numbers mean

`pnt stats` prints `--`, not `0`, when a denominator is empty. A player with no
3-bet opportunities has an *unknown* 3-bet percentage, and showing `0%` would be a
claim the data does not support.

Other PokerNow HUDs show `--` everywhere for a different and worse reason: they
pre-segment by table size in *storage*, which fragments every denominator past the
point of repair. Here that segmentation happens at query time instead — see
[Positional breakdown](#positional-breakdown).

Every definition is pinned in **[`pnt/stats/SPEC.md`](pnt/stats/SPEC.md)** —
numerator, denominator, exclusions, and the judgement calls. That file is the
artifact; `derive.py` mirrors it and is a bug if they disagree.

---

## Documentation

| File | Contents |
|---|---|
| [`docs/findings.md`](docs/findings.md) | The log format: identity, ordering, the cumulative-amount rule, complete line vocabulary, traps, and the live-capture endpoint |
| [`pnt/stats/SPEC.md`](pnt/stats/SPEC.md) | Stat definitions, line and sizing facts, range views |
| [`tests/fixtures/README.md`](tests/fixtures/README.md) | What each fixture log exercises |

---

## Three things worth knowing before you touch the parser

1. **`bets N`, `raises to N`, `calls N` — and the live forced posts `posts a
   straddle of N` and `posts a missed big blind of N` — are all cumulative for the
   street**, not incremental. Get this wrong and every pot, net-won figure and
   bb/100 is wrong while still looking plausible. Guarded by a conservation law
   over every hand in every fixture.

   The forced posts are the nastier half. An additive straddle over-counts the pot
   by exactly the small blind, and then *hides*: the straddler's next `raises to N`
   re-derives from the street total and cancels the error. It only survives in
   hands where they never act again, so it surfaced in 6 fold-arounds out of 34
   straddles — invisible in aggregate, wrong in the ledger.

   A forced post can also be **all in**: a stack shorter than the blind it owes
   posts what it has, and the log says so on the same line — `posts a big blind of
   1 and go all in`, the same suffix `bets` and `raises to` carry. The rule must
   accept it, because an unmatched post is not a missing label but a missing
   *blind*: those chips never reach the pot and the hand records no blind post at
   all. Pinned by `test_allin_posts.py`.
2. **The roster is the `Player stacks:` line** — the dealt-in roster — never join
   events and never "who acted". Getting it wrong is invisible: every rate comes
   out quietly too low for exactly the players who sit out most.
3. **Unrecognized lines are recorded, never dropped.** `pnt misses` shows them.
   An empty table is the claim that the parse was total.

4. **A log can end mid-hand.** That hand carries `complete = False`, stored on the
   row: its chips are half-recorded, so it is excluded from the conservation law
   rather than counted as a mismatch, and from bb/100 rather than booked as a loss
   that never happened. Everything else about it is real — the folds, bets and
   showdowns all happened — so it still counts for every other stat. Re-importing
   the finished export repairs it.

---

## Testing

```bash
pytest -q
```

The suite is organized around invariants rather than examples:

- `test_amounts.py` — chips in equal chips out, every hand
- `test_parser.py` — the big-blind poster lands on the big-blind slot, every hand;
  two independent position derivations agree on all 548 regular hands
- `test_amounts.py` also pins the two forced-post cases by hand: a straddle that
  restates the street total, and a missed big blind that absorbs the small blind
  posted with it
- `test_idempotent.py` — **the success criterion as a test**: re-importing changes
  no stat; a partial capture plus a later full import converges on the same
  numbers as a clean import; shuffled out-of-order ingest converges too
- `test_stats.py` — derived stats checked against a hand-worked manual count of 20
  hands, including the exact set of BB walks that must be excluded
- `test_identity.py` — IDs survive churn, names do not, merges are cheap
- `test_caching.py` — reading less, and not deriving twice, change no number. One
  player's figures read only that player's hands; the unfiltered report is memoized
  against a counter bumped inside the same transaction as every change that could
  invalidate it, so a hit is provably current
- `test_incomplete_hands.py` — a truncated hand leaves bb/100 alone and counts
  everywhere else
- `test_concurrency.py` — concurrent writers queue instead of failing. Two tabs on
  one table, or `pnt import` while the server is up, put two writers on the file;
  a deferred transaction that reads before it writes cannot upgrade, and SQLite
  refuses it *without* consulting `busy_timeout`. Every write goes through
  `writing()`, which takes the lock up front with `BEGIN IMMEDIATE`

---

## Live capture (Phase 3)

`pnt/extension/` is an unpacked Chrome extension (Manifest V3). On a
`pokernow.com/games/…` page (or the older `pokernow.club` address) it:

1. polls the game's log endpoint with the page's own session cookie —
   `GET /games/{gameId}/log?after_at=…&before_at=…`, the one `PokerNowGrabber`
   uses, which the captcha does not gate;
2. normalizes the response into `{entry, at, order}` — **the same three fields as
   the CSV export** — and posts it to `POST /ingest`. Live capture and backfill
   feed one parser with identical input, so they cannot disagree, and overlapping
   fetches are free because `/ingest` dedupes on `(game_id, order)`;
3. draws a draggable overlay listing everyone dealt into the latest hand, keyed by
   PokerNow ID via `GET /hud/{gameId}`, with lifetime VPIP / PFR / 3-bet / fold to
   3-bet / c-bet / WTSD. Click a row to embed that player's range chart straight
   from the local server, opened on their single-raised pots. The spot, board
   texture and view are the chart page's own controls, so the overlay adds none
   of its own to fall out of step with them; *open ↗* carries whatever you have
   picked in there out into a full tab.

No manual seat mapping is needed: the log names every player as `Name @ ID`, and
the alias table already joins one person's devices.

### Install

```bash
pnt setup
```

Then `chrome://extensions` -> *Developer mode* -> *Load unpacked* -> pick the
folder `pnt extension` prints. It lives inside the package, so an installed copy
has one without a checkout; `pnt extension --open` reveals it in Explorer. Open a
PokerNow game; the panel appears top-right. The toolbar popup shows the server
URL, poll interval, and capture status.

`pnt serve` in a terminal still works for a one-off session.

Why one command rather than the steps in order: `pnt service install` refuses a
database that does not exist, and it is right to -- a missing file would be
created empty and the HUD would then quietly show nothing. But until the first
import nothing had created one, so a fresh install ran that command, got an
error, and had no obvious next move.

### Background server

`pnt service install` registers a Task Scheduler task for your Windows user. The
server then starts hidden at every login and restarts itself after a crash, with no
terminal and no admin rights.

| Command | |
|---|---|
| `pnt service status` | Task state, whether the server answers, and which database it has open |
| `pnt service log` | The last lines of `~\.pnt\server.log` |
| `pnt service restart` | **Run after pulling code changes** — a running server keeps the old code |
| `pnt service stop` / `start` | Stop until the next login, or start again |
| `pnt service uninstall` | Stop it and remove the task; the database is untouched |

Why it is built the way it is:

- **`--db` is resolved to a full path at install.** The task does not start in your
  project folder, and a missing database file is created empty rather than
  reported — so a relative path would give a server that answers and shows zero
  hands. `install` refuses a path that does not exist.
- **A terminal `pnt serve` wins.** If the port is already taken, the task waits
  instead of crash-looping, and takes over once you close the terminal.
- **Two Windows defaults would kill it**: tasks are ended after 72 hours, and
  whenever a laptop goes on battery. Both are turned off.
- **The port is 52000, not 8000.** 8000 is the busiest port on a developer's
  machine, and the clash is quiet in both directions: this server holding it makes
  your other server fail to bind, and your other server holding it makes the task
  wait politely forever while the HUD reports the tracker unreachable. 52000 sits
  in the IANA dynamic range, which is never assigned to a registered service.
  `--port N` changes it — set the same address in the extension's popup.
- **Re-installing ends the running instance first.** The task is registered
  `IgnoreNew`, so a start request is *silently* ignored while an instance is alive:
  without ending it, changing `--db` or `--port` rewrote the definition, reported
  success, and left the old server running on the old settings.
- **Crashes restart in-process**, after 1 s and doubling up to 60 s; a run longer
  than a minute resets the delay. Task Scheduler's own restart is only a backup.
- **Idle cost** is about 75 MB of memory (a 62 MB server behind an 11 MB venv
  launcher) and no measurable CPU. The server only works when the extension posts.
  Stopping or restarting the task takes the launcher's child down with it; nothing
  is left holding the port.

### How capture reads the log

PokerNow's `/log` endpoint was checked against a live table on 2026-09-11
([`docs/findings.md`](docs/findings.md) §8). It returns
`{logs: [{at, created_at, msg}]}`, newest first, 50 lines per request, and
`created_at` is exactly the CSV export's `order` — so a line captured live and the
same line imported from a CSV later land on one row.

Its `after_at` only *filters*: it returns the newest 50 lines above the value, never
the next 50. So the extension pages backwards with `before_at` until it reaches
lines already stored ([`pnt/extension/pager.js`](pnt/extension/pager.js)), pausing 3 s
between pages because PokerNow answers bursts with HTTP 429. The first load of a
long game takes several minutes to walk its history; the HUD fills in as it goes,
and the popup's **history** row says when it is complete.

Everything that knows the response shape is in
[`pnt/extension/normalize.js`](pnt/extension/normalize.js). If PokerNow changes it, the
popup says **UNRECOGNIZED** and the page console prints the first item.

```bash
node --test pnt/extension/normalize.test.mjs pnt/extension/pager.test.mjs
```

The websocket trigger (`gC` / `gameResult`) is deliberately not used: a 5-second
poll is fast enough for a HUD and survives a PokerNow socket change.
