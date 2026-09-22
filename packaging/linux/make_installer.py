#!/usr/bin/env python3
"""Build the self-extracting Linux/SteamOS installer.

    python packaging/linux/make_installer.py --dist dist/FieldStation42 \\
        --version 1.2.3 --output artifacts/FieldStation42-1.2.3-linux-x64-installer.run

The result is a shell script with a gzip'd tarball appended: run it and it
unpacks to a temporary directory and starts the graphical wizard
(``FieldStation42 install``, Qt from the payload itself) when there is a
display, or ``install.sh`` in the terminal otherwise or when given any
arguments (``--yes``, ``--prefix``, ``--steam`` ...).  ``--extract-only DIR``
just unpacks.  No makeself dependency; the header below is the whole trick.
"""

import argparse
import hashlib
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

HEADER = r"""#!/bin/sh
# FieldStation42 {version} - self-extracting installer for Linux and SteamOS.
#
#   sh FieldStation42-*-installer.run            graphical installer (terminal
#                                                installer when there is no display)
#   sh FieldStation42-*-installer.run --no-gui   terminal installer, interactive
#   sh FieldStation42-*-installer.run --yes      terminal installer, no questions
#   sh FieldStation42-*-installer.run --help     installer options
#   sh FieldStation42-*-installer.run --extract-only DIR
#
# Nothing here needs root. The archive is a plain tar.gz after the
# __ARCHIVE_BELOW__ line; sha256 of the payload: {sha256}
set -e
payload_size={size}
if [ "${{1:-}}" = "--extract-only" ]; then
    dest="${{2:?--extract-only needs a directory}}"; mkdir -p "$dest"
    sed '1,/^__ARCHIVE_BELOW__$/d' "$0" | tar xzf - -C "$dest"
    echo "Extracted to $dest"; exit 0
fi
if [ "${{1:-}}" = "--check" ]; then
    sum=$(sed '1,/^__ARCHIVE_BELOW__$/d' "$0" | sha256sum | cut -d' ' -f1)
    [ "$sum" = "{sha256}" ] && echo "payload ok" || {{ echo "payload CORRUPT"; exit 1; }}
    exit 0
fi
if [ "${{1:-}}" = "--help" ] || [ "${{1:-}}" = "-h" ]; then
    sed -n '2,10p' "$0" | sed 's/^# \{{0,1\}}//'; echo
    tmp="${{TMPDIR:-/tmp}}/fs42-help.$$"; mkdir -p "$tmp"
    sed '1,/^__ARCHIVE_BELOW__$/d' "$0" | tar xzf - -C "$tmp" install.sh
    sh "$tmp/install.sh" --help; rm -rf "$tmp"; exit 0
fi
tmp="${{TMPDIR:-/tmp}}/fs42-install.$$"
trap 'rm -rf "$tmp"' EXIT INT TERM
mkdir -p "$tmp"
echo "Unpacking FieldStation42 {version} ({size_mb} MB)..."
sed '1,/^__ARCHIVE_BELOW__$/d' "$0" | tar xzf - -C "$tmp"
# Graphical wizard when there is a display and no terminal options were given.
gui=1
for a in "$@"; do gui=0; done
if [ "$gui" = 1 ] && {{ [ -n "${{DISPLAY:-}}" ] || [ -n "${{WAYLAND_DISPLAY:-}}" ]; }}; then
    "$tmp/FieldStation42/FieldStation42" install --payload "$tmp" && exit 0
    code=$?
    [ "$code" = 1 ] && exit 1        # cancelled in the wizard
    echo "The graphical installer could not start (exit $code); using the terminal installer."
fi
[ "${{1:-}}" = "--no-gui" ] && shift
sh "$tmp/install.sh" "$@"
exit $?
__ARCHIVE_BELOW__
"""


def build(dist: Path, version: str, output: Path) -> Path:
    app_dir = dist.resolve()
    if not (app_dir / "FieldStation42").is_file():
        raise SystemExit(f"{app_dir} does not look like the folder build (no FieldStation42 executable)")

    with tempfile.TemporaryDirectory() as scratch:
        payload = Path(scratch) / "payload"
        payload.mkdir()
        # cp -a semantics: the bundled mpv AppDir is full of symlinks.
        shutil.copytree(app_dir, payload / "FieldStation42", symlinks=True)
        for name in ("install.sh", "uninstall.sh", "steam_shortcut.py", "fieldstation42.desktop", "fieldstation42.png"):
            shutil.copy2(HERE / name, payload / name)
        for name in ("LICENSE", "THIRD_PARTY_LICENSES.md"):
            if (ROOT / name).exists():
                shutil.copy2(ROOT / name, payload / name)
        for name in ("install.sh", "uninstall.sh", "steam_shortcut.py"):
            path = payload / name
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        tarball = Path(scratch) / "payload.tar.gz"
        # Reproducible-ish: fixed owner, sorted names.
        with tarfile.open(tarball, "w:gz", compresslevel=6) as tar:
            def clean(info):
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                return info
            for entry in sorted(payload.iterdir()):
                tar.add(entry, arcname=entry.name, filter=clean)

        data = tarball.read_bytes()

    digest = hashlib.sha256(data).hexdigest()
    header = HEADER.format(version=version, sha256=digest, size=len(data), size_mb=len(data) // (1024 * 1024))
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "wb") as handle:
        handle.write(header.encode("utf-8"))
        handle.write(data)
    output.chmod(output.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return output


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dist", default=str(ROOT / "dist" / "FieldStation42"), help="PyInstaller onedir output.")
    parser.add_argument("--version", default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)

    version = args.version
    if not version:
        sys.path.insert(0, str(ROOT))
        import fs42

        version = fs42.__version__
    output = Path(args.output) if args.output else ROOT / "artifacts" / f"FieldStation42-{version}-linux-x64-installer.run"
    result = build(Path(args.dist), version, output)
    print(f"{result} ({result.stat().st_size // (1024 * 1024)} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
