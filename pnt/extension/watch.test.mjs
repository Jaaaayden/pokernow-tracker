// node --test pnt/extension/watch.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { tableSignature, pokeDelay } = require("./watch.js");

// Just enough of a DOM for querySelector(All) over the selectors watch.js reads.
const node = (className, text = "", kids = {}) => ({
  className, textContent: text, childElementCount: Object.keys(kids).length,
  querySelector: (sel) => kids[sel] ?? null,
});
function table({ pot = "0 total 30", board = [], dealer = 3, seats = [], clock = "81%" } = {}) {
  const players = seats.map((s, i) => node(`table-player table-player-${i + 1} ${s.cls || ""}`, "", {
    ".table-player-bet-value": node("table-player-bet-value", s.bet ?? ""),
    ".table-player-stack": node("table-player-stack", s.stack ?? "1000"),
  }));
  const kids = {
    ".table-pot-size": node("table-pot-size", pot),
    ".table-cards": node("table-cards run-1", board.join(""), Object.fromEntries(board.map((c) => [c, c]))),
    ".dealer-button-ctn": node(`dealer-button-ctn dealer-position-${dealer}`),
    ".time-to-talk .normal-time": node("normal-time", clock),
  };
  return { querySelector: (sel) => kids[sel] ?? null, querySelectorAll: (sel) => (sel === ".seats .table-player" ? players : []) };
}

const seats = [{ cls: "you-player" }, { cls: "decision-current" }, {}];

test("the same table gives the same signature, whatever the shot clock does", () => {
  assert.equal(tableSignature(table({ seats })), tableSignature(table({ seats, clock: "12%" })));
});

test("a fold or a check shows as the action moving to the next seat", () => {
  const after = [{ cls: "you-player" }, {}, { cls: "decision-current" }];
  assert.notEqual(tableSignature(table({ seats })), tableSignature(table({ seats: after })));
});

test("a bet shows in the chips in front of the player and in the pot", () => {
  const bet = [{ cls: "you-player" }, { bet: "20", stack: "980" }, { cls: "decision-current" }];
  assert.notEqual(tableSignature(table({ seats })), tableSignature(table({ seats: bet, pot: "0 total 50" })));
});

test("a new street and a new hand each change it", () => {
  const base = tableSignature(table({ seats }));
  assert.notEqual(base, tableSignature(table({ seats, board: ["Ks", "3d", "7d"] })));
  assert.notEqual(base, tableSignature(table({ seats, dealer: 4 })));
});

test("a page with no table yet still signs", () => {
  const empty = { querySelector: () => null, querySelectorAll: () => [] };
  assert.equal(typeof tableSignature(empty), "string");
});

test("table-triggered polls start at least a second apart", () => {
  assert.equal(pokeDelay(10_000, 0), 0);
  assert.equal(pokeDelay(10_000, 9_700), 700);
  assert.equal(pokeDelay(10_000, 8_000), 0);
  assert.equal(pokeDelay(10_000, 9_700, 500), 200);
});
