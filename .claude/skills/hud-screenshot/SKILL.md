---
name: hud-screenshot
description: Screenshot the HUD side panel (pnt/extension/sidepanel.html) in any state (loaded, loading, stuck boxes, ? key, twins, sample, update banner) with headless Chrome and a stubbed chrome.*. Use for any HUD visual check or before/after shot of a side-panel change. Not for server pages (/chart, /), which screenshot headless directly.
---

# HUD side-panel screenshots

The claude-in-chrome screenshot calls time out on the loaded side panel (CDP
captureScreenshot hits 30 s), so don't use them here. This script renders a copy of
the panel outside the extension instead.

## Run it

```
.venv/Scripts/python .claude/skills/hud-screenshot/shoot.py --out <scratchpad>/hud --states loaded,key
```

- `--states`: comma list or `all`: `loading`, `between`, `stuck-loading`,
  `stuck-down`, `stuck-tab`, `waiting`, `empty`, `error`, `loaded`, `update`,
  `sample`, `key`, `twins`.
- `--server`: defaults to this worktree's port (`.pnt-dev/port`), else 52000. In a
  worktree, start `python scripts/orca_worktree.py serve` first, and never point it at 52000.
- `--game`: defaults to the hero's latest game. `--height` (default 900).

It prints one PNG path per state. Read each PNG to look at it.

## How it works

- Copies `sidepanel.html`, `sidepanel.js`, `spot.js` and `annotate.js` from `pnt/extension/` into
  `<out>/site`, adding `harness/stub.js` before spot.js and `harness/scenario.js`
  after sidepanel.js. The panel code is always the checkout's current code.
- `stub.js` fakes `chrome.runtime.sendMessage` (health and settings),
  `chrome.tabs.query` and `chrome.storage.session` plus `onChanged`. It also exposes
  `__push(snap)` to feed a status snapshot (`hudData`, `liveData`, `hudEmpty`,
  `hudError`, `trackerDown`, `text`, `rev`).
- `scenario.js` reads `?s=<state>` and pushes that snapshot. `hudData` is a real
  `GET /hud/<game>`. `liveData` is `GET /live/<game>`, or between hands an
  unopened preflop hand built from the HUD's seats.
- Headless Chrome uses `--virtual-time-budget`, so the 10, 15 and 30 s stuck boxes
  show up without waiting. `<out>/profile` is reused between runs, so localStorage
  (`pnt-cols`) persists.

## When it breaks

- "script tags changed": `sidepanel.html` no longer loads `spot.js` and then
  `sidepanel.js`. Update the injection in `build_site`.
- A new `chrome.*` call in sidepanel.js: add it to `harness/stub.js`.
- A new panel state: add a branch to `scenario.js` and the name to `STATES` (and
  to `SLOW` if it needs time to pass).
