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
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Log exports can carry very large single fields; lift the default cap once.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

#: `poker_now_log_pgl41zM3_CKphpnKM1DMIosUT.csv` -> `pgl41zM3_CKphpnKM1DMIosUT`
_FILENAME_GAME_ID = re.compile(r"poker_now_log_(?P<gid>.+?)\.csv$")


@dataclass(frozen=True, slots=True)
class RawEntry:
    ord: int
    at: str
    entry: str


def game_id_from_filename(path: str | Path) -> str | None:
    m = _FILENAME_GAME_ID.search(Path(path).name)
    return m.group("gid") if m else None


def read_csv(path: str | Path) -> list[RawEntry]:
    """Read an export and return entries sorted by `order` ascending."""
    rows: list[RawEntry] = []
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = {"entry", "at", "order"} - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path}: missing required column(s): {sorted(missing)}")
        for row in reader:
            rows.append(
                RawEntry(ord=int(row["order"]), at=row["at"], entry=row["entry"])
            )
    rows.sort(key=lambda r: r.ord)
    return rows
