# Sample logs

PokerNow exports, redacted and anonymized for publication by
`pnt redact --anonymize`: 65 games, 15,996 hands. They ship inside the package, but
a new install starts empty. They load only when asked, into an empty log folder:

```bash
pnt setup --sample               # or `pnt import --sample` into an existing database
pnt import path/to/pnt/logs      # or name the folder outright
```

Either way `aliases.csv` is applied afterwards, so one person who joined from
several devices is one player, as they were in the original database.

**These are someone else's hands.** Every player is a stand-in (`player 3`), and
every PokerNow ID and game ID is invented. Drop your own exports in your log folder
(`pnt where` names it) and re-run to add yours.

Two things changed from the originals, and nothing else:

- **Hole cards nobody saw.** A `Your hand is` entry survives only where the same two
  cards also appear in a `shows a` entry for that hand. Hands the log owner did not
  show down no longer name their holding. Every opponent's cards are untouched:
  those were only ever in the log because they were shown at the table.
- **Who was playing.** Each player ID and game ID is replaced by the same stand-in
  everywhere, across every file and in `aliases.csv`, and each player is named for
  their alias rather than whatever display name they used that night. A real game
  ID would be enough to fetch the original log from PokerNow, so those go too. The
  stand-ins come from a key that was never kept, so a real ID cannot be checked
  against them.

Every stat `pnt` derives comes from the action stream, and the stand-ins map one
to one. These give the same numbers the raw exports do: the same hands, heroes and
merges. What is thinner is one player's range coverage, now at the showdown-only
level everybody else is at.

`tests/test_sample_logs.py` re-checks this folder on every run. It fails if anything
here names a holding that never reached showdown, or a player who is not a
stand-in. To rebuild it, export the merges and redact the whole log folder in one
run, so every file shares one set of stand-ins. Clear the old files first: each run
draws new stand-ins, and two runs' files would show one person as two.

```bash
pnt alias export merges.csv
rm pnt/logs/poker_now_log_*.csv
pnt redact --anonymize --aliases merges.csv --out pnt/logs
```

Never copy an export in by hand.
