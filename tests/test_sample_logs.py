"""The bundled corpus: what ships, and when it is reached.

`pnt/logs/` is the only data this project distributes, and it is distributed to
people who did not play those hands. Two things therefore have to hold, and both
are asserted here rather than reviewed: what is in there leaks nothing, and it is
never substituted for logs the user actually asked for.
"""

from __future__ import annotations

import pytest
import typer

from pnt.cli import BUNDLED_LOG_DIR, LOG_GLOB, _expand, _logs_in
from pnt.ingest.csv_source import game_id_from_filename, read_csv
from pnt.logfmt import redact as rd
from pnt.logfmt.parser import parse

BUNDLED = sorted(BUNDLED_LOG_DIR.glob(LOG_GLOB))


def test_the_corpus_is_there():
    assert BUNDLED, f"nothing matching {LOG_GLOB} in {BUNDLED_LOG_DIR}"


@pytest.mark.parametrize("path", BUNDLED, ids=lambda p: p.stem[-6:])
def test_nothing_shipped_names_an_unshown_holding(path):
    """The guard on the folder, not on the tool that fills it. A raw export dropped
    in here would publish one player's whole range on the next release."""
    assert rd.audit(path) == []


@pytest.mark.parametrize("path", BUNDLED, ids=lambda p: p.stem[-6:])
def test_everything_shipped_parses_clean(path):
    """Redaction removes entries; it must not have broken any of the ones it kept."""
    res = parse(read_csv(path), game_id_from_filename(path))
    assert res.hands
    assert not res.misses


def test_an_empty_log_folder_falls_back_to_the_corpus(tmp_path):
    assert _expand(None, tmp_path, sample=True) == _logs_in(BUNDLED_LOG_DIR)


def test_the_fallback_is_opt_in(tmp_path):
    with pytest.raises(typer.BadParameter):
        _expand(None, tmp_path, sample=False)


def test_a_fresh_install_starts_empty(tmp_path):
    """`pnt setup` without `--sample` loads no one else's hands, even with an empty
    log folder: the sample is for trying the tracker, not a default history."""
    from typer.testing import CliRunner

    from pnt.cli import app
    from pnt.db.conn import connect

    db = tmp_path / "fresh.sqlite"
    empty = tmp_path / "logs"
    empty.mkdir()
    result = CliRunner().invoke(app, ["setup", "--no-service", "--db", str(db), "--log-dir", str(empty)])
    assert result.exit_code == 0, result.output
    assert connect(db).execute("SELECT COUNT(*) FROM hands").fetchone()[0] == 0
    assert "--sample" in result.output


def test_the_fallback_is_off_by_default(tmp_path):
    """Only the two commands that fill a database pass sample=True."""
    with pytest.raises(typer.BadParameter):
        _expand(None, tmp_path)


def test_a_folder_with_logs_is_never_substituted(tmp_path):
    """The fallback triggers on *empty*, not on *small*. Getting 20 strangers'
    sessions back because your own folder held one log would be a silent corruption
    of every stat in the database."""
    mine = tmp_path / BUNDLED[0].name
    mine.write_bytes(BUNDLED[0].read_bytes())
    assert _expand(None, tmp_path, sample=True) == [str(mine)]


def test_explicit_paths_never_reach_the_fallback(tmp_path):
    """Naming a folder and silently getting a different one is worse than an error."""
    with pytest.raises(typer.BadParameter):
        _expand([str(tmp_path)], tmp_path, sample=True)


@pytest.mark.parametrize("path", BUNDLED, ids=lambda p: p.stem[-6:])
def test_every_player_shipped_is_a_stand_in(path):
    """The people in these logs did not agree to be in a download: every one is
    replaced (pnt/logfmt/anonymize.py). A log copied in by hand fails here."""
    import re

    from pnt.logfmt.anonymize import STAND_IN, players_in

    real = [name for name, _ in players_in(path) if not re.fullmatch(STAND_IN, name)]
    assert real == [], f"{path.name} names players who are not stand-ins"


def test_the_alias_file_names_only_ids_the_logs_have():
    """An ID in aliases.csv that no log mentions was never run through the stand-ins."""
    import csv

    from pnt.logfmt.anonymize import players_in

    ids = {i for p in BUNDLED for _, i in players_in(p)}
    with (BUNDLED_LOG_DIR / "aliases.csv").open(encoding="utf-8", newline="") as fh:
        listed = {r["pn_id"] for r in csv.DictReader(fh)}
    assert listed <= ids
