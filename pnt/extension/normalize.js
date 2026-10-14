/* Turn whatever `GET /games/{id}/log` returns into the entries `/ingest` takes.
 *
 * Confirmed against pokernow.com on 2026-09-11: the endpoint returns
 * `{logs: [{at, created_at, msg}], infos: {min, max}}`, newest first, 50 per page,
 * and `created_at` is a digit string equal to the CSV export's `order`. This file
 * is still the only place that knows the envelope, still accepts the other shapes
 * PokerNowGrabber-style tools use, and still reports what it saw so the popup can
 * say "recognized" or "unrecognized" instead of silently ingesting nothing.
 *
 * Output entries match the CSV export exactly: {entry, at, order}. That is the
 * whole point -- live capture and backfill feed one parser with identical input,
 * and the same line from either source lands on the same (game_id, order) row.
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
  const ORDER_KEYS = ["order", "ord", "created_at", "index", "seq", "id"];

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
    if (!shape.entry || !shape.at) return { entries: [], shape, sample: first, ok: false };

    let rows = [];
    for (const item of list) {
      const entry = item[shape.entry];
      const at = toIso(item[shape.at]);
      if (typeof entry === "string" && at) rows.push({ entry, at, item });
    }

    // Trust a native order only if it IS the CSV's formula, epoch ms * 100 +
    // sequence. Anything else -- a row id, an index -- would give a line a
    // different key from the same line in a CSV import, and every hand captured
    // live and later imported would be counted twice.
    const orderKey = pick(first, ORDER_KEYS)[0];
    const nativeOrder = (row) => {
      const v = row.item[orderKey];
      if (!/^\d+$/.test(String(v))) return null;
      const n = Number(v);
      return Math.floor(n / 100) === Date.parse(row.at) ? n : null;
    };

    if (orderKey && rows.every((r) => nativeOrder(r) !== null)) {
      shape.order = orderKey;
      rows = rows.map((r) => ({ entry: r.entry, at: r.at, order: nativeOrder(r) }));
    } else {
      // Rebuild the formula, numbering lines that share a millisecond in true log
      // order. A newest-first page lists those lines newest first, so it is walked
      // backwards; numbering in the order received would reverse them.
      shape.synthesized_order = true;
      if (rows.length > 1 && Date.parse(rows[0].at) > Date.parse(rows[rows.length - 1].at)) rows.reverse();
      rows.sort((a, b) => Date.parse(a.at) - Date.parse(b.at)); // stable: keeps same-ms order
      let lastMs = null;
      let seq = 0;
      rows = rows.map((r) => {
        const ms = Date.parse(r.at);
        seq = ms === lastMs ? seq + 1 : 0;
        lastMs = ms;
        return { entry: r.entry, at: r.at, order: ms * 100 + seq };
      });
    }

    rows.sort((a, b) => a.order - b.order);
    return { entries: rows, shape, sample: null, ok: true };
  }

  const api = { normalize, unwrap };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PNT = Object.assign(root.PNT || {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this);
