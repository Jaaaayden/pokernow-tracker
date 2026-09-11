"""Keep the tracker server running in the background: start at login, restart on crash.

`pnt serve` in a terminal lasts until the terminal closes. This module makes the
same server permanent on Windows with a per-user Task Scheduler task.

* **Task Scheduler, not a Windows service.** Installing a service needs admin
  rights, and a service runs as another account in session 0, away from this
  user's files. A task registered for the current user at logon needs neither.
* **Not a Startup-folder shortcut.** A shortcut starts the server once and has no
  state to ask about; a task can be started, stopped, restarted and queried.
* **Restarts happen in-process** (`supervise`). Task Scheduler's restart-on-failure
  is kept as a second layer only: it waits at least a minute between attempts and
  logs nothing about why.

Idle, the server costs tens of megabytes and no measurable CPU; it only does work
while the extension is posting.
"""

from __future__ import annotations

import copy
import json
import os
import socket
import subprocess
import sys
import time
import traceback
import urllib.request
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TextIO
from xml.sax.saxutils import escape

TASK_NAME = "PokerNow Tracker"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

#: A hidden process has no console, so everything it would have printed goes here.
#: Deliberately not under %LOCALAPPDATA%: Microsoft Store Python silently redirects a
#: new folder there into its private package cache, where schtasks cannot read the
#: task definition and Explorer never shows the log.
LOG_DIR = Path.home() / ".pnt"
LOG_FILE = LOG_DIR / "server.log"
#: Checked at each start. With access logging off, a day of play writes far less.
LOG_MAX_BYTES = 5 * 1024 * 1024

#: A run at least this long counts as healthy, so the next crash restarts quickly.
HEALTHY_RUN_SECONDS = 60.0
MAX_BACKOFF_SECONDS = 60.0
#: How often to re-check a port that another process is holding.
PORT_POLL_SECONDS = 15.0


class ServiceError(RuntimeError):
    """Something the user can act on: shown as a one-line CLI error."""


# ------------------------------------------------------------------ running ---


def run_server(db: Path, host: str, port: int, **uvicorn_options) -> None:
    """Run the API in this process until it stops. Shared by `pnt serve` and the task."""
    # The app reads PNT_DB once, at import, so this must be set before uvicorn loads it.
    os.environ["PNT_DB"] = str(db)
    import uvicorn

    uvicorn.run("pnt.server.app:app", host=host, port=port, **uvicorn_options)


