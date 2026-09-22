"""Self-check: what works on this machine, and what does not.

Run ``FieldStation42 doctor`` when something is wrong.  It reports where the
data lives, which helper binaries were found, whether the optional GUI pieces
(the tkinter guide channel, the Qt overlays, the QtWebEngine add-on) are
usable, and whether mpv actually accepts an IPC connection.

It is also the check CI runs against a built executable, because a frozen
build is exactly where Qt plugins and tcl/tk data go missing.
"""

import os
import platform
import sys
import time

OK = "ok"
WARN = "warn"
FAIL = "fail"


class Report:
    def __init__(self):
        self.lines = []
        self.failures = 0

    def add(self, level, name, detail=""):
        self.lines.append((level, name, detail))
        if level == FAIL:
            self.failures += 1

    def render(self, stream=sys.stdout):
        marks = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}
        for level, name, detail in self.lines:
            line = f"[{marks[level]}] {name}"
            if detail:
                line += f"\n           {detail}"
            print(line, file=stream)
        print(file=stream)
        if self.failures:
            print(f"{self.failures} problem(s) found.", file=stream)
        else:
            print("Everything checks out.", file=stream)


def _check_layout(report):
    from fs42 import paths

    info = paths.describe()
    report.add(OK, f"FieldStation42 {info['version']} ({'packaged' if info['frozen'] else 'from source'})")
    report.add(OK, "Platform", f"{platform.system()} {platform.machine()}, Python {platform.python_version()}")
    report.add(OK, "Data folder", info["data"])
    report.add(OK, "Cache folder", info["cache"])

    try:
        paths.first_run_seed()
    except Exception as e:
        report.add(FAIL, "Data folder is not writable", str(e))
        return

    probe = paths.runtime(".doctor-write-test")
    try:
        probe.write_text("ok")
        probe.unlink()
        report.add(OK, "Data folder is writable")
    except OSError as e:
        report.add(FAIL, "Data folder is not writable", str(e))


def _check_binaries(report):
    from fs42 import paths

    manifest = paths.bundled_manifest()
    for name, required in (("mpv", True), ("ffprobe", True), ("ffmpeg", False)):
        found = paths.bin_path(name)
        if found:
            detail = str(found)
            entry = manifest.get("ffmpeg" if name == "ffprobe" else name)
            if entry and found.parent == paths.bin_dir() and entry.get("version"):
                detail += f"  (bundled {entry['version']})"
            report.add(OK, f"{name}", detail)
        elif required:
            report.add(
                FAIL,
                f"{name} not found",
                f'Install it, or set "{name}_path" in {paths.confs("main_config.json")}',
            )
        else:
            report.add(WARN, f"{name} not found", "Only needed for break detection")


def _check_stations(report):
    from fs42 import paths

    try:
        from fs42.station_manager import StationManager

        stations = StationManager().stations
    except SystemExit:
        report.add(FAIL, "Station configs did not load", f"Check the JSON in {paths.confs()}")
        return
    except Exception as e:
        report.add(FAIL, "Station configs did not load", str(e))
        return

    if not stations:
        report.add(
            WARN,
            "No channels configured",
            f"Add one from the web console, or copy an example from {paths.confs('examples')}",
        )
    else:
        names = ", ".join(f"{s['channel_number']} {s['network_name']}" for s in stations[:6])
        more = f" (+{len(stations) - 6} more)" if len(stations) > 6 else ""
        report.add(OK, f"{len(stations)} channel(s)", names + more)


def _check_state_bus(report):
    from fs42 import ipc

    try:
        ipc.init_db()
        ipc.set_state("doctor", {"at": time.time()})
        assert ipc.get_state("doctor") is not None
        ipc.push("doctor", {"ping": True})
        assert ipc.pop("doctor", "doctor") == {"ping": True}
        report.add(OK, "State database", ipc.db_path())
    except Exception as e:
        report.add(FAIL, "State database is not usable", str(e))


def _pick_detail(output, marker=None):
    """Last meaningful line of a probe's output.

    Qt is chatty on stderr, so prefer the line we deliberately printed.
    """
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    if marker:
        for line in reversed(lines):
            if line.startswith(marker):
                return line
    return lines[-1] if lines else ""


def _run_probe(name, timeout=60):
    """Run one probe in a child process.

    Qt calls qFatal and aborts the whole process when it cannot load a
    platform plugin, so probing it in-process would take the doctor down with
    it - which is exactly the situation we most want reported.
    """
    from fs42 import platform_compat

    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        command += [os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "fieldstation42.py")]
    command += ["--fs42-role=probe", name]
    try:
        result = platform_compat.run_hidden(command, timeout=timeout)
    except Exception as e:
        return None, str(e)
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    return result.returncode, output


