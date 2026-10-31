"""What you told the tracker, as opposed to what it read from a log.

Settings, saved spots and hand tags. Like a review mark, a note or a flag
(`stats/review.py`), none of it can be rebuilt from raw entries, so no rebuild,
merge or game deletion touches these tables: a tag is keyed on (game_id,
hand_number), the log's own name for a hand, and outlives the hand ids a rebuild
hands out again. Every write runs under `writing()`.

Data functions only; the routes that call them live in the server.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from .conn import writing


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --------------------------------------------------------------- settings ---


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    """The stored value under `key`, decoded from JSON, or `default` when unset."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> Any:
    """Store `value` (anything JSON can hold) under `key`, replacing what was there.
    Returns it as stored, read back through JSON."""
    text = json.dumps(value)
    with writing(conn):
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, text, _now()),
        )
    return json.loads(text)


def delete_setting(conn: sqlite3.Connection, key: str) -> bool:
    """Forget `key`, so `get_setting` falls back to its default. True if it was set."""
    with writing(conn):
        return conn.execute("DELETE FROM settings WHERE key = ?", (key,)).rowcount > 0


# ------------------------------------------------------------ saved spots ---

_SPOT_COLUMNS = "spot_id, name, filter, pinned, ord, created_at"
#: Pinned first, then your order, then the order they were saved in.
_SPOT_ORDER = "pinned DESC, ord IS NULL, ord, spot_id"


def _spot_row(r: sqlite3.Row) -> dict:
    return {**dict(r), "pinned": bool(r["pinned"])}


def _clean(text: str | None, what: str) -> str:
    out = (text or "").strip()
    if not out:
        raise ValueError(f"a spot needs a {what}")
    return out


def get_spot(conn: sqlite3.Connection, spot_id: int) -> dict:
    """One saved spot. Raises LookupError when there is none with that id."""
    row = conn.execute(f"SELECT {_SPOT_COLUMNS} FROM saved_spots WHERE spot_id = ?", (spot_id,)).fetchone()
    if row is None:
        raise LookupError(f"no saved spot {spot_id}")
    return _spot_row(row)


def list_spots(conn: sqlite3.Connection) -> list[dict]:
    """Every saved spot, pinned first, then in your order."""
    return [_spot_row(r) for r in conn.execute(f"SELECT {_SPOT_COLUMNS} FROM saved_spots ORDER BY {_SPOT_ORDER}")]


def save_spot(conn: sqlite3.Connection, name: str, filter: str) -> dict:
    """Save a filter under a new name, last in the order. Returns the spot.

    The name and the filter are stripped and must not be empty. A name already
    taken raises ValueError rather than overwriting it: saving over a spot is an
    edit (`update_spot`), never a side effect. The filter is stored as written;
    whether it parses is the caller's to check, with the name map it needs.
    """
    name, filter = _clean(name, "name"), _clean(filter, "filter")
    with writing(conn):
        if conn.execute("SELECT 1 FROM saved_spots WHERE name = ?", (name,)).fetchone():
            raise ValueError(f"a spot named {name!r} already exists")
        cur = conn.execute(
            "INSERT INTO saved_spots (name, filter, pinned, ord, created_at)"
            " VALUES (?, ?, 0, (SELECT COALESCE(MAX(ord), -1) + 1 FROM saved_spots), ?)",
            (name, filter, _now()),
        )
        spot_id = cur.lastrowid
    return get_spot(conn, spot_id)


