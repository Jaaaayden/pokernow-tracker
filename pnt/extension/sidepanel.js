/* Side panel: the HUD, beside the PokerNow page rather than drawn over it.
 *
 * It captures nothing. The content script on the game tab reports its counters,
 * status text and the /hud and /live payloads to the background worker, which
 * keeps them in session storage under `status:<tabId>`. This page shows the
 * report for the active tab in its window and redraws whenever it changes, so a
 * panel opened mid-game fills in at once and switching tabs switches games.
 *
 * Each seat is a card rather than a table row: the panel is narrow. A card can
 * open that player's range chart, embedded from the local server underneath;
 * the chart follows the live action (spot.js decides whom and when).
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
    hud: null, live: null,
    acting: null,       // the name on the table's decision-current seat, ahead of /live
    follow: true,       // the chart follows the live spot
    compact: false,     // the stats table only: no tags, spot lines or chart
    sentSpots: [],      // the last few spots posted into the chart, to tell an echo from an edit
    flagged: null,      // {game, n, flagged}: what the 🚩 last wrote, ahead of /live
  };
  let selected = null, pinned = null;
  let heldAt = null; // the moment a card was clicked at; see followSpot
  // The data the panel is drawn from: a new /live or HUD report, or the action
  // moving on the table before the log has caught up.
  const moment = () => `${state.rev}|${state.acting ?? ""}`;

  // ------------------------------------------------------------- the tab --
  // The panel belongs to a window, not a tab: it shows whichever tab is active.
  // Framed on the game page instead (float mode, content.js), it belongs to that
  // page's tab, which content.js names in `?tab=`.
  const params = new URLSearchParams(location.search);
  const EMBED = params.get("embed") === "1";
  const EMBED_TAB = EMBED ? Number(params.get("tab")) : NaN;
  const keyOf = (id) => `status:${id}`;
  async function track() {
    let id;
    if (EMBED) id = Number.isInteger(EMBED_TAB) ? EMBED_TAB : null;
    else id = (await chrome.tabs.query({ active: true, currentWindow: true }))[0]?.id ?? null;
    if (id !== state.tabId) { state.tabId = id; state.rev = -1; }
    const got = id == null ? {} : await chrome.storage.session.get(keyOf(id));
    show(got[keyOf(id)] || null);
  }
  if (!EMBED) chrome.tabs.onActivated.addListener(track);

  // ------------------------------------------------------------ float mode --
  // The header drags the box it is framed in. The frame moves under the pointer
  // as it goes, so the steps are measured on the screen, not in the frame, and
  // pointer capture keeps them coming once the pointer is outside it.
  if (EMBED) {
    document.body.classList.add("embed");
    $("hide").hidden = false;
    $("hide").addEventListener("click", () => parent.postMessage({ type: "pnt-hide" }, "*"));
    const head = document.querySelector(".head");
    let last = null;
    head.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || e.target.closest("button")) return;
      last = { x: e.screenX, y: e.screenY };
      head.setPointerCapture(e.pointerId);
      e.preventDefault();
    });
    head.addEventListener("pointermove", (e) => {
      if (!last) return;
      const dx = e.screenX - last.x, dy = e.screenY - last.y;
      if (!dx && !dy) return;
      last = { x: e.screenX, y: e.screenY };
      parent.postMessage({ type: "pnt-drag", dx, dy }, "*");
    });
    const drop = () => {
      if (!last) return;
      last = null;
      parent.postMessage({ type: "pnt-drop" }, "*");
    };
    head.addEventListener("pointerup", drop);
    head.addEventListener("pointercancel", drop);
  }

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
      $("flag").hidden = true;
      $("live").textContent = "";
      setStatus("");
      return;
    }
    // Another game in this tab, or another tab: nothing on show carries over.
    if (snap.game !== state.game) {
      state.game = snap.game;
      state.sentSpots = [];
      state.rev = -1;
      closeChart();
    }
    setStatus(snap.text || "");
    $("pause").textContent = snap.paused ? "▶" : "⏸";
    $("pause").title = snap.paused ? "resume capture" : "pause capture";
    // Only new data redraws the cards: a redraw on every status tick would throw
    // away the tooltip being read. The table's player to act counts as new data --
    // it arrives a poll or more before /live says the same.
    const acting = snap.tableActing ?? null;
    if (snap.rev !== state.rev || acting !== state.acting) {
      state.rev = snap.rev;
      state.acting = acting;
      state.hud = snap.hudData ?? null;
      state.live = snap.liveData ?? null;
      render();
      followSpot();
    }
    paintFlag();
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

  // ------------------------------------------------------------------ flag --
  // 🚩 flags the hand that last ended for the chart's Manual review tab; pressed
  // means flagged, and a second click clears it. /live says which hand that is and
  // whether it is flagged, but it is read again only when new lines arrive, so
  // what the button last wrote stands in for it until /live names the same hand.
  function previousHand() {
    const p = state.live?.previous;
    if (!p || !state.snap) return null;
    const mine = state.flagged;
    const flagged = mine && mine.game === state.snap.game && mine.n === p.hand_number ? mine.flagged : p.flagged;
    return { n: p.hand_number, flagged };
  }
  function paintFlag() {
    const p = previousHand();
    $("flag").hidden = !p;
    if (!p) return;
    $("flag-n").textContent = `#${p.n}`;
    $("flag").setAttribute("aria-pressed", String(p.flagged));
    $("flag").title = p.flagged
      ? `hand #${p.n} is flagged for manual review — click to unflag`
      : `flag hand #${p.n}, the last to finish, for manual review in the chart`;
  }
  $("flag").addEventListener("click", async () => {
    const p = previousHand();
    if (!p || $("flag").disabled) return;
    const game = state.snap.game;
    $("flag").disabled = true;
    const r = await send({ type: "flag", game_id: game, hand_number: p.n, flagged: !p.flagged });
    $("flag").disabled = false;
    if (!r.ok) { setStatus(`could not flag hand #${p.n}: ${r.error}`); return; }
    state.flagged = { game, n: p.n, flagged: r.data.flagged };
    paintFlag();
    // An open chart may be on its Manual review tab: it fetches again, quietly.
    if (isOpen()) $("frame").contentWindow?.postMessage({ type: "pnt-refresh" }, origin());
  });

  // The settings that used to be the toolbar popup; the toolbar icon opens this panel now.
  $("gear").addEventListener("click", () => {
    const open = $("settings").hidden;
    if (open && !$("settings-frame").src) $("settings-frame").src = "popup.html";
    $("settings").hidden = !open;
    $("gear").setAttribute("aria-pressed", String(open));
    fit();
  });

  // ----------------------------------------------------------------- chart --
  $("close").addEventListener("click", closeChart);
  $("follow").addEventListener("click", () => setFollow(!state.follow));
  // The pin keeps the chart on one player while the action goes round; without
  // it the chart goes to whoever is to act.
  $("pin").addEventListener("click", () => {
    pinned = pinned ? null : selected;
    heldAt = null;
    updateWho();
    followSpot();
  });

  // The chart page owns the spot, board and view controls -- it has chips for
  // all three, and a text box for filters no dropdown here could express. This
  // bar only says who is on show, whether the live action is being followed,
  // and how to get out, so the two can never disagree.
  // `view` is optional: {by, street, kind} when a tag chip wants the hands
  // behind it shown as made hands or by size rather than on the preflop grid.
  function chartUrl(player, filter, view) {
    const q = new URLSearchParams({ player, theme: "dark" });
    if (filter) q.set("filter", filter);
    for (const k of ["by", "street", "kind"]) if (view?.[k]) q.set(k, view[k]);
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
    $("who").textContent = !selected ? "" : pinned ? `pinned: ${selected}` : state.follow ? `following: ${selected}` : selected;
    $("follow").setAttribute("aria-pressed", String(state.follow));
    $("pin").setAttribute("aria-pressed", String(!!pinned));
  }
  function setFollow(on) {
    state.follow = on;
    try { localStorage.setItem("pnt-follow", on ? "1" : "0"); } catch {}
    updateWho();
    if (on) followSpot();
  }
  function remember(spot) {
    state.sentSpots = [spot, ...state.sentSpots].slice(0, 5);
  }
  // Put `alias` on show in the chart, in `filter` (all their hands when empty).
  // An open chart is told over postMessage rather than reloaded, so nothing
  // flashes and the view and colour mode picked in there survive.
  function selectPlayer(alias, filter, view) {
    selected = alias;
    chartHands = handsOf(alias);
    markSel();
    const spot = { player: alias, filter: filter || "" };
    remember(spot);
    if (!isOpen()) {
      const url = chartUrl(alias, filter, view);
      $("frame").src = url;
      $("ext").href = url;
      restoreChartHeight();
      $("chart").hidden = false;
    } else {
      $("frame").contentWindow?.postMessage({ type: "pnt-spot", ...spot, ...(view || {}) }, origin());
    }
    updateWho();
  }
  // The live spot, into the chart, whenever it moves. spot.js says whether it
  // did: a player with no data behind their spot is shown on all their hands.
  // The table's player to act leads it: the chart moves to them as soon as the
  // table shows it, and takes their spot once /live has caught up.
  // A card just clicked holds until the data next moves, so the frame loading
  // does not snatch it straight back to the player to act.
  function followSpot() {
    if (!state.follow || !isOpen()) return;
    if (!pinned && heldAt === moment()) return;
    heldAt = null;
    const s = PNT.nextSpot(state.live, pinned, state.sentSpots[0], state.acting);
    if (s) selectPlayer(s.player, s.filter);
  }
  function closeChart() {
    $("chart").hidden = true;
    selected = null;
    pinned = null;
    heldAt = null;
    markSel();
    updateWho();
  }
  // Once the frame has loaded, the spot may already have moved on.
  $("frame").addEventListener("load", followSpot);

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
    // A spot the HUD did not ask for is the user reaching into the chart --
    // typing a filter, pressing a chip, picking a player. Following would snatch
    // it away on the next action, so it stops here until switched back on.
    if (state.follow && state.sentSpots.length && !PNT.isEcho(e.data.url, state.sentSpots)) setFollow(false);
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
      if (pinned) pinned = player;
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

  // One card per seat. The live hand's roster when there is one -- it is the
  // table as it is now -- else the last completed hand's. Stats come from the
  // HUD payload by PokerNow ID; a player dealt in for the first time has none
  // yet and still gets a card, named from the log.
  function rows() {
    const hud = new Map((state.hud?.seats ?? []).map((s) => [s.pn_id, s]));
    const live = state.live?.players;
    if (!live) return [...hud.values()].map((s) => ({ ...s, live: null }));
    return live.map((p) => {
      const s = hud.get(p.pn_id) || { pn_id: p.pn_id, alias: p.alias, stats: { hands: 0 }, session: hud.size ? { hands: 0 } : null, tags: null };
      return { ...s, seat: p.seat, alias: s.alias || p.alias, name: p.name, live: p };
    });
  }

  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  function render() {
    const body = $("body");
    body.replaceChildren();
    const seats = rows().sort((a, b) => a.seat - b.seat);
    const lv = state.live?.hand_number ? state.live : null;
    $("live").textContent = lv
      ? `hand #${lv.hand_number} · ${lv.street}${lv.pot ? ` · pot ${lv.pot}` : ""}${lv.board?.length ? ` · ${lv.board.join(" ")}` : ""}`
      : "";
    // An older server sends lifetime only; the cards then print just that.
    const hasSession = seats.some((s) => s.session);
    // The compact table carries the legend in its own header.
    $("legend").hidden = !hasSession || state.compact;
    if (!seats.length) {
      body.append(el("div", "empty", state.hud || state.live ? "no one dealt in yet" : "waiting for the first hand…"));
    } else if (state.compact) {
      body.append(grid(seats, lv, hasSession));
    } else {
      for (const s of seats) body.appendChild(card(s, lv));
      markSel();
      refreshChart();
    }
    fit();
  }

  // ---------------------------------------------------------------- compact --
  // The – in the header strips the panel down to the stats table; + brings the
  // cards, tags and chart back. Remembered, like the chart's height.
  function setCompact(on) {
    state.compact = on;
    try { localStorage.setItem("pnt-compact", on ? "1" : "0"); } catch {}
    $("compact").textContent = on ? "+" : "–";
    $("compact").title = on ? "expand: cards, tags and the range chart" : "compact: one line of stats per player, no tags or chart";
    if (on) closeChart();
    render();
    widthPending = on;
    if (EMBED && !on) parent.postMessage({ type: "pnt-full" }, "*");
    fit();
  }
  $("compact").addEventListener("click", () => setCompact(!state.compact));

  // On the game page, a compact HUD is only as tall as the table: the box is
  // told the height after every redraw. Its width is the table's on the way into
  // compact -- once there is a table to measure -- and after that only grows, so
  // a width dragged by hand stays.
  let widthPending = false;
  function fit() {
    if (!EMBED || !state.compact) return;
    const g = document.querySelector(".grid");
    const msg = { type: "pnt-fit", h: document.body.offsetHeight };
    if (g && (widthPending || g.scrollWidth > innerWidth)) {
      msg.w = g.scrollWidth;
      widthPending = false;
    }
    parent.postMessage(msg, "*");
  }

  // One figure into `into`: this session, then lifetime in grey, the session one
  // blue when it drifts. Returns the hover text. The cards and the compact table
  // both use it, so the two can never read a figure differently.
  function figure(s, k, tip, into) {
    const st = s.stats || {}, ss = s.session;
    if (!ss) {
      into.appendChild(el("span", null, fmt(st[k])));
      return tip;
    }
    const now = el("span", null, fmt(ss[k]));
    const nOpp = sample(ss, k), lOpp = sample(st, k);
    if (typeof ss[k] === "number" && typeof st[k] === "number" && k !== "hands"
        && (nOpp ?? 0) >= DRIFT_MIN_OPP && Math.abs(ss[k] - st[k]) >= DRIFT_POINTS) {
      now.className = "drift";
    }
    into.append(now, el("small", "life", fmt(st[k])));
    const of = (v, n) => (n == null ? fmt(v) : `${fmt(v)}% of ${n}`);
    return `${tip}\n\n` + (k === "hands"
      ? `this session: ${fmt(ss[k])} hands · lifetime: ${fmt(st[k])}`
      : `this session: ${of(ss[k], nOpp)} · lifetime: ${of(st[k], lOpp)}`);
  }

  // Compact: every player in one table, a row each, and nothing else -- the
  // player to act in yellow, the folded dimmed, the rest is the numbers.
  function grid(seats, lv, hasSession) {
    const table = el("table");
    const head = el("thead");
    if (hasSession) {
      const tr = el("tr");
      tr.append(el("th"));
      const g = el("th", "group", "this session · lifetime");
      g.colSpan = COLS.length;
      g.title = $("legend").title;
      tr.append(g);
      head.append(tr);
    }
    const tr = el("tr");
    tr.append(el("th", null, "player"));
    for (const [, label, tip] of COLS) {
      const th = el("th", null, label);
      th.title = tip;
      tr.append(th);
    }
    head.append(tr);
    const body = el("tbody");
    for (const s of seats) {
      const row = el("tr");
      if (s.live) {
        if (lv && PNT.actingId(lv, state.acting) === s.pn_id) row.classList.add("act");
        if (s.live.folded) row.classList.add("out");
      }
      const name = el("td", "name", s.alias || s.name || s.pn_id);
      name.appendChild(el("small", null, `#${s.seat}`));
      name.title = `${s.alias || s.name || s.pn_id} · seat ${s.seat} · ${s.pn_id}`;
      row.append(name);
      for (const [k, , tip] of COLS) {
        const td = el("td");
        td.title = figure(s, k, tip, td);
        row.append(td);
      }
      body.append(row);
    }
    table.append(head, body);
    const wrap = el("div", "grid");
    wrap.append(table);
    return wrap;
  }

  function card(s, lv) {
    const c = el("div", "card");
    c.dataset.alias = s.alias || "";
    c.title = `seat ${s.seat} · ${s.pn_id}`;
    if (s.live) {
      if (lv && PNT.actingId(lv, state.acting) === s.pn_id) c.classList.add("act");
      if (s.live.folded) c.classList.add("out");
    }

    // Who and where.
    const who = el("div", "who");
    const name = el("span", "name", s.alias || s.name || s.pn_id);
    name.appendChild(el("small", null, `#${s.seat}`));
    who.appendChild(name);
    if (s.live) {
      const pos = el("span", "pos", s.live.position || "");
      pos.title = "Position this hand. Blank when the button or a blind is dead.";
      const stack = el("span", "stack", fmt(s.live.stack));
      stack.title = s.live.committed ? `${s.live.committed} in the pot this hand` : "Chips behind, after what they have put in this hand.";
      who.append(pos, stack);
    }
    c.appendChild(who);

    // Tag chips, every one: they wrap onto as many lines as they need. A chip
    // opens the hands behind the tag: that pins the player and switches follow
    // off, since the live action would otherwise replace the spot on the next report.
    // Streaky tags -- ones a single session carries -- come last, dimmed.
    const tags = el("div", "tags");
    const { shown } = PNT.tagChips(s.tags?.tags, Infinity);
    for (const tag of [...shown, ...(s.tags?.streaky || [])]) {
      const chip = el("span", `tag kind-${tag.kind}${tag.carried_by ? " streaky" : ""}`, tag.label);
      chip.title = PNT.tagTitle(tag);
      chip.addEventListener("click", (e) => {
        e.stopPropagation();
        if (!s.alias) return;
        pinned = s.alias;
        setFollow(false);
        selectPlayer(s.alias, tag.filter, { by: tag.by, street: tag.street, kind: tag.bet_kind });
      });
      tags.appendChild(chip);
    }
    c.appendChild(tags);

    // The numbers: this session, then lifetime in grey.
    const stats = el("div", "stats");
    for (const [k, label, tip] of COLS) {
      const cell = el("span", "stat");
      cell.appendChild(el("span", "k", label));
      cell.title = figure(s, k, tip, cell);
      stats.appendChild(cell);
    }
    c.appendChild(stats);

    // One line: what they have done this hand so far, in grey, then where they
    // are now and how they have played that spot before. A folded player's
    // line is only the path, which ends in the fold.
    if (s.live) {
      const path = PNT.pathText(s.live);
      const now = PNT.spotText(s.live);
      const spot = el("div", "spot");
      if (path.text) spot.appendChild(el("span", "path", s.live.folded ? path.text : `${path.text} › `));
      if (!s.live.folded || !path.text) spot.append(now.text);
      spot.title = [path.title, now.title].filter(Boolean).join("\n\n");
      if (s.live.node && !s.live.node.decision && !s.live.folded) spot.classList.add("pending");
      c.appendChild(spot);
    }

    // Clicking a card shows that player in the chart. Following goes on: the
    // chart stays on them only until the action next moves, then goes back to
    // whoever is to act. With a pin in, the pin moves to them instead -- the 📌
    // in the chart bar is what pins and unpins. A player without an identity
    // yet has no chart to open.
    c.addEventListener("click", () => {
      if (!s.alias) return;
      heldAt = moment();
      if (pinned) pinned = s.alias;
      const spot = state.follow && s.live?.resolved ? s.live.resolved.filter : "";
      selectPlayer(s.alias, spot);
    });
    return c;
  }

  // ----------------------------------------------------------------- start --
  (async () => {
    try { state.follow = localStorage.getItem("pnt-follow") !== "0"; } catch {}
    let compact = false;
    try { compact = localStorage.getItem("pnt-compact") === "1"; } catch {}
    setCompact(compact);
    updateWho();
    const s = await send({ type: "settings" });
    if (s.ok) state.server = s.data.server;
    await track();
  })();
})();
