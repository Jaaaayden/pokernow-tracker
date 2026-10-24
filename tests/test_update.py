"""`pnt update`: where it installs from, what it runs, and in what order.

pip and Task Scheduler are both replaced by recorders here. A real update -- a
pipx install replaced in place while its own `pnt.exe` ran it -- was checked by
hand on Windows 11.
"""

from __future__ import annotations

import importlib.metadata
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pnt import service as svc
from pnt import update as upd
from pnt.cli import app

# ----------------------------------------------------------- extension build ---


def _extension(tmp_path: Path) -> Path:
    folder = tmp_path / "extension"
    (folder / "icons").mkdir(parents=True)
    (folder / "manifest.json").write_text("{}", encoding="utf-8")
    (folder / "content.js").write_text("one", encoding="utf-8")
    (folder / "icons" / "icon16.png").write_bytes(b"\x89PNG")
    return folder


def test_the_build_follows_the_files_chrome_loads(tmp_path):
    folder = _extension(tmp_path)
    before = upd.extension_build(folder)
    assert before == upd.extension_build(folder)
    (folder / "content.js").write_text("two", encoding="utf-8")
    assert upd.extension_build(folder) != before


def test_the_build_ignores_the_tests_beside_the_scripts(tmp_path):
    """Chrome never loads them; a change to one must not reload anybody's extension."""
    folder = _extension(tmp_path)
    before = upd.extension_build(folder)
    (folder / "content.test.mjs").write_text("test", encoding="utf-8")
    assert upd.extension_build(folder) == before


def test_the_build_covers_the_shipped_extension():
    assert len(upd.extension_build()) == 12


def _background() -> str:
    return (upd.EXTENSION_DIR / "background.js").read_text(encoding="utf-8")


def test_the_extension_hashes_every_file_the_server_does():
    """background.js lists its files by hand: it cannot read its own folder."""
    src = _background()
    listed = src[src.index("const FILES = [") : src.index("];", src.index("const FILES = ["))]
    assert sorted(re.findall(r'"([^"]+)"', listed)) == sorted(upd.extension_files())


