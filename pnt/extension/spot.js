/* Following the live spot: which player the chart should show, and in which
 * spot, given what /live/{game} said. Pure functions, so `spot.test.mjs` can
 * pin them down without a browser.
 *
 * The rule: the chart follows the player the action is on, unless a row has
 * been pinned, and it moves only when the resolved spot actually changes. A
 * player with no data behind their spot is shown on all their hands (nextSpot).
 */
(function (root) {
  "use strict";

  const END_LINE = /^-- ending hand #\d+ --$/;

  /** True when any of these log entries closes a hand. */
  function handEnded(entries) {
    return (entries || []).some((e) => END_LINE.test(e.entry || ""));
  }

  /** The /live player row the table says is to act, which moves before the log
   * does. `acting` is that seat's PokerNow ID when content.js could read it off
   * the stacks line, else the name on the table's decision-current seat
   * (watch.js). The ID still matches when /live masks the names (the sample).
   * Null unless it names exactly one player still in. */
  function tableRow(live, acting) {
    if (!acting) return null;
    const players = (live?.players || []).filter((p) => !p.folded);
    const byId = players.filter((p) => p.pn_id === acting);
    const hits = byId.length ? byId : players.filter((p) => p.name === acting);
    return hits.length === 1 ? hits[0] : null;
  }

  /** The pn_id the action is on: the table's word when it names someone, else
   * the log's. */
  function actingId(live, acting) {
    return tableRow(live, acting)?.pn_id ?? live?.to_act ?? null;
  }

  /** The /live row the chart should show: the pinned player's seat if they are
   * seated, else the seat the action is on. Null when neither is known.
   *
   * A row, not an alias: one player merged from two accounts can hold two seats
   * at once, and the spot is the seat's. A pin then takes the seat to act, else
   * one still in. */
  function pickRow(live, pinned, acting) {
    const players = live?.players || [];
    const mine = pinned ? players.filter((p) => p.alias === pinned) : [];
    if (mine.length) {
      const id = actingId(live, acting);
      return mine.find((p) => p.pn_id === id) ?? mine.find((p) => !p.folded) ?? mine[0];
    }
    const table = tableRow(live, acting);
    if (table?.alias) return table;
    const logged = live?.to_act ? players.find((p) => p.pn_id === live.to_act) : null;
    return logged?.alias ? logged : null;
  }

  /** The next spot to put in the chart -- with the seat it is read from, `pn_id`,
   * since one player can hold two -- or null when there is nothing new to
   * show: no player to follow, or the same player and filter as last time.
   *
   * A player with no data behind their spot is still followed, on all their
   * hands. Leaving the chart where it was kept it on whoever last had data --
   * against a new opponent, that was never them, so it stopped alternating.
   *
   * So is a player the table has put to act before the log has: their resolved
   * spot is the one they were in before the action came round, so it is left out
   * until /live agrees, and the chart shows all their hands meanwhile. */
  function nextSpot(live, pinned, lastSent, acting) {
    const p = pickRow(live, pinned, acting);
    if (!p) return null;
    const player = p.alias;
    const ahead = player !== pinned && p.pn_id !== live.to_act;
    const resolved = ahead ? null : p.resolved;
    const filter = resolved?.filter || "";
    if (lastSent && lastSent.player === player && lastSent.filter === filter) return null;
    if (!resolved) return { player, pn_id: p.pn_id, filter, exact: false, label: "all hands" };
    return { player, pn_id: p.pn_id, filter, exact: !!resolved.exact, label: resolved.label || "" };
  }

  function paramOf(url, key) {
    try { return new URL(url).searchParams.get(key); } catch { return null; }
  }

  /** Whether a chart URL is one the HUD itself asked for. The chart reports its
   * URL after every change; one that matches a spot the HUD sent is an echo,
   * anything else is the user reaching in. */
  function isEcho(url, sent) {
    const list = Array.isArray(sent) ? sent : sent ? [sent] : [];
    const player = paramOf(url, "player");
    const filter = paramOf(url, "filter") || "";
    return list.some((s) => s && s.player === player && (s.filter || "") === filter);
  }

  /** How often they took each decision: `fold 20% · call 60% · raise 20%`. */
  function mix(decisions) {
    const entries = Object.entries(decisions || {}).filter(([, n]) => n > 0);
    const total = entries.reduce((a, [, n]) => a + n, 0);
    if (!total) return "";
    return entries.map(([k, n]) => `${k} ${Math.round((100 * n) / total)}%`).join(" · ");
  }

  // The server's five buckets as the four words on the line: a pair of their own
  // short of strong on that board is marginal, whatever else it is.
  const WORDS = [
    ["value", (r) => r.strong],
    ["marginal", (r) => (r.medium || 0) + (r.weak || 0)],
    ["draw", (r) => r.draw],
    ["air", (r) => r.air],
  ];

  /** What they showed up with after one decision, as shares of the hands whose
   * cards are known: `value 50% · draw 25% · air 25% (4 of 9 shown)`. Empty when
   * none are known -- a split over nothing is not a split. */
  function showText(row) {
    if (!row || !row.known) return "";
    const parts = WORDS
      .map(([word, of]) => [word, of(row) || 0])
      .filter(([, n]) => n > 0)
      .map(([word, n]) => `${word} ${Math.round((100 * n) / row.known)}%`);
    return `${parts.join(" · ")} (${row.known} of ${row.hands} shown)`;
  }

  /** The spot cell for one /live player row: a short line, and a hover text
   * with the filter and how it was widened.
   *
   * The line is what they showed up with: after the decision they just made,
   * or one row per decision while they are still to act. A spot answered from
   * earlier on their path -- a node nothing tracks, or a preflop spot they have
   * no history at -- reads "arrived with", since those hands are the range they
   * brought here, read on this street's board. How often they fold, call or
   * raise is in the hover. */
  function spotText(p) {
    if (!p) return { text: "", title: "" };
    if (p.folded) return { text: "folded", title: "" };
    const node = p.node;
    if (!node) return { text: "", title: "" };
    const where = node.decision ? `${node.label} → ${node.decision}` : node.label;
    const r = p.resolved;
    if (!r) {
      return { text: `${where} · no data`, title: `${where}\nno hands in this spot, nor in any wider one` };
    }
    const sh = r.showings || {};
    let body;
    if (r.arrived || r.decision) {
      const s = showText(sh[r.decision]);
      body = !s ? `no shown hands (${r.hands})` : r.arrived ? `arrived with ${s}` : s;
    } else {
      const rows = Object.entries(sh).map(([d, row]) => [d, showText(row)]).filter(([, s]) => s);
      body = rows.length ? rows.map(([d, s]) => `${d}: ${s}`).join(" · ") : `no shown hands (${r.hands})`;
    }
    const approx = r.exact ? "" : "≈ ";
    const text = `${where} · ${approx}${body}`;
    const m = mix(r.decisions);
    const title = [
      where,
      (r.exact ? "spot: " : "closest spot: ") + r.filter,
      ...(r.relaxed || []),
      ...(r.texture?.length ? [`boards like this one: ${r.texture.join(", ")}`] : []),
      `${r.hands} hands, ${r.known} with cards known`,
      ...(m ? [`${r.label}: ${m}`] : []),
      ...(r.street ? [`hands read on the ${r.street}`] : []),
    ].join("\n");
    return { text, title };
  }

  /** How a /live player got to the spot the spot line shows: their earlier
   * decisions this hand, oldest first, each as street and decision only
   * ("pre call · flop check"); `title` has the full node labels. The spot line
   * has the latest one, so it is left off -- unless they folded, when the spot
   * line has nothing to add and the fold itself belongs here. Empty before
   * they have acted. A flop three or more saw leads with "3-way" (the count),
   * since their spot was looked up among multiway flops alone. */
  function pathText(p) {
    const path = Array.isArray(p?.path) ? p.path : [];
    const earlier = p?.folded ? path : path.slice(0, -1);
    const steps = earlier.map((s) => {
      const [label, decision] = String(s).split(" → ");
      if (!decision) return "";
      const street = (label.match(/\((flop|turn|river)\)/) || [])[1] || "pre";
      return `${street} ${decision}`;
    }).filter(Boolean);
    const n = p?.node?.players_flop;
    const way = n >= 3 ? [`${n}-way`] : [];
    const title = [...(way.length ? [`${n} players saw the flop`] : []), ...earlier];
    return { text: [...way, ...steps].join(" · "), title: title.join("\n") };
  }

  /** The hover text of a tag chip: the evidence, then the count it rests on. */
  function tagTitle(tag) {
    if (!tag) return "";
    const n = tag.n ?? 0;
    const count = tag.hits != null && tag.hits !== n ? `${tag.hits} of ${n}` : `${n} hands`;
    const c = tag.carried_by;
    const streaky = c ? `One session only: gone without a ${c.hands}-hand game (${c.sessions} sessions in all).` : "";
    return [tag.tip || tag.label || "", count, streaky].filter(Boolean).join("\n");
  }

  /** The chips in the order a card shows them: the archetype first, then the
   * rest as the server sent them. */
  function tagChips(tags) {
    const list = Array.isArray(tags) ? tags.filter(Boolean) : [];
    return [...list.filter((t) => t.kind === "archetype"), ...list.filter((t) => t.kind !== "archetype")];
  }

  // Wilson 95% interval for a rate of p (0..1) over n chances, as [lo, hi]
  // fractions; null with no chances. Mirrors queries.wilson (SPEC.md,
  // "Confidence intervals"); stats.html and chart.html carry the same copy.
  function wilson(p, n, z = 1.96) {
    if (!(n > 0) || p == null) return null;
    const z2 = z * z;
    const centre = (p + z2 / (2 * n)) / (1 + z2 / n);
    const half = z * Math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n);
    return [Math.max(0, centre - half), Math.min(1, centre + half)];
  }

  // A rate in percent over n chances, as hover text: "± 8.6 pts (95%: 31.2–48.5%)".
  // Wilson is lopsided near 0% and 100%, so the range is printed beside the ±.
  function ciText(pct, n) {
    const ci = pct == null ? null : wilson(pct / 100, n);
    if (!ci) return "";
    const [lo, hi] = ci.map((x) => 100 * x);
    return `± ${((hi - lo) / 2).toFixed(1)} pts (95%: ${lo.toFixed(1)}–${hi.toFixed(1)}%)`;
  }

  // The HUD's clock on the player to act: how long since the action reached them,
  // as "7s" or "1:05". Empty when unknown. See SPEC.md, "Timing tells".
  function clockText(ms) {
    if (ms == null || !(ms >= 0)) return "";
    const s = Math.floor(ms / 1000);
    return s < 60 ? `${s}s` : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }

  const api = { handEnded, actingId, pickRow, nextSpot, isEcho, spotText, showText, pathText, mix, tagTitle, tagChips, wilson, ciText, clockText };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.PNT = Object.assign(root.PNT || {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this);
