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

function boot({ acting = null, fail = {} } = {}) {
  const sent = [];       // message types to the worker, in order (status reports left out)
  const reports = [];    // every status report, with the payloads it carried as `data`
  const timers = [];
  const storage = [];
  const lines = [];
  let created = 17600000000000000; // created_at units
  const add = (msg) => {
    created += 100000;
    lines.push({ at: new Date(Math.floor(created / 100)).toISOString(), created_at: String(created), msg });
  };
  const stored = new Set();
  // The page's clock, moved on by `tick`; it stands still otherwise.
  let now = Date.now();
  class Clock extends Date {
    constructor(...a) { super(...(a.length ? a : [now])); }
    static now() { return now; }
  }
  const tick = (ms) => { now += ms; };
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
        if (msg.type === "status") reports.push({ ...msg.status, data: msg.data });
        else sent.push(msg.type);
        // `fail[type]`: that request fails with this error, while it is set.
        if (fail[msg.type]) { queueMicrotask(() => cb({ ok: false, error: fail[msg.type] })); return; }
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
    chrome, console: { info() {}, warn() {}, log() {}, error() {} }, URL, URLSearchParams, Date: Clock, Promise, JSON, Math, Number, String, Error, Array, Object, Set, Map,
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
  // content.js again in the same page, as the worker's injection after an update does.
  const again = async () => { vm.runInContext(SCRIPTS.at(-1)[1], ctx, { filename: "content.js" }); await flush(); };
  return { add, start, poll, again, tick, sent, reports, storage, answer, chrome, ctx };
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

test("a second copy in the same page stops at once, unless the first was orphaned", async () => {
  const t = boot();
  HAND.forEach(t.add);
  await t.start();
  t.sent.length = 0;
  await t.again();
  assert.deepEqual(t.sent, [], "the live copy goes on alone: no second settings, health or poll");
  // The extension reloaded: the first copy's context is gone, the new one's is not.
  const fresh = { ...t.chrome, runtime: { ...t.chrome.runtime, id: "ext-new" } };
  t.chrome.runtime.id = undefined;
  t.ctx.chrome = fresh;
  await t.again();
  assert.ok(t.sent.includes("settings"), "the fresh copy takes over");
});

test("the /hud and /live payloads go out only when they changed", async () => {
  const t = boot();
  HAND.forEach(t.add);
  await t.start();
  const withData = t.reports.filter((r) => r.data);
  assert.ok(withData.length >= 1);
  assert.ok(t.reports.every((r) => !("hudData" in r) && !("liveData" in r)), "never in the status itself");
  const revs = withData.map((r) => r.data.rev);
  assert.deepEqual(revs, [...new Set(revs)], "each rev once");
  // Nothing new on the table: the reports carry no payload.
  t.reports.length = 0;
  await t.poll();
  assert.ok(t.reports.length > 0 && t.reports.every((r) => !r.data));
});

test("the same /hud again is no news: no new rev for it", async () => {
  const t = boot();
  HAND.forEach(t.add);
  await t.start();
  const first = t.reports.filter((r) => r.data?.hudData).length;
  t.add("-- ending hand #1 --");
  t.reports.length = 0;
  // ingest, live, rebuild, hud, live: the same /hud makes no rev of its own.
  assert.deepEqual(await t.poll(), ["ingest", "live", "rebuild", "hud", "live"]);
  assert.equal(new Set(t.reports.filter((r) => r.data).map((r) => r.data.rev)).size, 2, "one per /live");
  t.answer.hud = { seats: [{ pn_id: "A1", alias: "ann" }] };
  t.add('-- starting hand #2 (id: y)  No Limit Texas Hold\'em (dealer: "Ann @ A1") --');
  t.add("-- ending hand #2 --");
  t.reports.length = 0;
  await t.poll();
  assert.ok(first >= 1);
  assert.ok(t.reports.some((r) => r.data?.hudData?.seats?.length === 1), "a changed /hud goes out");
});

const hudCalls = (sent) => sent.filter((m) => m === "hud").length;

test("a /hud the worker gave up waiting on is still loading, not a failure", async () => {
  const fail = { hud: "tracker busy: no answer within 25 s" };
  const t = boot({ fail });
  HAND.forEach(t.add);
  await t.start();
  assert.equal(hudCalls(t.sent), 1, "one /hud, not a second in the same poll");
  assert.ok(t.reports.some((r) => r.text === "stats still loading"));
  assert.equal(t.reports.at(-1).hudError, null, "no error line in the panel");
  fail.hud = "The message port closed before a response was received.";
  t.tick(31_000);
  assert.equal(hudCalls(await t.poll()), 1);
  assert.equal(t.reports.at(-1).hudError, null, "Chrome stopping the worker mid-call reads the same");
  delete fail.hud;
  t.tick(31_000);
  assert.equal(hudCalls(await t.poll()), 1, "asked again once the back-off is over");
  assert.ok(t.reports.some((r) => r.data?.hudData), "and the stats go out");
});

test("a slow /hud is not asked again for 30 s, a hand end's rebuild included", async () => {
  const fail = { hud: "tracker busy: no answer within 25 s" };
  const t = boot({ fail });
  HAND.forEach(t.add);
  await t.start();
  t.tick(5_000);
  assert.equal(hudCalls(await t.poll()), 0, "the next poll leaves the server to finish");
  t.add("-- ending hand #1 --");
  t.tick(5_000);
  const sent = await t.poll();
  assert.ok(sent.includes("rebuild"));
  assert.equal(hudCalls(sent), 0, "nor does the rebuild ask while it is still working");
  t.tick(21_000);
  assert.equal(hudCalls(await t.poll()), 1, "30 s on, it is asked once more");
});

test("going slow clears a failed /hud's box", async () => {
  const fail = { hud: "500 Internal Server Error" };
  const t = boot({ fail });
  HAND.forEach(t.add);
  await t.start();
  assert.equal(t.reports.at(-1).hudError, "500 Internal Server Error");
  const rev = Math.max(...t.reports.filter((r) => r.data).map((r) => r.data.rev));
  fail.hud = "tracker busy: no answer within 25 s";
  t.tick(31_000);
  await t.poll();
  assert.equal(t.reports.at(-1).hudError, null, "no 'Stats failed' beside 'still loading'");
  assert.ok(t.reports.some((r) => r.data && r.data.rev > rev), "a new rev, so the panel shimmers again");
});

test("a port closed by an extension reload is not read as slow", async () => {
  const fail = { hud: "500 Internal Server Error" };
  const t = boot({ fail });
  HAND.forEach(t.add);
  await t.start();
  fail.hud = "The message port closed before a response was received.";
  t.tick(31_000);
  // The reload takes the context away while the /hud is out.
  const orig = t.chrome.runtime.sendMessage;
  t.chrome.runtime.sendMessage = (msg, cb) => {
    if (msg.type === "hud") t.chrome.runtime.id = undefined;
    return orig(msg, cb);
  };
  t.reports.length = 0;
  await t.poll();
  assert.ok(t.reports.every((r) => r.text !== "stats still loading"));
});

test("payloads the worker dropped are sent again", async () => {
  const t = boot();
  HAND.forEach(t.add);
  await t.start();
  t.reports.length = 0;
  await t.poll();
  assert.ok(t.reports.every((r) => !r.data), "nothing new, nothing sent");
  // The worker forgot the tab's payloads (a "loading" the page lived through).
  t.answer.status = { held: false };
  t.reports.length = 0;
  await t.poll();
  t.answer.status = { held: true };
  const resent = t.reports.filter((r) => r.data);
  assert.ok(resent.length >= 1, "sent again at once");
  assert.ok(resent[0].data.liveData, "with what the panel draws");
  t.reports.length = 0;
  await t.poll();
  assert.ok(t.reports.every((r) => !r.data), "and only until the worker holds them");
});
