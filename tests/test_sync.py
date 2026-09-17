"""Deleting a log from the log folder removes its game; adding one imports it."""

from __future__ import annotations

import shutil

import pytest
from typer.testing import CliRunner

from pnt import cli
from pnt.db.conn import connect
from pnt.ingest import sync
from pnt.ingest.csv_source import read_csv
from pnt.ingest.importer import delete_game, import_csv, merge_players, rename_player
from pnt.stats.queries import report
from tests.conftest import ALL_LOGS, HU, HU_GAME, MULTIWAY, MULTIWAY_GAME, THREE


def _count(conn, table: str, game_id: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE game_id = ?", (game_id,)).fetchone()[0]


@pytest.fixture()
def folder(tmp_path):
    """A log folder holding two of the core logs."""
    d = tmp_path / "logs"
    d.mkdir()
    for log in (HU, MULTIWAY):
        shutil.copy(log, d / log.name)
    return d


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "sync.sqlite")
    yield c
    c.close()


def test_delete_game_is_the_inverse_of_importing_it(db, tmp_path):
    only_multiway = connect(tmp_path / "one.sqlite")
    import_csv(only_multiway, MULTIWAY)

    assert delete_game(db, HU_GAME) == 188
    for table in ("hands", "games", "raw_entries", "imports"):
        assert _count(db, table, HU_GAME) == 0, table
    orphans = db.execute(
        "SELECT COUNT(*) FROM hand_players WHERE hand_id NOT IN (SELECT hand_id FROM hands)"
    ).fetchone()[0]
    assert orphans == 0

    # THREE is still in `db`; restrict to MULTIWAY to compare like with like.
    assert report(db, game_id=MULTIWAY_GAME) == report(only_multiway, game_id=MULTIWAY_GAME)

    import_csv(db, HU)
    assert _count(db, "hands", HU_GAME) == 188


def _only_in(db, game_id: str) -> list[str]:
    """PokerNow IDs dealt into `game_id` and no other game."""
    return [
        r[0]
        for r in db.execute(
            "SELECT DISTINCT hp.pn_id FROM hand_players hp JOIN hands h USING (hand_id)"
            " WHERE h.game_id = ? AND hp.pn_id NOT IN ("
            "   SELECT hp.pn_id FROM hand_players hp JOIN hands h USING (hand_id)"
            "   WHERE h.game_id != ?)"
            " ORDER BY hp.pn_id",
            (game_id, game_id),
        )
    ]


def _alias(db, pn_id: str) -> str | None:
    row = db.execute(
        "SELECT p.alias FROM players p JOIN player_identities pi USING (player_id)"
        " WHERE pi.pn_id = ?",
        (pn_id,),
    ).fetchone()
    return row[0] if row else None


def test_deleting_a_game_keeps_renames_and_drops_players_it_alone_created(db):
    renamed, untouched, *_ = _only_in(db, MULTIWAY_GAME)
    rename_player(db, _alias(db, renamed), "kept by hand")

    delete_game(db, MULTIWAY_GAME)
    assert _alias(db, renamed) == "kept by hand"
    assert _alias(db, untouched) is None, "a player only the deleted log knew about goes with it"


def test_a_merged_player_survives_losing_every_hand(db):
    a, b, *_ = _only_in(db, MULTIWAY_GAME)
    merge_players(db, _alias(db, a), _alias(db, b))
    target = _alias(db, b)

    delete_game(db, MULTIWAY_GAME)
    assert _alias(db, a) == _alias(db, b) == target


def test_sync_imports_the_folder_then_does_nothing(conn, folder):
    first = sync.sync_folder(conn, folder)
    assert {s["game_id"] for s in first.imported} == {HU_GAME, MULTIWAY_GAME}
    assert _count(conn, "hands", HU_GAME) == 188

    again = sync.sync_folder(conn, folder)
    assert again.imported == [] and again.removed == {}


def test_deleting_a_log_removes_its_game(conn, folder):
    sync.sync_folder(conn, folder)
    (folder / HU.name).unlink()

    out = sync.sync_folder(conn, folder)
    assert out.removed == {HU_GAME: 188}
    assert _count(conn, "raw_entries", HU_GAME) == 0
    assert _count(conn, "hands", MULTIWAY_GAME) > 0


