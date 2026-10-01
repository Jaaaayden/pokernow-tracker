"""The hand in progress, read from raw lines, and who is to act in it."""

from __future__ import annotations

import dataclasses

import pytest

from pnt.db.conn import connect
from pnt.ingest.csv_source import read_csv
from pnt.ingest.importer import ingest_entries, merge_players, rebuild_game
from pnt.logfmt.parser import parse
from pnt.stats import queries as q
from pnt.stats.live import current_street, hand_row, live_hand, snapshot, to_act
from pnt.stats.queries import load_hands
from tests.conftest import HU, HU_GAME, MULTIWAY_GAME, STRADDLE, STRADDLE_GAME

CHRIS = "5NARaPRkSp"
GP = "gpP9uUffpu"


def _hand_entries(path, game_id, hand_number):
    """The raw lines of one hand, oldest first, up to and including its end line."""
    entries = sorted(read_csv(path), key=lambda e: e.ord)
    start = next(i for i, e in enumerate(entries) if e.entry.startswith(f"-- starting hand #{hand_number} "))
    end = next(i for i, e in enumerate(entries) if e.entry == f"-- ending hand #{hand_number} --")
    return entries, start, end


def test_hand_row_matches_what_the_database_builds(db):
    """One adapter, checked field for field against the stored path."""
    from pnt.logfmt.hero import apply_hero_cards, infer_hero

    stored = {h.hand_number: h for h in load_hands(db, HU_GAME)}
    result = parse(read_csv(HU), HU_GAME)
    # The stored path also writes the hero's own cards in; do the same here.
    guess = infer_hero(result.hands)
    assert guess.confident
    apply_hero_cards(result.hands, guess.pn_id)
    for parsed in result.hands:
        row = hand_row(parsed, hand_id=stored[parsed.hand_number].hand_id)
        assert dataclasses.asdict(row) == dataclasses.asdict(stored[parsed.hand_number])


def test_to_act_follows_hand_18_line_by_line():
    """Replaying #18 one line at a time gives the seat the action is on, or None
    while a card is due. Chris is the button/small blind and acts first preflop;
    gp, the big blind, acts first postflop."""
    entries, start, end = _hand_entries(HU, HU_GAME, 18)
    seen = []
    for cut in range(start + 1, end + 1):
        hand = parse(entries[start:cut], HU_GAME).hands[-1]
        if hand.players:
            seen.append((entries[cut - 1].entry[:40], current_street(hand), to_act(hand)))
    got = [(s, w) for _, s, w in seen]
    # After the roster: Chris to act (no posts yet -- still the first seat).
    assert got[0] == ("preflop", CHRIS)
    # posts a small blind, posts a big blind: still Chris.
    assert got[1] == ("preflop", CHRIS) and got[2] == ("preflop", CHRIS)
    # Chris raises to 20 -> gp; gp raises to 60 -> Chris; Chris calls -> closed.
    assert got[4:7] == [("preflop", GP), ("preflop", CHRIS), ("preflop", None)]
    # Flop: gp first; gp bets -> Chris; Chris calls -> closed.
    assert got[7:10] == [("flop", GP), ("flop", CHRIS), ("flop", None)]
    # Turn: gp checks -> Chris; Chris bets -> gp; gp calls -> closed.
    assert got[10:14] == [("turn", GP), ("turn", CHRIS), ("turn", GP), ("turn", None)]
    # River: gp bets -> Chris; Chris calls -> closed; the show and the end line.
    assert got[14:17] == [("river", GP), ("river", CHRIS), ("river", None)]
    assert got[-1][1] is None


def test_to_act_is_none_when_a_live_player_has_no_position(parsed_all):
    """#26 of the multiway log has a dead button: no seat can be trusted."""
    hands = {h.hand_number: h for h in parsed_all[MULTIWAY_GAME].hands}
    h26 = hands[26]
    assert h26.dead_button
    cut = dataclasses.replace(h26, actions=h26.actions[:3], board_runs=[], complete=False)
    for p in cut.players.values():
        p.folded = False
    if any(p.seats_from_button is None for p in cut.players.values()):
        assert to_act(cut) is None


