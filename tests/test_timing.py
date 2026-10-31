"""Timing tells: when each action was logged, how long each first decision took,
and the tempo bucket it falls in. See SPEC.md, "Timing tells".
"""

from __future__ import annotations

import sqlite3
from collections import Counter

import pytest

from pnt.db.conn import connect
from pnt.ingest.csv_source import RawEntry, read_csv
from pnt.ingest.importer import import_csv
from pnt.logfmt.parser import parse
from pnt.stats.derive import TEMPO_BUCKETS, TEMPO_CUTS, derive, tempo_of
from pnt.stats.filters import parse_filter
from pnt.stats.queries import load_hands
from tests.conftest import ALL_LOGS, EDGE_LOGS, FIXTURES, ROOT

GAME = "pgltest_timing"
#: A log whose table changed its decision clock (20 s to 10 s).
CLOCK_LOG = FIXTURES / "poker_now_log_pglSP7RQmNX3BBkEAr9HPCGLW.csv"


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


def test_every_timing_term_is_documented():
    from pnt.stats.filters import TEMPO_TERMS, VOCABULARY, vocabulary

    documented = {t.replace("<street>", s).split(">")[0].split("=")[0].split("!")[0]
                  for _, terms in VOCABULARY for t, _, _ in terms for s in ("flop", "turn", "river")}
    for name in (*TEMPO_TERMS, "think_preflop", "think_flop", "think_turn", "think_river"):
        assert name in documented, name
    assert vocabulary()["tempos"] == list(TEMPO_BUCKETS)


# ----------------------------------------------------------------- report -----


@pytest.fixture(scope="module")
def db():
    conn = connect(":memory:")
    for path in ALL_LOGS + EDGE_LOGS:
        import_csv(conn, path)
    return conn


def _every_row(out: dict):
    """(filter, n) of every row a timing report prints."""
    for st in out["streets"]:
        yield st["filter"], st["timed"]
        for b in st["buckets"]:
            yield b["filter"], b["n"]
    for block in (out["spots"], out.get("relative", {}).get("spots", [])):
        for sp in block:
            yield sp["filter"], sp["n"]
            for r in sp["rows"]:
                yield r["filter"], r["n"]


def test_every_row_reproduces_from_its_filter(db):
    from pnt.stats.queries import display_names, facts_for, report
    from pnt.stats.timing import timing_report

    names = display_names(db)
    seen = Counter()
    for row in report(db):
        alias = row["player"]
        everything = facts_for(db, alias)
        for spot in ("", "srp", "position=BB"):
            pred = parse_filter(spot, names) if spot else None
            facts = [f for f in everything if pred is None or pred(f)]
            out = timing_report(facts, baseline=everything, with_ids=True)
            for flt, n in _every_row(out):
                combined = parse_filter(",".join(t for t in (spot, flt) if t), names)
                assert sum(1 for f in everything if combined(f)) == n, (alias, spot, flt)
                seen[flt.split(",")[-1]] += n
            for sp in (*out["spots"], *out["relative"]["spots"]):
                assert sum(r["n"] for r in sp["rows"]) == sp["timed"] <= sp["n"], (alias, sp["key"])
                for r in sp["rows"]:
                    assert len(r["hand_ids"]) == r["n"] and r["known"] <= r["n"]
    assert seen["tempo_river=tank"] and seen["tempo_flop=pre"], "the fixtures have both"


def test_the_pool_splits_owners_from_shown_hands(db):
    from pnt.stats.pool import owners
    from pnt.stats.queries import display_names, pool_facts
    from pnt.stats.timing import timing_report

    facts = pool_facts(db)
    own = owners(db)
    assert own, "the fixtures' logs have owners"
    out = timing_report(facts, owners=own, who=display_names(db))
    assert "hand_ids" not in out["spots"][0]["rows"][0]
    for sp in out["spots"]:
        for r in sp["rows"]:
            assert r["own"]["n"] + r["others"]["n"] == r["n"]
            assert r["own"]["known"] + r["others"]["known"] == r["known"]
            # An owner's cards are known on every hand of their own log.
            assert r["own"]["known"] == r["own"]["n"]
            pred = parse_filter(r["filter"])
            assert sum(1 for f in facts if pred(f)) == r["n"]
    assert set(out["thin"]) == {"flop_call", "turn_call", "river_bet", "river_call"}


def test_a_database_from_before_the_clock_asks_for_a_rebuild(db):
    from pnt.stats.timing import needs_rebuild, timing_report

    assert not needs_rebuild(db)
    conn = connect(":memory:")
    import_csv(conn, ALL_LOGS[0])
    conn.execute("UPDATE actions SET ord = NULL")
    assert needs_rebuild(conn)
    out = timing_report([f for h in load_hands(conn) for f in derive(h)])
    assert out["timed"]["timed"] == 0 and out["timed"]["rebuild"]
    assert all(sp["timed"] == 0 for sp in out["spots"])


