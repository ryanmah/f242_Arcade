"""Steam Deck Game Mode (gamescope) support for FieldStation42's own windows.

gamescope shows one window of a game at a time - the one it gives focus
to.  The menu, the on-screen overlays and the CRT effects are separate,
see-through windows on top of the video, so in Game Mode they would either
never be seen or would replace the picture.  gamescope does composite
"external overlay" windows over the focused one (that is how MangoHud's
mangoapp draws), and a window becomes one by carrying the
``GAMESCOPE_EXTERNAL_OVERLAY`` property.  Nothing here does anything
outside gamescope.
"""

import ctypes
import ctypes.util
import logging
import os

_l = logging.getLogger("GAMESCOPE")
_x11 = None


def active() -> bool:
    return bool(os.environ.get("GAMESCOPE_WAYLAND_DISPLAY")) or os.environ.get("XDG_CURRENT_DESKTOP", "").lower() == "gamescope"


def _lib():
    global _x11
    if _x11 is None:
        x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XInternAtom.restype = ctypes.c_ulong
        x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x11.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        _x11 = x11
    return _x11


def set_cardinal(window_id: int, name: str, value: int = 1) -> bool:
    """Set a 32-bit CARDINAL property on an X11 window."""
    try:
        x11 = _lib()
        display = x11.XOpenDisplay(None)
        if not display:
            return False
        try:
            atom = x11.XInternAtom(display, name.encode(), 0)
            data = (ctypes.c_ulong * 1)(value)   # format 32 is passed as longs
            XA_CARDINAL, PROP_MODE_REPLACE = 6, 0
            x11.XChangeProperty(display, window_id, atom, XA_CARDINAL, 32, PROP_MODE_REPLACE,
                                ctypes.cast(data, ctypes.c_void_p), 1)
            x11.XFlush(display)
            return True
        finally:
            x11.XCloseDisplay(display)
    except Exception as e:
        _l.debug("Could not set %s: %s", name, e)
        return False


def mark_overlay(widget) -> bool:
    """Make a Qt top-level window draw over the video in Game Mode."""
    if not active():
        return False
    try:
        window_id = int(widget.winId())   # creates the native window if needed
    except Exception as e:
        _l.debug("No native window to mark: %s", e)
        return False
    ok = set_cardinal(window_id, "GAMESCOPE_EXTERNAL_OVERLAY", 1)
    if ok:
        _l.info("Marked %s as a gamescope overlay", type(widget).__name__)
    return ok
