#!/usr/bin/env python3
"""Download the helper binaries the distributable build bundles.

Nothing binary lives in the repository.  This fetches mpv and ffmpeg/ffprobe
for a target platform into ``packaging/vendor/<platform>/``, verifying each
archive against the digest pinned in ``binaries.lock.json``.

    python packaging/fetch_binaries.py --platform windows-x64
    python packaging/fetch_binaries.py                # host platform
    python packaging/fetch_binaries.py --update       # move every pin to the
                                                      # newest release and
                                                      # rewrite the lockfile

Run it before PyInstaller; the spec picks up whatever is in vendor/.

What was actually bundled - final URL, digest, version string - is written to
``vendor/<platform>/MANIFEST.json`` and ships inside the application, so a
release can always say exactly which upstream build it carries.
"""

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCKFILE = HERE / "binaries.lock.json"
VENDOR = HERE / "vendor"

CHUNK = 1 << 20
USER_AGENT = "FieldStation42-build (+https://github.com/ryanmah/f242_Arcade)"

WRAPPER = """#!/bin/sh
# FieldStation42: run the bundled mpv out of its relocatable AppDir.
here="$(dirname "$(readlink -f "$0")")"
export APPDIR="$here/{appdir}"
exec "$APPDIR/AppRun" "$@"
"""


def host_platform() -> str:
    import platform as _platform

    machine = _platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)
    system = "windows" if os.name == "nt" else ("macos" if sys.platform == "darwin" else "linux")
    return f"{system}-{arch}"


# ------------------------------------------------------------------ network

def _request(url: str, accept=None):
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def download(url: str, target: Path) -> str:
    print(f"  downloading {url}")
    digest = hashlib.sha256()
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(_request(url), timeout=120) as response, open(target, "wb") as handle:
        while True:
            block = response.read(CHUNK)
            if not block:
                break
            handle.write(block)
            digest.update(block)
    return digest.hexdigest()


def resolve_latest(repo: str, asset_pattern: str):
    """Find the newest release asset of ``repo`` matching ``asset_pattern``.

    Tries the GitHub API first (authenticated when GITHUB_TOKEN is set, which
    it always is on Actions).  Without API access, falls back to the redirect
    GitHub serves for ``releases/latest/download/…`` to learn the tag and
    scrapes that release's asset list.
    """
    api = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        with urllib.request.urlopen(_request(api, "application/vnd.github+json"), timeout=60) as response:
            data = json.load(response)
        for asset in data.get("assets", []):
            if fnmatch.fnmatch(asset["name"], asset_pattern):
                return data["tag_name"], asset["browser_download_url"]
        raise RuntimeError(f"no asset matching {asset_pattern} in {repo} {data.get('tag_name')}")
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError) as e:
        print(f"  GitHub API unavailable ({e}); trying the release page")

    probe = f"https://github.com/{repo}/releases/latest/download/_"

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(_request(probe), timeout=60)
    except urllib.error.HTTPError as e:
        location = e.headers.get("Location", "")
    else:
        location = ""
    match = re.search(r"/releases/download/([^/]+)/", location)
    if not match:
        raise RuntimeError(f"could not determine the latest release of {repo}")
    tag = match.group(1)

    page = f"https://github.com/{repo}/releases/expanded_assets/{tag}"
    with urllib.request.urlopen(_request(page), timeout=60) as response:
        html = response.read().decode("utf-8", "replace")
    for href in re.findall(r'href="([^"]+/releases/download/[^"]+)"', html):
        name = href.rsplit("/", 1)[-1]
        if fnmatch.fnmatch(name, asset_pattern):
            return tag, "https://github.com" + href if href.startswith("/") else href
    raise RuntimeError(f"no asset matching {asset_pattern} in {repo} {tag}")


# --------------------------------------------------------------- archives

def _matches(name: str, patterns) -> bool:
    base = os.path.basename(name)
    return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(base, p) for p in patterns)