def test_a_player_with_no_timed_hands_gets_no_relative_rows_not_a_crash():
    # Before a rebuild there is no median to cut from, so no relative spot at all:
    # this was a KeyError, and a 500 from /players/{alias}/timing.
    from pnt.stats.timing import timing_report

    conn = connect(":memory:")
    import_csv(conn, ALL_LOGS[0])
    conn.execute("UPDATE actions SET ord = NULL")
    facts = [f for h in load_hands(conn) for f in derive(h)]
    out = timing_report(facts, baseline=facts, with_ids=True)
    assert out["relative"] == {"medians_ms": {}, "cuts_ms": {}, "spots": []}


# ----------------------------------------------------------------- server -----


@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import importlib

    from pnt.ingest.importer import rename_player
    from tests.conftest import local_client

    monkeypatch.setenv("PNT_DB", str(tmp_path / "api.sqlite"))
    from pnt.server import app as app_module

    importlib.reload(app_module)
    conn = connect(tmp_path / "api.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    rename_player(conn, "genericpoker", "9/2")
    conn.close()
    return local_client(app_module.app)


def test_the_endpoints_carry_rows_that_reproduce(client):
    body = client.get("/players/HSJ/timing").json()
    assert body["player"] == "HSJ" and body["hands"] > 0 and not body["timed"]["rebuild"]
    row = next(r for sp in body["spots"] for r in sp["rows"] if r["n"])
    one = client.get("/players/HSJ/stats", params={"filter": row["filter"]}).json()
    assert one["hands"] == row["n"] == len(row["hand_ids"])
    assert body["relative"]["spots"] and body["relative"]["medians_ms"]
    spot = client.get("/players/HSJ/timing", params={"filter": "srp"}).json()
    assert spot["filter"] == "srp" and spot["hands"] < body["hands"]
    assert client.get("/players/9%2F2/timing").json()["hands"] > 0
    assert client.get("/players/HSJ/timing", params={"filter": "tempo_river=slow"}).status_code == 400
    assert client.get("/players/ghost/timing").status_code == 404

    pool = client.get("/timing").json()
    assert pool["player"] is None and pool["players"] >= 2
    r = next(r for sp in pool["spots"] for r in sp["rows"] if r["n"])
    assert "hand_ids" not in r and {"own", "others"} <= r.keys()
    stats = client.get("/stats", params={"filter": r["filter"]}).json()
    assert sum(p["hands"] for p in stats) == r["n"]


def test_the_cli_prints_the_tables(tmp_path):
    import json

    from typer.testing import CliRunner

    from pnt import cli

    path = tmp_path / "cli.sqlite"
    conn = connect(path)
    for log in ALL_LOGS:
        import_csv(conn, log)
    conn.close()
    run = lambda *args: CliRunner().invoke(cli.app, ["timing", *args, "--db", str(path)])
    out = run("HSJ")
    assert out.exit_code == 0, out.output
    assert "River bet" in out.output and "line_river=b*,tempo_river=tank" in out.output
    assert "pnt rebuild" not in out.output
    out = run("HSJ", "--relative")
    assert out.exit_code == 0 and "own median cuts" in out.output and "tempo_flop!=pre" in out.output
    out = run()
    assert out.exit_code == 0 and "owners, own cards" in out.output and "thin per player" in out.output
    body = json.loads(run("HSJ", "--json", "--filter", "srp").output)
    assert body["filter"] == "srp" and body["spots"]
    assert run("nobody").exit_code != 0


def test_a_restart_says_when_a_rebuild_is_needed(tmp_path, capsys):
    from pnt import cli

    path = tmp_path / "hint.sqlite"
    conn = connect(path)
    import_csv(conn, ALL_LOGS[0])
    conn.close()
    cli._rebuild_hint(str(path))
    assert "pnt rebuild" not in capsys.readouterr().out
    conn = connect(path)
    conn.execute("UPDATE actions SET ord = NULL")
    conn.commit()
    conn.close()
    cli._rebuild_hint(str(path))
    assert "Run `pnt rebuild` once" in capsys.readouterr().out
    cli._rebuild_hint(str(tmp_path / "missing.sqlite"))  # no database: nothing to say
    assert capsys.readouterr().out == ""


def _read(*parts: str) -> str:
    return ROOT.joinpath(*parts).read_text("utf-8")


def test_the_chart_has_the_view():
    html = _read("pnt", "server", "static", "chart.html")
    assert 'data-view="timing"' in html and "function renderTiming(" in html
    assert "/timing?" in html and "timing: " in html
    views = html[html.index("const VIEWS = ["):]
    views = views[: views.index("];")]
    assert '"timing"' in views and views.index('"timing"') < views.index('"results"')


def test_the_side_panel_runs_a_clock_on_the_player_to_act():
    content = _read("pnt", "extension", "content.js")
    panel = _read("pnt", "extension", "sidepanel.js")
    assert "tableActingAt: state.tableActingAt" in content
    assert "snap.tableActingAt" in panel and "PNT.clockText(" in panel and "clockChip()" in panel
