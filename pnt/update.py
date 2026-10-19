"""`pnt update`: replace the installed code, restart the server, let the extension follow.

Updating by hand took three steps in an order that mattered: stop the service,
reinstall, start it again, then reload the extension at chrome://extensions. Done
the other way round, a reinstall meets the running server's interpreter -- which
Windows will not delete -- and a restart without the reinstall keeps the old code.

* **pip inside the install's own environment, not pipx.** A `pipx reinstall`
  deletes the venv, interpreter and all, and the service runs that interpreter.
  pip replaces only the package, leaving the interpreter the task names in place.
* **`--force-reinstall`.** The version is not bumped per change and the default
  source is a branch archive, so pip has nothing to compare: without the flag it
  calls the install up to date and changes nothing.
* **The server restarts in a fresh process**, so the restart is the new code's.
* **The extension reloads itself.** The server reports a hash of the extension's
  files in `/health` (`extension_build`); a loaded extension that sees the hash
  change under it reloads (background.js), and Chrome reads the new files from
  the same folder, which a reinstall does not move.
* **The server says when there is one.** An archive install knows its commit
  (`installed_commit`); the server asks GitHub every few hours how far behind its
  branch that is and reports it in `/health` (`update_behind`), which the front
  page and the side panel show as a banner.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import re
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path

DIST = "pokernow-tracker"
REPO = "Jaaaayden/pokernow-tracker"
#: Where friends install from: an archive needs no git on their machine.
DEFAULT_SOURCE = f"https://github.com/{REPO}/archive/refs/heads/main.zip"
PACKAGE_DIR = Path(__file__).parent
EXTENSION_DIR = PACKAGE_DIR / "extension"
#: Holds `$Format:%H$`, which GitHub's archives replace with the commit
#: (.gitattributes: export-subst). A checkout keeps the placeholder.
COMMIT_FILE = PACKAGE_DIR / "_commit.txt"


class UpdateError(RuntimeError):
    """Something the user can act on: shown as a one-line CLI error."""


def extension_build(folder: Path = EXTENSION_DIR) -> str:
    """A short hash of the files Chrome loads from the extension folder.

    The manifest version is no use here: nobody bumps it. The tests beside the
    scripts are left out, since Chrome never loads them and a change to one
    should not reload anybody's extension.

    background.js hashes its own copy the same way, so the order is the plain
    string order of the paths, which JavaScript's sort() also gives.
    """
    h = hashlib.sha256()
    for rel in sorted(extension_files(folder)):
        h.update(rel.encode() + b"\0" + (folder / rel).read_bytes() + b"\0")
    return h.hexdigest()[:12]


def extension_files(folder: Path = EXTENSION_DIR) -> list[str]:
    """The files the build covers, as paths relative to the folder."""
    rels = (p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())
    return [rel for rel in rels if not rel.endswith(".test.mjs")]


def checkout_root(package_dir: Path = PACKAGE_DIR) -> Path | None:
    """The source checkout this code runs from, or None for an installed copy.

    A checkout has pyproject.toml beside the package; site-packages never does.
    """
    root = package_dir.parent
    return root if (root / "pyproject.toml").exists() else None


def installed_source(dist: str = DIST) -> str | None:
    """The URL or path this install came from, as pip recorded it (PEP 610)."""
    try:
        raw = importlib.metadata.distribution(dist).read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return None
    if not raw:
        return None
    info = json.loads(raw)
    url = info.get("url")
    if not url:
        return None
    if "vcs_info" in info:
        vcs = info["vcs_info"]
        # Followed back to the branch, never the commit: the commit is what is
        # installed now, and reinstalling it would be no update at all.
        ref = vcs.get("requested_revision")
        return f"{vcs['vcs']}+{url}" + (f"@{ref}" if ref else "")
    return url


def installed_commit(path: Path = COMMIT_FILE) -> str | None:
    """The commit this copy was installed from, or None when it cannot know.

    Only an install from a GitHub archive knows: a checkout, or pip building from
    one (`git+`), keeps the placeholder.
    """
    try:
        sha = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else None


def source_branch(source: str | None) -> str:
    """The branch an archive URL names, so a copy installed from v1 follows v1."""
    m = re.search(r"/archive/refs/heads/(.+)\.zip$", source or "")
    return m.group(1) if m else "main"


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "pnt", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def commits_behind(sha: str, branch: str, *, fetch: Callable[[str], dict] = _get_json) -> int | None:
    """How many commits on `branch` this install lacks: 0 when it has them all,
    None when GitHub cannot say (offline, rate-limited, a commit it never saw).

    Unauthenticated, the API allows 60 calls an hour; the server makes one every
    few hours.
    """
    try:
        body = fetch(f"https://api.github.com/repos/{REPO}/compare/{sha}...{branch}")
        # base...head: "ahead" means the branch has commits this install lacks.
        if body["status"] in ("ahead", "diverged"):
            return int(body["ahead_by"])
        return 0
    except Exception:
        return None


def requirements(dist: str = DIST) -> list[str]:
    """The installed package's runtime dependencies, read fresh from disk."""
    importlib.invalidate_caches()
    reqs = importlib.metadata.requires(dist) or []
    return [r for r in reqs if "extra ==" not in r]


def pip(*args: str) -> None:
    """Run pip in this environment, its output going straight to the terminal."""
    result = subprocess.run([sys.executable, "-m", "pip", *args], check=False)
    if result.returncode != 0:
        raise UpdateError(f"pip failed (exit code {result.returncode}); see its output above")


def install(source: str, *, pip: Callable[..., None] = pip) -> None:
    """Replace the package from `source`, then add any dependency it newly needs.

    On Windows pip warns that it could not remove a temporary directory: the old
    `pnt.exe`, which is running this, can be moved aside but not deleted. Harmless.

    Two steps so the dependencies are not reinstalled too: `--force-reinstall`
    would apply to all of them, a dozen downloads for what is usually a change to
    this package alone. The second step only installs what is missing.
    """
    pip("install", "--quiet", "--force-reinstall", "--no-deps", "--no-cache-dir", source)
    reqs = requirements()
    if reqs:
        pip("install", "--quiet", *reqs)
