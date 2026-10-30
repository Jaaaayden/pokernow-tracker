/* Every page's loading signal: the front page, stats, chart, all-in, pots,
 * tendencies and players all load this first, in <head>.
 *
 * It wraps fetch, so no page has to opt in. While any request is out, a thin bar
 * moves along the top; past PILL_MS a pill counts the seconds, so a slow answer
 * reads as working, not frozen; past SLOW_MS a box says what to try, in order.
 * A request that cannot reach the server at all says so at once. Each clears
 * itself when the requests do.
 */
(() => {
  "use strict";
  const PILL_MS = 2000, SLOW_MS = 20000;
  // Requests nobody is waiting on: the native file picker, which waits on a
  // person, and the chart's chip counts, badges that fill in behind the page.
  const SKIP = [/\/settings\/paths\/browse/, /\/counts(\?|$)/];
  const EMBED = window.parent !== window; // the chart inside the HUD's side panel

  const out = new Map(); // request id -> started at
  let next = 0, down = false, shut = null, ui = null;

  const realFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const url = typeof input === "string" ? input : input?.url || String(input);
    if (SKIP.some((r) => r.test(url))) return realFetch(input, init);
    const id = ++next;
    out.set(id, Date.now());
    paint();
    return realFetch(input, init).then(
      (r) => { settle(id, false); return r; },
      (e) => { settle(id, e?.name !== "AbortError"); throw e; },
    );
  };

  // A failed connection is a TypeError with no response; an abort is the page's
  // own doing (a newer request replaced it), not the server's.
  function settle(id, failed) {
    out.delete(id);
    if (failed) down = true;
    else if (!out.size) down = false;
    paint();
  }

  const CSS = `
  #pnt-load-bar { position: fixed; top: 0; left: 0; right: 0; height: 2px; z-index: 2147483000;
    overflow: hidden; pointer-events: none; opacity: 0; transition: opacity .2s; }
  #pnt-load-bar.on { opacity: 1; }
  #pnt-load-bar::before { content: ""; position: absolute; top: 0; bottom: 0; width: 30%;
    background: var(--accent, #3987e5); animation: pnt-load-slide 1.1s ease-in-out infinite; }
  @keyframes pnt-load-slide { from { left: -30%; } to { left: 100%; } }
  #pnt-load-pill { position: fixed; right: 12px; bottom: 12px; z-index: 2147483000; display: none;
    font: 12px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; color: var(--ink-2, #c3c2b7);
    background: var(--surface, #1a1a19); border: 1px solid var(--ring, rgba(255,255,255,.1));
    border-radius: 999px; padding: 3px 10px; font-variant-numeric: tabular-nums; }
  #pnt-load-pill.on { display: block; }
  #pnt-load-pill::before { content: ""; display: inline-block; width: 8px; height: 8px; margin-right: 6px;
    border-radius: 50%; border: 2px solid var(--accent, #3987e5); border-right-color: transparent;
    vertical-align: -1px; animation: pnt-load-spin .8s linear infinite; }
  @keyframes pnt-load-spin { to { transform: rotate(360deg); } }
  #pnt-load-box { position: fixed; right: 12px; bottom: 44px; z-index: 2147483000; display: none;
    max-width: min(360px, calc(100vw - 24px)); box-sizing: border-box;
    font: 13px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; color: var(--ink, #fff);
    background: var(--surface, #1a1a19); border: 1px solid var(--down, #e66e6d); border-radius: 10px;
    padding: 10px 12px; box-shadow: 0 4px 18px rgba(0,0,0,.25); }
  #pnt-load-box.on { display: block; }
  #pnt-load-box b { color: var(--down, #e66e6d); }
  #pnt-load-box ol { margin: 6px 0 0; padding-left: 18px; color: var(--ink-2, #c3c2b7); }
  #pnt-load-box li { margin: 3px 0; }
  #pnt-load-box code { font: 12px ui-monospace, Consolas, monospace; background: var(--mid, #383835);
    padding: 1px 5px; border-radius: 4px; color: var(--ink, #fff); }
  #pnt-load-box button { font: inherit; font-size: 12px; color: var(--ink, #fff); background: none; cursor: pointer;
    border: 1px solid var(--ring, rgba(255,255,255,.2)); border-radius: 5px; padding: 0 7px; margin-left: 4px; }
  #pnt-load-box button:hover { border-color: var(--accent, #3987e5); }
  #pnt-load-box .shut { float: right; border: 0; margin: -2px -4px 0 6px; color: var(--muted, #898781); }
  @media (prefers-reduced-motion: reduce) {
    #pnt-load-bar::before { animation: none; left: 0; width: 100%; opacity: .5; }
    #pnt-load-pill::before { animation: none; }
  }`;

  function build() {
    if (ui || !document.body) return ui;
    const style = document.createElement("style");
    style.textContent = CSS;
    document.head.appendChild(style);
    const bar = document.createElement("div"); bar.id = "pnt-load-bar";
    const pill = document.createElement("div"); pill.id = "pnt-load-pill"; pill.setAttribute("role", "status");
    const box = document.createElement("div"); box.id = "pnt-load-box"; box.setAttribute("role", "alert");
    document.body.append(bar, pill, box);
    ui = { bar, pill, box, kind: null };
    return ui;
  }

  const el = (tag, text) => { const n = document.createElement(tag); if (text != null) n.textContent = text; return n; };
  function cmd(text) {
    const span = el("span");
    const copy = el("button", "copy");
    copy.type = "button";
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(text); copy.textContent = "copied"; } catch { copy.textContent = "select it"; }
    });
    span.append(el("code", text), copy);
    return span;
  }
  function reloadButton() {
    const b = el("button", "Reload page");
    b.type = "button";
    b.addEventListener("click", () => location.reload());
    return b;
  }
  const li = (...parts) => { const l = el("li"); l.append(...parts); return l; };

  function fill(box, kind, secs) {
    box.replaceChildren();
    const x = el("button", "✕");
    x.type = "button"; x.className = "shut"; x.title = "hide this";
    x.addEventListener("click", () => { shut = kind; box.classList.remove("on"); });
    const steps = el("ol");
    if (kind === "down") {
      box.append(x, el("b", "Can't reach the tracker."), " The server has stopped or restarted.");
      steps.append(li("In a terminal: ", cmd("pnt service status")),
        li("Stopped or stuck: ", cmd("pnt service restart")), li("Then ", reloadButton()));
    } else {
      const head = el("b", "Still loading after ");
      const n = el("span", String(secs));
      n.className = "secs";
      head.append(n, " s.");
      box.append(x, head, " A big question on a cold server can take a minute.");
      steps.append(li("Still stuck: ", reloadButton()), li(cmd("pnt service restart"), ", then reload"));
      if (EMBED) steps.append(li("Still stuck in the HUD: the panel's ", el("b", "Reload extension"),
        ", or ↻ on chrome://extensions"));
    }
    box.append(steps);
  }

  function paint() {
    const u = build();
    if (!u) return;
    const now = Date.now();
    const oldest = out.size ? Math.min(...out.values()) : null;
    const age = oldest == null ? 0 : now - oldest;
    u.bar.classList.toggle("on", out.size > 0);
    u.pill.classList.toggle("on", age >= PILL_MS);
    if (age >= PILL_MS) u.pill.textContent = `loading · ${Math.floor(age / 1000)} s`;
    const kind = down ? "down" : age >= SLOW_MS ? "slow" : null;
    if (kind !== shut) shut = null;
    const show = kind && kind !== shut ? kind : null;
    // Filled once per case; after that only the count moves, so a copy button's
    // "copied" survives.
    if (show && show !== u.kind) fill(u.box, show, Math.floor(age / 1000));
    else if (show === "slow") u.box.querySelector(".secs").textContent = String(Math.floor(age / 1000));
    u.kind = show;
    u.box.classList.toggle("on", !!show);
  }

  setInterval(() => { if (out.size || down || ui?.kind) paint(); }, 1000);
  document.addEventListener("DOMContentLoaded", paint);

  // ---- What every page shares: the nav, the sample banner, the theme, and the
  // number formats. Exposed as window.pntPage for the page's own script.
  const theme = new URLSearchParams(location.search).get("theme");
  const THEME = theme === "light" || theme === "dark" ? theme : null;
  if (THEME) document.documentElement.dataset.theme = THEME;
  // `path` with `params` and the page's ?theme, which every link carries along.
  const href = (path, params) => {
    const q = new URLSearchParams(params || {});
    if (THEME) q.set("theme", THEME);
    const s = q.toString();
    return s ? path + (path.includes("?") ? "&" : "?") + s : path;
  };

  // One list, one order, on every page. [key, path, label]; each link's id is
  // `<key>link`, which the pages set to carry their spot or player along.
  const PAGES = [
    ["home", "/", "tracker"],
    ["stats", "/stats.html", "stats"],
    ["chart", "/chart", "range chart"],
    ["pots", "/pots.html", "biggest pots"],
    ["allin", "/allin.html", "all-in EV"],
    ["tendencies", "/tendencies.html", "tendencies"],
    ["players", "/players.html", "players"],
  ];
  // :where() keeps these at no specificity, so a page's own rule wins.
  const PAGE_CSS = `
  :where(.pnt-nav) { display: flex; flex-wrap: wrap; gap: 2px 16px; margin: 0 0 12px; font-size: 13px; }
  :where(.pnt-nav) a { color: var(--accent, #3987e5); text-decoration: none; }
  :where(.pnt-nav) a:hover { text-decoration: underline; }
  :where(.pnt-nav) a[aria-current=page] { color: var(--ink, #fff); font-weight: 600; }
  :where(.pnt-sample) { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 12px; margin: 0 0 12px;
    padding: 6px 12px; border-radius: 8px; font-size: 13px; color: var(--ink, #fff);
    border: 1px solid var(--accent, #3987e5); background: color-mix(in srgb, var(--accent, #3987e5) 10%, transparent); }
  :where(.pnt-sample) a { color: var(--accent, #3987e5); }`;
  let pageStyled = false;
  function pageStyle() {
    if (pageStyled) return;
    pageStyled = true;
    const s = document.createElement("style");
    s.textContent = PAGE_CSS;
    document.head.appendChild(s);
  }

  // Fills <nav class="pnt-nav" id="crumb" data-page="..."> once; the page's
  // script calls it first thing, so its links are there to set.
  function nav() {
    const box = document.getElementById("crumb");
    if (!box || box.childElementCount) return box;
    pageStyle();
    const here = box.dataset.page;
    for (const [key, path, label] of PAGES) {
      const a = el("a", label);
      a.id = key + "link";
      a.href = href(path);
      if (key === here) a.setAttribute("aria-current", "page");
      box.append(a);
    }
    return box;
  }

  // While the pages show the sample, every page says so, not just the front one
  // (it has a banner of its own, with the switch on it).
  async function sampleBanner() {
    if (EMBED || document.getElementById("sample-banner")) return;
    try {
      const r = await realFetch("/settings/dataset");
      if (!r.ok || (await r.json()).active !== "sample") return;
    } catch { return; }
    pageStyle();
    const b = el("div");
    b.className = "pnt-sample";
    b.id = "pnt-sample";
    b.setAttribute("role", "status");
    const back = el("a", "back to your hands ›");
    back.href = href("/") + "#files-h";
    b.append(el("b", "Sample data"), " anonymized games, not yours.", back);
    const at = document.getElementById("crumb");
    if (at) at.after(b);
    else (document.querySelector("main") || document.body).prepend(b);
  }

  const DASH = "–";
  const ok = (v) => v != null && Number.isFinite(Number(v));
  window.pntPage = {
    theme: THEME, href, nav, DASH,
    // A count with thousands separators.
    int: (v) => (ok(v) ? Number(v).toLocaleString() : DASH),
    // A rate in percent, one decimal: 29.0%.
    pct: (v) => (ok(v) ? Number(v).toFixed(1) + "%" : DASH),
    // bb/100 and other signed figures, one decimal: +38.4.
    signed: (v, dp = 1) => (ok(v) ? (v > 0 ? "+" : "") + Number(v).toFixed(dp) : DASH),
    // Names in the order a person reads them: player 2 before player 10.
    byName: (a, b) => String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" }),
    // A page newer than the server answers 404 Not Found on a route it added.
    OLD_SERVER: "the running server is older than this page: run `pnt service restart`",
    // The first line of a server error: an unknown filter term's long list of
    // every term stays in the API and the CLI; the ? Filters panel has it here.
    firstLine: (text) => String(text ?? "").split("\n")[0],
  };
  document.addEventListener("DOMContentLoaded", () => { nav(); sampleBanner(); });
})();
