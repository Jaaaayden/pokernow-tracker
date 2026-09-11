/* Content script on a PokerNow game page: capture + overlay.
 *
 * Capture: poll GET /games/{id}/log with the page's own cookie, normalize, and
 * hand the entries to the background worker, which posts them to /ingest.
 * Fetches overlap on purpose -- the server dedupes on (game_id, order), so a
 * window that starts a minute before the last entry seen costs nothing and
 * cannot lose a line to clock skew.
 *
 * Overlay: after new entries land, ask /hud/{game} for the current roster and
 * lifetime stats, keyed by PokerNow ID (never by seat -- seats are reused), and
 * draw a panel. Each row can open the range chart for that player, embedded
 * from the local server.
 */
(() => {
  "use strict";
  const GAME = (location.pathname.match(/\/games\/([A-Za-z0-9_-]+)/) || [])[1];
  if (!GAME) return;

  const OVERLAP_MS = 60_000;
  const HUD_EVERY_MS = 30_000;

  const state = {
    server: "http://127.0.0.1:8000",
    pollSeconds: 5,
    lastAtMs: 0,          // newest `at` we have seen, in epoch ms
    backfilled: false,    // first sweep from the start of the game done
    offered: 0, inserted: 0, polls: 0, errors: 0,
    lastError: null, lastPoll: null, shape: null, envelopeOk: null,
    hud: null, hudAt: 0, paused: false,
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
      shape: state.shape, envelopeOk: state.envelopeOk, lastAt: state.lastAtMs,
      seats: state.hud ? state.hud.seats.length : 0, paused: state.paused,
    },
  });

  // ---------------------------------------------------------------- capture --
  async function fetchLog(afterMs, beforeMs) {
    const url = `${location.origin}/games/${GAME}/log?after_at=${afterMs}&before_at=${beforeMs}`;
    const r = await fetch(url, { credentials: "include", headers: { accept: "application/json" } });
    if (!r.ok) throw new Error(`log ${r.status}`);
    return r.json();
  }

  async function pollOnce() {
    const now = Date.now();
    const after = state.backfilled ? Math.max(0, state.lastAtMs - OVERLAP_MS) : state.lastAtMs;
    const body = await fetchLog(after, now + 60_000);
    const { entries, shape, sample, ok } = PNT.normalize(body);
    state.shape = shape;
    if (state.envelopeOk !== ok) {
      state.envelopeOk = ok;
      if (!ok) console.warn("[pnt] unrecognized /log envelope; first item:", sample, "full body:", body);
      else console.info("[pnt] /log envelope recognized:", shape);
    }
    if (!ok) throw new Error("unrecognized /log response shape (see console)");

    if (entries.length) {
      const res = await send({ type: "ingest", game_id: GAME, entries });
      if (!res.ok) throw new Error(res.error);
      state.offered += res.data.offered;
      state.inserted += res.data.new;
      const newest = Math.max(...entries.map((e) => Date.parse(e.at)));
      if (newest > state.lastAtMs) state.lastAtMs = newest;
      // A full page of entries on the first sweep means there may be more:
      // keep walking forward until a fetch adds nothing.
      if (!state.backfilled && res.data.new > 0) return true;
    }
    state.backfilled = true;
    return false;
  }

  async function loop() {
    if (state.paused) return schedule();
    try {
      state.polls += 1;
      let more = true;
      while (more) more = await pollOnce();
      state.lastPoll = Date.now();
      state.lastError = null;
      if (Date.now() - state.hudAt > HUD_EVERY_MS || state.inserted > (state._hudInserted || 0)) {
        state._hudInserted = state.inserted;
        await refreshHud();
      }
    } catch (e) {
      state.errors += 1;
      state.lastError = String(e.message || e);
      overlay.setStatus(state.lastError);
    }
    report();
    schedule();
  }

  let timer = null;
  function schedule() { clearTimeout(timer); timer = setTimeout(loop, state.pollSeconds * 1000); }

  async function refreshHud() {
    const r = await send({ type: "hud", game_id: GAME });
    state.hudAt = Date.now();
    if (!r.ok) { overlay.setStatus(r.error.includes("no hands") ? "waiting for the first hand" : r.error); return; }
    state.hud = r.data;
    overlay.render(r.data);
  }

  // ---------------------------------------------------------------- overlay --
  const overlay = (() => {
    const host = document.createElement("div");
    host.id = "pnt-overlay";
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        :host { all: initial; position: fixed; top: 12px; right: 12px; z-index: 2147483000; }
        .panel { font: 12px/1.35 system-ui, -apple-system, "Segoe UI", sans-serif; color: #fff;
          background: rgba(20,20,19,.92); border: 1px solid rgba(255,255,255,.14); border-radius: 10px;
          min-width: 300px; max-width: 460px; box-shadow: 0 10px 30px rgba(0,0,0,.45); }
        .head { display: flex; align-items: center; gap: 8px; padding: 7px 10px; cursor: move; user-select: none;
          border-bottom: 1px solid rgba(255,255,255,.1); }
        .head b { font-weight: 600; }
        .head .st { color: #c3c2b7; margin-left: auto; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 200px; }
        button { all: unset; cursor: pointer; padding: 2px 7px; border-radius: 5px; color: #c3c2b7; }
        button:hover { background: rgba(255,255,255,.1); color: #fff; }
        table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
        th, td { padding: 4px 8px; text-align: right; white-space: nowrap; }
        th { color: #898781; font-weight: 500; font-size: 11px; }
        td:first-child, th:first-child { text-align: left; }
        td.name { font-weight: 600; max-width: 120px; overflow: hidden; text-overflow: ellipsis; }
        td.name small { color: #898781; font-weight: 400; margin-left: 4px; }
        tr.sel td { background: rgba(57,135,229,.18); }
        .chart { display: none; border-top: 1px solid rgba(255,255,255,.1); }
        .chart.open { display: block; }
        .chart iframe { width: 100%; height: 560px; border: 0; background: #0d0d0d; border-radius: 0 0 10px 10px; }
        .chart .bar { display: flex; gap: 6px; padding: 6px 10px; align-items: center; }
        .chart select { font: inherit; background: #1a1a19; color: #fff; border: 1px solid #383835; border-radius: 5px; padding: 2px 6px; }
        .chart a { color: #86b6ef; margin-left: auto; }
        .empty { padding: 10px; color: #c3c2b7; }
        .hidden { display: none; }
      </style>
      <div class="panel">
        <div class="head"><b>PNT</b><span class="st" id="st">starting…</span>
          <button id="pause" title="pause capture">⏸</button><button id="min" title="collapse">–</button></div>
        <div id="body"><div class="empty">waiting for the first hand…</div></div>
        <div class="chart" id="chart">
          <div class="bar">
            <span id="who"></span>
            <select id="spot">
              <option value="opener,srp">opened, single-raised pot</option>
              <option value="opener,open_bb>=4,srp">opened 4bb+</option>
              <option value="3bet">3-bet</option>
              <option value="faced_3bet">faced a 3-bet</option>
              <option value="pfa,cbet_flop">c-bet flop</option>
              <option value="">all hands</option>
            </select>
            <select id="board">
              <option value="">any flop</option>
              <option value="flop=ace_high">ace high</option>
              <option value="flop=king_high">king high</option>
              <option value="flop=low">low (9 or under)</option>
              <option value="flop=monotone">monotone</option>
              <option value="flop=twotone">two-tone</option>
              <option value="flop=rainbow">rainbow</option>
              <option value="flop=paired">paired</option>
              <option value="flop=connected">connected</option>
            </select>
            <select id="view">
              <option value="preflop">chart</option>
              <option value="made">made hands</option>
            </select>
            <a id="ext" target="_blank" rel="noopener">open ↗</a>
            <button id="close">✕</button>
          </div>
          <iframe id="frame" title="range chart"></iframe>
        </div>
      </div>`;
    const $ = (id) => root.getElementById(id);
    let collapsed = false, selected = null;

    // drag
    const head = root.querySelector(".head");
    let drag = null;
    head.addEventListener("pointerdown", (e) => {
      if (e.target.tagName === "BUTTON") return;
      const r = host.getBoundingClientRect();
      drag = { dx: e.clientX - r.left, dy: e.clientY - r.top };
      head.setPointerCapture(e.pointerId);
    });
    head.addEventListener("pointermove", (e) => {
      if (!drag) return;
      host.style.left = Math.max(0, e.clientX - drag.dx) + "px";
      host.style.top = Math.max(0, e.clientY - drag.dy) + "px";
      host.style.right = "auto";
    });
    head.addEventListener("pointerup", () => {
      drag = null;
      try { localStorage.setItem("pnt-pos", JSON.stringify({ left: host.style.left, top: host.style.top })); } catch {}
    });
    try {
      const pos = JSON.parse(localStorage.getItem("pnt-pos") || "null");
      if (pos && pos.left) { host.style.left = pos.left; host.style.top = pos.top; host.style.right = "auto"; }
    } catch {}

    $("min").addEventListener("click", () => {
      collapsed = !collapsed;
      $("body").classList.toggle("hidden", collapsed);
      $("chart").classList.toggle("hidden", collapsed);
      $("min").textContent = collapsed ? "+" : "–";
    });
    $("pause").addEventListener("click", () => {
      state.paused = !state.paused;
      $("pause").textContent = state.paused ? "▶" : "⏸";
      setStatus(state.paused ? "paused" : "capturing");
      report();
    });
    $("close").addEventListener("click", () => { $("chart").classList.remove("open"); selected = null; markSel(); });
    $("spot").addEventListener("change", showChart);
    $("board").addEventListener("change", showChart);
    $("view").addEventListener("change", showChart);

    function chartUrl() {
      const q = new URLSearchParams({ player: selected, theme: "dark" });
      const f = [$("spot").value, $("board").value].filter(Boolean).join(",");
      if (f) q.set("filter", f);
      if ($("view").value !== "preflop") q.set("by", $("view").value);
      return `${state.server}/chart?${q}`;
    }
    function showChart() {
      if (!selected) return;
      $("who").textContent = selected;
      const url = chartUrl();
      $("frame").src = url;
      $("ext").href = url;
      $("chart").classList.add("open");
    }
    function markSel() {
      root.querySelectorAll("tr[data-alias]").forEach((tr) => tr.classList.toggle("sel", tr.dataset.alias === selected));
    }

    const fmt = (v) => (v == null ? "–" : String(v));

    function render(hud) {
      const body = $("body");
      body.replaceChildren();
      const seats = [...hud.seats].sort((a, b) => a.seat - b.seat);
      if (!seats.length) { body.innerHTML = '<div class="empty">no one dealt in yet</div>'; return; }
      const t = document.createElement("table");
      const hr = document.createElement("tr");
      for (const h of ["player", "hands", "VPIP", "PFR", "3Bet", "F3B", "CBet", "WTSD"]) {
        const th = document.createElement("th"); th.textContent = h; hr.appendChild(th);
      }
      t.appendChild(hr);
      for (const s of seats) {
        const st = s.stats || {};
        const tr = document.createElement("tr");
        tr.dataset.alias = s.alias || "";
        tr.title = `seat ${s.seat} · ${s.pn_id}`;
        const name = document.createElement("td"); name.className = "name";
        name.textContent = s.alias || s.pn_id;
        const seat = document.createElement("small"); seat.textContent = `#${s.seat}`; name.appendChild(seat);
        tr.appendChild(name);
        for (const k of ["hands", "vpip", "pfr", "3bet", "fold_to_3bet", "cbet_flop", "wtsd"]) {
          const td = document.createElement("td"); td.textContent = fmt(st[k]); tr.appendChild(td);
        }
        tr.addEventListener("click", () => { selected = s.alias; markSel(); showChart(); });
        t.appendChild(tr);
      }
      body.appendChild(t);
      markSel();
      setStatus(state.paused ? "paused" : `capturing · ${state.inserted} new`);
    }

    function setStatus(text) { $("st").textContent = text; $("st").title = text; }

    document.documentElement.appendChild(host);
    return { render, setStatus };
  })();

  // ------------------------------------------------------------------ start --
  (async () => {
    const s = await send({ type: "settings" });
    if (s.ok) { state.server = s.data.server; state.pollSeconds = Number(s.data.pollSeconds) || 5; }
    const h = await send({ type: "health" });
    overlay.setStatus(h.ok ? `connected · ${h.data.hands} hands in db` : `tracker not reachable at ${state.server}`);
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area !== "sync") return;
      if (changes.server) state.server = changes.server.newValue.replace(/\/+$/, "");
      if (changes.pollSeconds) state.pollSeconds = Number(changes.pollSeconds.newValue) || 5;
    });
    loop();
  })();
})();
