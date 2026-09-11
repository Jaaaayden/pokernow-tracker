// node --test extension/
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const { normalize } = createRequire(import.meta.url)("./normalize.js");

const csvLike = [
  { entry: '-- ending hand #3 --', at: "2026-08-10T08:29:52.078Z", order: 178635059207801 },
  { entry: '"Chris @ 5NARaPRkSp" calls 30', at: "2026-08-10T08:29:52.078Z", order: 178635059207800 },
];

test("bare array with the CSV's own fields passes through, sorted by order", () => {
  const r = normalize(csvLike);
  assert.equal(r.ok, true);
  assert.deepEqual(r.shape, { list: "array", entry: "entry", at: "at", order: "order", synthesized_order: false });
  assert.deepEqual(r.entries.map((e) => e.order), [178635059207800, 178635059207801]);
  assert.equal(r.entries[0].entry, '"Chris @ 5NARaPRkSp" calls 30');
});

test("{logs: [...]} envelope with msg/createdAt and no order synthesizes the CSV formula", () => {
  const ms = Date.parse("2026-08-10T08:29:52.078Z");
  const r = normalize({ logs: [
    { msg: "a", createdAt: ms },
    { msg: "b", createdAt: ms },       // same millisecond -> seq 1
    { msg: "c", createdAt: ms + 1 },
  ] });
  assert.equal(r.ok, true);
  assert.equal(r.shape.list, "logs");
  assert.equal(r.shape.synthesized_order, true);
  assert.deepEqual(r.entries.map((e) => [e.entry, e.order]), [["a", ms * 100], ["b", ms * 100 + 1], ["c", (ms + 1) * 100]]);
  assert.equal(r.entries[0].at, "2026-08-10T08:29:52.078Z");
});

test("numeric-string timestamps and ISO strings both become ISO `at`", () => {
  const r = normalize([{ entry: "x", at: "1786350592078", order: 1 }, { entry: "y", at: "2026-08-10T08:29:52.078Z", order: 2 }]);
  assert.equal(r.entries[0].at, r.entries[1].at);
});

test("an unrecognized shape is reported, never silently empty", () => {
  const r = normalize({ game: "x", players: [] });
  assert.equal(r.ok, false);
  assert.equal(r.entries.length, 0);
  assert.deepEqual(r.sample, { game: "x", players: [] });
  const r2 = normalize([{ foo: "bar" }]);
  assert.equal(r2.ok, false);
  assert.deepEqual(r2.sample, { foo: "bar" });
});

test("an empty page is ok and empty", () => {
  const r = normalize({ logs: [] });
  assert.equal(r.ok, true);
  assert.equal(r.entries.length, 0);
});

test("rows missing text or time are skipped, not fabricated", () => {
  const r = normalize([{ entry: "ok", at: "2026-08-10T08:29:52.078Z", order: 5 }, { entry: 42, at: "2026-08-10T08:29:52.078Z", order: 6 }, { entry: "no time", order: 7 }]);
  assert.equal(r.entries.length, 1);
});
