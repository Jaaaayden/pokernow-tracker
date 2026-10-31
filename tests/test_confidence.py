"""Confidence intervals on the pages: every JS copy of `wilson` agrees with
`queries.wilson`, and the endpoints the pages read carry bb/100's interval. See
SPEC.md, "Intervals on the pages"."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from pnt.db.conn import connect
from pnt.ingest.importer import import_csv
from pnt.stats.queries import facts_for, report, wilson
from pnt.stats.results import results_breakdown
from tests.conftest import ALL_LOGS, local_client

ROOT = Path(__file__).parent.parent
#: Every file with its own copy of the page-side helpers.
COPIES = ["pnt/server/static/loading.js", "pnt/server/static/chart.html", "pnt/extension/spot.js"]


def _function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    return src[start:src.index("\n  }\n", start) + 4]


def test_every_copy_of_the_helpers_is_the_same():
    """Three files each carry `wilson` and `ciText` (loading.js for the pages but
    the chart); they must read a rate alike."""
    for name in ("wilson", "ciText"):
        bodies = {path: _function((ROOT / path).read_text(encoding="utf-8"), name) for path in COPIES}
        assert len(set(bodies.values())) == 1, f"{name} differs between {list(bodies)}"


def test_the_js_wilson_is_the_python_one():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    cases = [(k, n) for n in (1, 2, 5, 10, 20, 37, 100, 1000) for k in sorted({0, 1, n // 3, n // 2, n - 1, n})
             if 0 <= k <= n]
    for path in COPIES:
        body = _function((ROOT / path).read_text(encoding="utf-8"), "wilson")
        script = f"{body}\nconsole.log(JSON.stringify({json.dumps(cases)}.map(([k, n]) => wilson(k / n, n))));"
        out = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout)
        for (k, n), got in zip(cases, out, strict=True):
            assert got == pytest.approx(list(wilson(k, n))), (path, k, n)
    # No chances, no interval, the way there is no rate.
    assert wilson(0, 0) is None


def test_results_rows_carry_the_bb_interval(db):
    rows = [r for t in results_breakdown(facts_for(db, "HSJ"))["tables"] for r in t["rows"]]
    assert rows and all("bb_per_100_ci" in r for r in rows)
    total = results_breakdown(facts_for(db, "HSJ"))["total"]
    assert total["bb_per_100_ci"] == next(r for r in report(db) if r["player"] == "HSJ")["bb_per_100_ci"]


def test_stats_endpoint_returns_the_bb_interval(tmp_path, monkeypatch):
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

    rows = client.get("/stats").json()
    assert rows and all("bb_per_100_ci" in r for r in rows)
    big = [r for r in rows if r["hands"] >= 100]
    assert big and all(r["bb_per_100_ci"] > 0 for r in big)
    total = client.get("/players/HSJ/results").json()["total"]
    assert total["bb_per_100_ci"] == next(r for r in rows if r["player"] == "HSJ")["bb_per_100_ci"]
