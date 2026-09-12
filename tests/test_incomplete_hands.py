"""A log that stops mid-hand records only half that hand's chips.

The parser has always known -- `ParsedHand.complete` -- but the flag was never
written to the database, so nothing downstream could act on it. The consequence is
quiet and one-directional: an unfinished hand has `collected = 0` for everyone
still in it, so each of them is booked as losing everything they put in. It is a
loss that never happened, and it only ever biases one way.

The actions in such a hand are real, so it still counts for VPIP, PFR and the rest.
Only the money is unknown, and unknown is not zero.
"""

from __future__ import annotations

import dataclasses

from pnt.db.conn import connect
from pnt.ingest.importer import import_csv
from pnt.stats.queries import aggregate, facts_for, load_hands
from tests.conftest import TRUNCATED, TRUNCATED_GAME


def _db(tmp_path):
    conn = connect(tmp_path / "t.sqlite")
    import_csv(conn, TRUNCATED)
    return conn


def test_the_flag_reaches_the_database(tmp_path):
    conn = _db(tmp_path)
    rows = conn.execute(
        "SELECT hand_number, complete FROM hands WHERE game_id = ? ORDER BY hand_number",
        (TRUNCATED_GAME,),
    ).fetchall()
    assert rows, "fixture did not import"
    incomplete = [r["hand_number"] for r in rows if not r["complete"]]
    assert incomplete, "this fixture exists because its log stops mid-hand"
    # Only the last hand can be cut off; anything else would mean a parsing fault.
    assert incomplete == [max(r["hand_number"] for r in rows)]


def test_load_hands_carries_it_into_the_derivation(tmp_path):
    hands = load_hands(_db(tmp_path), TRUNCATED_GAME)
    assert sum(1 for h in hands if not h.complete) == 1


def test_an_unfinished_hand_is_left_out_of_bb_per_100(tmp_path):
    """Its chips are half-recorded, so it may not move a money figure."""
    conn = _db(tmp_path)
    hands = load_hands(conn, TRUNCATED_GAME)
    cut = next(h for h in hands if not h.complete)
    loser = next(p for p in cut.players.values() if p.contributed > 0 and p.collected == 0)

    alias = conn.execute(
        "SELECT p.alias FROM players p JOIN player_identities pi"
        " ON pi.player_id = p.player_id WHERE pi.pn_id = ?",
        (loser.pn_id,),
    ).fetchone()["alias"]

    facts = facts_for(conn, alias)
    assert any(not f.complete for f in facts), "the player is in the unfinished hand"

    stats = aggregate(facts)
    without = aggregate([f for f in facts if f.complete])
    assert stats["bb_per_100"] == without["bb_per_100"]

    # And it would have mattered: the hand is a pure phantom loss.
    naive = aggregate([dataclasses.replace(f, complete=True) for f in facts])
    assert naive["bb_per_100"] < stats["bb_per_100"], (
        "counting the unfinished hand must look worse than excluding it"
    )


def test_the_hand_still_counts_for_everything_else(tmp_path):
    """Only the money is unknown. The player folded or bet or showed down for real."""
    conn = _db(tmp_path)
    hands = load_hands(conn, TRUNCATED_GAME)
    cut = next(h for h in hands if not h.complete)
    pn_id = next(iter(cut.players))
    alias = conn.execute(
        "SELECT p.alias FROM players p JOIN player_identities pi"
        " ON pi.player_id = p.player_id WHERE pi.pn_id = ?",
        (pn_id,),
    ).fetchone()["alias"]

    facts = facts_for(conn, alias)
    assert aggregate(facts)["hands"] == len(facts)
    assert aggregate(facts)["hands"] > len([f for f in facts if f.complete])
