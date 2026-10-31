/* Service worker: the only part of the extension that talks to the tracker.
 *
 * The content script cannot fetch 127.0.0.1 from the PokerNow origin without
 * CORS games, but a background worker with host_permissions can. Everything the
 * page needs goes through one message: {type, ...} -> {ok, ...}.
 */

// `liveMin`: hands a spot needs before the live view shows it, else it widens.
// `liveKnown`: shown hands a spot narrowed to the board's texture must keep.
// `hudMode`: "panel" draws the HUD in Chrome's side panel, beside the page;
// "float" draws the same page in a box on the game page that can be dragged about.
const DEFAULTS = { server: "http://127.0.0.1:52000", pollSeconds: 5, liveMin: 1, liveKnown: 5, hudMode: "panel" };

// A saved number, or the default when nothing sensible was saved. Zero is a
// choice here (no texture gate), so `|| fallback` would be wrong.
function count(v, fallback, floor) {
  const n = Number(v);
  return Number.isFinite(n) ? Math.max(floor, Math.trunc(n)) : fallback;
}

// The default port moved off 8000, which is the busiest port on a dev machine.
// Anyone whose saved value is exactly an old default was accepting that default
// rather than choosing 8000, so they are moved across; any other value is a
// deliberate choice and is left alone.
const RETIRED_SERVERS = ["http://127.0.0.1:8000", "http://localhost:8000"];

// Browsers without Chrome's side panel API (Opera among them) have only the
// floating box. Saved "panel" there would leave the icon doing nothing, with the
// settings -- reached from the HUD -- out of reach too, so it reads as "float".
const modeOf = (v) => (v === "float" || !chrome.sidePanel ? "float" : "panel");

async function settings() {
  const s = await chrome.storage.sync.get(DEFAULTS);
  let server = String(s.server || DEFAULTS.server).replace(/\/+$/, "");
  // Saved before the settings checked it: "127.0.0.1:52000" with no scheme would
  // make the chart's URL relative to the extension and its origin unreadable.
  if (!/^https?:\/\//i.test(server)) server = `http://${server}`;
  if (RETIRED_SERVERS.includes(server)) {
    server = DEFAULTS.server;
    await chrome.storage.sync.set({ server });
  }
  return { ...DEFAULTS, ...s, server, hudMode: modeOf(s.hudMode) };
}

// The tracker refuses a write without this header: a site in another tab can POST
// to 127.0.0.1, but cannot add a custom header without a preflight the server
// never grants (see app.py).
const WRITE_HEADER = { "x-pnt": "1" };

// Chrome stops a service worker whose fetch has waited 30 s for its response, and
// the caller then hears only "message port closed". A first /hud on a big
// database can take that long, so the call gives up first, with an answer the
// content script reads as "still loading" (SLOW in content.js) rather than as a
// failure. The server goes on and caches it: the next poll's call is quick.
const SLOW_MS = 25_000;
const SLOW_TEXT = `no answer within ${SLOW_MS / 1000} s`;

async function call(path, init = {}) {
  const { server } = await settings();
  let r;
  try {
    r = await fetch(server + path, {
      ...init, headers: { ...init.headers, ...WRITE_HEADER }, signal: AbortSignal.timeout(SLOW_MS),
    });
  } catch (e) {
    throw e?.name === "TimeoutError" ? new Error(`tracker busy: ${SLOW_TEXT}`) : e;
  }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `${r.status} ${r.statusText}`);
  return body;
}