def test_the_extension_hashes_its_files_as_the_server_does():
    """Else every extension would see a stranger's hash and reload once a minute."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    src = _background()
    body = src[src.index("const FILES = [") : src.index("let hashing")]
    folder = json.dumps(str(upd.EXTENSION_DIR))
    script = (
        f"{body}\nconst fs = require('fs'), path = require('path');"
        f"buildOf(FILES, async (rel) => fs.readFileSync(path.join({folder}, rel))).then(console.log);"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout
    assert out.strip() == upd.extension_build()


# ------------------------------------------------------------------- source ---


class _Dist:
    def __init__(self, direct_url: dict | None):
        self.raw = None if direct_url is None else json.dumps(direct_url)

    def read_text(self, name):
        assert name == "direct_url.json"
        return self.raw


def _installed(monkeypatch, direct_url):
    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: _Dist(direct_url))


def test_source_is_the_archive_it_came_from(monkeypatch):
    _installed(monkeypatch, {"url": upd.DEFAULT_SOURCE, "archive_info": {}})
    assert upd.installed_source() == upd.DEFAULT_SOURCE


def test_source_follows_a_git_install_back_to_its_branch(monkeypatch):
    """Not the commit: reinstalling what is installed now would update nothing."""
    _installed(
        monkeypatch,
        {
            "url": "https://github.com/Jaaaayden/pokernow-tracker",
            "vcs_info": {"vcs": "git", "commit_id": "abc123", "requested_revision": "main"},
        },
    )
    assert upd.installed_source() == "git+https://github.com/Jaaaayden/pokernow-tracker@main"


def test_source_is_unknown_without_a_record(monkeypatch):
    _installed(monkeypatch, None)
    assert upd.installed_source() is None

    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "distribution", missing)
    assert upd.installed_source() is None


def test_a_checkout_is_told_apart_from_an_installed_copy(tmp_path):
    (tmp_path / "pnt").mkdir()
    assert upd.checkout_root(tmp_path / "pnt") is None
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    assert upd.checkout_root(tmp_path / "pnt") == tmp_path


def test_install_replaces_the_package_and_adds_only_missing_dependencies(monkeypatch):
    calls = []
    monkeypatch.setattr(upd, "requirements", lambda: ["typer>=0.12", "fastapi>=0.110"])
    upd.install("x.zip", pip=lambda *a: calls.append(a))
    assert calls == [
        ("install", "--quiet", "--force-reinstall", "--no-deps", "--no-cache-dir", "x.zip"),
        ("install", "--quiet", "typer>=0.12", "fastapi>=0.110"),
    ]


def test_requirements_leave_out_the_dev_extra():
    reqs = upd.requirements()
    assert any(r.startswith("fastapi") for r in reqs)
    assert not any("extra ==" in r for r in reqs)


# --------------------------------------------------------------------- CLI ---


@pytest.fixture(params=["win32", "darwin"])
def updating(monkeypatch, request):
    """`pnt update` on an installed copy under `pnt service`, all faked, on each
    platform it covers: the two must update alike."""
    calls = []
    monkeypatch.setattr(upd, "checkout_root", lambda: None)
    monkeypatch.setattr(upd, "installed_source", lambda: "from.zip")
    monkeypatch.setattr(upd, "install", lambda source: calls.append(("install", source)))
    monkeypatch.setattr(sys, "platform", request.param)
    monkeypatch.setattr(svc, "task_state", lambda name=svc.TASK_NAME: "Running")
    monkeypatch.setattr(svc, "stop_and_wait", lambda: calls.append("stop"))
    monkeypatch.setattr(svc, "start", lambda: calls.append("start"))

    def run(args, check):
        calls.append(tuple(args[1:]))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_update_stops_installs_then_restarts_on_the_new_code(updating):
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    assert updating == [
        "stop",
        ("install", "from.zip"),
        # The restart, on the new code, rebuilds any stale hands while the server is down.
        ("-m", "pnt.cli", "service", "restart", "--host", "127.0.0.1", "--port", "52000"),
    ]


def test_update_starts_the_old_server_again_when_pip_fails(updating, monkeypatch):
    def fail(source):
        raise upd.UpdateError("pip failed (exit code 1); see its output above")

    monkeypatch.setattr(upd, "install", fail)
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 1
    assert updating == ["stop", "start"]
    assert "pip failed" in result.output


def test_update_falls_back_to_the_main_archive(updating, monkeypatch):
    monkeypatch.setattr(upd, "installed_source", lambda: None)
    assert CliRunner().invoke(app, ["update"]).exit_code == 0
    assert ("install", upd.DEFAULT_SOURCE) in updating


def test_update_refuses_to_overwrite_a_checkout(updating, monkeypatch, tmp_path):
    monkeypatch.setattr(upd, "checkout_root", lambda: tmp_path)
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 1
    assert "git pull" in result.output
    assert updating == []


@pytest.fixture
def updating_unmanaged(updating, monkeypatch):
    """The same with no service installed: nothing to stop or restart."""
    monkeypatch.setattr(svc, "task_state", lambda name=svc.TASK_NAME: None)
    return updating


def test_update_without_a_service_installs_only_and_names_the_old_server(updating_unmanaged, monkeypatch):
    monkeypatch.setattr(svc, "health", lambda host, port: {"pid": 4242})
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    assert updating_unmanaged == [("install", "from.zip"), ("-m", "pnt.cli", "rebuild", "--if-stale")]
    assert "pid 4242" in result.output
    assert "still runs the old code" in result.output
    assert "restart above" not in result.output


def test_update_without_a_service_says_to_start_the_server(updating_unmanaged, monkeypatch):
    monkeypatch.setattr(svc, "health", lambda host, port: None)
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    assert "Start the server with `pnt serve`" in result.output


def test_update_on_linux_never_asks_for_a_service(updating, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(svc, "task_state", lambda name=svc.TASK_NAME: pytest.fail("no service here"))
    monkeypatch.setattr(svc, "health", lambda host, port: None)
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    assert updating == [("install", "from.zip"), ("-m", "pnt.cli", "rebuild", "--if-stale")]


def test_a_checkout_on_linux_is_told_to_restart_pnt_serve(updating, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(upd, "checkout_root", lambda: tmp_path)
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 1
    assert "restart `pnt serve`" in result.output


# ------------------------------------------------------------ update banner ---

SHA = "0123456789abcdef0123456789abcdef01234567"


def test_an_archive_install_knows_its_commit(tmp_path):
    f = tmp_path / "_commit.txt"
    f.write_text(SHA + "\n", encoding="utf-8")
    assert upd.installed_commit(f) == SHA


def test_a_checkout_or_a_missing_file_knows_no_commit(tmp_path):
    f = tmp_path / "_commit.txt"
    assert upd.installed_commit(f) is None
    f.write_text("$Format:%H$\n", encoding="utf-8")
    assert upd.installed_commit(f) is None


def test_github_archives_fill_in_the_commit():
    """The .gitattributes line that makes main.zip carry its commit."""
    if shutil.which("git") is None:
        pytest.skip("no git here")
    root = upd.PACKAGE_DIR.parent
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    if head.returncode != 0:
        pytest.skip("not a git checkout")
    if subprocess.run(["git", "-C", str(root), "cat-file", "-e", "HEAD:pnt/_commit.txt"], check=False).returncode:
        pytest.skip("pnt/_commit.txt is not committed yet")
    # The working tree's attributes, so an uncommitted change to them counts.
    out = subprocess.run(
        ["git", "-C", str(root), "archive", "--worktree-attributes", "--format=tar", "HEAD", "pnt/_commit.txt"],
        capture_output=True, check=True,
    ).stdout
    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(out)) as tar:
        text = tar.extractfile("pnt/_commit.txt").read().decode().strip()
    assert text == head.stdout.strip()


@pytest.mark.parametrize(
    ("source", "branch"),
    [
        (upd.DEFAULT_SOURCE, "main"),
        ("https://github.com/Jaaaayden/pokernow-tracker/archive/refs/heads/v1.zip", "v1"),
        ("C:/somewhere/else", "main"),
        (None, "main"),
    ],
)
def test_the_check_follows_the_branch_installed_from(source, branch):
    assert upd.source_branch(source) == branch


def test_commits_behind_reads_githubs_compare():
    asked = []

    def fetch(url):
        asked.append(url)
        return {"status": "ahead", "ahead_by": 4, "behind_by": 0}

    assert upd.commits_behind(SHA, "main", fetch=fetch) == 4
    assert asked == [f"https://api.github.com/repos/{upd.REPO}/compare/{SHA}...main"]
    assert upd.commits_behind(SHA, "main", fetch=lambda url: {"status": "identical", "ahead_by": 0}) == 0
    # The install ahead of the branch (a developer's own build) is no update.
    assert upd.commits_behind(SHA, "main", fetch=lambda url: {"status": "behind", "ahead_by": 0}) == 0


def test_commits_behind_is_none_when_github_cannot_say():
    def offline(url):
        raise OSError("offline")

    assert upd.commits_behind(SHA, "main", fetch=offline) is None


def test_rebuild_if_stale_rebuilds_only_what_needs_it(tmp_path):
    """What `pnt update` runs: nothing on a current database, one summary line and
    every hand re-read on one stored before the game modes were."""
    from pnt.db.conn import connect
    from pnt.ingest.importer import import_csv
    from tests.conftest import ALL_LOGS

    db = tmp_path / "t.sqlite"
    conn = connect(db)
    import_csv(conn, ALL_LOGS[0])
    conn.close()
    current = CliRunner().invoke(app, ["rebuild", "--if-stale", "--db", str(db)])
    assert current.exit_code == 0 and current.output == "", current.output

    conn = connect(db)
    conn.execute("UPDATE hands SET variant = NULL")  # as stored before the column existed
    conn.commit()
    conn.close()
    stale = CliRunner().invoke(app, ["rebuild", "--if-stale", "--db", str(db)])
    assert stale.exit_code == 0, stale.output
    lines = stale.output.strip().splitlines()
    assert lines[0].startswith("rebuilding the stored hands") and "PLO" in lines[0]
    assert lines[-1].startswith("rebuilt ") and len(lines) == 2
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM hands WHERE variant IS NULL").fetchone()[0] == 0
    conn.close()


def test_rebuild_if_stale_reparses_games_an_older_parser_stored(tmp_path):
    """A parser change leaves no trace in the data's shape, so each game carries
    the parser version that stored it, and an older one is re-parsed."""
    from pnt.db.conn import connect
    from pnt.ingest.importer import import_csv, needs_parse_rebuild
    from pnt.logfmt.parser import PARSE_CHANGE, PARSE_VERSION
    from tests.conftest import ALL_LOGS

    db = tmp_path / "t.sqlite"
    conn = connect(db)
    assert not needs_parse_rebuild(conn), "an empty database has nothing to rebuild"
    import_csv(conn, ALL_LOGS[0])
    assert not needs_parse_rebuild(conn)
    conn.execute("UPDATE games SET parsed_with = 0")  # as stored before the column existed
    conn.commit()
    conn.close()
    stale = CliRunner().invoke(app, ["rebuild", "--if-stale", "--db", str(db)])
    assert stale.exit_code == 0, stale.output
    assert PARSE_CHANGE in stale.output.splitlines()[0]
    conn = connect(db)
    assert {r[0] for r in conn.execute("SELECT parsed_with FROM games")} == {PARSE_VERSION}
    conn.close()


def test_service_restart_rebuilds_stale_hands_while_the_server_is_down(tmp_path, monkeypatch):
    """`pnt update` runs the restart on the new code, so a first update to a version
    that reads hands differently needs nothing more than the update itself."""
    from pnt import local_config
    from pnt.db.conn import connect
    from pnt.ingest.importer import import_csv
    from tests.conftest import ALL_LOGS

    db = tmp_path / "t.sqlite"
    conn = connect(db)
    import_csv(conn, ALL_LOGS[0])
    conn.execute("UPDATE hands SET variant = NULL")  # as stored before the column existed
    conn.commit()
    conn.close()
    monkeypatch.setattr(local_config, "database", lambda default: db)
    order = []

    def restart(*, host, port, while_stopped):
        order.append("stopped")
        while_stopped()
        order.append("started")
        return {"pid": 1, "hands": 0, "db": str(db)}

    monkeypatch.setattr(svc, "restart", restart)
    result = CliRunner().invoke(app, ["service", "restart"])
    assert result.exit_code == 0, result.output
    assert order == ["stopped", "started"]
    assert "rebuilt " in result.output and "Run `pnt rebuild`" not in result.output
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM hands WHERE variant IS NULL").fetchone()[0] == 0
    conn.close()
