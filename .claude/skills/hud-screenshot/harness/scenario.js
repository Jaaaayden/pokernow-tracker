// Screenshot harness: ?s=<state> puts the panel in one state. Loaded after sidepanel.js.
(async () => {
  const s = new URLSearchParams(location.search).get("s") || "loaded";
  const conf = JSON.parse(document.getElementById("harness-conf").textContent);
  document.body.style.width = "400px";
  const live = await fetch("live.json").then((r) => r.json());
  const hud = await fetch("hud.json").then((r) => r.json());
  const text = `connected · ${conf.hands} hands in db`;
  const base = { game: live.game_id, text, paused: false,
    hudEmpty: false, hudError: null, trackerDown: false, tableActing: null };
  const between = { game_id: live.game_id, hand: null, previous: live.previous ?? null };
  const warming = { ...base, text: `${text} · warming up` };
  const tick = () => new Promise((r) => setTimeout(r, 50));
  await tick();
  if (s === "loading") __push({ ...warming, liveData: live, hudData: null });
  if (s === "between") __push({ ...warming, liveData: between, hudData: null });
  if (s === "stuck-loading") __push({ ...warming, liveData: between, hudData: null });
  if (s === "stuck-down") __push({ ...base, text: "tracker not reachable", trackerDown: true, liveData: null, hudData: null });
  if (s === "stuck-tab") __push(null);
  if (s === "waiting") __push({ ...base, liveData: null, hudData: null });
  if (s === "empty") __push({ ...base, liveData: live, hudData: null, hudEmpty: true });
  if (s === "error") __push({ ...base, liveData: live, hudData: null, hudError: "harness: HTTP 500" });
  if (s === "loaded" || s === "update") __push({ ...base, liveData: live, hudData: hud });
  if (s === "sample") __push({ ...base, liveData: live, hudData: { ...hud, dataset: "sample" } });
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