def test_to_act_after_a_straddle_starts_left_of_the_straddler():
    """The straddler acts last preflop, so the first decision is the next seat.

    The straddle log is heads-up with the button straddling, so the big blind
    opens the action -- and that is who the log shows acting first.
    """
    entries = sorted(read_csv(STRADDLE), key=lambda e: e.ord)
    result = parse(entries, STRADDLE_GAME)
    checked = 0
    for hand in result.hands:
        posts = [a for a in hand.actions if a.is_forced]
        if not any(a.post_kind == "straddle" for a in posts) or hand.blinds_irregular:
            continue
        voluntary = [a for a in hand.actions if not a.is_forced and a.street == "preflop"]
        if not voluntary:
            continue
        cut = dataclasses.replace(hand, actions=posts, board_runs=[], complete=False)
        for p in cut.players.values():
            p.folded = False
        straddler = next(a.pn_id for a in posts if a.post_kind == "straddle")
        first = to_act(cut)
        assert first == voluntary[0].pn_id, hand.hand_number
        assert first != straddler
        checked += 1
    assert checked > 10


def test_live_hand_is_none_between_hands(db):
    last = db.execute("SELECT MAX(hand_number) FROM hands WHERE game_id = ?", (HU_GAME,)).fetchone()[0]
    assert live_hand(db, HU_GAME) is None
    # Between hands there is still a hand to flag: the one that just ended.
    assert snapshot(db, HU_GAME) == {
        "game_id": HU_GAME, "hand": None, "previous": {"hand_number": last, "flagged": False},
    }
    assert snapshot(db, "no-such-game")["hand"] is None
    assert snapshot(db, "no-such-game")["previous"] is None


def test_the_previous_hand_can_be_flagged_before_it_is_rebuilt(tmp_path):
    """The HUD's flag button marks the hand that just ended, seconds before the
    rebuild derives it: the flag is checked against the raw lines and kept through it."""
    from pnt.stats import review as rv

    conn = connect(tmp_path / "live.sqlite")
    entries, start, _ = _hand_entries(HU, HU_GAME, 18)
    ingest_entries(conn, HU_GAME, entries[: start + 3], source="test")
    assert conn.execute("SELECT COUNT(*) FROM hands").fetchone()[0] == 0

    snap = snapshot(conn, HU_GAME)
    assert snap["hand_number"] == 18 and snap["previous"] == {"hand_number": 17, "flagged": False}
    assert rv.set_flag(conn, HU_GAME, 17)
    assert snapshot(conn, HU_GAME)["previous"] == {"hand_number": 17, "flagged": True}
    # Hand 18 has started but not ended: it can be flagged, and is not "previous".
    assert rv.set_flag(conn, HU_GAME, 18)
    with pytest.raises(ValueError, match="no hand #19"):
        rv.set_flag(conn, HU_GAME, 19)

    rebuild_game(conn, HU_GAME)
    assert rv.flagged_hands(conn, HU_GAME).keys() == {(HU_GAME, 17), (HU_GAME, 18)}
    conn.close()


def test_snapshot_of_a_hand_cut_before_the_3bet(tmp_path):
    """Ingest the heads-up log up to gp's 3-bet in #18: gp is to act facing the open."""
    conn = connect(tmp_path / "live.sqlite")
    entries, start, end = _hand_entries(HU, HU_GAME, 18)
    cut = next(i for i in range(start, end) if "raises to 60" in entries[i].entry)
    # Everything before this hand is complete history; the hand itself stops short.
    ingest_entries(conn, HU_GAME, entries[:cut], source="test")
    rebuild_game(conn, HU_GAME)

    snap = snapshot(conn, HU_GAME)
    assert snap["hand_number"] == 18 and snap["street"] == "preflop"
    assert snap["to_act"] == GP
    by = {p["pn_id"]: p for p in snap["players"]}
    assert by[GP]["position"] == "BB" and by[CHRIS]["position"] == "BTN/SB"
    assert by[GP]["node"]["filter"] == "position=BB,faced_open"
    assert by[GP]["node"]["decision"] is None
    assert by[GP]["path"] == ["facing open"]
    assert by[CHRIS]["path"] == ["unopened → raise"]
    assert by[CHRIS]["node"]["decision"] == "raise"
    assert by[GP]["resolved"]["hands"] > 0
    assert by[GP]["resolved"]["kind"] == "preflop"
    assert by[GP]["stack"] == 990 and by[CHRIS]["committed"] == 20
    assert snap["pot"] == 30
    # What they showed up with, read preflop: one row per decision, never a texture.
    r = by[GP]["resolved"]
    assert r["street"] == "preflop" and r["texture"] == [] and r["decision"] is None and not r["arrived"]
    assert set(r["showings"]) == set(r["decisions"])
    assert all(row["hands"] == r["decisions"][d] for d, row in r["showings"].items())
    assert by[CHRIS]["resolved"]["decision"] == "raise"

    assert all(p["resolved"] is None for p in snapshot(conn, HU_GAME, min_hands=10**6)["players"])

    # The rest of the log arrives: the hand ends and the view goes quiet.
    ingest_entries(conn, HU_GAME, entries[cut:], source="test")
    assert snapshot(conn, HU_GAME)["hand"] is None
    conn.close()


