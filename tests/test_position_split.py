"""The stats page's position split and its "vs fold" column. See SPEC.md, "bb/100
against folding"."""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from pnt.db.conn import connect
from pnt.ingest.importer import import_csv
from pnt.stats.filters import POSITIONS, parse_filter
from pnt.stats.results import SEAT_ORDER
from tests.conftest import ALL_LOGS, local_client
from tests.test_multiway import STATS_HTML, _splits


def test_the_position_split_is_every_seat_name_in_seat_order():
    """One row per name `position_name` gives, in table order, each a working term."""
    groups = _splits()["position"]
    assert [label for label, _ in groups] == list(SEAT_ORDER)
    assert set(SEAT_ORDER) == set(POSITIONS)
    for label, term in groups:
        assert term == f"position={label}"
        parse_filter(term)
        parse_filter(f"srp,{term}")


def test_the_vs_fold_column_follows_the_bb_interval():
    page = STATS_HTML.read_text(encoding="utf-8")
    base = page[page.index("const BASE = ["):page.index("];", page.index("const BASE = ["))]
    keys = re.findall(r'^\s*\["(\w+)", "([^"]+)"', base, re.MULTILINE)
    at = keys.index(("bb_per_100_ci", "±"))
    assert keys[at + 1] == ("bb_per_100_vs_fold", "vs fold")
    assert 'SPLIT_NAMES.position = "position"' in page


def test_stats_by_position_returns_vs_fold(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import importlib

    monkeypatch.setenv("PNT_DB", str(tmp_path / "api.sqlite"))
    from pnt.server import app as app_module

    importlib.reload(app_module)
    conn = connect(tmp_path / "api.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    conn.close()
    client = local_client(app_module.app)

    rows = client.get("/stats", params={"filter": "position=BB", "min_hands": 1}).json()
    assert rows and all("bb_per_100_vs_fold" in r and "bb_per_100_vs_fold_ci" in r for r in rows)
    # Folding the big blind every hand is −100 raw; the column puts that back.
    big = [r for r in rows if r["hands"] >= 30 and r["bb_per_100"] is not None]
    assert big and all(r["bb_per_100_vs_fold"] > r["bb_per_100"] for r in big)


def test_the_page_script_parses(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    page = STATS_HTML.read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", page, re.DOTALL)
    assert scripts
    for i, body in enumerate(scripts):
        path = tmp_path / f"stats_{i}.js"
        path.write_text(body, encoding="utf-8")
        subprocess.run([node, "--check", str(path)], capture_output=True, text=True, check=True)
