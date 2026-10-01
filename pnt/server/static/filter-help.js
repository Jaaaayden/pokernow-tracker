// The `?` beside a page's Spot box: every term a filter understands, read from
// /filters so the list cannot drift from the parser. Shared by chart.html,
// stats.html and allin.html; styled from their CSS variables.
//
// The panel follows the term under the caret. While a term is being typed it
// lists only the terms that fit; once the term reaches a parameter (a street, a
// position, a size...) it lists that parameter's values instead; a comma starts
// a fresh term and brings the whole list back. Clicking fills in the term under
// the caret rather than appending. A finished term that contradicts an earlier
// one (`srp` then `3bet_pot`, `position=BTN` then `position=CO`) replaces it.
(() => {
  const input = document.getElementById("filter");
  if (!input) return;

  const style = document.createElement("style");
  style.textContent = `
    .fh-btn { width: 28px; padding: 5px 0; border-radius: 999px; font-weight: 600; color: var(--ink-2); }
    .fh-btn[aria-expanded=true] { background: var(--accent); color: #fff; border-color: transparent; }
    .fh { background: var(--surface); border: 1px solid var(--ring); border-radius: 10px;
      padding: 8px 12px 10px; margin: 0 0 12px; font-size: 12px; line-height: 1.3; }
    .fh [hidden] { display: none !important; }
    .fh-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; margin-bottom: 6px; }
    .fh-head b { font-size: 13px; }
    .fh-head span { color: var(--ink-2); }
    .fh-head button { border: 0; background: transparent; color: var(--muted); font-size: 16px; line-height: 1; padding: 0 4px; }
    /* Columns rather than a grid: groups differ a lot in length, and a grid row
       is as tall as its longest group, which left the short ones floating. */
    .fh-groups { columns: 3 240px; column-gap: 20px; }
    .fh-group { break-inside: avoid; margin-bottom: 8px; }
    .fh-group h3 { font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase;
      letter-spacing: .04em; margin: 0 0 1px; }
    .fh-row { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 6px; padding: 1px 0; }
    .fh-row button { border: 0; background: transparent; padding: 0; color: var(--accent);
      font-family: ui-monospace, Consolas, monospace; font-size: 11.5px; text-align: left; }
    .fh-row button:hover, .fh-row button:focus-visible { text-decoration: underline; outline: 0; }
    .fh-row span { color: var(--ink-2); }
    .fh-slot-head { margin: 0 0 6px; color: var(--ink-2); }
    .fh-slot-head code { font-family: ui-monospace, Consolas, monospace; font-size: 11.5px; color: var(--accent); }
    .fh-opts { display: flex; flex-wrap: wrap; gap: 4px 6px; }
    .fh-opts button { border: 1px solid var(--ring); background: transparent; border-radius: 999px; padding: 2px 9px;
      color: var(--accent); font-family: ui-monospace, Consolas, monospace; font-size: 11.5px; }
    .fh-opts button:hover, .fh-opts button:focus-visible { border-color: var(--accent); outline: 0; }
    .fh-opts button span { margin-left: 6px; color: var(--ink-2); font-family: inherit; }
    .fh-hint { color: var(--muted); }
    .fh-values { margin: 2px 0 0; padding-top: 6px; border-top: 1px solid var(--grid);
      display: grid; grid-template-columns: max-content 1fr; gap: 2px 12px; }
    .fh-values dt { color: var(--muted); }
    .fh-values dd { margin: 0; font-family: ui-monospace, Consolas, monospace; font-size: 11.5px; color: var(--ink-2); }
    @media (max-width: 620px) { .fh-values { grid-template-columns: 1fr; } .fh-values dd { margin-bottom: 4px; } }
  `;
  document.head.appendChild(style);

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "fh-btn";
  btn.textContent = "?";
  btn.title = "every term you can put in a spot";
  btn.setAttribute("aria-expanded", "false");
  btn.setAttribute("aria-controls", "filter-help");
  input.after(btn);

  const panel = document.createElement("section");
  panel.className = "fh";
  panel.id = "filter-help";
  panel.hidden = true;
  (input.closest(".controls") || input).after(panel);
  // Keep the caret in the box while picking: a blur would lose the term under
  // the caret and fire `change` on a half-written term.
  panel.addEventListener("mousedown", (e) => { if (e.target.closest("button")) e.preventDefault(); });

  const el = (tag, text, cls) => {
    const e = document.createElement(tag);
    if (text != null) e.textContent = text;
    if (cls) e.className = cls;
    return e;
  };

  let vocab = null;      // the /filters payload
  let names = [];        // player names, for vs=
  let templates = [];    // one per vocabulary row, parsed into segments
  let groupBoxes = [];
  let pending = null;    // the template last clicked, followed through its parameters

  // --- Templates. A vocabulary term is literals and parameter slots:
  // `cbet_<street>=SIZE` is "cbet_", street, "=", SIZE. A trailing `>=N` is an
  // operator slot then a number, since every comparison takes any operator.
  const SLOT = /(<street>|<spot>|POS|SIZE|TEXTURE|DECISION|CLASS|NAME)/;
  const OP_WORDS = { ">=": "at least", "<=": "at most", "=": "exactly", ">": "more than", "<": "less than" };
  const PICK = {
    street: "pick a street", spot: "pick a decision point", POS: "pick a position", SIZE: "pick a size",
    TEXTURE: "pick a texture", DECISION: "pick what they did", op: "pick a comparison", NAME: "pick a player",
    N: "type a number", CLASS: "type a hand like AKs, T9o, 77 or 72",
  };

  function segments(term) {
    const segs = [];
    const num = term.match(/(>=|<=|=|>|<)N$/);
    const body = num ? term.slice(0, -num[0].length) : term;
    for (const part of body.split(SLOT)) {
      if (!part) continue;
      segs.push(SLOT.test(part) && part.match(SLOT)[0] === part ? { slot: part.replace(/[<>]/g, "") } : { lit: part });
    }
    if (num) segs.push({ slot: "op" }, { slot: "N" });
    return segs;
  }

  // What can go in a slot, as [value, description] pairs; null for free text.
  function options(kind) {
    const plain = (list) => list.map((v) => [v]);
    switch (kind) {
      case "street": return plain(vocab.streets);
      case "spot": return plain(vocab.decided);
      case "POS": return plain(vocab.positions);
      case "SIZE": return Object.entries(vocab.sizes);
      case "TEXTURE": return plain(vocab.textures);
      case "DECISION": return plain(vocab.decisions);
      case "op": return vocab.operators.map((o) => [o, OP_WORDS[o]]);
      case "NAME": return plain(names);
      default: return null;
    }
  }

  // How far `frag` gets through template `t`, ignoring case: "fail"; "literal",
  // partway through literal `i`; "slot", inside slot `i` with `partial` typed;
  // or "complete". `at` is where segment `i` starts in `frag`.
  const FAIL = { state: "fail" };
  function match(t, frag) {
    let pos = 0;
    for (let i = 0; i < t.segs.length; i++) {
      const s = t.segs[i];
      const rest = frag.slice(pos);
      const low = rest.toLowerCase();
      if (s.lit != null) {
        if (low.length < s.lit.length) return s.lit.startsWith(low) ? { state: "literal", i, at: pos } : FAIL;
        if (!low.startsWith(s.lit)) return FAIL;
        pos += s.lit.length;
        continue;
      }
      const opts = options(s.slot);
      const inSlot = { state: "slot", i, at: pos, partial: rest };
      if (!opts) return inSlot; // a number or a hand: the rest of the term is the value
      const vals = opts.map(([v]) => v.toLowerCase());
      // `>` could still become `>=`, and `UTG` `UTG+1`: stay in the slot.
      if (vals.some((v) => v.startsWith(low) && v !== low)) return inSlot;
      const hit = vals.filter((v) => low.startsWith(v)).sort((a, b) => b.length - a.length)[0];
      if (hit == null) return s.slot === "NAME" ? inSlot : FAIL; // a name not seen yet is still a name
      pos += hit.length;
    }
    return pos === frag.length ? { state: "complete" } : FAIL;
  }

  // `text` with the literals from segment `i` on added, up to the next slot.
  function fillLits(t, i, text) {
    while (i < t.segs.length && t.segs[i].lit != null) text += t.segs[i++].lit;
    return text;
  }

  // --- The term under the caret.
  function current() {
    const v = input.value;
    const caret = input.selectionStart ?? v.length;
    let start = v.lastIndexOf(",", caret - 1) + 1;
    while (start < caret && v[start] === " ") start++;
    const end = v.indexOf(",", caret);
    return { start, end: end < 0 ? v.length : end, frag: v.slice(start, caret) };
  }
  const termIndex = () => input.value.slice(0, input.selectionStart ?? input.value.length).split(",").length - 1;

  // --- Conflicts. Two terms conflict when no hand could match both, or when the
  // later one re-states the same parameter: the earlier one is dropped.
  function parts(term) {
    const m = term.match(/^(.*?)(!=|>=|<=|=|>|<)(.*)$/);
    return m
      ? { name: m[1].toLowerCase(), op: m[2], value: m[3].trim().toLowerCase() }
      : { name: term.toLowerCase(), op: "", value: "" };
  }
  const BOARDS = new Set(["flop", "turn", "river", "board"]);
  const LOWER = new Set([">=", ">"]);
  const UPPER = new Set(["<=", "<"]);
  const isNum = (s) => s !== "" && !Number.isNaN(Number(s));

  function conflicts(a, b) {
    if (a.toLowerCase() === b.toLowerCase()) return true;
    const p = parts(a), q = parts(b);
    if (!p.op && !q.op) return (vocab?.exclusive ?? []).some((g) => g.includes(p.name) && g.includes(q.name));
    if (p.name !== q.name) return false;
    // A flag and its own sized or decided form: `cbet_flop`, `cbet_flop=medium`.
    if (!p.op || !q.op) {
      const o = p.op ? p : q;
      return o.op === "=" && !isNum(o.value);
    }
    if (p.op === "!=" || q.op === "!=") return false;
    // Texture tags overlap on purpose; only one family's tags exclude each other.
    if (BOARDS.has(p.name)) {
      return p.op === "=" && q.op === "="
        && (vocab?.texture_families ?? []).some((f) => f.includes(p.value) && f.includes(q.value));
    }
    if (p.name === "vs") return false; // two opponents in one hand is a real spot
    if (p.op === "=" || q.op === "=") return true;
    // `pot>=500,pot<=1000` is a range; `pot>=500,pot>=200` restates the bound.
    return (LOWER.has(p.op) && LOWER.has(q.op)) || (UPPER.has(p.op) && UPPER.has(q.op));
  }

  // Drop every term that conflicts with the term at `keep` (the one just
  // finished, which wins wherever it sits) or with a later term. With
  // `afterComma` the caret lands after the comma that follows `keep`.
  function resolve(keep, afterComma) {
    const pieces = input.value.split(",");
    const terms = pieces.map((s) => s.trim());
    const ready = (t) => t && !/(!=|>=|<=|=|>|<)$/.test(t); // a half-typed `pot>=` takes no part
    const order = [...terms.keys()].reverse();
    if (keep >= 0 && keep < terms.length) order.unshift(...order.splice(order.indexOf(keep), 1));
    const kept = [];
    const dropped = new Set();
    for (const i of order) {
      if (!ready(terms[i])) continue;
      if (kept.some((j) => conflicts(terms[i], terms[j]))) dropped.add(i);
      else kept.push(i);
    }
    if (!dropped.size) return;
    const out = [];
    let caret = null;
    pieces.forEach((p, i) => {
      if (dropped.has(i)) return;
      out.push(p);
      if (i === keep) caret = out.join(",").length + (afterComma && i < pieces.length - 1 ? 1 : 0);
    });
    input.value = out.join(",");
    if (caret != null) input.setSelectionRange(caret, caret);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }

  // --- Clicks. Each replaces the term under the caret.
  function commit(text, t) {
    const { start, end } = current();
    const v = input.value;
    input.value = v.slice(0, start) + text + v.slice(end);
    input.focus();
    input.setSelectionRange(start + text.length, start + text.length);
    const state = t ? match(t, text).state : "fail";
    pending = state === "slot" || state === "literal" ? t : null;
    if (!pending && templates.some((x) => match(x, text).state === "complete")) resolve(termIndex(), false);
    input.dispatchEvent(new Event("input", { bubbles: true })); // the chart's chips read the box
  }

  // A term row: keep what was typed of it, fill it up to its first parameter.
  function pickTerm(t) {
    const { frag } = current();
    const m = match(t, frag);
    if (m.state === "fail") commit(fillLits(t, 0, ""), t);
    else if (m.state === "literal") commit(fillLits(t, m.i + 1, frag.slice(0, m.at) + t.segs[m.i].lit), t);
    else commit(frag, t);
  }

  // A parameter value. Following a clicked template, run on to its next parameter.
  function pickOption(before, t, i, value) {
    const text = before + value;
    commit(t && t === pending ? fillLits(t, i + 1, text) : text, pending);
  }

  // --- Rendering.
  const head = el("div", null, "fh-head");
  const groupsBox = el("div", null, "fh-groups");
  const slotBox = el("div", null, "fh-slot");
  const empty = el("div", null, "fh-hint");
  const values = el("dl", null, "fh-values");

  function build() {
    const title = el("div");
    title.append(el("b", "Spot terms"), " ", el("span",
      "Join with commas; a hand must match all of them. A term that contradicts an earlier one replaces it. Type to narrow, click to add."));
    const close = el("button", "×");
    close.type = "button";
    close.title = "close";
    close.addEventListener("click", () => toggle(false));
    head.replaceChildren(title, close);

    templates = [];
    groupBoxes = [];
    groupsBox.replaceChildren();
    for (const g of vocab.groups) {
      const box = el("div", null, "fh-group");
      box.append(el("h3", g.name));
      const members = [];
      for (const term of g.terms) {
        const t = { ...term, segs: segments(term.term) };
        // What a substring search looks through: the term, and the term with each
        // street or decision point written in, so `fl` finds `cbet_<street>`.
        t.hay = [term.term, ...vocab.streets.map((s) => term.term.replace("<street>", s)),
          ...(term.term.includes("<spot>") ? vocab.decided.map((s) => term.term.replace("<spot>", s)) : [])]
          .map((h) => h.toLowerCase());
        t.row = el("div", null, "fh-row");
        const b = el("button", term.term);
        b.type = "button";
        b.title = `e.g. ${term.example}`;
        b.addEventListener("click", () => pickTerm(t));
        t.row.append(b, el("span", term.desc));
        box.append(t.row);
        templates.push(t);
        members.push(t);
      }
      groupBoxes.push([box, members]);
      groupsBox.append(box);
    }

    values.replaceChildren();
    const put = (k, list) => values.append(el("dt", k), el("dd", list));
    put("<street>", vocab.streets.join("  "));
    put("N", `a number, with ${vocab.operators.join("  ")}`);
    put("POS", vocab.positions.join("  "));
    put("SIZE", Object.entries(vocab.sizes).map(([k, d]) => `${k} (${d})`).join("  "));
    put("TEXTURE", vocab.textures.join("  "));
    put("NAME", "a player's name, as on the players page");

    panel.replaceChildren(head, groupsBox, slotBox, empty, values);
  }

  // Which list the fragment calls for: a parameter's values when the clicked
  // template, or every template the fragment fits, is waiting on the same kind
  // of parameter; otherwise the terms it fits.
  function view(frag) {
    const ms = templates.map((t) => [t, match(t, frag)]);
    if (pending) {
      const m = ms.find(([t]) => t === pending)[1];
      if (m.state === "slot") return { t: pending, m };
      if (m.state !== "literal") pending = null;
    }
    const live = ms.filter(([, m]) => m.state !== "fail");
    if (frag && live.length && live.every(([, m]) => m.state === "slot")
      && new Set(live.map(([t, m]) => t.segs[m.i].slot)).size === 1) {
      return { t: live[0][0], m: live[0][1] };
    }
    return { live: new Set(live.map(([t]) => t)) };
  }

  function renderSlot(frag, t, m) {
    const kind = t.segs[m.i].slot;
    const before = frag.slice(0, m.at);
    const top = el("div", null, "fh-slot-head");
    if (before) top.append(el("code", before), " — ");
    top.append(PICK[kind]);
    const box = el("div", null, "fh-opts");
    const opts = options(kind);
    if (!opts || (kind === "NAME" && !names.length)) {
      box.append(el("span", kind === "NAME" ? "type a player's name, as on the players page" : "then a comma for the next term", "fh-hint"));
    } else {
      const p = m.partial.toLowerCase();
      const shown = opts.filter(([v]) => (kind === "NAME" ? v.toLowerCase().includes(p) : v.toLowerCase().startsWith(p)));
      for (const [v, d] of shown) {
        const b = el("button", v);
        b.type = "button";
        if (d) b.append(el("span", d));
        b.addEventListener("click", () => pickOption(before, t, m.i, v));
        box.append(b);
      }
      if (!shown.length) {
        box.append(el("span", kind === "NAME" ? `no player called ‘${m.partial}’` : `nothing here starts ‘${m.partial}’`, "fh-hint"));
      }
    }
    slotBox.replaceChildren(top, box);
  }

  function update() {
    if (panel.hidden || !templates.length) return;
    const { frag } = current();
    const v = view(frag);
    const inSlot = !v.live;
    groupsBox.hidden = inSlot;
    slotBox.hidden = !inSlot;
    values.hidden = inSlot || frag !== "";
    empty.hidden = true;
    if (inSlot) return renderSlot(frag, v.t, v.m);
    const f = frag.toLowerCase();
    let any = false;
    for (const [box, members] of groupBoxes) {
      let shown = 0;
      for (const t of members) {
        const on = !f || v.live.has(t) || t.hay.some((h) => h.includes(f));
        t.row.hidden = !on;
        shown += on;
      }
      box.hidden = !shown;
      any ||= shown > 0;
    }
    if (!any) {
      empty.textContent = `No term matches ‘${frag}’.`;
      empty.hidden = false;
    }
  }

  // --- Loading. Fetched up front, quietly: conflicts are dropped whether or not
  // the panel was ever opened.
  let loading = null;
  function load() {
    loading ??= (async () => {
      const r = await fetch("/filters");
      if (!r.ok) throw new Error(`${r.status}`);
      vocab = await r.json();
      build();
      fetch("/players")
        .then((p) => (p.ok ? p.json() : []))
        .then((rows) => { names = rows.map((p) => p.alias).filter(Boolean); update(); })
        .catch(() => {});
    })().catch((e) => { loading = null; throw e; });
    return loading;
  }
  load().catch(() => {});

  async function toggle(open) {
    panel.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
    if (!open) return;
    if (!templates.length) {
      panel.replaceChildren(el("span", "loading…", "muted"));
      try {
        await load();
      } catch {
        panel.replaceChildren(el("span", "Could not load the term list. If the server is older than this page, restart `pnt service`.", "err"));
        return;
      }
    }
    update();
  }

  btn.addEventListener("click", () => toggle(panel.hidden));
  input.addEventListener("input", (e) => {
    if (e.inputType === "insertText" && e.data === ",") {
      pending = null;
      resolve(termIndex() - 1, true);
    }
    update();
  });
  input.addEventListener("click", update);
  input.addEventListener("keyup", (e) => { if (e.key.startsWith("Arrow") || e.key === "Home" || e.key === "End") update(); });
  // Before the page's own Enter handler reads the box (capture on document runs
  // first), and on blur, which comes before an Apply button's click.
  document.addEventListener("keydown", (e) => {
    if (e.target === input && e.key === "Enter") resolve(termIndex(), false);
  }, true);
  input.addEventListener("change", () => resolve(termIndex(), false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !panel.hidden) toggle(false);
  });
})();
