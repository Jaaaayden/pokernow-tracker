"""The date terms: `since:`, `before:` and `last:Nd` (SPEC.md, "Dates").

A day is the machine's local calendar day, so the boundaries here are built from
local midnight and turned into the parser's UTC timestamps, which keeps every
test true in any timezone the suite runs in.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from pnt.stats import filters
from pnt.stats.derive import Facts, derive
from pnt.stats.estimate import resolve_node
from pnt.stats.filters import date_key, parse_filter
from pnt.stats.queries import load_hands
from tests.conftest import ALL_LOGS, local_client

#: The fixture logs' three games, by first hand (UTC): 07-23 05:59, 08-01 00:39,
#: 08-10 07:22. Each sits a week or more from the next, so a cut on 07-28 or 08-05
#: falls between two games in every timezone.
LATE_GAME = "pgl41zM3_CKphpnKM1DMIosUT"


def _stamp(at: datetime) -> str:
    """`at`, a local time, as the parser writes `hands.ts`."""
    u = at.astimezone(UTC)
    return f"{u.strftime('%Y-%m-%dT%H:%M:%S')}.{u.microsecond // 1000:03d}Z"


def _row(ts: str | None) -> Facts:
    return Facts(hand_id=1, pn_id="p", n_dealt_in=6, seats_from_button=0, dead_button=False, ts=ts)


MIDNIGHT = datetime.combine(date(2026, 10, 20), time())
JUST_BEFORE = _row(_stamp(MIDNIGHT - timedelta(milliseconds=1)))
AT_MIDNIGHT = _row(_stamp(MIDNIGHT))
LATER = _row(_stamp(MIDNIGHT + timedelta(hours=23, minutes=59)))
UNTIMED = _row(None)


def test_since_and_before_split_at_local_midnight():
    since, before = parse_filter("since:2026-10-20"), parse_filter("before:2026-10-20")
    assert [since(f) for f in (JUST_BEFORE, AT_MIDNIGHT, LATER)] == [False, True, True]
    assert [before(f) for f in (JUST_BEFORE, AT_MIDNIGHT, LATER)] == [True, False, False]
    # A hand with no timestamp is on no side of any day.
    assert not since(UNTIMED) and not before(UNTIMED)


def test_since_and_before_make_a_range_and_combine_with_any_term():
    one_day = parse_filter("since:2026-10-20,before:2026-10-21")
    assert [one_day(f) for f in (JUST_BEFORE, AT_MIDNIGHT, LATER)] == [False, True, True]
    assert not one_day(_row(_stamp(MIDNIGHT + timedelta(days=1))))
    assert not parse_filter("since:2026-10-20,vpip")(LATER)  # the row never put money in
    assert parse_filter("since:2026-10-20|vpip")(LATER)
    assert parse_filter("!(since:2026-10-20)")(JUST_BEFORE)
    assert parse_filter("position=BTN|since:2026-10-20")(LATER)


def test_last_counts_back_whole_days_from_today(monkeypatch):
    monkeypatch.setattr(filters, "_today", lambda: date(2026, 10, 26))
    week = parse_filter("last:7d")  # the 20th to the 26th
    assert [week(f) for f in (JUST_BEFORE, AT_MIDNIGHT, LATER)] == [False, True, True]
    assert not parse_filter("last:6d")(LATER)
    monkeypatch.setattr(filters, "_today", lambda: date(2026, 10, 20))
    assert parse_filter("last:1d")(AT_MIDNIGHT) and not parse_filter("last:1d")(JUST_BEFORE)
    assert parse_filter("last:1D")(AT_MIDNIGHT)


def test_a_second_bound_of_one_side_is_the_tighter_one():
    """Two `since:` are an AND, so the later date is the bound; the `?` panel drops
    the earlier one as it is typed (filter-help.js, `conflicts`)."""
    both = parse_filter("since:2026-10-01,since:2026-10-20")
    assert both(AT_MIDNIGHT) and not both(JUST_BEFORE)
    assert not parse_filter("before:2026-10-21,before:2026-10-20")(AT_MIDNIGHT)


@pytest.mark.parametrize(
    ("expr", "says"),
    [
        ("since:2026-13-40", "bad date '2026-13-40' for since"),
        ("before:2026-02-30", "bad date '2026-02-30' for before"),
        ("since:20261020", "use YYYY-MM-DD"),
        ("since:", "bad date '' for since"),
        ("last:abc", "bad span 'abc' for last"),
        ("last:0d", "bad span '0d'"),
        ("last:7", "a number of days, e.g. last:7d"),
        ("since:2026-10-01|2026-10-05", "unknown filter term"),
        ("!since:2026-10-01", "only a flag can be negated"),
    ],
)
def test_a_malformed_date_term_raises_like_any_bad_term(expr, says):
    with pytest.raises(ValueError, match=says):
        parse_filter(expr)


def test_the_fixture_games_split_by_date(db):
    facts = [f for h in load_hands(db) for f in derive(h)]
    late = [f for f in facts if f.game_id == LATE_GAME]
    assert late and len(late) < len(facts)
    assert [f for f in facts if parse_filter("since:2026-08-05")(f)] == late
    assert len([f for f in facts if parse_filter("before:2026-08-05")(f)]) == len(facts) - len(late)
    assert not [f for f in facts if parse_filter("before:2026-07-01")(f)]


def test_a_date_term_is_part_of_an_estimates_spot():
    node = resolve_node("last:30d,position=BTN,unopened=raise")
    assert "last:30d" in node.context and not node.ignored


def test_only_last_needs_the_date_in_a_cache_key(monkeypatch):
    monkeypatch.setattr(filters, "_today", lambda: date(2026, 10, 20))
    assert date_key("last:7d") == "2026-10-20"
    assert date_key("vpip,(last:7d|pfr)") == "2026-10-20"
    assert date_key("since:2026-10-01,before:2026-10-20") is None
    assert date_key(None) is None and date_key("vpip") is None
    monkeypatch.setattr(filters, "_today", lambda: date(2026, 10, 21))
    assert date_key("last:7d") == "2026-10-21"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PNT_DB", str(tmp_path / "api.sqlite"))
    import importlib

    from pnt.db.conn import connect
    from pnt.ingest.importer import import_csv
    from pnt.server import app as app_module

    importlib.reload(app_module)
    conn = connect(tmp_path / "api.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    conn.close()
    return local_client(app_module.app)


def test_last_is_not_served_from_yesterdays_cache(client, monkeypatch):
    """The same `last:3d` text, asked on two days, is two answers: the stats cache
    is keyed by filter text, and a key without the date would replay the first."""
    def hands(spot: str = "last:3d") -> int:
        return sum(r["hands"] for r in client.get("/stats", params={"filter": spot}).json())

    monkeypatch.setattr(filters, "_today", lambda: date(2026, 8, 11))  # the 9th to the 11th
    recent = hands()
    assert recent == hands("since:2026-08-05") > 0
    monkeypatch.setattr(filters, "_today", lambda: date(2026, 9, 30))
    assert hands() == 0
    monkeypatch.setattr(filters, "_today", lambda: date(2026, 8, 11))
    assert hands() == recent


def test_the_server_refuses_a_bad_date(client):
    r = client.get("/stats", params={"filter": "since:2026-13-40"})
    assert r.status_code == 400 and "YYYY-MM-DD" in r.json()["detail"]