const handlers = {
  settings: () => settings(),

  health: async () => {
    const h = await call("/health");
    checkBuild(h);
    return h;
  },

  ingest: ({ game_id, entries, rebuild = true }) =>
    call("/ingest", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ game_id, entries, source: "extension", rebuild }),
    }),

  rebuild: ({ game_id }) => call(`/rebuild/${encodeURIComponent(game_id)}`, { method: "POST" }),

  hud: ({ game_id }) => call(`/hud/${encodeURIComponent(game_id)}`),

  // The hand in progress, read from the raw lines: needs no rebuild.
  live: ({ game_id, min, known }) =>
    call(`/live/${encodeURIComponent(game_id)}?min=${count(min, 1, 1)}&known=${count(known, DEFAULTS.liveKnown, 0)}`),

  // The HUD's 🚩: flag a hand for the chart's Tagged tab, or clear the flag.
  // `capture=1` on both of these writes: the HUD's table is in your own database
  // even while the server's pages show the sample.
  flag: ({ game_id, hand_number, flagged }) =>
    call(`/games/${encodeURIComponent(game_id)}/hands/${count(hand_number, 0, 0)}/flag?capture=1`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ flagged: !!flagged }),
    }),

  // A card's ✎: what you know about a player, written at the table. Empty clears it.
  playerNote: ({ alias, note }) =>
    call(`/players/${encodeURIComponent(alias)}/note?capture=1`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ note: String(note ?? "") }),
    }),

  // The content script reports here and the side panel and settings page read it
  // back: counters and status text under `status:<tab>`, several times a poll,
  // and the /hud and /live payloads under `data:<tab>`, only when they changed
  // (`data` in the message). Every write reaches every open extension page.
  status: async (msg, sender) => {
    const id = sender.tab?.id ?? "?";
    const put = { [`status:${id}`]: { ...msg.status, tabId: sender.tab?.id, at: Date.now() } };
    if (msg.data) put[`data:${id}`] = msg.data;
    await chrome.storage.session.set(put);
    return {};
  },

  // Which tab the content script is in; it cannot ask the tabs API itself. The
  // floating HUD's frame is told, so it shows that tab and never the active one.
  tab: async (msg, sender) => ({ id: sender.tab?.id ?? null }),
};

// ------------------------------------------------------------- HUD mode --
// The side panel is off by default and switched on per game tab, in panel mode
// only. A tab's own panel is shown on that tab alone: switch away and it goes,
// switch back and it returns.
//
// A game tab is known by its URL, not by its content script reporting in: after
// the extension is reloaded, a game tab already open has no working content
// script until one is injected again (see self-update), which can fail, and the
// icon must still open the panel there (which then says to reload). The URL is
// visible for these sites only, through host_permissions.
const sidePanel = chrome.sidePanel;
const GAME_URLS = [
  "https://www.pokernow.com/games/*", "https://pokernow.com/games/*",
  "https://www.pokernow.club/games/*", "https://pokernow.club/games/*",
];
const isGame = (url) => /^https:\/\/(www\.)?pokernow\.(com|club)\/games\/[A-Za-z0-9_-]+/.test(url || "");
async function hudMode() {
  const { hudMode } = await chrome.storage.sync.get({ hudMode: DEFAULTS.hudMode });
  return modeOf(hudMode);
}
// Off a game tab the icon opens the settings instead, with the tracker a click
// away. The popup is set per tab, never in the manifest: there it would take the
// click on game tabs too, and the panel would never open.
async function applyTab(tabId, game, mode) {
  await chrome.action.setPopup({ tabId, popup: game ? "" : "popup.html" }).catch(() => {});
  if (!sidePanel) return;
  const enabled = game && (mode ?? (await hudMode())) === "panel";
  await sidePanel.setOptions(enabled ? { tabId, path: "sidepanel.html", enabled } : { tabId, enabled }).catch(() => {});
}
// In panel mode the toolbar icon opens the panel; in float mode it shows and
// hides the box on the page (action.onClicked only fires when the panel does not
// take the click).
async function applyMode() {
  const mode = await hudMode();
  await sidePanel?.setPanelBehavior({ openPanelOnActionClick: mode === "panel" }).catch(() => {});
  // Every tab, for the popup: a tab's URL is hidden off these sites, which is
  // enough to know it is no game.
  const tabs = await chrome.tabs.query({}).catch(() => []);
  for (const t of tabs) await applyTab(t.id, isGame(t.url), mode);
}
sidePanel?.setOptions({ enabled: false }).catch(() => {});
applyMode();
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "sync" && changes.hudMode) applyMode();
});
// A tab whose popup was lost (Chrome drops a tab's action settings when it loads
// a new page) still gets the settings rather than a click that does nothing.
chrome.action.onClicked.addListener(async (tab) => {
  if (tab?.id == null) return;
  if (isGame(tab.url)) return chrome.tabs.sendMessage(tab.id, { type: "toggle-hud" }).catch(() => {});
  await chrome.action.setPopup({ tabId: tab.id, popup: "popup.html" }).catch(() => {});
  chrome.action.openPopup?.({ windowId: tab.windowId }).catch(() => {});
});

// A closed tab's report would otherwise sit in session storage until the browser closes.
const forget = (tabId) => chrome.storage.session.remove([`status:${tabId}`, `data:${tabId}`]);
chrome.tabs.onRemoved.addListener(forget);
// Nor may it outlive a reload or a move off the game: the side panel would go on
// showing a table that is no longer there. A game page reports again as it loads.
// Its panel follows its URL: a reload keeps it, and a move off the game drops it.
// Again once loaded: the popup setting can be dropped after "loading" is heard.
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status === "loading") forget(tabId);
  if (info.status || info.url) applyTab(tabId, isGame(tab.url));
});

