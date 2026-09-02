# Fixtures

Real PokerNow log exports, kept under their original filenames so that
`game_id_from_filename` is exercised the way it will be in production.

| Game ID | Hands | Table sizes | Exercises |
|---|---|---|---|
| `pgl41zM3_CKphpnKM1DMIosUT` | 188 | 2 | Heads-up (button == small blind), one run-it-twice, a quit-and-rejoin keeping the same ID, an admin stack edit, a mid-game blind change |
| `pgl1UViJ4BhoVP-KKHpux1Mpv` | 128 | 2, 3, 4 | **Non-contiguous seats** (`#1 #2 #3 #10`), **one dead-button hand**, 11 run-it-twice, `stand up` / `sit back` |
| `pglSdQtyFGypDbrqD5IhXXlYz` | 233 | 2, 3 | 14 run-it-twice, a missed big blind + missing small blind, a `Dead Small Blind` line |

Together: 549 hands, 9,206 log entries, 0 parse misses.

## Edge-case corpus

Added because the core three cannot fail on any of it. Each of these logs broke
chip conservation before live forced posts were read as cumulative, so each one
is a regression test with money at stake rather than a bigger sample.

| Game ID | Hands | Exercises |
|---|---|---|
| `pgldBYgodxANW2_YvaxBEJh-3` | 70 | **34 straddles**, 17 of them posted by the small blind. The 6 that fold around are where an additive straddle loses exactly one small blind from the pot |
| `pgl7sRNQr64BIPFwmlFel-Le5` | 290 | A **missed big blind posted alongside a live small blind** by a player who then never acts again (#74) — the only hand in the corpus that pins the missed-BB rule, since everywhere else a later cumulative action silently absorbs the error |
| `pglkWn5b4Y8whHqWY3tVmrtW1` | 35 | A log **exported mid-hand** (#35 has blinds in and nobody paid), the rebuy and admin vocabulary, and blind-change lines in **decimal currency** (`0.10` → `0.05`) |

Edge corpus: 395 hands, 7,158 log entries, 0 parse misses, 1 deliberately
incomplete hand.

Why the two corpora stay separate: the hand-worked stat totals in
`test_stats.py` are pinned to the core three by a manual count. Adding logs to
`ALL_LOGS` would invalidate that count; invariants that must hold everywhere use
the `parsed_every` fixture instead.

Two identity facts these logs establish, both used in tests:

- `gpP9uUffpu` appears as **`genericpoker`** in `pgl41zM3` and as **`500`** in
  `pglSdQty` — one ID, two display names, across two games.
- Hero is `gpP9uUffpu` in two logs but `MBFczOlpuA` in `pgl1UViJ4` — the same
  human on a second device. That is the case `player_identities` exists for.
