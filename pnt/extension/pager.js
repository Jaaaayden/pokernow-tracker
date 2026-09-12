/* Walk PokerNow's /log endpoint so that no line is ever skipped.
 *
 * How the endpoint behaves (probed on pokernow.com, 2026-09-11; docs/findings.md §8):
 *
 *   - Lines come newest first, at most PAGE_SIZE per request.
 *   - `before_at` and `after_at` are created_at values -- epoch ms * 100 + seq, the
 *     CSV `order` -- and both are exclusive. Plain milliseconds sit below every
 *     line, so `before_at=<ms>` returns nothing: the first version's bug.
 *   - `before_at=X` returns the newest lines older than X, so it pages backwards.
 *   - `after_at=X` returns the NEWEST lines above X. It filters; it never pages
 *     forwards. A full page says nothing about what lies between X and that page,
 *     and the only way to reach those lines is to walk back from the page.
 *   - Requests in quick succession get HTTP 429.
 *
 * So one sync pass fetches the newest page above the cursor and, while pages come
 * back full, asks for `before_at=<oldest line so far>`. It stops at a short page
 * (nothing older is left above the cursor) or at a page that adds nothing
 * (everything older is already stored -- by an earlier pass, or a CSV import).
 *
 * Plain script, like normalize.js: loaded on the page, and required by the node test.
 */
(function (root) {
  "use strict";

  const PAGE_SIZE = 50;

  // Re-fetch this much below the cursor on every pass: 2 s in created_at units.
  // /ingest dedupes, so it costs a few repeated lines and guards the cursor's edge.
  const OVERLAP = 2000 * 100;

  class RateLimited extends Error {
    constructor(waitMs) {
      super("rate limited by PokerNow");
      this.name = "RateLimited";
      this.waitMs = waitMs; // from Retry-After, or null
    }
  }

  /**
   * One sync pass. Mutates `state` = {cursor, walk}:
   *   cursor  newest created_at at or below which everything is stored (0 = nothing yet)
   *   walk    the unfinished pass, or null
   *
   * Resumable: when a request throws (a 429, a network error), `state.walk` keeps its
   * place and the next call continues from it. Restarting at the newest page would
   * meet lines already stored, conclude that history was complete, and leave the
   * gap below them empty for good.
   *
   * io.fetchPage({after, before}) -> {entries: ascending by order, size: raw lines}
   * io.ingest(entries)           -> {new}
   * io.pause()                   -> resolves when the next request may go out
   */
  async function sync(state, io, { onPage } = {}) {
    if (!state.walk) {
      state.walk = {
        floor: state.cursor ? state.cursor - OVERLAP : null,
        before: null,
        top: state.cursor || 0,
      };
    }
    const walk = state.walk;
    let pages = 0;
    let fresh = 0;
    for (;;) {
      if (pages) await io.pause();
      const { entries, size } = await io.fetchPage({ after: walk.floor, before: walk.before });
      pages += 1;
      if (!entries.length) break;
      if (walk.before != null && entries[entries.length - 1].order >= walk.before) {
        // Stopping here would look exactly like "history complete". Refuse instead.
        throw new Error("/log ignored before_at; not walking further");
      }

      const { new: added } = await io.ingest(entries);
      fresh += added;
      walk.top = Math.max(walk.top, entries[entries.length - 1].order);
      walk.before = entries[0].order;
      if (onPage) await onPage({ pages, fresh, added });

      if (size < PAGE_SIZE) break;
      // A page of lines already stored means everything older is stored too. Not on
      // the first page of a call, though: after a failure that struck once a page
      // was stored, the retry fetches that same page again and it adds nothing.
      if (added === 0 && pages > 1) break;
    }
    state.cursor = Math.max(state.cursor || 0, walk.top);
    state.walk = null;
    return { pages, fresh };
  }

  const api = { sync, RateLimited, PAGE_SIZE, OVERLAP };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PNT = Object.assign(root.PNT || {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this);
