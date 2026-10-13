"""Player tags: every rule fires at its threshold and stays silent under its sample.

The rules are pinned on synthetic `Facts` rows so each one is tested in isolation,
and then on the fixture database as an invariant: every tag's filter is a working
spot, and its count is the count of hands that filter finds.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from pnt import cli
from pnt.db.conn import connect
from pnt.ingest.importer import import_csv
from pnt.stats.derive import Facts
from pnt.stats.filters import parse_filter
from pnt.stats.queries import display_names, facts_for, report
from pnt.stats.tags import (
    BALANCED_HANDS,
    MIN_ARCHETYPE_OPPS,
    MIN_SESSIONS,
    PREFLOP_STRENGTH,
    THRESHOLDS,
    TROLL_HANDS,
    counts,
    profile,
    strength,
    strength_at,
    tags_for,
)
from tests.conftest import ALL_LOGS

BOARD = ("Ac", "7s", "2d", "9c", "4h")
STRONG, MEDIUM, WEAK, AIR = "AhAd", "7h3h", "2h3h", "Kh3d"


def mk(i: int = 0, **kw) -> Facts:
    base = {"hand_id": i, "pn_id": "p", "n_dealt_in": 6, "seats_from_button": 0, "dead_button": False}
    return Facts(**{**base, **kw})


def many(n: int, **kw) -> list[Facts]:
    return [mk(i, **kw) for i in range(n)]


def ids(out: dict) -> list[str]:
    return [t["id"] for t in out["tags"]]


def test_strength_buckets_the_shown_hand():
    assert strength(mk(hole_cards=STRONG, board=BOARD)) == "strong"
    assert strength(mk(hole_cards=MEDIUM, board=BOARD)) == "medium"
    assert strength(mk(hole_cards=WEAK, board=BOARD)) == "weak"
    assert strength(mk(hole_cards=AIR, board=BOARD)) == "air"
    assert strength(mk(hole_cards="AhKh", board=("Ac", "Ks", "2d"))) == "strong", "top pair is strong"
    assert strength(mk(hole_cards=None, board=BOARD)) is None
    assert strength(mk(hole_cards=STRONG, board=())) is None


def test_strength_at_reads_the_board_as_it_stood_on_that_street():
    # A flush draw on the flop is a draw; on the river, if it missed, it is air.
    assert strength_at("Kc3c", ("Ac", "7c", "2d", "9h", "4h"), "flop") == "draw"
    assert strength_at("Kc3c", ("Ac", "7c", "2d", "9h", "4h"), "turn") == "draw"
    assert strength_at("Kc3c", ("Ac", "7c", "2d", "9h", "4h"), "river") == "air"
    assert strength_at("Kc3c", ("Ac", "7c", "2d", "9c", "4h"), "turn") == "strong", "and one that got there"
    assert strength_at(AIR, BOARD, "flop") == "air"
    # Top pair on the flop is only second pair once the turn brings an ace.
    assert strength_at("Kh3d", ("Kc", "7s", "2d", "Ah", "4h"), "flop") == "strong"
    assert strength_at("Kh3d", ("Kc", "7s", "2d", "Ah", "4h"), "turn") == "medium"
    assert strength_at(STRONG, BOARD, "river") == "strong"
    assert strength_at(WEAK, BOARD, "flop") == "weak"
    # Unknown when the cards are, or when the board never got that far.
    assert strength_at(None, BOARD, "flop") is None
    assert strength_at(STRONG, BOARD[:3], "turn") is None
    assert strength_at(STRONG, (), "flop") is None
    # Preflop is a percentile, and a board is beside the point.
    assert strength_at("AhAd", (), "preflop") == "strong"
    assert strength_at("Kh3d", BOARD, "preflop") == "medium"
    assert strength_at("7h3h", (), "preflop") == "weak"
    assert PREFLOP_STRENGTH["strong"] < PREFLOP_STRENGTH["medium"]


def test_empty_input_is_the_empty_shape():
    assert tags_for([]) == {"archetype": None, "tags": [], "profile": {"hands": 0}, "streaky": []}


def test_a_rate_tag_needs_its_sample():
    th = THRESHOLDS["folds_river"]
    n = int(th["n"])
    folded = many(n - 1, faced_bet={"river": True}, folded_to_bet={"river": True})
    assert "folds_river" not in ids(tags_for(folded)), "one short of the minimum sample"
    folded.append(mk(99, faced_bet={"river": True}, folded_to_bet={"river": True}))
    out = tags_for(folded)
    tag = next(t for t in out["tags"] if t["id"] == "folds_river")
    assert tag["n"] == n and tag["hits"] == n and tag["pct"] == 100.0
    assert tag["filter"] == "faced_bet_river" and tag["kind"] == "exploit"
    assert "shown" not in tag["tip"], "a rate over every hand is not showdown evidence"


def test_river_bluff_tags_read_the_shown_hands():
    river = {"aggressor": {"river": True}, "wtsd": True, "board": BOARD}
    n = int(THRESHOLDS["no_bluff"]["n"])
    value = many(n, hole_cards=STRONG, **river)
    out = tags_for(value)
    assert "no_bluff" in ids(out) and "bluffs_river" not in ids(out)
    tag = next(t for t in out["tags"] if t["id"] == "no_bluff")
    assert (tag["n"], tag["hits"], tag["by"]) == (n, 0, "made") and "shown" in tag["tip"]
    assert "no_bluff" not in ids(tags_for(value[:-1])), "one short of the minimum sample"
    # A third air is a bluffer; hands without a showdown are not evidence either way.
    bluffs = many(7, hole_cards=STRONG, **river) + many(3, hole_cards=AIR, **river)
    assert "bluffs_river" in ids(tags_for(bluffs)) and "no_bluff" not in ids(tags_for(bluffs))
    unseen = many(20, hole_cards=None, **river)
    assert ids(tags_for(unseen)) == []


def test_calls_down_light_counts_weak_pairs_as_well_as_air():
    call = {"called_bet": {"river": True}, "wtsd": True, "board": BOARD}
    hands = many(6, hole_cards=STRONG, **call) + many(2, hole_cards=WEAK, **call)
    tag = next(t for t in tags_for(hands)["tags"] if t["id"] == "calls_down_light")
    assert (tag["n"], tag["hits"], tag["pct"]) == (8, 2, 25.0)


def test_check_back_tags_are_a_pair_of_opposites():
    cb = {"check_back": {"flop": True}, "wtsd": True, "board": BOARD}
    weak = many(10, hole_cards=WEAK, **cb)
    assert "checks_back_weak" in ids(tags_for(weak)) and "traps" not in ids(tags_for(weak))
    strong = many(10, hole_cards=STRONG, **cb)
    assert "traps" in ids(tags_for(strong)) and "checks_back_weak" not in ids(tags_for(strong))


def test_light_hand_tags_need_a_share_and_exclude_troll_hands():
    th = THRESHOLDS["fourbets_light"]
    junk = many(int(th["hits"]), hole_cards="9h4d", pf_faced={3: "raise"})
    assert "fourbets_light" in ids(tags_for(junk))
    for cls in ("7h2d", "9d2s", "Kh2c"):
        troll = many(int(th["hits"]), hole_cards=cls, pf_faced={3: "raise"})
        assert "fourbets_light" not in ids(tags_for(troll)), f"{cls} is played on purpose"
    diluted = junk + [mk(100 + i, hole_cards="AhAd", pf_faced={3: "raise"}) for i in range(40)]
    assert "fourbets_light" not in ids(tags_for(diluted)), "three junk 4-bets in forty-three is a share, not a habit"
    assert TROLL_HANDS == {"72o", "72s", "92o", "92s", "K2o"}


def test_inelastic_reads_either_the_fold_rate_or_the_shown_calls():
    th = THRESHOLDS["inelastic_vs_3bet"]
    n = int(th["n"])
    sticky = many(n, fold_to_3bet_opp=True, fold_to_3bet=False)
    tag = next(t for t in tags_for(sticky)["tags"] if t["id"] == "inelastic_vs_3bet")
    assert tag["tip"].startswith("Opened and folded") and tag["n"] == n
    calls = many(int(th["hits"]), hole_cards="9h4d", pf_faced={2: "call", 3: "call"})
    tag = next(t for t in tags_for(calls)["tags"] if t["id"] == "inelastic_vs_3bet")
    assert "bottom-40%" in tag["tip"] and "shown" in tag["tip"]
    elastic = many(n, fold_to_3bet_opp=True, fold_to_3bet=True)
    assert "folds_to_3bet" in ids(tags_for(elastic)) and "inelastic_vs_3bet" not in ids(tags_for(elastic))
    # Folding to 3-bets outranks the junk they call one with: one tag, not both.
    both = tags_for(elastic + calls)
    assert "folds_to_3bet" in ids(both) and "inelastic_vs_3bet" not in ids(both)


def test_never_squeezes_needs_its_sample_and_a_near_zero_rate():
    th = THRESHOLDS["never_squeezes"]
    assert th == {"pct": 3, "n": 25}
    n = int(th["n"])
    flats = many(n - 1, callers_before=1, pf_faced={2: "call"})
    assert "never_squeezes" not in ids(tags_for(flats)), "one short of the minimum sample"
    flats.append(mk(99, callers_before=2, pf_faced={2: "fold"}))
    tag = next(t for t in tags_for(flats)["tags"] if t["id"] == "never_squeezes")
    assert (tag["n"], tag["hits"], tag["pct"], tag["kind"]) == (n, 0, 0.0, "exploit")
    assert tag["filter"] == "squeeze_opp" and tag["label"] == "NEVER SQUEEZES"
    assert "shown" not in tag["tip"]
    # One squeeze in 25 is 4%: over the line.
    flats[0] = mk(0, callers_before=1, pf_faced={2: "raise"})
    assert "never_squeezes" not in ids(tags_for(flats))
    # Facing an open with no caller in front is not a squeeze chance at all.
    assert ids(tags_for(many(40, callers_before=0, pf_faced={2: "call"}))) == []


def test_never_squeezes_reproduces_from_its_filter(db, monkeypatch):
    """No fixture player is under the line, so it is lowered until every player
    with a chance fires, and each tag's n and hits are the filters' counts."""
    monkeypatch.setitem(THRESHOLDS, "never_squeezes", {"pct": 100, "n": 1})
    names = display_names(db)
    opp, hit = parse_filter("squeeze_opp", names), parse_filter("squeeze", names)
    fired = 0
    for row in report(db):
        facts = facts_for(db, row["player"])
        out = tags_for(facts)
        # A player whose chances all fall in one session gets it as streaky.
        tag = next((t for t in out["tags"] + out["streaky"] if t["id"] == "never_squeezes"), None)
        n = sum(1 for f in facts if opp(f))
        assert (tag is not None) == (n > 0), row["player"]
        if tag:
            fired += 1
            assert tag["n"] == n and tag["hits"] == sum(1 for f in facts if hit(f))
    assert fired


def test_fun_tags_prefer_the_jam_and_write_a_hand_filter():
    raised = many(3, hole_cards="7h2d", vpip_opp=True, vpip=True, pfr=True, pf_faced={1: "raise"})
    out = tags_for(raised)
    fun = [t for t in out["tags"] if t["kind"] == "fun"]
    assert [t["label"] for t in fun] == ["7-2 RAISES ×3"] and fun[0]["filter"] == "hand=72,pfr"
    raised[0].jam["preflop"] = True
    fun = next(t for t in tags_for(raised)["tags"] if t["kind"] == "fun")
    assert fun["label"] == "7-2 ALL-IN-PRE ×1" and fun["filter"] == "hand=72,jam_preflop" and fun["n"] == 1
    assert "raised it 3 times" in fun["tip"]
    assert ids(tags_for(raised[1:2])) == [], "one plain raise is not a habit; one jam is a story"


def _preflop(n: int, vpip_pct: float, pfr_pct: float, **kw) -> list[Facts]:
    rows = []
    for i in range(n):
        v = i < n * vpip_pct / 100
        r = i < n * pfr_pct / 100
        rows.append(mk(i, vpip_opp=True, vpip=v, pfr=r, pf_faced={1: "raise" if r else "call" if v else "fold"}, **kw))
    return rows


def test_archetype_needs_a_sample_and_follows_precedence():
    n = MIN_ARCHETYPE_OPPS
    assert tags_for(_preflop(n - 1, 60, 10))["archetype"] is None
    # Six-handed baseline is 30/21. Loose and passive is a fish...
    fish = tags_for(_preflop(n, 60, 10))
    assert fish["archetype"]["id"] == "fish" and fish["tags"][0] is fish["archetype"]
    assert fish["profile"]["vpip_excess"] == pytest.approx(30, abs=0.5)
    # ...unless they also raise far more than the table and 3-bet a lot: a maniac wins.
    maniac = _preflop(n, 60, 45)
    for f in maniac[:20]:
        f.three_bet_opp = f.three_bet = True
    assert tags_for(maniac)["archetype"]["id"] == "maniac"
    assert tags_for(_preflop(n, 45, 35))["archetype"]["id"] == "lag"
    assert tags_for(_preflop(n, 10, 5))["archetype"]["id"] == "nit"
    assert tags_for(_preflop(n, 30, 20))["archetype"]["id"] == "tag"
    assert tags_for(_preflop(n, 30, 12))["archetype"]["id"] == "passive"
    # Heads-up, 60% VPIP is under the baseline: the same numbers read as a nit.
    assert tags_for(_preflop(n, 50, 40, n_dealt_in=2))["archetype"]["id"] == "nit"


def test_balanced_needs_the_hands_and_no_exploit():
    reg = _preflop(BALANCED_HANDS, 30, 20)
    assert tags_for(reg)["archetype"]["id"] == "balanced"
    assert tags_for(reg[:-1])["archetype"]["id"] == "tag"
    for f in reg[:15]:
        f.faced_bet["river"] = f.folded_to_bet["river"] = True
    out = tags_for(reg)
    assert out["archetype"]["id"] == "fit_or_fold" and "folds_river" in ids(out)


def test_an_unplaced_player_is_named_for_their_exploit_tags():
    rows = _preflop(MIN_ARCHETYPE_OPPS, 30, 20)
    river = {"aggressor": {"river": True}, "wtsd": True, "board": BOARD, "hole_cards": STRONG}
    cb = {"check_back": {"flop": True}, "wtsd": True, "board": BOARD, "hole_cards": STRONG}
    # One tag each for FIT OR FOLD (no bluff, 10 shown) and TRAPPER (traps, 8 shown)...
    tagged = rows + many(10, **river) + many(8, **cb)
    out = tags_for(tagged)
    assert {"no_bluff", "traps"} <= set(ids(out))
    assert out["archetype"]["id"] == "fit_or_fold", "a tie goes to the tag on more hands"
    assert "Bet when they check" in out["archetype"]["tip"]
    # ...and a second TRAPPER tag outweighs it.
    for f in many(25, cbet_opp={"flop": True}):
        tagged.append(f)
    out = tags_for(tagged)
    assert "rarely_cbets" in ids(out) and out["archetype"]["id"] == "trapper"


def _sessions(n: int, per: int, **kw) -> list[Facts]:
    return [mk(g * per + i, game_id=f"g{g}", **kw) for g in range(n) for i in range(per)]


def test_a_tag_one_session_carries_is_streaky():
    folds = {"faced_bet": {"river": True}, "folded_to_bet": {"river": True}}
    calls = {"faced_bet": {"river": True}}
    # 20 folds in one night and calls every other night: 20 of 36 fires, 0 of 16 does not.
    one_night = _sessions(1, 20, **folds) + [mk(100 + i, game_id=f"h{i % 4}", **calls) for i in range(16)]
    out = tags_for(one_night)
    assert "folds_river" not in ids(out)
    [tag] = out["streaky"]
    assert tag["id"] == "folds_river" and tag["carried_by"] == {"game_id": "g0", "hands": 20, "sessions": 5}
    # The same folds spread over four nights hold without any one of them.
    habit = _sessions(4, 5, **folds) + [mk(100 + i, game_id=f"g{i % 4}", **calls) for i in range(16)]
    out = tags_for(habit)
    assert "folds_river" in ids(out) and out["streaky"] == []
    # Under MIN_SESSIONS there is nothing to hold a tag against.
    two = _sessions(1, 20, **folds) + [mk(100 + i, game_id="h", **calls) for i in range(16)]
    assert MIN_SESSIONS == 3 and "folds_river" in ids(tags_for(two))


def test_counts_add_across_sessions():
    rows = _preflop(MIN_ARCHETYPE_OPPS, 30, 20, n_dealt_in=4) + _preflop(MIN_ARCHETYPE_OPPS, 10, 5, n_dealt_in=6)
    a, b = counts(rows[:MIN_ARCHETYPE_OPPS]), counts(rows[MIN_ARCHETYPE_OPPS:])
    both = a.copy()
    both.update(b)
    assert profile(both) == profile(rows), "negative excess sums must survive the addition"
    assert profile(rows)["vpip_excess"] < 0


def test_the_size_tell_compares_big_bets_against_small_ones():
    shown = {"wtsd": True, "board": BOARD}
    big = many(6, hole_cards=STRONG, bet_size={"river": "large"}, **shown)
    small = many(6, hole_cards=AIR, bet_size={"river": "small"}, **shown)
    out = tags_for(big + small)
    tag = next(t for t in out["tags"] if t["id"] == "size_tell")
    assert tag["label"] == "BIG = STRONG" and tag["kind"] == "exploit"
    assert (tag["by"], tag["street"], tag["bet_kind"]) == ("sizing", "river", "bet")


def test_station_is_read_from_the_flop_and_showdown():
    rows = _preflop(MIN_ARCHETYPE_OPPS, 60, 10)
    for f in rows[:40]:
        f.saw_flop = f.wtsd_opp = f.wtsd = True
    for f in rows[:25]:
        f.fold_to_cbet_opp["flop"] = True
    assert tags_for(rows)["archetype"]["id"] == "station"


# ------------------------------------------------------------ invariant -----

#: Tags whose `n` is exactly the hands their filter finds.
EXACT = {"folds_river", "folds_to_cbet", "sticky_vs_cbet", "auto_cbet", "rarely_cbets", "folds_to_3bet",
         "slowplays", "never_squeezes"}
#: Tags whose `hits` is exactly the hands their filter finds.
HITS = {"limper", "donks"}
#: Tags judged on shown hands: `n` is the hands their filter finds with cards known.
SHOWN = {"no_bluff", "bluffs_river", "calls_down_light", "checks_back_weak", "traps"}
#: Tags judged on voluntary shows (`shows.show_tags`): not `Facts`, so no filter.
#: `n` is the player's two-card rows of `v_voluntary_shows`, and `hand_ids` the hits.
VOLUNTARY = {"shows_bluffs", "shows_nuts"}


def _summary(**classes) -> dict:
    cls = {c: classes.get(c, 0) for c in ("strong", "marginal", "draw", "air")}
    rows = [{"hand_id": i, "class": c} for c, k in cls.items() for i in range(k)]
    return {"two_card": sum(cls.values()), "classes": cls, "rows": rows}


def test_show_tags_need_a_count_and_more_than_half():
    from pnt.stats.shows import show_tags

    t = THRESHOLDS["shows"]
    assert t == {"hits": 3, "pct": 50}
    assert [x["id"] for x in show_tags(_summary(air=2, draw=1, strong=2))] == ["shows_bluffs"]
    assert show_tags(_summary(air=2, strong=1)) == [], "two is under the count"
    assert show_tags(_summary(air=3, strong=3)) == [], "half is not more than half"
    nuts = show_tags(_summary(strong=4, marginal=1))
    assert [x["id"] for x in nuts] == ["shows_nuts"]
    assert (nuts[0]["n"], nuts[0]["hits"], nuts[0]["kind"], nuts[0]["label"]) == (5, 4, "fun", "SHOWS NUTS ×4")
    assert show_tags(_summary()) == []


def test_every_show_tag_reproduces_from_the_view(db):
    from pnt.stats.queries import identities_of
    from pnt.stats.shows import with_show_tags

    seen = set()
    for row in report(db):
        alias = row["player"]
        out = with_show_tags(db, alias, tags_for(facts_for(db, alias)))
        ids = identities_of(db, alias)
        q = ",".join("?" * len(ids))
        two = {
            r[0] for r in db.execute(
                f"SELECT hand_id FROM v_voluntary_shows WHERE n_cards = 2 AND pn_id IN ({q})", ids
            )
        }
        for tag in out["tags"]:
            if tag["id"] not in VOLUNTARY:
                continue
            seen.add(tag["id"])
            assert tag["kind"] == "fun" and tag["by"] == "shows" and tag["filter"] == ""
            assert tag["n"] == len(two)
            assert tag["hits"] == len(tag["hand_ids"]) and set(tag["hand_ids"]) <= two
            assert tag is out["tags"][-1] or out["tags"][-1]["id"] in VOLUNTARY
    assert "shows_nuts" in seen, "HSJ shows five strong hands of eight in the fixtures"


def test_every_tag_reproduces_on_the_chart(db):
    names = display_names(db)
    seen, archetypes = set(), set()
    for row in report(db):
        facts = facts_for(db, row["player"])
        out = tags_for(facts)
        assert {"archetype", "tags", "profile"} <= set(out)
        for tag in out["tags"]:
            seen.add(tag["id"])
            assert {"id", "label", "kind", "tip", "n", "hits", "pct", "filter", "by"} <= set(tag)
            pred = parse_filter(tag["filter"], names)  # every filter is a working spot
            count = sum(1 for f in facts if pred(f))
            known = sum(1 for f in facts if pred(f) and f.hole_cards)
            if tag["id"] in EXACT:
                assert tag["n"] == count, tag
            elif tag["id"] in HITS:
                assert tag["hits"] == count, tag
            elif tag["id"] in SHOWN or tag["kind"] == "fun":
                assert tag["n"] == known, tag
            elif tag["kind"] == "archetype":
                assert tag["n"] == count == out["profile"]["vpip_opp"]
        if out["archetype"]:
            archetypes.add(out["archetype"]["id"])
            assert out["tags"][0] is out["archetype"]
            assert all(t["kind"] != "fun" or i >= len(out["tags"]) - 3 for i, t in enumerate(out["tags"]))
    assert archetypes, "the fixtures have players with 100+ decisions"


def test_cli_prints_every_player_and_json_for_one(tmp_path):
    path = tmp_path / "tags.sqlite"
    conn = connect(path)
    for log in ALL_LOGS:
        import_csv(conn, log)
    conn.close()
    result = CliRunner().invoke(cli.app, ["tags", "--db", str(path)])
    assert result.exit_code == 0, result.output
    assert "genericpoker" in result.output and "hands" in result.output
    result = CliRunner().invoke(cli.app, ["tags", "genericpoker", "--json", "--db", str(path)])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["player"] == "genericpoker" and {"archetype", "tags", "profile"} <= set(body)
    result = CliRunner().invoke(cli.app, ["tags", "nobody", "--db", str(path)])
    assert result.exit_code != 0 and "unknown alias" in result.output
