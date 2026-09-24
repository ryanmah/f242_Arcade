#!/bin/sh
# Build the Windows installer on a Linux machine, without Windows.
#
# One-time setup (Ubuntu/Debian):
#   dpkg --add-architecture i386 && apt-get update
#   apt-get install -y wine64 wine32:i386 xvfb p7zip-full
#   mkdir -p /opt/winbuild && cd /opt/winbuild
#   # Windows CPython (python-build-standalone) and Inno Setup 6 (32-bit)
#   curl -L -o cpython-win.tar.gz https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.11.16+20260901-x86_64-pc-windows-msvc-install_only.tar.gz
#   tar xzf cpython-win.tar.gz                              # -> /opt/winbuild/python
#   curl -L -o innosetup.exe https://github.com/jrsoftware/issrc/releases/download/is-6_4_3/innosetup-6.4.3.exe
#   WINEPREFIX=/opt/winbuild/prefix   /usr/lib/wine/wine64 wineboot -i
#   WINEPREFIX=/opt/winbuild/prefix32 WINEARCH=win32 wine wineboot -i
#   WINEPREFIX=/opt/winbuild/prefix32 WINEARCH=win32 xvfb-run -a wine innosetup.exe /VERYSILENT /ALLUSERS "/DIR=C:\InnoSetup"
#   # Windows wheels, fetched with the Linux pip and installed offline:
#   pip download -d wheels --platform win_amd64 --python-version 3.11 --only-binary=:all: \
#       -r install/requirements.txt "pyinstaller>=6.6" colorama pywin32-ctypes pefile "numpy==1.26.4"
#   WINEPREFIX=/opt/winbuild/prefix /usr/lib/wine/wine64 python/python.exe -m pip install --no-index --find-links wheels \
#       -r install/requirements.txt "pyinstaller>=6.6" "numpy==1.26.4"
#
# numpy 2.x needs a ucrtbase function Wine 9 lacks (crealf); 1.26 is fine and
# moviepy accepts it.  Wine's Python refuses to start with stdin/stdout on
# plain files, hence the `echo |` and `| cat` below.
#
# Then, from the repository root, with packaging/vendor/windows-x64 fetched:
#   sh packaging/windows/build_under_wine.sh [version] [--split]
#
# --split produces setup.exe + 19MB .bin slices (Inno disk spanning) for
# channels with a per-file size cap; Setup reads them from its own folder.

set -e
VERSION="${1:-$(python3 -c 'import sys; sys.path.insert(0, "."); import fs42; print(fs42.__version__)')}"
SPLIT=""
[ "${2:-}" = "--split" ] && SPLIT="/DSplitForTransfer"

export WINEDEBUG=-all WINEDLLOVERRIDES="mscoree,mshtml="
WINE64=/usr/lib/wine/wine64
PY=/opt/winbuild/python/python.exe
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

echo "== PyInstaller (Windows) =="
WINEPREFIX=/opt/winbuild/prefix WINELOADER=$WINE64 \
  sh -c "echo | $WINE64 $PY -m PyInstaller --clean --noconfirm --distpath dist-win --workpath build-win \
      packaging/fieldstation42.spec -- --mode onedir --tag windows-x64 2>&1 | cat"
test -f dist-win/FieldStation42/FieldStation42.exe

echo "== Self-check under Wine =="
WINEPREFIX=/opt/winbuild/prefix WINELOADER=$WINE64 \
  sh -c "echo | xvfb-run -a $WINE64 dist-win/FieldStation42/FieldStation42.exe --version 2>&1 | cat"

echo "== Inno Setup =="
mkdir -p artifacts
WINEPREFIX=/opt/winbuild/prefix32 WINEARCH=win32 \
  sh -c "echo | xvfb-run -a wine 'C:\\InnoSetup\\ISCC.exe' /DAppVersion=$VERSION '/DSourceDir=..\\..\\dist-win\\FieldStation42' \
      /DNoSeparateLzma $SPLIT 'Z:$ROOT/packaging/windows/FieldStation42.iss' 2>&1 | grep -v 'X connection' | cat"
ls -la artifacts/
