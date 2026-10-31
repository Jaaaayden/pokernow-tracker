"""Settings, saved spots and hand tags: what you told the tracker, kept through a
rebuild like a flag is."""

from __future__ import annotations

import pytest

from pnt.db import userdata as ud
from tests.conftest import HU_GAME, MULTIWAY_GAME


def _rebuild_all(db):
    from pnt.ingest.importer import rebuild_game

    for (gid,) in db.execute("SELECT game_id FROM raw_entries GROUP BY game_id").fetchall():
        rebuild_game(db, gid)


# --------------------------------------------------------------- settings ---


def test_a_setting_round_trips_as_json(db):
    assert ud.get_setting(db, "hud") is None
    assert ud.get_setting(db, "hud", {"columns": []}) == {"columns": []}
    value = {"columns": ["vpip", "pfr", "cbet_flop"], "n": 3}
    assert ud.set_setting(db, "hud", value) == value
    assert ud.get_setting(db, "hud") == value
    ud.set_setting(db, "hud", ["fold_to_cbet_flop"])
    assert ud.get_setting(db, "hud") == ["fold_to_cbet_flop"]
    assert ud.delete_setting(db, "hud") and not ud.delete_setting(db, "hud")
    assert ud.get_setting(db, "hud", 7) == 7


# ------------------------------------------------------------ saved spots ---


def test_spots_save_list_and_refuse_a_taken_name(db):
    a = ud.save_spot(db, "  BTN opens ", "opener,position=BTN")
    b = ud.save_spot(db, "river bluffs", "bet_river=overbet")
    assert a["name"] == "BTN opens" and a["pinned"] is False and a["created_at"]
    assert [s["spot_id"] for s in ud.list_spots(db)] == [a["spot_id"], b["spot_id"]]
    with pytest.raises(ValueError, match="already exists"):
        ud.save_spot(db, "BTN opens", "srp")
    for name, filt in (("", "srp"), ("x", "  ")):
        with pytest.raises(ValueError):
            ud.save_spot(db, name, filt)


def test_spots_rename_pin_reorder_and_delete(db):
    ids = [ud.save_spot(db, n, "srp")["spot_id"] for n in ("one", "two", "three")]
    assert ud.rename_spot(db, ids[0], "uno")["name"] == "uno"
    with pytest.raises(ValueError):
        ud.rename_spot(db, ids[1], "uno")
    assert ud.update_spot(db, ids[1], filter="3bet_pot")["filter"] == "3bet_pot"

    assert ud.pin_spot(db, ids[2])["pinned"] is True
    assert [s["spot_id"] for s in ud.list_spots(db)] == [ids[2], ids[0], ids[1]]  # pinned first

    ud.reorder_spots(db, [ids[1]])
    assert [s["spot_id"] for s in ud.list_spots(db)] == [ids[2], ids[1], ids[0]]
    with pytest.raises(LookupError):
        ud.reorder_spots(db, [ids[0], 999])
    assert [s["spot_id"] for s in ud.list_spots(db)] == [ids[2], ids[1], ids[0]]  # nothing written

    ud.pin_spot(db, ids[2], False)
    assert ud.delete_spot(db, ids[1]) and not ud.delete_spot(db, ids[1])
    assert [s["spot_id"] for s in ud.list_spots(db)] == [ids[2], ids[0]]
    with pytest.raises(LookupError):
        ud.rename_spot(db, ids[1], "gone")
    with pytest.raises(LookupError):
        ud.get_spot(db, ids[1])


# -------------------------------------------------------------- hand tags ---


def test_tags_are_normalised():
    assert ud.normalise_tag("  River   Bluff ") == "river-bluff"
    assert ud.normalise_tag("cooler") == "cooler"
    for bad in ("", "   ", "a,b", "a|b", "(x)", "x" * 41):
        with pytest.raises(ValueError):
            ud.normalise_tag(bad)


