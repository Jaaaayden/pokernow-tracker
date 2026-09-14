// Hand replay, shared by the chart and all-in pages: one hand from /hands/{id},
// street by street, with the pot before each action and a size word on each bet.
// Exposed as window.pntReplay for the page's own script, which runs after this
// one. Styled by the page: it only emits elements with the classes the pages'
// `.replay` rules already cover.
(() => {
  const SUIT = { s: "♠", h: "♥", d: "♦", c: "♣" };
  const pretty = (cards) => cards.replace(/([2-9TJQKA])([shdc])/g, (_, r, s) => r + SUIT[s] + " ").trim();
  const SIZE_LABEL = { small: "under ½ pot", medium: "½–¾ pot", large: "¾ pot to pot", overbet: "overbet", check: "checked" };
  // Mirrors size_bucket() in derive.py, for labelling bets in a replay.
  const bucketOf = (amount, pot) => amount > pot ? "overbet"
    : amount >= Math.floor(0.75 * pot) ? "large"
    : amount >= Math.floor(0.5 * pot) ? "medium" : "small";

  function message(text, cls = "muted") {
    const p = document.createElement("p"); p.className = cls; p.textContent = text; return p;
  }

  // Fetch one hand and render it into `box`, which is unhidden first so the
  // "loading…" line shows where the replay is about to appear.
  async function show(box, id) {
    box.classList.remove("hidden");
    box.replaceChildren(message("loading…"));
    try {
      const r = await fetch(`/hands/${id}`);
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(body.detail || `${r.status} ${r.statusText}`);
      }
      render(box, await r.json());
    } catch (e) {
      box.replaceChildren(message(String(e.message || e), "err"));
    }
  }

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
    who.textContent = d.players.map(p => {
      const net = p.collected - p.contributed + (p.bounty || 0);
      return `${name(p.pn_id)}${p.hole_cards ? " " + pretty(p.hole_cards) : ""} ${net ? amt(net).replace(/^(?!-)/, "+") : "±0"}`;
    }).join("  ·  ");
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
      info.textContent = [dealt.length ? pretty(dealt.join("")) : "", street === "preflop" ? "" : `pot ${amt(pot)}`].filter(Boolean).join(" · ");
      info.classList.add("cards");
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
    if (runs.length > 1) box.appendChild(message(`second run: ${pretty(runs[1].join(""))}`));
    if (d.voluntary_shows?.length) {
      box.appendChild(message("shown after the hand: " + d.voluntary_shows.map(s => `${name(s.pn_id)} ${pretty(s.cards)}`).join(", ")));
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

  window.pntReplay = { pretty, bucketOf, SIZE_LABEL, show, render };
})();
