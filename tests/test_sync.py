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


def test_a_second_download_of_an_export_is_the_same_game(conn, folder):
    """A browser saves it as `... (1).csv`; read as game `X (1)`, every hand counted twice."""
    shutil.copy(HU, folder / HU.name.replace(".csv", " (1).csv"))
    shutil.copy(HU, folder / HU.name.replace(".csv", "(2).csv"))  # Firefox's spelling

    out = sync.sync_folder(conn, folder)
    assert out.failed == {}
    assert {s["game_id"] for s in out.imported} == {HU_GAME, MULTIWAY_GAME}
    assert conn.execute("SELECT COUNT(DISTINCT game_id) FROM hands").fetchone()[0] == 2
    assert _count(conn, "hands", HU_GAME) == 188

    # Each copy is on record, so deleting the original leaves the game to the copies.
    (folder / HU.name).unlink()
    assert sync.sync_folder(conn, folder).removed == {}
    assert _count(conn, "hands", HU_GAME) == 188


def test_a_log_name_without_a_game_id_is_reported_not_imported(conn, folder):
    bad = folder / "poker_now_log_not an id!.csv"
    shutil.copy(HU, bad)
    out = sync.sync_folder(conn, folder)
    assert "game ID" in out.failed[str(bad)]
    assert {s["game_id"] for s in out.imported} == {HU_GAME, MULTIWAY_GAME}