def update_spot(
    conn: sqlite3.Connection,
    spot_id: int,
    *,
    name: str | None = None,
    filter: str | None = None,
    pinned: bool | None = None,
) -> dict:
    """Change any of a spot's name, filter or pin; None leaves a field alone.

    Raises LookupError on an unknown id, ValueError on an empty value or a name
    another spot has.
    """
    sets: list[str] = []
    args: list[Any] = []
    if name is not None:
        sets.append("name = ?")
        args.append(_clean(name, "name"))
    if filter is not None:
        sets.append("filter = ?")
        args.append(_clean(filter, "filter"))
    if pinned is not None:
        sets.append("pinned = ?")
        args.append(int(bool(pinned)))
    with writing(conn):
        if conn.execute("SELECT 1 FROM saved_spots WHERE spot_id = ?", (spot_id,)).fetchone() is None:
            raise LookupError(f"no saved spot {spot_id}")
        if name is not None and conn.execute(
            "SELECT 1 FROM saved_spots WHERE name = ? AND spot_id != ?", (args[0], spot_id)
        ).fetchone():
            raise ValueError(f"a spot named {args[0]!r} already exists")
        if sets:
            conn.execute(f"UPDATE saved_spots SET {', '.join(sets)} WHERE spot_id = ?", (*args, spot_id))
    return get_spot(conn, spot_id)


def delete_spot(conn: sqlite3.Connection, spot_id: int) -> bool:
    """Remove a saved spot. True if there was one; deleting it twice is a no-op."""
    with writing(conn):
        return conn.execute("DELETE FROM saved_spots WHERE spot_id = ?", (spot_id,)).rowcount > 0


def reorder_spots(conn: sqlite3.Connection, spot_ids: Iterable[int]) -> list[dict]:
    """Put the listed spots first, in that order; the rest follow in theirs.

    Pinned spots still list before the rest (`list_spots`): the order applies
    within each. Raises LookupError, writing nothing, if any id is unknown.
    """
    wanted = list(dict.fromkeys(spot_ids))
    with writing(conn):
        known = [r["spot_id"] for r in conn.execute(f"SELECT spot_id FROM saved_spots ORDER BY {_SPOT_ORDER}")]
        missing = [i for i in wanted if i not in set(known)]
        if missing:
            raise LookupError(f"no saved spot {missing[0]}")
        listed = set(wanted)
        order = wanted + [i for i in known if i not in listed]
        conn.executemany("UPDATE saved_spots SET ord = ? WHERE spot_id = ?", [(n, i) for n, i in enumerate(order)])
    return list_spots(conn)


# -------------------------------------------------------------- hand tags ---

#: The longest tag kept. A tag is a word or two, not a note.
TAG_MAX = 40
#: What a tag may be made of once normalised: letters, digits, `_` and `-`. The
#: filter language's own characters (`, | ( ) ! = < >`) are out, so `tag=<tag>`
#: always reads as one term.
_TAG_RE = re.compile(r"[\w-]+")
#: The 🚩 as a tag: `tag=flagged` is the hands flagged for manual review, and the
#: Tagged view lists them first. No hand can carry a tag of that name.
FLAGGED_TAG = "flagged"


def normalise_tag(tag: str) -> str:
    """A tag as it is stored: trimmed, lower case, each run of spaces one `-`.
    `" River Bluff "` is `river-bluff`. Raises ValueError on an empty, too long or
    unusable tag."""
    out = re.sub(r"\s+", "-", (tag or "").strip().lower())
    if not out:
        raise ValueError("a tag cannot be empty")
    if len(out) > TAG_MAX:
        raise ValueError(f"a tag is at most {TAG_MAX} characters")
    if not _TAG_RE.fullmatch(out):
        raise ValueError(f"tag {tag!r}: use letters, digits, - and _")
    return out


def require_logged(conn: sqlite3.Connection, game_id: str, hand_number: int) -> None:
    """Raise unless the database has that hand, derived or still only raw lines. A
    hand is flagged or tagged seconds after it ends, often before the rebuild that
    derives it, so `hands` alone would refuse it."""
    if conn.execute(
        "SELECT 1 FROM hands WHERE game_id = ? AND hand_number = ?", (game_id, hand_number)
    ).fetchone() is not None:
        return
    if conn.execute(
        "SELECT 1 FROM raw_entries WHERE game_id = ? AND entry LIKE ?",
        (game_id, f"-- starting hand #{hand_number} %"),
    ).fetchone() is None:
        raise ValueError(f"no hand #{hand_number} in game {game_id}")


