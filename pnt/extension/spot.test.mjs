// node --test pnt/extension/spot.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { handEnded, actingId, pickPlayer, nextSpot, isEcho, spotText, showText, pathText, mix, tagTitle, tagChips } = require("./spot.js");

// One decision's shown hands, in the server's five buckets.
const row = (hands, known, over = {}) =>
  ({ hands, known, strong: 0, medium: 0, weak: 0, draw: 0, air: 0, ...over });

test("the path line holds the earlier decisions; the spot line has the latest", () => {
  const path = ["unopened → call", "checked to (flop) → check", "checked to (turn)"];
  assert.deepEqual(pathText({ path, folded: false }),
    { text: "pre call · flop check", title: "unopened → call\nchecked to (flop) → check" });
  assert.equal(pathText({ path: ["unopened"], folded: false }).text, "");
  // A fold: the spot line has nothing to add, so the fold stays on this one.
  assert.equal(pathText({ path: ["unopened → call", "facing bet (turn) → fold"], folded: true }).text,
    "pre call · turn fold");
  assert.equal(pathText(null).text, "");
  assert.equal(pathText({ folded: false }).text, "");
});

test("a flop three or more saw leads the path line with its count", () => {
  const path = ["unopened → call", "checked to (flop) → check", "checked to (turn)"];
  const three = pathText({ path, folded: false, node: { street: "turn", players_flop: 3 } });
  assert.equal(three.text, "3-way · pre call · flop check");
  assert.match(three.title, /^3 players saw the flop\n/);
  assert.equal(pathText({ path, folded: false, node: { players_flop: 4 } }).text.split(" · ")[0], "4-way");
  // Heads-up, and preflop (no count yet), say nothing.
  assert.equal(pathText({ path, folded: false, node: { players_flop: 2 } }).text, "pre call · flop check");
  assert.equal(pathText({ path: ["unopened"], folded: false, node: { players_flop: null } }).text, "");
});

const live = (over = {}) => ({
  to_act: "gp",
  players: [
    { pn_id: "chris", alias: "Chris", folded: false,
      node: { label: "unopened", decision: "raise" },
      resolved: { filter: "position=BTN/SB,unopened", hands: 40, known: 9, exact: true,
                  relaxed: [], decisions: { fold: 10, call: 12, raise: 18 }, label: "unopened",
                  decision: "raise", arrived: false, street: "preflop", texture: [],
                  showings: { fold: row(10, 0), call: row(12, 3, { medium: 1, weak: 2 }),
                              raise: row(18, 6, { strong: 3, medium: 2, weak: 1 }) } } },
    { pn_id: "gp", alias: "genericpoker", folded: false,
      node: { label: "facing open", decision: null },
      resolved: { filter: "position=BB,faced_open", hands: 33, known: 7, exact: true,
                  relaxed: [], decisions: { fold: 20, call: 8, raise: 5 }, label: "facing open",
                  decision: null, arrived: false, street: "preflop", texture: [],
                  showings: { fold: row(20, 0), call: row(8, 4, { strong: 1, medium: 1, weak: 2 }),
                              raise: row(5, 3, { strong: 3 }) } } },
    { pn_id: "new", alias: null, folded: false, node: { label: "unopened", decision: null }, resolved: null },
  ],
  ...over,
});

test("the hand-end line is what triggers a rebuild", () => {
  assert.equal(handEnded([{ entry: '"A @ 1" folds' }, { entry: "-- ending hand #7 --" }]), true);
  assert.equal(handEnded([{ entry: "-- starting hand #8 (id: x) --" }]), false);
  assert.equal(handEnded([]), false);
});

test("the pinned player wins while seated, else the player to act", () => {
  assert.equal(pickPlayer(live(), "Chris"), "Chris");
  assert.equal(pickPlayer(live(), null), "genericpoker");
  assert.equal(pickPlayer(live(), "someone who left"), "genericpoker");
  assert.equal(pickPlayer(live({ to_act: null }), null), null);
  assert.equal(pickPlayer(live({ to_act: "new" }), null), null, "a player with no alias has no chart");
  assert.equal(pickPlayer(null, "Chris"), null);
});

