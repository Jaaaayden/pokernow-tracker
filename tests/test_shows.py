"""Voluntary shows read as a tell: what a player chose to turn over, and what it was.

`test_voluntary_shows.py` pins how the cards are kept apart; this pins how they
are read. Every count has to come back to the view's rows, and the range views
must still never see them.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pnt import cli
from pnt.db.conn import connect
from pnt.ingest.importer import import_csv
from pnt.stats.filters import FLAGS
from pnt.stats.queries import facts_for, identities_of
from pnt.stats.ranges import range_grid
from pnt.stats.shows import (
    CLASSES,
    CONTEXTS,
    POT_TYPES,
    classify,
    context_of,
    pot_type,
    versus,
    voluntary_shows,
)
from tests.conftest import ALL_LOGS, MULTIWAY_GAME, local_client


def _aliases(db) -> list[str]:
    return [r["alias"] for r in db.execute("SELECT alias FROM players ORDER BY alias")]


# ------------------------------------------------------------- the reading ---


def test_a_fold_is_read_on_the_street_it_was_folded():
    board = ("Ac", "7c", "2d", "9h", "4h")
    assert classify("Kc3c", board, "flop") == ("draw", "flush_draw")
    assert classify("Kc3c", board, "river") == ("air", "flush_draw"), "a river draw has missed"
    assert classify("9d9s", board, "turn") == ("strong", "set")
    assert classify("8s8d", board, "flop") == ("marginal", "pocket_pair")


def test_preflop_shows_are_read_by_the_chart():
    assert classify("AsAh", (), "preflop") == ("strong", "AA")
    assert classify("7s2d", (), "preflop") == ("air", "72o")


def test_a_fold_wins_over_a_showdown_others_reached():
    assert context_of(True, True) == "folded"
    assert context_of(False, True) == "showdown"
    assert context_of(False, False) == "uncontested"


# --------------------------------------------------------------- the counts ---


def test_class_counts_sum_to_the_two_card_total(db):
    seen = 0
    for alias in _aliases(db):
        s = voluntary_shows(db, alias)
        assert set(s["classes"]) == set(CLASSES)
        assert [c["key"] for c in s["contexts"]] == list(CONTEXTS)
        assert sum(s["classes"].values()) == s["two_card"]
        assert s["two_card"] + s["one_card"] == s["shows"] == len(s["rows"])
        for c in s["contexts"]:
            assert sum(c["classes"].values()) == c["two_card"]
            assert c["two_card"] + c["one_card"] == c["shows"]
        for k in ("shows", "two_card", "one_card"):
            assert sum(c[k] for c in s["contexts"]) == s[k]
        for cls in CLASSES:
            assert sum(c["classes"][cls] for c in s["contexts"]) == s["classes"][cls]
        # Every row is classified exactly when it has two cards.
        assert all((r["class"] is not None) == (r["n_cards"] == 2) for r in s["rows"])
        seen += s["two_card"]
    assert seen > 0, "the fixtures must exercise two-card shows"


def test_every_row_carries_the_pot_type_the_filter_terms_read(db):
    terms = {"limped": "limped", "srp": "srp", "3bet": "3bet_pot", "4bet": "4bet_pot"}
    seen = Counter()
    for alias in _aliases(db):
        facts = {f.hand_id: f for f in facts_for(db, alias)}
        for r in voluntary_shows(db, alias)["rows"]:
            f = facts[r["hand_id"]]
            assert r["pot_level"] == f.pot_level
            assert r["pot"] == pot_type(f.pot_level) in POT_TYPES
            for word, term in terms.items():
                assert FLAGS[term](f) == (r["pot"] == word)
            seen[r["pot"]] += 1
    assert seen["srp"] and seen["3bet"], "the fixtures show from more than one pot type"
    assert (pot_type(0), pot_type(1), pot_type(2), pot_type(5)) == ("other", "limped", "srp", "4bet")


def test_fixture_counts_match_the_view(db):
    total = 0
    for alias in _aliases(db):
        ids = identities_of(db, alias)
        q = ",".join("?" * len(ids))
        view = db.execute(
            f"SELECT count(*) AS n, COALESCE(sum(n_cards = 2), 0) AS two,"
            f" COALESCE(sum(folded = 1), 0) AS folded FROM v_voluntary_shows WHERE pn_id IN ({q})",
            ids,
        ).fetchone()
        s = voluntary_shows(db, alias)
        by = {c["key"]: c for c in s["contexts"]}
        assert (s["shows"], s["two_card"], by["folded"]["shows"]) == (view["n"], view["two"], view["folded"])
        assert {r["hand_id"] for r in s["rows"]} == {
            r[0] for r in db.execute(f"SELECT hand_id FROM v_voluntary_shows WHERE pn_id IN ({q})", ids)
        }
        total += s["shows"]
    # Every show belongs to someone with an alias, so the players add up to the view.
    assert total == db.execute("SELECT count(*) FROM v_voluntary_shows").fetchone()[0] > 0


def test_a_game_narrows_the_shows(db):
    alias = "getting even"
    every = voluntary_shows(db, alias)
    one = voluntary_shows(db, alias, MULTIWAY_GAME)
    assert 0 < one["shows"] <= every["shows"]
    assert all(r["game_id"] == MULTIWAY_GAME for r in one["rows"])


def test_the_multiway_fold_is_air_on_the_flop(db):
    """Hand #29 of the multiway log: hsj folds the flop and shows Q♣ 8♣ on 5♣ 6♠ K♥."""
    s = voluntary_shows(db, "hsj")
    row = next(r for r in s["rows"] if r["game_id"] == MULTIWAY_GAME and r["hand_number"] == 29)
    assert (row["context"], row["street"], row["board"]) == ("folded", "flop", ["5c", "6s", "Kh"])
    assert (row["class"], row["made"]) == ("air", "high_card")


