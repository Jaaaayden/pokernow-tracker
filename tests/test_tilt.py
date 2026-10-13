"""Tilt and session dynamics: the post-pass, its filter terms, the report, the HUD flag.

See SPEC.md, "Session dynamics".
"""

from __future__ import annotations

import random
from collections import Counter

import pytest

from pnt.stats.derive import Facts
from pnt.stats.filters import parse_filter
from pnt.stats.queries import display_names, facts_by_player, facts_cached, facts_for, report
from pnt.stats.tilt import BIG_LOSS_BB, WINDOW, annotate, next_hand, post_loss, tilt_report
from tests.conftest import ALL_LOGS, HU_GAME, local_client

BB = 10


def hand(n: int, game: str = "g1", pn: str = "p1", net: int = 0, *, minute: int | None = None,
         complete: bool = True, bb: int | None = BB) -> Facts:
    """One synthetic row: hand `n` of `game`, `minute` minutes after 20:00."""
    ts = None if minute is None else f"2026-10-01T{20 + minute // 60:02d}:{minute % 60:02d}:00.000Z"
    return Facts(hand_id=hash((game, pn, n)) & 0xFFFFFF, pn_id=pn, n_dealt_in=2, seats_from_button=0,
                 dead_button=False, game_id=game, hand_number=n, ts=ts, net=net, bb_size=bb, complete=complete)


def session(k: int, game: str = "g1", losses: dict[int, int] | None = None, pn: str = "p1") -> list[Facts]:
    """Hands 1..k of one game, a minute apart, with `losses` as {hand: net in bb}."""
    losses = losses or {}
    return [hand(n, game, pn, losses.get(n, 0) * BB, minute=n - 1) for n in range(1, k + 1)]


# ------------------------------------------------------------ annotate -----


def test_the_window_is_the_next_window_hands_after_a_big_loss():
    rows = annotate(session(40, losses={3: -BIG_LOSS_BB}))
    by_n = {f.hand_number: f for f in rows}
    assert all(by_n[n].hands_since_loss is None and by_n[n].loss_bb is None for n in (1, 2, 3))
    assert (by_n[4].hands_since_loss, by_n[4].loss_bb) == (1, float(BIG_LOSS_BB))
    assert by_n[3 + WINDOW].hands_since_loss == WINDOW and post_loss(by_n[3 + WINDOW])
    assert by_n[4 + WINDOW].hands_since_loss == WINDOW + 1 and not post_loss(by_n[4 + WINDOW])
    assert sum(post_loss(f) for f in rows) == WINDOW
    assert [f.session_hand for f in rows] == list(range(1, 41))


def test_the_threshold_is_inclusive_and_needs_a_result():
    rows = session(5, losses={1: -BIG_LOSS_BB + 0.5})
    rows[1].net, rows[1].complete = -100 * BB, False  # a log that stopped mid-hand
    rows[2].net, rows[2].bb_size = -100 * BB, None  # no known big blind
    annotate(rows)
    assert not any(post_loss(f) for f in rows)


def test_a_new_loss_restarts_the_window_and_carries_its_own_size():
    rows = annotate(session(30, losses={2: -50, 10: -120}))
    by_n = {f.hand_number: f for f in rows}
    assert (by_n[9].hands_since_loss, by_n[9].loss_bb) == (7, 50.0)
    assert (by_n[11].hands_since_loss, by_n[11].loss_bb) == (1, 120.0)
    assert by_n[30].hands_since_loss == 20 and post_loss(by_n[30])


def test_a_loss_never_carries_into_the_next_session():
    first = session(10, "g1", losses={10: -200})
    second = session(5, "g2")
    rows = annotate(first + second)
    nxt = [f for f in rows if f.game_id == "g2"]
    assert [f.session_hand for f in nxt] == [1, 2, 3, 4, 5]
    assert all(f.hands_since_loss is None for f in nxt)
    assert nxt[0].session_hour == 0


def test_each_player_and_order_is_its_own_run():
    rows = session(25, pn="p1", losses={1: -60}) + session(25, pn="p2")
    random.Random(7).shuffle(rows)
    annotate(rows)
    assert sum(post_loss(f) for f in rows if f.pn_id == "p1") == WINDOW
    assert not any(post_loss(f) for f in rows if f.pn_id == "p2")
    # Idempotent, and a game's rows read the same with or without other games in the list.
    before = [(f.hands_since_loss, f.session_hand, f.session_hour) for f in rows]
    annotate(rows)
    assert before == [(f.hands_since_loss, f.session_hand, f.session_hour) for f in rows]


