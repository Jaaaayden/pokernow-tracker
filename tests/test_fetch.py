"""`pnt backfill`: fetching a game's log by link. No network -- pages are faked."""

from __future__ import annotations

import email.message
import email.utils
import http.client
import io
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from pnt import cli
from pnt.ingest import fetch as f
from pnt.ingest import log_folder as lf
from pnt.ingest.csv_source import RawEntry, read_csv

GID = "pgltDzcp7-NBx28QZGFom_fKX"


def _game(n: int) -> list[RawEntry]:
    """n lines, ascending, with orders shaped like the real thing (ms * 100 + seq).

    The first opens a hand: backfill writes nothing for a game no hand was dealt in.
    """
    return [
        RawEntry(
            178900000000000 + i * 100,
            f"2026-09-10T00:00:{i % 60:02d}.000Z",
            "-- starting hand #1 --" if i == 0 else f"line {i}",
        )
        for i in range(n)
    ]


class FakeLog:
    """Serves /log the way PokerNow does: newest first, PAGE_SIZE at most, before exclusive."""

    def __init__(self, entries: list[RawEntry]):
        self.entries = entries
        self.calls: list[int | None] = []

    def __call__(self, game_id, before, cookie):
        self.calls.append(before)
        older = [e for e in self.entries if before is None or e.ord < before]
        return sorted(older, key=lambda e: e.ord, reverse=True)[: f.PAGE_SIZE]


def _pacer():
    return f.Pacer(pause=0, sleep=lambda s: None)


@pytest.mark.parametrize(
    "link",
    [
        f"https://www.pokernow.com/games/{GID}",
        f"https://pokernow.com/games/{GID}/",
        f"https://www.pokernow.club/games/{GID}?foo=bar",
        f"  {GID}  ",
    ],
)
def test_game_id_from_link(link):
    assert f.game_id_from_link(link) == GID


def test_rejects_a_link_that_is_not_a_game():
    with pytest.raises(f.FetchError):
        f.game_id_from_link("https://www.pokernow.com/start-game")


@pytest.mark.parametrize("link", ["a" * 65, f"https://www.pokernow.com/games/{'a' * 65}"])
def test_rejects_an_id_the_log_folder_could_not_name_a_file_after(link):
    """`log_path` refuses it, and backfill called that outside its try."""
    with pytest.raises(f.FetchError):
        f.game_id_from_link(link)


class _Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code: int, retry_after: str | None = None):
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://www.pokernow.com", code, "", headers, None)


def _cut_short(*a, **k):
    class Reset(_Answer):
        def read(self, *a):
            raise http.client.IncompleteRead(b"{")

    return Reset(b"")


def _timed_out(*a, **k):
    class Slow(_Answer):
        def read(self, *a):
            raise TimeoutError("The read operation timed out")

    return Slow(b"")


def _raise(exc):
    def urlopen(*a, **k):
        raise exc

    return urlopen


@pytest.mark.parametrize(
    "urlopen, error",
    [
        (lambda *a, **k: _Answer(b"<html>Just a moment...</html>"), f.FetchError),  # Cloudflare
        (lambda *a, **k: _Answer(b"[]"), f.FetchError),
        (lambda *a, **k: _Answer(b'{"logs": [{"msg": "x"}]}'), f.FetchError),
        (lambda *a, **k: _Answer(b'{"logs": 5}'), f.FetchError),
        (_timed_out, f.TransientFetchError),
        (_cut_short, f.TransientFetchError),
        # Cut short with no Content-Length: the transport reports nothing wrong.
        (lambda *a, **k: _Answer(b'{"logs": [{"msg": "x", "at": "2026-'), f.TransientFetchError),
        (lambda *a, **k: _Answer('{"logs": [{"msg": "♠'.encode()[:-1]), f.TransientFetchError),
        (lambda *a, **k: _Answer(b""), f.TransientFetchError),
        (lambda *a, **k: _Answer(b"Service Unavailable"), f.FetchError),
        (_raise(ConnectionResetError(10054, "reset")), f.TransientFetchError),
        (_raise(urllib.error.URLError("no route")), f.TransientFetchError),
        (_raise(_http_error(500)), f.FetchError),
    ],
)
def test_a_bad_answer_is_a_fetch_error(monkeypatch, urlopen, error):
    """Anything else escaped as itself and ended the whole backfill there."""
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(error):
        f.fetch_page(GID, None)


