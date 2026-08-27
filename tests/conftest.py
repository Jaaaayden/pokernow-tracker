from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures"

HU = FIXTURES / "poker_now_log_pgl41zM3_CKphpnKM1DMIosUT.csv"
MULTIWAY = FIXTURES / "poker_now_log_pgl1UViJ4BhoVP-KKHpux1Mpv.csv"
THREE = FIXTURES / "poker_now_log_pglSdQtyFGypDbrqD5IhXXlYz.csv"
ALL_LOGS = [HU, MULTIWAY, THREE]

HU_GAME = "pgl41zM3_CKphpnKM1DMIosUT"


@pytest.fixture(scope="session")
def all_logs() -> list[Path]:
    return ALL_LOGS


@pytest.fixture(scope="session")
def parsed_all():
    """Every fixture parsed once: {game_id: ParseResult}."""
    from pnt.ingest.csv_source import game_id_from_filename, read_csv
    from pnt.logfmt.parser import parse

    out = {}
    for path in ALL_LOGS:
        gid = game_id_from_filename(path)
        out[gid] = parse(read_csv(path), gid)
    return out


@pytest.fixture()
def db(tmp_path):
    """A fresh database with all three logs imported."""
    from pnt.db.conn import connect
    from pnt.ingest.importer import import_csv

    conn = connect(tmp_path / "t.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    yield conn
    conn.close()
