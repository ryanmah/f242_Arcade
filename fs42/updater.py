"""Update FieldStation42 from GitHub.

Two ways in, picked by how this copy was installed:

* ``release`` - the packaged app (Windows setup / Linux .run).  Asks the
  GitHub API for the repository's latest release, downloads the installer
  built for this platform, checks it against the release's SHA256SUMS and
  runs it unattended.  The installer stops the running app, replaces it
  and starts it again; channels, catalogs and settings live in the data
  folder and are not touched.
* ``git`` - a source checkout: ``git pull --ff-only`` and restart.

The repository is ``update_repo`` in main_config.json (default below).
Releases are what the Build workflow publishes when a ``v*`` tag is pushed;
the repository (or at least its releases) must be public, since the app
asks anonymously.

The menu and the web console both drive this through ``UpdateJob``, which
runs on a thread and reports progress through ``status()``.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from fs42 import paths

_l = logging.getLogger("UPDATE")

DEFAULT_REPO = "ryanmah/f242_Arcade"
API_LATEST = "https://api.github.com/repos/{repo}/releases/latest"
TIMEOUT = 15
CHUNK = 256 * 1024


# ------------------------------------------------------------------ basics

def current_version() -> str:
    from fs42 import __version__

    return __version__


def repo() -> str:
    try:
        with open(paths.confs("main_config.json")) as f:
            value = (json.load(f) or {}).get("update_repo")
        if isinstance(value, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", value.strip()):
            return value.strip()
    except Exception:
        pass
    return DEFAULT_REPO


def parse_version(text) -> tuple:
    """'v1.2.10' -> (1, 2, 10).  Anything after the numbers is ignored."""
    numbers = []
    for part in re.split(r"[.\-+]", str(text or "").strip().lstrip("vV")):
        if not part.isdigit():
            break
        numbers.append(int(part))
    return tuple(numbers)


def is_newer(latest, current) -> bool:
    a, b = parse_version(latest), parse_version(current)
    return bool(a) and a > b


def platform_asset_suffix():
    if os.name == "nt":
        return "-windows-x64-setup.exe"
    if sys.platform.startswith("linux"):
        return "-linux-x64-installer.run"
    return None


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def mode() -> str:
    if paths.IS_FROZEN:
        return "release" if platform_asset_suffix() else "none"
    if (_repo_root() / ".git").exists() and shutil.which("git"):
        return "git"
    return "none"


def install_dir() -> Path:
    return Path(sys.executable).resolve().parent


# ------------------------------------------------------------------- check

def _get(url, accept="application/vnd.github+json"):
    request = urllib.request.Request(url, headers={
        "Accept": accept,
        "User-Agent": f"FieldStation42/{current_version()}",
    })
    return urllib.request.urlopen(request, timeout=TIMEOUT)


def check() -> dict:
    """What is installed, what is available, and whether to offer it."""
    how = mode()
    base = {"ok": True, "mode": how, "current": current_version(), "repo": repo(), "available": False}
    try:
        if how == "release":
            return {**base, **_check_release()}
        if how == "git":
            return {**base, **_check_git()}
        return {**base, "ok": False, "error": "Updating is not supported for this copy of FieldStation42."}
    except Exception as e:
        _l.warning("Update check failed: %s", e)
        return {**base, "ok": False, "error": str(e)}


def _check_release() -> dict:
    name = repo()
    page = f"https://github.com/{name}/releases"
    try:
        with _get(API_LATEST.format(repo=name)) as response:
            release = json.load(response)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"ok": False, "page": page,
                    "error": f"No release is published at github.com/{name}/releases yet "
                             "(push a version tag such as v1.0.21 to build one; "
                             "the repository must be public)."}
        if e.code in (403, 429):
            limited = (e.headers or {}).get("X-RateLimit-Remaining") == "0" or e.code == 429
            return {"ok": False, "page": page,
                    "error": "GitHub is limiting requests from this network; try again in an hour." if limited
                    else f"GitHub refused the request ({e.code})."}
        raise
    except urllib.error.URLError as e:
        return {"ok": False, "page": page, "error": f"Could not reach GitHub ({e.reason})."}

    tag = release.get("tag_name") or ""
    latest = tag.lstrip("vV")
    plan = pick_assets(release.get("assets") or [])
    newer = is_newer(latest, current_version())
    result = {
        "latest": latest,
        "available": bool(newer and plan),
        "asset": plan["files"][0] if plan else None,
        "plan": plan,
        "notes": (release.get("body") or "")[:4000],
        "page": release.get("html_url") or page,
        "published": release.get("published_at"),
    }
    if newer and not plan:
        result["error"] = f"Release {latest} has no installer for this platform yet (it may still be building)."
    return result


# Split installers: what the Windows/SteamOS installer folders hold, so a
# release can be made by dragging those folders' files onto GitHub.
_WIN_SPLIT_EXE = "FieldStation42-setup.exe"
_WIN_SPLIT_BIN = re.compile(r"FieldStation42-setup-(\d+)\.bin$")
_LINUX_PART = re.compile(r"FieldStation42-steamos\.part(\d+)$")
_LINUX_SHA = "FieldStation42-steamos.sha256"


def pick_assets(assets) -> dict:
    """Which release files make this platform's installer.

    Returns {"files": [...], "launch": name, "join": name|None,
    "sha256": url|None, "sums_url": url|None, "size": bytes} or None.
    """
    by_name = {}
    for item in assets:
        name = item.get("name") or ""
        if name:
            by_name[name] = {"name": name, "url": item.get("browser_download_url"), "size": int(item.get("size") or 0)}
    sums = by_name.get("SHA256SUMS", {}).get("url")
    suffix = platform_asset_suffix()
    plan = None
    single = [f for n, f in by_name.items() if suffix and n.endswith(suffix)]
    if single:
        plan = {"files": single[:1], "launch": single[0]["name"], "join": None, "sha256": None}
    elif os.name == "nt":
        bins = sorted(((int(m.group(1)), f) for n, f in by_name.items() if (m := _WIN_SPLIT_BIN.match(n))), key=lambda x: x[0])
        if _WIN_SPLIT_EXE in by_name and bins and [i for i, _ in bins] == list(range(1, len(bins) + 1)):
            plan = {"files": [by_name[_WIN_SPLIT_EXE]] + [f for _, f in bins], "launch": _WIN_SPLIT_EXE,
                    "join": None, "sha256": None}
    elif suffix:
        parts = sorted(((int(m.group(1)), f) for n, f in by_name.items() if (m := _LINUX_PART.match(n))), key=lambda x: x[0])
        if parts and [i for i, _ in parts] == list(range(len(parts))):
            plan = {"files": [f for _, f in parts], "launch": "FieldStation42-steamos-installer.run",
                    "join": "FieldStation42-steamos-installer.run", "sha256": by_name.get(_LINUX_SHA, {}).get("url")}
    if plan:
        plan["sums_url"] = sums
        plan["size"] = sum(f["size"] for f in plan["files"])
    return plan


def _git(*args, timeout=60):
    return subprocess.run(["git", *args], cwd=str(_repo_root()), capture_output=True, text=True, timeout=timeout)


def _check_git() -> dict:
    fetch = _git("fetch", "--quiet", timeout=120)
    if fetch.returncode != 0:
        return {"ok": False, "error": "git fetch failed: " + (fetch.stderr.strip() or fetch.stdout.strip())}
    branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    behind = _git("rev-list", "--count", "HEAD..@{u}")
    if behind.returncode != 0:
        return {"ok": False, "error": f"Branch {branch} does not track a GitHub branch."}
    count = int(behind.stdout.strip() or 0)
    log = _git("log", "--format=%s", "-n", "10", "HEAD..@{u}").stdout.strip()
    return {
        "latest": f"{branch} +{count} commit{'s' if count != 1 else ''}" if count else branch,
        "available": count > 0,
        "notes": log,
        "commits": count,
    }


# ----------------------------------------------------------------- install

class UpdateJob:
    """Download, verify and launch an update on a background thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._status = {"state": "idle", "progress": 0.0, "message": "", "error": ""}

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def _set(self, **values):
        with self._lock:
            self._status.update(values)

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, info=None, on_launched=None) -> dict:
        """Begin updating.  ``info`` is a check() result (checked again if None).

        ``on_launched`` runs once the installer is on its way - the caller
        uses it to shut the app down (Windows) or show a final message.
        """
        if self.busy:
            return self.status()
        self._set(state="checking", progress=0.0, message="Checking GitHub...", error="")
        self._thread = threading.Thread(target=self._run, args=(info, on_launched), daemon=True, name="fs42-update")
        self._thread.start()
        return self.status()

    def _run(self, info, on_launched):
        try:
            info = info or check()
            if not info.get("ok", True) and info.get("error"):
                raise RuntimeError(info["error"])
            if not info.get("available"):
                raise RuntimeError(f"FieldStation42 {current_version()} is already the newest version.")
            if info.get("mode") == "git":
                self._run_git()
            else:
                path = self._download(info)
                self._verify(path, info)
                self._set(state="installing", progress=1.0,
                          message=f"Installing {info.get('latest')} - FieldStation42 will close and start again...")
                launch_installer(path)
                if os.name == "nt":
                    # Setup stops whatever is still running before it copies;
                    # closing down first just makes that tidy.
                    time.sleep(2)
                    from fs42 import ipc

                    ipc.request_shutdown("update")
            if on_launched:
                on_launched()
        except Exception as e:
            _l.warning("Update failed: %s", e)
            self._set(state="error", error=str(e), message="")

    def _download(self, info) -> Path:
        """Fetch every file of the plan; returns the file to launch."""
        plan = info.get("plan") or {}
        latest = info.get("latest")
        folder = paths.cache("updates", str(latest or "latest"))
        folder.mkdir(parents=True, exist_ok=True)
        total = int(plan.get("size") or 0)
        done = 0
        last = 0.0
        self._set(state="downloading", progress=0.0, message=f"Downloading {latest}...")
        for item in plan["files"]:
            target = folder / item["name"]
            if target.exists() and item["size"] and target.stat().st_size == item["size"]:
                done += item["size"]          # kept from an interrupted attempt
                continue
            partial = target.with_name(target.name + ".part")
            got = 0
            with _get(item["url"], accept="application/octet-stream") as response, open(partial, "wb") as out:
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    got += len(chunk)
                    now = time.monotonic()
                    if now - last > 0.25:
                        last = now
                        so_far = done + got
                        self._set(progress=min(0.99, so_far / total) if total else 0.0,
                                  message=f"Downloading {latest}... {so_far // (1024 * 1024)} of {total // (1024 * 1024)} MB")
            if item["size"] and got != item["size"]:
                raise RuntimeError("The download was cut short; try again.")
            os.replace(partial, target)
            done += got
        if plan.get("join"):
            self._set(state="verifying", message="Joining the download...")
            joined = folder / plan["join"]
            with open(joined, "wb") as out:
                for item in plan["files"]:
                    with open(folder / item["name"], "rb") as part:
                        shutil.copyfileobj(part, out, 1024 * 1024)
            for item in plan["files"]:
                (folder / item["name"]).unlink(missing_ok=True)
            return joined
        return folder / plan["launch"]

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest().lower()

    def _verify(self, path: Path, info):
        plan = info.get("plan") or {}
        expected = None
        if plan.get("sha256"):
            with _get(plan["sha256"], accept="application/octet-stream") as response:
                expected = (response.read().decode("utf-8", "replace").split() or [""])[0].lower()
        elif plan.get("sums_url"):
            with _get(plan["sums_url"], accept="application/octet-stream") as response:
                listing = response.read().decode("utf-8", "replace")
            for line in listing.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[-1].lstrip("*") == path.name:
                    expected = parts[0].lower()
        if not expected:
            # Split Windows setups check their own pieces as they unpack.
            _l.info("No checksum published for %s", path.name)
            return
        self._set(state="verifying", message="Checking the download...")
        if self._sha256(path) != expected:
            path.unlink(missing_ok=True)
            raise RuntimeError("The download does not match the release's checksum; try again.")

    def _run_git(self):
        self._set(state="installing", progress=0.5, message="git pull...")
        pull = _git("pull", "--ff-only", timeout=300)
        if pull.returncode != 0:
            raise RuntimeError("git pull failed: " + (pull.stderr.strip() or pull.stdout.strip()))
        from fs42 import ipc

        self._set(state="done", progress=1.0, message="Updated from GitHub - restarting FieldStation42...")
        ipc.request_restart("update")


