"""CRT effects over windows that are not mpv - today, the guide channel.

The guide is a tkinter window, which cannot draw a translucent layer over
itself.  Instead a small Qt process keeps one full-screen, click-through,
see-through window on top of it that paints the same scanlines and noise
as the menu (``fs42.menu.effects.EffectsOverlay``).

The player drives it through a queue:

    ("show", values)    appear on top with these settings
    ("values", values)  change the settings (shown or not)
    ("hide",)           disappear
    ("exit",)           quit the process
"""

import logging
import os
import sys

_l = logging.getLogger("VFX")

RAISE_MS = 500


def compositing_available() -> bool:
    """Whether a see-through window really is see-through on this desktop.

    Always on Windows and under Wayland.  On a bare X server it needs a
    compositor; without one a "transparent" window is black, which would
    blank the guide, so the overlay is not used there.
    """
    if os.environ.get("FS42_FORCE_OVERLAY"):
        return True
    if os.name == "nt" or sys.platform == "darwin":
        return True
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    display_name = os.environ.get("DISPLAY")
    if not display_name:
        return False
    try:
        import ctypes
        import ctypes.util

        name = ctypes.util.find_library("X11") or "libX11.so.6"
        x11 = ctypes.CDLL(name)
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XInternAtom.restype = ctypes.c_ulong
        x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x11.XGetSelectionOwner.restype = ctypes.c_ulong
        x11.XGetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x11.XDefaultScreen.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        display = x11.XOpenDisplay(display_name.encode())
        if not display:
            return False
        try:
            screen = x11.XDefaultScreen(display)
            atom = x11.XInternAtom(display, f"_NET_WM_CM_S{screen}".encode(), 0)
            return bool(x11.XGetSelectionOwner(display, atom))
        finally:
            x11.XCloseDisplay(display)
    except Exception as e:
        _l.debug("compositor check failed: %s", e)
        return False


def run_overlay(queue):
    """Process body."""
    from queue import Empty

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication

    from fs42.menu.effects import EffectsOverlay

    app = QApplication.instance() or QApplication(sys.argv)
    window = EffectsOverlay(None)
    window.setWindowTitle("FieldStation42 effects")
    window.setWindowFlags(
        Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus
    )
    window.setAttribute(Qt.WA_TranslucentBackground)
    window.setAttribute(Qt.WA_ShowWithoutActivating)
    window.setGeometry(QApplication.primaryScreen().geometry())

    def keep_on_top():
        if window.isVisible():
            window.raise_()

    raiser = QTimer()
    raiser.timeout.connect(keep_on_top)
    raiser.start(RAISE_MS)

    def poll():
        while True:
            try:
                message = queue.get_nowait()
            except Empty:
                return
            except Exception:
                app.quit()
                return
            kind = message[0] if message else None
            if kind == "show":
                window.apply(message[1])
                window.setGeometry(QApplication.primaryScreen().geometry())
                window.show()
                window.raise_()
            elif kind == "values":
                window.apply(message[1])
            elif kind == "hide":
                window.hide()
            elif kind == "exit":
                app.quit()
                return

    poller = QTimer()
    poller.timeout.connect(poll)
    poller.start(50)
    app.exec()


def _overlay_entry(queue):
    """Module-level process target (closures cannot be pickled under spawn)."""
    try:
        run_overlay(queue)
    except Exception as e:
        _l.warning("Effects overlay stopped: %s", e)