def test_tags_round_trip(db):
    first = ud.add_tag(db, HU_GAME, 53, "River Bluff")
    assert first == "river-bluff"
    ud.add_tag(db, HU_GAME, 53, "river-bluff")  # idempotent
    ud.add_tag(db, HU_GAME, 53, "study")
    ud.add_tag(db, MULTIWAY_GAME, 10, "study")
    assert ud.tags_for_hand(db, HU_GAME, 53) == ["river-bluff", "study"]
    assert ud.tags_for_hands(db) == {(HU_GAME, 53): ["river-bluff", "study"], (MULTIWAY_GAME, 10): ["study"]}
    assert ud.tags_for_hands(db, MULTIWAY_GAME) == {(MULTIWAY_GAME, 10): ["study"]}
    assert [(t["tag"], t["hands"]) for t in ud.all_tags(db)] == [("study", 2), ("river-bluff", 1)]
    assert ud.tagged_keys(db, "Study") == {(HU_GAME, 53), (MULTIWAY_GAME, 10)}
    assert ud.tagged_keys(db, ["river bluff"]) == {(HU_GAME, 53)}
    assert ud.tagged_keys(db, []) == set()

    assert ud.remove_tag(db, HU_GAME, 53, "STUDY") and not ud.remove_tag(db, HU_GAME, 53, "study")
    assert ud.tags_for_hand(db, HU_GAME, 53) == ["river-bluff"]


def test_tagging_a_hand_that_is_not_there_raises_but_removing_does_not(db):
    with pytest.raises(ValueError, match="no hand #99999"):
        ud.add_tag(db, HU_GAME, 99999, "x")
    with pytest.raises(ValueError):
        ud.add_tag(db, "nosuchgame", 1, "x")
    assert ud.remove_tag(db, "nosuchgame", 1, "x") is False


def test_flagged_is_refused_as_a_tag_by_the_data_layer(db):
    """`tag=flagged` reads the 🚩s, so a stored `flagged` tag would never be found.
    Refused in `add_tag` itself, not only by the server's route."""
    for tag in ("flagged", " Flagged "):
        with pytest.raises(ValueError, match="flag the hand instead"):
            ud.add_tag(db, HU_GAME, 53, tag)
    assert ud.tags_for_hand(db, HU_GAME, 53) == []


# ---------------------------------------------------------------- rebuild ---


def test_a_rebuild_keeps_all_of_it(db):
    ud.set_setting(db, "hud", {"columns": ["vpip"]})
    spot = ud.save_spot(db, "mine", "srp")
    ud.pin_spot(db, spot["spot_id"])
    ud.add_tag(db, HU_GAME, 53, "cooler")
    before = (ud.get_setting(db, "hud"), ud.list_spots(db), ud.tags_for_hands(db), ud.all_tags(db))
    _rebuild_all(db)
    assert (ud.get_setting(db, "hud"), ud.list_spots(db), ud.tags_for_hands(db), ud.all_tags(db)) == before


def test_deleting_a_game_keeps_its_tags_like_its_flags(db):
    """A flag outlives its game, so the hand can be imported again and keep it;
    a tag does the same."""
    from pnt.ingest.importer import delete_game, import_csv
    from pnt.stats import review as rv
    from tests.conftest import HU

    rv.set_flag(db, HU_GAME, 53)
    ud.add_tag(db, HU_GAME, 53, "cooler")
    delete_game(db, HU_GAME)
    assert rv.is_flagged(db, HU_GAME, 53)
    assert ud.tags_for_hand(db, HU_GAME, 53) == ["cooler"]
    import_csv(db, HU)
    assert ud.tagged_keys(db, "cooler") == {(HU_GAME, 53)}


def test_an_older_database_gains_the_tables(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` reaches a file made before them on its next open."""
    import sqlite3

    from pnt.db.conn import connect

    path = tmp_path / "old.sqlite"
    conn = connect(path)
    for table in ("settings", "saved_spots", "hand_tags"):
        conn.execute(f"DROP TABLE {table}")
    conn.commit()
    conn.close()
    raw = sqlite3.connect(path)
    assert not raw.execute("SELECT name FROM sqlite_master WHERE name = 'hand_tags'").fetchone()
    raw.close()
    conn = connect(path)
    assert ud.set_setting(conn, "k", 1) == 1
    assert ud.list_spots(conn) == []
    conn.close()
