# Architecture

How PokerNow Tracker works on the inside, and why it is built the way it is. For
what each feature does and how to use it, see the [README](../README.md) and the
[guide](guide.md). For the log format itself, see [findings.md](findings.md).

## The design principle

The product is a persistent, queryable database of PokerNow hands, keyed to stable
player identity, with every per-player stat derived at read time. The live HUD is
one consumer of that database, not the product.

Actions are stored raw, with their street and sequence. Nothing is pre-aggregated.
That is what makes spot queries possible at all: a pre-aggregated schema cannot
answer "hands that reached this point", while a raw one can filter to any line and
then measure behaviour inside it.

It also explains three other properties:

- **Merging two players is a single UPDATE**, and nothing is recomputed, because
  no statistic is materialized.
- **Position is computed at query time** from `seats_from_button` and
  `n_dealt_in` and is never stored. Seat 4 is UTG on one hand and the cutoff on the
  next once two players leave.
- **Table sizes are pooled by default.** Storage keeps `n_dealt_in` raw, so a
  pooled stat can always be split later (`--split-by-size`), while a fragmented one
  can never be unpooled. Other PokerNow HUDs pre-segment by table size in
  *storage*, which fragments every denominator past the point of repair, so they
  show `--` everywhere. Here `--` means only what it says: the denominator is
  empty, and the rate is unknown rather than 0%.

Every stat definition (numerator, denominator, exclusions and judgement calls) is
pinned in [`pnt/stats/SPEC.md`](../pnt/stats/SPEC.md). That file is the artifact;
`derive.py` mirrors it, and any disagreement between them is a bug.

### What is stored rather than derived

- **`equity_cache`** is the one derived table. All-in equities are computed once
  and kept there, and it is safe to drop. The first `pnt allin` or page load after
  an import takes about half a second per preflop all-in; everything after that is
  instant. Flop, turn and river all-ins are enumerated exactly. Preflop all-ins are
  sampled (50,000 seeded deals, marked `~`).
- **Review marks and notes** are the only judgements that are stored rather than
  derived on every request, because nothing in a log could ever re-derive them.
  They live on the *hand*, so a hand with two flags, or one that appears on two
  players' reviews, is marked once and noted once. They are keyed on the hand's own
  number within its game rather than on `hand_id`, which a rebuild reassigns, so
  `pnt rebuild` leaves them alone.
- **The alias table** is the one thing a re-import cannot rebuild, which is why
  it is exported to [`pnt/logs/aliases.csv`](../pnt/logs/aliases.csv) and kept in
  the repo.

### Identity merges and their undo

`POST /aliases/merge` returns the IDs it moved rather than a count, because the
merge deletes the source player row: that list is the only record of what was
behind it. `POST /aliases/split` takes it back. A merge followed by its undo
restores every number exactly, which `test_identity_api.py` asserts.

Merging cannot be automated. The names one ID has shown (`harry <- bread, Woolball
(har), wool, wool ball, fish`) are not related by any string comparison, so the
players page shows the evidence and leaves the decision to a person.

## Parser: what to know before you touch it

1. **`bets N`, `raises to N`, `calls N`, and the live forced posts `posts a
   straddle of N` and `posts a missed big blind of N`, are all cumulative for the
   street**, not incremental. Get this wrong and every pot, net-won figure and
   bb/100 is wrong while still looking plausible. A conservation law over every
   hand in every fixture guards it.

   The forced posts are the nastier half. An additive straddle over-counts the pot
   by exactly the small blind and then *hides*: the straddler's next `raises to N`
   re-derives from the street total and cancels the error. The error only survives
   in hands where the straddler never acts again, so it surfaced in 6 fold-arounds
   out of 34 straddles. It was invisible in aggregate and wrong in the ledger.

   A forced post can also be **all in**. A stack shorter than the blind it owes
   posts what it has, and the log says so on the same line: `posts a big blind of 1
   and go all in`, the same suffix `bets` and `raises to` carry. The rule must
   accept it, because an unmatched post is not a missing label but a missing
   *blind*: those chips never reach the pot and the hand records no blind post at
   all. `test_allin_posts.py` pins it.
2. **The roster is the `Player stacks:` line**, meaning the dealt-in roster. It is
   never join events and never "who acted". Getting it wrong is invisible: every
   rate comes out quietly too low for exactly the players who sit out most.
3. **Unrecognized lines are recorded, never dropped.** `pnt misses` shows them. An
   empty table is the claim that the parse was total.
4. **A log can end mid-hand.** That hand carries `complete = False`, stored on the
   row. Its chips are half-recorded, so it is excluded from the conservation law
   rather than counted as a mismatch, and from bb/100 rather than booked as a loss
   that never happened. Everything else about it is real (the folds, bets and
   showdowns all happened), so it still counts for every other stat. Re-importing
   the finished export repairs it.

