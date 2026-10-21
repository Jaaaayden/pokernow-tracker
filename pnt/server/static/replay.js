// Hand replay, shared by the chart and all-in pages: one hand from /hands/{id},
// street by street, with the pot before each action and a size word on each bet.
// Exposed as window.pntReplay for the page's own script, which runs after this
// one. Styled by the page, with one exception: the card chips. Every page that
// lists hands draws them, so their rules are injected here once rather than
// copied into each page's stylesheet.
(() => {
  const SUIT = { s: "♠", h: "♥", d: "♦", c: "♣" };
  // Plain text, for tooltips and captions where no element can go.
  const pretty = (cards) => cards.replace(/([2-9TJQKA])([shdc])/g, (_, r, s) => r + SUIT[s] + " ").trim();

  // Cards as chips: a cream tile per card, red for hearts and diamonds. The tile
  // keeps its colour in dark mode, like a real card on a dark table.
  const CARD_CSS = `
    .pc-row { display: inline-flex; gap: 2px; vertical-align: baseline; }
    .pc { display: inline-flex; align-items: baseline; gap: 1px; padding: 0 3px; border-radius: 3px;
      background: #f4efe3; color: #1b1b1b; border: 1px solid rgba(0,0,0,.18);
      font-size: .92em; font-weight: 600; line-height: 1.35; font-variant-numeric: tabular-nums;
      white-space: nowrap; }
    .pc[data-suit=h], .pc[data-suit=d] { color: #c62f2f; }
    .pc b { font-weight: 400; }
    @media (max-width: 600px) { .pc { padding: 0 2px; border-radius: 2px; } }`;
  if (!document.getElementById("pnt-cards")) {
    const style = document.createElement("style");
    style.id = "pnt-cards";
    style.textContent = CARD_CSS;
    document.head.appendChild(style);
  }
  // "Kc6h" (or ["Kc", "6h"]) as a row of chips. Ten prints as 10.
  function cardsEl(cards) {
    const row = document.createElement("span");
    row.className = "pc-row";
    const text = Array.isArray(cards) ? cards.join("") : cards;
    for (const [, r, s] of text.matchAll(/([2-9TJQKA])([shdc])/g)) {
      const c = document.createElement("span");
      c.className = "pc"; c.dataset.suit = s;
      const suit = document.createElement("b"); suit.textContent = SUIT[s];
      c.append(r === "T" ? "10" : r, suit);
      row.appendChild(c);
    }
    return row;
  }
  const SIZE_LABEL = { blocker: "under ⅕ pot", small: "⅕–½ pot", medium: "½–¾ pot", large: "¾ pot to pot", overbet: "overbet", check: "checked" };
  // Mirrors size_bucket() in derive.py, for labelling bets in a replay. The
  // blocker edge is an integer division there, and Math.floor of it here.
  const bucketOf = (amount, pot) => amount > pot ? "overbet"
    : amount >= Math.floor(0.75 * pot) ? "large"
    : amount >= Math.floor(0.5 * pot) ? "medium"
    : amount >= Math.floor(pot / 5) ? "small" : "blocker";

  function message(text, cls = "muted") {
    const p = document.createElement("p"); p.className = cls; p.textContent = text; return p;
  }

  // Fetch one hand and render it into `box`, which is unhidden first so the
  // "loading…" line shows where the replay is about to appear. Only the box's
  // latest request is drawn: stepping through a list with the arrow keys asks
  // for several hands at once, and they need not come back in order.
  let seq = 0;
  async function show(box, id) {
    const req = String(++seq);
    box.dataset.replayReq = req;
    box.classList.remove("hidden");
    box.replaceChildren(message("loading…"));
    try {
      const r = await fetch(`/hands/${id}`);
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(body.detail || `${r.status} ${r.statusText}`);
      }
      const d = await r.json();
      if (box.dataset.replayReq === req) render(box, d);
    } catch (e) {
      if (box.dataset.replayReq === req) box.replaceChildren(message(String(e.message || e), "err"));
    }
  }
  // Drop whatever `box` is still fetching, for a page that clears or hides it.
  const cancel = (box) => { delete box.dataset.replayReq; };

  function render(box, d) {
    box.replaceChildren();
    const h = d.hand;
    const bb = h.bb_effective || h.bb;
    const name = (id) => d.names?.[id] || id;
    const amt = (chips) => bb ? `${+(chips / bb).toFixed(2)}bb` : `${chips}`;
    const runs = JSON.parse(h.board_json || "[]");
    const board = runs[0] || [];

    const title = document.createElement("h3");
    title.textContent = `Hand #${h.hand_number}`;
    const sub = document.createElement("small");
    sub.textContent = [h.game_id, h.ts, `${h.n_dealt_in} dealt in`].filter(Boolean).join(" · ");
    title.appendChild(sub);
    box.appendChild(title);
    box.appendChild(tagBox(h.game_id, h.hand_number));

    const who = document.createElement("p"); who.className = "who";
    d.players.forEach((p, i) => {
      const net = p.collected - p.contributed + (p.bounty || 0) + (p.nit || 0);
      if (i) who.append("  ·  ");
      who.append(name(p.pn_id) + " ");
      if (p.hole_cards) who.append(cardsEl(p.hole_cards), " ");
      who.append(net ? amt(net).replace(/^(?!-)/, "+") : "±0");
    });
    box.appendChild(who);

    const STREETS = [["preflop", 0], ["flop", 3], ["turn", 4], ["river", 5]];
    let pot = 0;
    for (const [street, cards] of STREETS) {
      const acts = d.actions.filter(a => a.street === street);
      if (!acts.length && board.length < cards) continue;
      const sec = document.createElement("div"); sec.className = "street";
      const b = document.createElement("b"); b.textContent = street;
      const info = document.createElement("span");
      const deal = (b) => street === "flop" ? b.slice(0, 3) : street === "turn" ? b.slice(3, 4) : street === "river" ? b.slice(4, 5) : [];
      const dealt = deal(board);
      if (dealt.length) info.append(cardsEl(dealt));
      // Double Board deals both boards every street, bet on together: show them side by side.
      const second = h.double_board && runs[1] ? deal(runs[1]) : [];
      if (second.length) info.append(" / ", cardsEl(second));
      if (street !== "preflop") info.append(`${dealt.length ? " · " : ""}pot ${amt(pot)}`);
      sec.append(b, info);
      const ol = document.createElement("ol");
      for (const a of acts) {
        const li = document.createElement("li");
        li.textContent = actionText(a, pot, name, amt, street);
        if (a.is_forced) li.className = "forced";
        pot += a.amount;
        ol.appendChild(li);
      }
      if (acts.length) sec.appendChild(ol);
      box.appendChild(sec);
    }
    if (runs.length > 1 && !h.double_board) {
      const p = message("second run: ");
      p.append(cardsEl(runs[1]));
      box.appendChild(p);
    }
    if (d.voluntary_shows?.length) {
      const p = message("shown after the hand: ");
      d.voluntary_shows.forEach((v, i) => p.append(i ? ", " : "", `${name(v.pn_id)} `, cardsEl(v.cards)));
      box.appendChild(p);
    }
  }

  function actionText(a, potBefore, name, amt, street) {
    const who = name(a.pn_id);
    const allIn = a.all_in ? " (all-in)" : "";
    switch (a.action_type) {
      case "post": return `${who} posts ${a.post_kind ? a.post_kind.replace(/_/g, " ") + " " : ""}${amt(a.amount)}`;
      case "fold": return `${who} folds`;
      case "check": return `${who} checks`;
      case "call": return `${who} calls ${amt(a.amount)}${allIn}`;
      case "bet": {
        const size = street !== "preflop" && potBefore > 0 ? ` (${SIZE_LABEL[bucketOf(a.amount, potBefore)]})` : "";
        return `${who} bets ${amt(a.amount)} into ${amt(potBefore)}${size}${allIn}`;
      }
      case "raise": return `${who} raises to ${amt(a.amount_to ?? a.amount)}${allIn}`;
      default: return `${who} ${a.action_type} ${amt(a.amount)}`;
    }
  }

  // ---- hand tags -------------------------------------------------------------
  // A hand's tags as chips, × to take one off, and a box to add one (Enter), which
  // suggests every tag in use. The replay has one, and the chart puts one on each
  // hand row. Each write that lands is announced on `document` as a `pnt-tags`
  // event, {game_id, hand_number, tags}, so every box on that hand -- and the page
  // -- follows it.
  const TAG_CSS = `
    .tagbox { display: flex; flex-wrap: wrap; align-items: center; gap: 3px 4px; margin: 2px 0 4px; font-size: 11px; }
    .tagbox .tg { display: inline-flex; align-items: center; padding: 0 0 0 7px; border: 1px solid var(--ring);
      border-radius: 999px; color: var(--ink-2); line-height: 1.6; }
    .tagbox .tg button { border: 0; background: transparent; color: var(--muted); padding: 0 6px 0 4px;
      font-size: 12px; line-height: 1; border-radius: 999px; }
    .tagbox .tg button:hover, .tagbox .tg button:focus-visible { color: var(--ink); outline: 0; }
    .tagbox input[type=text] { flex: none; width: 6.5em; min-width: 0; font: inherit; font-size: 11px; padding: 1px 7px;
      border: 1px dashed var(--ring); border-radius: 999px; background: transparent; color: var(--ink); }
    .tagbox input[type=text]:focus { border-style: solid; border-color: var(--accent); outline: 0; width: 10em; }
    .tagbox input::-webkit-calendar-picker-indicator { display: none !important; }
    .tagbox .tg-err { color: #c62f2f; }`;
  if (!document.getElementById("pnt-tags-css")) {
    const style = document.createElement("style");
    style.id = "pnt-tags-css";
    style.textContent = TAG_CSS;
    document.head.appendChild(style);
  }
  // One list of suggestions for every box: filled from /tags on first use, and
  // again after each write, since a new tag belongs in it.
  let tagList = null;
  function fillTagList() {
    if (!tagList) {
      tagList = document.createElement("datalist");
      tagList.id = "pnt-tag-list";
      document.body.appendChild(tagList);
    }
    fetch("/tags").then(r => (r.ok ? r.json() : [])).then(rows => {
      tagList.replaceChildren(...rows.map(t => { const o = document.createElement("option"); o.value = t.tag; return o; }));
    }).catch(() => {});
  }

  // `tags` is what the caller already holds (a row's `tags`); left out, the box
  // asks the server.
  function tagBox(gameId, handNumber, tags) {
    const box = document.createElement("div");
    box.className = "tagbox";
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = "+ tag";
    input.spellcheck = false;
    input.maxLength = 40;
    input.setAttribute("aria-label", `tag hand ${handNumber}`);
    input.title = "Enter adds it, e.g. river-bluff · cooler · study";
    const err = document.createElement("span");
    err.className = "tg-err";
    let current = tags ? [...tags] : [];
    const url = `/games/${encodeURIComponent(gameId)}/hands/${handNumber}/tags`;

    function paint() {
      const chips = current.map(t => {
        const c = document.createElement("span");
        c.className = "tg";
        c.append(t);
        const x = document.createElement("button");
        x.type = "button"; x.textContent = "×"; x.title = `remove ${t}`;
        x.setAttribute("aria-label", `remove tag ${t}`);
        x.addEventListener("click", () => send("DELETE", `${url}/${encodeURIComponent(t)}`));
        c.appendChild(x);
        return c;
      });
      box.replaceChildren(...chips, input, err);
    }
    async function send(method, path, body) {
      err.textContent = "";
      try {
        const r = await fetch(path, {
          // x-pnt: the server refuses writes without it (see app.py).
          method, headers: { "Content-Type": "application/json", "x-pnt": "1" },
          body: body ? JSON.stringify(body) : undefined,
        });
        const out = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(out.detail || `${r.status} ${r.statusText}`);
        document.dispatchEvent(new CustomEvent("pnt-tags", { detail: out }));
        fillTagList();
        return true;
      } catch (e) {
        err.textContent = String(e.message || e);
        return false;
      }
    }
    input.addEventListener("keydown", async (e) => {
      if (e.key === "Escape") { e.stopPropagation(); input.value = ""; err.textContent = ""; input.blur(); return; }
      if (e.key !== "Enter" || !input.value.trim()) return;
      e.preventDefault();
      if (await send("POST", url, { tag: input.value })) { input.value = ""; input.focus(); }
    });
    const follow = (e) => {
      if (!box.isConnected && box.dataset.drawn) { document.removeEventListener("pnt-tags", follow); return; }
      const d = e.detail;
      if (d.game_id !== gameId || d.hand_number !== handNumber) return;
      const focused = document.activeElement === input;
      current = d.tags;
      paint();
      if (focused) input.focus();
    };
    document.addEventListener("pnt-tags", follow);
    paint();
    // Marked once on the page, so a box not yet attached is not taken for one thrown away.
    requestAnimationFrame(() => { box.dataset.drawn = "1"; });
    if (!tagList) fillTagList();
    input.setAttribute("list", "pnt-tag-list");
    if (!tags) {
      fetch(url).then(r => (r.ok ? r.json() : null)).then(d => {
        if (d && d.tags) { current = d.tags; paint(); }
      }).catch(() => {});
    }
    return box;
  }

  window.pntReplay = { pretty, cardsEl, bucketOf, SIZE_LABEL, show, cancel, render, tagBox };
})();
