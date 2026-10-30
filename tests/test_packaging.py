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

import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Read at runtime by conn.py, server/app.py and cli.py respectively.
REQUIRED = [
    "pnt/_commit.txt",
    "pnt/db/schema.sql",
    "pnt/server/static/chart.html",
    "pnt/server/static/stats.html",
    "pnt/server/static/allin.html",
    "pnt/server/static/filter-help.js",
    "pnt/server/static/replay.js",
    "pnt/server/static/loading.js",
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
    """`pnt import --sample` and `pnt setup --sample` fall back to `pnt/logs/` when
    the user's log folder is empty. Left out of the wheel that fallback is dead code everywhere
    except a checkout -- the same shape of bug as the missing `db/schema.sql`,
    and just as invisible to a suite that runs from one.
    """
    shipped = [p for p in wheel_contents if p.startswith("pnt/logs/poker_now_log_")]
    assert shipped, (
        "the bundled sample logs are not in the wheel -- add logs/*.csv to "
        "[tool.setuptools.package-data] in pyproject.toml"
    )
    assert len(shipped) == len(list((ROOT / "pnt" / "logs").glob("poker_now_log_*.csv")))


def test_the_extension_tests_stay_out(wheel_contents):
    """The node tests sit beside the extension's code and nothing reads them at runtime."""
    shipped = sorted(p for p in wheel_contents if p.endswith(".test.mjs"))
    assert not shipped, f"{shipped} ship -- see [tool.setuptools.exclude-package-data]"


#: Raw exports, with the real names and PokerNow IDs of the people who were at the
#: table. The test reads its list from them rather than spelling anyone out here.
_RAW = sorted((ROOT / "tests" / "fixtures").glob("poker_now_log_*.csv"))
_SEAT = re.compile(r'"([^"@]+) @ ([A-Za-z0-9_-]{10})"')

#: Real but not in any fixture: the name every example used to reach for.
_ALSO = {"henry"}

#: Files another branch is clearing of names right now; drop each once it is clean.
_PENDING: set[str] = set()


def _real_strings() -> tuple[set[str], set[str]]:
    """(names, ids) seen in the raw fixtures. Names are the distinctive ones only:
    a seat called "500" or "all in" says nothing about who sat in it."""
    names, ids = set(_ALSO), set()
    for path in _RAW:
        ids.add(path.stem.removeprefix("poker_now_log_"))
        for name, pn_id in _SEAT.findall(path.read_text(encoding="utf-8").replace('""', '"')):
            ids.add(pn_id)
            if name.isalpha() and len(name) >= 5:
                names.add(name.lower())
    return names, ids


def test_no_real_player_ships():
    """Help text, API docs, comments and the extension's tests all ship. The
    anonymized sample in pnt/logs is checked by test_sample_logs.py."""
    names, ids = _real_strings()
    assert "genericpoker" in names, "the fixtures changed; this test reads them"
    word = re.compile(r"(?<![a-z0-9_])(" + "|".join(map(re.escape, sorted(names))) + r")(?![a-z0-9_])")
    hits = []
    for path in sorted((ROOT / "pnt").rglob("*")):
        rel = path.relative_to(ROOT).as_posix()
        if not path.is_file() or rel.startswith("pnt/logs/") or rel in _PENDING:
            continue
        if path.suffix not in {".py", ".js", ".mjs", ".html", ".json", ".md", ".sql", ".txt", ".css"}:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            found = word.findall(line.lower()) + [i for i in ids if i in line]
            hits += [f"{rel}:{n}: {f}" for f in found]
    assert not hits, "real names or IDs ship:\n" + "\n".join(hits)


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
