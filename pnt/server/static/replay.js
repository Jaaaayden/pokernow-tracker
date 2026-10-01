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
  const SIZE_LABEL = { small: "under ½ pot", medium: "½–¾ pot", large: "¾ pot to pot", overbet: "overbet", check: "checked" };
  // Mirrors size_bucket() in derive.py, for labelling bets in a replay.
  const bucketOf = (amount, pot) => amount > pot ? "overbet"
    : amount >= Math.floor(0.75 * pot) ? "large"
    : amount >= Math.floor(0.5 * pot) ? "medium" : "small";

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

    const who = document.createElement("p"); who.className = "who";
    d.players.forEach((p, i) => {
      const net = p.collected - p.contributed + (p.bounty || 0);
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
      const dealt = street === "flop" ? board.slice(0, 3) : street === "turn" ? board.slice(3, 4) : street === "river" ? board.slice(4, 5) : [];
      if (dealt.length) info.append(cardsEl(dealt));
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
    if (runs.length > 1) {
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

  window.pntReplay = { pretty, cardsEl, bucketOf, SIZE_LABEL, show, cancel, render };
})();
