"""The background server: task definition, supervision, and install guards.

Nothing here touches the real Task Scheduler or launchd -- `schtasks` and
`launchctl` are replaced by recorders, and time by a fake clock. Registering,
starting, stopping and restarting a real task were checked by hand on Windows 11.
"""

from __future__ import annotations

import copy
import itertools
import plistlib
import socket
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from pnt import service as svc

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


# -------------------------------------------------------- task definition ---


def _task(**overrides) -> ET.Element:
    kw = {
        "command": r"C:\venv\Scripts\pythonw.exe",
        "arguments": "-m pnt.cli service run",
        "working_dir": r"C:\data",
        "user": r"PC\me",
    }
    kw.update(overrides)
    # Parsed from the same UTF-16 bytes `install` writes for schtasks.
    return ET.fromstring(svc.task_xml(**kw).encode("utf-16"))


def _text(root: ET.Element, path: str) -> str | None:
    return root.find(path, NS).text


def test_task_never_times_out_and_keeps_running_on_battery():
    root = _task()
    assert _text(root, "t:Settings/t:ExecutionTimeLimit") == "PT0S"
    assert _text(root, "t:Settings/t:DisallowStartIfOnBatteries") == "false"
    assert _text(root, "t:Settings/t:StopIfGoingOnBatteries") == "false"


def test_task_runs_at_normal_priority():
    # Task Scheduler's default (7) is below normal; 4-6 is NORMAL_PRIORITY_CLASS.
    assert _text(_task(), "t:Settings/t:Priority") == "5"


def test_task_starts_at_this_users_logon_without_elevation():
    root = _task(user=r"PC\me")
    assert _text(root, "t:Triggers/t:LogonTrigger/t:UserId") == r"PC\me"
    assert _text(root, "t:Principals/t:Principal/t:UserId") == r"PC\me"
    assert _text(root, "t:Principals/t:Principal/t:RunLevel") == "LeastPrivilege"


def test_paths_with_xml_metacharacters_round_trip():
    args = '-m pnt.cli service run --db "C:\\R&D <poker>\\pokernow.sqlite"'
    root = _task(arguments=args, working_dir="C:\\R&D <poker>")
    assert _text(root, "t:Actions/t:Exec/t:Arguments") == args
    assert _text(root, "t:Actions/t:Exec/t:WorkingDirectory") == "C:\\R&D <poker>"


# -------------------------------------------------------------- supervise ---


class FakeTime:
    """A clock that moves only when something sleeps or a run takes time."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []
        self.logs: list[str] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def log(self, message: str) -> None:
        self.logs.append(message)


def _runs(fake: FakeTime, *outcomes):
    """A `run` whose Nth call lasts outcomes[N][0] seconds, then raises outcomes[N][1]
    (or returns, when that is None)."""
    it = iter(outcomes)

    def run():
        seconds, end = next(it)
        fake.now += seconds
        if end is not None:
            raise end

    return run


def _supervise(fake: FakeTime, run, port_busy=lambda: False):
    svc.supervise(run, port_busy=port_busy, log=fake.log, sleep=fake.sleep, clock=fake.clock)


def test_failures_restart_with_doubling_backoff_and_a_clean_stop_ends_it():
    f = FakeTime()
    _supervise(
        f,
        _runs(f, (0, SystemExit(1)), (0, RuntimeError("boom")), (0, SystemExit(3)), (5, None)),
    )
    assert f.sleeps == [1, 2, 4]
    assert "RuntimeError: boom" in f.logs[1], "the traceback reaches the log"
    assert f.logs[-1] == "server stopped"


def test_backoff_is_capped():
    f = FakeTime()
    _supervise(f, _runs(f, *[(0, SystemExit(1))] * 9, (0, None)))
    assert f.sleeps == [1, 2, 4, 8, 16, 32, 60, 60, 60]


def test_a_healthy_run_resets_the_backoff():
    f = FakeTime()
    _supervise(
        f, _runs(f, (0, SystemExit(1)), (0, SystemExit(1)), (3600, SystemExit(1)), (0, None))
    )
    assert f.sleeps == [1, 2, 1]


def test_keyboard_interrupt_is_a_stop_not_a_crash():
    f = FakeTime()
    with pytest.raises(KeyboardInterrupt):
        _supervise(f, _runs(f, (0, KeyboardInterrupt())))
    assert f.sleeps == []


def test_a_busy_port_is_waited_on_not_crash_looped():
    """A terminal `pnt serve` holds the port; the task must not fight it."""
    f = FakeTime()
    busy = iter([True, True, True, False])
    calls = []
    _supervise(f, lambda: calls.append(1), port_busy=lambda: next(busy))
    assert calls == [1]
    assert f.sleeps == [svc.PORT_POLL_SECONDS] * 3
    assert sum("waiting" in m for m in f.logs) == 1, "log the wait once, not every poll"


def test_port_in_use_detects_a_listener():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert svc.port_in_use("127.0.0.1", port)
    assert not svc.port_in_use("127.0.0.1", port)


def test_a_second_server_cannot_share_the_port():
    """Two servers on one port split its requests between them.

    uvicorn's own SO_REUSEADDR allows exactly that on Windows: after an update, an
    old server left running went on answering beside the new one, and the pages
    said the server was older than them however often the service restarted.
    """
    first = svc.bind("127.0.0.1", 0)
    try:
        first.listen()
        port = first.getsockname()[1]
        with pytest.raises(OSError):
            svc.bind("127.0.0.1", port)
        # Nor may an older server, binding the way uvicorn does, join it.
        with socket.socket() as old:
            old.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with pytest.raises(OSError):
                old.bind(("127.0.0.1", port))
                old.listen()
    finally:
        first.close()
    svc.bind("127.0.0.1", port).close()  # and the port is free again once it closes


NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1240
  TCP    127.0.0.1:52000        0.0.0.0:0              LISTENING       16084
  TCP    127.0.0.1:52000        0.0.0.0:0              ABHÖREN         39440
  TCP    127.0.0.1:52000        127.0.0.1:61234        ESTABLISHED     16084
  TCP    127.0.0.1:520001       0.0.0.0:0              LISTENING       7
  TCP    [::1]:52000            [::]:0                 LISTENING       16084
"""


