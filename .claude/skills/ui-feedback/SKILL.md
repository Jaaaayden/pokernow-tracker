---
name: ui-feedback
description: Work through the notes the user left on the tracker's pages or HUD with the note overlay (the backtick key) - read the open ones, find each element's code, fix it, check it with a screenshot, and reply on the note. Use when the user says they left notes, comments or annotations on the UI, or asks to fix UI feedback.
---

# UI feedback from the note overlay

The user clicks an element with the overlay on (`pnt/extension/annotate.js`) and types what's wrong. Each note is stored by `pnt/server/annotations.py`, so there's no screenshot to decode.

## 1. Read the open notes

| Where the note was left | Where it is stored | How to read it |
|---|---|---|
| Live tracker (port 52000, main checkout) | `~/.pnt/annotations.json` | `curl -s "http://127.0.0.1:52000/annotations?status=open"` |
| A worktree's own server | `<worktree>/.pnt-dev/annotations.json` | Same call on the port in `.pnt-dev/port` |

When you work in a worktree, check both places. Reading is always fine. AGENTS.md's worktree rules cover the database, the config and port 52000, not this file.

Each note carries:
- `note`: what the user wrote.
- `page`, `query`: where it was left, as the path ("/chart", or "sidepanel" for the HUD) and the page's query.
- `selector`, `tag`, `classes`: which element.
- `text`, `context`: the element's text, and the nearest heading or container id above it.
- `rect`, `viewport`: where it sat and how wide the window was. A width of about 400 means the side panel, or the chart framed inside it.
- `reply`: an earlier answer. A note that was reopened still has it; read it to see what was tried.

## 2. Find the code

| `page` | Code |
|---|---|
| `/` | `pnt/server/static/index.html` |
| `/chart` | `pnt/server/static/chart.html` (big: grep, don't read it whole) |
| `/stats.html`, `/players.html`, `/allin.html`, `/pots.html`, `/tendencies.html` | The same name under `pnt/server/static/` |
| `sidepanel` | `pnt/extension/sidepanel.html` plus `sidepanel.js`, which builds the seat cards in JS |

Search in this order:
1. Ids in `selector` and `context`.
2. Class names.
3. A fixed phrase from `text`. Numbers and player names come from the API, so grep for the label next to them instead.

Several notes on the same component make one change.

## 3. Fix, then look at it

- **The HUD:** use the `hud-screenshot` skill.
- **Server pages:** take a headless screenshot at the note's `viewport` width: `chrome --headless=new --window-size=W,H --screenshot=OUT URL`. Use `/chart`, not `/chart.html`.
- **What needs a restart:** pages and scripts are re-read on every request, so HTML and JS edits show up on reload. A Python change needs the server restarted: in a worktree, run `python scripts/orca_worktree.py serve` again. On the live tracker, that's the user's `pnt service restart`.
- **Before you push:** run the `check` skill.

## 4. Reply on each note

Reply with one line saying what changed and where. The user then dismisses the note ("Looks good") or reopens it.

```
curl -s -X POST "$SERVER/annotations/<id>" -H "x-pnt: 1" -H "content-type: application/json" \
  -d '{"status":"resolved","reply":"Widened the 3bet column, chart.html .lines table (a1b2c3d)"}'
```

- **When you won't change something:** still resolve it, and put the reason in the reply.
- **Never delete a note.** Deleting is the user's call.
- **If `/annotations` answers 404:** the live tracker predates the overlay. The user needs to run `pnt service restart`.
