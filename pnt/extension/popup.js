(async () => {
  const $ = (id) => document.getElementById(id);
  const send = (msg) => new Promise((res) => chrome.runtime.sendMessage(msg, res));

  const s = (await send({ type: "settings" })).data;
  $("server").value = s.server;
  $("poll").value = s.pollSeconds;
  $("livemin").value = s.liveMin ?? 1;
  $("liveknown").value = s.liveKnown ?? 5;
  if (!chrome.sidePanel) $("hudmode").querySelector('option[value="panel"]').remove();
  $("hudmode").value = s.hudMode === "float" ? "float" : "panel";
  // Known ahead of the click: the side panel opens only in the click's own turn,
  // before anything has been awaited.
  const [here] = await chrome.tabs.query({ active: true, currentWindow: true });
  $("tracker").href = s.server + "/";

  $("save").addEventListener("click", async () => {
    const hudMode = $("hudmode").value;
    // Back to the side panel from the floating box: open it for this tab rather
    // than leave the HUD nowhere until the toolbar icon is clicked. The panel is
    // switched on for the tab first, since it is off everywhere in float mode.
    if (hudMode === "panel" && s.hudMode === "float" && here?.id != null && chrome.sidePanel?.open) {
      chrome.sidePanel.setOptions({ tabId: here.id, path: "sidepanel.html", enabled: true }).catch(() => {});
      chrome.sidePanel.open({ tabId: here.id }).catch(() => {});
    }
    s.hudMode = hudMode;
    await chrome.storage.sync.set({
      hudMode,
      server: $("server").value.trim().replace(/\/+$/, "") || "http://127.0.0.1:52000",
      pollSeconds: Math.max(2, Number($("poll").value) || 5),
      liveMin: Math.max(1, Number($("livemin").value) || 1),
      liveKnown: $("liveknown").value === "" ? 5 : Math.max(0, Math.trunc(Number($("liveknown").value)) || 0),
    });
    $("tracker").href = $("server").value.trim().replace(/\/+$/, "") + "/";
    health();
  });

  async function health() {
    const h = await send({ type: "health" });
    $("health").textContent = h.ok ? `connected · ${h.data.hands} hands` : `not reachable: ${h.error}`;
    $("health").className = h.ok ? "ok" : "bad";
  }

  async function status() {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const all = await chrome.storage.session.get(null);
    const st = tab && all[`status:${tab.id}`];
    const kv = $("status");
    kv.replaceChildren();
    const put = (k, v) => { const a = document.createElement("span"); a.textContent = k; const b = document.createElement("span"); b.textContent = v; kv.append(a, b); };
    if (!st) { put("this tab", "not a PokerNow game page"); return; }
    put("game", st.game);
    put("capture", st.paused ? "paused" : "running");
    put("polls", `${st.polls} (${st.errors} errors)`);
    put("entries", `${st.inserted} new of ${st.offered} offered`);
    put("history", st.history ? `${st.history}${st.pages ? ` · ${st.pages} pages` : ""}` : "–");
    put("last poll", st.lastPoll ? new Date(st.lastPoll).toLocaleTimeString() : "–");
    put("seated", String(st.seats));
    if (st.live) put("live", st.live);
    put("log shape", st.envelopeOk == null ? "not seen yet" : st.envelopeOk
      ? `ok (${st.shape.list}/${st.shape.entry}/${st.shape.at}/${st.shape.order || "synth"})`
      : "UNRECOGNIZED – see page console");
    if (st.lastError) put("last error", st.lastError);
  }

  health();
  status();
  setInterval(status, 2000);
})();
