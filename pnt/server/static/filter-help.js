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
    /* Each a glyph and a word, so neither is a bare ? or ★ to guess at. */
    .fh-btn { padding: 5px 10px; border-radius: 999px; color: var(--ink-2); white-space: nowrap; }
    .fh-btn b { font-weight: 700; margin-right: 4px; }
    .fh-btn[aria-expanded=true] { background: var(--accent); color: #fff; border-color: transparent; }
    @media (max-width: 600px) { .fh-btn { padding: 2px 7px; } .fh-btn b { margin-right: 3px; } }
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
  const glyph = document.createElement("b");
  glyph.textContent = "?";
  btn.append(glyph, "Filters");
  btn.title = "every term the Spot box takes, with what it means: click one to add it";
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
  let tags = [];         // the /tags rows, for tag=
  let templates = [];    // one per vocabulary row, parsed into segments
  let groupBoxes = [];
  let pending = null;    // the template last clicked, followed through its parameters

  // --- Templates. A vocabulary term is literals and parameter slots:
  // `cbet_<street>=SIZE` is "cbet_", street, "=", SIZE. A trailing `>=N` is an
  // operator slot then a number, since every comparison takes any operator.
  const SLOT = /(<street>|<spot>|POS|SIZE|TEXTURE|DECISION|CLASS|NAME|LINE|SEQ|MADE|TIER|ANSWER|FLAG|TAG|DATE|DAYS)/;
  const OP_WORDS = { ">=": "at least", "<=": "at most", "=": "exactly", ">": "more than", "<": "less than" };
  const PICK = {
    street: "pick a street", spot: "pick a decision point", POS: "pick a position", SIZE: "pick a size",
    TEXTURE: "pick a texture", DECISION: "pick what they did", op: "pick a comparison", NAME: "pick a player",
    N: "type a number", CLASS: "type a hand like AKs, T9o, 77 or 72",
    MADE: "pick a made hand", TIER: "pick a tier", ANSWER: "pick fold, call or raise", FLAG: "pick a flag to negate",
    TAG: "pick a tag", DATE: "type a day as YYYY-MM-DD, or pick one", DAYS: "type a number of days then d, or pick one",
  };
  // The slots that take several values after a plain `=` or `!=`, joined by `|`.
  const MANY = new Set(["MADE", "TIER", "TAG"]);

  // The two pattern slots read differently on one street (`line_flop=`) and on
  // all three (`line=`, flop/turn/river split by `/`), so their hint and their
  // ready-made patterns depend on which template is being filled. Picking one
  // fills it in; typing over it is just as good, since the slot is free text.
  const PATTERN_HINT = {
    LINE: "their own actions on {where}: x check, b bet, c call, r raise, f fold."
      + " * is any number of actions (x* = checked, then anything); ? is exactly one, whichever it was"
      + " (x? = xc, xf or xr); - on its own = did not act",
    SEQ: "everyone's actions on {where}, in order: THEIRS in capitals (X B C R F), everyone else's in lower case."
      + " * is any number of actions (xB* = checked to them, they bet, then anything); ? is exactly one,"
      + " whichever it was (xB? = xBc or xBf); - on its own = not in the hand; case matters",
  };
  const PATTERNS = {
    LINE: [
      ["x", "checked"], ["xc", "check-called"], ["xf", "check-folded"], ["xr*", "check-raised"],
      ["b", "bet"], ["bc", "bet, called a raise"], ["bf", "bet, folded to a raise"], ["c", "called"],
    ],
    LINE_ALL: [
      ["xc/xc/xc", "check-called every street"], ["c/c/c", "called every street"], ["b/b/b", "bet every street"],
      ["b/x/b", "bet, checked the turn, bet the river"], ["b/x/", "bet the flop, checked the turn"],
      ["x/b/", "checked the flop, bet the turn"], ["xr*//", "check-raised the flop"],
    ],
    SEQ: [
      ["xB*", "checked to them, they bet"], ["xBr*", "…and got check-raised"], ["xBrF", "…and folded to it"],
      ["*Br*", "their bet got raised"], ["Xb*", "they checked, someone bet"], ["XbR*", "they check-raised"],
      ["bC*", "they called a bet"], ["bR*", "they raised a bet"],
    ],
    SEQ_ALL: [
      ["xB*/xB*/", "bet flop and turn after a check"], ["Bc/Bc/Bc", "bet every street, called each time"],
      ["Xb*/Xb*/", "checked and faced a bet on flop and turn"], ["xBr*//", "got check-raised on the flop"],
    ],
    // Days counted back from today, on this computer's calendar as the server's are.
    get DATE_ALL() { return [[dayAgo(0), "today"], [dayAgo(7), "a week ago"], [dayAgo(30), "30 days ago"]]; },
    DAYS_ALL: [["1d", "today"], ["7d", "today and the 6 days before"], ["30d", "the last 30 days"]],
  };
  function dayAgo(n) {
    const d = new Date();
    d.setDate(d.getDate() - n);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }
  const allStreets = (t) => !t.segs.some((s) => s.slot === "street");
  function pickText(t, kind) {
    if (MANY.has(kind)
      && t.segs.some((s, j) => s.slot === kind && /(^|[^<>])=$/.test(t.segs[j - 1]?.lit ?? ""))) {
      return `${PICK[kind]}; after one, click more to match any of them`;
    }
    if (!PATTERN_HINT[kind]) return PICK[kind];
    const where = allStreets(t) ? "each street, flop/turn/river split by / (empty = any)" : "that street";
    return "type " + PATTERN_HINT[kind].replace("{where}", where);
  }
  const patterns = (t, kind) => PATTERNS[allStreets(t) ? `${kind}_ALL` : kind] ?? [];

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
      case "MADE": return Object.entries(vocab.made).map(([v, d]) => (d ? [v, d] : [v]));
      case "TIER": return Object.entries(vocab.tiers ?? {});
      case "ANSWER": return plain(vocab.answers);
      case "FLAG": return plain(vocab.flags);
      case "TAG": return [["flagged", "the 🚩 ones"], ...tags.map((t) => [t.tag, `${t.hands} hand${t.hands === 1 ? "" : "s"}`])];
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
      if (!opts) return { state: "slot", i, at: pos, partial: rest }; // a number or a hand: the rest is the value
      const vals = opts.map(([v]) => v.toLowerCase());
      // After a plain `=` or `!=` a made hand takes several, `made_flop=set|flush`,
      // so the slot runs on past each one. `from` is where the first one starts.
      const many = MANY.has(s.slot) && /(^|[^<>])=$/.test(t.segs[i - 1]?.lit ?? "");
      const from = pos;
      for (;;) {
        const piece = frag.slice(pos);
        const low = piece.toLowerCase();
        const inSlot = { state: "slot", i, at: pos, from, partial: piece };
        // A finished one at the end of the term -- `flush` too, though `flush_draw`
        // starts with it: stay open, so a click adds the next and typing runs on.
        if (many && i === t.segs.length - 1 && vals.includes(low)) {
          return { state: "slot", i, at: frag.length, from, partial: "", more: true };
        }
        // `>` could still become `>=`, and `UTG` `UTG+1`: stay in the slot.
        if (vals.some((v) => v.startsWith(low) && v !== low)) return inSlot;
        const hit = vals.filter((v) => low.startsWith(v)).sort((a, b) => b.length - a.length)[0];
        // A name not seen yet is still a name, and a tag no hand carries yet a tag.
        if (hit == null) return s.slot === "NAME" || s.slot === "TAG" ? inSlot : FAIL;
        pos += hit.length;
        if (!many || frag[pos] !== "|") break;
        pos += 1;
      }
    }
    return pos === frag.length ? { state: "complete" } : FAIL;
  }

  // `text` with the literals from segment `i` on added, up to the next slot.
  function fillLits(t, i, text) {
    while (i < t.segs.length && t.segs[i].lit != null) text += t.segs[i++].lit;
    return text;
  }

  // --- Where terms start and end. Brackets nest and `|` binds tighter than `,`
  // (SPEC.md, "Filter language"), so the box is never cut at a comma inside a
  // bracket. The top-level pieces are what the conflicts compare: a bracketed
  // piece takes no part, and `3bet_pot|4bet_pot` is one piece.
  function pieces(v) {
    const out = [];
    let depth = 0, from = 0;
    for (let i = 0; i < v.length; i++) {
      if (v[i] === "(") depth++;
      else if (v[i] === ")") depth = Math.max(0, depth - 1);
      else if (v[i] === "," && !depth) { out.push([from, i]); from = i + 1; }
    }
    out.push([from, v.length]);
    return out;
  }
  const atTop = (v, at) => pieces(v.slice(0, at + 1)).length > pieces(v.slice(0, at)).length;
  const OPERATOR = /[!=<>]/;
  // A segment that starts a `key=value` list (`position=BTN`), which the bare
  // segments after it continue (`|CO`), as the parser reads them.
  const LIST_HEAD = /^\s*[^\s!=<>()][^()]*?(!=|>=|<=|=|>|<)/;

  // --- The term under the caret: back to the `,` or `(` at its own depth, then on
  // past a `|` unless what follows is one more value of the term before it
  // (`made_flop=set|fl`), so a click after `3bet_pot|` adds a term to the OR
  // instead of replacing it.
  function current() {
    const v = input.value;
    const caret = input.selectionStart ?? v.length;
    let start = 0;
    const outer = [];
    for (let i = 0; i < caret; i++) {
      if (v[i] === "(") { outer.push(start); start = i + 1; }
      else if (v[i] === ")") { if (outer.length) start = outer.pop(); }
      else if (v[i] === ",") start = i + 1;
    }
    const segs = [];
    let depth = 0, from = start;
    for (let i = start; i < caret; i++) {
      if (v[i] === "(") depth++;
      else if (v[i] === ")") depth--;
      else if (v[i] === "|" && !depth) { segs.push([from, i]); from = i + 1; }
    }
    let afterBar = false;
    if (segs.length && !OPERATOR.test(v.slice(from, caret))) {
      let k = segs.length - 1;
      while (k >= 0 && !OPERATOR.test(v.slice(...segs[k])) && !/[()]/.test(v.slice(...segs[k]))) k--;
      const head = k >= 0 ? v.slice(...segs[k]) : "";
      if (LIST_HEAD.test(head)) start = segs[k][0];
      else { start = from; afterBar = true; }
    } else if (segs.length) {
      start = from;
      afterBar = true;
    }
    while (start < caret && v[start] === " ") start++;
    let end = caret;
    depth = 0;
    for (; end < v.length; end++) {
      const ch = v[end];
      if (ch === "(") depth++;
      else if (ch === ")") { if (!depth) break; depth--; }
      else if (!depth && (ch === "," || (afterBar && ch === "|"))) break;
    }
    return { start, end, frag: v.slice(start, caret) };
  }
  const termIndex = () => {
    const v = input.value;
    const caret = input.selectionStart ?? v.length;
    return pieces(v).findIndex(([s, e]) => caret >= s && caret <= e);
  };

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
  // Which end of the dates a term bounds: `since:` and `last:` both set the first
  // day, so either restates the other, and `before:` the last.
  const DATE_SIDE = { since: "from", last: "from", before: "to" };
  const dateSide = (t) => DATE_SIDE[t.trim().toLowerCase().match(/^(since|before|last):/)?.[1]];

  function conflicts(a, b) {
    if (a.toLowerCase() === b.toLowerCase()) return true;
    if (dateSide(a) || dateSide(b)) return dateSide(a) === dateSide(b);
    // `!x` against `x`, or against a sized or compared `x` (`!cbet_flop` and
    // `cbet_flop=medium`): no hand is both.
    const negates = (x, y) => x.startsWith("!") && parts(y).name === x.slice(1).toLowerCase();
    if (negates(a, b) || negates(b, a)) return true;
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
    if (p.name === "vs" || p.name === "tag") return false; // two opponents, or two tags, on one hand is a real spot
    if (p.op === "=" || q.op === "=") return true;
    // `pot>=500,pot<=1000` is a range; `pot>=500,pot>=200` restates the bound.
    return (LOWER.has(p.op) && LOWER.has(q.op)) || (UPPER.has(p.op) && UPPER.has(q.op));
  }

  // Drop every term that conflicts with the term at `keep` (the one just
  // finished, which wins wherever it sits) or with a later term. With
  // `afterComma` the caret lands after the comma that follows `keep`.
  function resolve(keep, afterComma) {
    const v = input.value;
    const parts = pieces(v).map(([s, e]) => v.slice(s, e));
    const terms = parts.map((s) => s.trim());
    // A half-typed `pot>=` takes no part, nor does a bracket: its terms are not the box's.
    const ready = (t) => t && !/(!=|>=|<=|=|>|<)$/.test(t) && !/[()]/.test(t);
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
    parts.forEach((p, i) => {
      if (dropped.has(i)) return;
      out.push(p);
      if (i === keep) caret = out.join(",").length + (afterComma && i < parts.length - 1 ? 1 : 0);
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
    put("LINE", "x check  b bet  c call  r raise  f fold  * any run  ? one action  - didn't act");
    put("SEQ", "as LINE, theirs in CAPS (X B C R F), others' lower case  - not in the hand");
    put("MADE", Object.keys(vocab.made).join("  "));
    put("TIER", Object.keys(vocab.tiers ?? {}).join("  "));
    put("ANSWER", vocab.answers.join("  "));
    put("NAME", "a player's name, as on the players page");
    put("TAG", "a tag you put on a hand (the + tag box on the chart's rows)  flagged the 🚩 ones");
    put("DATE", "a day as YYYY-MM-DD, on this computer's calendar");
    put("DAYS", "a number of days then d: 1d today, 7d today and the 6 before");

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
    // Made hands already in the term (`set|flush|…`) are not offered again, and
    // once one is finished a click joins the next with `|`.
    const chosen = m.from == null ? [] : frag.slice(m.from, m.at).toLowerCase().split("|").filter(Boolean);
    const before = frag.slice(0, m.at) + (m.more ? "|" : "");
    const top = el("div", null, "fh-slot-head");
    if (before) top.append(el("code", before.replace(/\|$/, "")), " — ");
    top.append(m.more
      ? "click another to add it (a hand matches any of them), or type a comma for the next term"
      : pickText(t, kind));
    const box = el("div", null, "fh-opts");
    const opts = options(kind);
    // Case matters in an action sequence, so its patterns narrow by exact prefix.
    const fold = kind === "SEQ" ? (s) => s : (s) => s.toLowerCase();
    const ready = patterns(t, kind).filter(([v]) => fold(v).startsWith(fold(m.partial)));
    if (!opts && ready.length) {
      for (const [v, d] of ready) {
        const b = el("button", v);
        b.type = "button";
        b.append(el("span", d));
        b.addEventListener("click", () => pickOption(before, t, m.i, v));
        box.append(b);
      }
    } else if (!opts || (kind === "NAME" && !names.length)) {
      box.append(el("span", kind === "NAME" ? "type a player's name, as on the players page" : "then a comma for the next term", "fh-hint"));
    } else {
      const p = m.partial.toLowerCase();
      const shown = opts.filter(([v]) => (kind === "NAME" ? v.toLowerCase().includes(p) : v.toLowerCase().startsWith(p)))
        .filter(([v]) => !chosen.includes(v.toLowerCase()));
      for (const [v, d] of shown) {
        const b = el("button", v);
        b.type = "button";
        if (d) b.append(el("span", d));
        b.addEventListener("click", () => pickOption(before, t, m.i, v));
        box.append(b);
      }
      if (!shown.length) {
        box.append(el("span", kind === "NAME" ? `no player called ‘${m.partial}’`
          : kind === "TAG" ? `no hand tagged ‘${m.partial}’ yet` : `nothing here starts ‘${m.partial}’`, "fh-hint"));
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
      fetch("/tags")
        .then((t) => (t.ok ? t.json() : []))
        .then((rows) => { tags = rows; update(); })
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
      // A comma inside a bracket finishes no top-level term.
      if (atTop(input.value, (input.selectionStart ?? input.value.length) - 1)) resolve(termIndex() - 1, true);
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
  // A tag put on a hand anywhere on the page (replay.js's tag box) is one to offer.
  document.addEventListener("pnt-tags", () => {
    fetch("/tags").then((t) => (t.ok ? t.json() : tags)).then((rows) => { tags = rows; update(); }).catch(() => {});
  });

  // --- Saved spots: the ★ beside the `?`. A dropdown of the spots you saved
  // (GET /spots), each applied with a click -- into the box, then the page's own
  // Apply -- and renamed, pinned or deleted in place; "Save current spot" names the
  // box's filter. Every change is sent to the page as a `pnt-spots` event on
  // `document` with the whole list, which the chart reads for its pinned chips.
  // No browser dialogs: names are typed inline, and delete asks by a second click.
  {
    const css = document.createElement("style");
    css.textContent = `
      .fh-star-wrap { position: relative; display: inline-flex; }
      .fh-star { font-weight: 400; }
      .fh-spots { position: absolute; top: calc(100% + 4px); left: 0; z-index: 40; width: 300px; max-width: calc(100vw - 32px);
        background: var(--surface); border: 1px solid var(--ring); border-radius: 10px; padding: 6px;
        box-shadow: 0 6px 24px rgba(0,0,0,.18); font-size: 12px; line-height: 1.3; }
      .fh-spots[hidden] { display: none; }
      .fh-spots h3 { font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase;
        letter-spacing: .04em; margin: 2px 6px 4px; }
      .fh-spot { display: grid; grid-template-columns: 1fr auto; align-items: center; gap: 0 4px; padding: 4px 6px; border-radius: 6px; }
      .fh-spot:hover { background: var(--mid); }
      .fh-spot .nm { border: 0; background: transparent; padding: 0; color: var(--ink); font: inherit; font-weight: 600;
        text-align: left; overflow-wrap: anywhere; }
      .fh-spot .nm:hover, .fh-spot .nm:focus-visible { color: var(--accent); outline: 0; }
      .fh-spot code { grid-column: 1; font-family: ui-monospace, Consolas, monospace; font-size: 11px; color: var(--muted);
        overflow-wrap: anywhere; }
      .fh-spot .acts { grid-column: 2; grid-row: 1 / span 2; display: flex; gap: 1px; }
      .fh-spot .acts button { border: 0; background: transparent; padding: 2px 5px; border-radius: 4px; color: var(--muted); font-size: 12px; }
      .fh-spot .acts button:hover, .fh-spot .acts button:focus-visible { background: var(--surface); color: var(--ink); outline: 0; }
      .fh-spot .acts .pin { filter: grayscale(1); opacity: .5; }
      .fh-spot .acts .pin[aria-pressed=true] { filter: none; opacity: 1; }
      .fh-spot .acts .armed { color: #c62f2f; font-weight: 600; }
      .fh-spots input { width: 100%; box-sizing: border-box; font: inherit; padding: 4px 8px; border: 1px solid var(--ring);
        border-radius: 6px; background: var(--surface); color: var(--ink); }
      .fh-spots input:focus { border-color: var(--accent); outline: 0; }
      .fh-spots .save { width: 100%; margin-top: 4px; border: 1px dashed var(--ring); background: transparent; color: var(--accent);
        border-radius: 6px; padding: 5px 8px; font: inherit; text-align: left; }
      .fh-spots .save:disabled { color: var(--muted); cursor: default; }
      .fh-spots .fh-hint, .fh-spots .err { display: block; margin: 4px 6px 0; }
      .fh-spots .err { color: #c62f2f; }
    `;
    document.head.appendChild(css);

    const wrap = el("span", null, "fh-star-wrap");
    const star = el("button", null, "fh-btn fh-star");
    star.append(el("b", "★"), "Saved");
    star.type = "button";
    star.title = "your saved spots: apply one, or save the Spot box's under a name";
    star.setAttribute("aria-expanded", "false");
    star.setAttribute("aria-haspopup", "true");
    const menu = el("div", null, "fh-spots");
    menu.hidden = true;
    menu.setAttribute("role", "dialog");
    menu.setAttribute("aria-label", "Saved spots");
    wrap.append(star, menu);
    btn.after(wrap);

    let spots = [];
    let saving = false;    // the "Save current spot" name box is open
    let renaming = null;   // the spot_id whose name is being edited
    let armed = null;      // the spot_id whose delete is waiting for its second click
    let armTimer = null;
    let error = "";

    const send = async (method, url, body) => {
      const r = await fetch(url, {
        // x-pnt: the server refuses writes without it (see app.py).
        method, headers: { "Content-Type": "application/json", "x-pnt": "1" },
        body: body ? JSON.stringify(body) : undefined,
      });
      const out = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(out.detail || `${r.status} ${r.statusText}`);
      return out;
    };
    async function refresh() {
      const r = await fetch("/spots");
      if (!r.ok) throw new Error(`${r.status}`);
      spots = await r.json();
      document.dispatchEvent(new CustomEvent("pnt-spots", { detail: spots }));
      draw();
    }
    // One write, then the list read back; a refusal stays on screen, in the menu.
    async function change(method, url, body) {
      error = "";
      try {
        await send(method, url, body);
        await refresh();
        return true;
      } catch (e) {
        error = String(e.message || e);
        draw();
        return false;
      }
    }

    function nameBox(value, placeholder, onEnter, onCancel) {
      const box = el("input");
      box.type = "text"; box.value = value; box.placeholder = placeholder; box.maxLength = 80;
      box.addEventListener("keydown", async (e) => {
        if (e.key === "Escape") { e.stopPropagation(); onCancel(); return; }
        if (e.key !== "Enter") return;
        e.preventDefault();
        if (box.value.trim()) await onEnter(box.value);
      });
      requestAnimationFrame(() => { box.focus(); box.select(); });
      return box;
    }

    function useSpot(s) {
      input.value = s.filter;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      open(false);
      const apply = document.getElementById("apply");
      if (apply) apply.click();
      else input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    }

    function row(s) {
      const r = el("div", null, "fh-spot");
      if (renaming === s.spot_id) {
        r.append(nameBox(s.name, "name", async (name) => {
          if (await change("PATCH", `/spots/${s.spot_id}`, { name })) { renaming = null; draw(); }
        }, () => { renaming = null; draw(); star.focus(); }));
      } else {
        const nm = el("button", s.name, "nm");
        nm.type = "button"; nm.title = `apply ${s.filter}`;
        nm.addEventListener("click", () => useSpot(s));
        r.append(nm);
      }
      const acts = el("span", null, "acts");
      const act = (text, title, cls, fn) => {
        const b = el("button", text, cls);
        b.type = "button"; b.title = title;
        b.setAttribute("aria-label", `${title}: ${s.name}`);
        b.addEventListener("click", fn);
        acts.append(b);
        return b;
      };
      act("✎", "rename", "", () => { renaming = s.spot_id; armed = null; draw(); });
      act("📌", s.pinned ? "unpin from the chart's Common row" : "pin to the chart's Common row", "pin",
        () => change("PATCH", `/spots/${s.spot_id}`, { pinned: !s.pinned })).setAttribute("aria-pressed", String(s.pinned));
      act(armed === s.spot_id ? "delete?" : "×", armed === s.spot_id ? "click again to delete" : "delete",
        armed === s.spot_id ? "armed" : "", () => {
          clearTimeout(armTimer);
          if (armed !== s.spot_id) {
            armed = s.spot_id;
            armTimer = setTimeout(() => { armed = null; draw(); }, 3000);
            draw();
            return;
          }
          armed = null;
          change("DELETE", `/spots/${s.spot_id}`);
        });
      r.append(acts, el("code", s.filter));
      return r;
    }

    function draw() {
      if (menu.hidden) return;
      const kids = [el("h3", "Saved spots")];
      if (!spots.length) kids.push(el("span", "none yet: save the spot in the box, e.g. opener,position=BTN", "fh-hint"));
      kids.push(...spots.map(row));
      const filter = input.value.trim();
      if (saving) {
        kids.push(nameBox("", `name for ${filter}`, async (name) => {
          if (await change("POST", "/spots", { name, filter })) { saving = false; draw(); }
        }, () => { saving = false; draw(); star.focus(); }));
      } else {
        const save = el("button", filter ? "Save current spot…" : "Save current spot… (type one first)", "save");
        save.type = "button";
        save.disabled = !filter;
        save.title = filter;
        save.addEventListener("click", () => { saving = true; armed = null; draw(); });
        kids.push(save);
      }
      if (error) kids.push(el("span", error, "err"));
      menu.replaceChildren(...kids);
    }

    function open(on) {
      menu.hidden = !on;
      star.setAttribute("aria-expanded", String(on));
      saving = false; renaming = null; armed = null; error = "";
      if (!on) return;
      menu.style.left = "0px";
      draw();
      // Kept on screen at phone width: pulled left by however far it runs over.
      const over = menu.getBoundingClientRect().right - (document.documentElement.clientWidth - 16);
      if (over > 0) menu.style.left = `${-over}px`;
      refresh().catch(() => { error = "Could not load the saved spots. If the server is older than this page, restart `pnt service`."; draw(); });
    }

    star.addEventListener("click", () => open(menu.hidden));
    document.addEventListener("mousedown", (e) => { if (!menu.hidden && !wrap.contains(e.target)) open(false); });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !menu.hidden) { open(false); star.focus(); }
    });
    // Once at load, quietly, so the chart's pins are there before the menu is opened.
    refresh().catch(() => {});
  }
})();
