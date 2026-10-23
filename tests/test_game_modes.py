"""Bomb pots, Double Board and PLO, shaped exactly like pgl1Wk5jGXImZ1BZ5QCdUwt6j.

A bomb pot has no blinds and no preflop betting: everyone's buy-in is forced, so
no preflop rate counts the hand, and its big blind comes from the table's
settings. A PLO hand counts for money only -- every rate, node and review rule
assumes two hole cards.
"""

from __future__ import annotations

from pnt.ingest.csv_source import RawEntry
from pnt.logfmt import events as E
from pnt.logfmt.grammar import classify
from pnt.logfmt.parser import parse
from pnt.stats.derive import derive
from pnt.stats.live import hand_row
from pnt.stats.nodes import nodes_for

GAME = "pgltest_modes"


def _hand(variant: str, double: bool = True) -> list[str]:
    lines = [
        "Game Config Changes\n* Bomb Pot: off » on\n* Bomb Pot BB Quantity: off » 3",
        f'-- starting hand #1 (id: aaaa)  {variant} (dealer: "B @ BBB") --',
        'Player stacks: #1 "A @ AAA" (1000) | #2 "B @ BBB" (1000)',
        '"B @ BBB" posts a bet of 30 (bomb pot bet)',
        '"A @ AAA" calls 30 (bomb pot bet)',
        "Flop:  [A♥, 5♥, 3♦]",
        "Flop (second board):  [5♠, 6♠, 8♠]",
        '"A @ AAA" bets 60',
        '"B @ BBB" folds',
        'Uncalled bet of 60 returned to "A @ AAA"',
        '"A @ AAA" collected 60 from pot',
        "-- ending hand #1 --",
        "Undealt cards (second board): 5♠, 6♠, 8♠ [K♥, 9♦]",
    ]
    return lines if double else [t for t in lines if "second board" not in t]


def _parsed(variant: str = "No Limit Texas Hold'em", double: bool = True):
    lines = _hand(variant, double)
    return parse([RawEntry(ord=i, at="2026-01-01T00:00:00Z", entry=t) for i, t in enumerate(lines)], GAME)


def test_bomb_pot_bets_are_forced_posts():
    for line in ('"B @ BBB" posts a bet of 30 (bomb pot bet)', '"A @ AAA" calls 30 (bomb pot bet)'):
        ev = classify(line, 0)
        assert isinstance(ev, E.Post) and ev.kind == E.POST_BOMB and ev.amount == 30
    short = classify('"A @ AAA" calls 12 and go all in (bomb pot bet)', 0)
    assert isinstance(short, E.Post) and short.all_in and short.amount == 12
    # pglTZnz_2SBvWvOokUiP_uLi2: a one-chip stack's post, two spaces before the label.
    short = classify('"B @ BBB" posts a bet of 1 and go all in  (bomb pot bet)', 0)
    assert isinstance(short, E.Post) and short.kind == E.POST_BOMB and short.all_in and short.amount == 1


def test_a_second_board_rabbit_hunt_is_noise():
    ev = classify("Undealt cards (second board): K♦, 5♠, K♥ [Q♣, 9♦]", 0)
    assert isinstance(ev, E.Noise) and ev.kind == "rabbit_hunt"


def test_a_bomb_pot_balances_and_takes_its_big_blind_from_the_settings():
    res = _parsed()
    assert res.misses == []
    h = res.hands[0]
    assert h.total_contributed == h.total_collected == 60
    assert (h.players["AAA"].net, h.players["BBB"].net) == (30, -30)
    assert h.bb == 10, "30 is a 3 big blind bomb"


def test_a_bomb_pot_counts_in_no_preflop_rate():
    """On one board it is analysed: no preflop decision, its postflop play counted."""
    f = {x.pn_id: x for x in derive(hand_row(_parsed(double=False).hands[0]))}
    assert not any(x.vpip_opp or x.pfr_opp for x in f.values())
    assert f["AAA"].analysed and f["AAA"].saw_flop and f["AAA"].net == 30
    assert f["AAA"].bet_size.get("flop") and f["AAA"].hole_cards is None  # unknown, not hidden


def test_double_board_counts_for_money_only():
    """Two boards are past every rule here: money and the hand lists, like PLO."""
    h = hand_row(_parsed().hands[0])
    money = {x.pn_id: x for x in derive(h)}["AAA"]
    full = {x.pn_id: x for x in derive(h, every_game=True)}["AAA"]
    assert money.holdem and not money.analysed and money.net == 30
    assert not money.vpip_opp and not money.cbet_opp and money.pot_level == 0
    assert full.saw_flop and full.pot_level == 0 and full.board2 == ("5s", "6s", "8s")


