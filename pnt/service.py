"""Keep the tracker server running in the background: start at login, restart on crash.

`pnt serve` in a terminal lasts until the terminal closes. This module makes the
same server permanent with a per-user Task Scheduler task on Windows, and a
per-user launch agent on macOS. Both answer to the same `pnt service` commands,
so `pnt setup` and `pnt update` treat the two alike.

* **Task Scheduler, not a Windows service.** Installing a service needs admin
  rights, and a service runs as another account in session 0, away from this
  user's files. A task registered for the current user at logon needs neither.
* **Not a Startup-folder shortcut.** A shortcut starts the server once and has no
  state to ask about; a task can be started, stopped, restarted and queried.
* **A launch agent, not a daemon,** on macOS for the same reason: it runs as this
  user, in their login session, with no sudo.
* **Restarts happen in-process** (`supervise`). Task Scheduler's restart-on-failure
  and launchd's KeepAlive are kept as a second layer only: they wait a minute
  between attempts and log nothing about why.

Idle, the server costs tens of megabytes and no measurable CPU; it only does work
while the extension is posting.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import logging
import os
import plistlib
import socket
import subprocess
import sys
import threading
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
#: macOS: the launch agent's label, and the plist launchd reads at every login.
AGENT_LABEL = "com.pokernow-tracker.server"
AGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"
DEFAULT_HOST = "127.0.0.1"
#: Deliberately not 8000. That is the busiest port on a developer's machine --
#: Django, `python -m http.server` and most tutorials all default to it -- and
#: the clash is quiet in both directions: this server holding it makes the other
#: thing fail to bind, and the other thing holding it makes `supervise` wait
#: politely forever while the HUD sits there saying the tracker is unreachable.
#: 52000 is in the IANA dynamic range, which is never assigned to a registered
#: service, and 52 is the number of cards in a deck. Override with --port.
DEFAULT_PORT = 52000

#: A hidden process has no console, so everything it would have printed goes here.
#: Deliberately not under %LOCALAPPDATA%: Microsoft Store Python silently redirects a
#: new folder there into its private package cache, where schtasks cannot read the
#: task definition and Explorer never shows the log.
LOG_DIR = Path.home() / ".pnt"
LOG_FILE = LOG_DIR / "server.log"
#: macOS: launchd's own capture of the agent's output, for a crash before the
#: server has redirected it to LOG_FILE. Separate, since LOG_FILE is rotated by
#: renaming, which would leave launchd writing to the old file.
AGENT_LOG_FILE = LOG_DIR / "launchd.log"
#: Past this the log rotates (`_RotatingLog`), at a start or while running. With
#: access logging off, a day of play writes far less.
LOG_MAX_BYTES = 5 * 1024 * 1024

#: A run at least this long counts as healthy, so the next crash restarts quickly.
HEALTHY_RUN_SECONDS = 60.0
MAX_BACKOFF_SECONDS = 60.0
#: How often to re-check a port that another process is holding.
PORT_POLL_SECONDS = 15.0


class ServiceError(RuntimeError):
    """Something the user can act on: shown as a one-line CLI error."""


# ------------------------------------------------------------------ running ---


def bind(host: str, port: int) -> socket.socket:
    """The server's listening socket, which no second server can share.

    uvicorn binds with SO_REUSEADDR, and on Windows that flag lets another process
    listen on a port already in use -- silently, both binds succeed. Two servers
    then share the port and Windows hands each request to either: after an update,
    a page from the new server fetching data from an old one left running, which
    reads as "the running server is older than this page" no matter how often the
    service is restarted. SO_EXCLUSIVEADDRUSE refuses the second bind whichever
    side asks for sharing. Elsewhere SO_REUSEADDR only lets a restart take a port
    still in TIME_WAIT, which is wanted.
    """
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    return sock


def loopback(host: str) -> bool:
    """Whether a server bound to `host` is reachable from this machine alone."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def exposed_warning(host: str, port: int) -> str:
    """What `pnt serve` and `pnt service install` say about a non-loopback --host.

    The write header (app.py) stops a web page, not a program: anything that can
    reach the port can send it, and a Host of 127.0.0.1 with it. With no login, a
    LAN bind hands every hand stored, and the switch of database and log folder, to
    whoever is on the network."""
    return (
        f"WARNING: --host {host} lets other machines reach the tracker on port {port}."
        " It has no login: anyone who can reach it can read every hand stored and"
        " change the database. Use 127.0.0.1 unless only your own devices are on"
        " that network."
    )