@pytest.mark.parametrize(
    "header, wait",
    [
        ("7", 7.0),
        ("Wed, 21 Oct 2015 07:28:00 GMT", 0.0),  # a date gone by: go now
        ("whenever", None),
        ("nan", None),
        ("1e12", f.MAX_BACKOFF),
    ],
)
def test_retry_after_as_seconds_or_a_date(monkeypatch, header, wait):
    monkeypatch.setattr(urllib.request, "urlopen", _raise(_http_error(429, header)))
    with pytest.raises(f.RateLimited) as exc:
        f.fetch_page(GID, None)
    assert exc.value.wait == wait


def test_retry_after_a_date_ahead_waits_until_then():
    ahead = email.utils.format_datetime(datetime.now(UTC) + timedelta(seconds=60), usegmt=True)
    assert 55 <= f._retry_after(ahead) <= 60


def test_a_page_lost_on_the_way_is_fetched_again():
    game = _game(120)
    log = FakeLog(game)
    lost = iter([f.TransientFetchError("timed out"), f.TransientFetchError("reset")])
    slept: list[float] = []

    def flaky(game_id, before, cookie):
        if before is not None:
            err = next(lost, None)
            if err:
                raise err
        return log(game_id, before, cookie)

    pacer = f.Pacer(pause=0, sleep=slept.append)
    assert f.fetch_log(GID, get_page=flaky, pacer=pacer) == game
    assert slept == [5, 10]


