// Screenshot harness: a stand-in for the chrome.* APIs the side panel uses.
// Loaded before spot.js. shoot.py puts the server address and hand count in #harness-conf.
(() => {
  const listeners = [];
  let snap = null;
  const conf = JSON.parse(document.getElementById("harness-conf").textContent);
  const behind = Number((location.hash.match(/update=(\d+)/) || [])[1] || 0);
  window.__health = { ok: true, data: { hands: conf.hands, update_behind: behind } };
  window.chrome = {
    runtime: {
      lastError: undefined,
      sendMessage(msg, cb) {
        const r = msg.type === "health" ? window.__health
          : msg.type === "settings" ? { ok: true, data: { server: conf.server } }
          : { ok: false, error: "harness" };
        setTimeout(() => cb(r), 0);
      },
      reload() {},
    },
    tabs: { query: async () => [{ id: 1 }], onActivated: { addListener() {} }, sendMessage: async () => {} },
    storage: {
      session: { get: async () => (snap ? { "status:1": snap } : {}) },
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
