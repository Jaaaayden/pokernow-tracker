# PokerNow Tracker

A persistent, queryable database of PokerNow hands keyed to stable player
identity, with per-player stats derived at read time. A live HUD is one consumer
of that database, not the product.

**Status**: Phases 0–2 complete — parser, schema, importer, CLI, stat engine and
local API. Phases 3–5 (live capture, overlay HUD, refinement) are not built.

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
pnt serve                                    # http://127.0.0.1:8000/docs
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
| [`pnt/stats/SPEC.md`](pnt/stats/SPEC.md) | Stat definitions |
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

## Next: Phase 3 (live capture)

The groundwork is done — `POST /ingest` takes raw log entries and is idempotent, so
the capture contract is already fixed and tested.

The captcha gates only the "download full log" button, not the endpoint
`PokerNowGrabber` uses mid-game:

```
GET https://www.pokernow.club/games/{gameId}/log?after_at={ms}&before_at={ms}
```

A content script on `pokernow.club` sends the `npt` cookie automatically. Poll that
endpoint as the primary path and use the websocket (`gC` / `gameResult`) only as a
low-latency trigger, so a PokerNow socket change degrades to slower capture rather
than none.

Because live capture and backfill would then consume **byte-identical input through
one parser**, they cannot disagree — which is the whole point.

Verify the `/log` JSON envelope in DevTools first; it is the one assumption not yet
confirmed against a live table.
