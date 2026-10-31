"""Notes left on the pages themselves, for whoever changes the UI next.

The overlay (`extension/annotate.js`, on every server page and in the side panel)
lets you click an element and write what is wrong with it. Each note keeps where
it was left -- the page, a CSS selector, the element's text and size -- so the
fix can be found from the note alone, without a screenshot to squint at.

Kept in a JSON file beside the machine config rather than in the database: a note
about the chart is about this checkout's code, not about any hands, and it must
survive switching databases or the sample on and off. A worktree's server writes
its own file (`.pnt-dev/`, through $PNT_CONFIG), as it does its own config.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pnt import local_config

# A note is open until someone answers it, resolved with a reply until you have
# looked and dismissed it, then closed (app.py's `AnnotationChange.status`).
#: Past this, the oldest closed notes go: the file is read whole on every request.
KEEP = 500

_lock = threading.Lock()


def path() -> Path:
    # Read at call time: the tests point local_config.PATH at a temp folder.
    return local_config.PATH.parent / "annotations.json"


def load() -> list[dict]:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [a for a in data if isinstance(a, dict)] if isinstance(data, list) else []


def _save(notes: list[dict]) -> None:
    if len(notes) > KEEP:
        closed = [a for a in notes if a.get("status") == "closed"]
        drop = {a.get("id") for a in closed[: len(notes) - KEEP]}
        notes = [a for a in notes if a.get("id") not in drop]
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Replaced whole, as local_config.put does, so a crash never leaves half a file.
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.part")
    try:
        tmp.write_text(json.dumps(notes, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def listing(status: str | None = None, page: str | None = None) -> list[dict]:
    """Oldest first, the order they were written in. `page` matches the path only,
    so a note left on one player's chart shows on every player's."""
    return [
        a
        for a in load()
        if (status is None or a.get("status") == status) and (page is None or a.get("page") == page)
    ]


def add(fields: dict) -> dict:
    note = {"id": uuid.uuid4().hex[:10], "at": _now(), "status": "open", "reply": None, **fields}
    with _lock:
        notes = load()
        notes.append(note)
        _save(notes)
    return note


def change(note_id: str, fields: dict) -> dict | None:
    """Set some of a note's fields; None when there is no such note. Moving it to
    resolved stamps when, so the overlay can show the answer as new."""
    with _lock:
        notes = load()
        note = next((a for a in notes if a.get("id") == note_id), None)
        if note is None:
            return None
        if fields.get("status") == "resolved" and note.get("status") != "resolved":
            note["resolved_at"] = _now()
        note.update(fields)
        _save(notes)
    return note


def remove(note_id: str) -> bool:
    with _lock:
        notes = load()
        kept = [a for a in notes if a.get("id") != note_id]
        if len(kept) == len(notes):
            return False
        _save(kept)
    return True
