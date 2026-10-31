// Screenshot harness: ?s=<state> puts the panel in one state. Loaded after sidepanel.js.
(async () => {
  const s = new URLSearchParams(location.search).get("s") || "loaded";
  // A first install: no columns kept from an earlier /hud. Cleared before
  // sidepanel.js reads it, by loading the page once more.
  if (s === "fresh" && localStorage.getItem("pnt-cols")) {
    localStorage.removeItem("pnt-cols");
    location.reload();
    return;
  }
  const conf = JSON.parse(document.getElementById("harness-conf").textContent);
  document.body.style.width = s === "narrow" || s === "compact" ? "320px" : "400px";
  // Compact is remembered in the shared profile: only `compact` leaves it on.
  if ((localStorage.getItem("pnt-compact") === "1") !== (s === "compact")) document.getElementById("compact").click();
  const live = await fetch("live.json").then((r) => r.json());
  const hud = await fetch("hud.json").then((r) => r.json());
  const text = `connected · ${conf.hands} hands in db`;
  const base = { game: live.game_id, text, paused: false,
    hudEmpty: false, hudError: null, trackerDown: false, tableActing: null };
  const between = { game_id: live.game_id, hand: null, previous: live.previous ?? null };
  const warming = { ...base, text: `${text} · warming up` };
  // /health's `warming`, which the stuck-loading box asks for.
  window.__health.data.warming = s === "stuck-loading";
  const tick = () => new Promise((r) => setTimeout(r, 50));
  // A PLO or Double Board table: every hand here is left out of the stats.
  const unanalysed = (h, n) => ({ ...h, seats: h.seats.map((x) => ({ ...x,
    session: { hands: 0, not_analysed: n }, stats: { ...x.stats, not_analysed: (x.stats?.not_analysed || 0) + n } })) });
  await tick();
  if (s === "loading") __push({ ...warming, liveData: live, hudData: null });
  if (s === "between") __push({ ...warming, liveData: between, hudData: null });
  if (s === "stuck-loading" || s === "stuck-restart") __push({ ...warming, liveData: between, hudData: null });
  if (s === "stuck-down") __push({ ...base, text: "tracker not reachable", trackerDown: true, liveData: null, hudData: null });
  if (s === "stuck-tab" || s === "nogame") __push(null);
  if (s === "waiting") __push({ ...base, liveData: null, hudData: null });
  if (s === "empty") __push({ ...base, liveData: live, hudData: null, hudEmpty: true });
  if (s === "error") {
    __push({ ...base, liveData: live, hudData: null, hudError: "harness: HTTP 500" });
    await tick();
    // The next /live: the status line moves on, the error line stays.
    __push({ ...base, text: "capturing · 3 new · 9:41:07 PM", liveData: live, hudData: null, hudError: "harness: HTTP 500" });
  }
  if (s === "loaded" || s === "update" || s === "compact") __push({ ...base, liveData: live, hudData: hud });
  if (s === "fresh") __push({ ...warming, liveData: between, hudData: null });
  if (s === "settings") {
    __push({ ...base, liveData: live, hudData: hud });
    await tick();
    document.getElementById("gear").click();
  }
  if (s === "sample") __push({ ...base, liveData: live, hudData: { ...hud, dataset: "sample" } });
  if (s === "plo") {
    __push({ ...base, liveData: { ...live, analysed: false, players: live.players.map((p) => ({ ...p, resolved: null })) },
      hudData: unanalysed(hud, 109) });
  }
  if (s === "double") {
    const b1 = ["2h", "Qc", "5c", "Ks"], b2 = ["3d", "Tc", "Qs", "4c"];
    __push({ ...base, liveData: { ...live, hand_number: live.hand_number || 101, street: "turn", pot: 480, board: b1, boards: [b1, b2],
      analysed: false, players: live.players.map((p) => ({ ...p, resolved: null })) }, hudData: unanalysed(hud, 12) });
  }
  if (s === "narrow") {
    // 320px, a long status and the 🚩: the buttons stay on the first line. And
    // bb/100 among the columns, the widest figure.
    const previous = live.previous ?? { hand_number: 456, flagged: false };
    const bb = { key: "bb_per_100", label: "bb100", tip: "Big blinds won per 100 hands.", opp: null };
    __push({ ...warming, liveData: { ...live, previous }, hudData: { ...hud, columns: [...(hud.columns || []), bb] } });
  }
  if (s === "key") {
    __push({ ...base, liveData: live, hudData: hud });
    await tick();
    document.getElementById("keybtn").click();
  }
  if (s === "twins" && hud.seats.length > 1) {
    // One alias on two seats: the second seat's account merged into the first.
    const [a, b] = hud.seats;
    const seats = hud.seats.map((x) => (x === b ? { ...a, seat: b.seat, pn_id: b.pn_id } : x));
    const players = live.players.map((p) => (p.pn_id === b.pn_id ? { ...p, alias: a.alias, name: `${a.alias}_alt` } : p));
    __push({ ...base, liveData: { ...live, players }, hudData: { ...hud, seats } });
  }
})();
