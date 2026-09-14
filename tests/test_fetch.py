"""`pnt backfill`: fetching a game's log by link. No network -- pages are faked."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from pnt import cli
from pnt.ingest import fetch as f
from pnt.ingest import log_folder as lf
from pnt.ingest.csv_source import RawEntry, read_csv

GID = "pgltDzcp7-NBx28QZGFom_fKX"


def _game(n: int) -> list[RawEntry]:
    """n lines, ascending, with orders shaped like the real thing (ms * 100 + seq)."""
    return [
        RawEntry(178900000000000 + i * 100, f"2026-09-10T00:00:{i % 60:02d}.000Z", f"line {i}")
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


def test_backfill_reports_a_bad_game_and_exits_nonzero(tmp_path, monkeypatch):
    def missing(game_id, before, cookie):
        raise f.FetchError(f"{game_id}: no such game (404)")

    monkeypatch.setattr(f, "fetch_page", missing)
    result = CliRunner().invoke(cli.app, ["backfill", "pglNOPE", "--log-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "no such game" in result.output
    assert not list(tmp_path.iterdir())