def launch_installer(path: Path):
    """Start the installer so that it outlives this process.

    The installer stops FieldStation42 before replacing it, so it must not
    be one of FieldStation42's own processes: on Linux it gets its own
    session (the supervisor stops whole process groups), and on Windows it
    is started through ``cmd /c start`` so it is not in the tree Setup's
    ``taskkill /T`` walks.
    """
    path = Path(path)
    if os.name == "nt":
        args = ["cmd.exe", "/c", "start", "", str(path),
                "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/MERGETASKS=!firewall", "/relaunch=1"]
        subprocess.Popen(args, creationflags=0x08000000, close_fds=True)  # CREATE_NO_WINDOW
        return

    prefix = install_dir()
    paths.cache("updates").mkdir(parents=True, exist_ok=True)
    log = paths.cache("updates", "update.log")
    flags = ["--yes", "--no-steam", "--prefix", str(prefix)]
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    flags.append("--autostart" if os.path.exists(os.path.join(config_home, "autostart", "fieldstation42.desktop")) else "--no-autostart")
    quoted = " ".join(_sh_quote(a) for a in flags)
    script = paths.cache("updates", "run-update.sh")
    script.write_text(f"""#!/bin/sh
# Written by FieldStation42's updater.
exec >>{_sh_quote(str(log))} 2>&1
echo "=== $(date) updating from {_sh_quote(path.name)}"
sleep 1
if sh {_sh_quote(str(path))} {quoted}; then
    echo "=== installed; starting FieldStation42"
else
    echo "=== installer failed ($?); starting the existing FieldStation42"
fi
nohup {_sh_quote(str(prefix / 'FieldStation42'))} >/dev/null 2>&1 &
""")
    env = dict(os.environ)
    # Do not hand the bundle's library path to the system shell and tools.
    if "LD_LIBRARY_PATH_ORIG" in env:
        env["LD_LIBRARY_PATH"] = env.pop("LD_LIBRARY_PATH_ORIG")
    else:
        env.pop("LD_LIBRARY_PATH", None)
    subprocess.Popen(["sh", str(script)], env=env, start_new_session=True, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _sh_quote(text: str) -> str:
    return "'" + str(text).replace("'", "'\"'\"'") + "'"


_job = None


def job() -> UpdateJob:
    """The process-wide update job."""
    global _job
    if _job is None:
        _job = UpdateJob()
    return _job
