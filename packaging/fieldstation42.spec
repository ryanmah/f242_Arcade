# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the FieldStation42 single executable.

    pyinstaller --clean --noconfirm packaging/fieldstation42.spec -- --mode onedir
    pyinstaller --clean --noconfirm packaging/fieldstation42.spec -- --mode onefile

Two artifacts per platform:

* onefile - the single downloadable executable.  It re-extracts its payload on
  every launch, so it trades startup time for being one file.
* onedir  - a folder build that starts instantly, and keeps Qt's shared
  libraries individually replaceable, which is what LGPL v3 asks for.

QtWebEngine is deliberately excluded.  It is roughly 400MB, about 70% of an
all-in build, and only web-source channels need it; it ships as a separate
add-on that unpacks into the user's data directory (see cli._prepare_environment).
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

# ---------------------------------------------------------------- arguments

MODE = "onedir"
TAG = None
for index, value in enumerate(sys.argv):
    if value == "--mode" and index + 1 < len(sys.argv):
        MODE = sys.argv[index + 1]
    elif value.startswith("--mode="):
        MODE = value.split("=", 1)[1]
    elif value == "--tag" and index + 1 < len(sys.argv):
        TAG = sys.argv[index + 1]
    elif value.startswith("--tag="):
        TAG = value.split("=", 1)[1]

ROOT = Path(SPECPATH).resolve().parent
PACKAGING = ROOT / "packaging"


def platform_tag():
    if TAG:
        return TAG
    import platform as _platform

    machine = _platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)
    system = "windows" if os.name == "nt" else ("macos" if sys.platform == "darwin" else "linux")
    return f"{system}-{arch}"


PLATFORM = platform_tag()
IS_WINDOWS = PLATFORM.startswith("windows")

# -------------------------------------------------------------------- data

datas = [
    # Web console, web remote, themes, PPV and custom guide assets.
    (str(ROOT / "fs42" / "fs42_server" / "static"), "fs42/fs42_server/static"),
    # Station config schema, validated at load time.
    (str(ROOT / "fs42" / "station_config_schema.json"), "fs42"),
    # Textual stylesheets, resolved relative to their defining module.
    (str(ROOT / "fs42" / "ux"), "fs42/ux"),
    # The VCR font the on-screen menu is drawn with.
    (str(ROOT / "fs42" / "menu" / "fonts"), "fs42/menu/fonts"),
    # Seeded into the user's data directory on first run.
    (str(ROOT / "confs" / "examples"), "confs/examples"),
    (str(ROOT / "osd"), "osd"),
    (str(PACKAGING / "licenses"), "licenses"),
    # Icon for the graphical installer / uninstaller wizard.
    (str(PACKAGING / "linux" / "fieldstation42.png"), "installer"),
    (str(ROOT / "THIRD_PARTY_LICENSES.md"), "."),
]

# Default media: sign-off, static, off-air pattern and reference art.  Bundled
# so a fresh install has something to play immediately.
for name in ("static.mp4", "standby.png", "brb.png", "off_air_pattern.mp4", "signoff.mp4"):
    candidate = ROOT / "docs" / name
    if candidate.exists():
        datas.append((str(candidate), "docs"))
if (ROOT / "docs" / "logo_images").is_dir():
    datas.append((str(ROOT / "docs" / "logo_images"), "docs/logo_images"))

# Package data that would otherwise be missed: jsonschema ships its
# metaschemas as data files, and StationIO.load_schema fails without them.
for package in ("jsonschema", "jsonschema_specifications", "referencing", "textual", "mutagen", "moviepy", "proglog"):
    try:
        datas += collect_data_files(package)
    except Exception:
        pass

# ---------------------------------------------------------------- binaries

