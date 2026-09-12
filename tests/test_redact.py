"""Redaction: a publishable log gives away no holding a showdown did not.

The interesting assertions here are the negative ones. `test_no_unshown_holding_survives`
is the guarantee the feature exists for; `test_only_hero_entries_are_touched` is what
stops it quietly rewriting a log while it does that.
"""

from __future__ import annotations

import re

import pytest
from conftest import ALL_LOGS, EDGE_LOGS

from pnt.db.conn import connect
from pnt.ingest.csv_source import game_id_from_filename, read_csv
from pnt.ingest.importer import import_csv
from pnt.logfmt import events as E
from pnt.logfmt import redact as rd
from pnt.logfmt.grammar import classify
from pnt.logfmt.hero import infer_hero
from pnt.logfmt.parser import parse
from pnt.stats.queries import report

EVERY_LOG = ALL_LOGS + EDGE_LOGS


@pytest.fixture(params=EVERY_LOG, ids=lambda p: p.stem[-6:])
def pair(request, tmp_path):
    """(original, redacted) for one log."""
    src = request.param
    dst = tmp_path / src.name
    plan = rd.redact_file(src, dst)
    return src, dst, plan


def _hero_lines(path):
    return [r.entry for r in read_csv(path) if r.entry.startswith("Your hand is")]


def _hands(path):
    """[(hero_cards, [shown holdings]), ...] straight off the entry stream."""
    out, hero, shown = [], None, []
    for raw in read_csv(path):
        ev = classify(raw.entry, raw.ord)
        if isinstance(ev, E.HandStart):
            out.append((hero, shown))
            hero, shown = None, []
        elif isinstance(ev, E.HeroCards):
            hero = frozenset(ev.cards)
        elif isinstance(ev, E.Shows):
            shown.append(frozenset(ev.cards))
    out.append((hero, shown))
    return [h for h in out if h != (None, [])]


def test_no_unshown_holding_survives(pair):
    """The point of the whole module: what is left was already public."""
    _, dst, _ = pair
    assert rd.audit(dst) == []
    for hero, shown in _hands(dst):
        if hero is not None:
            assert hero in shown, f"{hero} survived without a matching showdown"


def test_showdown_holdings_are_kept(pair):
    """Fail-closed is not an excuse to redact everything. Every hand hero showed
    down keeps its entry, so a published log is still worth importing."""
    src, dst, plan = pair
    expected = sum(1 for hero, shown in _hands(src) if hero is not None and hero in shown)
    assert plan.kept == expected
    assert len(_hero_lines(dst)) == expected
    assert expected > 0, "a log with no showdowns would make this test vacuous"


def test_only_hero_entries_are_touched(pair):
    """Every surviving line is byte-identical and in the original order; every
    removed one is a hero-cards entry. Nothing is re-serialised."""
    src, dst, plan = pair
    before = src.read_text(encoding="utf-8").split("\n")
    after = dst.read_text(encoding="utf-8").split("\n")
    kept = iter(after)
    removed = 0
    for line in before:
        nxt = next(kept, None)
        if nxt == line:
            continue
        kept = iter([nxt, *kept]) if nxt is not None else kept
        assert line.startswith('"Your hand is '), f"removed a non-hero line: {line[:80]}"
        removed += 1
    assert removed == len(plan.drop_ords) == plan.dropped
    assert next(kept, None) is None, "output has lines the original did not"


def test_redaction_is_idempotent(pair, tmp_path):
    _, dst, _ = pair
    again = tmp_path / "again" / dst.name
    plan = rd.redact_file(dst, again)
    assert plan.dropped == 0
    assert again.read_text(encoding="utf-8") == dst.read_text(encoding="utf-8")


def test_hero_is_still_identifiable(pair):
    """Redaction keeps exactly the hands `infer_hero` votes on, so a published log
    still knows whose it is -- the reason for matching on cards, not on identity."""
    src, dst, _ = pair
    gid = game_id_from_filename(src)
    before = infer_hero(parse(read_csv(src), gid).hands)
    after = infer_hero(parse(read_csv(dst), gid).hands)
    assert after.pn_id == before.pn_id
    assert after.votes == before.votes  # votes only ever came from showdowns
    assert after.confident == before.confident


def test_stats_are_unaffected(pair, tmp_path):
    """Hole cards feed range charts, not the action stats. Every rate a published
    log reports must equal the one the original reports."""
    src, dst, _ = pair
    rows = []
    for i, path in enumerate((src, dst)):
        conn = connect(tmp_path / f"stats{i}.sqlite")
        import_csv(conn, path, game_id=game_id_from_filename(src))
        rows.append(report(conn))
        conn.close()
    assert rows[0] == rows[1]


_COMBINATION = re.compile(r"\(combination: (?P<cards>[^)]*)\)")


def test_combination_lines_never_name_an_unshown_player(pair):
    """`redact.py` claims the pot-award line is not a second leak channel. It names
    five cards, so if it could appear for a player who never showed, redacting the
    hero entry alone would not be enough. It cannot -- asserted, not assumed."""
    src, _, _ = pair
    shown, seen_start = set(), False
    for raw in read_csv(src):
        ev = classify(raw.entry, raw.ord)
        if isinstance(ev, E.HandStart):
            shown, seen_start = set(), True
        elif isinstance(ev, E.Shows):
            shown.add(ev.player.pn_id)
        elif isinstance(ev, E.Collected) and _COMBINATION.search(raw.entry):
            assert seen_start
            assert ev.player.pn_id in shown, (
                f"{raw.entry[:90]} names a holding for a player who never showed"
            )
