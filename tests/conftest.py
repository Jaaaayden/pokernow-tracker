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

#: The core corpus. Hand counts and the hand-worked stat totals are pinned to
#: exactly these three logs, so nothing may be added here without redoing that
#: manual count by hand.
ALL_LOGS = [HU, MULTIWAY, THREE]

STRADDLE = FIXTURES / "poker_now_log_pgldBYgodxANW2_YvaxBEJh-3.csv"
MISSED_BLINDS = FIXTURES / "poker_now_log_pgl7sRNQr64BIPFwmlFel-Le5.csv"
TRUNCATED = FIXTURES / "poker_now_log_pglkWn5b4Y8whHqWY3tVmrtW1.csv"

#: Logs carrying the cases the core three never exercise. Each one failed chip
#: conservation before the cumulative-post rule covered live forced posts.
EDGE_LOGS = [STRADDLE, MISSED_BLINDS, TRUNCATED]

HU_GAME = "pgl41zM3_CKphpnKM1DMIosUT"
#: Carries the two hands SPEC.md flags: #25 has a dead small blind
#: (`blinds_irregular`) and #26 a dead button.
MULTIWAY_GAME = "pgl1UViJ4BhoVP-KKHpux1Mpv"
STRADDLE_GAME = "pgldBYgodxANW2_YvaxBEJh-3"
MISSED_BLINDS_GAME = "pgl7sRNQr64BIPFwmlFel-Le5"
TRUNCATED_GAME = "pglkWn5b4Y8whHqWY3tVmrtW1"


@pytest.fixture(scope="session")
def all_logs() -> list[Path]:
    return ALL_LOGS


def _parse_logs(paths):
    from pnt.ingest.csv_source import game_id_from_filename, read_csv
    from pnt.logfmt.parser import parse

    out = {}
    for path in paths:
        gid = game_id_from_filename(path)
        out[gid] = parse(read_csv(path), gid)
    return out


@pytest.fixture(scope="session")
def parsed_all():
    """The core corpus parsed once: {game_id: ParseResult}."""
    return _parse_logs(ALL_LOGS)


@pytest.fixture(scope="session")
def parsed_edge():
    """The edge-case logs parsed once: {game_id: ParseResult}."""
    return _parse_logs(EDGE_LOGS)


@pytest.fixture(scope="session")
def parsed_every(parsed_all, parsed_edge):
    """Every fixture log. Invariants that must hold everywhere use this."""
    return {**parsed_all, **parsed_edge}


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
