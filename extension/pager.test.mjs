// node --test extension/normalize.test.mjs extension/pager.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { sync, RateLimited, PAGE_SIZE } = require("./pager.js");
const { normalize } = require("./normalize.js");

const T0 = Date.parse("2026-09-11T09:00:00.000Z");

/** A fake /log with exactly the behaviour probed on pokernow.com. */
function fakePokerNow(n, { ignoreBefore = false } = {}) {
  const lines = [];
  const server = {
    requests: [],
    add(k) {
      for (let i = 0; i < k; i++) {
        // Pairs of lines share a millisecond, as blinds and hand starts do.
        const ms = T0 + Math.floor(lines.length / 2) * 700;
        const created = ms * 100 + (lines.length % 2);
        lines.push({ at: new Date(ms).toISOString(), created_at: String(created), msg: `line ${lines.length}` });
      }
    },
    async fetchPage({ after, before }) {
      server.requests.push({ after, before });
      const c = (l) => Number(l.created_at);
      const logs = lines
        .filter((l) => (after == null || c(l) > after) && (ignoreBefore || before == null || c(l) < before))
        .slice(-PAGE_SIZE)
        .reverse(); // newest first
      const { entries } = normalize({ logs, infos: { min: null, max: null } });
      return { entries, size: logs.length };
    },
  };
  server.add(n);
  return { server, lines };
}

function store(preloaded = []) {
  const orders = new Set(preloaded);
  return {
    orders,
    async ingest(entries) {
      let added = 0;
      for (const e of entries) if (!orders.has(e.order)) { orders.add(e.order); added++; }
      return { new: added, offered: entries.length };
    },
  };
}

const ioFor = (server, db) => ({ fetchPage: server.fetchPage, ingest: db.ingest, pause: async () => {} });
const ordersOf = (lines) => lines.map((l) => Number(l.created_at));
const sorted = (set) => [...set].sort((a, b) => a - b);

test("a fresh pass stores a whole game, walking back page by page", async () => {
  const { server, lines } = fakePokerNow(180);
  const db = store();
  const state = { cursor: 0, walk: null };
  const r = await sync(state, ioFor(server, db));
  assert.deepEqual(sorted(db.orders), ordersOf(lines));
  assert.equal(r.pages, 4, "50 + 50 + 50 + 30");
  assert.equal(state.cursor, Math.max(...ordersOf(lines)));
  assert.equal(state.walk, null);
});

test("cursors are created_at values, never plain milliseconds", async () => {
  const { server } = fakePokerNow(180);
  const state = { cursor: 0, walk: null };
  const db = store();
  await sync(state, ioFor(server, db));
  server.add(120);
  await sync(state, ioFor(server, db));
  for (const { after, before } of server.requests) {
    for (const v of [after, before]) assert.ok(v == null || v > 1e14, `cursor ${v} looks like milliseconds`);
  }
});

test("a quiet table costs one request per poll", async () => {
  const { server, lines } = fakePokerNow(30);
  const db = store();
  const state = { cursor: 0, walk: null };
  await sync(state, ioFor(server, db));
  server.requests.length = 0;
  server.add(3);
  await sync(state, ioFor(server, db));
  assert.equal(server.requests.length, 1);
  assert.deepEqual(sorted(db.orders), ordersOf(lines));
});

test("more than a page of new lines between polls is filled without a gap", async () => {
  const { server, lines } = fakePokerNow(60);
  const db = store();
  const state = { cursor: 0, walk: null };
  await sync(state, ioFor(server, db));
  server.add(130); // after_at alone would return only the newest 50 of these
  await sync(state, ioFor(server, db));
  assert.deepEqual(sorted(db.orders), ordersOf(lines));
});

test("an interrupted walk resumes where it stopped instead of declaring history complete", async () => {
  const { server, lines } = fakePokerNow(260);
  const db = store();
  const state = { cursor: 0, walk: null };
  let calls = 0;
  const flaky = {
    ...ioFor(server, db),
    fetchPage: async (args) => {
      if (++calls === 3) throw new RateLimited(5000);
      return server.fetchPage(args);
    },
  };
  await assert.rejects(sync(state, flaky), RateLimited);
  assert.ok(state.walk, "the walk keeps its place");
  assert.equal(db.orders.size, 100);
  await sync(state, flaky);
  assert.deepEqual(sorted(db.orders), ordersOf(lines));
});

test("a failure after a page was stored does not end the retried walk early", async () => {
  const { server, lines } = fakePokerNow(200);
  const db = store();
  const state = { cursor: 0, walk: null };
  let n = 0;
  const lossy = {
    ...ioFor(server, db),
    ingest: async (entries) => {
      const r = await db.ingest(entries);
      if (++n === 2) throw new Error("response lost after the rows were written");
      return r;
    },
  };
  await assert.rejects(sync(state, lossy));
  await sync(state, lossy);
  assert.deepEqual(sorted(db.orders), ordersOf(lines));
});

test("the walk stops at history already stored, such as a CSV import", async () => {
  const { server, lines } = fakePokerNow(400);
  const db = store(ordersOf(lines).slice(0, 330));
  const r = await sync({ cursor: 0, walk: null }, ioFor(server, db));
  assert.equal(db.orders.size, 400);
  assert.equal(r.pages, 3, "50 new, then 20 new in a full page, then a page already stored");
});

test("a server that ignores before_at is an error, not a quiet stop", async () => {
  const { server } = fakePokerNow(120, { ignoreBefore: true });
  await assert.rejects(sync({ cursor: 0, walk: null }, ioFor(server, store())), /ignored before_at/);
});

test("it pauses between requests, never before the first", async () => {
  const { server } = fakePokerNow(180);
  let pauses = 0;
  const r = await sync({ cursor: 0, walk: null }, { ...ioFor(server, store()), pause: async () => { pauses++; } });
  assert.equal(pauses, r.pages - 1);
});
