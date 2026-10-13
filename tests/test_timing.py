"""Timing tells: when each action was logged, how long each first decision took,
and the tempo bucket it falls in. See SPEC.md, "Timing tells".
"""

from __future__ import annotations

import sqlite3

import pytest

from pnt.db.conn import connect
from pnt.ingest.csv_source import RawEntry, read_csv
from pnt.ingest.importer import import_csv
from pnt.logfmt.parser import parse
from pnt.stats.derive import TEMPO_BUCKETS, TEMPO_CUTS, derive, tempo_of
from pnt.stats.filters import parse_filter
from pnt.stats.queries import load_hands
from tests.conftest import ALL_LOGS, EDGE_LOGS, ROOT

GAME = "pgltest_timing"
#: The one bundled log whose table changed its decision clock (20 s to 10 s).
CLOCK_LOG = ROOT / "pnt" / "logs" / "poker_now_log_pglSP7RQmNX3BBkEAr9HPCGLW.csv"


def ms(t: float) -> int:
    """An `ord` for a line logged `t` seconds into the test hand."""
    return (1_800_000_000_000 + round(t * 1000)) * 100


#: One heads-up hand, with the seconds each line was logged at. A seat request
#: lands mid-street: chat, which must never restart the clock.
HAND = [
    (0.0, '-- starting hand #1 (id: tttt) No Limit Texas Hold\'em (dealer: "Aa @ AAA") --'),
    (0.0, 'Player stacks: #1 "Aa @ AAA" (1000) | #2 "Bb @ BBB" (1000)'),
    (0.0, '"Aa @ AAA" posts a small blind of 10'),
    (0.0, '"Bb @ BBB" posts a big blind of 20'),
    (3.0, '"Aa @ AAA" calls 20'),  # 3 s after the last blind: normal
    (3.9, '"Bb @ BBB" checks'),  # 0.9 s after an action: pre-selected
    (4.8, "Flop:  [7♦, 4♠, 9♦]"),
    (5.5, '"Bb @ BBB" checks'),  # 0.7 s after the board: snap, never pre
    (6.0, 'The player "Cc @ CCC" requested a seat.'),
    (12.5, '"Aa @ AAA" bets 40'),  # 7 s after the check, not 6.5 after the chat: tank
    (14.0, '"Bb @ BBB" calls 40'),
    (15.0, "Turn:  [7♦, 4♠, 9♦, 2♣]"),
    (16.0, '"Bb @ BBB" checks'),
    (46.5, '"Aa @ AAA" checks'),  # 30.5 s: the 20 s + 10 s clock ran out
    (47.4, "River:  [7♦, 4♠, 9♦, 2♣, K♥]"),
    (49.0, '"Bb @ BBB" bets 100'),  # 1.6 s: a snap bet
    (49.9, '"Aa @ AAA" folds'),  # 0.9 s: pre-selected fold
    (50.0, 'Uncalled bet of 100 returned to "Bb @ BBB"'),
    (50.0, '"Bb @ BBB" collected 120 from pot'),
    (50.0, "-- ending hand #1 --"),
]


def entries(lines=HAND) -> list[RawEntry]:
    # Lines logged in the same millisecond keep their order through the seq digits.
    return [RawEntry(ord=ms(t) + i, at="2026-10-01T00:00:00.000Z", entry=e) for i, (t, e) in enumerate(lines)]


def facts_of(lines=HAND) -> dict[str, object]:
    conn = connect(":memory:")
    from pnt.ingest.importer import ingest_entries, rebuild_game

    ingest_entries(conn, GAME, entries(lines), source="test")
    rebuild_game(conn, GAME)
    (hand,) = load_hands(conn)
    return {f.pn_id: f for f in derive(hand)}


# ----------------------------------------------------------------- parser -----


def test_every_action_carries_its_line_and_the_order_is_monotone(parsed_all):
    for result in parsed_all.values():
        for hand in result.hands:
            ords = [a.ord for a in hand.actions]
            assert None not in ords
            assert ords == sorted(ords), hand.hand_number
            assert hand.ord <= ords[0] if ords else True
            # A board line sits between the street before and the street it opens.
            for street, o in hand.board_ords.items():
                before = [a.ord for a in hand.actions if a.street != street and a.ord < o]
                on = [a.ord for a in hand.actions if a.street == street]
                assert all(x > o for x in on) and before


def test_board_lines_are_run_one_and_the_clock_defaults_to_twenty_plus_ten():
    result = parse(entries(), GAME)
    (hand,) = result.hands
    assert list(hand.board_ords) == ["flop", "turn", "river"]
    assert hand.board_ords["flop"] == ms(4.8) + 6
    assert hand.clock_ms == 30_000
    assert [a.ord for a in hand.actions][:3] == [ms(0) + 2, ms(0) + 3, ms(3) + 4]


def test_a_config_line_changes_the_clock_from_the_next_hand():
    result = parse(read_csv(CLOCK_LOG), "clock")
    assert {h.clock_ms for h in result.hands} == {20_000}  # 10 s limit + the 10 s bank
    lines = [
        (0.0, "Game Config Changes\n* Decision Limit Time: 20s » 40s\n* Max Time Bank: 10s » 20s"),
        *HAND,
        (60.0, "Game Config Changes\n* Decision Limit Time: 40s » off"),
        *[(t + 61, e.replace("hand #1", "hand #2").replace("tttt", "uuuu")) for t, e in HAND],
    ]
    first, second = parse(entries(lines), GAME).hands
    assert first.clock_ms == 60_000
    assert second.clock_ms is None  # switched off: unknown, not a guess