def test_plo_counts_for_money_only():
    h = hand_row(_parsed("Pot Limit Omaha 5 Hi").hands[0])
    assert h.variant == "Pot Limit Omaha 5 Hi"
    f = {x.pn_id: x for x in derive(h)}
    assert (f["AAA"].net, f["BBB"].net) == (30, -30)
    assert not f["AAA"].vpip_opp and not f["AAA"].cbet_opp
    assert all(not path for path in nodes_for(h).values())


def test_plo_variant_reaches_the_database(tmp_path):
    from pnt.db.conn import connect
    from pnt.ingest.importer import ingest_entries, rebuild_game
    from pnt.stats.queries import load_hands

    lines = _hand("Pot Limit Omaha Hi")
    entries = [RawEntry(ord=i, at="2026-01-01T00:00:00Z", entry=t) for i, t in enumerate(lines)]
    conn = connect(tmp_path / "m.sqlite")
    ingest_entries(conn, GAME, entries, "test")
    rebuild_game(conn, GAME)
    hand = load_hands(conn, GAME)[0]
    conn.close()
    assert hand.variant == "Pot Limit Omaha Hi" and hand.bb == 10
    assert not any(x.vpip_opp for x in derive(hand))


# --- PLO in the hand lists, money only everywhere else -------------------------

PLO_SHOWDOWN = [
    '-- starting hand #{n} (id: p{n})  Pot Limit Omaha 5 Hi (dealer: "B @ BBB") --',
    'Player stacks: #1 "A @ AAA" (1000) | #2 "B @ BBB" (1000)',
    "Your hand is 5♦, 9♥, 9♦, 2♣, 5♥",
    '"B @ BBB" posts a small blind of 10',
    '"A @ AAA" posts a big blind of 20',
    '"B @ BBB" calls 20',
    '"A @ AAA" checks',
    "Flop:  [8♦, Q♦, 5♠]",
    '"A @ AAA" bets 40',
    '"B @ BBB" calls 40',
    "Turn: 8♦, Q♦, 5♠ [10♦]",
    '"A @ AAA" checks',
    '"B @ BBB" checks',
    "River: 8♦, Q♦, 5♠, 10♦ [3♣]",
    '"A @ AAA" checks',
    '"B @ BBB" checks',
    '"A @ AAA" shows a 5♦, 9♥, 9♦, 2♣, 5♥.',
    '"B @ BBB" shows a 4♥, J♦, 7♦, 10♥, K♣.',
    '"B @ BBB" collected 120 from pot with Flush, Q High (combination: 7♦, 8♦, 10♦, J♦, Q♦)',
    "-- ending hand #{n} --",
]


def _plo_entries(n_hands: int = 3) -> list[RawEntry]:
    lines = [t.replace("{n}", str(n)) for n in range(1, n_hands + 1) for t in PLO_SHOWDOWN]
    return [RawEntry(ord=i, at="2026-01-01T00:00:00Z", entry=t) for i, t in enumerate(lines)]


def test_plo_showdown_cards_are_kept_and_name_the_hero():
    from pnt.logfmt.hero import infer_hero

    hands = parse(_plo_entries(), GAME).hands
    assert hands[0].players["AAA"].hole_cards == ("5d", "9h", "9d", "2c", "5h")
    guess = infer_hero(hands)
    assert guess.pn_id == "AAA" and guess.confident


def test_plo_is_listed_in_full_but_counted_for_money_only():
    h = hand_row(_parse_one(_plo_entries(1)))
    money = {f.pn_id: f for f in derive(h)}["AAA"]
    full = {f.pn_id: f for f in derive(h, every_game=True)}["AAA"]
    assert money.net == full.net == -60
    # Who saw the flop and the showdown is kept, for Results' lines; no opportunity is.
    assert not money.vpip_opp and money.saw_flop and money.wtsd and money.hole_cards is None
    assert money.pot_level == 0 and not money.holdem
    assert full.saw_flop and full.vpip_opp and len(full.cards) == 10
    # Every card reader reads `hole_cards`, so PLO's five never reach one, and no
    # pot-type term matches a PLO hand even listed in full.
    assert full.hole_cards is None and full.pot_level == 0


def _parse_one(entries):
    return parse(entries, GAME).hands[0]


def test_bomb_pot_is_its_own_pot_type():
    from pnt.stats.filters import parse_filter

    f = {x.pn_id: x for x in derive(hand_row(_parsed().hands[0]))}["AAA"]
    assert f.bomb_pot and f.double_board and f.pot_level == 0
    assert f.board == ("Ah", "5h", "3d") and f.board2 == ("5s", "6s", "8s"), "the second board rides along"
    assert not parse_filter("limped")(f)
    assert parse_filter("bomb_pot,double_board,holdem")(f)
    assert not parse_filter("plo")(f)