test("a spot is sent once; a player with no data is still followed, on all their hands", () => {
  const first = nextSpot(live(), null, null);
  assert.deepEqual(first, { player: "genericpoker", filter: "position=BB,faced_open", exact: true, label: "facing open" });
  assert.equal(nextSpot(live(), null, { player: first.player, filter: first.filter }), null, "same spot again");
  assert.equal(nextSpot(live({ to_act: "new" }), null, null), null, "no identity yet: nothing to chart");
  const gone = live();
  gone.players[1].resolved = null;
  assert.deepEqual(nextSpot(gone, null, first), { player: "genericpoker", filter: "", exact: false, label: "all hands" },
    "no data: the chart still moves to them");
  assert.equal(nextSpot(gone, null, { player: "genericpoker", filter: "" }), null, "and is not sent twice");
  assert.equal(nextSpot(live(), "Chris", first).player, "Chris", "pinning changes who is sent");
});

// The same table, with the names the page shows on each seat.
const named = (over = {}) => {
  const l = live(over);
  l.players.forEach((p, i) => { p.name = ["chris", "gp", "newbie"][i]; });
  return l;
};

test("the table's player to act leads the log's", () => {
  // The log still has gp to act; the table has already moved on to Chris.
  assert.equal(pickPlayer(named(), null, "chris"), "Chris");
  assert.equal(actingId(named(), "chris"), "chris");
  assert.equal(pickPlayer(named({ to_act: null }), null, "chris"), "Chris", "the log thinks the street is closed");
  assert.equal(pickPlayer(named(), "genericpoker", "chris"), "genericpoker", "a pin still wins");
  assert.equal(pickPlayer(named(), null, "a stranger"), "genericpoker", "an unknown name falls back to the log");
  assert.equal(actingId(named(), "a stranger"), "gp");
  assert.equal(pickPlayer(named(), null, "newbie"), "genericpoker", "no alias: fall back to the log");
  const folded = named();
  folded.players[0].folded = true;
  assert.equal(pickPlayer(folded, null, "chris"), "genericpoker", "a folded seat is never to act");
  const twins = named();
  twins.players[1].name = "chris";
  assert.equal(pickPlayer(twins, null, "chris"), "genericpoker", "an ambiguous name is not trusted");
});

test("ahead of the log, the chart shows all their hands; the spot follows once /live agrees", () => {
  const ahead = nextSpot(named(), null, null, "chris");
  assert.deepEqual(ahead, { player: "Chris", filter: "", exact: false, label: "all hands" },
    "their resolved spot is from before the action came round");
  assert.equal(nextSpot(named(), null, ahead, "chris"), null, "and is not sent twice");
  const caught = nextSpot(named({ to_act: "chris" }), null, ahead, "chris");
  assert.deepEqual(caught, { player: "Chris", filter: "position=BTN/SB,unopened", exact: true, label: "unopened" });
  assert.deepEqual(nextSpot(named(), null, null, "gp"), nextSpot(named(), null, null),
    "when the table and the log agree, nothing changes");
  assert.equal(nextSpot(named(), "Chris", null, "gp").filter, "position=BTN/SB,unopened",
    "a pinned player keeps the spot /live gave them");
});

test("the chart's echo of a sent spot is recognised; an edit is not", () => {
  const sent = { player: "genericpoker", filter: "position=BB,faced_open" };
  const base = "http://127.0.0.1:52000/chart?theme=dark&player=genericpoker";
  assert.equal(isEcho(`${base}&filter=position%3DBB%2Cfaced_open`, sent), true);
  assert.equal(isEcho(`${base}&filter=3bet`, sent), false, "the user typed a spot");
  assert.equal(isEcho(`${base}`, sent), false, "the user cleared the spot");
  assert.equal(isEcho(`${base}`, { player: "genericpoker", filter: "" }), true);
  assert.equal(isEcho(`${base}&filter=3bet`, [sent, { player: "genericpoker", filter: "3bet" }]), true,
    "any recently sent spot counts, so two quick changes cannot look like an edit");
  assert.equal(isEcho(`${base}`, null), false);
});

test("a decision's shown hands read as value, marginal, draw and air", () => {
  assert.equal(showText(row(9, 4, { strong: 2, weak: 1, draw: 1 })), "value 50% · marginal 25% · draw 25% (4 of 9 shown)");
  assert.equal(showText(row(9, 3, { medium: 1, weak: 2 })), "marginal 100% (3 of 9 shown)", "any lesser pair is marginal");
  assert.equal(showText(row(9, 0)), "", "no known hands: no split");
  assert.equal(showText(null), "");
  assert.equal(mix({ fold: 20, call: 8, raise: 5 }), "fold 61% · call 24% · raise 15%");
});

