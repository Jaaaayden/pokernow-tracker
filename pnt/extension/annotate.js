/* Notes on the page itself: click an element, say what is wrong with it.
 *
 * A screenshot shows what you saw but not what it is: whoever fixes it has to
 * find the element again by eye. A note left here keeps the page, a CSS selector,
 * the element's text and the heading it sits under, so the fix starts from the
 * code (pnt/server/annotations.py stores them; the ui-feedback skill reads them).
 *
 * One file for both places it runs: every server page (app.py's _page adds the
 * tag) and the HUD side panel, which loads it as its own -- an extension page may
 * not run a script fetched from a server. Off until switched on, so it costs a
 * page nothing: the \ key toggles it, `?annotate` in a URL turns it on, and it
 * stays on per origin (localStorage) until toggled off.
 *
 * Everything it draws sits in a shadow root on top of the page, so the page's
 * styles cannot reach it and its own cannot leak out.
 */
(() => {
  "use strict";
  if (window.__pntAnnotate) return;
  window.__pntAnnotate = true;

  const EXT = location.protocol === "chrome-extension:";
  // The side panel is "sidepanel"; a server page is its path, without the query,
  // so a note on one player's chart shows on every player's.
  const PAGE = EXT ? location.pathname.replace(/^\/|\.html$/g, "") : location.pathname;
  const KEY = "pnt-annotate";
  const remembered = () => { try { return localStorage.getItem(KEY) === "1"; } catch { return false; } };
  const remember = (on) => {
    try { if (on) localStorage.setItem(KEY, "1"); else localStorage.removeItem(KEY); } catch { /* private window */ }
  };
  if (new URLSearchParams(location.search).has("annotate")) remember(true);

  let server = EXT ? "http://127.0.0.1:52000" : "";
  const ready = EXT && globalThis.chrome?.storage?.sync
    ? chrome.storage.sync.get("server").then((v) => {
      if (v?.server) server = String(v.server).replace(/\/+$/, "");
    }, () => {})
    : Promise.resolve();

  async function api(method, path, body) {
    await ready;
    const headers = { "x-pnt": "1" };
    if (body) headers["content-type"] = "application/json";
    const r = await fetch(server + path, { method, headers, body: body ? JSON.stringify(body) : undefined });
    if (!r.ok) throw new Error(r.status === 404 && path === "/annotations" ? "old server" : `${r.status}`);
    return r.json();
  }

  // ---------------------------------------------------------- the element --
  const unique = (sel) => { try { return document.querySelectorAll(sel).length === 1; } catch { return false; } };

  function selectorFor(el) {
    const parts = [];
    for (let n = el; n && n.nodeType === 1 && n !== document.documentElement; n = n.parentElement) {
      if (n.id && unique(`#${CSS.escape(n.id)}`)) { parts.unshift(`#${CSS.escape(n.id)}`); break; }
      // A class that tells it from its siblings outlasts a position, which shifts
      // whenever a page adds a banner or a row above it.
      const sibs = n.parentElement ? [...n.parentElement.children].filter((c) => c !== n) : [];
      const cls = [...n.classList].find((c) => !sibs.some((s) => s.localName === n.localName && s.classList.contains(c)));
      let part = cls ? `${n.localName}.${CSS.escape(cls)}` : n.localName;
      const same = sibs.filter((c) => c.localName === n.localName);
      if (!cls && same.length) part += `:nth-of-type(${[...n.parentElement.children].filter((c) => c.localName === n.localName).indexOf(n) + 1})`;
      parts.unshift(part);
    }
    return parts.join(" > ");
  }

  const squash = (s, n) => String(s || "").replace(/\s+/g, " ").trim().slice(0, n);

  // The spot as a person would name it: the nearest heading above the element,
  // and the nearest container with an id, which is what a grep finds first.
  function contextOf(el) {
    let heading = "", id = "";
    for (let n = el.parentElement, depth = 0; n && n !== document.body && depth < 8; n = n.parentElement, depth++) {
      if (!id && n.id) id = n.id;
      if (!heading) {
        const h = [...n.querySelectorAll("h1,h2,h3,h4,legend,caption,summary,.title")]
          .find((x) => x !== el && x.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING);
        if (h) heading = squash(h.textContent, 80);
      }
      if (heading && id) break;
    }
    return [heading, id && `#${id}`].filter(Boolean).join(" · ");
  }

  function describe(el) {
    const r = el.getBoundingClientRect();
    return {
      page: PAGE,
      query: location.search.replace(/^\?/, ""),
      selector: selectorFor(el),
      tag: el.localName,
      classes: squash(el.getAttribute("class"), 300),
      text: squash(el.innerText ?? el.textContent, 300),
      context: contextOf(el).slice(0, 300),
      rect: { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) },
      viewport: { w: innerWidth, h: innerHeight },
    };
  }

  const query = (sel) => { try { return sel ? document.querySelector(sel) : null; } catch { return null; } };
  const textOf = (n) => squash(n.innerText ?? n.textContent, 300);

  // The selector first, but only if the element there still says what it said;
  // else the element of that tag that does (the page redrew in another order);
  // else whatever the selector finds, since a fix may well have changed the text.
  function find(a) {
    const at = query(a.selector);
    if (!a.text || (at && textOf(at) === a.text)) return at;
    const same = [...document.querySelectorAll(a.tag || "*")].find((n) => !host.contains(n) && textOf(n) === a.text);
    return same || at;
  }

  // --------------------------------------------------------------- drawing --
  const host = document.createElement("pnt-annotate");
  host.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483647";
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `<style>
    :host { all: initial; }
    * { box-sizing: border-box; font: 12px/1.4 system-ui, sans-serif; }
    .hl { position: fixed; border: 2px solid #e8a33d; background: #e8a33d22; border-radius: 3px; display: none; }
    .pin { position: fixed; pointer-events: auto; min-width: 18px; height: 18px; padding: 0 5px; border-radius: 9px;
      border: 1px solid #0006; color: #1b1b1b; font-weight: 700; font-size: 11px; line-height: 16px; text-align: center;
      cursor: pointer; box-shadow: 0 1px 3px #0005; }
    .pin.open { background: #f2b544; } .pin.resolved { background: #6fcf8b; }
    .bar { position: fixed; right: 10px; bottom: 10px; pointer-events: auto; display: flex; gap: 2px; align-items: center;
      background: #1f2328; color: #eee; border-radius: 16px; padding: 3px; box-shadow: 0 2px 8px #0006; }
    .bar button { background: none; border: 0; color: inherit; cursor: pointer; padding: 3px 9px; border-radius: 12px; }
    .bar button:hover, .bar button.on { background: #ffffff22; }
    .bar .pick.on { background: #e8a33d; color: #1b1b1b; }
    .pop { position: fixed; pointer-events: auto; width: min(300px, calc(100vw - 16px)); background: #fff; color: #1b1b1b;
      border: 1px solid #0003; border-radius: 6px; padding: 8px; box-shadow: 0 4px 16px #0004; display: none; }
    .pop .where { color: #666; font-size: 11px; margin-bottom: 4px; word-break: break-word; }
    .pop textarea { width: 100%; min-height: 70px; resize: vertical; border: 1px solid #0003; border-radius: 4px; padding: 4px; }
    .pop .body { white-space: pre-wrap; word-break: break-word; margin: 2px 0 6px; }
    .pop .reply { white-space: pre-wrap; background: #eaf7ee; border-left: 3px solid #6fcf8b; padding: 4px 6px; margin-bottom: 6px; }
    .pop .row { display: flex; gap: 6px; justify-content: flex-end; margin-top: 6px; flex-wrap: wrap; }
    .pop button { border: 1px solid #0003; background: #f4f4f4; border-radius: 4px; padding: 3px 9px; cursor: pointer; }
    .pop button.go { background: #1f2328; color: #fff; }
    .pop .item { padding: 4px 0; border-top: 1px solid #0001; cursor: pointer; }
    .pop .item:first-of-type { border-top: 0; }
    .err { color: #b3261e; }
  </style>
  <div class="hl"></div><div class="pins"></div><div class="pop"></div>
  <div class="bar">
    <button class="pick" title="Pick an element to leave a note on (Esc stops)">✎ note</button>
    <button class="list" title="Every note on this page">0 open</button>
    <button class="off" title="Hide the note overlay (the backslash key brings it back)">✕</button>
  </div>`;
  const $ = (s) => root.querySelector(s);
  const hl = $(".hl"), pins = $(".pins"), pop = $(".pop"), pickBtn = $(".pick"), listBtn = $(".list");

  const state = { on: false, picking: false, notes: [], error: "", timer: null };
  const found = new Map(); // note id -> the element its pin sits on
  const mine = () => state.notes.filter((a) => a.page === PAGE && a.status !== "closed");

  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const button = (text, fn, cls) => { const b = el("button", cls, text); b.addEventListener("click", fn); return b; };

  function place(box, rect) {
    // Below the element when it fits, else above; always inside the window.
    const w = box.offsetWidth || 300, h = box.offsetHeight || 140;
    let top = rect.bottom + 6;
    if (top + h > innerHeight - 8) top = Math.max(8, rect.top - h - 6);
    box.style.top = `${top}px`;
    box.style.left = `${Math.min(Math.max(8, rect.left), innerWidth - w - 8)}px`;
  }

  function drawPins() {
    pins.replaceChildren();
    const open = mine().filter((a) => a.status === "open").length;
    listBtn.textContent = state.error || `${open} open`;
    listBtn.classList.toggle("err", Boolean(state.error));
    mine().forEach((a, i) => {
      // Kept until the page drops the element: the text search reads layout, too
      // dear to run for every note on every tick.
      let target = found.get(a.id);
      if (!target?.isConnected) found.set(a.id, (target = find(a)));
      if (!target) return;
      const r = target.getBoundingClientRect();
      if (!r.width && !r.height) return;
      if (r.bottom < 0 || r.top > innerHeight) return;
      const pin = el("div", `pin ${a.status}`, String(i + 1));
      pin.title = a.note;
      pin.style.top = `${Math.max(0, r.top - 9)}px`;
      pin.style.left = `${Math.min(innerWidth - 22, r.right - 9)}px`;
      pin.addEventListener("click", () => show(a, target));
      pins.append(pin);
    });
  }

  function closePop() { pop.style.display = "none"; pop.replaceChildren(); }

  function openPop(nodes, rect) {
    pop.replaceChildren(...nodes);
    pop.style.display = "block";
    place(pop, rect);
  }

  // Typing in a note must not reach the page's own key handlers.
  const fence = (n) => { n.addEventListener("keydown", (e) => e.stopPropagation()); return n; };

  function compose(target) {
    const facts = describe(target);
    const area = fence(el("textarea"));
    area.placeholder = "What's wrong here? (Ctrl+Enter saves)";
    const save = async () => {
      const note = area.value.trim();
      if (!note) return;
      try {
        state.notes.push(await api("POST", "/annotations", { ...facts, note }));
        state.error = "";
        closePop();
      } catch (e) {
        area.before(el("div", "err", `Not saved: ${e.message === "old server" ? "restart the tracker (pnt service restart)" : e.message}`));
      }
      drawPins();
    };
    area.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); save(); }
      if (e.key === "Escape") closePop();
    });
    const row = el("div", "row");
    row.append(button("Cancel", closePop), button("Save", save, "go"));
    openPop([el("div", "where", `${facts.tag}${facts.context ? ` · ${facts.context}` : ""} · ${facts.text.slice(0, 60)}`), area, row],
      target.getBoundingClientRect());
    area.focus();
  }

  async function update(a, fields) {
    try {
      Object.assign(a, await api("POST", `/annotations/${a.id}`, fields));
    } catch (e) { state.error = `not saved (${e.message})`; }
    closePop();
    drawPins();
  }

  function show(a, target) {
    const nodes = [el("div", "where", `${a.status} · ${a.at.replace("T", " ").slice(0, 16)}`), el("div", "body", a.note)];
    if (a.reply) nodes.push(el("div", "reply", a.reply));
    const row = el("div", "row");
    if (a.status === "resolved") {
      row.append(button("Reopen", () => update(a, { status: "open" })), button("Looks good", () => update(a, { status: "closed" }), "go"));
    } else {
      row.append(button("Delete", async () => {
        try { await api("DELETE", `/annotations/${a.id}`); state.notes = state.notes.filter((x) => x !== a); } catch (e) { state.error = e.message; }
        closePop();
        drawPins();
      }), button("Close", closePop, "go"));
    }
    nodes.push(row);
    openPop(nodes, (target || listBtn).getBoundingClientRect());
  }

  function showList() {
    const all = mine();
    if (!all.length) {
      openPop([el("div", "body", state.error || "No notes on this page yet. ✎ note, then click what's wrong.")], listBtn.getBoundingClientRect());
      return;
    }
    openPop(all.map((a, i) => {
      const item = el("div", "item", `${i + 1}. [${a.status}] ${a.note.slice(0, 120)}`);
      item.addEventListener("click", () => {
        const target = find(a);
        target?.scrollIntoView({ block: "center" });
        setTimeout(() => show(a, target), target ? 250 : 0);
      });
      return item;
    }), listBtn.getBoundingClientRect());
  }

  // --------------------------------------------------------------- picking --
  const ours = (e) => e.composedPath().includes(host);

  function hover(e) {
    if (ours(e)) { hl.style.display = "none"; return; }
    const r = e.target.getBoundingClientRect();
    Object.assign(hl.style, { display: "block", top: `${r.top}px`, left: `${r.left}px`, width: `${r.width}px`, height: `${r.height}px` });
  }

  // Swallowed while picking: the click is for choosing the element, not for
  // opening a hand or switching a view underneath it.
  function swallow(e) {
    if (ours(e)) return;
    e.preventDefault();
    e.stopPropagation();
    e.stopImmediatePropagation();
    if (e.type === "click") compose(e.target);
  }

  const PICK_EVENTS = ["pointerdown", "mousedown", "mouseup", "click"];
  function pick(on) {
    state.picking = on;
    pickBtn.classList.toggle("on", on);
    hl.style.display = "none";
    document.documentElement.style.cursor = on ? "crosshair" : "";
    for (const t of PICK_EVENTS) document[on ? "addEventListener" : "removeEventListener"](t, swallow, true);
    document[on ? "addEventListener" : "removeEventListener"]("mousemove", hover, true);
  }

  // ------------------------------------------------------------ on and off --
  async function refresh() {
    try {
      state.notes = await api("GET", "/annotations");
      state.error = "";
    } catch (e) {
      state.error = e.message === "old server" ? "restart tracker" : "no server";
    }
    drawPins();
  }

  function setOn(on) {
    state.on = on;
    remember(on);
    if (!on) {
      pick(false);
      closePop();
      host.remove();
      clearInterval(state.timer);
      return;
    }
    document.documentElement.append(host);
    refresh();
    // Pages redraw under the pins (the HUD every hand, the chart on every filter),
    // so they are re-placed on a clock rather than on events that would miss that.
    state.timer = setInterval(drawPins, 700);
  }

  pickBtn.addEventListener("click", () => pick(!state.picking));
  listBtn.addEventListener("click", showList);
  $(".off").addEventListener("click", () => setOn(false));
  addEventListener("scroll", () => state.on && drawPins(), { passive: true, capture: true });
  addEventListener("resize", () => state.on && drawPins());
  document.addEventListener("visibilitychange", () => { if (state.on && !document.hidden) refresh(); });
  // A key of its own rather than a chord: Chrome keeps taking the chords (Alt+Shift+N
  // became its split view). Not while typing, so a \ in the spot box stays a \.
  const typing = (e) => {
    const t = e.composedPath()[0];
    return t instanceof Element && (t.closest("input, textarea, select, [contenteditable]") !== null);
  };
  document.addEventListener("keydown", (e) => {
    if (e.code === "Backslash" && !e.ctrlKey && !e.altKey && !e.metaKey && !typing(e)) {
      e.preventDefault();
      setOn(!state.on);
      if (state.on) pick(true);
    } else if (e.key === "Escape" && state.on) {
      if (pop.style.display === "block") closePop();
      else if (state.picking) pick(false);
    }
  });

  if (remembered()) setOn(true);
})();