def test_the_note_says_these_are_chosen(db):
    s = voluntary_shows(db, "genericpoker")
    note = " ".join(s["note"])
    assert "chosen" in note.lower() and "never shown" in note


def test_versus_reads_the_board_as_it_stood():
    assert versus("AhAs", "KdKc", ("2c", "7d", "9h")) == "ahead"
    assert versus("AhAs", "KdKc", ("Kh", "7d", "9h")) == "behind"
    assert versus("Ah2s", "Ad3c", ("Kh", "Kd", "Qh", "Qs", "Jc")) == "chop"


def test_rates_and_folds_against_the_winner_add_up(db):
    seen = 0
    for alias in _aliases(db):
        s = voluntary_shows(db, alias)
        n = s["two_card"]
        assert s["air_pct"] == (round(100 * s["classes"]["air"] / n, 1) if n else None)
        assert s["strong_pct"] == (round(100 * s["classes"]["strong"] / n, 1) if n else None)
        fv = s["folded_vs"]
        judged = [r for r in s["rows"] if r["vs"]]
        assert fv["known"] == len(judged) == fv["ahead"] + fv["chop"] + fv["behind"]
        for r in judged:
            # Only a two-card fold after the flop, held against someone else.
            assert r["context"] == "folded" and r["n_cards"] == 2 and len(r["board"]) >= 3
            assert r["vs"]["result"] == versus(r["cards"], r["vs"]["cards"], tuple(r["board"]))
        seen += fv["known"]
    assert seen, "the fixtures have shown folds against a known winner"


def test_an_unknown_alias_raises(db):
    with pytest.raises(ValueError):
        voluntary_shows(db, "nobody")


# --------------------------------------------------------------- regression ---


