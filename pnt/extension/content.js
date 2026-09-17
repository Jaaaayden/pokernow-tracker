/* Content script on a PokerNow game page: capture, and nothing drawn on the page.
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
 * A rebuild, which re-derives the whole game, waits for the hand to end.
 *
 * The HUD goes out in the status report, which the background worker keeps per tab.
 * The side panel (sidepanel.js) draws it beside the page rather than over it,
 * and sends back the one thing it needs from here: pause and resume.
 */
(() => {
  "use strict";
  const GAME = (location.pathname.match(/\/games\/([A-Za-z0-9_-]+)/) || [])[1];
  if (!GAME) return;

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

  const state = {
    server: "http://127.0.0.1:52000",
    pollSeconds: 5,
    sync: { cursor: 0, walk: null }, // see pager.js
    offered: 0, inserted: 0, polls: 0, errors: 0, pages: 0,
    lastError: null, lastPoll: null, shape: null, envelopeOk: null,
    hud: null, hudAt: 0, paused: false,
    statusText: "starting…",
    rev: 0,             // bumped whenever the hud changes, so the panel redraws only then
    backoffMs: 0,
    rebuiltAt: 0,       // `inserted` at the last rebuild
    rebuiltTime: 0,
    endedSince: false,  // a hand-end line arrived since the last rebuild
    running: false, pollStartedAt: 0,
    poked: 0,           // why the next poll runs: 1 the table changed, n > 1 the nth look for its log line
    pokedWhileRunning: false,
  };

  const send = (msg) => new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage(msg, (r) => {
        if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
        else resolve(r || { ok: false, error: "no response" });
      });
    } catch (e) { resolve({ ok: false, error: String(e) }); }
  });

  const report = () => send({
    type: "status",
    status: {
      game: GAME, polls: state.polls, offered: state.offered, inserted: state.inserted,
      errors: state.errors, lastError: state.lastError, lastPoll: state.lastPoll,
      shape: state.shape, envelopeOk: state.envelopeOk, pages: state.pages,
      history: state.sync.walk ? "loading" : state.sync.cursor ? "complete" : "not loaded",
      seats: state.hud ? state.hud.seats.length : 0, paused: state.paused,
      text: state.statusText, rev: state.rev, hudData: state.hud,
    },
  });

  function setStatus(text) {
    state.statusText = text;
    report();
  }
  // New HUD data: the panel redraws its cards on the next report.
  function changed() {
    state.rev += 1;
    setStatus(state.paused ? "paused" : `capturing · ${state.inserted} new`);
  }

  const END_LINE = /^-- ending hand #\d+ --$/;
  /** True when any of these log entries closes a hand. */
  const handEnded = (entries) => (entries || []).some((e) => END_LINE.test(e.entry || ""));

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
      const res = await send({ type: "ingest", game_id: GAME, entries, rebuild: false });
      if (!res.ok) throw new Error(res.error);
      state.offered += res.data.offered;
      state.inserted += res.data.new;
      if (res.data.new && handEnded(entries)) state.endedSince = true;
      return res.data;
    },
    pause: () => new Promise((resolve) => setTimeout(resolve, PAGE_PAUSE_MS)),
  };

  // Re-derive the game when a hand has ended since the last time, when forced (a
  // history-walk checkpoint), or when lines have been arriving for a minute with
  // no end line seen. Mid-hand lines alone never trigger one: the derived tables
  // would not change.
  async function rebuildIfDue(force = false) {
    if (state.inserted === state.rebuiltAt) return false;
    const stale = Date.now() - state.rebuiltTime > REBUILD_STALE_MS;
    if (!force && !state.endedSince && !stale) return false;
    const r = await send({ type: "rebuild", game_id: GAME });
    if (!r.ok) throw new Error(r.error);
    state.rebuiltAt = state.inserted;
    state.rebuiltTime = Date.now();
    state.endedSince = false;
    await refreshHud();
    return true;
  }

  async function loop() {
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
          if (pages === 1 || pages % REBUILD_EVERY_PAGES === 0) await rebuildIfDue(true);
          if (pages > 1) state.statusText = `loading history · ${state.inserted} lines`;
          report();
        },
      });
      await rebuildIfDue();
      if (Date.now() - state.hudAt > HUD_EVERY_MS) await refreshHud();
      state.lastPoll = Date.now();
      state.lastError = null;
      state.backoffMs = 0;
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
      const sig = PNT.tableSignature(document);
      if (sig === last) return;
      const first = last === null;
      last = sig;
      if (!first) poke();
    };
    // Class and text changes only: the shot clock moves by inline style many
    // times a second, and must not wake anything. One look per burst of changes.
    new MutationObserver(() => { if (!settle) settle = setTimeout(check, SETTLE_MS); })
      .observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ["class"] });
    check();
  }

  async function refreshHud() {
    const r = await send({ type: "hud", game_id: GAME });
    state.hudAt = Date.now();
    if (!r.ok) { setStatus(r.error.includes("no hands") ? "waiting for the first hand" : r.error); return; }
    state.hud = r.data;
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

  // ------------------------------------------------------------------ start --
  (async () => {
    const s = await send({ type: "settings" });
    if (s.ok) {
      state.server = s.data.server;
      state.pollSeconds = Number(s.data.pollSeconds) || 5;
    }
    const h = await send({ type: "health" });
    setStatus(h.ok ? `connected · ${h.data.hands} hands in db` : `tracker not reachable at ${state.server}`);
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area !== "sync") return;
      if (changes.server) state.server = changes.server.newValue.replace(/\/+$/, "");
      if (changes.pollSeconds) state.pollSeconds = Number(changes.pollSeconds.newValue) || 5;
    });
    loop();
    watchTable();
  })();
})();
