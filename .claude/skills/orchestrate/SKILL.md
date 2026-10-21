---
name: orchestrate
description: Split a batch of features or fixes across parallel worktree subagents and merge them back - plan waves, pick Sonnet or Opus per task, write self-contained briefs, and merge the files everyone appends to without losing code. Use when the user hands over several tasks at once (a list, docs/ideas.md items, a batch of UI notes) or says to delegate or parallelize.
---

# Orchestrate a batch

You plan, brief, merge and verify. Workers do the slices. A subagent with no `model` copies yours (Opus), so the model you pick for each worker is where the cost goes.

## 1. Plan the waves

1. List the tasks. For each, name the files it will touch.
2. **Foundation first.** Changes that several tasks need go in wave 1, alone or together, and land before anything that builds on them. That means new fact fields in `derive.py`, filter terms in `filters.py`, `Tally`, or the schema. The Oct 2026 batches went this way: foundation, then views.
3. Put independent tasks in waves of 2-4. More than that is hard to merge and review.
4. Show the plan to the user before spawning: waves, task, model, files.

## 2. Pick a model for each worker

| Model | Use it for |
|---|---|
| `sonnet` | Clear, one-area work where there's a pattern to copy: a chart view like an existing one, a HUD or page tweak, a CLI flag, docs, tests for behavior that already exists, `ui-feedback` notes, renames |
| `opus` | Stat definitions (SPEC.md and `derive.py` must agree), the filter grammar, identity merging, caching, locks, concurrency, schema changes, refactors across modules: anything where a wrong answer is quietly wrong numbers |
| `haiku` | Read-only questions ("where is X computed?"). Use `subagent_type: "Explore"`, which also runs on Opus unless told otherwise |

- **Always pass `model` explicitly.**
- **When unsure, start with Sonnet.** If that worker reports it's stuck, or `check` fails twice, run the slice again on Opus and put the failure in the brief.
- **Merging and resolving hotspot conflicts stays with you,** the orchestrator.

Each spawn looks like this:

```
Agent(subagent_type="general-purpose", model="sonnet", isolation="worktree",
      run_in_background=true, description="B2 chart group-by", prompt=<brief>)
```

## 3. The brief (each worker starts cold)

- **Goal and done-when:** the behavior, the endpoint or view, and the test that proves it.
- **Files:** the ones to touch, and the ones to leave alone (another worker's slice).
- **Base:** "run `git fetch origin && git rebase origin/main` and confirm HEAD is `<sha>` before writing code". Workers have started on stale commits before.
- **Setup:** `python scripts/orca_worktree.py setup`. After that, use only that worktree's `.venv`, port and `.pnt-dev`. Follow AGENTS.md's worktree rules.
- **Hotspots:** insert next to related code, never at the end of `app.py`, `cli.py`, `chart.html`'s view blocks or the TITLE/VIEWS lines, or SPEC.md sections. Check that a new helper's name isn't taken (`pool_facts` exists three times).
- **Gate:** `python scripts/orca_worktree.py check` passes. Commit in AGENTS.md's style. Don't push; you merge.
- **Report back:** branch, commit SHAs, check output, files touched, anything left undone, and whether `pnt rebuild` is needed.

## 4. Merge

1. Merge in wave order onto a fresh branch from `origin/main`.
2. Use `blast-radius` on a foundation change before the next wave builds on it.
3. In the hotspots, keep-both conflict resolution breaks code: shared context steals closing braces and interleaves `cli.py` commands. Instead, rebuild the file from HEAD plus the branch's pure insertions, placed by anchor.
4. Then parse `chart.html`'s script with node, and run ruff.
5. Run `check` after every merge, not just the last one.
6. Bluff:value keeps its own hide list. A new chart view goes on it (`test_the_chart_hides_every_other_view_on_bluff_value`).

## 5. Finish

1. Push and open the PR per AGENTS.md, with `## Summary` and `## Testing`.
2. Write a table: task, model, result.
3. Say whether the user must run `pnt rebuild` or `pnt service restart`.
4. Remove the worktrees and branches you made.