Hands with a dead button or a dead blind are left out of the positional view only,
since those are the two cases where the position *label* is best-effort. They still
count in `pnt stats`, because the actions are certain even when the label is not.

## Live capture

`pnt/extension/` is an unpacked Chrome extension (Manifest V3). On a
`pokernow.com/games/…` page (or the older `pokernow.club` address) it does four
things.

1. **Polls the game's log endpoint** with the page's own session cookie:
   `GET /games/{gameId}/log?after_at=…&before_at=…`. This is the endpoint
   `PokerNowGrabber` uses, and the captcha does not gate it.
2. **Normalizes the response** into `{entry, at, order}`, the same three fields as
   the CSV export, and posts it to `POST /ingest`. Live capture and backfill feed
   one parser with identical input, so they cannot disagree. Overlapping fetches
   are free because `/ingest` dedupes on `(game_id, order)`.
3. **Shows the HUD** in Chrome's side panel, keyed by PokerNow ID via
   `GET /hud/{gameId}`. No manual seat mapping is needed: the log names every
   player as `Name @ ID`, and the alias table already joins one person's devices.
4. **Follows the hand** by calling `GET /live/{gameId}` on every poll that brings
   lines. That endpoint parses the hand in progress straight from the raw lines,
   without a rebuild, and works out each player's spot as a filter. Because the live
   view reads raw lines, the whole-game rebuild that refreshes the roster and stats
   waits for `-- ending hand --` (with a one-minute safety net) instead of running on
   every poll. The poll reads `/live` before any rebuild or HUD refresh, so the live
   view never waits behind them.

### How capture reads the log

PokerNow's `/log` endpoint was checked against a live table on 2026-09-11
([findings.md](findings.md) §8). It returns `{logs: [{at, created_at, msg}]}`,
newest first, 50 lines per request. `created_at` is exactly the CSV export's
`order`, so a line captured live and the same line imported from a CSV later land
on one row.

Its `after_at` only *filters*: it returns the newest 50 lines above the value,
never the next 50. So the extension pages backwards with `before_at` until it
reaches lines already stored ([`pager.js`](../pnt/extension/pager.js)), pausing 3 s
between pages because PokerNow answers bursts with HTTP 429. The first load of a
long game takes several minutes to walk its history. The HUD fills in as it goes,
and the ⚙ settings' **history** row says when it is complete.

Everything that knows the response shape is in
[`normalize.js`](../pnt/extension/normalize.js). If PokerNow changes it, the ⚙
settings say **UNRECOGNIZED** and the page console prints the first item.

### Reacting to the table

The poll interval is only the slowest the HUD can be. The content script also
watches the table on the page ([`watch.js`](../pnt/extension/watch.js)): the pot,
the board, the dealer button, and each seat's classes, bet and stack. When an
action changes any of them (a bet moves chips; a fold or a check moves
`decision-current` to the next seat), it polls `/log` straight away, so the HUD
follows the action within about a second. The log is still the only thing read into
the tracker; the table only says when to look. Table-triggered polls start at least
a second apart. A change that finds no new log line yet gets up to three more looks
a second apart, and the shot clock (which redraws constantly) is ignored.

The table also says who is to act before the log does. The name on the
`decision-current` seat goes to the side panel as soon as it moves, so the chart
switches to that player at once, on all their hands, and narrows to their spot when
`/live` catches up.

[`spot.js`](../pnt/extension/spot.js) holds the follow rules as plain functions:
whom the chart shows, when a spot is sent, and how to tell a spot the chart echoes
back from one the user typed.

The websocket trigger (`gC` / `gameResult`) is deliberately not used. A 5-second
poll is fast enough for a HUD and survives a PokerNow socket change.

## Background server

`pnt service install` registers a Task Scheduler task for your Windows user. The
server starts hidden at every login and restarts itself after a crash, with no
terminal and no admin rights. It is built this way for these reasons:

- **`--db` is resolved to a full path at install.** The task does not start in your
  project folder, and a missing database file is created empty rather than
  reported, so a relative path would give a server that answers and shows zero
  hands. `install` refuses a path that does not exist.
- **A terminal `pnt serve` wins.** If the port is already taken, the task waits
  instead of crash-looping, and takes over once you close the terminal.
- **Two Windows defaults would kill it:** tasks are ended after 72 hours, and
  whenever a laptop goes on battery. Both are turned off.
