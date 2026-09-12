/* Service worker: the only part of the extension that talks to the tracker.
 *
 * The content script cannot fetch 127.0.0.1 from the PokerNow origin without
 * CORS games, but a background worker with host_permissions can. Everything the
 * page needs goes through one message: {type, ...} -> {ok, ...}.
 */

const DEFAULTS = { server: "http://127.0.0.1:52000", pollSeconds: 5 };

// The default port moved off 8000, which is the busiest port on a dev machine.
// Anyone whose saved value is exactly an old default was accepting that default
// rather than choosing 8000, so they are moved across; any other value is a
// deliberate choice and is left alone.
const RETIRED_SERVERS = ["http://127.0.0.1:8000", "http://localhost:8000"];

async function settings() {
  const s = await chrome.storage.sync.get(DEFAULTS);
  let server = (s.server || DEFAULTS.server).replace(/\/+$/, "");
  if (RETIRED_SERVERS.includes(server)) {
    server = DEFAULTS.server;
    await chrome.storage.sync.set({ server });
  }
  return { ...DEFAULTS, ...s, server };
}

async function call(path, init) {
  const { server } = await settings();
  const r = await fetch(server + path, init);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `${r.status} ${r.statusText}`);
  return body;
}

const handlers = {
  settings: () => settings(),

  health: () => call("/health"),

  ingest: ({ game_id, entries, rebuild = true }) =>
    call("/ingest", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ game_id, entries, source: "extension", rebuild }),
    }),

  rebuild: ({ game_id }) => call(`/rebuild/${encodeURIComponent(game_id)}`, { method: "POST" }),

  hud: ({ game_id }) => call(`/hud/${encodeURIComponent(game_id)}`),

  // The content script reports here; the popup reads it back.
  status: async (msg, sender) => {
    const key = `status:${sender.tab?.id ?? "?"}`;
    await chrome.storage.session.set({ [key]: { ...msg.status, tabId: sender.tab?.id, at: Date.now() } });
    return {};
  },
};

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const h = handlers[msg?.type];
  if (!h) { sendResponse({ ok: false, error: `unknown message ${msg?.type}` }); return false; }
  h(msg, sender).then(
    (data) => sendResponse({ ok: true, data }),
    (err) => sendResponse({ ok: false, error: String(err?.message || err) }),
  );
  return true; // async sendResponse
});
