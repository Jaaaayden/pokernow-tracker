# PokerNow log format — findings

Derived from real log exports plus the `PokerNowGrabber` and `PokerNow-HUD`
source. Everything below is verified against data, not inferred from documentation:
the six logs in `tests/fixtures/` (944 hands, 16,364 entries) are what the test
suite pins, and the grammar is additionally run against a 24-game working database
(6,255 hands, 125,341 entries). Both parse with **zero unrecognized lines**.

---

## 1. Identity

### The token

Players appear as `"Name @ ID"`. The ID charset is base64url-ish — `[A-Za-z0-9_-]`
— confirmed by `d-4X_F_SSU`, which contains both a dash and underscores. Anything
matching only `[A-Za-z0-9]` will silently mis-parse those players.

### IDs are stable; names are not

| Evidence | Consequence |
|---|---|
| `Chris @ 5NARaPRkSp` quits with stack 0 at 08:27:06, requests a seat at 08:28:38, is re-approved — **same ID** | The ID survives quit/rejoin *within* a game |
| `gpP9uUffpu` is **`genericpoker`** in `pgl41zM3` and **`500`** in `pglSdQty` | The ID survives renames *across* games. Keying on name would split one person's 421 hands into two |
| `d-4X_F_SSU` is **`getting even`** then **`1500`** | Same |
| `hsj @ PEMYRVPxOS` and `HSJ @ FJqZyN7BjN` | One human, two IDs — the cross-device case. Only an alias table can join these |

**The ID is the key. The display name is decoration.**

### Hero

`Your hand is 5♦, 8♣` carries no player name, so a downloaded log does not say
whose it is. Hero can be recovered by matching those cards against showdown
`shows` lines: hand #188 shows `genericpoker` holding exactly `5♦, 8♣`.

Done as elimination plus voting, this is self-validating — a player who ever shows
cards that are *not* hero's cards for that hand cannot be hero. On all three
fixtures the inference is unanimous: every other player at the table is
contradicted, and hero collects 24–59 confirming votes.

It matters more than it looks. Hero's cards are printed on *every* hand, not just
showdowns, so identifying hero lifts hole-card coverage from ~15% to ~58%.

**The user's own identity is not constant.** Hero is `gpP9uUffpu` in two logs and
`MBFczOlpuA` in the third — a second device. The alias table is needed on day one,
for you, before any opponent is considered.

---

## 2. Ordering and dedupe

`order` = `epoch_ms × 100 + sequence`. Verified: the entry at
`2026-08-10T08:30:37.861Z` has order `178635063786100`, and `1786350637861` is that
instant in epoch milliseconds.

It is globally unique and monotonic, which makes it both the sort key and the
dedupe key — overlapping fetches become free.

**`at` alone is not sufficient.** Six entries in the sample log share the
millisecond `2026-08-10T08:29:52.078Z`. Sorting by timestamp scrambles the order of
actions within a hand.

The CSV is written **newest-first**. Sort ascending by `order` before parsing.

The sub-index is not positionally stable: the `Player stacks:` line sits at `+03`
in hand #1 and `+01` in hand #188, because join events consume slots in between.
Never address entries by sub-index.

---

## 3. Bet amounts are cumulative per street

**The single highest-value finding.**

`bets N`, `raises to N` **and `calls N`** all state the player's *total commitment
for that street* — not the chips just pushed forward.

```
incremental = N − already_committed_this_street
```

Verified by reconstructing pot totals:

| Hand | Sequence | Pot |
|---|---|---|
| #180 | `raises to 30` / `calls 30` → preflop **60**, not 75; then 20/20, 50/50, 150 folded | 200 ✓ |
| #92 | flop `bets 10`, `raises to 40`, `calls 40` → flop total 80 (the call is incrementally 30) | 480 ✓ |
| #44 | `raises to 605 and go all in` vs 50 committed → `Uncalled bet of 555 returned` (605−50) | 100 ✓ |

Read `calls N` as incremental and every pot, every net-won figure and every bb/100
is wrong — while still looking entirely plausible. This is guarded by a
conservation law over all 549 hands: `Σ contributed == Σ collected`, per hand.

Uncalled returns are **not** contributed, because those chips were never at risk.
Hand #187: Chris posts SB 5, raises to 30, opponent folds, 20 is returned, he
collects 20 → net **+10**, exactly the opponent's big blind.

---

## 4. The roster

The dealt-in roster is the `Player stacks:` line, and only that line.

`The player "X" joined the game` events appear at order `…52441502` — *between* the
hand-start line at `…500` and `Player stacks:` at `…503`. They are inside the
hand-start block, and building a roster from them produces a different (wrong)
answer.

Seat numbers are physical table indices 1–10 and are **not contiguous**: a real
hand is seated `#1 | #2 | #3 | #10`. Positions must be counted over dealt-in
players in seat order, never over raw seat numbers.

---

## 5. Positions

