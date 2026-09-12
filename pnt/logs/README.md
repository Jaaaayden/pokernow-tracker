# Sample logs

PokerNow exports, redacted for publication by `pnt redact`. They ship inside the
package, and `pnt import` and `pnt setup` fall back to them when your own log folder
is empty — so a fresh install has something to query instead of an empty page.

**These are someone else's hands.** Every name in `pnt stats` after a fallback import
is a stranger. Drop your own exports in `~/Downloads/pokernow-logs` and re-run to add
yours, or pass `--no-sample` to skip these entirely.

```bash
pnt import path/to/pnt/logs      # or just `pnt import` with an empty log folder
```

They are the originals byte for byte with one exception: a `Your hand is` entry
survives only where the same two cards also appear in a `shows a` entry for that
hand. Hands the log owner did not show down no longer name their holding. Every
opponent's cards are untouched — those were only ever in the log because they were
shown at the table.

Nothing else changes. Every stat `pnt` derives comes from the action stream, so
these produce the same numbers the raw exports do; what is thinner is one player's
range coverage, now at the showdown-only level everybody else is at.

`tests/test_sample_logs.py` re-audits this folder on every run and fails if anything
here names a holding that never reached showdown. To add a log, put the raw export in
your log folder and run `pnt redact --out pnt/logs` — never copy an export in by hand.