// ---------------------------------------------------------- self-update --
// `pnt update` (or a reinstall) replaces these files in place, and Chrome goes on
// running the copy it loaded until the extension is reloaded. The server reports
// a hash of the files it shipped with; one that differs from this instance's own
// means the files changed underneath, so it reloads.
//
// Its own is hashed from the folder the way the server hashes it (update.py's
// extension_build), when Chrome has just read the files: on install, on a reload
// and at browser start. It used to be the first hash heard from the server, which
// was the new one whenever the server first answered after the reinstall -- the
// browser left open, no game opened since, or the server down until then -- so
// the old copy took the new hash as its own and never reloaded.
//
// Every file in the folder bar the tests: test_update.py holds the two lists equal.
const FILES = [
  "annotate.js", "background.js", "content.js", "icons/icon128.png", "icons/icon16.png", "icons/icon32.png",
  "icons/icon48.png", "manifest.json", "normalize.js", "pager.js", "popup.html", "popup.js",
  "sidepanel.html", "sidepanel.js", "spot.js", "watch.js",
];
async function buildOf(files, read) {
  const enc = new TextEncoder(), zero = new Uint8Array([0]), parts = [];
  for (const rel of [...files].sort()) parts.push(enc.encode(rel), zero, new Uint8Array(await read(rel)), zero);
  const all = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  parts.reduce((at, p) => (all.set(p, at), at + p.length), 0);
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", all));
  return [...digest].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 12);
}
let hashing = null;
function hashOwnFiles() {
  const read = (rel) => fetch(chrome.runtime.getURL(rel)).then((r) => r.arrayBuffer());
  // Unreadable (the folder mid-reinstall): the first hash heard stands in, as before.
  hashing = buildOf(FILES, read).then((build) => chrome.storage.session.set({ build }), () => {});
}
chrome.runtime.onStartup.addListener(hashOwnFiles);

const BUILD_CHECK_MS = 60_000;
let buildCheckedAt = 0;
async function checkBuild(health) {
  buildCheckedAt = Date.now();
  const build = health?.extension_build;
  if (!build) return; // a server older than the check
  await hashing;
  const { build: mine } = await chrome.storage.session.get("build");
  if (!mine) return chrome.storage.session.set({ build });
  // Once per server build: if the files read after a reload still differ from
  // it, another reload reads them again and differs again -- Chrome stops that
  // loop only by disabling the extension. Local storage outlives the reload.
  const { reloadedFor } = await chrome.storage.local.get("reloadedFor");
  if (mine === build) { if (reloadedFor) chrome.storage.local.remove("reloadedFor"); }
  else if (reloadedFor !== build) {
    await chrome.storage.local.set({ reloadedFor: build });
    chrome.runtime.reload();
  }
}
// On any message, at most once a minute: a table being played messages every few
// seconds, and an idle extension has nothing to update for.
function checkBuildSoon() {
  if (Date.now() - buildCheckedAt < BUILD_CHECK_MS) return;
  buildCheckedAt = Date.now();
  call("/health").then(checkBuild, () => {});
}
checkBuildSoon();

// A reload leaves every open game tab with a content script that can no longer
// reach this worker (it notices, and stops: content.js). Without a fresh one the
// table would go uncaptured until the page was reloaded by hand.
chrome.runtime.onInstalled.addListener(async ({ reason }) => {
  if (reason !== "install" && reason !== "update") return;
  hashOwnFiles();
  const files = chrome.runtime.getManifest().content_scripts.flatMap((c) => c.js);
  const tabs = await chrome.tabs.query({ url: GAME_URLS }).catch(() => []);
  for (const t of tabs) chrome.scripting.executeScript({ target: { tabId: t.id }, files }).catch(() => {});
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  checkBuildSoon();
  // Own keys only: "constructor" or "toString" would otherwise find Object's.
  const h = Object.hasOwn(handlers, msg?.type) ? handlers[msg.type] : null;
  if (!h) { sendResponse({ ok: false, error: `unknown message ${msg?.type}` }); return false; }
  h(msg, sender).then(
    (data) => sendResponse({ ok: true, data }),
    (err) => sendResponse({ ok: false, error: String(err?.message || err) }),
  );
  return true; // async sendResponse
});