def test_game_filters_and_results_rows():
    from pnt.stats.filters import parse_filter
    from pnt.stats.results import results_breakdown

    plo = derive(hand_row(_parse_one(_plo_entries(1))))
    double = derive(hand_row(_parsed().hands[0]))
    bomb = derive(hand_row(_parsed(double=False).hands[0]))
    assert all(parse_filter("plo")(f) for f in plo)
    rows = results_breakdown([*plo, *double, *bomb])["tables"][0]["rows"]
    pots = {r["key"]: r["hands"] for r in rows}
    assert pots["plo"] == 2 and pots["double"] == 2 and pots["bomb"] == 2
    # Each row's filter finds exactly its hands.
    for r in rows:
        if r["hands"]:
            pred = parse_filter(r["filter"])
            assert sum(pred(f) for f in [*plo, *double, *bomb]) == r["hands"], r["key"]
    assert "walk" not in pots or pots["walk"] == 0


def test_the_nit_board_and_the_session_list(tmp_path, monkeypatch):
    import importlib

    from pnt.db.conn import connect
    from pnt.ingest.importer import ingest_entries, rebuild_game
    from pnt.server import app as app_module
    from tests.conftest import local_client
    from tests.test_nit_game import LINES as NIT_LINES

    path = tmp_path / "api.sqlite"
    monkeypatch.setenv("PNT_DB", str(path))
    importlib.reload(app_module)
    conn = connect(path)
    ingest_entries(conn, "pgltest_nitgame", [RawEntry(ord=i, at="t", entry=t) for i, t in enumerate(NIT_LINES)], "test")
    rebuild_game(conn, "pgltest_nitgame")
    ingest_entries(conn, GAME, _plo_entries(), "test")
    rebuild_game(conn, GAME)
    conn.close()

    client = local_client(app_module.app)
    nit = client.get("/nit").json()
    assert nit["balance"] == 0 and nit["total"]["penalties"] == 1
    assert {r["player"]: r["net"]["chips"] for r in nit["players"]} == {"Win": 40, "Two": 40, "Pay": -80}
    # AAA sat in both games, named "Win" in the first.
    resp = client.get("/players/Win/hands", params={"game": GAME}).json()
    session = resp["hands"]
    assert len(session) == 3 and all(r["saw_flop"] and len(r["hole_cards"]) == 10 for r in session)
    assert client.get("/players/Win/review", params={"game": GAME}).json()["hands"] == []
    grid = client.get("/players/Win/range", params={"game": GAME}).json()
    # Not in the range chart's spot at all; in Results, for its money.
    assert grid["hands"] == 0
    results = client.get("/players/Win/results", params={"game": GAME}).json()
    assert results["total"]["hands"] == 3


def test_card_terms_on_a_list_with_plo_answer_rather_than_crash(tmp_path, monkeypatch):
    """`hand=`, `hand_pct` and the strength terms read `hole_cards`: a Session list
    holding PLO hands answers them, the PLO hands simply unknown."""
    import importlib

    from pnt.db.conn import connect
    from pnt.ingest.importer import ingest_entries, rebuild_game
    from pnt.server import app as app_module
    from tests.conftest import local_client

    path = tmp_path / "api.sqlite"
    monkeypatch.setenv("PNT_DB", str(path))
    importlib.reload(app_module)
    conn = connect(path)
    ingest_entries(conn, GAME, _plo_entries(), "test")
    rebuild_game(conn, GAME)
    conn.close()
    client = local_client(app_module.app)
    for spot in ("hand=AKs", "hand_pct>=60", "srp", "limped", "plo"):
        r = client.get("/players/A/hands", params={"game": GAME, "filter": spot})
        assert r.status_code == 200, (spot, r.text)
        assert len(r.json()["hands"]) == (3 if spot == "plo" else 0), spot
    counts = client.post("/counts", json={"player": "A", "games": "every", "terms": ["plo", "holdem"]}).json()
    assert counts["counts"] == {"plo": 3, "holdem": 0}
    assert client.post("/counts", json={"player": "A", "terms": ["plo"]}).json()["counts"] == {"plo": 0}


def test_tree_names_a_bomb_pot():
    from pnt.stats.tree import _POT_TERM, _POT_WORDS

    assert _POT_WORDS[_POT_TERM[0]] == "bomb pot"