test("the spot cell says where they are and what they have shown up with there", () => {
  // Still to act: one row per decision that has a shown hand behind it. The
  // fold row has none, so it is left out; the frequencies are in the hover.
  const gp = live().players[1];
  assert.deepEqual(spotText(gp), {
    text: "facing open · call: value 25% · marginal 75% (4 of 8 shown) · raise: value 100% (3 of 5 shown)",
    title: "facing open\nspot: position=BB,faced_open\n33 hands, 7 with cards known\n"
      + "facing open: fold 61% · call 24% · raise 15%\nhands read on the preflop",
  });
  const widened = { ...gp, resolved: { ...gp.resolved, exact: false, relaxed: ["any position"], filter: "faced_open" } };
  assert.match(spotText(widened).text, /^facing open · ≈ call: /);
  assert.match(spotText(widened).title, /closest spot: faced_open\nany position/);
  // Decided: the row for the decision they made.
  assert.equal(spotText(live().players[0]).text, "unopened → raise · value 50% · marginal 50% (6 of 18 shown)");
  assert.equal(spotText(live().players[2]).text, "unopened · no data");
  assert.equal(spotText({ folded: true }).text, "folded");
  assert.equal(spotText({ folded: false, node: null }).text, "");
});

test("a spot answered from earlier on the path reads as the range they arrived with", () => {
  // Facing a flop raise is not a tracked spot; the answer is their preflop call,
  // read on the flop board, and never the preflop fold/call/raise mix.
  const p = {
    folded: false,
    node: { label: "facing flop raise", decision: null },
    resolved: {
      filter: "faced_open,flop=ace_high", hands: 12, known: 5, exact: false,
      relaxed: ["facing flop raise is not a tracked spot; showing facing open"],
      decisions: { fold: 3, call: 8, raise: 1 }, label: "facing open",
      decision: "call", arrived: true, street: "flop", texture: ["flop=ace_high"],
      showings: { fold: row(3, 0), call: row(8, 4, { strong: 1, draw: 1, air: 2 }), raise: row(1, 1, { strong: 1 }) },
    },
  };
  const got = spotText(p);
  assert.equal(got.text, "facing flop raise · ≈ arrived with value 25% · draw 25% · air 50% (4 of 8 shown)");
  assert.match(got.title, /boards like this one: flop=ace_high/);
  assert.match(got.title, /hands read on the flop/);
  // Decided, but nothing was ever shown after that decision.
  const dark = { ...p, node: { label: "facing open", decision: "call" },
                 resolved: { ...p.resolved, arrived: false, showings: { call: row(8, 0) } } };
  assert.equal(spotText(dark).text, "facing open → call · ≈ no shown hands (12)");
});

const tags = [
  { id: "fish", label: "FISH", kind: "archetype", tip: "Loose and passive.", n: 4907, hits: 4907 },
  { id: "no_bluff", label: "NO BLUFF", kind: "exploit", tip: "Showed air 6.9% (shown).", n: 276, hits: 19 },
  { id: "folds_river", label: "FOLDS RIVER", kind: "exploit", tip: "Folded 51%.", n: 965, hits: 492 },
  { id: "limper", label: "LIMPER", kind: "exploit", tip: "Limped 46%.", n: 3051, hits: 1411 },
  { id: "fun_72", label: "7-2 RAISES ×2", kind: "fun", tip: "Raised 7-2 twice (shown).", n: 2, hits: 2 },
];

test("a tag chip's hover text is the evidence and the count behind it", () => {
  assert.equal(tagTitle(tags[1]), "Showed air 6.9% (shown).\n19 of 276");
  assert.equal(tagTitle(tags[0]), "Loose and passive.\n4907 hands", "a whole-sample tag says how many");
  assert.equal(tagTitle(null), "");
  const streaky = { ...tags[1], carried_by: { game_id: "g", hands: 74, sessions: 11 } };
  assert.equal(tagTitle(streaky),
    "Showed air 6.9% (shown).\n19 of 276\nOne session only: gone without a 74-hand game (11 sessions in all).");
});

test("the archetype always gets a chip; the rest are capped with the overflow named", () => {
  const { shown, hidden } = tagChips(tags, 3);
  assert.deepEqual(shown.map((t) => t.id), ["fish", "no_bluff", "folds_river"]);
  assert.deepEqual(hidden.map((t) => t.id), ["limper", "fun_72"]);
  assert.deepEqual(tagChips(tags.slice(1), 1).shown.map((t) => t.id), ["no_bluff"]);
  assert.deepEqual(tagChips([tags[0]], 3), { shown: [tags[0]], hidden: [] });
  assert.deepEqual(tagChips(null), { shown: [], hidden: [] });
});
