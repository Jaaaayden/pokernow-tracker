"""Set up, serve, check and tear down one Orca worktree of this repo.

    python scripts/orca_worktree.py setup     # own .venv, a private copy of the database, a port
    python scripts/orca_worktree.py serve     # `pnt serve` on that copy and port
    python scripts/orca_worktree.py check     # pytest + node tests + ruff, the gate before a PR
    python scripts/orca_worktree.py archive   # stop that server so the folder can be deleted

orca.yaml runs `setup` when Orca makes a worktree and `archive` before it removes
one. Plain Python with one entry point because on Windows Orca hands those scripts
to cmd.exe, and agents type into whatever shell their pane has: a bash or
PowerShell script would work in one of them only.

What a worktree must not share with the root checkout:

* **The venv.** The root `.venv` has an editable install pointing at the root
  folder, so its `pnt` and `pytest` would run the root's code, not this worktree's.
* **The database and port.** `pnt service` serves the real `pokernow.sqlite` on
  52000 from the root checkout. A worktree's server gets a copy and a port of its
  own, derived from the workspace name so it stays the same across restarts.
* **The machine config.** `~/.pnt/config.json` names the real database, and the
  front page writes to it. The worktree's server reads `.pnt-dev/config.json`
  instead ($PNT_CONFIG), as the tests do.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
DEV = ROOT / ".pnt-dev"
DB = DEV / "pokernow.sqlite"
CONFIG = DEV / "config.json"
PORT_FILE = DEV / "port"
PID_FILE = DEV / "server.pid"

#: The live service's port; a worktree never takes it.
LIVE_PORT = 52000
#: Worktree servers sit just above it, one per workspace name.
PORT_BASE, PORT_SPAN = 52100, 800


def say(msg: str) -> None:
    # Orca shows this in the setup pane; unflushed, it arrives all at once at the end.
    print(f"[orca_worktree] {msg}", flush=True)


def run(cmd: list[str], **kwargs) -> int:
    say("$ " + " ".join(str(c) for c in cmd))
    return subprocess.call([str(c) for c in cmd], cwd=ROOT, **kwargs)


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)


def workspace_port() -> int:
    if PORT_FILE.exists():
        return int(PORT_FILE.read_text().strip())
    name = os.environ.get("ORCA_WORKSPACE_NAME") or ROOT.name
    return PORT_BASE + zlib.crc32(name.encode()) % PORT_SPAN


def live_database() -> Path | None:
    """The database the live service serves, to copy from."""
    candidates = []
    try:
        config = json.loads((Path.home() / ".pnt" / "config.json").read_text(encoding="utf-8"))
        if isinstance(config.get("db"), str):
            candidates.append(Path(config["db"]))
    except (OSError, ValueError):
        pass
    if os.environ.get("ORCA_ROOT_PATH"):
        candidates.append(Path(os.environ["ORCA_ROOT_PATH"]) / "pokernow.sqlite")
    # The main checkout, for a worktree made by hand rather than by Orca.
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    if common:
        candidates.append(Path(common).parent / "pokernow.sqlite")
    return next((p for p in candidates if p.is_file() and p.resolve() != DB.resolve()), None)


def setup(_args: list[str]) -> int:
    if not PYTHON.exists():
        # The base interpreter, not this one: run from inside another venv,
        # sys.executable would make a venv of a venv.
        base = getattr(sys, "_base_executable", sys.executable)
        if run([base, "-m", "venv", VENV]):
            return 1
    if run([PYTHON, "-m", "pip", "install", "-q", "--disable-pip-version-check", "-e", ".[dev]"]):
        return 1

    DEV.mkdir(exist_ok=True)
    (DEV / "logs").mkdir(exist_ok=True)
    if not DB.exists():
        source = live_database()
        if source is None:
            say("no live database found; the server starts empty (`pnt import --sample` fills it)")
        else:
            # The backup API, not a file copy: the live service holds the file open
            # in WAL mode, and a plain copy can catch it mid-checkpoint.
            say(f"copying {source} -> {DB}")
            # Read-only on the live file: nothing done here may write to it.
            src = sqlite3.connect(f"{Path(source).resolve().as_uri()}?mode=ro", uri=True)
            dst = sqlite3.connect(DB)
            try:
                src.backup(dst)
            finally:
                src.close()
                dst.close()
    port = workspace_port()
    PORT_FILE.write_text(f"{port}\n")
    CONFIG.write_text(json.dumps({"db": str(DB), "log_dir": str(DEV / "logs")}, indent=2) + "\n")
    say(f"ready. venv {VENV}, server port {port} (`python scripts/orca_worktree.py serve`)")
    return 0


def serve(args: list[str]) -> int:
    if not PYTHON.exists():
        say("no .venv here yet; run `python scripts/orca_worktree.py setup` first")
        return 1
    port = workspace_port()
    if port == LIVE_PORT:  # only reachable through a hand-edited port file
        say(f"refusing port {LIVE_PORT}: that is the live tracker")
        return 1
    env = {**os.environ, "PNT_CONFIG": str(CONFIG), "PNT_UPDATE_CHECK": "0"}
    cmd = [PYTHON, "-m", "pnt.cli", "serve", "--db", DB, "--port", str(port), *args]
    # ASCII only: a piped Windows console is cp1252, and the gear icon crashes it.
    say(f"to try the HUD here, set the extension's server address to http://127.0.0.1:{port}")
    say("$ " + " ".join(str(c) for c in cmd))
    proc = subprocess.Popen([str(c) for c in cmd], cwd=ROOT, env=env)
    PID_FILE.write_text(f"{proc.pid}\n")
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return proc.wait()
    finally:
        PID_FILE.unlink(missing_ok=True)


def check(args: list[str]) -> int:
    """Everything CI runs. Extra arguments go to pytest (`check -x tests/test_api.py`)."""
    tests = sorted(str(p.relative_to(ROOT)) for p in ROOT.glob("pnt/extension/*.test.mjs"))
    steps = {
        "pytest": [PYTHON, "-m", "pytest", "-q", *args],
        "node": ["node", "--test", *tests],
        "ruff": [PYTHON, "-m", "ruff", "check", "pnt", "tests"],
    }
    failed = [name for name, cmd in steps.items() if run(cmd)]
    say(f"FAILED: {', '.join(failed)}" if failed else "all checks passed")
    return 1 if failed else 0


def archive(_args: list[str]) -> int:
    # A server still running from this folder holds files open, and Windows then
    # refuses to delete the worktree.
    if PID_FILE.exists():
        pid = PID_FILE.read_text().strip()
        say(f"stopping this worktree's server (pid {pid})")
        if os.name == "nt":
            subprocess.call(["taskkill", "/PID", pid, "/T", "/F"])
        else:
            try:
                os.kill(int(pid), 15)
            except (OSError, ValueError):
                pass
        PID_FILE.unlink(missing_ok=True)
    ahead = git("log", "--oneline", "@{upstream}..HEAD")
    if ahead.returncode != 0:  # no upstream: the branch was never pushed
        ahead = git("log", "--oneline", "origin/main..HEAD")
    if ahead.stdout.strip():
        say("warning: commits on this branch not pushed anywhere:\n" + ahead.stdout.rstrip())
    return 0


COMMANDS = {"setup": setup, "serve": serve, "check": check, "archive": archive}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        return 2
    return COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