def test_holdem_and_plo_replace_each_other_in_the_box():
    from pnt.stats.filters import EXCLUSIVE

    assert ("holdem", "plo") in EXCLUSIVE and ("nit_won", "nit_paid") in EXCLUSIVE


def test_a_live_bomb_pot_reads_its_big_blind_from_the_settings(tmp_path):
    from pnt.db.conn import connect
    from pnt.ingest.importer import ingest_entries
    from pnt.stats.live import live_hand

    lines = _hand("No Limit Texas Hold'em")[:7]  # mid-flop: the hand is live
    conn = connect(tmp_path / "live.sqlite")
    ingest_entries(conn, GAME, [RawEntry(ord=i, at="t", entry=t) for i, t in enumerate(lines)], "test")
    hand = live_hand(conn, GAME)
    conn.close()
    assert hand is not None and hand.bb == 10


def _blinds_hand(n: int) -> list[str]:
    """A plain Hold'em hand at 10/20: B raises, A folds."""
    return [
        f'-- starting hand #{n} (id: h{n})  No Limit Texas Hold\'em (dealer: "B @ BBB") --',
        'Player stacks: #1 "A @ AAA" (1000) | #2 "B @ BBB" (1000)',
        '"B @ BBB" posts a small blind of 10',
        '"A @ AAA" posts a big blind of 20',
        '"B @ BBB" raises to 60',
        '"A @ AAA" folds',
        'Uncalled bet of 40 returned to "B @ BBB"',
        '"B @ BBB" collected 40 from pot',
        f"-- ending hand #{n} --",
    ]


def test_a_live_bomb_pot_follows_a_blind_change_with_no_quantity_line(tmp_path):
    """No `Bomb Pot BB Quantity` on record: the table's big blind as `parse` keeps
    it -- here a blind change after the last stored hand, not that hand's 20."""
    from pnt.db.conn import connect
    from pnt.ingest.importer import ingest_entries, rebuild_game
    from pnt.stats.live import live_hand, to_act

    lines = [
        *_blinds_hand(1),
        "The game's big blind was changed from 20 to 50.",
        '-- starting hand #2 (id: h2)  No Limit Texas Hold\'em (dealer: "A @ AAA") --',
        'Player stacks: #1 "A @ AAA" (980) | #2 "B @ BBB" (1020)',
        '"A @ AAA" posts a bet of 150 (bomb pot bet)',
        '"B @ BBB" calls 150 (bomb pot bet)',
    ]
    conn = connect(tmp_path / "live.sqlite")
    ingest_entries(conn, GAME, [RawEntry(ord=i, at="t", entry=t) for i, t in enumerate(lines)], "test")
    rebuild_game(conn, GAME)
    hand = live_hand(conn, GAME)
    assert hand is not None and hand.bb == 50
    # Everyone posted and the flop is next: nobody has a preflop decision.
    assert to_act(hand) is None
    assert live_hand(conn, GAME).bb == 50, "asked again, from the kept table state"
    conn.close()


def test_a_list_opened_from_a_tab_lists_that_tab_s_hands(tmp_path, monkeypatch):
    import importlib

    from pnt.db.conn import connect
    from pnt.ingest.importer import ingest_entries, rebuild_game
    from pnt.server import app as app_module
    from tests.conftest import local_client

    path = tmp_path / "api.sqlite"
    monkeypatch.setenv("PNT_DB", str(path))
    importlib.reload(app_module)
    conn = connect(path)
    ingest_entries(conn, GAME, _plo_entries(), "test")
    rebuild_game(conn, GAME)
    conn.close()
    client = local_client(app_module.app)

    def hands(games: str) -> list:
        return client.get("/players/A/hands", params={"games": games}).json()["hands"]

    assert len(hands("every")) == len(hands("money")) == 3
    assert hands("play") == [], "an analysis tab's count has no PLO hand, nor does its list"
    pots = client.get("/pots", params={"all_time": True, "min_pot": 0}).json()["pots"]
    assert len(pots) == 3 and all(r["plo"] and not r["double_board"] for r in pots)


def test_a_database_parsed_before_game_modes_asks_for_a_rebuild(tmp_path):
    from pnt.db.conn import connect
    from pnt.ingest.importer import ingest_entries, needs_game_modes_rebuild, rebuild_game

    conn = connect(tmp_path / "old.sqlite")
    assert not needs_game_modes_rebuild(conn), "an empty database has nothing to rebuild"
    ingest_entries(conn, GAME, _plo_entries(1), "test")
    rebuild_game(conn, GAME)
    assert not needs_game_modes_rebuild(conn)
    conn.execute("UPDATE hands SET variant = NULL")  # as stored before the column existed
    assert needs_game_modes_rebuild(conn)
    conn.close()