def add_tag(conn: sqlite3.Connection, game_id: str, hand_number: int, tag: str) -> str:
    """Tag one hand. Returns the tag as stored. Idempotent: tagging again keeps the
    first time. Raises ValueError on a bad tag, or a hand the log does not have."""
    tag = normalise_tag(tag)
    require_logged(conn, game_id, hand_number)
    with writing(conn):
        conn.execute(
            "INSERT INTO hand_tags (game_id, hand_number, tag, tagged_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(game_id, hand_number, tag) DO NOTHING",
            (game_id, hand_number, tag, _now()),
        )
    return tag


def remove_tag(conn: sqlite3.Connection, game_id: str, hand_number: int, tag: str) -> bool:
    """Take one tag off a hand. True if it had it. Never checks the hand, so a tag
    left over from a game since deleted can still be swept up."""
    tag = normalise_tag(tag)
    with writing(conn):
        return conn.execute(
            "DELETE FROM hand_tags WHERE game_id = ? AND hand_number = ? AND tag = ?",
            (game_id, hand_number, tag),
        ).rowcount > 0


def tags_for_hand(conn: sqlite3.Connection, game_id: str, hand_number: int) -> list[str]:
    """One hand's tags, alphabetically."""
    return [
        r["tag"]
        for r in conn.execute(
            "SELECT tag FROM hand_tags WHERE game_id = ? AND hand_number = ? ORDER BY tag",
            (game_id, hand_number),
        )
    ]


def tags_for_hands(conn: sqlite3.Connection, game_id: str | None = None) -> dict[tuple[str, int], list[str]]:
    """(game_id, hand_number) -> its tags, alphabetically, for one game or all of
    them: the shape `review.flagged_hands` has, for a hand list to join against."""
    sql = "SELECT game_id, hand_number, tag FROM hand_tags"
    args: tuple = ()
    if game_id is not None:
        sql += " WHERE game_id = ?"
        args = (game_id,)
    out: dict[tuple[str, int], list[str]] = {}
    for r in conn.execute(sql + " ORDER BY game_id, hand_number, tag", args):
        out.setdefault((r["game_id"], r["hand_number"]), []).append(r["tag"])
    return out


def all_tags(conn: sqlite3.Connection) -> list[dict]:
    """Every tag in use with how many hands carry it and when it was last used:
    `{"tag", "hands", "last"}`, most used first. What a tag box autocompletes from."""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT tag, COUNT(*) AS hands, MAX(tagged_at) AS last FROM hand_tags"
            " GROUP BY tag ORDER BY hands DESC, tag"
        )
    ]


def tagged_keys(conn: sqlite3.Connection, tags: str | Iterable[str]) -> set[tuple[str, int]]:
    """The (game_id, hand_number) of every hand carrying any of `tags`, each
    normalised first: the key set a `tag=` filter term matches against."""
    wanted = sorted({normalise_tag(t) for t in ([tags] if isinstance(tags, str) else tags)})
    if not wanted:
        return set()
    return {
        (r["game_id"], r["hand_number"])
        for r in conn.execute(
            f"SELECT game_id, hand_number FROM hand_tags WHERE tag IN ({','.join('?' * len(wanted))})",
            wanted,
        )
    }


def tag_resolver(conn: sqlite3.Connection):
    """`tag=`'s resolver (filters.TagResolver) for `parse_filter`, shared by the
    server and the CLI: the hands carrying a tag, or the flagged ones for
    `flagged`. A bad tag raises ValueError, as a bad term does."""

    def resolve(tag: str) -> set[tuple[str, int]]:
        if normalise_tag(tag) == FLAGGED_TAG:
            rows = conn.execute("SELECT game_id, hand_number FROM hand_flags")
            return {(r["game_id"], r["hand_number"]) for r in rows}
        return tagged_keys(conn, tag)

    return resolve
