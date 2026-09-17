/* Side panel: the HUD, beside the PokerNow page rather than drawn over it.
 *
 * It captures nothing. The content script on the game tab reports its counters,
 * status text and the /hud payload to the background worker, which
 * keeps them in session storage under `status:<tabId>`. This page shows the
 * report for the active tab in its window and redraws whenever it changes, so a
 * panel opened mid-game fills in at once and switching tabs switches games.
 *
 * Each seat is a card rather than a table row: the panel is narrow. A card can
 * open that player's range chart, embedded from the local server underneath.
 */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);

  const send = (msg) => new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage(msg, (r) => {
        if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
        else resolve(r || { ok: false, error: "no response" });
      });
    } catch (e) { resolve({ ok: false, error: String(e) }); }
  });

  const state = {
    server: "http://127.0.0.1:52000",
    tabId: null,
    snap: null,         // the latest report from the tab on show
    game: null,
    rev: -1,            // the report's rev at the last redraw
    hud: null,
  };
  let selected = null;

  // ------------------------------------------------------------- the tab --
  // The panel belongs to a window, not a tab: it shows whichever tab is active.
  const keyOf = (id) => `status:${id}`;
  async function track() {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const id = tab?.id ?? null;
    if (id !== state.tabId) { state.tabId = id; state.rev = -1; }
    const got = id == null ? {} : await chrome.storage.session.get(keyOf(id));
    show(got[keyOf(id)] || null);
  }
  chrome.tabs.onActivated.addListener(track);

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "session" && state.tabId != null && keyOf(state.tabId) in changes) {
      show(changes[keyOf(state.tabId)].newValue || null);
    }
    if (area === "sync" && changes.server) {
      state.server = String(changes.server.newValue || state.server).replace(/\/+$/, "");
    }
  });

  function show(snap) {
    state.snap = snap;
    $("nogame").hidden = !!snap;
    $("main").hidden = !snap;
    $("pause").hidden = !snap;
    if (!snap) {
      setStatus("");
      return;
    }
    // Another game in this tab, or another tab: nothing on show carries over.
    if (snap.game !== state.game) {
      state.game = snap.game;
      state.rev = -1;
      closeChart();
    }
    setStatus(snap.text || "");
    $("pause").textContent = snap.paused ? "▶" : "⏸";
    $("pause").title = snap.paused ? "resume capture" : "pause capture";
    // Only new data redraws the cards: a redraw on every status tick would throw
    // away the tooltip being read.
    if (snap.rev !== state.rev) {
      state.rev = snap.rev;
      state.hud = snap.hudData ?? null;
      render();
    }
  }

  function setStatus(text) { $("st").textContent = text; $("st").title = text; }

  $("pause").addEventListener("click", async () => {
    if (state.tabId == null || !state.snap) return;
    try {
      await chrome.tabs.sendMessage(state.tabId, { type: "pause", game: state.snap.game });
    } catch (e) {
      setStatus(`the game tab did not answer: ${e.message || e}`);
    }
  });

  // The settings that used to be the toolbar popup; the toolbar icon opens this panel now.
  $("gear").addEventListener("click", () => {
    const open = $("settings").hidden;
    if (open && !$("settings-frame").src) $("settings-frame").src = "popup.html";
    $("settings").hidden = !open;
    $("gear").setAttribute("aria-pressed", String(open));
  });

  // ----------------------------------------------------------------- chart --
  $("close").addEventListener("click", closeChart);
  // The chart page owns the spot, board and view controls -- it has chips for
  // all three, and a text box for filters no dropdown here could express. This
  // bar only says who is on show and how to get out, so the two can never disagree.
  function chartUrl(player, filter) {
    const q = new URLSearchParams({ player, theme: "dark" });
    if (filter) q.set("filter", filter);
    return `${state.server}/chart?${q}`;
  }
  const origin = () => new URL(state.server).origin;
  const isOpen = () => !$("chart").hidden;

  // The chart page fetches its data once. When the player on show has played more
  // hands, tell it to fetch again rather than reloading the frame: a reload would
  // flash, and would throw away anything changed inside the chart itself.
  let chartHands = null; // that player's hand count when the chart last had data
  const handsOf = (alias) => state.hud?.seats.find((s) => s.alias === alias)?.stats?.hands ?? null;
  function refreshChart() {
    if (!selected || !isOpen()) return;
    const hands = handsOf(selected);
    if (hands == null || hands === chartHands) return;
    chartHands = hands;
    $("frame").contentWindow?.postMessage({ type: "pnt-refresh" }, origin());
  }

  function updateWho() {
    $("who").textContent = selected || "";
  }
  // Put `alias` on show in the chart, in `filter` (all their hands when empty).
  // An open chart is told over postMessage rather than reloaded, so nothing
  // flashes and the view and colour mode picked in there survive.
  function selectPlayer(alias, filter) {
    selected = alias;
    chartHands = handsOf(alias);
    markSel();
    const spot = { player: alias, filter: filter || "" };
    if (!isOpen()) {
      const url = chartUrl(alias, filter);
      $("frame").src = url;
      $("ext").href = url;
      restoreChartHeight();
      $("chart").hidden = false;
    } else {
      $("frame").contentWindow?.postMessage({ type: "pnt-spot", ...spot }, origin());
    }
    updateWho();
  }
  function closeChart() {
    $("chart").hidden = true;
    selected = null;
    markSel();
    updateWho();
  }
  // The chart page reports its URL and current player whenever the spot, view,
  // colour or player changes inside the frame, so the link out keeps up with
  // what is on screen.
  const paramOf = (url, key) => {
    try { return new URL(url).searchParams.get(key); } catch { return null; }
  };
  addEventListener("message", (e) => {
    if (e.source !== $("frame").contentWindow) return;
    if (e.origin !== origin()) return;
    if (!e.data || e.data.type !== "pnt-url") return;
    $("ext").href = e.data.url;
    // The chart has a player dropdown of its own, over every player in the
    // database rather than only the ones seated here. Using it leaves the frame
    // showing someone other than the card that opened it, so follow it: otherwise
    // the bar names the wrong player, the wrong card stays highlighted, and
    // `refreshChart` keeps watching the hand count of a player no longer on
    // screen -- refetching when they act and never when the shown player does.
    // `player` falls back to the URL so an older server's page still tracks.
    const player = e.data.player ?? paramOf(e.data.url, "player");
    if (player && player !== selected) {
      selected = player;
      markSel();
      chartHands = handsOf(player);
      updateWho();
    }
  });

  // The chart remembers the height it was dragged to. Width is the panel's.
  function restoreChartHeight() {
    let h = NaN;
    try { h = parseFloat(localStorage.getItem("pnt-chart-h")); } catch {}
    if (Number.isFinite(h)) $("chart").style.height = Math.max(240, h) + "px";
  }
  // `resize` fires no event of its own; the observer sees the drag land. Opening
  // and closing fire it too, but only a drag writes the inline height.
  new ResizeObserver(() => {
    const c = $("chart");
    if (c.hidden || !c.style.height) return;
    try { localStorage.setItem("pnt-chart-h", c.style.height); } catch {}
  }).observe($("chart"));

  function markSel() {
    document.querySelectorAll(".card[data-alias]").forEach((el) => el.classList.toggle("sel", !!el.dataset.alias && el.dataset.alias === selected));
  }

  // ----------------------------------------------------------------- cards --
  const fmt = (v) => (v == null ? "–" : String(v));
  // Which `_opp` count sits under each figure; VPIP and PFR share one.
  const OPP_KEY = { vpip: "vpip", pfr: "vpip", "3bet": "3bet", fold_to_3bet: "fold_to_3bet",
    cbet_flop: "cbet_flop", wtsd: "wtsd", af_flop: "af_flop" };
  // The panel has no room to spell these out, so the hover text does it -- it is
  // the first place a new player meets the acronyms.
  const COLS = [
    ["hands", "hands", "Hands they were dealt into. Every rate here is over some subset of these."],
    ["vpip", "VPIP", "Voluntarily Put $ In Pot: how often they call, bet or raise preflop. "
      + "Blinds are forced and never count. High means loose."],
    ["pfr", "PFR", "Pre-Flop Raise: how often they raise preflop. Close to VPIP means aggressive; "
      + "far below it means they call far more than they raise."],
    ["3bet", "3Bet", "How often they re-raise someone's open."],
    ["fold_to_3bet", "F3B", "Fold to Three-Bet: they opened, someone re-raised, and they folded."],
    ["cbet_flop", "CBet", "Continuation bet: they raised preflop and then bet the flop first-in."],
    ["wtsd", "WTSD", "Went To Showdown: of the flops they saw, how often they were still there at the end."],
    ["af_flop", "Agg", "Aggression Frequency on the flop: of everything they did bar checking, how often "
      + "it was a bet or a raise rather than a call or a fold."],
  ];
  // A session figure is flagged as drifting when it has some sample behind it
  // and sits well away from the lifetime one. Neither direction is "good" for
  // a VPIP, so it is one colour, not red and green.
  const DRIFT_POINTS = 10, DRIFT_MIN_OPP = 10;
  const sample = (st, k) => (k === "hands" ? null : st._opp?.[OPP_KEY[k]]);

  // One card per seat, from the HUD payload, keyed by PokerNow ID.
  const rows = () => state.hud?.seats ?? [];

  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  function render() {
    const body = $("body");
    body.replaceChildren();
    const seats = [...rows()].sort((a, b) => a.seat - b.seat);
    // An older server sends lifetime only; the cards then print just that.
    const hasSession = seats.some((s) => s.session);
    $("legend").hidden = !hasSession;
    if (!seats.length) {
      body.append(el("div", "empty", state.hud ? "no one dealt in yet" : "waiting for the first hand…"));
      return;
    }
    for (const s of seats) body.appendChild(card(s));
    markSel();
    refreshChart();
  }

  function card(s) {
    const st = s.stats || {};
    const c = el("div", "card");
    c.dataset.alias = s.alias || "";
    c.title = `seat ${s.seat} · ${s.pn_id}`;

    // Who.
    const who = el("div", "who");
    const name = el("span", "name", s.alias || s.name || s.pn_id);
    name.appendChild(el("small", null, `#${s.seat}`));
    who.appendChild(name);
    c.appendChild(who);

    // The numbers: this session, then lifetime in grey.
    const stats = el("div", "stats");
    const ss = s.session;
    for (const [k, label, tip] of COLS) {
      const cell = el("span", "stat");
      cell.appendChild(el("span", "k", label));
      if (!ss) {
        cell.appendChild(el("span", null, fmt(st[k])));
        cell.title = tip;
      } else {
        const now = el("span", null, fmt(ss[k]));
        const nOpp = sample(ss, k), lOpp = sample(st, k);
        if (typeof ss[k] === "number" && typeof st[k] === "number" && k !== "hands"
            && (nOpp ?? 0) >= DRIFT_MIN_OPP && Math.abs(ss[k] - st[k]) >= DRIFT_POINTS) {
          now.className = "drift";
        }
        cell.append(now, el("small", "life", fmt(st[k])));
        const of = (v, n) => (n == null ? fmt(v) : `${fmt(v)}% of ${n}`);
        cell.title = `${tip}\n\n` + (k === "hands"
          ? `this session: ${fmt(ss[k])} hands · lifetime: ${fmt(st[k])}`
          : `this session: ${of(ss[k], nOpp)} · lifetime: ${of(st[k], lOpp)}`);
      }
      stats.appendChild(cell);
    }
    c.appendChild(stats);

    // Clicking a card shows that player in the chart, on all their hands. A player
    // without an identity yet has no chart to open.
    c.addEventListener("click", () => {
      if (!s.alias) return;
      selectPlayer(s.alias, "");
    });
    return c;
  }

  // ----------------------------------------------------------------- start --
  (async () => {
    const s = await send({ type: "settings" });
    if (s.ok) state.server = s.data.server;
    await track();
  })();
})();
