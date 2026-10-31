// Screenshot harness: a stand-in for the chrome.* APIs the side panel uses.
// Loaded before spot.js. shoot.py puts the server address and hand count in #harness-conf.
(() => {
  const listeners = [];
  let snap = null;
  const conf = JSON.parse(document.getElementById("harness-conf").textContent);
  const behind = Number((location.hash.match(/update=(\d+)/) || [])[1] || 0);
  window.__health = { ok: true, data: { hands: conf.hands, update_behind: behind, dev: true } };
  window.chrome = {
    runtime: {
      lastError: undefined,
      sendMessage(msg, cb) {
        // The chosen columns: shoot.py saved GET /settings/hud beside the page.
        if (msg.type === "hudColumns") {
          fetch("settings-hud.json").then((r) => r.json()).then((data) => cb({ ok: true, data }), (e) => cb({ ok: false, error: String(e) }));
          return;
        }
        const r = msg.type === "health" ? window.__health
          : msg.type === "settings" ? { ok: true, data: { server: conf.server, pollSeconds: 5, liveMin: 1, liveKnown: 5, hudMode: "panel" } }
          : { ok: false, error: "harness" };
        setTimeout(() => cb(r), 0);
      },
      reload() {},
    },
    tabs: { query: async () => [{ id: 1 }], onActivated: { addListener() {} }, sendMessage: async () => {} },
    sidePanel: {}, // popup.js keeps the "side panel" choice only where there is one
    storage: {
      session: { get: async () => (snap ? { "status:1": snap } : {}) },
      sync: { set: async () => {} },
      onChanged: { addListener: (f) => listeners.push(f) },
    },
  };
  // Feed one status snapshot, as background.js would write it to storage.session.
  window.__push = (next) => {
    snap = next ? { ...next, rev: (snap?.rev ?? 0) + 1 } : null;
    for (const f of listeners) f({ "status:1": { newValue: snap } }, "session");
  };
  window.__snap = () => snap;
})();
