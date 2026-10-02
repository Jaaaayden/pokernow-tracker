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
  let server = (s.server || DEFAULTS.server).replace(/\/+$/, "");
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

async function call(path, init = {}) {
  const { server } = await settings();
  const r = await fetch(server + path, { ...init, headers: { ...init.headers, ...WRITE_HEADER } });
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

  // The HUD's 🚩: flag a hand for the chart's Manual review tab, or clear the flag.
  flag: ({ game_id, hand_number, flagged }) =>
    call(`/games/${encodeURIComponent(game_id)}/hands/${count(hand_number, 0, 0)}/flag`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ flagged: !!flagged }),
    }),

  // The content script reports here -- counters, status text, and the HUD and
  // live payloads -- and the side panel and settings page read it back.
  status: async (msg, sender) => {
    const key = `status:${sender.tab?.id ?? "?"}`;
    await chrome.storage.session.set({ [key]: { ...msg.status, tabId: sender.tab?.id, at: Date.now() } });
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
async function applyTab(tabId, game, mode) {
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
  const tabs = await chrome.tabs.query({ url: GAME_URLS }).catch(() => []);
  for (const t of tabs) await applyTab(t.id, true, mode);
}
sidePanel?.setOptions({ enabled: false }).catch(() => {});
applyMode();
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "sync" && changes.hudMode) applyMode();
});
chrome.action.onClicked.addListener((tab) => {
  if (tab?.id != null) chrome.tabs.sendMessage(tab.id, { type: "toggle-hud" }).catch(() => {});
});

// A closed tab's report would otherwise sit in session storage until the browser closes.
chrome.tabs.onRemoved.addListener((tabId) => chrome.storage.session.remove(`status:${tabId}`));
// Nor may it outlive a reload or a move off the game: the side panel would go on
// showing a table that is no longer there. A game page reports again as it loads.
// Its panel follows its URL: a reload keeps it, and a move off the game drops it.
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status === "loading") chrome.storage.session.remove(`status:${tabId}`);
  if (info.status === "loading" || info.url) applyTab(tabId, isGame(tab.url));
});

// ---------------------------------------------------------- self-update --
// `pnt update` replaces these files in place, and Chrome goes on running the copy
// it loaded until the extension is reloaded. The server reports a hash of the
// files it shipped with, and the first one this instance hears is taken as its
// own: session storage is emptied by a reload and by a browser restart, the two
// times Chrome reads the files afresh. A different hash after that means the
// files changed underneath, so it reloads, and takes the new hash as its own.
const BUILD_CHECK_MS = 60_000;
let buildCheckedAt = 0;
async function checkBuild(health) {
  buildCheckedAt = Date.now();
  const build = health?.extension_build;
  if (!build) return; // a server older than the check
  const { build: mine } = await chrome.storage.session.get("build");
  if (!mine) await chrome.storage.session.set({ build });
  else if (mine !== build) chrome.runtime.reload();
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
  const files = chrome.runtime.getManifest().content_scripts.flatMap((c) => c.js);
  const tabs = await chrome.tabs.query({ url: GAME_URLS }).catch(() => []);
  for (const t of tabs) chrome.scripting.executeScript({ target: { tabId: t.id }, files }).catch(() => {});
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  checkBuildSoon();
  const h = handlers[msg?.type];
  if (!h) { sendResponse({ ok: false, error: `unknown message ${msg?.type}` }); return false; }
  h(msg, sender).then(
    (data) => sendResponse({ ok: true, data }),
    (err) => sendResponse({ ok: false, error: String(err?.message || err) }),
  );
  return true; // async sendResponse
});