def port_in_use(host: str, port: int) -> bool:
    """True when something already accepts connections there.

    Usually a `pnt serve` left running in a terminal. Waiting for it beats
    crash-looping on the bind error.
    """
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def supervise(
    run: Callable[[], None],
    *,
    port_busy: Callable[[], bool],
    log: Callable[[str], None],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Call `run` until it returns normally, restarting it whenever it fails.

    * A normal return is a deliberate shutdown (Ctrl+C in a console) and ends the
      loop. So does `KeyboardInterrupt`, which is not an `Exception`.
    * A failure -- an exception, or uvicorn's `sys.exit` when it cannot bind --
      restarts after a delay that doubles up to a cap. A healthy run resets it.
    * While another process holds the port, nothing starts: the loop waits, so a
      terminal `pnt serve` and the background task never fight over the port.
    """
    delay = 1.0
    waiting = False
    while True:
        if port_busy():
            if not waiting:
                log("port is in use by another process; waiting for it to be released")
                waiting = True
            sleep(PORT_POLL_SECONDS)
            continue
        if waiting:
            log("port released; starting")
            waiting = False

        started = clock()
        try:
            run()
        except SystemExit as exc:
            reason = f"exit code {exc.code}"
        except Exception:  # noqa: BLE001 -- any failure means restart; the traceback is logged
            reason = traceback.format_exc().rstrip()
        else:
            log("server stopped")
            return

        ran = clock() - started
        if ran >= HEALTHY_RUN_SECONDS:
            delay = 1.0
        log(f"server died after {ran:.0f}s ({reason}); restarting in {delay:.0f}s")
        sleep(delay)
        delay = min(delay * 2, MAX_BACKOFF_SECONDS)


def _open_log(path: Path | None = None) -> TextIO:
    path = path or LOG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
        path.replace(path.with_name(path.name + ".1"))
    return open(path, "a", encoding="utf-8", buffering=1)  # the caller closes it


def _file_log_config() -> dict:
    """uvicorn's own logging, timestamped and without colour codes: it goes to a file."""
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    for formatter in config["formatters"].values():
        formatter["fmt"] = "%(asctime)s " + formatter["fmt"]
        formatter["use_colors"] = False
    return config


def run_service(db: Path, host: str, port: int) -> None:
    """What the scheduled task executes: the supervised server, logging to a file."""
    stream = _open_log()
    # Under pythonw, stdout and stderr are None and anything written to them is lost.
    # uvicorn resolves `ext://sys.stderr` when it configures logging, so it follows.
    saved = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = stream

    def log(message: str) -> None:
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        print(f"{stamp} pnt service: {message}", flush=True)

    try:
        db = db.resolve()
        if not db.exists():
            log(f"no database at {db}; not starting, because it would be created empty")
            raise SystemExit(2)
        log(f"starting pid {os.getpid()}: http://{host}:{port}  db {db}")
        supervise(
            # Access logging is off: the extension polls every few seconds, and those
            # lines would bury the errors this log exists for.
            lambda: run_server(db, host, port, log_config=_file_log_config(), access_log=False),
            port_busy=lambda: port_in_use(host, port),
            log=log,
        )
    finally:
        sys.stdout, sys.stderr = saved
        stream.close()


# ------------------------------------------------------------ Task Scheduler ---


def task_xml(*, command: str, arguments: str, working_dir: str, user: str) -> str:
    """The task definition. Each setting below overrides a default that would break
    a server meant to stay up."""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>PokerNow Tracker server for the browser extension. Managed by `pnt service`.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <!-- The default ends any task after 72 hours. -->
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <!-- The defaults refuse to start on battery and stop the task when unplugged. -->
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <AllowHardTerminate>true</AllowHardTerminate>
    <!-- A second layer only: `supervise` restarts the server in-process first. -->
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(working_dir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _require_windows() -> None:
    if sys.platform != "win32":
        raise ServiceError(
            "`pnt service` uses Windows Task Scheduler. On macOS or Linux, run "
            "`pnt serve` under launchd or systemd instead."
        )


def _schtasks(*args: str) -> subprocess.CompletedProcess[str]:
    # check=False: callers read the return code and raise a ServiceError themselves.
    return subprocess.run(
        ["schtasks", *args], capture_output=True, text=True, errors="replace", check=False
    )


def _check(result: subprocess.CompletedProcess[str], doing: str) -> None:
    if result.returncode != 0:
        raise ServiceError(f"could not {doing}: {(result.stderr or result.stdout).strip()}")


def _pythonw() -> Path:
    """The windowless interpreter of the environment running this command.

    Taken from the running interpreter rather than PATH, so the task uses the same
    virtualenv -- and a Windows Store Python update, which moves the versioned
    install directory, cannot break it.
    """
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    return windowless if windowless.exists() else exe


def _current_user() -> str:
    domain, name = os.environ.get("USERDOMAIN"), os.environ.get("USERNAME")
    if not name:
        import getpass

        name = getpass.getuser()
    return f"{domain}\\{name}" if domain else name


def task_state(name: str = TASK_NAME) -> str | None:
    """`Ready`, `Running` or `Disabled`; None when no such task exists.

    Asked through PowerShell because its states are enum names, whereas
    `schtasks /Query` prints them in the system language.
    """
    _require_windows()
    quoted = name.replace("'", "''")
    result = subprocess.run(
        [
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            f"(Get-ScheduledTask -TaskName '{quoted}' -ErrorAction SilentlyContinue).State",
        ],
        capture_output=True, text=True, errors="replace", check=False,
    )  # fmt: skip
    return result.stdout.strip() or None


def install(
    db: Path, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *, name: str = TASK_NAME
) -> Path:
    """Register (or replace) the task and start it. Returns the interpreter it runs.

    The database path is resolved here, while the current directory still means
    something: the task starts elsewhere, and a relative path there would open --
    and silently create -- an empty database.
    """
    _require_windows()
    db = db.resolve()
    if not db.exists():
        raise ServiceError(
            f"no database at {db}. Pass --db with the full path to the pokernow.sqlite "
            "you use; a missing file would be created empty and the HUD would show nothing."
        )
    interpreter = _pythonw()
    arguments = subprocess.list2cmdline(
        ["-m", "pnt.cli", "service", "run", "--db", str(db), "--host", host, "--port", str(port)]
    )
    spec = LOG_DIR / "task.xml"
    spec.parent.mkdir(parents=True, exist_ok=True)
    # schtasks reads the file as the UTF-16 its declaration names.
    spec.write_text(
        task_xml(
            command=str(interpreter),
            arguments=arguments,
            working_dir=str(db.parent),
            user=_current_user(),
        ),
        encoding="utf-16",
    )
    _check(_schtasks("/Create", "/TN", name, "/XML", str(spec), "/F"), "register the task")
    _check(_schtasks("/Run", "/TN", name), "start the task")
    return interpreter


def _require_installed(name: str) -> None:
    if task_state(name) is None:
        raise ServiceError("not installed. Run `pnt service install --db <full path>` first.")


def start(name: str = TASK_NAME) -> None:
    _require_installed(name)
    _check(_schtasks("/Run", "/TN", name), "start the task")


def stop(name: str = TASK_NAME) -> None:
    _require_installed(name)
    if task_state(name) == "Running":
        _check(_schtasks("/End", "/TN", name), "stop the task")


def restart(name: str = TASK_NAME, *, timeout: float = 15.0) -> None:
    """Stop, wait for the old instance to exit, start.

    Waiting matters: the task ignores a start request while an instance is still
    running, so starting straight away would silently keep the old code.
    """
    stop(name)
    deadline = time.monotonic() + timeout
    while task_state(name) == "Running":
        if time.monotonic() >= deadline:
            raise ServiceError(f"the task did not stop within {timeout:.0f}s")
        time.sleep(0.5)
    start(name)


def uninstall(name: str = TASK_NAME) -> bool:
    """Stop and remove the task. False when there was nothing to remove."""
    if task_state(name) is None:
        return False
    stop(name)
    _check(_schtasks("/Delete", "/TN", name, "/F"), "remove the task")
    return True


# ------------------------------------------------------------------ status ---


def health(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = 2.0) -> dict | None:
    """The server's `/health` body, or None when nothing answers."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=timeout) as r:
            return json.load(r)
    except (OSError, ValueError):
        return None


def wait_for_health(
    host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = 15.0
) -> dict | None:
    deadline = time.monotonic() + timeout
    while True:
        body = health(host, port)
        if body is not None or time.monotonic() >= deadline:
            return body
        time.sleep(0.5)


def tail(path: Path, lines: int) -> list[str]:
    with open(path, encoding="utf-8", errors="replace") as f:
        return [line.rstrip("\n") for line in deque(f, maxlen=lines)]
