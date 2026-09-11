"""Cards shown after a hand ended: kept, and kept apart from showdown cards.

PokerNow writes a voluntary show *after* ``-- ending hand #N --`` -- the same slot
as the 7-2 bounty -- so a parser that closes the hand at that line drops every one
of them. Across the fixtures that was 157 show lines with none recorded.

Recording them into `hole_cards` would be the second bug. Showdown cards are the
hands that got there; voluntary shows are the ones a player chose to reveal, and
every range view would inherit that bias without saying so.
"""

from __future__ import annotations

from pnt.ingest.csv_source import RawEntry, game_id_from_filename, read_csv
from pnt.ingest.importer import rebuild_game
from pnt.logfmt import events as E
from pnt.logfmt.grammar import classify
from pnt.logfmt.parser import parse
from tests.conftest import ALL_LOGS, EDGE_LOGS, MULTIWAY

GAME = "pgltest_shows"
MULTIWAY_GAME = game_id_from_filename(MULTIWAY)

#: Heads-up. Hand #1: Ann raises, Bob folds and then shows his fold; Ann shows one
#: card, then the other. Hand #2 is a showdown (streets elided -- only where the
#: show lines sit matters here), so its cards belong in `hole_cards` as before.
LINES = [
    '-- starting hand #1 (id: aaaa) No Limit Texas Hold\'em (dealer: "Ann @ AAA") --',
    'Player stacks: #1 "Ann @ AAA" (1000) | #2 "Bob @ BBB" (1000)',
    '"Ann @ AAA" posts a small blind of 10',
    '"Bob @ BBB" posts a big blind of 20',
    '"Ann @ AAA" raises to 60',
    '"Bob @ BBB" folds',
    'Uncalled bet of 40 returned to "Ann @ AAA"',
    '"Ann @ AAA" collected 40 from pot',
    "-- ending hand #1 --",
    '"Bob @ BBB" shows a K♠, Q♠.',
    '"Ann @ AAA" shows a 7♦.',
    '"Ann @ AAA" shows a 2♣.',
    '-- starting hand #2 (id: bbbb) No Limit Texas Hold\'em (dealer: "Bob @ BBB") --',
    'Player stacks: #1 "Ann @ AAA" (1020) | #2 "Bob @ BBB" (980)',
    '"Bob @ BBB" posts a small blind of 10',
    '"Ann @ AAA" posts a big blind of 20',
    '"Bob @ BBB" calls 20',
    '"Ann @ AAA" checks',
    '"Ann @ AAA" shows a A♠, A♥.',
    '"Bob @ BBB" shows a 9♦, 8♦.',
    '"Ann @ AAA" collected 40 from pot',
    "-- ending hand #2 --",
]

END_1 = LINES.index("-- ending hand #1 --")


def _parse(lines):
    return parse(
        [RawEntry(ord=i, at="2026-01-01T00:00:00Z", entry=t) for i, t in enumerate(lines)],
        GAME,
    )


def test_show_lines_are_recognized():
    ev = classify('"Bob @ BBB" shows a K♠, Q♠.', 0)
    assert isinstance(ev, E.Shows) and ev.cards == ("Ks", "Qs")


def test_a_fold_shown_after_the_hand_is_kept():
    res = _parse(LINES)
    assert res.misses == []
    h = res.hands[0]
    assert h.players["BBB"].folded
    assert h.voluntary_shows["BBB"].cards == ["Ks", "Qs"]


def test_one_card_then_the_other_is_one_show():
    """Two lines, one row: the union of the cards, stamped with the later line."""
    s = _parse(LINES).hands[0].voluntary_shows["AAA"]
    assert s.cards == ["7d", "2c"]
    assert s.ord == LINES.index('"Ann @ AAA" shows a 2♣.')


def test_voluntary_shows_never_reach_hole_cards():
    h = _parse(LINES).hands[0]
    assert all(not p.hole_cards for p in h.players.values())


def test_showdown_cards_stay_in_hole_cards():
    h = _parse(LINES).hands[1]
    assert h.went_to_showdown
    assert h.players["AAA"].hole_cards == ("As", "Ah")
    assert h.players["BBB"].hole_cards == ("9d", "8d")
    assert h.voluntary_shows == {}


def test_a_show_before_any_hand_is_dropped():
    res = _parse(['"Ann @ AAA" shows a 3♥.', *LINES])
    assert res.misses == []
    assert res.hands[0].voluntary_shows["AAA"].cards == ["7d", "2c"]


def test_a_show_by_someone_not_dealt_in_is_dropped():
    """Defensive: an unknown pn_id is skipped, exactly as every handler does."""
    lines = [*LINES[: END_1 + 1], '"Rail @ ZZZ" shows a A♣, A♦.', *LINES[END_1 + 1 :]]
    assert "ZZZ" not in _parse(lines).hands[0].voluntary_shows


# -------------------------------------------------------------- real logs ---


def test_no_card_shown_between_hands_is_lost():
    """Every card shown after a hand ended lands in that hand, in every fixture.

    The oracle walks the raw lines independently of the parser: a show with no
    hand open belongs to the hand that most recently ended (or was cut off).
    """
    total = 0
    for path in [*ALL_LOGS, *EDGE_LOGS]:
        gid = game_id_from_filename(path)
        entries = sorted(read_csv(path), key=lambda r: r.ord)
        shown: set[tuple[int, str, str]] = set()
        current = last = None
        for r in entries:
            ev = classify(r.entry, r.ord)
            if isinstance(ev, E.HandStart):
                if current is not None:
                    last = current
                current = ev.hand_number
            elif isinstance(ev, E.HandEnd):
                last, current = current, None
            elif isinstance(ev, E.Shows) and current is None and last is not None:
                shown.update((last, ev.player.pn_id, c) for c in ev.cards)
        recorded = {
            (h.hand_number, s.pn_id, c)
            for h in parse(entries, gid).hands
            for s in h.voluntary_shows.values()
            for c in s.cards
        }
        assert recorded == shown, path.name
        total += len(shown)
    assert total > 0, "the fixtures must actually exercise this"


def test_fixture_fold_then_show(parsed_all):
    """Hand #29 of the multiway log: hsj folds, then shows Q♣ 8♣."""
    h = next(h for h in parsed_all[MULTIWAY_GAME].hands if h.hand_number == 29)
    hsj = h.players["PEMYRVPxOS"]
    assert hsj.folded and not hsj.hole_cards
    assert h.voluntary_shows["PEMYRVPxOS"].cards == ["Qc", "8c"]


def test_table_survives_rebuild(db):
    """The table is layer 2: a rebuild must replace its rows, not duplicate them."""
    before = db.execute("SELECT * FROM voluntary_shows ORDER BY hand_id, pn_id").fetchall()
    assert before
    rebuild_game(db, MULTIWAY_GAME)
    after = db.execute("SELECT * FROM voluntary_shows ORDER BY hand_id, pn_id").fetchall()
    assert len(after) == len(before)


def test_view_finds_folds_shown_before_the_river(db):
    rows = db.execute(
        "SELECT * FROM v_voluntary_shows"
        " WHERE folded = 1 AND board_cards < 5 AND n_cards = 2 AND game_id = ?",
        (MULTIWAY_GAME,),
    ).fetchall()
    hit = [r for r in rows if r["hand_number"] == 29 and r["pn_id"] == "PEMYRVPxOS"]
    assert len(hit) == 1
    assert hit[0]["cards"] == "Qc8c"
    assert hit[0]["alias"] is not None
