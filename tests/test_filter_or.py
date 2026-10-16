"""The filter language's `|`, brackets and value lists, and the term counts.

Every OR here is held to the terms it joins, row by row over the fixture corpus:
`a|b` must match exactly the rows `a` or `b` matches, so a change in precedence
or in how a segment is read shows up as a row that disagrees.
"""

from __future__ import annotations

import pytest

from pnt.stats import filters
from pnt.stats.filters import FLAGS, count_terms, parse_filter
from pnt.stats.queries import display_names, facts_by_player
from tests.conftest import ALL_LOGS, local_client


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    """(every player's Facts, the name map) over the three core logs."""
    from pnt.db.conn import connect
    from pnt.ingest.importer import import_csv

    conn = connect(tmp_path_factory.mktemp("or") / "t.sqlite")
    for path in ALL_LOGS:
        import_csv(conn, path)
    grouped, _, _ = facts_by_player(conn)
    names = display_names(conn)
    conn.close()
    return [f for facts in grouped.values() for f in facts], names


def _hits(facts, expr, names=None) -> list[bool]:
    pred = parse_filter(expr, names)
    return [pred(f) for f in facts]


def _any(facts, exprs, names=None) -> list[bool]:
    cols = [_hits(facts, e, names) for e in exprs]
    return [any(row) for row in zip(*cols)]


def _all(facts, exprs, names=None) -> list[bool]:
    cols = [_hits(facts, e, names) for e in exprs]
    return [all(row) for row in zip(*cols)]


# --- precedence and brackets ---------------------------------------------------


def test_bar_binds_tighter_than_comma(corpus):
    facts, _ = corpus
    got = _hits(facts, "position=BB,3bet_pot|limped")
    pos, three, limped = (_hits(facts, e) for e in ("position=BB", "3bet_pot", "limped"))
    assert got == [p and (t or lp) for p, t, lp in zip(pos, three, limped)]
    assert any(got) and not all(got)
    # Not (BB and 3bet_pot) or limped: that reading would keep every limped pot.
    assert got != [(p and t) or lp for p, t, lp in zip(pos, three, limped)]


def test_cross_term_or(corpus):
    facts, _ = corpus
    for expr in ("3bet_pot|position=BTN", "eff_bb<40|eff_bb>=200", "srp|limped|3bet_pot"):
        assert _hits(facts, expr) == _any(facts, expr.split("|")), expr
    assert _hits(facts, "srp|limped|3bet_pot|4bet_pot") == [True] * len(facts)


def test_brackets_group_and_nest(corpus):
    facts, _ = corpus
    got = _hits(facts, "(srp,position=BTN)|(3bet_pot,position=BB)")
    assert got == _any(facts, ["srp,position=BTN", "3bet_pot,position=BB"])
    assert any(got)
    nested = _hits(facts, "((srp|limped),position=BB)|3bet_pot")
    inner = _all(facts, ["srp|limped", "position=BB"])
    assert nested == [a or b for a, b in zip(inner, _hits(facts, "3bet_pot"))]
    # A bracket around one term is that term, and brackets around the whole filter
    # change nothing.
    assert _hits(facts, "(srp)") == _hits(facts, "srp")
    assert _hits(facts, "(srp,position=BB)") == _hits(facts, "srp,position=BB")


def test_bang_negates_a_bracket(corpus):
    facts, _ = corpus
    got = _hits(facts, "!(srp,position=BB)")
    assert got == [not x for x in _hits(facts, "srp,position=BB")]
    assert _hits(facts, "!(limped)") == _hits(facts, "!limped")
    assert _hits(facts, "! ( limped | srp )") == [not x for x in _hits(facts, "limped|srp")]
    assert _hits(facts, "vpip,!(3bet_pot|4bet_pot)") == _all(facts, ["vpip", "!3bet_pot", "!4bet_pot"])
    # `!flag` is unchanged.
    assert _hits(facts, "!cbet_flop") == [not x for x in _hits(facts, "cbet_flop")]


