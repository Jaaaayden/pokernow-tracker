"""Reading an export: the game a file name holds, and rows that are not whole."""

from __future__ import annotations

import logging

import pytest

from pnt.ingest.csv_source import game_id_from_filename, read_csv

HEAD = "entry,at,order\n"
NEWER = '"-- starting hand #5 (id: x) --",2026-10-01T10:00:00.123Z,179084880012300\n'
OLDER = '"line two",2026-10-01T09:00:00.000Z,179084520000000\n'


@pytest.mark.parametrize(
    "name, gid",
    [
        ("poker_now_log_pglAbc_123.csv", "pglAbc_123"),
        ("poker_now_log_pglAbc_123 (1).csv", "pglAbc_123"),  # Chrome, Edge
        ("poker_now_log_pglAbc_123(12).csv", "pglAbc_123"),  # Firefox
        ("poker_now_log_pgldBYgodxANW2_YvaxBEJh-3.csv", "pgldBYgodxANW2_YvaxBEJh-3"),  # a real ID
        ("poker_now_log_not an id.csv", None),
        ("poker_now_log_..csv", None),
        ("poker_now_log_" + "a" * 65 + ".csv", None),
        ("something.csv", None),
    ],
)
def test_game_id_from_filename(name, gid):
    assert game_id_from_filename(name) == gid


def _read(tmp_path, text: str):
    path = tmp_path / "poker_now_log_g.csv"
    path.write_text(text, encoding="utf-8", newline="")
    return [(e.ord, e.entry) for e in read_csv(path)]


def test_a_whole_file_reads_every_row(tmp_path):
    assert _read(tmp_path, HEAD + NEWER + OLDER) == [
        (179084520000000, "line two"),
        (179084880012300, "-- starting hand #5 (id: x) --"),
    ]


def test_a_whole_last_row_without_a_newline_is_kept(tmp_path):
    """A hand-saved file may drop the final newline; its row is still whole."""
    assert len(_read(tmp_path, HEAD + NEWER + OLDER.rstrip("\n"))) == 2


@pytest.mark.parametrize("keep", [1, 7, 14])
def test_a_file_cut_inside_order_drops_that_row(tmp_path, keep):
    """`179084520000000` cut to `1790845` still parses, and would be stored for good."""
    cut = HEAD + NEWER + OLDER[: OLDER.rindex(",") + 1 + keep]
    assert _read(tmp_path, cut) == [(179084880012300, "-- starting hand #5 (id: x) --")]


@pytest.mark.parametrize(
    "tail",
    [
        '"line t',  # inside the quoted entry
        '"line two",2026-10-01T09:0',  # inside `at`
        '"line two",2026-10-01T09:00:00.000Z,',  # before `order`
    ],
)
def test_a_short_row_is_skipped_and_logged(tmp_path, caplog, tail):
    with caplog.at_level(logging.WARNING, logger="pnt.ingest.csv_source"):
        assert _read(tmp_path, HEAD + NEWER + tail) == [(179084880012300, "-- starting hand #5 (id: x) --")]
    assert "skipped 1 malformed row" in caplog.text


def test_a_bad_row_in_the_middle_is_skipped(tmp_path):
    bad = '"hand edit",2026-10-01T09:30:00.000Z,not a number\n'
    assert [o for o, _ in _read(tmp_path, HEAD + NEWER + bad + OLDER)] == [179084520000000, 179084880012300]


def test_missing_columns_are_still_an_error(tmp_path):
    with pytest.raises(ValueError, match="missing required column"):
        _read(tmp_path, "not,a,log\n1,2,3\n")