binaries = []
vendor = PACKAGING / "vendor" / PLATFORM
HAVE_VENDORED_FFMPEG = False
if vendor.is_dir():
    # Everything fetch_binaries.py produced goes in verbatim, as data rather
    # than as "binaries": PyInstaller would otherwise try to trace the
    # dependencies of a 120MB static mpv, and the Linux mpv is a whole
    # AppDir (symlinks included - PyInstaller 6 preserves them) with a shell
    # wrapper in front of it.
    for entry in vendor.iterdir():
        if entry.is_dir():
            datas.append((str(entry), f"bin/{PLATFORM}/{entry.name}"))
        else:
            datas.append((str(entry), f"bin/{PLATFORM}"))
        if entry.stem in ("ffmpeg", "ffprobe"):
            HAVE_VENDORED_FFMPEG = True
    print(f"[fs42] bundling vendored binaries from {vendor}")
else:
    print(f"[fs42] no vendored binaries for {PLATFORM}; the build will rely on mpv/ffprobe from the system")

# python-build-standalone's _tkinter links libtcl9.0.so / libtcl9tk9.0.so by
# bare name (no rpath), so PyInstaller cannot trace them and the guide
# channel fails with "libtcl9.0.so: cannot open shared object file".
# Ship them next to the other libraries.
if PLATFORM.startswith("linux"):
    import glob as _glob

    for _lib in sorted(_glob.glob(os.path.join(sys.base_prefix, "lib", "libtcl*.so*"))):
        binaries.append((_lib, "."))
        print(f"[fs42] bundling {os.path.basename(_lib)} for tkinter")

# imageio-ffmpeg ships its own ~75MB ffmpeg for moviepy.  Only bundle it when
# we are not already shipping one.
if not HAVE_VENDORED_FFMPEG:
    try:
        binaries += collect_dynamic_libs("imageio_ffmpeg")
        datas += collect_data_files("imageio_ffmpeg", include_py_files=False)
    except Exception:
        pass

# ----------------------------------------------------------- hidden imports

hiddenimports = [
    # uvicorn resolves these by string at runtime.
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # Roles and backends are imported by name.
    "fs42.app.roles",
    "fs42.app.supervisor",
    "fs42.app.doctor",
    "fs42.app.setup_wizard",
    "fs42.osd.backends.mpv_osd",
    "fs42.osd.main",
    # Menu process target and the pages it imports lazily.
    "fs42.menu.app",
    "fs42.menu.pages",
    "fs42.menu.gamepad",
    "fs42.build_jobs",
    "fs42.prewarm",
    "fs42.video_effects",
    "fs42.effects_overlay",
    "fs42.picture",
    "fs42.menu.effects",
    "fs42.live_schedule_agent",
    "field_player",
    "station_42",
    "encodings.idna",
]
try:
    hiddenimports += collect_submodules("fs42.fs42_server.api")
except Exception:
    pass

excludes = [
    # Removed with the Raspberry Pi support; make sure a stray install of
    # either never gets pulled in.
    "evdev",
    "serial",
    # The GLFW on-screen display is gone.
    "glfw",
    "OpenGL",
    # QtWebEngine ships as a separate add-on - see the module docstring.
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    # Qt modules nothing here uses; worth ~100MB on their own.
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtDesigner",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtQuick3D",
    # Test and notebook tooling.
    "pytest",
    "IPython",
    "matplotlib",
]

block_cipher = None

