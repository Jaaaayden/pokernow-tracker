"""What the `pnt` command writes, and where.

Output: redirected on Windows, Python encodes in the ANSI code page and raised on
the first suit. Files: the alias table and redacted logs hold real names, so
neither may land in `pnt/logs/`, which ships inside the package.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from pnt import cli
from pnt.db.conn import connect
from pnt.ingest.importer import import_csv
from tests.conftest import HU, ROOT


def test_a_cp1252_stream_takes_suits_and_emoji():
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    cli._utf8_output(stream)
    stream.write("A♠ K♥ 😀")
    stream.flush()
    assert raw.getvalue().decode("utf-8") == "A♠ K♥ 😀"


def _db_with_a_suited_miss(tmp_path):
    path = tmp_path / "t.sqlite"
    conn = connect(path)
    conn.execute(
        "INSERT INTO parse_misses (game_id, ord, entry, reason) VALUES ('g', 1, ?, 'unknown')",
        ('"x @ aaaaaaaaaa" wins with A♠ K♥ 😀',),
    )
    conn.commit()
    conn.close()
    return path


def test_misses_survive_a_cp1252_stdout(tmp_path):
    """CliRunner's stdout is a strict cp1252 stream here, like a Windows redirect."""
    path = _db_with_a_suited_miss(tmp_path)
    result = CliRunner(charset="cp1252").invoke(cli.app, ["misses", "--db", str(path)])
    assert result.exit_code == 0, result.output
    assert "1 parse miss(es)" in result.output


def test_misses_survive_a_real_redirect(tmp_path):
    """The real thing: a pipe, which on Windows is cp1252 by default."""
    path = _db_with_a_suited_miss(tmp_path)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    run = subprocess.run(
        [sys.executable, "-m", "pnt.cli", "misses", "--db", str(path)],
        capture_output=True,
        cwd=ROOT,
        env=env,
        check=False,
    )
    assert run.returncode == 0, run.stderr.decode("utf-8", "replace")
    assert "A♠ K♥ 😀" in run.stdout.decode("utf-8")


def test_alias_export_defaults_beside_the_config_not_into_the_package(tmp_path):
    db = tmp_path / "t.sqlite"
    conn = connect(db)
    import_csv(conn, HU)
    conn.close()
    bundled = cli.BUNDLED_ALIASES.read_bytes()
    target = cli.USER_ALIASES
    assert target.parent != cli.BUNDLED_LOG_DIR
    try:
        run = CliRunner().invoke(cli.app, ["alias", "export", "--db", str(db)])
        assert run.exit_code == 0, run.output
        assert target.exists()
        run = CliRunner().invoke(cli.app, ["alias", "import", "--db", str(db)])
        assert run.exit_code == 0, run.output
        assert "0 id(s) not in this database yet" in run.output
    finally:
        target.unlink(missing_ok=True)
    assert cli.BUNDLED_ALIASES.read_bytes() == bundled


def test_alias_export_refuses_the_bundled_file(tmp_path):
    bundled = cli.BUNDLED_ALIASES.read_bytes()
    run = CliRunner().invoke(
        cli.app, ["alias", "export", str(cli.BUNDLED_ALIASES), "--db", str(tmp_path / "t.sqlite")]
    )
    assert run.exit_code != 0
    assert cli.BUNDLED_ALIASES.read_bytes() == bundled


def _flat(output: str) -> str:
    return "".join(c for c in output if not c.isspace() and c not in "│╭╮╰╯─")  # rich wraps it


@pytest.mark.parametrize("command", [["sync"], ["import"], ["setup", "--no-service"], ["backfill", "pglx"]])
def test_the_sample_folder_is_refused_as_a_log_folder(tmp_path, command):
    """Read as a log folder, the sample's anonymized copies of real games go on
    record beside the games they copy; the server already refused it."""
    db = tmp_path / "t.sqlite"
    run = CliRunner().invoke(cli.app, [*command, "--log-dir", str(cli.BUNDLED_LOG_DIR), "--db", str(db)])
    assert run.exit_code == 2, run.output
    assert "bundledsample'sfolder" in _flat(run.output)
    assert not db.exists() or connect(db).execute("SELECT COUNT(*) FROM raw_entries").fetchone()[0] == 0


@pytest.mark.parametrize(
    "args",
    [
        ["stats", "--filter", "bogusterm"],
        ["allin", "--filter", "bogusterm"],
        ["alias", "merge", "nobody1", "nobody2"],
        ["positions", "nobody"],
        ["import", "nope.csv"],
        ["redact", "--anonymize", "--aliases", "nope.csv", "--out", "out"],
    ],
)
def test_a_typo_is_a_one_line_error_not_a_traceback(tmp_path, monkeypatch, args):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "in"
    src.mkdir()
    shutil.copy(HU, src / HU.name)
    import_csv(connect(tmp_path / "t.sqlite"), src / HU.name)
    extra = [str(src / HU.name)] if args[0] == "redact" else ["--db", str(tmp_path / "t.sqlite")]
    run = CliRunner().invoke(cli.app, [*args, *extra])
    assert run.exit_code == 2, run.output
    assert run.exception is None or isinstance(run.exception, SystemExit), run.exception
    assert "Traceback" not in run.output


def test_setup_skips_a_log_with_no_hands(tmp_path):
    """As `pnt import` and the sync do: a table joined and left before the first
    deal would only make an empty game."""
    folder = tmp_path / "logs"
    folder.mkdir()
    shutil.copy(HU, folder / HU.name)
    lines = HU.read_text(encoding="utf-8").splitlines()
    empty = folder / "poker_now_log_pglEmptyGame0000000000000.csv"
    empty.write_text("\n".join([lines[0], *[ln for ln in lines[1:] if "starting hand" not in ln][-3:]]) + "\n", encoding="utf-8")
    db = tmp_path / "t.sqlite"
    run = CliRunner().invoke(cli.app, ["setup", "--no-service", "--db", str(db), "--log-dir", str(folder)])
    assert run.exit_code == 0, run.output
    assert "no hands" in run.output
    assert [r[0] for r in connect(db).execute("SELECT game_id FROM games")] == [HU.stem.removeprefix("poker_now_log_")]


def test_redact_keeps_plain_copies_out_of_the_package(tmp_path):
    src = tmp_path / "in" / HU.name
    src.parent.mkdir()
    shutil.copy(HU, src)
    before = sorted(p.name for p in cli.BUNDLED_LOG_DIR.iterdir())
    run = CliRunner().invoke(cli.app, ["redact", str(src), "--out", str(cli.BUNDLED_LOG_DIR)])
    assert run.exit_code != 0
    flat = "".join(c for c in run.output if not c.isspace() and c != "│")  # rich wraps it
    assert "only--anonymizecopiesgothere" in flat
    assert sorted(p.name for p in cli.BUNDLED_LOG_DIR.iterdir()) == before