def test_empty_terms_between_commas_are_still_skipped(corpus):
    facts, _ = corpus
    assert _hits(facts, "srp,,position=BB,") == _hits(facts, "srp,position=BB")
    assert _hits(facts, "") == [True] * len(facts)
    assert _hits(facts, " , ") == [True] * len(facts)


def test_the_tree_is_compiled_once(corpus, monkeypatch):
    """No term is parsed again while rows are tested."""
    facts, _ = corpus
    pred = parse_filter("(srp,position=BTN|CO)|!(3bet_pot,hand=AA|KK)")
    calls = []
    real = filters._compile_term
    monkeypatch.setattr(filters, "_compile_term", lambda *a: calls.append(a) or real(*a))
    for f in facts:
        pred(f)
    assert calls == []


# --- value lists ---------------------------------------------------------------


def _vs_pair(facts, names):
    """Two opponents with hands against them, by display name."""
    from collections import Counter

    seen = Counter(names.get(o, o) for f in facts for o in f.opponents)
    (a, _), (b, _) = seen.most_common(2)
    return a, b


@pytest.mark.parametrize(
    "key, values",
    [
        ("position=", ["BTN", "BB", "btn/sb"]),
        ("fold_street=", ["flop", "turn"]),
        ("hand=", ["AKs", "AA", "72", "T9o"]),
        ("faced_open=", ["call", "raise"]),
        ("unopened=", ["call", "raise"]),
        ("faced_3bet_any=", ["fold", "call"]),
        ("bet_answer_river=", ["fold", "call"]),
        ("faced_check_raise_flop=", ["fold", "call"]),
        ("cbet_flop=", ["small", "medium"]),
        ("faced_bet_river=", ["large", "overbet"]),
        ("line_flop=", ["xc", "b*"]),
        ("line=", ["xc//", "b*/b*/"]),
        ("action_flop=", ["xB*", "*Bc"]),
        ("tempo_river=", ["snap", "tank"]),
        ("tempo_preflop=", ["pre", "normal"]),
        ("flop=", ["paired", "monotone"]),
        ("board=", ["twotone", "trips"]),
    ],
)
def test_value_lists_are_any_of_the_values(corpus, key, values):
    facts, _ = corpus
    got = _hits(facts, key + "|".join(values))
    assert got == _any(facts, [key + v for v in values])
    assert any(got), "a list that matches nothing proves nothing"


def test_vs_takes_a_list_of_names(corpus):
    facts, names = corpus
    a, b = _vs_pair(facts, names)
    got = _hits(facts, f"vs={a}|{b}", names)
    assert got == _any(facts, [f"vs={a}", f"vs={b}"], names)
    assert got != _hits(facts, f"vs={a}", names)


@pytest.mark.parametrize(
    "key, values",
    [
        ("hand!=", ["AA", "KK"]),
        ("flop!=", ["paired", "monotone"]),
        ("board!=", ["twotone", "rainbow"]),
        ("line_flop!=", ["x", "xc"]),
        ("line_river!=", ["b*", "x*"]),
        ("action_flop!=", ["xB*", "*Bc"]),
        ("tempo_river!=", ["snap", "tank"]),
    ],
)
def test_not_equal_lists_are_none_of_the_values(corpus, key, values):
    facts, _ = corpus
    got = _hits(facts, key + "|".join(values))
    assert got == _all(facts, [key + v for v in values])
    assert any(got)


def test_vs_not_equal_list_is_against_neither(corpus):
    facts, names = corpus
    a, b = _vs_pair(facts, names)
    got = _hits(facts, f"vs!={a}|{b}", names)
    assert got == _all(facts, [f"vs!={a}", f"vs!={b}"], names)
    assert got != _hits(facts, f"vs!={a}", names)


def test_a_segment_that_is_not_a_value_is_a_term(corpus):
    facts, _ = corpus
    for expr, parts in [
        ("position=BTN|CO|3bet_pot", ["position=BTN", "position=CO", "3bet_pot"]),
        ("flop=paired|3bet_pot", ["flop=paired", "3bet_pot"]),
        ("hand=AA|limped", ["hand=AA", "limped"]),
        ("faced_open=call|position=BB", ["faced_open=call", "position=BB"]),
        ("position=BTN|faced_open", ["position=BTN", "faced_open"]),
        ("made_flop=set|3bet_pot", ["made_flop=set", "3bet_pot"]),
        ("tier_flop=strong|srp", ["tier_flop=strong", "srp"]),
    ]:
        assert _hits(facts, expr) == _any(facts, parts), expr