- **Heads-up: the button is the small blind.** Verified on 300+ hands.
- **`(dead button)` exists.** One hand reads
  `-- starting hand #26 (id: ndddmwtmyhzo)  No Limit Texas Hold'em (dead button) --`
  with no dealer named at all. A parser that assumes a dealer token crashes or,
  worse, mis-positions everyone silently.
- **Dead blinds leave gaps.** Hand #25 of `pgl1UViJ4`: a player quits, a
  `Dead Small Blind` line appears, the button is on seat 2, and seat 10 posts the
  **big** blind while sitting one slot from the button. A plain 0..n−1 rotation
  calls seat 10 the small blind and mislabels the rest of the table.

The fix is to treat the big blind as authoritative and shift past the gap. The
invariant that catches all of this: **whoever posts the big blind must land on
`seats_from_button` 2 (or 1 heads-up), on every hand** — asserted across all 549.

---

## 6. Complete line vocabulary

Obtained by normalizing every entry into distinct shapes, so this is exhaustive for
the corpus rather than a guess. Grammar coverage is **0 unknowns** across all
125,341 entries. `"P"` below stands for the quoted `Name @ ID` token.

**Structure**: `-- starting hand #N (id: X)  <variant> (dealer: "P"|dead button) --`,
`-- ending hand #N --`, `Player stacks: #N "P" (S) | …`, `Your hand is C, C`

**Forced posts**: `"P" posts a <kind> of N`, where `<kind>` is one of
`small blind`, `big blind`, `ante`, `missed big blind`, `missing small blind`,
`straddle` — **each optionally suffixed ` and go all in`**. Also the bare line
`Dead Small Blind`.

> Two traps in one line. PokerNow writes **missed** big blind but **missing** small
> blind. And a post carries the same all-in suffix the voluntary actions do, which
> is easy to miss because it only appears when a stack is shorter than the blind it
> owes — see *Traps*.

**Actions**: `folds`, `checks`, `calls N`, `bets N`, `raises to N`, each of the last
three optionally suffixed ` and go all in`

**Board**: `Flop:  [C, C, C]` (two spaces), `Turn: … [C]`, `River: … [C]`, and
`Flop|Turn|River (second run): …` (run it twice) and
`Flop|Turn|River (second board): …` (Double Board)

**Pot**: `Uncalled bet of N returned to "P"`, `"P" collected N from pot`,
`"P" collected N from pot with <ranking> (combination: …)`

**Bounties**: `"P" paid N for the <kind> bounty to "P"`,
`"P" collected N from the <kind> bounty`. A side bet (the 7-2 game), settled
outside the pot — so it belongs in `net` but never in pot arithmetic.

**Showdown**: `"P" shows a C, C.` and `"P" shows a C.` (single-card voluntary show)

**Seating**: `The player "P" joined the game with a stack of N.`, and the same shape
for `quits the game with a stack of`, `stand up with the stack of` and
`sit back with the stack of`; `The player "P" requested a seat.`,
`The player "P" canceled the seat request.`,
`The admin approved the player "P" participation with a stack of N.`,
`The admin updated the player "P" stack from N to N.`

**Rebuys**: `The player "P" requested a rebuy of N.`,
`The player "P" rebought. New stack N.`,
`Asking to busted players the rebuy decision.`,
`Waiting for the game owner to approve or reject pending rebuy requests.`

**Room administration**: `The admin "P" enqueued|canceled the game stop on next hand.`,
`The admin "P" enqueued the removal of the player "…".`,
`The admin "P" rejected the seat request from the player "…".`,
`The admin "P" forced the player "…" to away mode in the next hand.`,
`The player "P" passed the room ownership to "…".`,
`WARNING: the admin queued the stack change…`

**Run it twice**: `"P" chooses to  run it twice.` and `chooses to  not run it twice.`
(note the doubled space), `All players in hand choose to run it twice.`,
`Some players choose to not run it twice.`,
`Remaining players decide whether to run it twice.`

**Config**: `The game's small|big blind|ante was changed from N to N.`,
`Game Config Changes` (multi-line), `Undealt cards: …` (rabbit hunt)

---

## 7. Traps

