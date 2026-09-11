/* Turn whatever `GET /games/{id}/log` returns into the entries `/ingest` takes.
 *
 * The exact JSON envelope of that endpoint is the one assumption in this project
 * not yet confirmed against a live table (docs/findings.md §8). So this file is
 * deliberately the only place that knows about it, it accepts every shape seen
 * in PokerNowGrabber-style tools, and it reports what it saw so the popup can say
 * "recognized" or "unrecognized" instead of silently ingesting nothing.
 *
 * Output entries match the CSV export exactly: {entry, at, order}. That is the
 * whole point -- live capture and backfill feed one parser with identical input.
 *
 * Plain script (no modules): loaded before content.js on the page, and required
 * by the node test.
 */
(function (root) {
  "use strict";

  // Unwrap the envelope: a bare array, or an object with the array under one of
  // these keys.
  const LIST_KEYS = ["logs", "log", "entries", "data", "items", "result"];

  const ENTRY_KEYS = ["entry", "msg", "message", "text", "line"];
  const AT_KEYS = ["at", "createdAt", "created_at", "time", "ts", "timestamp"];
  const ORDER_KEYS = ["order", "ord", "index", "seq", "id"];

  function pick(obj, keys) {
    for (const k of keys) if (obj[k] !== undefined && obj[k] !== null) return [k, obj[k]];
    return [null, undefined];
  }

  function toIso(v) {
    if (typeof v === "number") return new Date(v).toISOString();
    if (typeof v === "string") {
      if (/^\d{12,}$/.test(v)) return new Date(Number(v)).toISOString();
      const d = new Date(v);
      return isNaN(d) ? null : d.toISOString();
    }
    return null;
  }

  function unwrap(body) {
    if (Array.isArray(body)) return ["array", body];
    if (body && typeof body === "object") {
      for (const k of LIST_KEYS) if (Array.isArray(body[k])) return [k, body[k]];
    }
    return [null, null];
  }

  /**
   * @returns {{entries: Array<{entry:string, at:string, order:number}>,
   *            shape: {list:string|null, entry:string|null, at:string|null, order:string|null, synthesized_order:boolean},
   *            sample: any, ok: boolean}}
   */
  function normalize(body) {
    const [listKey, list] = unwrap(body);
    const shape = { list: listKey, entry: null, at: null, order: null, synthesized_order: false };
    if (!list) return { entries: [], shape, sample: body, ok: false };
    if (!list.length) return { entries: [], shape, sample: null, ok: true };

    const first = list[0];
    if (!first || typeof first !== "object") return { entries: [], shape, sample: first, ok: false };
    shape.entry = pick(first, ENTRY_KEYS)[0];
    shape.at = pick(first, AT_KEYS)[0];
    shape.order = pick(first, ORDER_KEYS)[0];
    if (!shape.entry || !shape.at) return { entries: [], shape, sample: first, ok: false };

    // Without a native `order`, rebuild the CSV's own formula: epoch ms * 100 +
    // sequence within the millisecond, in the order the server returned them.
    // Stable as long as the server returns same-ms lines in a fixed order.
    shape.synthesized_order = !shape.order || typeof first[shape.order] === "string" && !/^\d+$/.test(first[shape.order]);
    const out = [];
    let lastMs = null, seq = 0;
    for (const item of list) {
      const entry = item[shape.entry];
      const at = toIso(item[shape.at]);
      if (typeof entry !== "string" || !at) continue;
      let order;
      if (!shape.synthesized_order) {
        order = Number(item[shape.order]);
      } else {
        const ms = Date.parse(at);
        seq = ms === lastMs ? seq + 1 : 0;
        lastMs = ms;
        order = ms * 100 + seq;
      }
      out.push({ entry, at, order });
    }
    out.sort((a, b) => a.order - b.order);
    return { entries: out, shape, sample: null, ok: true };
  }

  const api = { normalize, unwrap };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PNT = Object.assign(root.PNT || {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this);