def test_a_cut_off_file_does_not_hold_up_the_rest_of_the_folder(conn, folder):
    """A short last row once raised TypeError out of every pass, for good."""
    first = folder / "poker_now_log_aaaBroken.csv"  # sorts ahead of the good logs
    text = HU.read_text(encoding="utf-8")
    first.write_text(text[: len(text) * 2 // 3], encoding="utf-8")
    skip: dict = {}

    out = sync.sync_folder(conn, folder, skip)
    assert out.failed == {}
    assert {s["game_id"] for s in out.imported} == {"aaaBroken", HU_GAME, MULTIWAY_GAME}
    # What was whole of the cut-off copy is in, and nothing out of place.
    rows = read_csv(first)
    stored = [r[0] for r in conn.execute("SELECT ord FROM raw_entries WHERE game_id = 'aaaBroken' ORDER BY ord")]
    assert stored == [r.ord for r in rows]
    assert set(stored) <= {r.ord for r in read_csv(HU)}


def test_a_file_the_import_trips_on_does_not_hold_up_the_rest(conn, folder, monkeypatch):
    real = sync.import_file

    def flaky(conn, path, *a, **k):
        if path.name == HU.name:
            raise KeyError("a parser bug")
        return real(conn, path, *a, **k)

    monkeypatch.setattr(sync, "import_file", flaky)
    out = sync.sync_folder(conn, folder, {})
    assert str(folder / HU.name) in out.failed
    assert [s["game_id"] for s in out.imported] == [MULTIWAY_GAME]


def test_the_stamp_recorded_is_the_one_read(conn, folder, monkeypatch):
    """A write landing while the file is imported must still read as a change."""
    real = sync.rebuild_game

    def rebuild_then_append(conn, gid):
        out = real(conn, gid)
        if gid == HU_GAME:
            with (folder / HU.name).open("a", encoding="utf-8") as fh:
                fh.write("\n")  # the file moves on under the import
        return out

    monkeypatch.setattr(sync, "rebuild_game", rebuild_then_append)
    sync.sync_folder(conn, folder)
    monkeypatch.setattr(sync, "rebuild_game", real)
    again = sync.sync_folder(conn, folder)
    assert [s["game_id"] for s in again.imported] == [HU_GAME]


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


#: A table joined and left before the first deal: the whole log.
JOINED_AND_LEFT = (
    "entry,at,order\n"
    '"The player ""DK @ 6AN-TeA3Jk"" requested a seat.",2026-09-17T02:40:51.200Z,178961285120000\n'
    '"The game\'s big blind was changed from 20 to 10.",2026-09-17T02:40:30.741Z,178961283074102\n'
)


def test_a_log_with_no_hands_is_not_imported(conn, folder):
    empty = folder / "poker_now_log_joinedandleft.csv"
    empty.write_text(JOINED_AND_LEFT, encoding="utf-8")

    out = sync.sync_folder(conn, folder)
    assert out.empty == [str(empty)]
    assert {s["game_id"] for s in out.imported} == {HU_GAME, MULTIWAY_GAME}
    assert _count(conn, "raw_entries", "joinedandleft") == 0
    assert _count(conn, "games", "joinedandleft") == 0

    # Remembered, like a broken file: a poller does not re-read it until it changes.
    skip: dict = {}
    assert sync.sync_folder(conn, folder, skip).empty == [str(empty)]
    assert sync.sync_folder(conn, folder, skip).empty == []


def test_remove_empty_deletes_handless_logs_and_their_games(conn, folder):
    empty = folder / "poker_now_log_joinedandleft.csv"
    empty.write_text(JOINED_AND_LEFT, encoding="utf-8")
    sync.sync_folder(conn, folder)
    # One imported before logs with no hands were skipped.
    sync.import_file(conn, empty)
    assert _count(conn, "games", "joinedandleft") == 1

    assert sync.remove_empty(conn, folder) == [empty]
    assert not empty.exists()
    assert _count(conn, "games", "joinedandleft") == 0
    assert _count(conn, "hands", HU_GAME) == 188  # games with hands are untouched
    assert (folder / HU.name).exists()


def test_sync_cli_removes_empty_logs_only_when_asked(tmp_path, folder):
    empty = folder / "poker_now_log_joinedandleft.csv"
    empty.write_text(JOINED_AND_LEFT, encoding="utf-8")
    db_path = tmp_path / "cli.sqlite"
    runner = CliRunner()

    r = runner.invoke(cli.app, ["sync", "--db", str(db_path), "--log-dir", str(folder)])
    assert r.exit_code == 0, r.output
    assert "1 log(s) with no hands skipped" in r.output
    assert empty.exists()

    r = runner.invoke(
        cli.app, ["sync", "--db", str(db_path), "--log-dir", str(folder), "--remove-empty"]
    )
    assert r.exit_code == 0, r.output
    assert f"{empty.name}: no hands, deleted" in r.output
    assert not empty.exists()


def test_the_server_sync_survives_a_failed_open(tmp_path, monkeypatch):
    """At start a migration or a rebuild can hold the database past busy_timeout.
    The sync thread must try again next interval, not die for good."""
    import sqlite3
    import threading

    from pnt.server import app as server

    stop = threading.Event()
    opens: list[str] = []

    def flaky_db():
        opens.append("try")
        if len(opens) == 1:
            raise sqlite3.OperationalError("database is locked")
        stop.set()  # the second open got through: one pass, then stop
        return connect(tmp_path / "t.sqlite")

    monkeypatch.setattr(server, "capture_db", flaky_db)
    monkeypatch.setattr(server, "SYNC_SECONDS", 0.01)
    monkeypatch.setattr(server.log_folder, "LOG_DIR", tmp_path / "logs")
    server._sync_loop(stop)
    assert len(opens) == 2


def test_a_log_name_without_a_game_id_is_refused_by_import(conn, tmp_path):
    """Stored under `my game`, it could be neither served (422) nor written back."""
    from pnt.ingest.importer import import_csv as imp

    bad = tmp_path / "poker_now_log_my game.csv"
    shutil.copy(HU, bad)
    with pytest.raises(ValueError, match="game ID"):
        imp(conn, bad)
    assert conn.execute("SELECT COUNT(*) FROM raw_entries").fetchone()[0] == 0
    # A bare ID is a name, and an explicit one wins over the file's.
    plain = tmp_path / f"{HU_GAME}.csv"
    shutil.copy(HU, plain)
    assert imp(conn, plain)["game_id"] == HU_GAME
    with pytest.raises(ValueError, match="game ID"):
        imp(conn, plain, game_id="../up")

    db_path = tmp_path / "cli.sqlite"
    r = CliRunner().invoke(cli.app, ["import", "--db", str(db_path), str(bad), str(MULTIWAY)])
    assert r.exit_code == 1
    assert r.exception is None or isinstance(r.exception, SystemExit)
    assert "poker_now_log_my game.csv: no PokerNow game ID in the file name" in r.output
    assert "Traceback" not in r.output
    assert f"{MULTIWAY.name}: 128 hands" in r.output  # the rest still go in


def test_remove_empty_spares_games_when_the_name_holds_no_id(conn, folder):
    empty = folder / "poker_now_log_not an id.csv"
    empty.write_text(JOINED_AND_LEFT, encoding="utf-8")
    sync.sync_folder(conn, folder)
    assert sync.remove_empty(conn, folder) == [empty]
    assert not empty.exists()
    assert _count(conn, "hands", HU_GAME) == 188
