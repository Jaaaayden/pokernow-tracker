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