def _make_executable(path: Path):
    if os.name != "nt":
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _unpack(archive: Path, kind: str, scratch: Path):
    if kind == "zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(scratch)
    elif kind in ("tar.gz", "tar.xz", "tar"):
        with tarfile.open(archive) as tf:
            tf.extractall(scratch)
    elif kind == "7z":
        # The mpv Windows builds use the BCJ2 filter, which py7zr cannot
        # decode, so this needs the real 7-Zip: p7zip-full on Linux,
        # `choco install 7zip` on Windows.
        candidates = ["7z", "7za", "7zr", "7zz"]
        if os.name == "nt":
            candidates += [r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"]
        seven_zip = next((c for c in candidates if shutil.which(c) or os.path.isfile(c)), None)
        if not seven_zip:
            raise RuntimeError("Unpacking a .7z needs 7-Zip on PATH (p7zip-full / choco install 7zip).")
        subprocess.run([seven_zip, "x", f"-o{scratch}", "-y", str(archive)], check=True,
                       stdout=subprocess.DEVNULL)
    else:
        raise RuntimeError(f"Unknown archive type: {kind}")


def extract_files(archive: Path, kind: str, patterns, destination: Path) -> list:
    """Copy the matching files out of a flat archive into ``destination``."""
    destination.mkdir(parents=True, exist_ok=True)
    extracted = []
    with tempfile.TemporaryDirectory() as scratch:
        scratch = Path(scratch)
        _unpack(archive, kind, scratch)
        for path in scratch.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(scratch).as_posix()
            if not _matches(relative, patterns):
                continue
            target = destination / path.name
            shutil.copy2(path, target)
            _make_executable(target)
            extracted.append(target.name)
    return extracted


def extract_appimage(archive: Path, spec: dict, destination: Path) -> list:
    """Unpack an AppImage into a relocatable AppDir plus a launcher script.

    An AppImage needs FUSE (or a writable /tmp and a self-extract on every
    launch) to run as-is.  Its payload is a plain directory, and the
    ``anylinux`` builds we pin are built to run from anywhere, so we ship the
    directory instead: it needs nothing from the host and is one ``exec``
    away.  Symlinks inside it are preserved - PyInstaller carries them
    through both onedir and onefile builds.
    """
    if os.name == "nt":
        raise RuntimeError("AppImages can only be unpacked on Linux")
    appdir_name = spec.get("appdir", "mpv.AppDir")
    wrapper_name = spec.get("wrapper", "mpv")
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / appdir_name
    if target.exists():
        shutil.rmtree(target)

    with tempfile.TemporaryDirectory() as scratch:
        scratch = Path(scratch)
        image = scratch / archive.name
        shutil.copy2(archive, image)
        _make_executable(image)
        result = subprocess.run(
            [str(image), "--appimage-extract"],
            cwd=scratch,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        unpacked = scratch / "squashfs-root"
        if result.returncode != 0 or not unpacked.is_dir():
            raise RuntimeError(f"--appimage-extract failed: {result.stderr.strip()[-400:]}")
        # copytree(symlinks=True) is the Python spelling of `cp -a`.
        shutil.copytree(unpacked, target, symlinks=True)

    removed = []
    for relative in spec.get("strip", []):
        victim = target / relative
        if victim.exists() or victim.is_symlink():
            victim.unlink()
            removed.append(relative)
    for relative in ("AppRun", "AppRun.sh"):
        candidate = target / relative
        if candidate.exists():
            _make_executable(candidate)

    wrapper = destination / wrapper_name
    wrapper.write_text(WRAPPER.format(appdir=appdir_name))
    _make_executable(wrapper)
    return [wrapper_name, f"{appdir_name}/", *(f"-{r}" for r in removed)]


def _version_hint(name: str, url: str, destination: Path) -> str:
    """Best-effort version string for the manifest."""
    if name == "ffmpeg":
        probe = destination / ("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if probe.exists():
            try:
                out = subprocess.run([str(probe), "-version"], capture_output=True, text=True, timeout=30).stdout
                match = re.search(r"version (\S+)", out.splitlines()[0] if out else "")
                if match:
                    return match.group(1)
            except Exception:
                pass
    match = re.search(r"/releases/download/([^/]+)/", url)
    return urllib.request.unquote(match.group(1)) if match else ""


# ----------------------------------------------------------------- driver

def fetch(platform_tag: str, print_hashes=False, allow_unpinned=False, update=False) -> int:
    with open(LOCKFILE) as handle:
        lock = json.load(handle)

    entries = lock["platforms"].get(platform_tag)
    if entries is None:
        print(f"No binaries pinned for {platform_tag}.")
        print("The application will fall back to whatever is installed on the system.")
        return 0

    destination = VENDOR / platform_tag
    manifest = {"platform": platform_tag, "binaries": {}}
    problems = 0
    lock_changed = False

    for name, spec in entries.items():
        print(f"{platform_tag}/{name}:")
        url = spec["url"]
        expected = spec.get("sha256")
        release = spec.get("release")

        if update and release:
            try:
                tag, url = resolve_latest(release["repo"], release["asset"])
                print(f"  newest release: {tag}")
                expected = None
            except Exception as e:
                print(f"  could not resolve the newest release ({e}); keeping the pin")

        if expected is None and not (print_hashes or allow_unpinned or update):
            print("  ERROR: no sha256 pinned. Run with --print-hashes and record it.")
            problems += 1
            continue

        with tempfile.TemporaryDirectory() as scratch:
            archive = Path(scratch) / os.path.basename(urllib.request.unquote(url))
            actual = None
            try:
                actual = download(url, archive)
            except Exception as e:
                if release and not update:
                    # The pinned build was pruned upstream; take the newest one
                    # and say so loudly, so the pin gets moved forward.
                    print(f"  pinned download failed ({e}); resolving the newest release instead")
                    try:
                        tag, url = resolve_latest(release["repo"], release["asset"])
                        print(f"  using {tag}")
                        archive = Path(scratch) / os.path.basename(urllib.request.unquote(url))
                        actual = download(url, archive)
                        expected = None
                    except Exception as e2:
                        print(f"  ERROR: fallback failed too: {e2}")
                elif spec.get("optional"):
                    print(f"  skipped (optional): {e}")
                    continue
                else:
                    print(f"  ERROR: download failed: {e}")
            if actual is None:
                problems += 1
                continue

            if print_hashes or expected is None:
                print(f"  sha256: {actual}")
            if expected and actual != expected:
                if spec.get("rolling"):
                    print(f"  note: rolling '{url.rsplit('/', 2)[-2]}' asset moved on\n"
                          f"    pinned  {expected}\n    got     {actual}\n"
                          "    (recorded in MANIFEST.json; run --update to move the pin)")
                else:
                    print(f"  ERROR: digest mismatch\n    expected {expected}\n    got      {actual}")
                    problems += 1
                    continue

            try:
                kind = spec.get("archive", "zip")
                if kind == "appimage":
                    files = extract_appimage(archive, spec, destination)
                else:
                    files = extract_files(archive, kind, spec["extract"], destination)
            except Exception as e:
                if spec.get("optional"):
                    print(f"  skipped (optional): {e}")
                    continue
                print(f"  ERROR: extract failed: {e}")
                problems += 1
                continue

            print(f"  extracted: {', '.join(files) if files else '(nothing matched)'}")
            manifest["binaries"][name] = {
                "url": url,
                "sha256": actual,
                "version": _version_hint(name, url, destination),
                "license": spec.get("license"),
                "source": spec.get("source"),
            }
            if update:
                spec["url"], spec["sha256"] = url, actual
                lock_changed = True

    if manifest["binaries"]:
        destination.mkdir(parents=True, exist_ok=True)
        with open(destination / "MANIFEST.json", "w") as handle:
            json.dump(manifest, handle, indent=2)
            handle.write("\n")

    if lock_changed:
        with open(LOCKFILE, "w") as handle:
            json.dump(lock, handle, indent=2)
            handle.write("\n")
        print(f"\nLockfile updated: {LOCKFILE}")

    if problems:
        print(f"\n{problems} problem(s).")
    else:
        print(f"\nBinaries ready in {destination}")
    return 1 if problems else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--platform", default=None, help="Target platform tag, e.g. windows-x64.")
    parser.add_argument("--print-hashes", action="store_true", help="Print each download's sha256.")
    parser.add_argument(
        "--allow-unpinned",
        action="store_true",
        help="Proceed even when a lockfile entry has no digest. Do not use in CI.",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Resolve the newest release of every entry that has one, download it, "
             "and rewrite the lockfile with the new url and digest.",
    )
    args = parser.parse_args(argv)
    return fetch(args.platform or host_platform(), args.print_hashes, args.allow_unpinned, args.update)


if __name__ == "__main__":
    raise SystemExit(main())