def test_session_hour_reads_the_clock_from_the_first_timestamped_hand():
    rows = [hand(1, minute=None), hand(2, minute=0), hand(3, minute=90), hand(4, minute=None)]
    annotate(rows)
    assert [f.session_hour for f in rows] == [None, 0, 1.5, None]


def test_next_hand_counts_the_latest_hand_as_a_loss_of_its_own():
    rows = annotate(session(5, losses={5: -80}))
    assert next_hand(rows, "g1") == {"post_loss": True, "since": 1, "loss_bb": 80.0}
    rows = annotate(session(3 + WINDOW - 1, losses={3: -80}))
    assert next_hand(rows, "g1")["post_loss"] and next_hand(rows, "g1")["since"] == WINDOW
    rows = annotate(session(3 + WINDOW, losses={3: -80}))
    assert next_hand(rows, "g1") == {"post_loss": False, "since": None, "loss_bb": None}
    assert not next_hand(rows, "another game")["post_loss"]


# --------------------------------------------------------------- terms -----


def test_the_terms_read_the_annotated_facts():
    rows = annotate(session(30, losses={5: -45}))
    count = lambda expr: sum(1 for f in rows if parse_filter(expr)(f))
    assert count("post_loss") == WINDOW and count("!post_loss") == 30 - WINDOW
    assert count("hands_since_loss<=5") == 5
    assert count("session_hand>25") == 5
    assert count("session_hour>=0.25") == 30 - 15


def test_an_unannotated_row_matches_no_session_term():
    raw = hand(1, minute=0)
    for expr in ("post_loss", "hands_since_loss>=0", "session_hand>=0", "session_hour>=0"):
        assert not parse_filter(expr)(raw), expr


# ------------------------------------------------------------- the pool -----


def test_every_list_queries_hands_out_is_annotated(db):
    grouped, aliases, _ = facts_by_player(db)
    alias = max(aliases.values(), key=lambda a: len(facts_for(db, a)))
    mine = facts_for(db, alias)
    cached = facts_cached(db, alias)
    key = lambda f: (f.hand_id, f.pn_id)
    session_of = lambda rows: {key(f): (f.session_hand, f.hands_since_loss, f.session_hour) for f in rows}
    assert session_of(mine) == session_of(cached)
    pid = next(p for p, a in aliases.items() if a == alias)
    assert session_of(grouped[pid]) == session_of(mine)
    assert all(f.session_hand >= 1 for f in mine)
    # One game's list carries the same context as that game's rows of the lifetime list.
    game = mine[-1].game_id
    assert session_of(facts_for(db, alias, game)) == {k: v for k, v in session_of(mine).items()
                                                      if k[0] in {f.hand_id for f in mine if f.game_id == game}}


def test_every_row_reproduces_from_its_filter(db):
    names = display_names(db)
    seen = Counter()
    for row in report(db):
        alias = row["player"]
        all_facts = facts_for(db, alias)
        for spot in ("", "vpip", "position=BB"):
            pred = parse_filter(spot, names) if spot else None
            facts = [f for f in all_facts if pred is None or pred(f)]
            out = tilt_report(facts)
            assert out["hands"] == len(facts)
            for table in out["tables"]:
                for r in table["rows"]:
                    combined = ",".join(t for t in (spot, r["filter"]) if t)
                    p = parse_filter(combined, names)
                    assert sum(1 for f in all_facts if p(f)) == r["hands"], (alias, spot, r)
                    seen[r["key"]] += r["hands"]
            loss, hour = out["tables"]
            assert loss["rows"][1]["hands"] + loss["rows"][2]["hands"] == out["hands"]
            assert sum(r["hands"] for r in hour["rows"]) == out["clock"]["known"]
    assert seen["post_loss"], "the fixtures have hands after a big loss"
    assert seen["h0"] and seen["h1"], "and sessions longer than an hour"


def test_the_report_figures_are_the_tallies_of_their_hands(db):
    facts = facts_for(db, "HSJ")
    out = tilt_report(facts)
    post = out["tables"][0]["rows"][1]
    window = [f for f in facts if post_loss(f)]
    assert post["hands"] == len(window)
    assert post["vpip_n"] == sum(f.vpip_opp for f in window)
    assert post["vpip"] == round(100 * sum(f.vpip for f in window) / post["vpip_n"], 1)
    assert out["vpip_gap"] == round(post["vpip"] - out["tables"][0]["rows"][0]["vpip"], 1)
    assert out["big_loss_bb"] == BIG_LOSS_BB and out["window"] == WINDOW


