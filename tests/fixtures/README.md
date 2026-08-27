# Fixtures

Real PokerNow log exports, kept under their original filenames so that
`game_id_from_filename` is exercised the way it will be in production.

| Game ID | Hands | Table sizes | Exercises |
|---|---|---|---|
| `pgl41zM3_CKphpnKM1DMIosUT` | 188 | 2 | Heads-up (button == small blind), one run-it-twice, a quit-and-rejoin keeping the same ID, an admin stack edit, a mid-game blind change |
| `pgl1UViJ4BhoVP-KKHpux1Mpv` | 128 | 2, 3, 4 | **Non-contiguous seats** (`#1 #2 #3 #10`), **one dead-button hand**, 11 run-it-twice, `stand up` / `sit back` |
| `pglSdQtyFGypDbrqD5IhXXlYz` | 233 | 2, 3 | 14 run-it-twice, a missed big blind + missing small blind, a `Dead Small Blind` line |

Together: 549 hands, 9,206 log entries, 0 parse misses.

Two identity facts these logs establish, both used in tests:

- `gpP9uUffpu` appears as **`genericpoker`** in `pgl41zM3` and as **`500`** in
  `pglSdQty` — one ID, two display names, across two games.
- Hero is `gpP9uUffpu` in two logs but `MBFczOlpuA` in `pgl1UViJ4` — the same
  human on a second device. That is the case `player_identities` exists for.