def test_range_views_still_never_include_voluntary_shows(db):
    """A voluntary show's cards stay out of `Facts`, so out of every range view.

    Rows whose cards were known anyway (the hero's own, or a showdown) are left
    aside: those cards came from `hole_cards`, not from the show.
    """
    checked = 0
    for alias in _aliases(db):
        shown = {
            (r["hand_id"], r["pn_id"])
            for r in db.execute(
                "SELECT v.hand_id, v.pn_id FROM v_voluntary_shows v"
                " JOIN hand_players hp ON hp.hand_id = v.hand_id AND hp.pn_id = v.pn_id"
                " WHERE v.alias = ? AND hp.hole_cards IS NULL",
                (alias,),
            )
        }
        if not shown:
            continue
        facts = facts_for(db, alias)
        hit = [f for f in facts if (f.hand_id, f.pn_id) in shown]
        assert len(hit) == len(shown)
        assert all(not f.hole_cards for f in hit)
        grid = range_grid(facts)
        assert grid["known"] == sum(1 for f in facts if f.hole_cards)
        checked += len(hit)
    assert checked > 0


# ------------------------------------------------------------ endpoint, cli ---


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PNT_DB", str(tmp_path / "api.sqlite"))
    import importlib

    from pnt.server import app as app_module

    importlib.reload(app_module)
    conn = connect(tmp_path / "api.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    conn.close()
    return local_client(app_module.app)


def test_endpoint_answers_for_a_player_and_a_slash_name(client):
    body = client.get("/players/genericpoker/shows").json()
    assert body["player"] == "genericpoker" and body["shows"] > 0
    assert all(isinstance(r["hand_id"], int) for r in body["rows"])
    assert client.get(f"/hands/{body['rows'][0]['hand_id']}").status_code == 200
    assert client.post("/aliases/rename", json={"old": "genericpoker", "new": "9/2"}).status_code == 200
    slashed = client.get("/players/9/2/shows").json()
    assert slashed["player"] == "9/2" and slashed["shows"] == body["shows"]
    assert client.get("/players/nobody/shows").status_code == 404
    one = client.get("/players/9/2/shows", params={"game": MULTIWAY_GAME}).json()
    assert one["shows"] <= body["shows"]


def test_every_shown_hand_is_in_the_list_its_chart_link_opens(client):
    """A shown hand links to `/chart?player=<alias>&hand=<id>`, which lists the
    player's hands and keeps that one: it has to be among them, slash name too."""
    assert client.post("/aliases/rename", json={"old": "genericpoker", "new": "9/2"}).status_code == 200
    checked = 0
    for alias in ("9/2", "hsj", "HSJ", "getting even"):
        shows = client.get(f"/players/{alias}/shows").json()
        listed = {h["hand_id"] for h in client.get(f"/players/{alias}/hands").json()["hands"]}
        for r in shows["rows"]:
            assert r["hand_id"] in listed, (alias, r["hand_number"])
            checked += 1
    assert checked


def test_the_chart_opens_a_linked_hand():
    """A link to the chart's `hand=` param opens a one-hand list with its replay,
    and the Shows view does the same for a row."""
    static = Path(__file__).resolve().parents[1] / "pnt" / "server" / "static"
    chart = static.joinpath("chart.html").read_text("utf-8")
    read_url = chart[chart.index("const readUrl = () => {"):chart.index("const writeUrl = (")]
    assert 'state.hand = ' in read_url and 'q.get("hand")' in read_url
    boot = chart[chart.index("  readUrl();\n"):]
    assert "openHands(" in boot and 'pntReplay.show($("replay"), state.hand)' in boot
    # The chart's own Shows view lists and replays a row's one hand the same way.
    shows = chart[chart.index("function renderShows("):chart.index("function renderBounties(")]
    assert "openHands(" in shows and 'pntReplay.show($("replay"), r.hand_id)' in shows
    assert "/hands/" not in shows, "raw JSON is not a link to read"


def test_cli_prints_the_table_and_json(tmp_path):
    path = tmp_path / "shows.sqlite"
    conn = connect(path)
    for log in ALL_LOGS:
        import_csv(conn, log)
    conn.close()
    result = CliRunner().invoke(cli.app, ["shows", "genericpoker", "--db", str(path)])
    assert result.exit_code == 0, result.output
    assert "folded, then showed" in result.output and "won uncontested" in result.output
    result = CliRunner().invoke(cli.app, ["shows", "genericpoker", "--json", "--db", str(path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["shows"] > 0
    assert CliRunner().invoke(cli.app, ["shows", "nobody", "--db", str(path)]).exit_code != 0
