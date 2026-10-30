"""Hero inference: when two showdowns are enough, and when they are not."""

from __future__ import annotations

from pnt.logfmt.hero import MIN_VOTES, infer_hero
from pnt.logfmt.parser import HandPlayer, ParsedHand

HERO, OTHER, THIRD = "heroHEROhh", "otherOTHER", "thirdTHIRD"


def _hand(n, hero_cards, shown, dealt=None):
    """A hand naming `hero_cards`, where `shown` maps pn_id -> cards shown down and
    `dealt` lists who else was dealt in without showing."""
    players = {}
    for seat, pid in enumerate([*shown, *(dealt or [])], start=1):
        players[pid] = HandPlayer(pn_id=pid, name=pid, seat=seat, starting_stack=1000,
                                  hole_cards=tuple(shown.get(pid, ())))
    return ParsedHand(game_id="g", hand_number=n, table_hand_id=str(n), ts="", ord=n,
                      variant="nlhe", dealer_seat=None, dead_button=False, n_dealt_in=len(players),
                      players=players, hero_cards=tuple(hero_cards))


def test_two_showdowns_name_hero_when_everyone_else_is_ruled_out():
    """A short session: hero showed down twice, and each other player either showed
    cards that were not hero's or sat out a hand hero was dealt."""
    hands = [
        _hand(1, ("As", "Kd"), {HERO: ("As", "Kd"), OTHER: ("7c", "7d")}),
        _hand(2, ("Qh", "Qs"), {HERO: ("Qh", "Qs")}, dealt=[OTHER]),
        _hand(3, ("2c", "3c"), {}, dealt=[HERO]),
        _hand(4, ("9h", "8h"), {}, dealt=[HERO, THIRD]),
    ]
    guess = infer_hero(hands)
    assert (guess.pn_id, guess.votes, guess.confident) == (HERO, 2, True)


def test_two_showdowns_are_not_enough_while_someone_else_could_be_hero():
    """THIRD was dealt in on every hand that named hero's cards and never showed
    anything else, so nothing rules them out."""
    hands = [
        _hand(1, ("As", "Kd"), {HERO: ("As", "Kd")}, dealt=[THIRD]),
        _hand(2, ("Qh", "Qs"), {HERO: ("Qh", "Qs")}, dealt=[THIRD]),
    ]
    guess = infer_hero(hands)
    assert (guess.pn_id, guess.votes, guess.confident) == (HERO, 2, False)


def test_one_showdown_is_never_enough():
    hands = [_hand(1, ("As", "Kd"), {HERO: ("As", "Kd"), OTHER: ("7c", "7d")})]
    assert infer_hero(hands).confident is False


def test_enough_showdowns_name_hero_whoever_else_was_dealt_in():
    hands = [
        _hand(n, cards, {HERO: cards}, dealt=[THIRD])
        for n, cards in enumerate([("As", "Kd"), ("Qh", "Qs"), ("2c", "3c")][:MIN_VOTES], start=1)
    ]
    guess = infer_hero(hands)
    assert (guess.pn_id, guess.confident) == (HERO, True)


def test_one_contradiction_rules_a_player_out_however_many_votes():
    hands = [
        _hand(1, ("As", "Kd"), {HERO: ("As", "Kd")}),
        _hand(2, ("Qh", "Qs"), {HERO: ("Qh", "Qs")}),
        _hand(3, ("2c", "3c"), {HERO: ("2c", "3c")}),
        _hand(4, ("9h", "8h"), {HERO: ("Ah", "Ad")}),
    ]
    guess = infer_hero(hands)
    assert guess.pn_id is None and guess.confident is False