def test_listening_pids_reads_netstat_in_any_language(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        svc.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, NETSTAT, "")
    )
    assert svc.listening_pids(52000) == [16084, 39440]
    assert "Stop-Process -Id 16084, 39440" in svc.port_holder_hint(52000)


def test_listening_pids_reads_lsof_on_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        svc.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "812\n812\n903\n", "")
    )
    assert svc.listening_pids(52000) == [812, 903]
    assert "kill 812 903" in svc.port_holder_hint(52000)


def test_listening_pids_names_none_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert svc.listening_pids(52000) == []
    assert "pnt serve" in svc.port_holder_hint(52000)


@pytest.fixture
def restarting(monkeypatch):
    """svc.restart with the task and the clock faked; set `answers` to /health bodies."""
    f = FakeTime()
    calls = []
    monkeypatch.setattr(svc, "stop", lambda name: calls.append("stop"))
    monkeypatch.setattr(svc, "_await_stopped", lambda name, timeout: None)
    monkeypatch.setattr(svc, "start", lambda name: calls.append("start"))
    monkeypatch.setattr(svc.time, "sleep", f.sleep)
    monkeypatch.setattr(svc.time, "monotonic", f.clock)
    monkeypatch.setattr(svc, "listening_pids", lambda port: [16084])
    state = {"answers": iter(())}
    monkeypatch.setattr(svc, "health", lambda host, port: next(state["answers"]))
    return state, calls


def test_restart_returns_once_the_new_server_answers(restarting):
    state, calls = restarting
    old, new = {"pid": 1, "hands": 5, "db": "x"}, {"pid": 2, "hands": 5, "db": "x"}
    state["answers"] = iter([old, None, None, new])
    assert svc.restart() == new
    assert calls == ["stop", "start"]


def test_restart_names_a_server_that_outlived_it(restarting):
    """The old server is not the task's: it keeps the port, and saying "restarted"
    would leave its old code answering."""
    state, _ = restarting
    stray = {"pid": 16084, "hands": 5, "db": "x"}
    state["answers"] = itertools.repeat(stray)
    with pytest.raises(svc.ServiceError, match="Stop-Process -Id 16084"):
        svc.restart()


def test_restart_does_not_mistake_a_server_too_old_to_name_itself(restarting):
    state, _ = restarting
    state["answers"] = itertools.repeat({"hands": 5, "db": "x"})
    with pytest.raises(svc.ServiceError, match="another server still answers"):
        svc.restart()


