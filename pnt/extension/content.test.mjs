// node --test pnt/extension/content.test.mjs
// content.js run in a vm with the real normalize/pager/spot/watch scripts, a fake
// PokerNow /log, and chrome.* answering from a table of responses.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const EXT = new URL(".", import.meta.url);
const SCRIPTS = ["normalize.js", "pager.js", "spot.js", "watch.js", "content.js"]
  .map((f) => [f, fs.readFileSync(new URL(f, EXT), "utf8")]);

const realTimeout = globalThis.setTimeout;
const flush = () => new Promise((r) => realTimeout(r, 20));

function table({ acting = null } = {}) {
  // Just the selectors watch.js reads: one seat to act, named `acting`.
  const seat = acting && {
    className: "table-player decision-current",
    querySelector: (sel) => (sel === ".table-player-name" ? { textContent: acting } : null),
  };
  return {
    body: {},
    documentElement: { appendChild() {} },
    querySelector: () => null,
    querySelectorAll: (sel) => (sel === ".seats .table-player" && seat ? [seat] : []),
  };
}

function boot({ acting = null } = {}) {
  const sent = [];       // message types to the worker, in order (status reports left out)
  const reports = [];    // every status report
  const timers = [];
  const storage = [];
  const lines = [];
  let created = 17600000000000000; // created_at units
  const add = (msg) => {
    created += 100000;
    lines.push({ at: new Date(Math.floor(created / 100)).toISOString(), created_at: String(created), msg });
  };
  const stored = new Set();
  const answer = {
    settings: { server: "http://127.0.0.1:52000", pollSeconds: 5, liveMin: 1, liveKnown: 5, hudMode: "panel" },
    health: { hands: 1 }, rebuild: {}, hud: { seats: [] }, tab: { id: 1 }, status: {},
    live: { hand_number: 1, street: "preflop", players: [] },
  };
  const chrome = {
    runtime: {
      id: "ext", lastError: null, getURL: (s) => `chrome-extension://ext/${s}`,
      onMessage: { addListener() {} },
      sendMessage(msg, cb) {
        if (msg.type === "status") reports.push(msg.status);
        else sent.push(msg.type);
        let data = answer[msg.type];
        if (msg.type === "ingest") {
          let n = 0;
          for (const e of msg.entries) if (!stored.has(e.order)) { stored.add(e.order); n++; }
          data = { offered: msg.entries.length, new: n, oldest: Math.min(...stored) };
        }
        queueMicrotask(() => cb({ ok: true, data }));
      },
    },
    storage: { onChanged: { addListener: (f) => storage.push(f) } },
  };
  const ctx = {
    chrome, console: { info() {}, warn() {}, log() {}, error() {} }, URL, URLSearchParams, Date, Promise, JSON, Math, Number, String, Error, Array, Object, Set, Map,
    location: { pathname: "/games/abc123", origin: "https://www.pokernow.com" },
    document: table({ acting }),
    MutationObserver: class { observe() {} disconnect() {} },
    ResizeObserver: class { observe() {} disconnect() {} },
    addEventListener() {}, localStorage: { getItem: () => null, setItem() {} },
    innerWidth: 1000, innerHeight: 800,
    setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout() {},
    queueMicrotask,
    fetch: async (url) => {
      const q = new URL(url).searchParams;
      const after = Number(q.get("after_at") || 0), before = q.get("before_at") ? Number(q.get("before_at")) : Infinity;
      const page = lines.filter((l) => Number(l.created_at) > after && Number(l.created_at) < before).reverse().slice(0, 50);
      return { status: 200, ok: true, headers: { get: () => null }, json: async () => ({ logs: page }) };
    },
  };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  const start = async () => {
    for (const [f, src] of SCRIPTS) vm.runInContext(src, ctx, { filename: f });
    await flush();
  };
  // Run the scheduled poll and report what it asked the worker for.
  const poll = async () => {
    sent.length = 0;
    const loop = timers.find((f) => f.name === "loop");
    timers.length = 0;
    loop();
    await flush();
    return [...sent];
  };
  return { add, start, poll, sent, reports, storage };
}

const HAND = [
  '-- starting hand #1 (id: x)  No Limit Texas Hold\'em (dealer: "Ann @ A1") --',
  'Player stacks: #1 "Ann @ A1" (1000) | #3 "Bo \"the\" Kid @ B2" (1000) | #4 "C @ x @ C3" (1000)',
  '"Ann @ A1" posts a small blind of 1',
  '"Bo \"the\" Kid @ B2" posts a big blind of 2',
];

test("a mid-hand line no longer forces a rebuild; the first poll and a hand end do", async () => {
  const t = boot();
  HAND.forEach(t.add);
  await t.start();
  assert.deepEqual(t.sent.filter((m) => m === "rebuild" || m === "live"), ["rebuild", "live"],
    "the first poll rebuilds at once, so the table's earlier hands show");
  t.add('"C @ x @ C3" calls 2');
  assert.deepEqual(await t.poll(), ["ingest", "live"], "a call mid-hand: the live hand only");
  t.add('"Ann @ A1" folds');
  assert.deepEqual(await t.poll(), ["ingest", "live"]);
  assert.deepEqual(await t.poll(), ["ingest"], "nothing new: nothing else");
  t.add('"Bo \"the\" Kid @ B2" collected 4 from pot');
  t.add("-- ending hand #1 --");
  assert.deepEqual(await t.poll(), ["ingest", "live", "rebuild", "hud", "live"], "the hand ended: rebuild");
});

test("the table's player to act goes out by ID too, read from the stacks line", async () => {
  const t = boot({ acting: "C @ x" });
  HAND.forEach(t.add);
  await t.start();
  const last = t.reports.at(-1);
  assert.equal(last.tableActing, "C @ x");
  assert.equal(last.tableActingPn, "C3", "a name with ' @ ' in it still finds its ID");
  const q = boot({ acting: "Zed" });
  HAND.forEach(q.add);
  await q.start();
  assert.equal(q.reports.at(-1).tableActingPn, null, "a name the stacks line does not hold: no ID");
});

test("a removed server setting falls back to the default rather than throwing", async () => {
  const t = boot();
  HAND.forEach(t.add);
  await t.start();
  assert.equal(t.storage.length, 1);
  assert.doesNotThrow(() => t.storage[0]({ server: { oldValue: "http://127.0.0.1:52001" } }, "sync"));
});
