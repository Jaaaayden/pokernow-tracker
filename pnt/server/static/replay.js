// Hand replay, shared by the chart, all-in and biggest pots pages: one hand from
// /hands/{id}, street by street, with the pot before each action and a size word
// on each bet. Exposed as window.pntReplay for the page's own script, which runs
// after this one; loading.js (pntPage) runs before it. Its rules, and the card
// chips' that every page listing hands draws, are injected here once rather
// than copied into each page's stylesheet; :where() keeps them at no
// specificity, so a page's own rule wins.
(() => {
  const { message, getJson } = window.pntPage;
  // A hand's time, local: pntPage.when, kept here too for the pages that list hands.
  const { when } = window.pntPage;
  const REPLAY_CSS = `
    :where(.replay) h3 { font-size: 14px; margin: 0 0 4px; overflow-wrap: anywhere; }
    :where(.replay) h3 small { color: var(--muted); font-weight: 400; margin-left: 6px; }
    :where(.replay) .who { color: var(--ink-2); margin: 0 0 6px; }
    :where(.replay) .street { margin-top: 10px; }
    :where(.replay) .street > b { text-transform: capitalize; }
    :where(.replay) .street > span { color: var(--muted); margin-left: 8px; }
    :where(.replay) ol { margin: 4px 0 0; padding-left: 22px; }
    :where(.replay) li.forced { color: var(--muted); }`;
  if (!document.getElementById("pnt-replay-css")) {
    const style = document.createElement("style");
    style.id = "pnt-replay-css";
    style.textContent = REPLAY_CSS;
    document.head.prepend(style);
  }
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

  // Chips in a hand's big blinds (at most one decimal, no ".0"), or as chips:
  // `unit` is "bb" (the default) or "chips". A hand with no big blind on record
  // is in chips whatever the unit.
  function amounts(bb, unit) {
    const inBb = unit !== "chips" && bb > 0;
    const amt = (chips) => {
      if (chips == null) return "–";
      const v = inBb ? Math.round(chips / bb * 10) / 10 || 0 : Math.round(chips) || 0;
      return inBb ? `${v.toLocaleString(undefined, { maximumFractionDigits: 1 })}bb` : v.toLocaleString();
    };
    // Signed, for a profit or a loss: "+12bb", "-3.5bb", "±0".
    const signed = (chips) => {
      const s = amt(Math.abs(chips));
      return /^0(bb)?$/.test(s) ? "±0" : (chips > 0 ? "+" : "-") + s;
    };
    return { amt, signed };
  }

  // Fetch one hand and render it into `box`, which is unhidden first so the
  // "loading…" line shows where the replay is about to appear. Only the box's
  // latest request is drawn: stepping through a list with the arrow keys asks
  // for several hands at once, and they need not come back in order.
  // `opts.unit` ("bb", the default, or "chips") holds for the box until changed;
  // setUnit() redraws the hand on show in another one, with no new fetch.
  let seq = 0;
  const drawn = new WeakMap(); // box -> {d, unit}: the hand on show, to redraw
  async function show(box, id, opts = {}) {
    const req = String(++seq);
    box.dataset.replayReq = req;
    delete box.dataset.empty; // a page's "click a hand" placeholder, if it had one
    box.classList.remove("hidden");
    const unit = opts.unit || drawn.get(box)?.unit || "bb";
    drawn.set(box, { d: null, unit });
    box.replaceChildren(message("loading…"));
    try {
      const d = await getJson(`/hands/${id}`);
      if (box.dataset.replayReq === req) {
        const now = drawn.get(box);
        now.d = d;
        render(box, d, { unit: now.unit });
      }
    } catch (e) {
      if (box.dataset.replayReq === req) box.replaceChildren(message(String(e.message || e), "err"));
    }
  }
  // Drop whatever `box` is still fetching, for a page that clears or hides it.
  const cancel = (box) => { delete box.dataset.replayReq; drawn.delete(box); };
  // Redraw the hand on show in `box` in another unit: one still loading takes
  // the unit when it lands.
  function setUnit(box, unit) {
    const now = drawn.get(box);
    if (!now) return;
    now.unit = unit;
    if (now.d) render(box, now.d, { unit });
  }

  function render(box, d, opts = {}) {
    box.replaceChildren();
    const h = d.hand;
    const bb = h.bb_effective || h.bb;
    const name = (id) => d.names?.[id] || id;
    const { amt, signed } = amounts(bb, opts.unit);
    const runs = JSON.parse(h.board_json || "[]");
    const board = runs[0] || [];

    const title = document.createElement("h3");
    title.textContent = `Hand #${h.hand_number}`;
    const sub = document.createElement("small");
    sub.textContent = [h.game_id, when(h.ts), `${h.n_dealt_in} dealt in`].filter(Boolean).join(" · ");
    title.appendChild(sub);
    box.appendChild(title);
    box.appendChild(tagBox(h.game_id, h.hand_number));

    // Each player's net for the hand. Side bets settle outside the pot, so a net
    // that holds one says how much of it was.
    const who = document.createElement("p"); who.className = "who";
    d.players.forEach((p, i) => {
      const net = p.collected - p.contributed + (p.bounty || 0) + (p.nit || 0);
      if (i) who.append("  ·  ");
      who.append(name(p.pn_id) + " ");
      if (p.hole_cards) who.append(cardsEl(p.hole_cards), " ");
      who.append(signed(net));
      const side = [p.bounty && `7-2 bounty ${signed(p.bounty)}`, p.nit && `nit ${signed(p.nit)}`].filter(Boolean);
      if (side.length) {
        const s = document.createElement("small");
        s.className = "muted";
        s.style.whiteSpace = "nowrap";
        s.textContent = ` (incl. ${side.join(", ")})`;
        who.appendChild(s);
      }
    });
    box.appendChild(who);

    // A bet nobody called in full comes back to its owner when the betting on
    // that street closes: off the pot from then on. It is always the last chips
    // that player put in, so it comes off at the end of that street.
    const back = {};
    for (const p of d.players) if (p.uncalled > 0) back[p.pn_id] = { chips: p.uncalled };
    for (const a of d.actions) if (a.amount > 0 && back[a.pn_id]) back[a.pn_id].street = a.street;

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
      for (const [pid, r] of Object.entries(back)) {
        if (r.street !== street) continue;
        const li = document.createElement("li");
        li.className = "forced";
        li.textContent = `uncalled ${amt(r.chips)} returned to ${name(pid)}`;
        pot -= r.chips;
        ol.appendChild(li);
      }
      if (ol.children.length) sec.appendChild(ol);
      box.appendChild(sec);
    }
    if (runs.length > 1 && !h.double_board) {
      const p = message("second run: ");
      p.append(cardsEl(runs[1]));
      box.appendChild(p);
    }
    // A log exported mid-hand: what follows was never written down.
    if (h.complete === 0) box.appendChild(message("the log stops here; chips after this are not on record"));
    if (d.showdown) box.appendChild(showdownEl(d, name, amt, signed));
    if (d.voluntary_shows?.length) {
      const p = message("shown after the hand: ");
      d.voluntary_shows.forEach((v, i) => p.append(i ? ", " : "", `${name(v.pn_id)} `, cardsEl(v.cards)));
      box.appendChild(p);
    }
  }

  // ---- showdown ----------------------------------------------------------------
  // The foot of the replay: who won each pot on each board, with what (the
  // payload's `showdown`, stats/showdown.py). Every line is that player's net in
  // that pot on that board -- what they won there, less their own stake in it --
  // so a player's lines add up to the pot money in their net at the top. Profit
  // and loss take the page's --up/--down, with the chart's values as the
  // fallback for a page without them.
  const SD_CSS = `
    .sd { --sd-up: var(--up, #1f7a4d); --sd-down: var(--down, #b93a39); }
    @media (prefers-color-scheme: dark) {
      :root:not([data-theme="light"]) .sd { --sd-up: var(--up, #4cc38a); --sd-down: var(--down, #e66e6d); }
    }
    :root[data-theme="dark"] .sd { --sd-up: var(--up, #4cc38a); --sd-down: var(--down, #e66e6d); }
    .sd ul { list-style: none; margin: 4px 0 0; padding-left: 22px; }
    .sd li + li.sd-where { margin-top: 4px; }
    .sd .sd-where { color: var(--muted); }
    .sd .sd-lose { color: var(--muted); }
    .sd .sd-up { color: var(--sd-up); font-weight: 600; }
    .sd .sd-down { color: var(--sd-down); }`;
  if (!document.getElementById("pnt-sd-css")) {
    const style = document.createElement("style");
    style.id = "pnt-sd-css";
    style.textContent = SD_CSS;
    document.head.appendChild(style);
  }
  // Each player's stake in pot `k` of the showdown: what they put in above the
  // last pot's level, up to this one's. The same layers as stats/showdown.py.
  function stakes(d) {
    const s = d.showdown;
    const put = Object.fromEntries(d.players.map(p => [p.pn_id, p.contributed]));
    const live = d.players.filter(p => !p.folded).map(p => p.pn_id);
    const elig = s.pots.map(pot => new Set([...pot.boards[0].winners, ...pot.boards[0].losers].map(x => x.pn_id)));
    return (k, q) => {
      const floor = Math.max(0, ...live.filter(p => !elig[k].has(p)).map(p => put[p]));
      const nxt = elig[k + 1];
      const cap = nxt ? Math.max(0, ...[...elig[k]].filter(p => !nxt.has(p)).map(p => put[p])) : Infinity;
      return Math.max(0, Math.min(put[q], cap) - floor);
    };
  }

  function showdownEl(d, name, amt, signed) {
    const s = d.showdown;
    const sec = document.createElement("div"); sec.className = "street sd";
    const b = document.createElement("b");
    sec.appendChild(b);
    if (s.uncontested) {
      b.textContent = "result";
      const info = document.createElement("span");
      info.textContent = `${name(s.uncontested.pn_id)} won the ${amt(s.uncontested.won)} pot uncontested`;
      sec.appendChild(info);
      return sec;
    }
    b.textContent = "showdown";
    const hole = Object.fromEntries(d.players.map(p => [p.pn_id, p.hole_cards]));
    const double = d.hand.double_board;
    const ul = document.createElement("ul");
    const stake = stakes(d);
    const line = (pid, hand, cls, net, won) => {
      const li = document.createElement("li");
      if (cls) li.className = cls;
      li.append(name(pid), " ");
      if (hole[pid]) li.append(cardsEl(hole[pid]), " ");
      li.append(hand || (won ? "not shown" : "mucked"));
      const n = document.createElement("span");
      const text = signed(net);
      n.className = text === "±0" ? "" : net > 0 ? "sd-up" : "sd-down";
      n.textContent = ` ${text}`;
      li.appendChild(n);
      return li;
    };
    s.pots.forEach((pot, k) => {
      const n = pot.boards.length;
      for (const r of pot.boards) {
        // Where: the pot, then the board when there is more than one, with the
        // share of the pot each board plays for.
        const where = document.createElement("li"); where.className = "sd-where";
        where.append([pot.label, amt(pot.chips)].join(" "));
        if (n > 1) where.append(` · ${double ? "board" : "run"} ${r.run + 1} for ${amt(pot.chips / n)} `, cardsEl(r.board));
        if (r.winners.length > 1) where.append(" · chopped");
        ul.appendChild(where);
        for (const w of r.winners) ul.appendChild(line(w.pn_id, w.hand, "", w.won - stake(k, w.pn_id) / n, true));
        for (const l of r.losers) ul.appendChild(line(l.pn_id, l.hand, "sd-lose", -stake(k, l.pn_id) / n, false));
      }
    });
    sec.appendChild(ul);
    return sec;
  }

  function actionText(a, potBefore, name, amt, street) {
    const who = name(a.pn_id);
    const allIn = a.all_in ? " (all-in)" : "";
    switch (a.action_type) {
      // A straddle or a missed big blind is posted *to* a size, the small blind
      // already in it: `amount` is only the fresh chips, `amount_to` the size.
      case "post": return `${who} posts ${a.post_kind ? a.post_kind.replace(/_/g, " ") + " " : ""}${amt(a.amount_to ?? a.amount)}`;
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
    .tagbox .tg-err { color: var(--down, #b93a39); }`;
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

  // One listener for every box: each `pnt-tags` event goes to the boxes on the
  // page, which repaint when it is their hand. A box thrown away is no longer on
  // the page, so it is skipped, and the WeakMap lets it go.
  const following = new WeakMap(); // box -> its repaint
  document.addEventListener("pnt-tags", (e) => {
    for (const box of document.querySelectorAll(".tagbox")) following.get(box)?.(e.detail);
  });

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
    following.set(box, (d) => {
      if (d.game_id !== gameId || d.hand_number !== handNumber) return;
      const focused = document.activeElement === input;
      current = d.tags;
      paint();
      if (focused) input.focus();
    });
    paint();
    if (!tagList) fillTagList();
    input.setAttribute("list", "pnt-tag-list");
    if (!tags) {
      fetch(url).then(r => (r.ok ? r.json() : null)).then(d => {
        if (d && d.tags) { current = d.tags; paint(); }
      }).catch(() => {});
    }
    return box;
  }

  window.pntReplay = { pretty, cardsEl, bucketOf, SIZE_LABEL, when, show, cancel, setUnit, render, tagBox };
})();