- **The port is 52000, not 8000.** 8000 is the busiest port on a developer's
  machine, and the clash is quiet in both directions: this server holding it makes
  your other server fail to bind, and your other server holding it makes the task
  wait politely forever while the HUD reports the tracker unreachable. 52000 is in
  the IANA dynamic range, which is never assigned to a registered service. `--port
  N` changes it; set the same address in the extension's ⚙ settings.
- **Re-installing ends the running instance first.** The task is registered
  `IgnoreNew`, so a start request is *silently* ignored while an instance is alive.
  Without ending it first, changing `--db` or `--port` rewrote the definition,
  reported success, and left the old server running on the old settings.
- **Crashes restart in-process**, after 1 s and doubling up to 60 s. A run longer
  than a minute resets the delay. Task Scheduler's own restart is only a backup.
- **Idle cost** is about 75 MB of memory (a 62 MB server behind an 11 MB venv
  launcher) and no measurable CPU. The server only works when the extension posts.
  Stopping or restarting the task takes the launcher's child down with it, so
  nothing is left holding the port.

### Why setup is one command

`pnt service install` refuses a database that does not exist, and it is right to:
a missing file would be created empty, and the HUD would then quietly show
nothing. But until the first import nothing had created a database, so a fresh
install ran that command, got an error, and had no obvious next move. `pnt setup`
does the steps in order.

## Redaction

`pnt redact` keeps a `Your hand is` entry only when the same two cards also appear
in a `shows a` entry for that hand, and drops the rest. A matching show *is* you
showing, since two players cannot hold the same two cards, so what survives is what
the table already saw. On the 20-log corpus that keeps 1,451 of 5,006 hands and
removes 3,555.

Matching on *cards* rather than on identity has three consequences:

- It never needs to know which player is hero, so it cannot protect the wrong one.
- It fails closed. A one-card voluntary show does not match a two-card hand, so that
  entry goes too. The shown card survives in the `shows a` entry, where it was
  public to begin with.
- The copies still import. `infer_hero` votes on hands where hero's cards match a
  showdown holding, which is exactly what is kept, so a published log still
  identifies its own hero.

Nothing else in an export carries a hidden holding. `collected N from pot with …
(combination: …)` names five cards, but only ever for a player who showed
(`test_redact.py` asserts that over every fixture log), and `Undealt cards:` is by
definition the part of the deck that reached nobody.

Only the removed lines change; every other byte is copied unchanged, so the diff
against the original is pure deletions. Originals are never written to, and `--out`
refuses to point at the folder they live in.

## Testing

```bash
pytest -q
node --test pnt/extension/normalize.test.mjs pnt/extension/pager.test.mjs pnt/extension/spot.test.mjs
```

The Python suite is organized around invariants rather than examples:

- `test_amounts.py`: chips in equal chips out, on every hand. It also pins the two
  forced-post cases by hand: a straddle that restates the street total, and a
  missed big blind that absorbs the small blind posted with it.
- `test_parser.py`: the big-blind poster lands on the big-blind slot on every hand,
  and two independent position derivations agree on all 548 regular hands.
- `test_idempotent.py` is **the success criterion as a test**. Re-importing changes
  no stat. A partial capture plus a later full import converges on the same numbers
  as a clean import, and shuffled out-of-order ingest converges too.
- `test_stats.py`: derived stats are checked against a hand-worked manual count of
  20 hands, including the exact set of BB walks that must be excluded.
- `test_identity.py`: IDs survive churn, names do not, and merges are cheap.
- `test_caching.py`: reading less, and not deriving twice, change no number. One
  player's figures read only that player's hands. The unfiltered report is memoized
  against a counter that is bumped inside the same transaction as every change that
  could invalidate it, so a cache hit is provably current.
- `test_incomplete_hands.py`: a truncated hand leaves bb/100 alone and counts
  everywhere else.
- `test_redact.py`: a published log gives away no holding that a showdown did not,
  and redaction touches nothing else. Every surviving line is byte-identical and in
  order, every stat is unchanged, and hero is still identifiable.
- `test_sample_logs.py` guards what actually ships. Every file in `pnt/logs/` is
  re-audited, so a raw export dropped in there fails the suite rather than shipping
  in the next release. It also proves the sample fallback triggers on an *empty*
  log folder only, never on a small one and never on an explicit path.
- `test_concurrency.py`: concurrent writers queue instead of failing. Two tabs on
  one table, or `pnt import` while the server is up, put two writers on the file. A
  deferred transaction that reads before it writes cannot upgrade, and SQLite
  refuses it *without* consulting `busy_timeout`. Every write therefore goes through
  `writing()`, which takes the lock up front with `BEGIN IMMEDIATE`.

The three node test files cover the extension: response normalization, backwards
paging, and the follow rules in `spot.js`.