def test_snapshot_of_a_hand_cut_at_the_flop_cbet(tmp_path):
    """#188 up to gp's flop c-bet: Chris faces it, and his spot is read on this flop.

    #18 is the same shape but the first of its kind in the log, so Chris has no
    history there; by #188 he has faced twenty-two of them."""
    conn = connect(tmp_path / "live.sqlite")
    entries, start, end = _hand_entries(HU, HU_GAME, 188)
    flop = next(i for i in range(start, end) if entries[i].entry.startswith("Flop:"))
    cut = next(i for i in range(flop, end) if " bets " in entries[i].entry) + 1
    ingest_entries(conn, HU_GAME, entries[:cut], source="test")
    rebuild_game(conn, HU_GAME)

    snap = snapshot(conn, HU_GAME, min_known=1)
    assert snap["street"] == "flop" and snap["to_act"] == CHRIS and len(snap["board"]) == 3
    by = {p["pn_id"]: p for p in snap["players"]}
    assert by[CHRIS]["node"]["kind"] == "faced_cbet" and by[CHRIS]["node"]["decision"] is None
    r = by[CHRIS]["resolved"]
    assert r["street"] == "flop"
    assert all(t.startswith("flop=") for t in r["texture"])
    assert r["filter"].endswith(",".join(r["texture"])) if r["texture"] else "flop=" not in r["filter"]
    assert set(r["showings"]) == {"fold", "call", "raise"}
    # gp's c-bet is decided, so its own row is the one reported.
    assert by[GP]["resolved"]["decision"] == "bet" and by[GP]["resolved"]["street"] == "flop"
    # A gate no spot can meet: the plain rungs, over every board.
    strict = snapshot(conn, HU_GAME, min_known=10**6)
    assert all(p["resolved"]["texture"] == [] for p in strict["players"] if p["resolved"])
    conn.close()


def test_snapshot_needs_no_rebuild(tmp_path):
    """Lines in `raw_entries` are enough: the live view never waits for `hands`."""
    conn = connect(tmp_path / "live.sqlite")
    entries, _, end = _hand_entries(HU, HU_GAME, 18)
    ingest_entries(conn, HU_GAME, entries[: end - 2], source="test")
    snap = snapshot(conn, HU_GAME)
    assert snap["hand_number"] == 18 and snap["street"] == "river"
    # Nobody is known to the identity table yet, so nothing resolves -- but the
    # names from the roster still show.
    assert {p["name"] for p in snap["players"]} == {"Chris", "genericpoker"}
    assert all(p["alias"] is None and p["resolved"] is None for p in snap["players"])
    conn.close()


# --- the per-player facts cache -------------------------------------------


@pytest.fixture(autouse=True)
def _clean():
    q.clear_caches()
    yield
    q.clear_caches()


def test_facts_cached_agrees_with_facts_for_and_is_served_once(db, monkeypatch):
    first = q.facts_cached(db, "genericpoker")
    assert first == q.facts_for(db, "genericpoker")
    calls = []
    real = q.facts_for
    monkeypatch.setattr(q, "facts_for", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    assert q.facts_cached(db, "genericpoker") is first
    assert calls == []


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda db: rebuild_game(db, HU_GAME), id="rebuild"),
        pytest.param(lambda db: merge_players(db, "onlybluffs", "genericpoker"), id="merge"),
    ],
)
def test_facts_cache_is_invalidated_by_every_derivation_change(db, change):
    before = q.facts_cached(db, "genericpoker")
    change(db)
    after = q.facts_cached(db, "genericpoker")
    assert after is not before
    q.clear_caches()
    assert after == q.facts_cached(db, "genericpoker")


def test_facts_cache_is_capped(db):
    aliases = [r["player"] for r in q.report(db)]
    for alias in aliases:
        q.facts_cached(db, alias)
    assert len(q._FACTS_CACHE) <= q._FACTS_CACHE_MAX
