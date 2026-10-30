(async () => {
  const $ = (id) => document.getElementById(id);
  const DEFAULT_SERVER = "http://127.0.0.1:52000";
  // A worker that does not answer says so, rather than leaving every field blank.
  const send = (msg) => new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage(msg, (r) => {
        if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
        else resolve(r || { ok: false, error: "no response" });
      });
    } catch (e) { resolve({ ok: false, error: String(e) }); }
  });
  const say = (text, ok) => { $("health").textContent = text; $("health").className = ok ? "ok" : "bad"; };

  const got = await send({ type: "settings" });
  if (!got.ok) {
    say("extension worker not answering: reload the extension", false);
    $("health").title = got.error;
    return;
  }
  const s = got.data;
  $("server").value = s.server;
  $("poll").value = s.pollSeconds;
  $("livemin").value = s.liveMin ?? 1;
  $("liveknown").value = s.liveKnown ?? 5;
  if (!chrome.sidePanel) $("hudmode").querySelector('option[value="panel"]').remove();
  $("hudmode").value = s.hudMode === "float" ? "float" : "panel";
  // Known ahead of the click: the side panel opens only in the click's own turn,
  // before anything has been awaited.
  const [here] = await chrome.tabs.query({ active: true, currentWindow: true });
  const isGame = (url) => /^https:\/\/(www\.)?pokernow\.(com|club)\/games\/[A-Za-z0-9_-]+/.test(url || "");
  $("tracker").href = s.server + "/";

  // An address with no scheme ("127.0.0.1:52000") gets http://; anything that is
  // still not an http(s) URL is refused, since the chart frame and its messages
  // need a real origin. Null when refused.
  function serverOf(text) {
    let v = text.trim().replace(/\/+$/, "") || DEFAULT_SERVER;
    if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(v)) v = `http://${v}`;
    try {
      const u = new URL(v);
      return /^https?:$/.test(u.protocol) && u.hostname ? v : null;
    } catch { return null; }
  }

  $("save").addEventListener("click", async () => {
    const hudMode = $("hudmode").value;
    const server = serverOf($("server").value);
    if (!server) { say("not a server address: e.g. http://127.0.0.1:52000", false); return; }
    // Back to the side panel from the floating box: open it for this tab rather
    // than leave the HUD nowhere until the toolbar icon is clicked. The panel is
    // switched on for the tab first, since it is off everywhere in float mode.
    // Only on a game tab: elsewhere the panel would open on nothing and shut again.
    if (hudMode === "panel" && s.hudMode === "float" && here?.id != null && isGame(here.url) && chrome.sidePanel?.open) {
      chrome.sidePanel.setOptions({ tabId: here.id, path: "sidepanel.html", enabled: true }).catch(() => {});
      chrome.sidePanel.open({ tabId: here.id }).catch(() => {});
    }
    s.hudMode = hudMode;
    $("server").value = server;
    await chrome.storage.sync.set({
      hudMode,
      server,
      pollSeconds: Math.max(2, Number($("poll").value) || 5),
      liveMin: Math.max(1, Number($("livemin").value) || 1),
      liveKnown: $("liveknown").value === "" ? 5 : Math.max(0, Math.trunc(Number($("liveknown").value)) || 0),
    });
    $("tracker").href = server + "/";
    health();
  });

  // The hands the HUD reads (`capture_hands`), as its header says: while the
  // server's pages show the sample, `hands` is the sample's.
  async function health() {
    const h = await send({ type: "health" });
    if (!h.ok) { say(`not reachable: ${h.error}`, false); return; }
    const sample = h.data.dataset === "sample" ? " · pages show the sample" : "";
    say(`connected · ${h.data.capture_hands ?? h.data.hands} hands${sample}`, true);
  }

  // This tab's report only, and only while the popup can be seen.
  async function status() {
    if (document.hidden) return;
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const key = tab ? `status:${tab.id}` : null;
    const st = key && (await chrome.storage.session.get(key))[key];
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
