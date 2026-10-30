"""Read PokerNow log exports into raw entries.

Two invariants live here, and both matter:

1. **Sort by `order`, ascending.** The CSV is written newest-first, and `at`
   timestamps tie constantly (six entries share one millisecond in the sample
   log). `order` is `epoch_ms * 100 + sequence` and is globally unique, so it is
   the only safe sort key -- and the only safe dedupe key.
2. **Use a real CSV reader.** Entries contain embedded newlines (the multi-line
   "Game Config Changes" block), so line-splitting the file corrupts them.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Log exports can carry very large single fields; lift the default cap once.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

log = logging.getLogger(__name__)

#: Every PokerNow game ID fits this, and it is the only shape allowed near a file
#: name: an ID with `..` or a slash in it would put the log outside the folder. The
#: extension reads IDs from the URL with the same pattern; the server checks it on
#: the way in, and `log_folder.log_path` is the last line of defence.
GAME_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

#: `poker_now_log_pgl41zM3_CKphpnKM1DMIosUT.csv` -> `pgl41zM3_CKphpnKM1DMIosUT`.
#: A browser saves a second download of the same export as `... (1).csv` (Chrome,
#: Edge) or `...(1).csv` (Firefox): the same game, so the copy number is no part of
#: the ID. Read as an ID of its own, every hand in it would be counted twice.
_FILENAME_GAME_ID = re.compile(r"poker_now_log_(?P<gid>.+?)(?: ?\(\d+\))?\.csv$")

#: The largest gap, in ms, allowed between a line's `at` and the time its `order`
#: encodes (`order // 100`), where that is checked. Every export seen has the two
#: equal; an `order` cut short is out by whole digits, so by decades.
_ORDER_SLACK_MS = 24 * 3600 * 1000


@dataclass(frozen=True, slots=True)
class RawEntry:
    ord: int
    at: str
    entry: str


def game_id_from_filename(path: str | Path) -> str | None:
    """The game a log file holds, read from its name; None for a name that is not
    `poker_now_log_<game ID>.csv`, give or take a browser's copy number."""
    m = _FILENAME_GAME_ID.search(Path(path).name)
    if not m or not GAME_ID.fullmatch(m.group("gid")):
        return None
    return m.group("gid")


#: The line every hand opens with. A log without one holds no hand at all: a table
#: joined and left before the first deal leaves a file of seat requests, ID changes
#: and config lines, a few hundred bytes, that would otherwise sit in the log folder
#: and the database as a game of nothing.
HAND_START = "-- starting hand #"


def has_hands(entries: list[RawEntry]) -> bool:
    return any(e.entry.startswith(HAND_START) for e in entries)


def _order_matches_at(order: int, at: str) -> bool:
    try:
        ms = datetime.fromisoformat(at).timestamp() * 1000
    except (ValueError, OverflowError, OSError):
        return False
    return abs(order // 100 - ms) <= _ORDER_SLACK_MS


def read_csv(path: str | Path) -> list[RawEntry]:
    """Read an export and return entries sorted by `order` ascending.

    A malformed row is skipped and logged, not raised: one bad line must not keep
    the rest of the file, or the files after it, out of the database. That is a
    short row (a field missing, as a cut-off copy or a hand edit leaves one) or an
    `order` that is not a whole number.

    A file that does not end in a newline was cut off mid-row -- every export, and
    every log this program writes, ends in one -- so its last row is kept only if
    its `order` still agrees with its `at`. A cut inside `order` leaves a number
    short by some digits that still parses, and ingest is append-only: a wrong
    `order` stored once would stay for good, out of place in the game.
    """
    with open(path, encoding="utf-8", newline="") as fh:
        text = fh.read()
    reader = csv.DictReader(io.StringIO(text, newline=""))
    missing = {"entry", "at", "order"} - set(reader.fieldnames or ())
    if missing:
        raise ValueError(f"{path}: missing required column(s): {sorted(missing)}")
    rows: list[RawEntry] = []
    skipped = 0
    last_kept = False  # whether the file's last row so far went into `rows`
    for row in reader:
        order, at, entry = row["order"], row["at"], row["entry"]
        try:
            if order is None or at is None or entry is None:
                raise ValueError("short row")
            rows.append(RawEntry(ord=int(order), at=at, entry=entry))
            last_kept = True
        except ValueError:
            skipped += 1
            last_kept = False
    cut_off = not text.endswith(("\n", "\r"))
    if cut_off and last_kept and not _order_matches_at(rows[-1].ord, rows[-1].at):
        rows.pop()
        skipped += 1
    if skipped:
        log.warning("%s: skipped %d malformed row(s)", path, skipped)
    rows.sort(key=lambda r: r.ord)
    return rows
