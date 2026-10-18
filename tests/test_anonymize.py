"""Anonymizing logs: nobody left in them, and nothing the tracker derives changed."""

from __future__ import annotations

import json
import re

from pnt.db.conn import connect
from pnt.ingest.csv_source import read_csv
from pnt.ingest.importer import import_csv
from pnt.logfmt.anonymize import STAND_IN, Pseudonyms, anonymize_aliases, anonymize_file, players_in
from pnt.stats.queries import report
from tests.conftest import ALL_LOGS, HU, HU_GAME


def test_stand_ins_are_consistent_and_keep_the_shape_of_what_they_replace():
    names = Pseudonyms(key=b"k" * 32)
    assert names.pn_id("5NARaPRkSp") == names.pn_id("5NARaPRkSp")
    assert len(names.pn_id("5NARaPRkSp")) == 10 and re.fullmatch(r"[A-Za-z0-9_-]+", names.pn_id("5NARaPRkSp"))
    assert names.pn_id("5NARaPRkSp") != "5NARaPRkSp"
    fake_game = names.game_id(HU_GAME)
    assert fake_game.startswith("pgl") and len(fake_game) == len(HU_GAME) and fake_game != HU_GAME
    assert names.name("Chris") == "player 1" and names.name("henry") == "player 2" and names.name("Chris") == "player 1"
    line = 'The player "Chris @ 5NARaPRkSp" quits the game with a stack of 0.'
    assert names.entry(line) == f'The player "player 1 @ {names.pn_id("5NARaPRkSp")}" quits the game with a stack of 0.'


def test_two_runs_share_no_stand_ins():
    """The key is never kept, so a published stand-in says nothing about the ID behind it."""
    assert Pseudonyms().pn_id("5NARaPRkSp") != Pseudonyms().pn_id("5NARaPRkSp")


def _stats(db):
    return sorted(json.dumps({k: v for k, v in r.items() if k != "player"}, sort_keys=True) for r in report(db))


def test_anonymized_logs_leave_every_number_alone(tmp_path):
    names = Pseudonyms()
    out = tmp_path / "out"
    written = [anonymize_file(p, out, names) for p in ALL_LOGS]
    for path in written:
        assert all(re.fullmatch(STAND_IN, n) for n, _ in players_in(path))
        assert "Chris @" not in path.read_text(encoding="utf-8")

    real, fake = connect(tmp_path / "real.sqlite"), connect(tmp_path / "fake.sqlite")
    real_heroes = [import_csv(real, p)["hero_pn_id"] for p in ALL_LOGS]
    fake_heroes = [import_csv(fake, p)["hero_pn_id"] for p in written]
    assert fake_heroes == [names.ids.get(h) if h else None for h in real_heroes]
    assert _stats(real) == _stats(fake)
    assert len(read_csv(written[0])) == len(read_csv(ALL_LOGS[0]))


def test_the_alias_file_goes_through_the_same_stand_ins(tmp_path):
    names = Pseudonyms()
    anonymize_file(HU, tmp_path, names)
    src = tmp_path / "aliases_in.csv"
    src.write_text("pn_id,alias\n5NARaPRkSp,chris\nnotinanylog,ghost\n", encoding="utf-8")
    assert anonymize_aliases(src, tmp_path / "aliases.csv", names) == 1
    text = (tmp_path / "aliases.csv").read_text(encoding="utf-8")
    assert "5NARaPRkSp" not in text and "notinanylog" not in text and "chris" not in text
    assert names.ids["5NARaPRkSp"] in text


def test_an_aliased_id_is_named_for_its_alias_not_its_display_name():
    """One person under a new display name each session stays one stand-in."""
    names = Pseudonyms(aliases={"AAAAAAAAAA": "luis", "BBBBBBBBBB": "luis"})
    a = names.entry('"Luis @ AAAAAAAAAA" calls 20')
    b = names.entry('"lu1s phone @ BBBBBBBBBB" folds')
    c = names.entry('"Guest @ CCCCCCCCCC" checks')
    assert a.startswith('"player 1 @') and b.startswith('"player 1 @') and c.startswith('"player 2 @')
