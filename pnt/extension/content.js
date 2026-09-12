/* Content script on a PokerNow game page: capture + overlay.
 *
 * Capture: poll GET /games/{id}/log with the page's own cookie, normalize, and
 * hand the entries to the background worker, which posts them to /ingest. How
 * that endpoint pages -- and why a full page means walking backwards -- is in
 * pager.js. The server dedupes on (game_id, order), so re-sending a line is free.
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

  const HUD_EVERY_MS = 30_000;
  // Between history pages. PokerNow answers a burst of /log requests with HTTP 429.
  const PAGE_PAUSE_MS = 3_000;
  // During a long history walk, re-derive and redraw this often so the HUD fills in.
  const REBUILD_EVERY_PAGES = 10;

  const state = {
    server: "http://127.0.0.1:52000",
    pollSeconds: 5,
    sync: { cursor: 0, walk: null }, // see pager.js
    offered: 0, inserted: 0, polls: 0, errors: 0, pages: 0,
    lastError: null, lastPoll: null, shape: null, envelopeOk: null,
    hud: null, hudAt: 0, paused: false,
    backoffMs: 0,
    rebuiltAt: 0, // `inserted` at the last rebuild
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
    },
  });

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
      // can run to a hundred pages. `rebuildIfNew` runs at checkpoints instead.
      const res = await send({ type: "ingest", game_id: GAME, entries, rebuild: false });
      if (!res.ok) throw new Error(res.error);
      state.offered += res.data.offered;
      state.inserted += res.data.new;
      return res.data;
    },
    pause: () => new Promise((resolve) => setTimeout(resolve, PAGE_PAUSE_MS)),
  };

  async function rebuildIfNew() {
    if (state.inserted === state.rebuiltAt) return;
    const r = await send({ type: "rebuild", game_id: GAME });
    if (!r.ok) throw new Error(r.error);
    state.rebuiltAt = state.inserted;
    await refreshHud();
  }

  async function loop() {
    if (state.paused) return schedule();
    let delay = state.pollSeconds * 1000;
    try {
      state.polls += 1;
      await PNT.sync(state.sync, io, {
        onPage: async ({ pages }) => {
          state.pages += 1;
          if (pages > 1) overlay.setStatus(`loading history · ${state.inserted} lines`);
          if (pages === 1 || pages % REBUILD_EVERY_PAGES === 0) await rebuildIfNew();
          report();
        },
      });
      await rebuildIfNew();
      if (Date.now() - state.hudAt > HUD_EVERY_MS) await refreshHud();
      state.lastPoll = Date.now();
      state.lastError = null;
      state.backoffMs = 0;
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
      overlay.setStatus(state.lastError);
    }
    report();
    schedule(delay);
  }

  let timer = null;
  function schedule(ms = state.pollSeconds * 1000) { clearTimeout(timer); timer = setTimeout(loop, ms); }

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
            <a id="ext" target="_blank" rel="noopener">open ↗</a>
            <button id="close">✕</button>
          </div>
          <iframe id="frame" title="range chart"></iframe>
        </div>
      </div>`;
    const $ = (id) => root.getElementById(id);
    let collapsed = false, selected = null;

    // drag
    //
    // Every move goes through place(), which keeps the panel inside the window.
    // Without that, dragging toward the right or bottom edge -- or reopening the
    // table on a smaller window later -- parks the panel off-screen, where it
    // keeps capturing hands with nothing to show for it.
    const EDGE = 4;
    function place(left, top) {
      const r = host.getBoundingClientRect();
      const w = r.width || 300, h = r.height || 80;
      const maxLeft = Math.max(0, innerWidth - Math.min(w, innerWidth) - EDGE);
      const maxTop = Math.max(0, innerHeight - Math.min(h, innerHeight) - EDGE);
      host.style.left = Math.min(Math.max(0, left), maxLeft) + "px";
      host.style.top = Math.min(Math.max(0, top), maxTop) + "px";
      host.style.right = "auto";
    }
    function savePos() {
      try { localStorage.setItem("pnt-pos", JSON.stringify({ left: host.style.left, top: host.style.top })); } catch {}
    }
    function resetPos() {
      host.style.left = "auto"; host.style.top = "12px"; host.style.right = "12px";
      try { localStorage.removeItem("pnt-pos"); } catch {}
    }

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
      place(e.clientX - drag.dx, e.clientY - drag.dy);
    });
    head.addEventListener("pointerup", () => {
      drag = null;
      savePos();
    });
    // Double-click the header to send it back to the top right.
    head.addEventListener("dblclick", (e) => { if (e.target.tagName !== "BUTTON") resetPos(); });

    function restorePos() {
      let pos = null;
      try { pos = JSON.parse(localStorage.getItem("pnt-pos") || "null"); } catch {}
      if (!pos || !pos.left) return;
      const left = parseFloat(pos.left), top = parseFloat(pos.top);
      if (Number.isFinite(left) && Number.isFinite(top)) place(left, top);
    }
    // A window that shrinks must not strand the panel outside it either.
    addEventListener("resize", () => {
      if (host.style.right === "auto") place(parseFloat(host.style.left) || 0, parseFloat(host.style.top) || 0);
    });

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
    // The chart page owns the spot, board and view controls -- it has chips for
    // all three, and a text box for filters no dropdown here could express. This
    // bar only says who is on show and how to get out, so the two can never
    // disagree. The filter below is just where the chart opens; change it there.
    const OPENING_FILTER = "opener,srp";
    function chartUrl() {
      const q = new URLSearchParams({ player: selected, filter: OPENING_FILTER, theme: "dark" });
      return `${state.server}/chart?${q}`;
    }
    // The chart page fetches its data once. When the player on show has played more
    // hands, tell it to fetch again rather than reloading the frame: a reload would
    // flash, and would throw away anything changed inside the chart itself.
    let chartHands = null; // that player's hand count when the chart last had data
    const handsOf = (alias) => state.hud?.seats.find((s) => s.alias === alias)?.stats?.hands ?? null;
    function refreshChart() {
      if (!selected || !$("chart").classList.contains("open")) return;
      const hands = handsOf(selected);
      if (hands == null || hands === chartHands) return;
      chartHands = hands;
      $("frame").contentWindow?.postMessage({ type: "pnt-refresh" }, new URL(state.server).origin);
    }
    // The chart page reports its URL and current player whenever the spot, view,
    // colour or player changes inside the frame, so the link out keeps up with
    // what is on screen.
    const paramOf = (url, key) => {
      try { return new URL(url).searchParams.get(key); } catch { return null; }
    };
    addEventListener("message", (e) => {
      if (e.source !== $("frame").contentWindow) return;
      if (e.origin !== new URL(state.server).origin) return;
      if (!e.data || e.data.type !== "pnt-url") return;
      $("ext").href = e.data.url;
      // The chart has a player dropdown of its own, over every player in the
      // database rather than only the ones seated here. Using it leaves the frame
      // showing someone other than the row that opened it, so follow it: otherwise
      // the bar names the wrong player, the wrong row stays highlighted, and
      // `refreshChart` keeps watching the hand count of a player no longer on
      // screen -- refetching when they act and never when the shown player does.
      // `player` falls back to the URL so an older server's page still tracks.
      const player = e.data.player ?? paramOf(e.data.url, "player");
      if (player && player !== selected) {
        selected = player;
        $("who").textContent = player;
        markSel();
        chartHands = handsOf(player);
      }
    });

    function showChart() {
      if (!selected) return;
      $("who").textContent = selected;
      const url = chartUrl();
      $("frame").src = url;
      $("ext").href = url;
      $("chart").classList.add("open");
      chartHands = handsOf(selected);
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
      // The HUD has no room to spell these out, so the hover text does it -- it is
      // the first place a new player meets the acronyms.
      const HEADS = [
        ["player", "Click to open their range chart below."],
        ["hands", "Hands they were dealt into. Every rate here is over some subset of these."],
        ["VPIP", "Voluntarily Put $ In Pot: how often they call, bet or raise preflop. "
          + "Blinds are forced and never count. High means loose."],
        ["PFR", "Pre-Flop Raise: how often they raise preflop. Close to VPIP means aggressive; "
          + "far below it means they call far more than they raise."],
        ["3Bet", "How often they re-raise someone's open."],
        ["F3B", "Fold to Three-Bet: they opened, someone re-raised, and they folded."],
        ["CBet", "Continuation bet: they raised preflop and then bet the flop first-in."],
        ["WTSD", "Went To Showdown: of the flops they saw, how often they were still there at the end."],
      ];
      for (const [h, tip] of HEADS) {
        const th = document.createElement("th"); th.textContent = h; th.title = tip;
        hr.appendChild(th);
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
        // Clicking the selected player again unselects them and closes the chart.
        tr.addEventListener("click", () => {
          if (selected === s.alias && $("chart").classList.contains("open")) {
            $("chart").classList.remove("open");
            selected = null;
            markSel();
            return;
          }
          selected = s.alias;
          markSel();
          showChart();
        });
        t.appendChild(tr);
      }
      body.appendChild(t);
      markSel();
      refreshChart();
      setStatus(state.paused ? "paused" : `capturing · ${state.inserted} new`);
    }

    function setStatus(text) { $("st").textContent = text; $("st").title = text; }

    document.documentElement.appendChild(host);
    // After it is in the page, so clamping can measure the panel.
    restorePos();
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