def test_a_list_does_not_cross_a_comma_or_a_bracket():
    with pytest.raises(ValueError, match="unknown filter term: 'CO'"):
        parse_filter("position=BTN,CO")
    with pytest.raises(ValueError, match="unknown filter term: 'CO'"):
        parse_filter("(position=BTN)|CO")


def test_no_flag_is_a_value():
    """Every family but `vs=` refuses every flag as a value, so a flag after a list
    is always a term of its own (SPEC.md, "Filter language", Collisions)."""
    keys = [
        "position=", "fold_street=", "hand=", "faced_open=", "bet_answer_river=",
        "faced_check_raise_flop=", "cbet_flop=", "line_flop=", "line=", "action_flop=",
        "tempo_river=", "flop=", "board=", "made_flop=", "tier_flop=",
    ]
    for flag in FLAGS:
        for key in keys:
            with pytest.raises(ValueError):
                parse_filter(key + flag)


def test_a_player_name_wins_over_a_flag_and_brackets_make_it_a_term(corpus):
    facts, names = corpus
    a, _ = _vs_pair(facts, names)
    named = {**names, "nobody-plays-here": "won"}
    # A player called `won` who never sat with anyone: the list adds no rows.
    assert _hits(facts, f"vs={a}|won", named) == _hits(facts, f"vs={a}", named)
    assert _hits(facts, f"vs={a}|(won)", named) == _any(facts, [f"vs={a}", "won"], named)
    # Nobody is called won here, so the segment is the flag.
    assert _hits(facts, f"vs={a}|won", names) == _any(facts, [f"vs={a}", "won"], names)


def test_made_and_tier_bars_are_unchanged(corpus):
    facts, _ = corpus
    for expr in ("made_flop=set|middle_pair", "made=flush|straight|pair", "tier_river=strong|nutted",
                 "tier_flop=draw|air"):
        key, _, rest = expr.partition("=")
        assert _hits(facts, expr) == _any(facts, [f"{key}={v}" for v in rest.split("|")]), expr
    for expr in ("made_flop!=set|pair", "tier_turn!=air|draw"):
        key, _, rest = expr.partition("!=")
        assert _hits(facts, expr) == _all(facts, [f"{key}!={v}" for v in rest.split("|")]), expr


# --- errors --------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr, message",
    [
        ("position=XYZ", "unknown position 'XYZ'"),
        ("position=", "unknown position ''"),
        ("position=BTN|XYZ", "unknown position 'XYZ'"),
        ("faced_open=jam", "unknown decision 'jam'"),
        ("faced_open=call|shove", "unknown decision 'shove'"),
        ("fold_street=showdown", "unknown street"),
        ("flop=paired|wet", "unknown board texture 'wet'"),
        ("tempo_river=snap|slow", "unknown tempo 'slow'"),
        ("hand=AA|ZZ", "unknown hand 'ZZ'"),
        ("made_flop=set|bogus", "unknown made hand 'bogus'"),
        ("made_flop=set|", r"a \| needs a made hand on each side"),
        ("made_flop>=set|flush", "only = and != take several"),
        ("tier_river>=strong|nutted", "only = and != take several"),
        ("pot_bb>=10|20", "a comparison takes one number"),
        ("players=6|7", "a comparison takes one number"),
        ("srp|", r"a \| needs a term on each side"),
        ("|srp", r"a \| needs a term on each side"),
        ("srp||limped", r"a \| needs a term on each side"),
        ("()", "empty brackets"),
        ("!()", "empty brackets"),
        ("(srp", "never closed"),
        ("srp)", r"a \) with no \("),
        ("(srp)(limped)", "put a , or a | before a"),
        ("srp(limped)", "put a , or a | before a"),
        ("!pot>=500", "only a flag"),
        ("3bet_pot|BTN", "unknown filter term: 'BTN'"),
    ],
)
def test_bad_filters_raise(expr, message):
    with pytest.raises(ValueError, match=message):
        parse_filter(expr)


