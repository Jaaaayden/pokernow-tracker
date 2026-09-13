"""What an installed copy must contain.

Every one of these files is read from disk at runtime, and every one of them is
invisible to the test suite as normally run: an editable install points at the
checkout, where they are all present regardless of whether packaging ships them.
`db/schema.sql` was missing from `package-data` and a `pip install` of this
project could not open a database at all -- with 240 tests passing.

So this builds a real wheel and looks inside it.

It builds from a *copy* of the tree with the build artifacts stripped, because
setuptools' `include-package-data` is on by default and will happily pull files in
via a leftover `*.egg-info/SOURCES.txt`. That is what made the original bug so
quiet: a tree that had been built before shipped the file, a fresh clone did not,
and the difference never showed up here. A clean copy is what a friend's `pipx
install git+...` actually starts from.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Read at runtime by conn.py, server/app.py and cli.py respectively.
REQUIRED = [
    "pnt/db/schema.sql",
    "pnt/server/static/chart.html",
    "pnt/server/static/stats.html",
    "pnt/server/static/filter-help.js",
    "pnt/extension/manifest.json",
    "pnt/extension/content.js",
    "pnt/extension/background.js",
    "pnt/extension/normalize.js",
    "pnt/extension/pager.js",
    "pnt/extension/popup.html",
    "pnt/extension/popup.js",
]

#: Anything that could let a previous build leak into this one.
ARTIFACTS = ("*.egg-info", "build", "dist", ".venv", ".git", ".pytest_cache", ".ruff_cache")


@pytest.fixture(scope="module")
def wheel_contents(tmp_path_factory) -> set[str]:
    """Build a wheel from a pristine copy of the tree and list what is in it."""
    work = tmp_path_factory.mktemp("src") / "tree"
    shutil.copytree(
        ROOT,
        work,
        ignore=shutil.ignore_patterns(*ARTIFACTS, "*.sqlite", "*.sqlite-*"),
    )
    out = tmp_path_factory.mktemp("wheel")
    built = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(work), "--no-deps", "--no-cache-dir", "-w", str(out), "-q"],
        capture_output=True,
        text=True,
        check=False,
    )
    wheels = list(out.glob("*.whl"))
    if not wheels:
        pytest.skip(f"could not build a wheel here: {(built.stderr or built.stdout).strip()[:300]}")
    return set(zipfile.ZipFile(wheels[0]).namelist())


@pytest.mark.parametrize("path", REQUIRED)
def test_runtime_file_is_shipped(wheel_contents, path):
    assert path in wheel_contents, (
        f"{path} is read at runtime but is not in the wheel -- add its glob to "
        "[tool.setuptools.package-data] in pyproject.toml"
    )


def test_the_sample_corpus_is_shipped(wheel_contents):
    """`pnt import` and `pnt setup` fall back to `pnt/logs/` when the user's log
    folder is empty. Left out of the wheel that fallback is dead code everywhere
    except a checkout -- the same shape of bug as the missing `db/schema.sql`,
    and just as invisible to a suite that runs from one.
    """
    shipped = [p for p in wheel_contents if p.startswith("pnt/logs/poker_now_log_")]
    assert shipped, (
        "the bundled sample logs are not in the wheel -- add logs/*.csv to "
        "[tool.setuptools.package-data] in pyproject.toml"
    )
    assert len(shipped) == len(list((ROOT / "pnt" / "logs").glob("poker_now_log_*.csv")))


def test_the_console_script_is_declared():
    """`pnt` on PATH is the whole point of a pipx install."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'pnt = "pnt.cli:app"' in text


def test_the_server_is_not_an_optional_extra():
    """`pnt serve`, `pnt service` and the extension all need it.

    It used to sit behind `[server]`, so the documented install line had to carry
    an extra that, forgotten, produced an install where the main feature failed at
    import time.
    """
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    base = text.split("[project.optional-dependencies]")[0]
    for package in ("fastapi", "uvicorn", "pydantic"):
        assert package in base, f"{package} must be a base dependency, not an extra"
