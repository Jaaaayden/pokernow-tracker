/* Content script on a PokerNow game page: capture, and the floating HUD's box
 * (float mode) -- nothing else is drawn on the page.
 *
 * Capture: poll GET /games/{id}/log with the page's own cookie, normalize, and
 * hand the entries to the background worker, which posts them to /ingest. How
 * that endpoint pages -- and why a full page means walking backwards -- is in
 * pager.js. The server dedupes on (game_id, order), so re-sending a line is free.
 * The timer is the floor: a change on the table itself (watch.js) polls at once,
 * so an action reaches the HUD in about a second rather than on the next tick.
 *
 * HUD: after new entries land, ask /hud/{game} for the current roster and
 * lifetime stats, keyed by PokerNow ID (never by seat -- seats are reused).
 *
 * Live: every poll that brings lines also asks /live/{game} for the hand in
 * progress -- who is to act, the spot each player is in, and the closest spot in
 * their history with hands behind it. A rebuild, which re-derives the whole
 * game, waits for the hand to end: the live view needs none.
 *
 * Both go out with the status report when they change (report()), and the
 * background worker keeps them per tab.
 * The side panel (sidepanel.js) draws them beside the page rather than over it,
 * and sends back the one thing it needs from here: pause and resume.
 */
(() => {
  "use strict";
  const GAME = (location.pathname.match(/\/games\/([A-Za-z0-9_-]+)/) || [])[1];
  if (!GAME) return;
  // Once per page. The worker injects this script into open game tabs after an
  // update, and a tab still loading then gets the manifest's copy as well: the
  // second one stops here. One orphaned by a reload stands aside (orphaned()):
  // each copy keeps its own extension context, so the test is the first copy's.
  const alive = globalThis.__pntContent;
  if (alive?.()) return;
  const runtime = chrome.runtime;
  globalThis.__pntContent = () => !!runtime?.id;

  const HUD_EVERY_MS = 30_000;
  // Between history pages. PokerNow answers a burst of /log requests with HTTP 429.
  const PAGE_PAUSE_MS = 3_000;
  // During a long history walk, re-derive and redraw this often so the HUD fills in.
  const REBUILD_EVERY_PAGES = 10;
  // A rebuild normally waits for `-- ending hand --`; this is the safety net for a
  // dropped end line, so the roster and the stats can never lag by more than this.
  const REBUILD_STALE_MS = 60_000;
  // Table watch (watch.js): how long a burst of redraws settles before the table
  // is compared, and the further looks for a log line that trails the table.
  const SETTLE_MS = 300;
  const LOG_LAG_MS = 1_000;
  const LOG_LAG_LOOKS = 3;
  const DEFAULT_SERVER = "http://127.0.0.1:52000";

  const state = {
    server: DEFAULT_SERVER,
    pollSeconds: 5,
    liveMin: 1,
    liveKnown: 5,
    hudMode: "panel",   // "float" frames the HUD on this page (see float mode)
    sync: { cursor: 0, walk: null }, // see pager.js
    offered: 0, inserted: 0, polls: 0, errors: 0, pages: 0,
    lastError: null, lastPoll: null, shape: null, envelopeOk: null,
    hud: null, hudJson: null, hudAt: 0, hudSlow: false, hudEmpty: false, hudError: null, trackerDown: false, paused: false,
    live: null, liveInserted: -1,
    statusText: "starting…",
    rev: 0,             // bumped whenever hud or live changes: the panel redraws, and the payloads go out, only then
    backoffMs: 0,
    rebuiltAt: 0,       // `inserted` at the last rebuild
    rebuiltTime: 0,
    endedSince: false,  // a hand-end line arrived since the last rebuild
    running: false, pollStartedAt: 0,
    poked: 0,           // why the next poll runs: 1 the table changed, n > 1 the nth look for its log line
    pokedWhileRunning: false,
    tableActing: null,  // the name on the table's decision-current seat (watch.js)
    tableActingAt: null, // Date.now() when the action reached them: the HUD's clock
    // The newest hand's "Player stacks" line: who sits where, by PokerNow ID. It
    // turns the table's name into an ID, which /live matches even when its names
    // are masked (the sample switch, app.py `_masks`).
    roster: { at: -1, seats: [] },
  };

  const send = (msg) => new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage(msg, (r) => {
        if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
        else resolve(r || { ok: false, error: "no response" });
      });
    } catch (e) { resolve({ ok: false, error: String(e) }); }
  });

  // A request to the tracker server, through the background worker. Whether the
  // last one could not reach it at all is `trackerDown`, which the panel's "not
  // answering" box reads: `lastError` holds PokerNow's fetch failures too (Wi-Fi,
  // an outage), and those are no reason to restart the tracker.
  const NO_SERVER = /failed to fetch|networkerror|load failed/i;
  async function tracker(msg) {
    const r = await send(msg);
    state.trackerDown = !r.ok && NO_SERVER.test(r.error || "");
    return r;
  }

  // The status goes out several times a poll; the /hud and /live payloads, much
  // the bigger part, only when `rev` has moved since the worker last took them
  // (background.js keeps the two apart, under `status:` and `data:`).
  let sentRev = -1;
  async function report() {
    const rev = state.rev;
    const data = rev !== sentRev ? { rev, hudData: state.hud, liveData: state.live } : undefined;
    if (data) sentRev = rev; // at once: the next report may go before this one is answered
    const r = await send({
      type: "status",
      status: {
        game: GAME, polls: state.polls, offered: state.offered, inserted: state.inserted,
        errors: state.errors, lastError: state.lastError, lastPoll: state.lastPoll,
        shape: state.shape, envelopeOk: state.envelopeOk, pages: state.pages,
        history: state.sync.walk ? "loading" : state.sync.cursor ? "complete" : "not loaded",
        seats: state.hud ? state.hud.seats.length : 0, paused: state.paused,
        live: state.live ? (state.live.hand_number ? `hand #${state.live.hand_number} · ${state.live.street}` : "between hands") : null,
        text: state.statusText, hudEmpty: state.hudEmpty, hudError: state.hudError, trackerDown: state.trackerDown,
        tableActing: state.tableActing, tableActingPn: actingPn(), tableActingAt: state.tableActingAt,
      },
      data,
    });
    if (data && !r.ok && sentRev === rev) sentRev = -1; // lost: the next report carries them
    // The worker no longer holds them (it drops a tab's on a load that left
    // this script running): sent again at once, or the panel waits for ever.
    if (!data && r.ok && r.data?.held === false && sentRev === rev) {
      sentRev = -1;
      return report();
    }
    return r;
  }

  // `#3 "Name @ ID" (1000)` per seat. A name may hold anything, " @ " and quotes
  // included; the ID's charset is known, so the match ends on it (tokens.py).
  const STACKS = /^Player stacks:/;
  const SEAT = /#\d+ "(.*?) @ ([A-Za-z0-9_-]+)" \(/g;
  function noteRoster(entries) {
    for (const e of entries) {
      if (!(e.order > state.roster.at) || !STACKS.test(e.entry || "")) continue;
      state.roster = { at: e.order, seats: [...e.entry.matchAll(SEAT)].map((m) => ({ name: m[1], pn_id: m[2] })) };
    }
  }
  // The ID on the seat the table has to act, when its name is one seat's alone.
  function actingPn() {
    const hits = state.roster.seats.filter((s) => s.name === state.tableActing);
    return hits.length === 1 ? hits[0].pn_id : null;
  }

  function setStatus(text) {
    state.statusText = text;
    report();
  }
  // New HUD or live data: the panel redraws its cards on the next report.
  function changed() {
    state.rev += 1;
    setStatus(capturing());
  }
  // With the time of the last good poll, so a quiet table (or a finished game)
  // still shows capture is alive: the count alone sits at the same number.
  function capturing() {
    if (state.paused) return "paused";
    const at = state.lastPoll ? ` · ${new Date(state.lastPoll).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}` : "";
    return `capturing · ${state.inserted} new${at}`;
  }

  // ---------------------------------------------------------------- capture --
  async function fetchPage({ after, before }) {
    // Both cursors are created_at values (epoch ms * 100 + seq); empty means none.
    const q = new URLSearchParams({ after_at: after ?? "", before_at: before ?? "" });
    const r = await fetch(`${location.origin}/games/${GAME}/log?${q}`, {
      credentials: "include", headers: { accept: "application/json" },
    });
    if (r.status === 429) {
      const seconds = Number(r.headers.get("retry-after"));
      throw new PNT.RateLimited(seconds > 0 ? seconds * 1000 : null);
    }
    if (!r.ok) throw new Error(`log ${r.status}`);
    const body = await r.json();
    const { entries, shape, sample, ok } = PNT.normalize(body);
    state.shape = shape;
    if (state.envelopeOk !== ok) {
      state.envelopeOk = ok;
      if (!ok) console.warn("[pnt] unrecognized /log envelope; first item:", sample, "full body:", body);
      else console.info("[pnt] /log envelope recognized:", shape);
    }
    if (!ok) throw new Error("unrecognized /log response shape (see console)");
    return { entries, size: PNT.unwrap(body)[1].length };
  }

  const io = {
    fetchPage,
    ingest: async (entries) => {
      // No rebuild per page: a rebuild re-derives the whole game, and a history walk
      // can run to a hundred pages. `rebuildIfDue` runs at checkpoints instead.
      noteRoster(entries);
      const res = await tracker({ type: "ingest", game_id: GAME, entries, rebuild: false });
      if (!res.ok) throw new Error(res.error);
      state.offered += res.data.offered;
      state.inserted += res.data.new;
      if (res.data.new && PNT.handEnded(entries)) state.endedSince = true;
      return res.data;
    },
    pause: () => new Promise((resolve) => setTimeout(resolve, PAGE_PAUSE_MS)),
  };

  // Re-derive the game when a hand has ended since the last time, when forced (a
  // history-walk checkpoint), or when lines have been arriving for a minute with
  // no end line seen. Mid-hand lines alone never trigger one: the derived tables
  // would not change, and the live view reads the raw lines directly.
  async function rebuildIfDue(force = false) {
    if (state.inserted === state.rebuiltAt) return false;
    const stale = Date.now() - state.rebuiltTime > REBUILD_STALE_MS;
    if (!force && !state.endedSince && !stale) return false;
    const r = await tracker({ type: "rebuild", game_id: GAME });
    if (!r.ok) throw new Error(r.error);
    state.rebuiltAt = state.inserted;
    state.rebuiltTime = Date.now();
    state.endedSince = false;
    await refreshHud();
    return true;
  }

  async function loop() {
    if (orphaned()) return retire();
    if (state.paused) return schedule();
    const poked = state.poked;
    state.poked = 0;
    state.running = true;
    state.pokedWhileRunning = false;
    state.pollStartedAt = Date.now();
    const before = state.inserted;
    let delay = state.pollSeconds * 1000;
    try {
      state.polls += 1;
      await PNT.sync(state.sync, io, {
        onPage: async ({ pages }) => {
          state.pages += 1;
          // `pages` counts within this pass, and every poll is a pass: page 1 forces
          // a rebuild only the first time, so the table's earlier hands show at
          // once. After that a poll's rebuild waits for the hand to end.
          if ((pages === 1 && !state.rebuiltTime) || pages % REBUILD_EVERY_PAGES === 0) await rebuildIfDue(true);
          if (pages > 1) state.statusText = `loading history · ${state.inserted} lines`;
          report();
        },
      });
      // The live hand is only worth reading once the walk has reached the present
      // and something has changed since it was last read. It goes first: the
      // panel follows it, and a rebuild, the HUD and the cold stats a rebuild
      // leaves behind can take a second between them.
      if (!state.sync.walk && state.inserted !== state.liveInserted) await refreshLive();
      // A rebuild may have given a new player an identity to resolve against.
      if (await rebuildIfDue() && !state.sync.walk) await refreshLive();
      if (Date.now() - state.hudAt > HUD_EVERY_MS) await refreshHud();
      state.lastPoll = Date.now();
      state.lastError = null;
      state.backoffMs = 0;
      if (state.hud) state.statusText = capturing();
      // The log can trail the table by a moment. A change that found no new
      // line yet gets a few more looks shortly after, rather than the next tick.
      if (poked && poked <= LOG_LAG_LOOKS && state.inserted === before) { state.poked = poked + 1; delay = LOG_LAG_MS; }
    } catch (e) {
      state.errors += 1;
      if (e instanceof PNT.RateLimited) {
        // The walk's place survives in state.sync, so the retry resumes it.
        state.backoffMs = Math.min(Math.max(state.backoffMs * 2, 10_000), 120_000);
        delay = e.waitMs ?? state.backoffMs;
        state.lastError = `PokerNow rate limit; retrying in ${Math.round(delay / 1000)}s`;
      } else {
        state.lastError = String(e.message || e);
      }
      state.statusText = state.lastError;
    }
    state.running = false;
    // The table moved while this poll was out: go again as soon as the floor allows.
    if (state.pokedWhileRunning && !state.backoffMs) {
      state.poked = 1;
      delay = Math.min(delay, PNT.pokeDelay(Date.now(), state.pollStartedAt));
    }
    report();
    schedule(delay);
  }

  let timer = null, dueAt = 0;
  function schedule(ms = state.pollSeconds * 1000) {
    clearTimeout(timer);
    dueAt = Date.now() + ms;
    timer = setTimeout(loop, ms);
  }

  // The extension was reloaded (by hand, or by itself after `pnt update`) and
  // this script can no longer reach it. The worker injects a fresh one, so this
  // one stops polling and takes its box off the page rather than run alongside.
  const orphaned = () => !chrome.runtime?.id;
  let observer = null;
  function retire() {
    clearTimeout(timer);
    observer?.disconnect();
    float.remove();
  }

  // ------------------------------------------------------------ table watch --
  // A change on the table asks for a poll now rather than at the next tick;
  // watch.js says what counts as a change. Never on top of a running poll (that
  // one goes again when it finishes), and never while paused, walking history
  // (the walk keeps its own pace) or backing off a rate limit.
  function poke() {
    if (state.paused || state.sync.walk || state.backoffMs) return;
    if (state.running) { state.pokedWhileRunning = true; return; }
    const ms = PNT.pokeDelay(Date.now(), state.pollStartedAt);
    if (Date.now() + ms >= dueAt) return; // the timer gets there first anyway
    state.poked = 1;
    schedule(ms);
  }

  function watchTable() {
    let last = null, settle = null;
    const check = () => {
      settle = null;
      if (orphaned()) return retire();
      // Who is to act goes to the panel now, not after the log: the chart can
      // move to them while the poll is still out, backing off, or walking history.
      const acting = PNT.tableActing(document);
      if (acting !== state.tableActing) {
        state.tableActing = acting;
        state.tableActingAt = acting ? Date.now() : null;
        report();
      }
      const sig = PNT.tableSignature(document);
      if (sig === last) return;
      const first = last === null;
      last = sig;
      if (!first) poke();
    };
    // Class and text changes only: the shot clock moves by inline style many
    // times a second, and must not wake anything. One look per burst of changes.
    observer = new MutationObserver(() => { if (!settle) settle = setTimeout(check, SETTLE_MS); });
    observer.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ["class"] });
    check();
  }

  // A /hud the server is still working on: the worker gave up waiting (its own
  // limit, background.js SLOW_MS) or was stopped mid-call by Chrome. Not a
  // failure: the server finishes and keeps it. Nor asked again for HUD_EVERY_MS,
  // a hand end's rebuild included: each ask starts the same work over beside
  // the first, and on a cold database they would pile up.
  const SLOW = /tracker busy|message port closed/i;

  async function refreshHud() {
    if (state.hudSlow && Date.now() - state.hudAt < HUD_EVERY_MS) return;
    const r = await tracker({ type: "hud", game_id: GAME });
    state.hudAt = Date.now();
    // The port closed because the extension was reloaded: the next poll retires.
    if (!r.ok && orphaned()) return;
    state.hudSlow = !r.ok && SLOW.test(r.error || "");
    if (state.hudSlow) {
      // Stats are on their way: no "failed" box beside "still loading".
      if (state.hudError || state.hudEmpty) {
        state.hudError = null;
        state.hudEmpty = false;
        state.rev += 1;
      }
      setStatus("stats still loading");
      return;
    }
    // A table with no complete hand yet has no stats to wait for, and a /hud that
    // failed has none coming: the panel tells both apart from a first /hud still
    // on its way (`hudEmpty`, `hudError`), so neither shimmers for ever.
    const empty = !r.ok && r.error.includes("no hands");
    const error = r.ok || empty ? null : r.error;
    if (empty !== state.hudEmpty || error !== state.hudError) {
      state.hudEmpty = empty;
      state.hudError = error;
      changed();
    }
    if (!r.ok) { setStatus(empty ? "waiting for the first hand" : r.error); return; }
    // The same figures again (the 30 s refresh of a quiet table) are no news: a
    // new `rev` would redraw every card and close the tooltip being read.
    const json = JSON.stringify(r.data);
    if (json === state.hudJson) return;
    state.hudJson = json;
    state.hud = r.data;
    changed();
  }

  // Zero is a real setting here (no texture gate), so it is not a missing value.
  function knownSetting(v) {
    const n = Number(v);
    return Number.isFinite(n) && v !== null && v !== "" ? Math.max(0, Math.trunc(n)) : 5;
  }

  async function refreshLive() {
    const r = await tracker({ type: "live", game_id: GAME, min: state.liveMin, known: state.liveKnown });
    if (!r.ok) { setStatus(r.error); return; }
    state.live = r.data;
    state.liveInserted = state.inserted;
    changed();
  }

  // The side panel's pause button. Answered with the new state, which also goes
  // out in the report so every panel showing this tab agrees.
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg?.type !== "pause" || msg.game !== GAME) return false;
    state.paused = !state.paused;
    setStatus(state.paused ? "paused" : "capturing");
    sendResponse({ ok: true, paused: state.paused });
    return false;
  });

  // ------------------------------------------------------------ float mode --
  // The ⚙ HUD setting's other half: instead of the side panel, the side panel's
  // own page framed in a box on the game page, so the two can never differ. The
  // box is dragged by the frame's header (sidepanel.js posts the steps here),
  // resized at its corner, and shown and hidden by the toolbar icon or its ✕.
  // Where it was left, its size and whether it is hidden are kept per site.
  const float = (() => {
    const EDGE = 4, SIZE = { w: 420, h: 640 };
    const EXT = chrome.runtime.getURL("");
    let host = null, box = null, frame = null, want = null, sizer = null;
    // Compact (the frame's –): the box is as tall as the table, and only its width
    // can be dragged. The full-size box's size is kept apart, to go back to.
    let compact = false;
    const read = (k) => { try { return JSON.parse(localStorage.getItem(k) || "null"); } catch { return null; } };
    const write = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} };

    // Every move goes through place(), which keeps the box inside the window:
    // dragged to an edge, or back on a smaller window, it would otherwise sit
    // off-screen with nothing to grab it by.
    function place(left, top) {
      const w = Math.min(box.offsetWidth || SIZE.w, innerWidth), h = Math.min(box.offsetHeight || SIZE.h, innerHeight);
      host.style.left = Math.min(Math.max(0, left), Math.max(0, innerWidth - w - EDGE)) + "px";
      host.style.top = Math.min(Math.max(0, top), Math.max(0, innerHeight - h - EDGE)) + "px";
    }
    const at = () => ({ left: parseFloat(host.style.left) || 0, top: parseFloat(host.style.top) || 0 });

    function setShown(on) {
      if (!host) return;
      host.style.display = on ? "" : "none";
      write("pnt-float-hidden", !on);
      if (on) place(at().left, at().top);
    }

    async function mount() {
      if (host) return;
      const t = await send({ type: "tab" });
      if (host || state.hudMode !== "float") return; // unmounted or remounted meanwhile
      // One left behind by a script the extension's reload orphaned, if it has
      // not yet noticed and removed it itself.
      for (const old of document.querySelectorAll("[data-pnt-float]")) old.remove();
      host = document.createElement("div");
      host.setAttribute("data-pnt-float", "");
      const root = host.attachShadow({ mode: "closed" });
      root.innerHTML = `
        <style>
          :host { all: initial; position: fixed; z-index: 2147483000; }
          .box { display: flex; flex-direction: column; resize: both; overflow: hidden;
            min-width: 260px; min-height: 160px; max-width: calc(100vw - ${EDGE * 2}px); max-height: calc(100vh - ${EDGE * 2}px);
            background: #141413; border: 1px solid rgba(255,255,255,.14); border-radius: 10px;
            box-shadow: 0 10px 30px rgba(0,0,0,.45); }
          .box.compact { resize: horizontal; }
          iframe { display: block; flex: 1; width: 100%; min-height: 0; border: 0; background: #141413; }
        </style>
        <div class="box"><iframe title="Tracker for PokerNow"></iframe></div>`;
      box = root.querySelector(".box");
      frame = root.querySelector("iframe");
      const size = read("pnt-float-size");
      box.style.width = (size?.w ?? SIZE.w) + "px";
      box.style.height = (size?.h ?? SIZE.h) + "px";
      frame.src = `${EXT}sidepanel.html?embed=1&tab=${t.ok && t.data.id != null ? t.data.id : ""}`;
      document.documentElement.appendChild(host);
      const pos = read("pnt-float-pos");
      if (pos) place(pos.left, pos.top);
      else place(innerWidth - box.offsetWidth - 12, 12);
      // `resize` fires no event of its own; the observer sees the corner drag land.
      sizer = new ResizeObserver(() => {
        if (!box || !box.offsetWidth || compact) return;
        write("pnt-float-size", { w: box.offsetWidth, h: box.offsetHeight });
      });
      sizer.observe(box);
      setShown(!read("pnt-float-hidden"));
    }

    function unmount() {
      sizer?.disconnect();
      sizer = null;
      host?.remove();
      host = box = frame = want = null;
      compact = false;
    }

    function setCompact(on, h, w) {
      compact = on;
      box.classList.toggle("compact", on);
      if (on) {
        box.style.height = Math.ceil(h) + 2 + "px"; // the border
        if (w) box.style.width = Math.ceil(w) + 2 + "px";
      } else {
        const size = read("pnt-float-size");
        box.style.width = (size?.w ?? SIZE.w) + "px";
        box.style.height = (size?.h ?? SIZE.h) + "px";
      }
      place(at().left, at().top);
    }

    // Steps from the header in the frame. `want` is where the box would be with
    // no window edge in the way, so dragging back from past an edge picks the box
    // up again where the pointer is rather than where it stopped.
    addEventListener("message", (e) => {
      if (!frame || e.source !== frame.contentWindow || `${e.origin}/` !== EXT) return;
      const m = e.data || {};
      if (m.type === "pnt-drag") {
        want ??= at();
        want = { left: want.left + (Number(m.dx) || 0), top: want.top + (Number(m.dy) || 0) };
        place(want.left, want.top);
      } else if (m.type === "pnt-drop") {
        want = null;
        write("pnt-float-pos", at());
      } else if (m.type === "pnt-fit") {
        const h = Number(m.h), w = Number(m.w);
        if (h > 0) setCompact(true, h, w > 0 ? w : 0);
      } else if (m.type === "pnt-full") {
        setCompact(false);
      } else if (m.type === "pnt-hide") {
        setShown(false);
      }
    });
    // A window that shrinks must not strand the box outside it either.
    addEventListener("resize", () => { if (host) place(at().left, at().top); });

    return {
      apply: () => (state.hudMode === "float" ? mount() : unmount()),
      toggle: () => setShown(host?.style.display === "none"),
      remove: unmount,
    };
  })();

  // The toolbar icon, in float mode (in panel mode it opens the panel instead).
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg?.type === "toggle-hud") float.toggle();
    return false;
  });

  // ------------------------------------------------------------------ start --
  (async () => {
    const s = await send({ type: "settings" });
    if (s.ok) {
      state.server = s.data.server;
      state.pollSeconds = Number(s.data.pollSeconds) || 5;
      state.liveMin = Math.max(1, Number(s.data.liveMin) || 1);
      state.liveKnown = knownSetting(s.data.liveKnown);
      state.hudMode = s.data.hudMode === "float" ? "float" : "panel";
    }
    float.apply();
    const h = await tracker({ type: "health" });
    setStatus(h.ok ? `connected · ${h.data.capture_hands ?? h.data.hands} hands in db${h.data.warming ? " · warming up" : ""}`
      : `tracker not reachable at ${state.server}: ${h.error}`);
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area !== "sync") return;
      if (changes.server) state.server = String(changes.server.newValue || DEFAULT_SERVER).replace(/\/+$/, "");
      if (changes.pollSeconds) state.pollSeconds = Number(changes.pollSeconds.newValue) || 5;
      if (changes.liveMin) {
        state.liveMin = Math.max(1, Number(changes.liveMin.newValue) || 1);
        state.liveInserted = -1; // re-read the live hand at the new threshold
      }
      if (changes.liveKnown) {
        state.liveKnown = knownSetting(changes.liveKnown.newValue);
        state.liveInserted = -1;
      }
      if (changes.hudMode) {
        // Through the worker, which knows whether this browser has a side panel.
        send({ type: "settings" }).then((s) => {
          if (!s.ok) return;
          state.hudMode = s.data.hudMode === "float" ? "float" : "panel";
          float.apply();
        });
      }
    });
    loop();
    watchTable();
  })();
})();
