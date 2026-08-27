"""Lexical helpers: player tokens and cards.

Player display names are hostile input. Real logs in this dataset contain players
literally named ``all in``, ``500`` and ``1500`` -- any of which will fool a regex
that pattern-matches loose text. Everything here anchors on the quoted
``"Name @ ID"`` token and splits from the *right*, because the ID charset is known
and the name's is not.

Encoding note: PokerNow exports are UTF-8 with ♠♥♦♣. If you ever see ``Aâ¦`` the
file was decoded as latin-1; fix that at the file-open boundary, not here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: PokerNow IDs are base64url-ish. Seen in the wild: `gpP9uUffpu`, `d-4X_F_SSU`.
PN_ID = r"[A-Za-z0-9_-]+"

_SUITS = {"♠": "s", "♥": "h", "♦": "d", "♣": "c"}


@dataclass(frozen=True, slots=True)
class PlayerRef:
    """A player as named in the log. ``pn_id`` is the identity key; ``name`` is not."""

    name: str
    pn_id: str

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.name} @ {self.pn_id}"


def split_player_token(tok: str) -> PlayerRef:
    """Split ``Name @ ID`` into its parts.

    Splits on the *last* ``" @ "`` so names containing " @ " still resolve to the
    correct ID. Raises on a token with no separator: that is a parse bug worth
    surfacing rather than swallowing.
    """
    name, sep, pn_id = tok.rpartition(" @ ")
    if not sep:
        raise ValueError(f"not a player token: {tok!r}")
    return PlayerRef(name=name, pn_id=pn_id)


def normalize_suits(text: str) -> str:
    for glyph, letter in _SUITS.items():
        text = text.replace(glyph, letter)
    return text


_CARD_RE = re.compile(r"(10|[2-9TJQKA])([shdc])")


def parse_cards(text: str) -> list[str]:
    """Parse a card list such as ``A♦, 5♣`` into ``['Ad', '5c']``.

    Ranks are normalized so ``10`` becomes ``T`` -- PokerNow writes ``10♠`` where
    every other poker tool writes ``Ts``.
    """
    return [
        ("T" if rank == "10" else rank) + suit
        for rank, suit in _CARD_RE.findall(normalize_suits(text))
    ]