def _check_guide(report):
    code, output = _run_probe("tkinter")
    if code == 0:
        report.add(OK, "Guide channel (tkinter)")
        return
    detail = _pick_detail(output)
    if "No module named" in detail:
        if os.name == "nt":
            detail += " - reinstall Python with the tcl/tk option enabled"
        else:
            detail += " - install it with: sudo apt install python3-tk"
    report.add(WARN, "Guide channel unavailable", detail or "tkinter could not start")


def _check_qt(report):
    code, output = _run_probe("qt")
    if code == 0:
        report.add(OK, "Overlays (Qt)", _pick_detail(output, "platform plugin:"))
    else:
        report.add(WARN, "Overlays unavailable", _pick_detail(output) or "Qt could not start")

    code, output = _run_probe("menu")
    if code == 0:
        report.add(OK, "Channel menu (Escape)", _pick_detail(output, "menu ok"))
    else:
        report.add(WARN, "Channel menu unavailable", _pick_detail(output) or "could not build the menu window")

    code, output = _run_probe("qtwebengine")
    if code == 0:
        report.add(OK, "Web channels (QtWebEngine)")
    else:
        from fs42 import paths

        report.add(
            WARN,
            "Web channels unavailable",
            f"Install the webengine add-on into {paths.data('plugins')} if you need them",
        )


def _check_mpv_ipc(report):
    """Start mpv headless and talk to it - the whole player rests on this."""
    from fs42 import paths, platform_compat

    if paths.bin_path("mpv") is None:
        report.add(WARN, "mpv IPC not tested", "mpv was not found")
        return
    try:
        from python_mpv_jsonipc import MPV
    except ImportError as e:
        report.add(FAIL, "mpv IPC library missing", str(e))
        return

    endpoint = platform_compat.mpv_ipc_name(f"doctor-{os.getpid()}")
    instance = None
    try:
        instance = MPV(
            start_mpv=True,
            ipc_socket=endpoint,
            mpv_location=str(paths.bin_path("mpv")),
            vo="null",
            ao="null",
            idle=True,
        )
        instance.volume = 42
        readback = int(float(instance.volume))
        if readback != 42:
            report.add(FAIL, "mpv IPC misbehaving", f"set volume 42, read back {readback}")
        else:
            report.add(OK, "mpv IPC", f"endpoint: {endpoint}")
    except Exception as e:
        report.add(FAIL, "mpv IPC failed", str(e))
    finally:
        if instance is not None:
            try:
                instance.terminate()
            except Exception:
                pass
        platform_compat.terminate_ipc_endpoint(endpoint)


CHECKS = (
    _check_layout,
    _check_binaries,
    _check_state_bus,
    _check_stations,
    _check_guide,
    _check_qt,
    _check_mpv_ipc,
)


def _stdout_is_visible() -> bool:
    """False inside a windowed (no-console) build, where print() goes nowhere."""
    stream = sys.stdout
    if stream is None:
        return False
    return type(stream).__name__ != "NullWriter"


def run() -> int:
    report = Report()
    for check in CHECKS:
        try:
            check(report)
        except Exception as e:
            report.add(FAIL, f"{check.__name__} raised", repr(e))
    report.render()

    # Always keep a copy where the user (or a bug report) can find it.  The
    # Windows build has no console, so this file - opened in Notepad - *is*
    # the doctor's output there.
    try:
        from fs42 import paths

        target = paths.data("doctor-report.txt")
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(f"FieldStation42 doctor - {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            report.render(handle)
        if not _stdout_is_visible() and os.name == "nt":
            os.startfile(str(target))  # noqa: S606 - opens the report in the default text editor
    except Exception:
        pass
    return 1 if report.failures else 0


# ---------------------------------------------------------------- probes
#
# Each runs in its own process (see _run_probe) so a hard abort inside a GUI
# toolkit is a reportable result rather than the end of the doctor.

def _probe_tkinter() -> int:
    import tkinter

    root = tkinter.Tk()
    root.withdraw()
    root.destroy()
    print("tkinter ok")
    return 0


def _probe_qt() -> int:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    print(f"platform plugin: {app.platformName()}")
    return 0


def _probe_menu() -> int:
    """Construct the in-app menu window offscreen - proves Qt widgets and the
    menu's own imports all resolve inside a frozen build."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from fs42.menu.app import _build_window

    app = QApplication.instance() or QApplication([])
    window = _build_window()()
    rows = len(window.stack[-1].rows)
    print(f"menu ok ({rows} rows on Home)")
    return 0


def _probe_qtwebengine() -> int:
    import PySide6.QtWebEngineWidgets  # noqa: F401

    print("QtWebEngine available")
    return 0


PROBES = {
    "tkinter": _probe_tkinter,
    "qt": _probe_qt,
    "qtwebengine": _probe_qtwebengine,
    "menu": _probe_menu,
}


def run_probe(name) -> int:
    probe = PROBES.get(name)
    if probe is None:
        print(f"Unknown probe: {name}", file=sys.stderr)
        return 2
    try:
        return probe()
    except Exception as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