a = Analysis(
    [str(ROOT / "fieldstation42.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(PACKAGING / "hooks" / "rthook_fs42.py")],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# --------------------------------------------------- prune unwanted Qt libs
#
# `excludes` only stops Python modules from being imported.  PySide6's own
# PyInstaller hook collects every Qt shared library it can find regardless, so
# QtWebEngineCore (178MB) and friends still land in the bundle.  Drop them by
# filename.  If someone installs pyside6-essentials instead of the full
# pyside6, none of these exist and this is a no-op.

_QT_LIBRARY_PREFIXES = (
    "libQt6WebEngine", "Qt6WebEngine",
    "libQt63D", "Qt63D",
    "libQt6Quick3D", "Qt6Quick3D",
    "libQt6Charts", "Qt6Charts",
    "libQt6DataVisualization", "Qt6DataVisualization",
    "libQt6Pdf", "Qt6Pdf",
    "libQt6Multimedia", "Qt6Multimedia",
    "libQt6Designer", "Qt6Designer",
    "libQt6SpatialAudio", "Qt6SpatialAudio",
    "libQt6Bluetooth", "Qt6Bluetooth",
    "libQt6Nfc", "Qt6Nfc",
    "libQt6Sensors", "Qt6Sensors",
    "libQt6WebSockets", "Qt6WebSockets",
    "libQt6WebChannel", "Qt6WebChannel",
    # QML / Quick and friends: nothing here is QML-based (the menu and the
    # overlays are plain QtWidgets); together they are ~70MB on Windows.
    "libQt6Quick", "Qt6Quick", "QtQuick",
    "libQt6Qml", "Qt6Qml", "QtQml",
    "libQt6Labs", "Qt6Labs",
    "libQt6Location", "Qt6Location", "QtLocation",
    "libQt6Positioning", "Qt6Positioning", "QtPositioning",
    "libQt6Graphs", "Qt6Graphs", "QtGraphs",
    "libQt6ShaderTools", "Qt6ShaderTools",
    "libQt6VirtualKeyboard", "Qt6VirtualKeyboard",
    "libQt6RemoteObjects", "Qt6RemoteObjects", "QtRemoteObjects",
    "libQt6Scxml", "Qt6Scxml", "QtScxml",
    "libQt6StateMachine", "Qt6StateMachine", "QtStateMachine",
    "libQt6TextToSpeech", "Qt6TextToSpeech", "QtTextToSpeech",
    "libQt6Help", "Qt6Help", "QtHelp",
    "libQt6SerialPort", "Qt6SerialPort", "QtSerialPort",
    "libQt6SerialBus", "Qt6SerialBus", "QtSerialBus",
    "libQt6HttpServer", "Qt6HttpServer", "QtHttpServer",
    "libQt6Quick3D", "Qt6Quick3D", "QtQuick3D",
)

_QT_DATA_MARKERS = (
    "QtWebEngine",
    "qtwebengine",
    "QtWebEngineProcess",
    "Qt/resources",
    "Qt/translations",
    "Qt/qml",
    # Windows wheels lay PySide6 out flat (PySide6/qml, PySide6/translations).
    "PySide6/qml",
    "PySide6/translations",
    "PySide6/plugins/qmltooling",
    "PySide6/plugins/scenegraph",
    "PySide6/plugins/geoservices",
    "PySide6/plugins/position",
)


def _drop_binary(item):
    name = os.path.basename(item[0])
    return name.startswith(_QT_LIBRARY_PREFIXES) or "QtWebEngineProcess" in item[0]


def _drop_data(item):
    destination = item[0].replace("\\", "/")
    return any(marker in destination for marker in _QT_DATA_MARKERS)


_before = len(a.binaries) + len(a.datas)
a.binaries = TOC([item for item in a.binaries if not _drop_binary(item)])
a.datas = TOC([item for item in a.datas if not _drop_data(item)])
print(f"[fs42] pruned {_before - len(a.binaries) - len(a.datas)} Qt entries not used by this build")

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

NAME = "FieldStation42"
CONSOLE = os.environ.get("FS42_CONSOLE_BUILD") == "1"
if CONSOLE:
    NAME = "FieldStation42-debug"

_icon = ROOT / "fs42" / "fs42_server" / "static" / "favicon.ico"

# UPX is off everywhere: it breaks Qt plugin loading, invalidates code
# signatures, and muddies the "these are the unmodified upstream binaries"
# story the GPL notices rely on.
COMMON = dict(
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(_icon) if _icon.exists() else None,
)

if MODE == "onefile":
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        runtime_tmpdir=None,
        **COMMON,
    )
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **COMMON)
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        name=NAME,
    )
