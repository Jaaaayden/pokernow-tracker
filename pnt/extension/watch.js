/* Table watch: poll the log the moment the table changes, not on the next tick.
 *
 * The log stays the only source of hands -- nothing here reads a card or a bet
 * into the tracker. The table on screen is only a doorbell: PokerNow redraws it
 * as soon as anyone acts, seconds before the timer would next poll /log.
 *
 * What counts as a change is a signature of the parts of the table an action
 * moves (checked against a live table, 2026-09-14):
 *
 *   .table-pot-size                  the pot, with the running total beside it: every bet, call, raise
 *   .table-cards                     the board: every street
 *   .dealer-button-ctn               its dealer-position-N class: every new hand
 *   .seats .table-player             per seat, its classes -- `decision-current` moves to the
 *                                    player to act, which is how a fold or a check shows --
 *     .table-player-bet-value        and the chips in front of them,
 *     .table-player-stack            and behind them.
 *
 * Left out on purpose: `.time-to-talk .normal-time`, the shot clock, rewrites
 * its inline width many times a second. The observer in content.js only listens
 * for class and text changes, and the signature does not read it either.
 *
 * The table also says who is to act, before the log does: `tableActing` reads the
 * name on the `decision-current` seat, which the side panel follows at once and
 * then checks against /live once the log has caught up. Still only a doorbell --
 * the name moves the chart, never a line into the tracker.
 */
(function (root) {
  "use strict";

  function tableSignature(doc) {
    const text = (sel, scope = doc) => (scope.querySelector(sel)?.textContent || "").trim();
    const board = doc.querySelector(".table-cards");
    const seats = [...doc.querySelectorAll(".seats .table-player")].map((p) =>
      [p.className, text(".table-player-bet-value", p), text(".table-player-stack", p)].join("|"));
    return [
      text(".table-pot-size"),
      board ? `${board.childElementCount}:${board.textContent.trim()}` : "",
      doc.querySelector(".dealer-button-ctn")?.className || "",
      ...seats,
    ].join("\n");
  }

  /** The name on the seat the action is on, as the table shows it; null when no
   * seat, or more than one, has `decision-current`. */
  function tableActing(doc) {
    const seats = [...doc.querySelectorAll(".seats .table-player")]
      .filter((p) => ` ${p.className} `.includes(" decision-current "));
    if (seats.length !== 1) return null;
    return (seats[0].querySelector(".table-player-name")?.textContent || "").trim() || null;
  }

  /** How long a poll the table asked for must wait so that polls start at least
   * `floorMs` apart. PokerNow answers a burst of /log requests with HTTP 429. */
  function pokeDelay(now, lastPollAt, floorMs = 1000) {
    return Math.max(0, (lastPollAt || 0) + floorMs - now);
  }

  const api = { tableSignature, tableActing, pokeDelay };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.PNT = Object.assign(root.PNT || {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this);