# ---------------------------------------------------------------- server -----


@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import importlib

    from pnt.db.conn import connect
    from pnt.ingest.importer import import_csv, rename_player

    monkeypatch.setenv("PNT_DB", str(tmp_path / "api.sqlite"))
    from pnt.server import app as app_module

    importlib.reload(app_module)
    conn = connect(tmp_path / "api.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    rename_player(conn, "genericpoker", "9/2")
    conn.close()
    return local_client(app_module.app)


def test_the_tilt_endpoint_matches_the_stats_filter(client):
    body = client.get("/players/HSJ/tilt").json()
    assert body["player"] == "HSJ" and body["filter"] is None
    post = next(r for r in body["tables"][0]["rows"] if r["key"] == "post_loss")
    assert post["filter"] == "post_loss" and post["hands"] > 0
    stats = client.get("/stats", params={"filter": "post_loss"}).json()
    assert next(r for r in stats if r["player"] == "HSJ")["hands"] == post["hands"]
    one = client.get("/players/HSJ/stats", params={"filter": "post_loss"}).json()
    assert one["hands"] == post["hands"] and one["vpip"] == post["vpip"]
    spot = client.get("/players/HSJ/tilt", params={"filter": "position=BB"}).json()
    assert spot["filter"] == "position=BB" and spot["hands"] < body["hands"]
    assert client.get("/players/9%2F2/tilt").json()["hands"] > 0
    assert client.get("/players/HSJ/tilt", params={"filter": "nonsense"}).status_code == 400
    assert client.get("/players/ghost/tilt").status_code == 404


def test_the_hud_carries_post_loss_per_seat(client):
    body = client.get(f"/hud/{HU_GAME}").json()
    assert body["seats"]
    for s in body["seats"]:
        assert isinstance(s["post_loss"], bool)
        if s["post_loss"]:
            assert 1 <= s["post_loss_since"] <= WINDOW and s["post_loss_bb"] >= BIG_LOSS_BB
        else:
            assert s["post_loss_since"] is None and s["post_loss_bb"] is None


# ---------------------------------------------------------------- pages -----


def _read(*parts: str) -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parents[1].joinpath(*parts)).read_text("utf-8")


def test_the_chart_has_the_view():
    html = _read("pnt", "server", "static", "chart.html")
    assert 'data-view="tilt"' in html and "function renderTilt(" in html and "/tilt?" in html
    views = html[html.index("const VIEWS = ["):]
    views = views[: views.index("];")]
    assert '"tilt"' in views and views.index('"tilt"') < views.index('"results"')
    assert "tilt: " in html


def test_the_side_panel_shows_the_chip():
    js = _read("pnt", "extension", "sidepanel.js")
    assert "post_loss" in js and "tilt?" in js


# ----------------------------------------------------------------- tag -----


def _nights(games: int, post_vpip: bool) -> list[Facts]:
    """`games` sessions, each a big loss on hand 1, then WINDOW hands, then 30 more
    where they play every other hand. In the window they play all of them, or half."""
    rows = []
    for g in range(games):
        night = session(1 + WINDOW + 30, game=f"g{g}", losses={1: -100})
        for f in night:
            f.vpip_opp = True
            f.vpip = post_vpip if 1 < f.hand_number <= 1 + WINDOW else f.hand_number % 2 == 0
        rows += night
    return annotate(rows)


def test_tilts_fires_on_a_post_loss_vpip_gap():
    from pnt.stats.tags import THRESHOLDS, tags_for

    assert THRESHOLDS["tilts"] == {"gap": 5, "n": 100}
    rows = _nights(7, post_vpip=True)
    out = tags_for(rows)
    tag = next(t for t in out["tags"] if t["id"] == "tilts")
    assert (tag["n"], tag["hits"], tag["filter"]) == (7 * WINDOW, 7 * WINDOW, "post_loss,acted_preflop")
    assert tag["n"] == sum(1 for f in rows if parse_filter(tag["filter"])(f))
    assert tag["pct"] - tag["baseline"] >= 5
    assert not any(t["id"] == "tilts" for t in tags_for(_nights(7, post_vpip=False))["tags"])
    assert not any(t["id"] == "tilts" for t in tags_for(_nights(4, post_vpip=True))["tags"]), "under n"
