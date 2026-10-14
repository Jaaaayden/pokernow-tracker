"""Strip hero's hole cards from an export, except where they were already public.

A downloaded log names hero's cards on *every* dealt hand::

    "Your hand is 4♦, 9♥",2026-07-23T08:18:05.455Z,178479468545503

which is the whole reason `hero.py` can double a session's hole-card coverage --
and the whole reason a raw export cannot be published. It says what you folded,
what you three-bet light with, and what you checked back on the river, for every
hand you ever played.

The rule here is the narrowest one that leaks nothing: **keep a ``Your hand is``
entry only when the same two cards also appear in a ``shows a`` entry in the same
hand.** Two players cannot hold the same two cards, so a matching show *is* hero
showing -- it is already in the log, visible to everyone who sat at that table,
and repeating it adds no information. Everything else goes.

Notice what this does not need: hero's identity. The match is between hero's cards
and a showdown holding, not between hero and a player ID, so redaction works on a
log whose hero was never inferred -- and cannot be wrong about which player to
protect. It fails closed in every unclear case: a one-card voluntary show does not
match a two-card hand, so that entry is dropped too (the shown card survives in
the ``shows a`` entry, which is where it was public in the first place).

What survives redaction is still enough to import. `infer_hero` votes on hands
where hero's cards match a showdown holding -- exactly the hands kept here -- so a
redacted log still identifies its own hero, as long as hero reached showdown
`hero.MIN_VOTES` times. Hands below that stay unattributed, the same as any log.

Nothing else in the export carries a hidden holding. ``collected N from pot with
... (combination: ...)`` names five cards, but only ever for a player who showed
(asserted over the whole corpus by `tests/test_redact.py`), and ``Undealt cards:``
is by definition the part of the deck that reached nobody.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Container, Iterable
from dataclasses import dataclass
from pathlib import Path

from ..ingest.csv_source import RawEntry, read_csv
from . import events as E
from .grammar import classify


@dataclass(frozen=True, slots=True)
class Plan:
    """Which entries to drop, and what that costs."""

    #: {order of the entry to delete: the hand it was dealt on}.
    drop_ords: dict[int, int | None]
    #: Hands where the export stated hero's cards at all.
    hero_hands: int
    #: ...of those, the ones kept because hero showed them down.
    kept: int

    @property
    def dropped(self) -> int:
        return self.hero_hands - self.kept


def plan(entries: Iterable[RawEntry]) -> Plan:
    """Decide which `Your hand is` entries a log can keep.

    Entries must be in `order` order, which `read_csv` guarantees.
    """
    drop: dict[int, int | None] = {}
    hero_hands = kept = 0
    hand_number: int | None = None
    hero_ord: int | None = None
    hero_cards: frozenset[str] = frozenset()
    shown: list[frozenset[str]] = []

    def close_hand() -> None:
        nonlocal hero_ord, hero_cards, shown, hero_hands, kept
        if hero_ord is not None:
            hero_hands += 1
            if hero_cards and hero_cards in shown:
                kept += 1
            else:
                drop[hero_ord] = hand_number
        hero_ord, hero_cards, shown = None, frozenset(), []

    for raw in entries:
        ev = classify(raw.entry, raw.ord)
        if isinstance(ev, E.HandStart):
            # A show logged after `-- ending hand #N --` but before the next hand
            # starts belongs to the hand that ended, so the boundary is the start
            # marker, never the end marker.
            close_hand()
            hand_number = ev.hand_number
        elif isinstance(ev, E.HeroCards):
            hero_ord, hero_cards = raw.ord, frozenset(ev.cards)
        elif isinstance(ev, E.Shows):
            shown.append(frozenset(ev.cards))
    close_hand()

    return Plan(drop, hero_hands, kept)


#: One physical line holding nothing but a hero-cards entry. Hero lines never carry
#: the embedded newlines that make line-splitting a CSV unsafe in general (only the
#: "Game Config Changes" block does), and matching the whole line -- quotes, both
#: separators, the order -- means a line that merely *looks* like one cannot match.
_HERO_LINE = re.compile(r'^"Your hand is [^"\r\n]*",[^,"\r\n]*,(?P<ord>\d+)\r?$')


def redact_text(raw: str, drop_ords: Container[int]) -> tuple[str, int]:
    """Delete the planned entries from an export's text, byte-for-byte otherwise.

    Deleting whole lines rather than re-serialising the CSV is deliberate: these
    exports are captured bytes (see `.gitattributes`), and Python's writer quotes
    minimally where PokerNow quotes the entry column always. Rewriting every row to
    drop one in five would leave a diff in which the redaction is invisible.
    """
    out: list[str] = []
    removed = 0
    for line in raw.split("\n"):
        m = _HERO_LINE.match(line)
        if m and int(m.group("ord")) in drop_ords:
            # Confirm against a real parse before deleting: the regex says the
            # shape is right, this says the field really is the entry.
            row = next(csv.reader(io.StringIO(line)), None)
            if row and len(row) == 3 and row[0].startswith("Your hand is"):
                removed += 1
                continue
        out.append(line)
    return "\n".join(out), removed


def redact_file(src: Path, dst: Path) -> Plan:
    """Write a publishable copy of `src` to `dst`. Returns what was taken out."""
    p = plan(read_csv(src))
    text = src.read_text(encoding="utf-8")
    redacted, removed = redact_text(text, p.drop_ords)
    if removed != len(p.drop_ords):
        raise RuntimeError(
            f"{src.name}: planned to remove {len(p.drop_ords)} entries but matched "
            f"{removed} lines -- refusing to write a half-redacted log"
        )
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(redacted, encoding="utf-8", newline="")
    return p


def audit(path: Path) -> list[str]:
    """Name every hand whose holding the file still gives away without a showdown.

    Run this on what you are about to publish, not on what you meant to publish.
    An empty list is the only acceptable result.

    The report names hands, never cards. An audit line is something you paste into
    a CI log or an issue; a tool that answers "you are leaking hole cards" by
    printing the hole cards has not helped.
    """
    p = plan(read_csv(path))
    return [
        f"{path.name}: hand #{hand if hand is not None else '?'} (order {ord_}) "
        f"names a holding that never reached showdown"
        for ord_, hand in sorted(p.drop_ords.items())
    ]