def test_unknown_player_in_a_list_raises(corpus):
    facts, names = corpus
    a, _ = _vs_pair(facts, names)
    with pytest.raises(ValueError, match="unknown player 'no-such-person'"):
        parse_filter(f"vs={a}|no-such-person", names)


def test_the_vocabulary_documents_the_syntax():
    from pnt.stats.filters import vocabulary

    combine = next(g for g in vocabulary()["groups"] if g["name"] == "Combine")
    examples = [t["example"] for t in combine["terms"]]
    assert "3bet_pot|4bet_pot" in examples and "position=BTN|CO" in examples
    assert "!(limped)" in examples
    for e in examples:
        parse_filter(e)


# --- counts --------------------------------------------------------------------


def test_count_terms_equals_a_direct_count(corpus):
    facts, names = corpus
    a, b = _vs_pair(facts, names)
    terms = ["position=BTN|CO", "3bet_pot", f"vs={a}|{b}", "(srp,position=BB)|limped", "position=XYZ", ""]
    for base in ("", "vpip", "srp|3bet_pot"):
        got = count_terms(facts, base, terms, names)
        assert got["base"] == sum(_hits(facts, base, names))
        for term in terms[:4]:
            assert got["counts"][term] == sum(_hits(facts, f"{base},{term}", names)), (base, term)
            assert got["counts"][term] == sum(_hits(facts, f"({base or 'vpip|!vpip'}),({term})", names))
        assert got["counts"]["position=XYZ"] is None
        assert got["counts"][""] == got["base"]
    with pytest.raises(ValueError):
        count_terms(facts, "position=XYZ", terms, names)


@pytest.fixture()
def client(tmp_path, monkeypatch):
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
    return local_client(app_module.app)


def test_counts_endpoint_matches_the_chart_and_the_stats_table(client):
    pytest.importorskip("fastapi")
    from tests.conftest import MULTIWAY_GAME

    # Someone who sat in the multiway game, so both scopes have hands to count.
    player = client.get("/stats", params={"game": MULTIWAY_GAME}).json()[0]["player"]
    terms = ["position=BTN|CO", "3bet_pot", "srp|limped", "!(srp)", "faced_open=call|raise", "position=XYZ"]
    for game in (None, MULTIWAY_GAME):
        for base in ("", "vpip"):
            body = {"player": player, "filter": base, "game": game, "terms": terms}
            mine = client.post("/counts", json=body).json()
            pool = client.post("/counts", json={**body, "player": None}).json()
            assert mine["counts"]["position=XYZ"] is None and pool["counts"]["position=XYZ"] is None
            params = {"game": game} if game else {}
            for term in terms[:-1]:
                spot = ",".join(x for x in (base, term) if x)
                chart = client.get(f"/players/{player}/stats", params={**params, "filter": spot}).json()
                assert mine["counts"][term] == chart["hands"], (game, base, term)
                table = client.get("/stats", params={**params, "filter": spot}).json()
                assert pool["counts"][term] == sum(r["hands"] for r in table), (game, base, term)
            chart = client.get(f"/players/{player}/stats", params={**params, "filter": base}).json()
            assert mine["base"] == chart["hands"] > 0
            assert all(mine["counts"][t] for t in terms[:3]), (game, base)


def test_counts_endpoint_rejects_what_it_cannot_count(client):
    pytest.importorskip("fastapi")
    ok = {"player": None, "filter": "", "game": None, "terms": ["srp"]}
    assert client.post("/counts", json=ok).status_code == 200
    assert client.post("/counts", json={**ok, "filter": "position=XYZ"}).status_code == 400
    assert client.post("/counts", json={**ok, "player": "no such player"}).status_code == 404
    assert client.post("/counts", json={**ok, "terms": ["srp"] * 201}).status_code == 422
    # A POST, so it carries the write header like every other.
    from fastapi.testclient import TestClient

    bare = TestClient(client.app, base_url="http://127.0.0.1")
    assert bare.post("/counts", json=ok).status_code == 403