def test_a_log_put_back_comes_back(conn, folder):
    sync.sync_folder(conn, folder)
    (folder / HU.name).unlink()
    sync.sync_folder(conn, folder)
    shutil.copy(HU, folder / HU.name)

    out = sync.sync_folder(conn, folder)
    assert [s["game_id"] for s in out.imported] == [HU_GAME]
    assert _count(conn, "hands", HU_GAME) == 188


def test_a_changed_log_is_read_again(conn, folder):
    rows = read_csv(HU)
    target = folder / HU.name
    from pnt.ingest.log_folder import write_log

    target.unlink()
    write_log(rows[:1000], target)
    sync.sync_folder(conn, folder)
    before = _count(conn, "raw_entries", HU_GAME)

    write_log(rows, target)
    out = sync.sync_folder(conn, folder)
    assert out.imported[0]["entries_new"] == len(rows) - before


def test_games_from_outside_the_folder_are_never_removed(conn, folder):
    import_csv(conn, THREE)  # straight from the fixtures, never in `folder`
    sync.sync_folder(conn, folder)
    gid = sync.untracked_games(conn)
    assert len(gid) == 1

    for f in folder.iterdir():
        f.unlink()
    out = sync.sync_folder(conn, folder)
    assert set(out.removed) == {HU_GAME, MULTIWAY_GAME}
    assert _count(conn, "raw_entries", gid[0]) > 0


def test_a_missing_folder_is_unavailable_not_deleted(conn, folder, tmp_path):
    sync.sync_folder(conn, folder)
    folder.rename(tmp_path / "unplugged")

    assert sync.sync_folder(conn, folder).removed == {}
    assert _count(conn, "hands", HU_GAME) == 188


def test_deleting_one_of_two_copies_keeps_the_game(conn, folder, tmp_path):
    """A game on record in two folders (say, the log folder moved) needs both gone."""
    other = tmp_path / "old-logs"
    other.mkdir()
    shutil.copy(HU, other / HU.name)
    sync.sync_folder(conn, other)
    sync.sync_folder(conn, folder)
    (folder / HU.name).unlink()

    assert sync.sync_folder(conn, folder).removed == {}
    assert _count(conn, "hands", HU_GAME) == 188

    (other / HU.name).unlink()
    assert sync.sync_folder(conn, folder).removed == {HU_GAME: 188}


def test_a_broken_file_is_reported_once(conn, folder):
    bad = folder / "poker_now_log_broken.csv"
    bad.write_text("not,a,log\n1,2,3\n", encoding="utf-8")
    skip: dict = {}

    assert str(bad) in sync.sync_folder(conn, folder, skip).failed
    assert sync.sync_folder(conn, folder, skip).failed == {}


def test_cli_import_puts_folder_files_on_record(tmp_path, folder):
    db_path = tmp_path / "cli.sqlite"
    runner = CliRunner()
    result = runner.invoke(cli.app, ["import", "--db", str(db_path), "--log-dir", str(folder)])
    assert result.exit_code == 0, result.output

    (folder / HU.name).unlink()
    result = runner.invoke(cli.app, ["sync", "--db", str(db_path), "--log-dir", str(folder)])
    assert result.exit_code == 0, result.output
    assert f"{HU_GAME}: log deleted, removed 188 hands" in result.output
    result = runner.invoke(cli.app, ["sync", "--db", str(db_path), "--log-dir", str(folder)])
    assert "in sync" in result.output


def test_cli_sync_prunes_untracked_only_when_asked(tmp_path, folder):
    db_path = tmp_path / "cli.sqlite"
    c = connect(db_path)
    for log in ALL_LOGS:
        import_csv(c, log)  # from the fixtures folder: none of them on record
    c.close()
    runner = CliRunner()
    args = ["sync", "--db", str(db_path), "--log-dir", str(folder)]

    result = runner.invoke(cli.app, args)
    assert "1 game(s) have no log file" in result.output  # THREE; the other two are in `folder`

    result = runner.invoke(cli.app, [*args, "--prune-untracked"])
    assert result.exit_code == 0, result.output
    c = connect(db_path)
    assert sync.untracked_games(c) == []
    assert c.execute("SELECT COUNT(DISTINCT game_id) FROM hands").fetchone()[0] == 2
