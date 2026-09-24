"""PyInstaller runtime hook.

Runs before any application code, in every process the build spawns - the
supervisor, each re-exec'd role, and each multiprocessing child.  Its whole
job is making the bundled binaries and Qt plugins findable before anything
tries to use them.
"""

import os
import sys

if getattr(sys, "frozen", False):
    _base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))

    # A windowed (no-console) build has no standard streams at all:
    # sys.stdout and sys.stderr are None.  print() tolerates that, but
    # anything that calls .isatty(), .write() or .flush() on them crashes -
    # uvicorn's log formatter does exactly that at startup.  Give every
    # process real streams backed by a log file in the data folder, so the
    # output is also somewhere a person can find it.
    if sys.stdout is None or sys.stderr is None:
        _stream = None
        try:
            if os.name == "nt":
                _data = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local"),
                                     "FieldStation42")
            else:
                _data = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
                                     "fieldstation42")
            _data = os.environ.get("FS42_HOME", _data)
            _logs = os.path.join(_data, "logs")
            os.makedirs(_logs, exist_ok=True)
            _stream = open(os.path.join(_logs, "console.log"), "a", encoding="utf-8", errors="replace", buffering=1)
        except OSError:
            _stream = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = _stream
        if sys.stderr is None:
            sys.stderr = _stream
        sys.__stdout__ = sys.__stdout__ or sys.stdout
        sys.__stderr__ = sys.__stderr__ or sys.stderr

    if os.name == "nt":
        _system = "windows"
    elif sys.platform == "darwin":
        _system = "macos"
    else:
        _system = "linux"

    import platform as _platform

    _machine = _platform.machine().lower()
    _arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(_machine, _machine)
    _bin = os.path.join(_base, "bin", f"{_system}-{_arch}")

    if os.path.isdir(_bin):
        os.environ["PATH"] = _bin + os.pathsep + os.environ.get("PATH", "")
        # mpv.exe loads its codec DLLs from its own directory; on Windows that
        # needs an explicit DLL search path.
        if os.name == "nt" and hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(_bin)
            except OSError:
                pass
        _ffmpeg = os.path.join(_bin, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if os.path.exists(_ffmpeg):
            os.environ.setdefault("IMAGEIO_FFMPEG_EXE", _ffmpeg)

        if os.name != "nt":
            # Belt and braces: whatever the extraction did to the mode bits,
            # the launchers must be executable.  The Linux mpv is a wrapper
            # script over a relocatable AppDir.
            for _launcher in ("mpv", "ffmpeg", "ffprobe", "mpv.AppDir/AppRun", "mpv.AppDir/AppRun.sh"):
                _path = os.path.join(_bin, _launcher)
                if os.path.isfile(_path) and not os.access(_path, os.X_OK):
                    try:
                        os.chmod(_path, os.stat(_path).st_mode | 0o111)
                    except OSError:
                        pass

    # Qt looks for its platform plugins relative to the executable, which is
    # wrong under onefile extraction.
    _qt_plugins = os.path.join(_base, "PySide6", "Qt", "plugins")
    if os.path.isdir(_qt_plugins):
        os.environ.setdefault("QT_PLUGIN_PATH", _qt_plugins)
        os.environ.setdefault(
            "QT_QPA_PLATFORM_PLUGIN_PATH", os.path.join(_qt_plugins, "platforms")
        )

    if os.name != "nt":
        # QtWebEngine's sandbox cannot work from an extracted bundle.
        os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
