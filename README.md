# PokerNow Tracker

A persistent, queryable database of PokerNow hands keyed to stable player
identity, with per-player stats derived at read time. A live HUD is one consumer
of that database, not the product.

**Status**: Phases 0–4 built — parser, schema, importer, CLI, stat engine, local
API, range charts, and a browser extension for live capture with an overlay HUD.
The extension has not yet been run against a live table (see
[Live capture](#live-capture-phase-3)).

```
2,761 hands · 50,202 log entries · 0 parse misses · 0 pot mismatches · 88 tests passing
```

---

## The design decision

**Store raw actions. Derive every stat at read time.** No counter is persisted
anywhere.

Counters look cheaper and are a trap: one missed hand corrupts a number
permanently, with no way to detect or repair it. With raw actions, a re-import
fixes any gap, and a stat invented next month computes retroactively across all
history.

Three layers, each rebuildable from the one above:

| Layer | Contents | Rebuildable? |
|---|---|---|
| `raw_entries` | Every log line, exactly as PokerNow emitted it | Immutable truth |
| `hands` / `hand_players` / `actions` | Parsed structure | Yes — `pnt rebuild` |
| Stats | SQL + Python at query time | Always fresh |

Keeping `raw_entries` is what makes "a re-import repairs any gap" true rather than
aspirational: improving the parser next month re-derives all history **without
needing the original CSVs again**.

---

## Quick start

```bash
pip install -e ".[server,dev]"

pnt import                                   # every log in ~/Downloads/pokernow-logs
pnt import path/to/log.csv                   # or specific files, a folder, or a glob
pnt stats                                    # every player, most hands first
pnt alias list                               # the player names you can query
pnt positions genericpoker                   # one player, split by position
pnt serve                                    # http://127.0.0.1:8000/chart
```

Keep every PokerNow export in one folder — `~/Downloads/pokernow-logs` by
default, or set `PNT_LOG_DIR` to point somewhere else — and a bare `pnt import``
picks up whatever is new. Re-importing is free: duplicate entries are ignored, so
the habit is "drop the export in the folder, run `pnt import`".

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
pnt range henry --filter "pfa,srp,cbet_flop,cbet_turn,bet_river>=1" --by made
```

```
henry  filter: pfa,srp,cbet_flop,cbet_turn,bet_river
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
`bet_turn` / `bet_river` (first bet on that street as a fraction of the pot, so
`>=1` is an overbet).

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
[http://127.0.0.1:8000/chart](http://127.0.0.1:8000/chart). The 13x13 chart can be
coloured by net won, by how often the player raised with each hand when they had
a preflop decision (0–100%), or by how their habitual raise size with it compares
to their usual size in the spot; the made-hand view shows what the shown hands had
by the end. "Habitual" is the mean when a hand's sizes agree and the median when
they do not, so a single tilt jam cannot repaint a cell — see
[`SPEC.md`](pnt/stats/SPEC.md).
Player, spot, view, colour mode and theme all live in the URL
(`/chart?player=henry&filter=opener,srp&color=size&theme=dark`), which is what the
HUD will embed once live capture exists.

### Identity

PokerNow IDs are stable per browser and survive renames and quit/rejoin; the same
human on a second device gets a different ID.

```bash
pnt alias list
pnt alias merge "onlybluffs" "genericpoker"   # one person, two devices
```

Merging is a single UPDATE, and nothing is recomputed — precisely because no
statistic is materialized.

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
2. **The roster is the `Player stacks:` line** — the dealt-in roster — never join
   events and never "who acted". Getting it wrong is invisible: every rate comes
   out quietly too low for exactly the players who sit out most.
3. **Unrecognized lines are recorded, never dropped.** `pnt misses` shows them.
   An empty table is the claim that the parse was total.

4. **A log can end mid-hand.** That hand carries `complete = False`: its chips are
   half-recorded, so it is excluded from the conservation law rather than counted
   as a mismatch. Re-importing the finished export repairs it.

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

---

## Live capture (Phase 3)

`extension/` is an unpacked Chrome extension (Manifest V3). On a
`pokernow.club/games/…` page it:

1. polls the game's log endpoint with the page's own session cookie —
   `GET /games/{gameId}/log?after_at=…&before_at=…`, the one `PokerNowGrabber`
   uses, which the captcha does not gate;
2. normalizes the response into `{entry, at, order}` — **the same three fields as
   the CSV export** — and posts it to `POST /ingest`. Live capture and backfill
   feed one parser with identical input, so they cannot disagree, and overlapping
   fetches are free because `/ingest` dedupes on `(game_id, order)`;
3. draws a draggable overlay listing everyone dealt into the latest hand, keyed by
   PokerNow ID via `GET /hud/{gameId}`, with lifetime VPIP / PFR / 3-bet / fold to
   3-bet / c-bet / WTSD. Click a row to embed that player's range chart, with a
   spot selector, straight from the local server.

No manual seat mapping is needed: the log names every player as `Name @ ID`, and
the alias table already joins one person's devices.

### Install

```bash
pnt serve                       # leave running; the extension talks to :8000
```

Then `chrome://extensions` → *Developer mode* → *Load unpacked* → pick
`extension/`. Open a PokerNow game; the panel appears top-right. The toolbar
popup shows the server URL, poll interval, and capture status.

### The one unverified assumption

The JSON shape of `/log` was never captured from a live table
([`docs/findings.md`](docs/findings.md) §8). Everything that depends on it lives
in [`extension/normalize.js`](extension/normalize.js), which accepts a bare array
or `{logs|log|entries|data: [...]}`, item text under `entry|msg|message|text`,
time under `at|createdAt|created_at|time|ts`, and `order` when present — and
rebuilds the CSV's `epoch_ms × 100 + seq` formula when it is not. If the shape is
something else, the popup says **UNRECOGNIZED** and the page console prints the
first item; paste that into an issue and the fix is one line in that file.

```bash
node --test extension/normalize.test.mjs
```

The websocket trigger (`gC` / `gameResult`) is deliberately not used: a 5-second
poll is fast enough for a HUD and survives a PokerNow socket change.