# -------------------------------------------------------------- migration -----


def test_the_migration_adds_the_columns_to_an_older_database(tmp_path):
    path = tmp_path / "old.sqlite"
    connect(path).close()
    raw = sqlite3.connect(path)
    for table, column in (("actions", "ord"), ("hands", "board_ords"), ("hands", "clock_ms")):
        raw.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    raw.commit()
    raw.close()
    conn = connect(path)
    cols = {t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})")} for t in ("actions", "hands")}
    assert "ord" in cols["actions"] and {"board_ords", "clock_ms"} <= cols["hands"]


def test_hands_imported_before_the_column_are_untimed_not_instant():
    conn = connect(":memory:")
    for path in ALL_LOGS:
        import_csv(conn, path)
    conn.execute("UPDATE actions SET ord = NULL")
    conn.execute("UPDATE hands SET board_ords = NULL, clock_ms = NULL")
    facts = [f for h in load_hands(conn) for f in derive(h)]
    assert facts and not any(f.think_ms or f.tempo for f in facts)
    for term in ("think_flop>=0", "think_preflop<=1", "tempo_river=snap", "tempo_flop!=pre"):
        assert not any(parse_filter(term)(f) for f in facts), term


# ------------------------------------------------------------------ facts -----


def test_the_clock_runs_from_the_last_post_action_or_board_line():
    f = facts_of()
    a, b = f["AAA"], f["BBB"]
    assert a.think_ms == {"preflop": 3000, "flop": 7000, "turn": 30500, "river": 900}
    assert b.think_ms == {"preflop": 900, "flop": 700, "turn": 1000, "river": 1600}
    assert a.tempo == {"preflop": "normal", "flop": "tank", "turn": "timeout", "river": "pre"}
    assert b.tempo == {"preflop": "pre", "flop": "snap", "turn": "snap", "river": "snap"}


def test_only_the_first_decision_on_a_street_is_timed():
    lines = [*HAND[:9], (8.0, '"Aa @ AAA" bets 40'), (20.0, '"Bb @ BBB" raises to 200'),
             (21.0, '"Aa @ AAA" folds'), (21.0, 'Uncalled bet of 160 returned to "Bb @ BBB"'),
             (21.0, '"Bb @ BBB" collected 120 from pot'), (21.0, "-- ending hand #1 --")]
    f = facts_of(lines)
    assert f["AAA"].think_ms["flop"] == 2500  # the bet, not the fold after the raise
    assert f["BBB"].think_ms["flop"] == 700  # the check, not the raise


def test_think_time_is_never_negative_and_every_bucket_is_known():
    conn = connect(":memory:")
    for path in ALL_LOGS + EDGE_LOGS:
        import_csv(conn, path)
    facts = [f for h in load_hands(conn) for f in derive(h)]
    timed = [(s, v) for f in facts for s, v in f.think_ms.items()]
    assert len(timed) > 4000
    assert all(v >= 0 for _, v in timed)
    assert all(set(f.tempo) == set(f.think_ms) for f in facts)
    assert {t for f in facts for t in f.tempo.values()} <= set(TEMPO_BUCKETS)


@pytest.mark.parametrize(("kind", "think", "after", "clock", "want"), [
    ("call", 1000, True, 30_000, "pre"),
    ("call", 1000, False, 30_000, "snap"),  # after a board line or a post: never pre
    ("call", 1001, True, 30_000, "normal"),
    ("fold", 3500, True, 30_000, "normal"),
    ("fold", 3501, True, 30_000, "tank"),
    ("bet", 900, True, 30_000, "snap"),  # a bet cannot be pre-selected
    ("bet", 1999, False, 30_000, "snap"),
    ("raise", 2000, True, 30_000, "normal"),
    ("raise", 5000, True, 30_000, "normal"),
    ("bet", 5001, False, 30_000, "tank"),
    ("fold", 30_000, True, 30_000, "timeout"),
    ("check", 30_999, False, 30_000, "timeout"),
    ("check", 31_000, False, 30_000, "tank"),
    ("call", 30_500, True, 30_000, "tank"),  # the clock folds or checks, never calls
    ("fold", 30_500, True, None, "tank"),  # no clock known
])
def test_tempo_cuts(kind, think, after, clock, want):
    assert tempo_of(kind, think, after, clock) == want


def test_the_cuts_are_the_calibrated_ones_and_one_dict():
    from pnt.stats.tags import THRESHOLDS

    assert THRESHOLDS["tempo"] is TEMPO_CUTS
    assert (TEMPO_CUTS["aggressive_snap_ms"], TEMPO_CUTS["aggressive_tank_ms"]) == (2000, 5000)
    assert (TEMPO_CUTS["passive_snap_ms"], TEMPO_CUTS["passive_tank_ms"]) == (1000, 3500)
    assert TEMPO_CUTS["pre_ms"] == 1000 and TEMPO_CUTS["timeout_ms"] == 1000


# ---------------------------------------------------------------- filters -----


def test_think_terms_read_seconds_and_tempo_terms_read_buckets():
    f = facts_of()
    a, b = f["AAA"], f["BBB"]
    assert parse_filter("think_flop>=7")(a) and not parse_filter("think_flop>7")(a)
    assert parse_filter("think_preflop=0.9")(b)
    assert parse_filter("tempo_turn=timeout")(a) and parse_filter("tempo_turn!=tank")(a)
    assert parse_filter("tempo_preflop=pre,tempo_river=snap")(b)
    assert not parse_filter("tempo_flop!=snap")(b)
    with pytest.raises(ValueError, match="unknown tempo"):
        parse_filter("tempo_river=slow")
