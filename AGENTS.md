# Working in this repo

PokerNow hand tracker: a parser and SQLite store, stats derived at read time, a
local FastAPI server on 127.0.0.1:52000, and a Chrome extension HUD. README.md has
the user's side; docs/architecture.md has the design.

## Map

- `pnt/logfmt/`: log grammar and parser, hero detection, redact and anonymize.
- `pnt/ingest/`: CSV import, fetch, sync, and the log folder.
- `pnt/db/`: `schema.sql` and connections.
- `pnt/stats/`: every stat and report. **`SPEC.md` is the source of truth**, and
  `derive.py` mirrors it. A changed stat definition changes SPEC.md in the same commit.
- `pnt/server/`: `app.py` (API) and `static/` (the pages: chart, replay, ...).
- `pnt/extension/`: the HUD side panel and content scripts, with `*.test.mjs` beside them.
- `pnt/cli.py`: the `pnt` command. `pnt/service.py`: the background server task.
- `.claude/skills/`: `check` (the test gate), `hud-screenshot`, `ui-feedback` (the
  note overlay's notes), `blast-radius` (who reads a shared change), `orchestrate`.

## Worktree rules (Orca)

**Only in a linked git worktree** (Orca's, or one made with `git worktree add`). Check with
`git rev-parse --git-dir`: a path containing `/worktrees/` means you are in one. In the main
checkout none of this applies; the live tracker, its database and `pnt service` are
used there as usual.

Each worktree gets its own `.venv`, `.pnt-dev/` and port from `python scripts/orca_worktree.py setup`.

- Use this worktree's `.venv`, never the main checkout's. That one runs the main checkout's code.
- Never run `pnt service ...`, never bind port 52000, and never open the main checkout's
  `pokernow.sqlite` or edit `~/.pnt/config.json`. All three belong to the user's live tracker.
- To run a server, use `python scripts/orca_worktree.py serve`. It serves a copy of
  the database on this worktree's port.
- After a merge, the user runs `pnt service restart` in the main checkout. You don't.

## Testing

- `python scripts/orca_worktree.py check` runs pytest, the node tests and ruff (in
  whichever checkout you are in): the gate before every commit you push. CI runs the same three.
- For a focused loop: `.venv/Scripts/python -m pytest -q -x tests/test_<area>.py`.
- `tests/conftest.py` already points the config and log folder at temp dirs, so no test
  can touch real data.
- `ALL_LOGS` in conftest is pinned to hand-counted totals. Never add a log to it.
  New cases go in their own fixture (see `tests/fixtures/README.md`).
- HUD visuals: the `hud-screenshot` skill renders `sidepanel.html` in headless Chrome
  with a stubbed `chrome.*`. The claude-in-chrome screenshots time out on the side
  panel. Server pages screenshot headless directly (`/chart`, not `/chart.html`).

## Merge hotspots

Parallel branches collide where everyone appends:
- the view blocks and the TITLE/VIEWS lines in `chart.html`
- the ends of `app.py` and `cli.py`
- sections of `SPEC.md`
- `derive.py`, `filters.py` and `Tally`

So:
- `git fetch origin && git rebase origin/main` before you start and again before the PR.
- Insert next to related code rather than at the end of the file.
- Check that a new helper's name isn't already taken in another module
  (`grep -rn "def <name>" pnt`); copies drift, as `pool_facts` and `_pct` did.

## Commits and PRs

- Write a subject of 72 characters or fewer, a blank line, then a body wrapped at
  72 columns saying what changed and why. Never write one long line. The Oct 11–19
  2026 commits in `git log` are the mistake, not the style.
- The PR body has `## Summary` and `## Testing` sections (exact commands and results). Say if
  the user must run `pnt rebuild` or `pnt service restart` after merging.
- Never force-push main, never `--no-verify`, never `git stash`.
- Never put real player names in `pnt/logs/`, which ships in the wheel.
