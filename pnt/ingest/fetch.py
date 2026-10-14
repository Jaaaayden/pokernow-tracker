"""Download a game's log from its PokerNow link, as the CSV export would have it.

For backfilling games played before live capture, without clicking "download full
log" on each one. The export CSV itself (`poker_now_log_<id>.csv`) answers 403 to a
script, but the `/log` endpoint the table reads is open, and its lines are the
export's rows: `msg`, `at` and `created_at` are `entry`, `at` and `order`, byte for
byte (docs/findings.md §8).

How the endpoint pages, and the rate limit, are the same ones `pnt/extension/pager.js`
works around: newest first, at most PAGE_SIZE lines, `before_at=<created_at>` (exclusive)
pages backwards, and quick requests get HTTP 429.

Without cookies the log is the export minus two kinds of line: `Your hand is` --
served only to the player dealt the cards -- and `Undealt cards:`, the rabbit hunt.
With both the `npt` and `apt` cookies it is the export exactly: 2,819 of 2,819 lines
identical in text, `at` and `order` (2026-09-13). `npt` alone is not enough; `apt`
is the one that says who you are (docs/findings.md §8).
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from .csv_source import RawEntry

BASE_URL = "https://www.pokernow.com"
PAGE_SIZE = 50

#: Seconds between requests. Three requests in about two seconds drew a 429, and a
#: 30-game backfill at 1.5 s was throttled for five minutes straight; 3 s is what the
#: extension's history walk uses. Each 429 slows the pace further, up to MAX_PAUSE.
PAUSE = 3.0
MAX_PAUSE = 10.0
#: Per page. Backoff doubles from 5 s and is capped, so this is ~20 minutes of waiting
#: before a game is given up on -- losing its pages is worse than waiting.
MAX_RETRIES = 14
MAX_BACKOFF = 120.0

_USER_AGENT = "Mozilla/5.0 (pokernow-tracker backfill)"
_ID = re.compile(r"[A-Za-z0-9_-]+")
_URL_ID = re.compile(r"/games/(?P<gid>[A-Za-z0-9_-]+)")


class FetchError(Exception):
    """A game that cannot be fetched: a bad link, or PokerNow refusing."""


class RateLimited(Exception):
    def __init__(self, wait: float | None):
        super().__init__("rate limited by PokerNow")
        self.wait = wait  # from Retry-After, or None


def game_id_from_link(link: str) -> str:
    """`https://www.pokernow.com/games/pglX...?foo` -> `pglX...`. A bare ID passes through."""
    link = link.strip()
    m = _URL_ID.search(link)
    if m:
        return m.group("gid")
    if _ID.fullmatch(link):
        return link
    raise FetchError(f"not a PokerNow game link or ID: {link!r}")


def _cookie_header(cookie: str) -> str:
    """A full `npt=...; apt=...` header is sent as-is; a bare value is taken as `npt`."""
    cookie = cookie.strip()
    return cookie if "=" in cookie else f"npt={cookie}"


def fetch_page(game_id: str, before: int | None, cookie: str | None = None) -> list[RawEntry]:
    """One request: the newest PAGE_SIZE lines older than `before` (all lines, if None)."""
    url = (
        f"{BASE_URL}/games/{game_id}/log"
        f"?after_at=&before_at={'' if before is None else before}&mm=false"
    )
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    if cookie:
        headers["Cookie"] = _cookie_header(cookie)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
            body = json.load(r)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            retry_after = exc.headers.get("Retry-After")
            raise RateLimited(float(retry_after) if retry_after else None) from exc
        if exc.code == 404:
            raise FetchError(f"{game_id}: no such game (404)") from exc
        raise FetchError(f"{game_id}: PokerNow answered HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"{game_id}: could not reach PokerNow ({exc.reason})") from exc
    return [
        RawEntry(ord=int(line["created_at"]), at=line["at"], entry=line["msg"])
        for line in body.get("logs", [])
    ]


class Pacer:
    """Keeps requests PAUSE apart, across games as well as pages -- the limit is per client."""

    def __init__(
        self,
        pause: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.pause = PAUSE if pause is None else pause
        self.sleep, self.clock = sleep, clock
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            remaining = self.pause - (self.clock() - self._last)
            if remaining > 0:
                self.sleep(remaining)
        self._last = self.clock()


def fetch_log(
    game_id: str,
    cookie: str | None = None,
    *,
    get_page: Callable[[str, int | None, str | None], list[RawEntry]] | None = None,
    pacer: Pacer | None = None,
    on_page: Callable[[int, int], None] | None = None,
    on_wait: Callable[[float], None] | None = None,
) -> list[RawEntry]:
    """Every line of a game, ascending by `order`. Walks back from the newest page.

    `on_wait(seconds)` is told before each rate-limit backoff, so a caller can say why
    nothing is happening.
    """
    get_page = get_page or fetch_page
    pacer = pacer or Pacer()
    seen: dict[int, RawEntry] = {}
    before: int | None = None
    pages = 0
    while True:
        for attempt in range(MAX_RETRIES + 1):
            pacer.wait()
            try:
                page = get_page(game_id, before, cookie)
                break
            except RateLimited as exc:
                if attempt == MAX_RETRIES:
                    raise FetchError(
                        f"{game_id}: still rate limited after {attempt} retries"
                    ) from exc
                wait = exc.wait if exc.wait is not None else min(5 * 2**attempt, MAX_BACKOFF)
                pacer.pause = min(max(pacer.pause, 1.0) * 1.5, MAX_PAUSE)
                if on_wait:
                    on_wait(wait)
                pacer.sleep(wait)
        pages += 1
        if not page:
            break
        oldest = min(e.ord for e in page)
        if before is not None and oldest >= before:
            # Carrying on would fetch the same page forever; stopping would pass off
            # a partial log as the whole game. Neither is acceptable.
            raise FetchError(f"{game_id}: /log ignored before_at; refusing to guess")
        added = 0
        for e in page:
            if e.ord not in seen:
                seen[e.ord] = e
                added += 1
        if on_page:
            on_page(pages, len(seen))
        if len(page) < PAGE_SIZE or added == 0:
            break
        before = oldest
    return sorted(seen.values(), key=lambda e: e.ord)


def count_hero_lines(entries: list[RawEntry]) -> int:
    return sum(e.entry.startswith("Your hand is") for e in entries)
