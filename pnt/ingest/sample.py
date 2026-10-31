"""The bundled sample: 65 anonymized games for trying the tracker out.

`pnt setup --sample` and `pnt import --sample` load it into a database of yours;
the front page's Data switch builds it into a database of its own (`database()`),
so a demo never mixes someone else's hands into your history.

These are `pnt redact --anonymize` output, not raw exports: a hole card survives
only where it was shown down, and every player is a `player N` stand-in. See
`pnt/logs/README.md`, `pnt/logfmt/redact.py` and `pnt/logfmt/anonymize.py`.
"""

from __future__ import annotations

import csv
import hashlib
import sqlite3
from collections.abc import Callable
from pathlib import Path

from pnt import local_config
from pnt.db.userdata import get_setting, set_setting
from pnt.ingest.csv_source import has_hands, read_csv
from pnt.ingest.importer import apply_aliases, import_csv
from pnt.ingest.sync import LOG_GLOB

LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
ALIASES = LOG_DIR / "aliases.csv"
#: Where the front page builds the sample: beside the config (~/.pnt, for the reason
#: given in service.py: not under %LOCALAPPDATA%, which Store Python redirects). It
#: follows $PNT_CONFIG, so a worktree's or a test's server builds its own sample,
#: and its clean-up of older ones never deletes the live server's.
HOME = local_config.PATH.parent
#: The setting a finished build stamps with its `version`; a build cut short has none.
VERSION_SETTING = "sample_version"


def logs() -> list[Path]:
    return sorted(LOG_DIR.glob(LOG_GLOB))


def is_sample_folder(path: Path) -> bool:
    """Whether `path` is the bundled sample's own folder (pnt/logs). Never a log
    folder: synced, its anonymized copies of real games land in your database beside
    the games they copy, and live capture would save real names into it."""
    try:
        return path.expanduser().resolve() == LOG_DIR.resolve()
    except OSError:
        return False


#: The last `version` worked out, under the files' (name, size, mtime) it was for.
_version: tuple[tuple, str] | None = None


def version() -> str:
    """The bundled files as they are now, so a `pnt update` that changes the sample
    makes the front page build it again. Their bytes, not only their sizes: swapping
    two stand-ins' names changes no size. Hashed again only when a file's size or
    time moves; 27 MB takes a moment, and the switch asks more than once."""
    global _version
    files = [*logs(), ALIASES]
    stamp = tuple((p.name, st.st_size, st.st_mtime_ns) for p in files for st in [p.stat()])
    if _version is not None and _version[0] == stamp:
        return _version[1]
    h = hashlib.sha256()
    for p in files:
        h.update(f"{p.name}:{p.stat().st_size}\n".encode())
        h.update(p.read_bytes())
    _version = (stamp, h.hexdigest()[:16])
    return _version[1]


def database() -> Path:
    """The sample's database for the bundled files as they are now. Named by
    `version`, so an updated sample is built beside the old one rather than over
    a file a reader may still hold open (Windows refuses to delete those)."""
    return HOME / f"sample-{version()}.sqlite"


def stale() -> list[Path]:
    """Older samples' databases, for tidying away."""
    keep = database().name
    return [p for p in HOME.glob("sample-*.sqlite") if p.name != keep]


def apply_bundled_aliases(conn: sqlite3.Connection) -> int:
    """Merge the sample's players the way the original table's were: one person on
    several devices is one `player N`, not one per PokerNow ID. Returns the rows moved."""
    with ALIASES.open(newline="", encoding="utf-8") as fh:
        pairs = [(r["pn_id"], r["alias"]) for r in csv.DictReader(fh)]
    moved, _ = apply_aliases(conn, pairs)
    return moved


def is_current(conn: sqlite3.Connection) -> bool:
    return get_setting(conn, VERSION_SETTING) == version()


def build(conn: sqlite3.Connection, on_file: Callable[[int, int], None] | None = None) -> None:
    """Import every bundled log into `conn`, merge the aliases, and stamp the
    version. `on_file(done, total)` is called after each file. Safe to re-run:
    duplicate entries are ignored."""
    files = logs()
    for i, path in enumerate(files, 1):
        if has_hands(read_csv(path)):
            import_csv(conn, str(path))
        if on_file is not None:
            on_file(i, len(files))
    apply_bundled_aliases(conn)
    set_setting(conn, VERSION_SETTING, version())
