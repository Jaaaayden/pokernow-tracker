---
name: check
description: Run this repo's full pre-PR gate (pytest, the node extension tests and ruff, the same three CI runs) or a focused test loop, and read failures. Use before every commit you push, after changing pnt/ or tests/, or when asked to run, fix or verify tests.
---

# Checking a change

## The gate (before every pushed commit)

```
python scripts/orca_worktree.py check
```

This runs `pytest -q`, `node --test pnt/extension/*.test.mjs` and
`ruff check pnt tests`, using whichever checkout you're in (its own `.venv`).
The last line is `all checks passed` or `FAILED: <steps>`. Extra arguments go
to pytest: `check -x tests/test_api.py`.

If `.venv` is missing in a worktree, run `python scripts/orca_worktree.py setup` first.

## Focused loop

```
.venv/Scripts/python -m pytest -q -x tests/test_<area>.py
.venv/Scripts/python -m pytest -q -x tests/test_<area>.py -k <name>
node --test pnt/extension/<file>.test.mjs
.venv/Scripts/python -m ruff check pnt tests --fix
```

Finish with the full gate. A focused pass doesn't replace it.

## Rules

- `tests/conftest.py` points the config and log folder at temp dirs, so tests
  never touch real data. Don't work around it.
- `ALL_LOGS` in conftest is pinned to hand-counted totals. Never add a log to it.
  New cases get their own fixture (`tests/fixtures/README.md`).
- A changed stat definition changes `pnt/stats/SPEC.md` in the same commit.
- Report failures faithfully in the PR's `## Testing` section: exact command and
  result. Never `--no-verify`.
- Visual changes need more than tests. For the HUD side panel, use the
  `hud-screenshot` skill. For server pages, screenshot `/chart`, not
  `/chart.html`, headless via `orca_worktree.py serve`.