| Trap | Why it bites |
|---|---|
| **Hostile player names** | Real players are named `all in`, `500`, `1500`. Loose pattern matching on `and go all in` or on digits will mis-parse them. Anchor on the quoted token |
| **Multi-line CSV fields** | `Game Config Changes` contains newlines *inside* one field. Splitting the file on `\n` corrupts it — use a real CSV reader |
| **Showdown ≠ `shows`** | Players voluntarily show after winning uncontested, and rabbit-hunt shows appear *between* hands. Detect showdown by counting players who never folded |
| **Voluntary shows come after the hand ends** | A fold shown, an uncontested win shown, or a muck revealed late is logged *after* `-- ending hand #N --`, sometimes one card per line. Close the hand at that line and every one is silently dropped (157 lines in the fixtures). Attach them to the hand that just ended, but keep them out of showdown `hole_cards`: they are the hands players *chose* to reveal |
| **A forced post can be all in** | `"P" posts a big blind of 1 and go all in` — a stack shorter than the blind it owes. If the post rule does not accept the suffix, the line matches *nothing*, and an unmatched post is not a missing label but a missing **blind**: the chips never enter the pot, the hand records no blind post, and every net figure in it is wrong. It is rare enough to hide — two lines in 125,341 — and it only ever shows up in the ledger, never in a rate |
| **Blind levels move** | `The game's big blind was changed from 20 to 10` occurs mid-log. bb/100 must normalize each hand by *its own* big blind |
| **Run it twice** | Produces two `collected` lines and `(second run)` streets. Only the first run advances the betting street — by the time a second run is dealt, all action is complete |
| **Double Board** | A table option. Every street deals two boards — `Flop:` then `Flop (second board):` at the same instant — with betting *between* streets, and the pot is split with one `collected … on the second board` line. Same shape as run it twice (run 1), but the second board's line must not reset the street's bets, because action is still to come |
| **A log can stop mid-hand** | The export is a snapshot, so the last hand may have chips committed and no `collected` line. Every player still in it then reads as having lost their whole contribution — a loss that never happened, biased one way only. Mark the hand incomplete and keep it out of money figures; its folds and bets are real and count everywhere else |
| **Encoding** | Real exports are clean UTF-8 (`♠♥♦♣`). If you see `Aâ¦`, the file was decoded as latin-1 — fix it at the file-open boundary, not in the parser |

---

## 8. Live capture — no captcha on the log endpoint

The captcha gates the **"download full log" UI flow** at game end. It does not gate
the endpoint `PokerNowGrabber` uses mid-game:

```
GET https://www.pokernow.club/games/{gameId}/log?after_at={ms}&before_at={ms}
Cookie: npt=…        (dpt for Discord games)
```

The websocket is used only as a *trigger*: socket.io to `www.pokernow.club` with
`{gameID}`; on a `gC` message where `gT == "gameResult"`, a hand has ended, so
fetch that slice of the log.

This is the architecture to copy, because it means **one parser** serves both
backfill and live capture, and the success criterion holds by construction rather
than by diligence. An extension content script running on `pokernow.club` (games are now served from
`pokernow.com`; the extension matches both) sends the
`npt` cookie automatically (same-origin, `credentials: 'include'`), so it never
needs the manual cookie extraction Grabber requires.

**Confirmed on pokernow.com, 2026-09-11**, with read-only requests against a live
table and against a finished game already imported from CSV:

| Behaviour | Evidence |
|---|---|
| The envelope is `{logs: [{at, created_at, msg}], infos: {min, max}}` | Every response |
| `created_at` is a digit string equal to the CSV `order`, with identical line text and `at` | 50 of 50 lines of `pgl9BTQl8…` matched the imported rows exactly |
| Lines come **newest first**, at most **50** per request | Every response |
| `before_at` and `after_at` take `created_at` units and are **exclusive** | A pivot line was never returned |
| `before_at=X` pages **backwards** | Resumed at the very next line after the pivot |
| `after_at=X` **filters and never pages forwards**: it returns the 50 *newest* lines above X | `after_at` an hour back still returned the newest 50 |
| Both together return exactly the lines strictly between them, newest first | A 29-line window returned those 29 lines and nothing else |
| Plain epoch milliseconds sit below every `created_at` | `after_at=0&before_at=<now in ms>` returned `{"logs": []}` — the first extension's bug |
| Requests in quick succession get **HTTP 429** | Three requests in about 2 s |
| The log of a game is readable without a cookie | All of the above were made without one. Hero's own `Your hand is` lines presumably need the cookie; unverified |
| Without a cookie, a full walk is the export minus `Your hand is` and `Undealt cards:` | 2026-09-13, `pgltDzcp7…`: 2,624 of the export's 2,819 lines, all identical; the other 195 were 143 `Your hand is` and 52 `Undealt cards:` |
| The export CSV (`poker_now_log_<id>.csv`) is not scriptable; the ledger is | 403 and 200 respectively, without a cookie |
| A `Set-Cookie: npt=` echoing the value sent proves nothing | A random 50-character value was echoed back just the same |
| `npt` alone does **not** bring back `Your hand is` from a script | 2026-09-13: 0 of 3 expected in a window of `pglxfOwX…` (a game whose hero lines live capture had stored), with the right `npt`; browser-like headers and dropping `mm=false` changed nothing |
| `npt` **and `apt`** do: the walk is then the export, line for line | Same window: 3 of 3, identical. `pgltDzcp7…`: 2,819 of 2,819 lines, identical to the manual export, `Undealt cards:` included. `cf_clearance` was not needed |

So a complete capture walks **backwards**: fetch the newest page and, while pages
come back full, fetch `before_at=<oldest line so far>` until a page is short or
already stored. `pnt/extension/pager.js` implements exactly that, paced between pages
and resumable after a 429.
