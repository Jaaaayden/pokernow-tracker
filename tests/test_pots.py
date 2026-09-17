"""Biggest pots: the window, the bar, and the pot itself.

The pot is the part worth pinning hardest -- it is `derive`'s figure computed a
second time, in SQL, and the whole page is wrong if the two ever disagree. The
window is pinned against a frozen `now`, because a test cannot wait a day to
watch a hand fall out of it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import ALL_LOGS, HU_GAME
from typer.testing import CliRunner

from pnt.stats import pots as pt
from pnt.stats.derive import derive
from pnt.stats.queries import load_hands

#: Later than every fixture hand, so "the last N days" is a known distance from them.
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


@pytest.fixture()
def latest(db):
    """When the newest fixture hand was dealt."""
    return datetime.fromisoformat(db.execute("SELECT MAX(ts) FROM hands").fetchone()[0])


# ----------------------------------------------------------------- the pot ---


def test_the_sql_pot_is_the_pot_derive_computes(db):
    """One definition, evaluated twice: SQL for the search, `derive` for the row."""
    out = pt.big_pots(db, days=None, min_pot=0, limit=500)
    assert out["pots"], "the fixture logs have hands"
    by_id = {r["hand_id"]: r["pot"] for r in out["pots"]}
    for hand in load_hands(db, hand_ids=list(by_id)):
        assert by_id[hand.hand_id] == derive(hand)[0].pot


def test_the_pot_excludes_uncalled_bets_and_bounties(db):
    """It is what sat in the middle: contributions, not what anyone took home."""
    out = pt.big_pots(db, days=None, min_pot=0, limit=500)
    for r in out["pots"]:
        assert r["pot"] == sum(s["contributed"] for s in r["seats"])
        # Every chip collected came out of the pot, bounties aside.
        assert sum(s["collected"] for s in r["seats"]) <= r["pot"]


def test_rows_are_biggest_first_and_all_clear_the_bar(db):
    out = pt.big_pots(db, days=None, min_pot=300, limit=20)
    pots = [r["pot"] for r in out["pots"]]
    assert pots == sorted(pots, reverse=True)
    assert all(p >= 300 for p in pots)
    assert out["biggest"] == pots[0]


def test_a_capped_list_says_what_it_is_a_slice_of(db):
    everything = pt.big_pots(db, days=None, min_pot=0, limit=500)
    capped = pt.big_pots(db, days=None, min_pot=0, limit=5)
    assert len(capped["pots"]) == 5
    assert capped["over"] == everything["over"] > 5
    assert capped["hands"] == everything["hands"]
    # The five it shows are the five biggest, not just five.
    assert [r["pot"] for r in capped["pots"]] == [r["pot"] for r in everything["pots"][:5]]


def test_the_bar_is_inclusive(db):
    out = pt.big_pots(db, days=None, min_pot=0, limit=500)
    size = out["pots"][3]["pot"]
    at = pt.big_pots(db, days=None, min_pot=size, limit=500)
    assert all(r["pot"] >= size for r in at["pots"])
    assert size in [r["pot"] for r in at["pots"]]
    above = pt.big_pots(db, days=None, min_pot=size + 1, limit=500)
    assert all(r["pot"] > size for r in above["pots"])


# -------------------------------------------------------------- the window ---


def test_the_cutoff_is_formatted_the_way_the_parser_stores_ts(db):
    """A string comparison, so the two formats have to match to the millisecond."""
    stored = db.execute("SELECT ts FROM hands LIMIT 1").fetchone()[0]
    made = pt.cutoff(7, NOW)
    assert made.endswith("Z") and "T" in made and len(made) == len(stored)
    assert made == "2026-09-13T12:00:00.000Z"

    # The trap this replaces. SQLite renders a space where ts has a `T`, and a
    # space sorts before it, so a hand from earlier on the cutoff's own date
    # compares as *later* than the cutoff and leaks into the window.
    sqlite_form = db.execute("SELECT datetime(?)", (made.replace("Z", ""),)).fetchone()[0]
    assert sqlite_form == "2026-09-13 12:00:00"
    stale = "2026-09-13T00:12:00.000Z"
    assert stale >= sqlite_form  # wrongly kept
    assert not stale >= made     # correctly dropped


def test_the_window_keeps_the_hands_inside_it_and_drops_the_rest(db, latest):
    """Slide a window over the newest hand and watch it fall out."""
    just_after = latest + timedelta(minutes=1)
    inside = pt.big_pots(db, days=1, min_pot=0, limit=500, now=just_after)
    assert inside["hands"] > 0
    ids = {r["hand_id"] for r in inside["pots"]}
    for hand in load_hands(db, hand_ids=list(ids)):
        assert hand.ts >= inside["since"]

    outside = pt.big_pots(db, days=1, min_pot=0, limit=500, now=latest + timedelta(days=3))
    assert outside["hands"] == 0 and outside["pots"] == [] and outside["biggest"] == 0


def test_a_window_of_none_reaches_over_everything(db):
    out = pt.big_pots(db, days=None, min_pot=0, limit=500)
    assert out["since"] is None
    assert out["hands"] == db.execute("SELECT COUNT(*) FROM hands").fetchone()[0]


def test_an_empty_window_still_answers(db, latest):
    """Nothing to show is a real answer, not a crash or a null."""
    out = pt.big_pots(db, days=1, min_pot=10**9, limit=10, now=latest)
    assert (out["over"], out["chips"], out["pots"]) == (0, 0, [])
    # `biggest` is the biggest pot in the *window*, not the biggest that cleared
    # the bar -- which is exactly how you find out the bar is set too high.
    assert out["hands"] > 0 and 0 < out["biggest"] < 10**9


# ---------------------------------------------------------------- the rows ---


def test_a_row_describes_the_hand_it_came_from(db):
    out = pt.big_pots(db, days=None, min_pot=0, limit=25)
    for r in out["pots"]:
        assert len(r["seats"]) == r["players"]
        assert [s["net"] for s in r["seats"]] == sorted((s["net"] for s in r["seats"]), reverse=True)
        assert r["street"] in ("preflop", "flop", "turn", "river")
        assert len(r["board"]) in (0, 3, 4, 5)
        assert (r["pot_bb"] is None) == (r["bb"] is None)
        # Chips balance within the hand: the pot went somewhere, bounties aside.
        assert sum(s["net"] - s["bounty"] for s in r["seats"]) == 0 or not r["complete"]


def test_a_chopped_pot_names_no_winner(db):
    out = pt.big_pots(db, days=None, min_pot=0, limit=500)
    chopped = [r for r in out["pots"] if r["chopped"]]
    assert chopped, "the fixture logs have split pots"
    for r in chopped:
        assert sum(1 for s in r["seats"] if s["collected"] > 0) > 1
    for r in out["pots"]:
        if not r["chopped"] and r["winner"]:
            assert r["won"] == max(s["net"] for s in r["seats"]) > 0
            assert r["seats"][0]["player"] == r["winner"]


def test_one_player_narrows_to_their_hands_without_changing_the_pot(db):
    everyone = pt.big_pots(db, days=None, min_pot=0, limit=500)
    mine = pt.big_pots(db, days=None, min_pot=0, limit=500, player="genericpoker")
    assert mine["hands"] < everyone["hands"]
    # The pot stays the table's, never narrowed to that player's share of it.
    whole = dict(db.execute("SELECT hand_id, SUM(contributed) FROM hand_players GROUP BY hand_id"))
    for r in mine["pots"]:
        assert any(s["player"] == "genericpoker" for s in r["seats"])
        assert r["pot"] == whole[r["hand_id"]]


def test_one_game_narrows_to_it(db):
    out = pt.big_pots(db, days=None, min_pot=0, limit=500, game_id=HU_GAME)
    assert out["pots"] and all(r["game_id"] == HU_GAME for r in out["pots"])
    assert out["hands"] == db.execute(
        "SELECT COUNT(*) FROM hands WHERE game_id = ?", (HU_GAME,)
    ).fetchone()[0]


def test_an_unknown_player_raises(db):
    with pytest.raises(ValueError, match="unknown alias"):
        pt.big_pots(db, player="nobody")


# ---------------------------------------------------------------- the edges --


def test_the_spec_table_is_the_other_copy_of_the_defaults():
    import pathlib

    spec = (pathlib.Path(__file__).resolve().parents[1] / "pnt" / "stats" / "SPEC.md").read_text(encoding="utf-8")
    section = spec.split("## Biggest pots", 1)[1].split("## Known judgement calls", 1)[0]
    assert f"| `days` | {pt.DEFAULT_DAYS} |" in section
    assert f"| `min_pot` | {pt.DEFAULT_MIN_POT} |" in section
    assert f"| `limit` | {pt.DEFAULT_LIMIT} |" in section


def test_the_cli_lists_pots_and_json(db, tmp_path):
    from pnt import cli

    path = str(tmp_path / "t.sqlite")
    runner = CliRunner()
    result = runner.invoke(cli.app, ["pots", "--db", path, "--all", "--min-pot", "0", "--limit", "3"])
    assert result.exit_code == 0, result.output
    assert "pot(s) over 0 in all time" in result.output

    result = runner.invoke(cli.app, ["pots", "--db", path, "--all", "--min-pot", "0", "--limit", "2", "--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert {"days", "min_pot", "hands", "over", "biggest", "pots"} <= set(body)
    assert len(body["pots"]) == 2

    # The default window is 7 days back from now, and the fixtures are older.
    quiet = runner.invoke(cli.app, ["pots", "--db", path, "--min-pot", "0"])
    assert quiet.exit_code == 0 and "(no pots that big)" in quiet.output

    assert runner.invoke(cli.app, ["pots", "--db", path, "--days", "0"]).exit_code != 0
    assert runner.invoke(cli.app, ["pots", "--db", path, "--player", "nobody"]).exit_code != 0


def test_the_endpoint_serves_the_page_to_browsers_and_json_to_everyone_else(tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PNT_DB", str(tmp_path / "api.sqlite"))
    import importlib

    from pnt.server import app as app_module

    importlib.reload(app_module)
    from pnt.db.conn import connect
    from pnt.ingest.importer import import_csv

    conn = connect(tmp_path / "api.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    conn.close()
    client = TestClient(app_module.app)

    page = client.get("/pots", headers={"Accept": "text/html"})
    assert page.status_code == 200 and "Biggest pots" in page.text
    assert client.get("/pots.html").status_code == 200

    body = client.get("/pots", params={"all_time": 1, "min_pot": 0, "limit": 4}).json()
    assert body["since"] is None and len(body["pots"]) == 4
    assert body["pots"][0]["pot"] == body["biggest"]

    assert client.get("/pots", params={"days": 0}).status_code == 422
    assert client.get("/pots", params={"limit": 9999}).status_code == 422
    assert client.get("/pots", params={"player": "nobody"}).status_code == 404
