---
name: blast-radius
description: For a change to shared code in this repo (a fact field in derive.py, a filter term, Tally, a query or schema change, an app.py helper, an API key the pages or HUD read, spot.js), list everything else that reads it and prove each reader still works by running its tests and diffing real API output before and after. Use before merging such a change, when one change lands under other work, or when asked what a change could break.
---

# Blast radius

A small change in shared code shows up as wrong numbers somewhere else. Find every reader, then prove each one is fine with a test or an output diff, not by reading the code.

## 1. What changed

Run `git diff origin/main...HEAD --stat`, then name each changed symbol:
- functions
- fact fields
- filter terms
- columns
- API JSON keys
- CSS classes or ids that JS looks up

## 2. Who reads it

Grep each symbol across `pnt/`, `tests/` and `docs/`. The usual paths:

| What changed | Where it goes |
|---|---|
| A fact field in `derive.py` | `filters.py` terms, `Tally`, the `pnt/stats/*` reports, SPEC.md, and the API responses built from them |
| An API JSON key | `pnt/server/static/*.html` and `*.js`, and `pnt/extension/*.js`. The HUD reads `/hud` and `/live` through `content.js` and `spot.js` |
| A filter term | `vocabulary()` (`/filters`), `filter-help.js`, saved spots in user data, and the chart's chips |
| Schema, or what ingest stores | Every stored game. The user must run `pnt rebuild`, so say so in the PR |
| An `app.py` helper or middleware | Every route that uses it, plus the write-header and Host tests in `test_api.py` |

Watch for aliases: three modules define `pool_facts`, and `app.py` imports them under other names.

Write one table row per reader: reader, how it uses the symbol, what would break, and how you'll prove it doesn't.

## 3. Prove it

- **Tests:** `.venv/Scripts/python -m pytest -q tests/test_<area>.py` for each reader's area, then the full `check` skill.
- **Numbers:** for a stat or filter change, take the same API calls before and after, and diff them.
  - Get the baseline from a second worktree at `origin/main`, or `git checkout-index -a --prefix=<scratch>/`. Never `git stash`.
  - Serve each with `python scripts/orca_worktree.py serve` and `curl` the same URLs, for example `/players/<alias>/stats`, `/hud/<game>` and `/chart` data endpoints.
  - Every number that moved must be explained by the change. If one isn't, it's a bug.
- **If a definition changed,** SPEC.md changes in the same commit.

## 4. Report

1. The reader table, each row marked: proven by a named test or diff, or not checked and why.
2. Whether the user needs `pnt rebuild` or `pnt service restart`.
