"""Identity: IDs survive churn, names do not, and merges are cheap."""

from __future__ import annotations

from pnt.ingest.importer import merge_players
from pnt.logfmt.hero import infer_hero
from pnt.stats.queries import report

CHRIS = "5NARaPRkSp"
HERO_HU = "gpP9uUffpu"


def test_id_survives_quit_and_rejoin(parsed_all):
    """Chris quits with a stack of 0 at 08:27:06, requests a seat at 08:28:38 and
    is re-approved -- keeping the same PokerNow ID throughout."""
    res = parsed_all["pgl41zM3_CKphpnKM1DMIosUT"]
    before = [h for h in res.hands if h.hand_number <= 182 and CHRIS in h.players]
    after = [h for h in res.hands if h.hand_number >= 183 and CHRIS in h.players]
    assert before and after, "Chris plays on both sides of the rejoin"


def test_rejoin_does_not_create_a_second_player(db):
    """One ID must map to exactly one canonical player, churn notwithstanding."""
    rows = db.execute(
        "SELECT COUNT(*) FROM player_identities WHERE pn_id = ?", (CHRIS,)
    ).fetchone()[0]
    assert rows == 1
    players = db.execute(
        "SELECT COUNT(DISTINCT player_id) FROM player_identities WHERE pn_id = ?",
        (CHRIS,),
    ).fetchone()[0]
    assert players == 1


def test_one_id_spans_two_games_under_two_display_names(db):
    """`gpP9uUffpu` is 'genericpoker' in one game and '500' in another.

    Keying on the display name would split this person in two; keying on the ID
    keeps their 421 hands together.
    """
    row = db.execute(
        "SELECT player_id FROM player_identities WHERE pn_id = ?", (HERO_HU,)
    ).fetchone()
    assert row is not None
    n = db.execute(
        """SELECT COUNT(DISTINCT h.game_id) FROM hand_players hp
           JOIN hands h ON h.hand_id = hp.hand_id WHERE hp.pn_id = ?""",
        (HERO_HU,),
    ).fetchone()[0]
    assert n == 2, "this ID appears in two different games"


def test_hero_inference_is_confident_on_every_fixture(parsed_all):
    """Hero's hole cards are printed without a name; matching them against
    showdowns identifies them, and contradictions eliminate everyone else."""
    expected = {
        "pgl41zM3_CKphpnKM1DMIosUT": HERO_HU,
        "pgl1UViJ4BhoVP-KKHpux1Mpv": "MBFczOlpuA",
        "pglSdQtyFGypDbrqD5IhXXlYz": HERO_HU,
    }
    for gid, res in parsed_all.items():
        guess = infer_hero(res.hands)
        assert guess.confident, f"{gid}: only {guess.votes} votes"
        assert guess.pn_id == expected[gid]
        # Every other player at the table was ruled out by a contradiction.
        assert guess.pn_id not in guess.contradictions


def test_hero_cards_fill_in_non_showdown_hands(db):
    """Hero's cards are known every hand, not just at showdown -- which is why a
    correct hero id roughly quadruples hole-card coverage."""
    hero_seats, hero_known = db.execute(
        """SELECT COUNT(*), COUNT(hp.hole_cards) FROM hand_players hp
           JOIN hands h ON h.hand_id = hp.hand_id
           JOIN games g ON g.game_id = h.game_id
           WHERE hp.pn_id = g.hero_pn_id"""
    ).fetchone()
    assert hero_seats > 0
    assert hero_known == hero_seats, "hero's cards should be known on every hand"


def test_merge_moves_identities_without_recomputing_stats(db):
    """The cross-device case: one human, two IDs.

    Hero is `gpP9uUffpu` in two logs but `MBFczOlpuA` in the third -- the same
    person on another device. Merging is a single UPDATE precisely because no
    statistic is materialized.
    """
    before = {r["player"]: r for r in report(db)}
    assert "genericpoker" in before and "onlybluffs" in before
    combined_hands = before["genericpoker"]["hands"] + before["onlybluffs"]["hands"]

    moved = merge_players(db, "onlybluffs", "genericpoker")
    assert moved == 1

    after = {r["player"]: r for r in report(db)}
    assert "onlybluffs" not in after
    assert after["genericpoker"]["hands"] == combined_hands


def test_merge_is_idempotent_and_rejects_unknown_alias(db):
    import pytest

    merge_players(db, "hsj", "HSJ")
    assert merge_players(db, "HSJ", "HSJ") == 0
    with pytest.raises(ValueError):
        merge_players(db, "nobody-at-all", "HSJ")
