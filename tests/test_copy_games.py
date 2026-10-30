"""A database that read `poker_now_log_X (1).csv` as a game `X (1)` of its own,
before csv_source dropped the copy number, has it merged into X on open."""

from __future__ import annotations

import shutil

from pnt.db.conn import COPY_GAMES_KEY, connect, generation
from pnt.ingest import sync
from pnt.ingest.csv_source import read_csv
from pnt.ingest.importer import ingest_entries, needs_parse_rebuild, rebuild_game
from pnt.logfmt.parser import PARSE_VERSION
from tests.conftest import HU, HU_GAME

ROWS = read_csv(HU)
NOW = "2026-10-01T00:00:00+00:00"


def _old_db(path, folder, game_ids: list[str]):
    """A database as the old copy-number-blind code left it: each of `game_ids`
    imported from its own file in `folder`, and the merge not yet run."""
    c = connect(path)
    for gid in game_ids:
        name = "poker_now_log_" + gid + ".csv"
        shutil.copy(HU, folder / name)
        ingest_entries(c, gid, ROWS, source=f"csv:{name}")
        rebuild_game(c, gid)
        sync.record_file(c, folder / name, gid)
    c.execute("DELETE FROM meta WHERE key = ?", (COPY_GAMES_KEY,))
    c.commit()
    return c


def _ids(c) -> set[str]:
    return {r[0] for r in c.execute("SELECT game_id FROM raw_entries GROUP BY game_id")} | {
        r[0] for r in c.execute("SELECT game_id FROM games")
    }


def test_a_copy_beside_its_game_is_merged_into_it(tmp_path):
    folder = tmp_path / "logs"
    folder.mkdir()
    copy = f"{HU_GAME} (1)"
    c = _old_db(tmp_path / "old.sqlite", folder, [HU_GAME, copy])
    base_hand_ids = [r[0] for r in c.execute(
        "SELECT hand_id FROM hands WHERE game_id = ? ORDER BY hand_number", (HU_GAME,))]
    # What you wrote on the copy's hands, and a note on the same hand in both.
    c.executescript(f"""
        INSERT INTO hand_reviews VALUES ('{copy}', 3, '{NOW}');
        INSERT INTO hand_flags VALUES ('{copy}', 4, '{NOW}');
        INSERT INTO hand_tags VALUES ('{copy}', 4, 'cooler', '{NOW}');
        INSERT INTO hand_tags VALUES ('{HU_GAME}', 4, 'cooler', '{NOW}');
        INSERT INTO hand_notes VALUES ('{copy}', 5, 'from the copy', '{NOW}');
        INSERT INTO hand_notes VALUES ('{HU_GAME}', 5, 'from the game', '2026-09-01');
        INSERT INTO hand_notes VALUES ('{copy}', 6, 'same', '{NOW}');
        INSERT INTO hand_notes VALUES ('{HU_GAME}', 6, 'same', '{NOW}');
    """)
    gen = generation(c)
    c.close()

    c = connect(tmp_path / "old.sqlite")
    assert _ids(c) == {HU_GAME}
    assert c.execute("SELECT COUNT(*) FROM raw_entries").fetchone()[0] == len(ROWS)
    assert c.execute("SELECT COUNT(*) FROM hands").fetchone()[0] == 188
    # The game's own hands are untouched until the rebuild the stale mark asks for.
    assert [r[0] for r in c.execute(
        "SELECT hand_id FROM hands WHERE game_id = ? ORDER BY hand_number", (HU_GAME,))
    ] == base_hand_ids
    assert generation(c) > gen
    assert c.execute("SELECT parsed_with FROM games").fetchone()[0] == 0
    assert needs_parse_rebuild(c)
    # The copy's file is on record as a file of the game.
    assert {r[0] for r in c.execute("SELECT DISTINCT game_id FROM log_files")} == {HU_GAME}
    assert {r[0] for r in c.execute("SELECT DISTINCT game_id FROM imports")} == {HU_GAME}

    assert [tuple(r) for r in c.execute("SELECT game_id, hand_number FROM hand_reviews")] == [(HU_GAME, 3)]
    assert [tuple(r) for r in c.execute("SELECT game_id, hand_number FROM hand_flags")] == [(HU_GAME, 4)]
    assert [tuple(r) for r in c.execute("SELECT game_id, hand_number, tag FROM hand_tags")] == [
        (HU_GAME, 4, "cooler")
    ]
    notes = {r[0]: (r[1], r[2]) for r in c.execute(
        "SELECT hand_number, note, noted_at FROM hand_notes WHERE game_id = ?", (HU_GAME,))}
    assert notes == {5: ("from the game\n\nfrom the copy", NOW), 6: ("same", NOW)}
    assert c.execute("SELECT COUNT(*) FROM hand_notes").fetchone()[0] == 2

    # A sync finds both files unchanged and brings no copy back.
    out = sync.sync_folder(c, folder)
    assert out.imported == [] and out.removed == {} and _ids(c) == {HU_GAME}

    rebuild_game(c, HU_GAME)
    assert c.execute("SELECT parsed_with FROM games").fetchone()[0] == PARSE_VERSION
    assert c.execute("SELECT COUNT(*) FROM hands").fetchone()[0] == 188
    assert not needs_parse_rebuild(c)

    # Once per database: a second open changes nothing.
    gen = generation(c)
    c.close()
    c = connect(tmp_path / "old.sqlite")
    assert generation(c) == gen and _ids(c) == {HU_GAME}
    c.close()