def test_restart_says_when_nothing_comes_back(restarting):
    state, _ = restarting
    state["answers"] = itertools.chain([{"pid": 1}], itertools.repeat(None))
    with pytest.raises(svc.ServiceError, match="pnt service log"):
        svc.restart()


def test_serve_refuses_a_port_the_tracker_already_holds(monkeypatch):
    from typer.testing import CliRunner

    from pnt.cli import app

    monkeypatch.setattr(svc, "port_in_use", lambda host, port: True)
    monkeypatch.setattr(svc, "health", lambda host, port: {"pid": 4242})
    monkeypatch.setattr(svc, "run_server", lambda *a, **k: pytest.fail("must not start"))
    result = CliRunner().invoke(app, ["serve"])
    assert result.exit_code == 1
    assert "already running there (pid 4242" in result.output


# ----------------------------------------------------------------- logging ---


def test_log_rotates_once_past_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "LOG_MAX_BYTES", 10)
    log = tmp_path / "server.log"
    log.write_text("x" * 50, encoding="utf-8")
    with svc._open_log(log) as stream:
        stream.write("fresh\n")
    assert (tmp_path / "server.log.1").read_text(encoding="utf-8") == "x" * 50
    assert log.read_text(encoding="utf-8") == "fresh\n"


def test_file_log_config_is_timestamped_and_leaves_uvicorn_alone():
    uvicorn_config = pytest.importorskip("uvicorn.config")
    before = copy.deepcopy(uvicorn_config.LOGGING_CONFIG)
    for formatter in svc._file_log_config()["formatters"].values():
        assert formatter["fmt"].startswith("%(asctime)s ")
        assert formatter["use_colors"] is False
    assert uvicorn_config.LOGGING_CONFIG == before


def test_the_trackers_own_lines_reach_the_log_with_a_time(tmp_path):
    """Only uvicorn's loggers were set up, so every `pnt.*` info line was dropped
    and a warning came out bare, through Python's last resort."""
    import logging
    import logging.config

    pytest.importorskip("uvicorn")
    out = tmp_path / "server.log"
    config = svc._file_log_config()
    config["handlers"]["default"]["stream"] = out.open("w", encoding="utf-8")
    try:
        logging.config.dictConfig(config)
        logging.getLogger("pnt.server.app").info("imported a log")
        logging.getLogger("pnt.ingest.sync").warning("could not import a log")
    finally:
        config["handlers"]["default"]["stream"].close()
        for name in ("pnt", "uvicorn", "uvicorn.error", "uvicorn.access"):
            logger = logging.getLogger(name)
            logger.handlers.clear()
            logger.propagate = True
            logger.setLevel(logging.NOTSET)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "INFO" in lines[0] and lines[0].endswith(" imported a log")
    assert "WARNING" in lines[1] and lines[1].endswith(" could not import a log")
    assert all(line[:4].isdigit() for line in lines)  # a timestamp first


def test_run_refuses_a_missing_database(tmp_path, monkeypatch):
    """Rather than serve an empty database it just created."""
    monkeypatch.setattr(svc, "LOG_FILE", tmp_path / "server.log")
    with pytest.raises(SystemExit) as exc:
        svc.run_service(tmp_path / "missing.sqlite", "127.0.0.1", 8000)
    assert exc.value.code == 2
    assert not (tmp_path / "missing.sqlite").exists()
    assert "no database" in (tmp_path / "server.log").read_text(encoding="utf-8")
    assert sys.stdout is not None and not sys.stdout.closed, "streams are restored"


def test_tail(tmp_path):
    log = tmp_path / "server.log"
    log.write_text("".join(f"line {i}\n" for i in range(100)), encoding="utf-8")
    assert svc.tail(log, 3) == ["line 97", "line 98", "line 99"]


# ----------------------------------------------------------------- install ---


