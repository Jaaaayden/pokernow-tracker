"""The alias table survives a fresh database by way of `export_aliases`/`apply_aliases`."""

from __future__ import annotations

from conftest import ALL_LOGS, HU

from pnt.db.conn import connect
from pnt.ingest.importer import (
    apply_aliases,
    export_aliases,
    import_csv,
    merge_players,
    rename_player,
)
from pnt.stats.queries import report


def _fresh(tmp_path, name, logs=ALL_LOGS):
    conn = connect(tmp_path / name)
    for path in logs:
        import_csv(conn, path)
    return conn


def _groups(conn):
    out: dict[str, set[str]] = {}
    for pn_id, alias in export_aliases(conn):
        out.setdefault(alias, set()).add(pn_id)
    return out


def test_round_trip_restores_merges_renames_and_every_number(db, tmp_path):
    aliases = [r["player"] for r in report(db)]
    merge_players(db, aliases[1], aliases[0])
    rename_player(db, aliases[0], "someone")
    pairs = export_aliases(db)

    fresh = _fresh(tmp_path, "fresh.sqlite")
    moved, unknown = apply_aliases(fresh, pairs)

    assert moved > 0 and unknown == 0
    assert _groups(fresh) == _groups(db)
    assert report(fresh) == report(db)


def test_applying_twice_moves_nothing(db, tmp_path):
    aliases = [r["player"] for r in report(db)]
    merge_players(db, aliases[1], aliases[0])
    pairs = export_aliases(db)
    fresh = _fresh(tmp_path, "fresh.sqlite")
    apply_aliases(fresh, pairs)
    assert apply_aliases(fresh, pairs) == (0, 0)


def test_ids_not_imported_yet_are_skipped_not_invented(db, tmp_path):
    pairs = export_aliases(db)
    partial = _fresh(tmp_path, "partial.sqlite", logs=[HU])
    known = {pn_id for pn_id, _ in export_aliases(partial)}

    _, unknown = apply_aliases(partial, pairs)

    assert unknown == sum(1 for pn_id, _ in pairs if pn_id not in known)
    assert {pn_id for pn_id, _ in export_aliases(partial)} == known


def test_a_stranger_holding_the_name_is_moved_aside_not_merged(db):
    (a, a_ids), (_b, b_ids) = list(_groups(db).items())[:2]
    # Say `_b`'s IDs belong to someone called `a` -- but `a` already exists and holds
    # IDs the file does not give it.
    apply_aliases(db, [(i, a) for i in b_ids])

    groups = _groups(db)
    assert groups[a] == b_ids
    assert any(alias.startswith(f"{a} (") and ids == a_ids for alias, ids in groups.items())
