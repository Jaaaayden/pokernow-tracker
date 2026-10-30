"""Replace the real players in logs with stand-ins, for logs published to strangers.

`redact` takes out the hole cards a log should not give away; this takes out who
was playing. The bundled sample corpus ships to everyone who installs the tracker,
and a player's name and PokerNow ID are theirs: people who did not agree to be in
a download should not be findable in one.

A player appears in a log in one shape only, quoted as ``"name @ id"`` (in join
and quit lines, in every action, in ``Player stacks:``). Each real name becomes
``player N``, numbered in order of first appearance, and each real ID a stand-in
of the same length and alphabet. Given an alias file, an ID that has an alias is
named for the alias instead of its display name, so one person who renamed
themselves every session is one ``player N`` throughout, not one per name. The game ID goes too: PokerNow serves any game's
log to anyone who has its ID, so a real one would lead straight back to the
original, names and all.

Every stand-in is the same everywhere in one run, across every file, so what the
tracker derives is unchanged: the same hands, the same stats, the same hero
(`infer_hero` votes on IDs, which map one to one), and merges still line up once
the alias file is run through the same `Pseudonyms`. They come from a keyed hash
whose key is random and never kept, so nobody can check a guessed real ID against
the published ones -- and running this again gives different stand-ins.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import re
import secrets
import string
from pathlib import Path

from ..ingest.csv_source import game_id_from_filename, read_csv
from ..ingest.log_folder import _quote
from .tokens import PN_ID

#: A quoted ``"name @ id"`` token. It opens at the start of the line or after a
#: space or ``(``, and closes before the end, a space or ``, . )``, as every token
#: in the grammar does. That anchoring, not a quote-free name, is what ends it:
#: a name may hold a ``"`` of its own (``Bob "the" Nit``), and stopping at it
#: would leave the start of the real name behind.
_PLAYER = re.compile(
    rf'(?<![^\s(])"(?P<name>[^\r\n]*?) @ (?P<id>{PN_ID})"(?=$|[\s,.)])', re.MULTILINE
)
#: The one line that names IDs outside a quoted token: a mid-session login. The
#: new ID is the one the player's token now carries, so leaving either bare would
#: tie a stand-in straight back to a real account.
_ID_CHANGE = re.compile(
    rf'^(?P<head>The player ".*" changed the ID from )(?P<old>{PN_ID}) to '
    rf"(?P<new>{PN_ID})(?P<tail> because .*)$"
)
_ALPHABET = string.ascii_letters + string.digits + "_-"
#: What every player in a published log must look like afterwards.
STAND_IN = r"player \d+"


class Pseudonyms:
    """One run's stand-ins: every real name, ID and game ID to its replacement."""

    def __init__(self, key: bytes | None = None, aliases: dict[str, str] | None = None) -> None:
        self._key = key or secrets.token_bytes(32)
        #: Real ID to real alias: what an ID is named for, over its display name.
        self.aliases = aliases or {}
        self.names: dict[str, str] = {}
        self.ids: dict[str, str] = {}
        self.games: dict[str, str] = {}

    def _code(self, kind: str, real: str, length: int) -> str:
        out, n = "", 0
        while len(out) < length:  # a longer ID than one digest covers: keep hashing
            digest = hmac.new(self._key, f"{kind}:{n}:{real}".encode(), hashlib.sha256).digest()
            out += "".join(_ALPHABET[b % len(_ALPHABET)] for b in digest)
            n += 1
        return out[:length]

    def _unique(self, table: dict[str, str], kind: str, real: str, length: int) -> str:
        if real not in table:
            taken = set(table.values())
            fake, salt = self._code(kind, real, length), 0
            while fake in taken:  # a collision in 64**10: not worth risking two players as one
                salt += 1
                fake = self._code(f"{kind}{salt}", real, length)
            table[real] = fake
        return table[real]

    def name(self, real: str) -> str:
        return self.names.setdefault(real, f"player {len(self.names) + 1}")

    def pn_id(self, real: str) -> str:
        return self._unique(self.ids, "id", real, len(real))

    def game_id(self, real: str) -> str:
        # PokerNow's "pgl" prefix is kept: it is not anyone's, and the IDs keep their look.
        prefix = "pgl" if real.startswith("pgl") else ""
        return prefix + self._unique(self.games, "game", real, len(real) - len(prefix))

    def entry(self, text: str) -> str:
        def swap(m: re.Match) -> str:
            name = self.name(self.aliases.get(m["id"], m["name"]))
            return f'"{name} @ {self.pn_id(m["id"])}"'

        def swap_ids(m: re.Match) -> str:
            return f'{m["head"]}{self.pn_id(m["old"])} to {self.pn_id(m["new"])}{m["tail"]}'

        return _ID_CHANGE.sub(swap_ids, _PLAYER.sub(swap, text))


def anonymize_file(src: Path, out: Path, names: Pseudonyms) -> Path:
    """Write `src` with its players and game replaced; the file is named for the new game."""
    real = game_id_from_filename(src)
    if real is None:
        raise ValueError(f"{src.name}: not named like a PokerNow export, so its game ID is unknown")
    dst = out / f"poker_now_log_{names.game_id(real)}.csv"
    rows = [f"{_quote(names.entry(e.entry))},{e.at},{e.ord}\n" for e in reversed(read_csv(src))]
    out.mkdir(parents=True, exist_ok=True)
    dst.write_text("entry,at,order\n" + "".join(rows), encoding="utf-8", newline="")
    return dst


def read_aliases(src: Path) -> dict[str, str]:
    """An alias file (`pnt alias export`) as real ID to real alias."""
    with src.open(encoding="utf-8", newline="") as fh:
        return {r["pn_id"]: r["alias"] for r in csv.DictReader(fh)}


def anonymize_aliases(src: Path, dst: Path, names: Pseudonyms) -> int:
    """The alias file (`pnt alias export`), through the same stand-ins. IDs no log
    mentioned are left out: they would be real IDs with nothing to replace them."""
    pairs = read_aliases(src).items()
    kept = [(names.ids[i], names.name(a)) for i, a in pairs if i in names.ids]
    with dst.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["pn_id", "alias"])
        writer.writerows(sorted(kept, key=lambda p: (int(p[1].split()[-1]), p[0])))
    return len(kept)


def players_in(path: Path) -> set[tuple[str, str]]:
    """Every (name, id) a file names: what an audit of a published folder reads.

    The bare IDs of a login line count too, under the name of the player on it."""
    found = set()
    for e in read_csv(path):
        tokens = [(m["name"], m["id"]) for m in _PLAYER.finditer(e.entry)]
        found.update(tokens)
        login = _ID_CHANGE.match(e.entry)
        if login and tokens:
            found.update({(tokens[0][0], login["old"]), (tokens[0][0], login["new"])})
    return found