@pytest.fixture()
def schtasks(monkeypatch, tmp_path):
    """Pretend to be Windows and record schtasks calls instead of making them."""
    calls: list[tuple[str, ...]] = []

    def fake(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(svc, "_schtasks", fake)
    monkeypatch.setattr(svc, "LOG_DIR", tmp_path / "state")
    # No task registered, unless a test says otherwise. Without this the real
    # Task Scheduler is consulted, so these tests would pass or fail depending on
    # whether the machine running them happens to have the tracker installed.
    monkeypatch.setattr(svc, "task_state", lambda name=svc.TASK_NAME: None)
    monkeypatch.setattr(svc.time, "sleep", lambda _: None)
    return calls


def test_install_refuses_a_database_that_does_not_exist(schtasks, tmp_path):
    with pytest.raises(svc.ServiceError, match="no database"):
        svc.install(tmp_path / "missing.sqlite")
    assert schtasks == [], "nothing was registered"


def test_install_registers_the_absolute_database_path_then_starts(schtasks, tmp_path, monkeypatch):
    (tmp_path / "pokernow.sqlite").touch()
    monkeypatch.chdir(tmp_path)
    svc.install(Path("pokernow.sqlite"), port=8123)

    assert [call[0] for call in schtasks] == ["/Create", "/Run"]
    create = schtasks[0]
    spec = Path(create[create.index("/XML") + 1]).read_text(encoding="utf-16")
    assert str((tmp_path / "pokernow.sqlite").resolve()) in spec
    assert "-m pnt.cli service run" in spec
    assert "--port 8123" in spec


def test_state_lives_outside_appdata():
    """Microsoft Store Python redirects a new folder under AppData into its private
    package cache. schtasks cannot see in there, so registration failed with "the
    system cannot find the path specified" -- and the log would be invisible too."""
    assert "appdata" not in str(svc.LOG_DIR).lower()


def test_service_commands_explain_themselves_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(svc.ServiceError, match="Task Scheduler"):
        svc.task_state()


def test_cli_exposes_the_service_commands():
    from typer.testing import CliRunner

    from pnt.cli import app

    out = CliRunner().invoke(app, ["service", "--help"]).output
    for command in ("install", "uninstall", "start", "stop", "restart", "status", "log"):
        assert command in out


def test_reinstalling_ends_the_running_instance_first(schtasks, tmp_path, monkeypatch):
    """Changing --db or --port on a live install must actually take effect.

    The task is registered MultipleInstancesPolicy=IgnoreNew, so `/Run` is silently
    ignored while an instance is alive. Without ending it first, `install` rewrote
    the definition, reported success, and left the old server running on the old
    port -- which is exactly how the port move failed the first time.
    """
    (tmp_path / "pokernow.sqlite").touch()
    # install asks once, stop asks twice (installed? running?), then the wait asks
    # until it is no longer running.
    states = iter(["Running", "Running", "Running", "Ready"])
    monkeypatch.setattr(svc, "task_state", lambda name=svc.TASK_NAME: next(states, "Ready"))

    svc.install(tmp_path / "pokernow.sqlite", port=52000)

    assert [call[0] for call in schtasks] == ["/End", "/Create", "/Run"], (
        "the old instance must be ended before the new definition is started"
    )


def test_a_fresh_install_does_not_try_to_end_anything(schtasks, tmp_path):
    (tmp_path / "pokernow.sqlite").touch()
    svc.install(tmp_path / "pokernow.sqlite", port=52000)
    assert [call[0] for call in schtasks] == ["/Create", "/Run"]


# ------------------------------------------------------------------ launchd ---


class FakeLaunchd:
    """launchctl as far as `pnt service` uses it: one agent, loaded or not."""

    def __init__(self):
        self.calls: list[tuple[str, ...]] = []
        self.loaded = False
        self.pid: int | None = None

    def __call__(self, *args):
        self.calls.append(args)
        verb = args[0]
        if verb == "list":
            if not self.loaded:
                return subprocess.CompletedProcess(args, 113, "", "Could not find service")
            pid = f'\t"PID" = {self.pid};\n' if self.pid else ""
            out = '{\n\t"Label" = "x";\n' + pid + "};"
            return subprocess.CompletedProcess(args, 0, out, "")
        if verb == "bootstrap":
            self.loaded, self.pid = True, 4242
        elif verb == "bootout":
            self.loaded, self.pid = False, None
        elif verb == "kickstart":
            self.pid = 4243
        return subprocess.CompletedProcess(args, 0, "", "")

    def verbs(self) -> list[str]:
        return [c[0] for c in self.calls if c[0] != "list"]


@pytest.fixture()
def launchd(monkeypatch, tmp_path):
    """Pretend to be macOS, with the agent's plist and logs under tmp_path."""
    fake = FakeLaunchd()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(svc, "_launchctl", fake)
    monkeypatch.setattr(svc.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(svc, "AGENT_PLIST", tmp_path / "LaunchAgents" / "agent.plist")
    monkeypatch.setattr(svc, "AGENT_LOG_FILE", tmp_path / "state" / "launchd.log")
    monkeypatch.setattr(svc.time, "sleep", lambda _: None)
    return fake


def test_agent_install_writes_the_absolute_database_path_then_loads(launchd, tmp_path, monkeypatch):
    (tmp_path / "pokernow.sqlite").touch()
    monkeypatch.chdir(tmp_path)
    interpreter = svc.install(Path("pokernow.sqlite"), port=8123)

    assert interpreter == Path(sys.executable)
    assert launchd.verbs() == ["bootstrap"]
    assert launchd.calls[-1] == ("bootstrap", "gui/501", str(svc.AGENT_PLIST))
    spec = plistlib.loads(svc.AGENT_PLIST.read_bytes())
    db = str((tmp_path / "pokernow.sqlite").resolve())
    assert spec["ProgramArguments"][1:] == [
        "-m", "pnt.cli", "service", "run", "--db", db, "--host", "127.0.0.1", "--port", "8123",
    ]  # fmt: skip
    assert spec["WorkingDirectory"] == str(tmp_path.resolve())
    assert svc.task_state() == "Running"


def test_agent_starts_at_login_restarts_on_crash_and_is_not_throttled():
    spec = plistlib.loads(svc.agent_plist(program=["python"], working_dir="/data"))
    assert spec["Label"] == svc.AGENT_LABEL
    assert spec["RunAtLoad"] is True
    assert spec["KeepAlive"] == {"SuccessfulExit": False}, "a stop must stay stopped"
    assert spec["ThrottleInterval"] == 60
    assert spec["ProcessType"] == "Interactive"


def test_agent_paths_with_plist_metacharacters_round_trip():
    weird = "/Users/me/<poker> & \"friends\"/pokernow.sqlite"
    spec = plistlib.loads(svc.agent_plist(program=["python", "--db", weird], working_dir="/x"))
    assert spec["ProgramArguments"][-1] == weird


def test_agent_reinstall_unloads_the_old_definition_first(launchd, tmp_path):
    """launchd keeps the definition it loaded: swapping the plist under a loaded
    agent would leave the old --db and --port running, as IgnoreNew did on Windows."""
    (tmp_path / "pokernow.sqlite").touch()
    svc.install(tmp_path / "pokernow.sqlite")
    svc.install(tmp_path / "pokernow.sqlite", port=8124)
    assert launchd.verbs() == ["bootstrap", "bootout", "bootstrap"]
    assert "8124" in plistlib.loads(svc.AGENT_PLIST.read_bytes())["ProgramArguments"]


def test_agent_states_match_the_tasks(launchd, tmp_path):
    assert svc.task_state() is None
    (tmp_path / "pokernow.sqlite").touch()
    svc.install(tmp_path / "pokernow.sqlite")
    assert svc.task_state() == "Running"
    launchd.pid = None  # exited, waiting out KeepAlive's throttle
    assert svc.task_state() == "Ready"
    svc.stop()
    assert svc.task_state() == "Ready", "stopped, but still installed: it starts at the next login"


def test_agent_start_loads_an_unloaded_agent_and_kicks_a_loaded_one(launchd, tmp_path):
    (tmp_path / "pokernow.sqlite").touch()
    svc.install(tmp_path / "pokernow.sqlite")
    svc.stop()
    svc.start()
    launchd.pid = None
    svc.start()
    assert launchd.verbs() == ["bootstrap", "bootout", "bootstrap", "kickstart"]


def test_agent_stop_and_wait_returns_once_unloaded(launchd, tmp_path):
    (tmp_path / "pokernow.sqlite").touch()
    svc.install(tmp_path / "pokernow.sqlite")
    svc.stop_and_wait()
    assert not launchd.loaded


def test_agent_uninstall_unloads_and_removes_the_plist(launchd, tmp_path):
    assert svc.uninstall() is False
    (tmp_path / "pokernow.sqlite").touch()
    svc.install(tmp_path / "pokernow.sqlite")
    assert svc.uninstall() is True
    assert not svc.AGENT_PLIST.exists()
    assert not launchd.loaded
    assert svc.task_state() is None


def test_agent_commands_need_it_installed(launchd):
    with pytest.raises(svc.ServiceError, match="not installed"):
        svc.start()
