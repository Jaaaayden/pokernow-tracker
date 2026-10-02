"""`pnt update`: where it installs from, what it runs, and in what order.

pip and Task Scheduler are both replaced by recorders here. A real update -- a
pipx install replaced in place while its own `pnt.exe` ran it -- was checked by
hand on Windows 11.
"""

from __future__ import annotations

import importlib.metadata
import json
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


@pytest.fixture
def updating(monkeypatch):
    """`pnt update` on an installed copy under a Windows service, all faked."""
    calls = []
    monkeypatch.setattr(upd, "checkout_root", lambda: None)
    monkeypatch.setattr(upd, "installed_source", lambda: "from.zip")
    monkeypatch.setattr(upd, "install", lambda source: calls.append(("install", source)))
    monkeypatch.setattr(sys, "platform", "win32")
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