def test_an_answer_cut_short_without_an_error_is_fetched_again(monkeypatch):
    """No Content-Length, so the transport cannot tell: the JSON just stops."""
    import json

    whole = json.dumps({"logs": [
        {"created_at": e.ord, "at": e.at, "msg": e.entry} for e in reversed(_game(3))
    ]}).encode()
    answers = iter([whole[: len(whole) // 2], whole])
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Answer(next(answers)))
    slept: list[float] = []
    assert f.fetch_log(GID, pacer=f.Pacer(pause=0, sleep=slept.append)) == _game(3)
    assert slept == [5]


def test_a_page_lost_every_time_gives_the_game_up():
    calls = 0

    def gone(game_id, before, cookie):
        nonlocal calls
        calls += 1
        raise f.TransientFetchError("timed out")

    with pytest.raises(f.TransientFetchError):
        f.fetch_log(GID, get_page=gone, pacer=_pacer())
    assert calls == f.MAX_TRANSIENT_RETRIES + 1


def test_backfill_carries_on_past_a_game_that_fails(tmp_path, monkeypatch):
    """A Cloudflare page for one game: that game is counted failed, the next fetched."""
    good = FakeLog(_game(3))

    def page(game_id, before, cookie):
        if game_id == "pglBAD":
            raise f.FetchError(f"{game_id}: PokerNow's answer was not a log")
        return good(game_id, before, cookie)

    monkeypatch.setattr(f, "fetch_page", page)
    monkeypatch.setattr(f, "PAUSE", 0)
    result = CliRunner().invoke(cli.app, ["backfill", "pglBAD", GID, "--log-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "not a log" in result.output
    assert lf.log_path(tmp_path, GID).exists()


@pytest.mark.parametrize("n", [0, 1, 49, 50, 51, 100, 137])
def test_walks_back_to_the_first_line(n):
    game = _game(n)
    log = FakeLog(game)
    assert f.fetch_log(GID, get_page=log, pacer=_pacer()) == game
    assert log.calls[0] is None  # newest page first


def test_retries_after_a_429_and_honours_retry_after():
    game = _game(120)
    log = FakeLog(game)
    slept: list[float] = []
    failures = iter([f.RateLimited(7.0), f.RateLimited(None)])

    def flaky(game_id, before, cookie):
        if before is not None and len(log.calls) == 1:
            err = next(failures, None)
            if err:
                raise err
        return log(game_id, before, cookie)

    pacer = f.Pacer(pause=0, sleep=lambda s: None)
    assert f.fetch_log(GID, get_page=flaky, pacer=pacer, on_wait=slept.append) == game
    assert slept == [7.0, 10.0]  # Retry-After, then backoff for the second attempt
    assert pacer.pause > 0  # and the pace slowed down for the rest of the walk


def test_backoff_is_capped():
    calls = 0

    def always_limited(game_id, before, cookie):
        nonlocal calls
        calls += 1
        raise f.RateLimited(None)

    waits: list[float] = []
    with pytest.raises(f.FetchError, match="still rate limited"):
        f.fetch_log(
            GID,
            get_page=always_limited,
            pacer=f.Pacer(pause=0, sleep=lambda s: None),
            on_wait=waits.append,
        )
    assert calls == f.MAX_RETRIES + 1
    assert max(waits) == f.MAX_BACKOFF


def test_refuses_an_endpoint_that_ignores_before_at():
    page = list(reversed(_game(f.PAGE_SIZE)))
    shifted = [RawEntry(e.ord + 10**6, e.at, e.entry) for e in page]
    pages = iter([page, shifted])
    with pytest.raises(f.FetchError, match="ignored before_at"):
        f.fetch_log(GID, get_page=lambda *a: next(pages), pacer=_pacer())


def test_cookie_header_accepts_a_bare_value_or_a_full_header():
    assert f._cookie_header("abc") == "npt=abc"
    assert f._cookie_header(" npt=abc; apt=xyz ") == "npt=abc; apt=xyz"


def test_backfill_warns_when_the_cookie_lacks_apt(tmp_path, monkeypatch):
    monkeypatch.setattr(f, "fetch_page", FakeLog(_game(3)))
    result = CliRunner().invoke(
        cli.app, ["backfill", GID, "--log-dir", str(tmp_path)], env={"PNT_COOKIE": "npt=abc"}
    )
    assert result.exit_code == 0, result.output
    assert "no apt=" in result.output


def test_written_log_reads_back_like_an_export(tmp_path):
    tricky = [
        RawEntry(
            178900000000100, "2026-09-10T00:00:01.000Z", 'The player "gurt @ gpP9uUffpu" joined'
        ),
        RawEntry(
            178900000000200, "2026-09-10T00:00:02.000Z", "Game Config Changes\n* Bomb Pot: off"
        ),
        RawEntry(178900000000300, "2026-09-10T00:00:03.000Z", "-- ending hand #1 --"),
    ]
    path = lf.log_path(tmp_path, GID)
    assert lf.write_log(tricky, path) == 3
    assert read_csv(path) == tricky
    text = path.read_text(encoding="utf-8")
    assert text.startswith('entry,at,order\n"-- ending hand #1 --",')  # newest first
    assert "\r\n" not in text


def test_rewriting_merges_instead_of_overwriting(tmp_path):
    game = _game(10)
    path = lf.log_path(tmp_path, GID)
    lf.write_log(game[:6], path)
    hero = RawEntry(game[3].ord + 1, game[3].at, "Your hand is A♠, K♥")
    assert lf.write_log(game + [hero], path) == 5
    back = read_csv(path)
    assert len(back) == 11 and hero in back
    assert f.count_hero_lines(back) == 1


def test_backfill_writes_into_the_log_folder_and_skips_what_is_there(tmp_path, monkeypatch):
    game = _game(75)
    log = FakeLog(game)
    monkeypatch.setattr(f, "fetch_page", log)
    monkeypatch.setattr(f, "PAUSE", 0)
    monkeypatch.delenv("PNT_COOKIE", raising=False)
    links = tmp_path / "links.txt"
    links.write_text(f"# old games\nhttps://www.pokernow.com/games/{GID}\n\n", encoding="utf-8")
    folder = tmp_path / "logs"
    runner = CliRunner()

    result = runner.invoke(cli.app, ["backfill", "-f", str(links), "--log-dir", str(folder)])
    assert result.exit_code == 0, result.output
    assert read_csv(lf.log_path(folder, GID)) == game
    assert "no cookie" in result.output
    assert len(log.calls) == 2

    result = runner.invoke(cli.app, ["backfill", GID, "--log-dir", str(folder)])
    assert result.exit_code == 0, result.output
    assert "skipped" in result.output
    assert len(log.calls) == 2  # nothing fetched


def test_backfill_writes_nothing_for_a_game_with_no_hands(tmp_path, monkeypatch):
    joined_and_left = [RawEntry(e.ord, e.at, f"seat request {i}") for i, e in enumerate(_game(3))]
    monkeypatch.setattr(f, "fetch_page", FakeLog(joined_and_left))
    monkeypatch.setattr(f, "PAUSE", 0)
    result = CliRunner().invoke(cli.app, ["backfill", GID, "--log-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "no hands were dealt" in result.output
    assert not lf.log_path(tmp_path, GID).exists()


def test_backfill_reports_a_bad_game_and_exits_nonzero(tmp_path, monkeypatch):
    def missing(game_id, before, cookie):
        raise f.FetchError(f"{game_id}: no such game (404)")

    monkeypatch.setattr(f, "fetch_page", missing)
    result = CliRunner().invoke(cli.app, ["backfill", "pglNOPE", "--log-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "no such game" in result.output
    assert not list(tmp_path.iterdir())
