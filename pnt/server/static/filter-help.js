// The `?` beside a page's Spot box: every term a filter understands, read from
// /filters so the list cannot drift from the parser. Clicking a term adds it to
// the box. Shared by chart.html and stats.html; styled from their CSS variables.
(() => {
  const input = document.getElementById("filter");
  if (!input) return;

  const style = document.createElement("style");
  style.textContent = `
    .fh-btn { width: 28px; padding: 5px 0; border-radius: 999px; font-weight: 600; color: var(--ink-2); }
    .fh-btn[aria-expanded=true] { background: var(--accent); color: #fff; border-color: transparent; }
    .fh { background: var(--surface); border: 1px solid var(--ring); border-radius: 10px;
      padding: 8px 12px 10px; margin: 0 0 12px; font-size: 12px; line-height: 1.3; }
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

  const el = (tag, text, cls) => {
    const e = document.createElement(tag);
    if (text != null) e.textContent = text;
    if (cls) e.className = cls;
    return e;
  };

  // Append to what is already typed. A term with a placeholder (N, POS, SIZE...)
  // has its example value selected, so typing replaces it.
  function add(term, example) {
    const typed = input.value.trim().replace(/,+$/, "");
    input.value = typed ? `${typed},${example}` : example;
    input.focus();
    const end = input.value.length;
    const op = example.match(/(!=|>=|<=|=|>|<)([^=<>!]*)$/);
    if (op && /[A-Z]$/.test(term)) input.setSelectionRange(end - op[2].length, end);
    else input.setSelectionRange(end, end);
  }

  function render(v) {
    const head = el("div", null, "fh-head");
    const title = el("div");
    title.append(el("b", "Spot terms"), " ", el("span", "Join with commas; a hand must match all of them. Click a term to add it."));
    const close = el("button", "×");
    close.type = "button";
    close.title = "close";
    close.addEventListener("click", () => toggle(false));
    head.append(title, close);

    const groups = el("div", null, "fh-groups");
    for (const g of v.groups) {
      const box = el("div", null, "fh-group");
      box.append(el("h3", g.name));
      for (const t of g.terms) {
        const row = el("div", null, "fh-row");
        const b = el("button", t.term);
        b.type = "button";
        b.title = `add ${t.example}`;
        b.addEventListener("click", () => add(t.term, t.example));
        row.append(b, el("span", t.desc));
        box.append(row);
      }
      groups.append(box);
    }

    const values = el("dl", null, "fh-values");
    const put = (k, list) => values.append(el("dt", k), el("dd", list));
    put("<street>", v.streets.join("  "));
    put("N", `a number, with ${v.operators.join("  ")}`);
    put("POS", v.positions.join("  "));
    put("SIZE", Object.entries(v.sizes).map(([k, d]) => `${k} (${d})`).join("  "));
    put("TEXTURE", v.textures.join("  "));
    put("NAME", "a player's name, as on the players page");

    panel.replaceChildren(head, groups, values);
  }

  let loaded = false;
  async function toggle(open) {
    panel.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
    if (!open || loaded) return;
    panel.replaceChildren(el("span", "loading…", "muted"));
    try {
      const r = await fetch("/filters");
      if (!r.ok) throw new Error(`${r.status}`);
      render(await r.json());
      loaded = true;
    } catch {
      panel.replaceChildren(el("span", "Could not load the term list. If the server is older than this page, restart `pnt service`.", "err"));
    }
  }

  btn.addEventListener("click", () => toggle(panel.hidden));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !panel.hidden) toggle(false);
  });
})();