def run_server(db: Path, host: str, port: int, **uvicorn_options) -> None:
    """Run the API in this process until it stops. Shared by `pnt serve` and the task.

    Raises OSError when the port is taken (see `bind`).
    """
    # The app reads PNT_DB once, at import, so this must be set before uvicorn loads it.
    os.environ["PNT_DB"] = str(db)
    # And the Host it may be reached by beyond 127.0.0.1 and localhost (see app.py).
    os.environ["PNT_HOST"] = host
    import uvicorn

    sock = bind(host, port)
    uvicorn_options.setdefault("log_config", _log_config())
    # What uvicorn.run does, but serving on our socket instead of binding its own.
    config = uvicorn.Config("pnt.server.app:app", host=host, port=port, **uvicorn_options)
    server = uvicorn.Server(config)
    # uvicorn stays quiet about the address when handed a socket, so the log would
    # lose the line that says where the server is. Config has set logging up by now.
    logging.getLogger("uvicorn.error").info(
        "Uvicorn running on http://%s:%d (Press CTRL+C to quit)", host, port
    )
    if not loopback(host):  # at every start, so the background task's log says it too
        logging.getLogger("uvicorn.error").warning(exposed_warning(host, port))
    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    if not server.started:
        raise SystemExit(3)  # uvicorn's own STARTUP_FAILURE


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