def test_a_copy_alone_becomes_the_game(tmp_path):
    folder = tmp_path / "logs"
    folder.mkdir()
    copy = f"{HU_GAME}(2)"  # Firefox's spelling
    c = _old_db(tmp_path / "old.sqlite", folder, [copy])
    hand_ids = [r[0] for r in c.execute("SELECT hand_id FROM hands ORDER BY hand_number")]
    c.execute(f"INSERT INTO hand_flags VALUES ('{copy}', 9, '{NOW}')")
    c.commit()
    c.close()

    c = connect(tmp_path / "old.sqlite")
    assert _ids(c) == {HU_GAME}
    assert c.execute("SELECT COUNT(*) FROM raw_entries").fetchone()[0] == len(ROWS)
    assert [r[0] for r in c.execute(
        "SELECT hand_id FROM hands WHERE game_id = ? ORDER BY hand_number", (HU_GAME,))
    ] == hand_ids
    assert c.execute("SELECT parsed_with FROM games WHERE game_id = ?", (HU_GAME,)).fetchone()[0] == 0
    assert [tuple(r) for r in c.execute("SELECT game_id, hand_number FROM hand_flags")] == [(HU_GAME, 9)]
    assert [r[0] for r in c.execute("SELECT game_id FROM log_files")] == [HU_GAME]

    # The file is still on record, so deleting it removes the game it now holds.
    (folder / f"poker_now_log_{copy}.csv").unlink()
    assert sync.sync_folder(c, folder).removed == {HU_GAME: 188}
    c.close()


def test_a_database_with_no_copies_is_left_alone(db, tmp_path):
    gen = generation(db)
    db.execute("DELETE FROM meta WHERE key = ?", (COPY_GAMES_KEY,))
    db.commit()
    path = db.execute("PRAGMA database_list").fetchone()[2]
    c = connect(path)
    assert generation(c) == gen
    assert c.execute("SELECT MIN(parsed_with) FROM games").fetchone()[0] == PARSE_VERSION
    assert c.execute("SELECT 1 FROM meta WHERE key = ?", (COPY_GAMES_KEY,)).fetchone()
    c.close()

    fresh = connect(tmp_path / "fresh.sqlite")
    assert generation(fresh) == 0
    fresh.close()