class _RotatingLog:
    """The server log as a text stream that rotates itself by size.

    Checking only at each start let a server up for weeks grow its log without
    bound: a fault repeated every few seconds (a sync failing on a locked
    database) writes a traceback each time. Past LOG_MAX_BYTES the file moves to
    `<name>.1`, replacing the one before, and a new one starts. A rename Windows
    refuses (the file open elsewhere) is tried again after another
    ROTATE_RETRY_BYTES, rather than on every line.

    Stands in for sys.stdout and sys.stderr, so it is what uvicorn's handlers and
    `print` write to; everything else is the open file's.
    """

    ROTATE_RETRY_BYTES = 64 * 1024

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate()
        self._open()

    def _open(self) -> None:
        self._file = open(self._path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 -- kept open until close()
        self._size = self._path.stat().st_size
        # Still past the cap, the rename was refused: try again a little further on.
        over = self._size > LOG_MAX_BYTES
        self._limit = self._size + self.ROTATE_RETRY_BYTES if over else LOG_MAX_BYTES

    def _rotate(self) -> None:
        try:
            if self._path.exists() and self._path.stat().st_size > LOG_MAX_BYTES:
                self._path.replace(self._path.with_name(self._path.name + ".1"))
        except OSError:
            pass  # held open elsewhere: `_open` sets the next try further on

    def write(self, text: str) -> int:
        with self._lock:
            n = self._file.write(text)
            self._size += len(text)
            if self._size > self._limit:
                self._file.close()
                self._rotate()
                self._open()
            return n

    def flush(self) -> None:
        with self._lock:
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            self._file.close()

    def __getattr__(self, name: str):
        if name == "_file":  # not open yet: no recursion through this method
            raise AttributeError(name)
        return getattr(self._file, name)

    def __enter__(self) -> _RotatingLog:  # noqa: PYI034 -- never subclassed
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _open_log(path: Path | None = None) -> TextIO:
    return _RotatingLog(path or LOG_FILE)  # type: ignore[return-value]  # the caller closes it


def _log_config() -> dict:
    """uvicorn's own logging, with the tracker's beside it.

    Without the `pnt` logger, every `pnt.*` line -- a log imported, the database
    switched, the sample shown -- went nowhere, and a warning came out bare, through
    Python's last-resort handler, with no time on it. It shares uvicorn's handler, so
    its lines land where uvicorn's do and look the same."""
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    config["loggers"]["pnt"] = {"handlers": ["default"], "level": "INFO", "propagate": False}
    return config


def _file_log_config() -> dict:
    """`_log_config`, timestamped and without colour codes: it goes to a file."""
    config = _log_config()
    for formatter in config["formatters"].values():
        formatter["fmt"] = "%(asctime)s " + formatter["fmt"]
        formatter["use_colors"] = False
    return config


def run_service(db: Path, host: str, port: int) -> None:
    """What the task or launch agent executes: the supervised server, logging to a file."""
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
    <!-- The default, 7, is below normal, with low disk and memory priority: every
         HUD answer queued behind the browser, and the front page's file picker
         took seconds to appear. 5 is an ordinary program's. -->
    <Priority>5</Priority>
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


def supported() -> bool:
    """Whether `pnt service` can manage a background server on this platform."""
    return sys.platform in ("win32", "darwin")


def _require_supported() -> None:
    if not supported():
        raise ServiceError(
            "`pnt service` uses Windows Task Scheduler or a macOS launch agent. On Linux,"
            " run `pnt serve` under systemd instead."
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
    `schtasks /Query` prints them in the system language. On macOS, the launch
    agent's state in the same words (`name` is the Windows task's; the agent has
    the one label).
    """
    _require_supported()
    if sys.platform == "darwin":
        return _agent_state()
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
    db: Path,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    name: str = TASK_NAME,
    timeout: float = 15.0,
) -> Path:
    """Register (or replace) the task and start it. Returns the interpreter it runs.

    The database path is resolved here, while the current directory still means
    something: the task starts elsewhere, and a relative path there would open --
    and silently create -- an empty database.
    """
    _require_supported()
    db = db.resolve()
    if not db.exists():
        raise ServiceError(
            f"no database at {db}. Pass --db with the full path to the pokernow.sqlite "
            "you use; a missing file would be created empty and the HUD would show nothing."
        )
    if sys.platform == "darwin":
        return _agent_install(db, host, port, timeout)
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
    # End any instance already running before swapping the definition in. The task
    # is registered MultipleInstancesPolicy=IgnoreNew, so `/Run` is *silently*
    # ignored while one is alive -- which meant re-installing to change --db or
    # --port replaced the definition on disk and left the old server running on the
    # old one, with every command reporting success. Nothing said otherwise until
    # you noticed the port had not moved.
    if task_state(name) is not None:
        stop(name)
        _await_stopped(name, timeout)

    _check(_schtasks("/Create", "/TN", name, "/XML", str(spec), "/F"), "register the task")
    _check(_schtasks("/Run", "/TN", name), "start the task")
    return interpreter


def _require_installed(name: str) -> None:
    if task_state(name) is None:
        raise ServiceError("not installed. Run `pnt service install --db <full path>` first.")


def start(name: str = TASK_NAME) -> None:
    _require_installed(name)
    if sys.platform == "darwin":
        _agent_start()
        return
    _check(_schtasks("/Run", "/TN", name), "start the task")


def stop(name: str = TASK_NAME) -> None:
    _require_installed(name)
    if sys.platform == "darwin":
        _agent_stop()
    elif task_state(name) == "Running":
        _check(_schtasks("/End", "/TN", name), "stop the task")


def stop_and_wait(name: str = TASK_NAME, timeout: float = 15.0) -> None:
    """Stop, and return only once the old instance has exited and let go of its files."""
    stop(name)
    _await_stopped(name, timeout)


def _await_stopped(name: str, timeout: float) -> None:
    """Block until the task has no running instance."""
    deadline = time.monotonic() + timeout
    while task_state(name) == "Running":
        if time.monotonic() >= deadline:
            raise ServiceError(f"the task did not stop within {timeout:.0f}s")
        time.sleep(0.5)


def restart(
    name: str = TASK_NAME,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    timeout: float = 15.0,
    answer_timeout: float = 30.0,
    while_stopped: Callable[[], None] | None = None,
) -> dict:
    """Stop, wait for the old instance to exit, start, and check the new one answers.

    `while_stopped` runs between the two, with nothing serving the database: what
    `pnt service restart` rebuilds stale hands in, so the new server starts on them.

    Waiting matters: the task ignores a start request while an instance is still
    running, so starting straight away would silently keep the old code.

    So does the check. A server this task does not own -- `pnt serve` in a
    terminal, or one left over from before an update -- outlives the restart and
    keeps the port, the new instance waits for it, and the old code goes on
    answering. The new instance is known by its pid in `/health`: a server too old
    to report one is by definition not it. Returns the new server's `/health`.
    """
    before = (health(host, port) or {}).get("pid")
    stop(name)
    _await_stopped(name, timeout)
    if while_stopped is not None:
        try:
            while_stopped()
        except Exception:
            start(name)  # whatever it hit, the tracker comes back up
            raise
    start(name)
    deadline = time.monotonic() + answer_timeout
    while True:
        body = health(host, port)
        if body is not None and body.get("pid") is not None and body["pid"] != before:
            return body
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    if body is None:
        raise ServiceError(
            f"the service restarted but nothing answers on http://{host}:{port}: see `pnt service log`"
        )
    raise ServiceError(
        f"another server still answers on http://{host}:{port}, so the service cannot "
        f"take the port: {port_holder_hint(port)}. The service starts within "
        f"{PORT_POLL_SECONDS:.0f}s of the port coming free."
    )


def uninstall(name: str = TASK_NAME) -> bool:
    """Stop and remove the task. False when there was nothing to remove."""
    if task_state(name) is None:
        return False
    stop(name)
    if sys.platform == "darwin":
        AGENT_PLIST.unlink()
    else:
        _check(_schtasks("/Delete", "/TN", name, "/F"), "remove the task")
    return True


# ------------------------------------------------------------------ launchd ---


def agent_plist(*, program: list[str], working_dir: str) -> bytes:
    """The launch agent's definition. Each setting below overrides a default that
    would break a server meant to stay up, as `task_xml` does on Windows."""
    return plistlib.dumps(
        {
            "Label": AGENT_LABEL,
            "ProgramArguments": program,
            "WorkingDirectory": working_dir,
            # Started at load, which launchd does at every login.
            "RunAtLoad": True,
            # A second layer only, as on Windows: `supervise` restarts in-process
            # first. A clean exit (a stop) is left alone.
            "KeepAlive": {"SuccessfulExit": False},
            # The default, 10s, would retry a missing database six times a minute.
            "ThrottleInterval": 60,
            # The default runs a job under resource limits an app does not have;
            # Interactive runs it with an app's, which are none. The task's normal
            # priority on Windows is for the same reason: HUD answers queued behind
            # the browser.
            "ProcessType": "Interactive",
            "StandardOutPath": str(AGENT_LOG_FILE),
            "StandardErrorPath": str(AGENT_LOG_FILE),
        }
    )


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    # check=False: callers read the return code and raise a ServiceError themselves.
    return subprocess.run(
        ["launchctl", *args], capture_output=True, text=True, errors="replace", check=False
    )


def _agent_target() -> str:
    """This user's login session, where a launch agent runs (`gui/<uid>`)."""
    return f"gui/{os.getuid()}"


def _agent_loaded() -> bool:
    return _launchctl("list", AGENT_LABEL).returncode == 0


def _agent_state() -> str | None:
    """None without a plist; `Running` while the agent has a process, else `Ready`.

    Installed but not loaded is `Ready` too: `pnt service stop` unloads it, and it
    loads again at the next login, just as a stopped task starts again.
    """
    if not AGENT_PLIST.exists():
        return None
    result = _launchctl("list", AGENT_LABEL)
    return "Running" if result.returncode == 0 and '"PID" =' in result.stdout else "Ready"


def _agent_start() -> None:
    # A loaded agent with no process (one that exited) is kicked; bootstrapping it
    # again would fail as already loaded.
    if _agent_loaded():
        _check(_launchctl("kickstart", f"{_agent_target()}/{AGENT_LABEL}"), "start the agent")
    else:
        _check(_launchctl("bootstrap", _agent_target(), str(AGENT_PLIST)), "start the agent")


def _agent_stop() -> None:
    """Unload the agent, which ends its process. KeepAlive cannot bring it back
    while it is unloaded; the plist stays, so the next login starts it again."""
    if _agent_loaded():
        _check(_launchctl("bootout", f"{_agent_target()}/{AGENT_LABEL}"), "stop the agent")


def _agent_install(db: Path, host: str, port: int, timeout: float) -> Path:
    """Write (or replace) the plist and load it. Returns the interpreter it runs.

    `sys.executable` as invoked, not resolved: in a pipx or virtualenv install it
    is a symlink to the base interpreter, and only the symlink's path finds the
    environment's packages.
    """
    interpreter = Path(sys.executable)
    program = [
        str(interpreter), "-m", "pnt.cli", "service", "run",
        "--db", str(db), "--host", host, "--port", str(port),
    ]  # fmt: skip
    # Unloaded before the plist is swapped: launchd keeps the definition it loaded,
    # so a reinstall to change --db or --port would otherwise leave the old one
    # running, the same trap as the task's IgnoreNew.
    if _agent_loaded():
        _agent_stop()
        _await_stopped(TASK_NAME, timeout)
    AGENT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    AGENT_PLIST.write_bytes(agent_plist(program=program, working_dir=str(db.parent)))
    _check(_launchctl("bootstrap", _agent_target(), str(AGENT_PLIST)), "load the agent")
    return interpreter


# ------------------------------------------------------------------ status ---


def listening_pids(port: int) -> list[int]:
    """The processes listening on `port`, on Windows and macOS; elsewhere, none are named.

    Read from `netstat -ano`, by the listening socket's 0.0.0.0:0 / [::]:0 remote
    address rather than its state, which netstat prints in the system language.
    On macOS, from `lsof`, which ships with it.
    """
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                capture_output=True, text=True, errors="replace", check=False,
            ).stdout  # fmt: skip
        except OSError:
            return []
        return list(dict.fromkeys(int(line) for line in out.split() if line.isdigit()))
    if sys.platform != "win32":
        return []
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, errors="replace", check=False,
        ).stdout  # fmt: skip
    except OSError:
        return []
    pids: list[int] = []
    for line in out.splitlines():
        parts = line.split()
        if (
            len(parts) >= 5
            and parts[1].endswith(f":{port}")
            and parts[2] in ("0.0.0.0:0", "[::]:0")
            and parts[-1].isdigit()
            and int(parts[-1]) not in pids
        ):
            pids.append(int(parts[-1]))
    return pids


def port_holder_hint(port: int) -> str:
    """How to free the port: which process holds it, where one can be found."""
    pids = listening_pids(port)
    if not pids:
        return "close any terminal running `pnt serve`, or end the process that holds it"
    ids = ", ".join(str(p) for p in pids)
    end = (
        f"Stop-Process -Id {ids} -Force" if sys.platform == "win32"
        else "kill " + " ".join(str(p) for p in pids)
    )  # fmt: skip
    return (
        f"it is held by pid {ids} -- a `pnt serve` in a terminal, or a server left over "
        f"from before an update. End it with: {end}"
    )


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
