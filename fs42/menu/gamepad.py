"""Gamepad input without a dependency.

Turned on with ``"gamepad": true`` in main_config.json.  Runs as a thread in
the player process so a controller can open the menu when it is closed, not
only drive it once open.

* Windows: XInput via ctypes (``xinput1_4.dll``, falling back to older
  versions), polled.
* Linux: the joystick API - 16-byte ``struct js_event`` records read from
  ``/dev/input/js0`` - which needs no library and no udev rules beyond what a
  distribution already ships for controllers.

Buttons map to the same actions the keyboard and the phone remote produce:

    D-pad / left stick   up, down, left, right
    A (bottom)           select
    B (right)            back
    Start                menu (opens or closes it)
    LB / RB              channel down / up, when the menu is closed
"""

import logging
import os
import struct
import threading
import time

from fs42 import ipc
from fs42.menu.input import Action

_l = logging.getLogger("GAMEPAD")

POLL_SECONDS = 0.02
REPEAT_DELAY = 0.45
REPEAT_RATE = 0.12
STICK_THRESHOLD = 0.6


def _menu_open() -> bool:
    try:
        return bool(ipc.get_state(ipc.KEY_MENU_OPEN))
    except Exception:
        return False


def emit(action: str):
    """Deliver one action to whoever should have it."""
    try:
        if action == Action.MENU.value:
            if _menu_open():
                ipc.push(ipc.TOPIC_MENU_INPUT, {"action": Action.MENU.value})
            else:
                ipc.push(ipc.TOPIC_PLAYER_CMD, {"command": "menu"})
        elif action in ("channel_up", "channel_down"):
            if not _menu_open():
                ipc.push(ipc.TOPIC_CHANNEL, {"command": action.split("_")[1]})
        elif _menu_open():
            ipc.push(ipc.TOPIC_MENU_INPUT, {"action": action})
    except Exception as e:
        _l.debug("gamepad emit failed: %s", e)


class _Repeater:
    """Turn held buttons into repeated actions, like a keyboard does."""

    def __init__(self):
        self.held = {}

    def update(self, pressed: set, now: float):
        for action in list(self.held):
            if action not in pressed:
                del self.held[action]
        for action in pressed:
            if action not in self.held:
                self.held[action] = now + REPEAT_DELAY
                emit(action)
            elif now >= self.held[action] and action in (
                Action.UP.value, Action.DOWN.value, Action.LEFT.value, Action.RIGHT.value
            ):
                self.held[action] = now + REPEAT_RATE
                emit(action)


# ---------------------------------------------------------------- Windows

class _XInputBackend:
    BUTTONS = {
        0x0001: Action.UP.value,      # DPAD_UP
        0x0002: Action.DOWN.value,    # DPAD_DOWN
        0x0004: Action.LEFT.value,    # DPAD_LEFT
        0x0008: Action.RIGHT.value,   # DPAD_RIGHT
        0x0010: Action.MENU.value,    # START
        0x0020: Action.BACK.value,    # BACK
        0x0100: "channel_down",       # LEFT_SHOULDER
        0x0200: "channel_up",         # RIGHT_SHOULDER
        0x1000: Action.SELECT.value,  # A
        0x2000: Action.BACK.value,    # B
    }

    def __init__(self):
        import ctypes

        self.ctypes = ctypes
        self.dll = None
        for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
            try:
                self.dll = ctypes.WinDLL(name)
                break
            except OSError:
                continue
        if self.dll is None:
            raise RuntimeError("XInput is not available")

        class Gamepad(ctypes.Structure):
            _fields_ = [
                ("wButtons", ctypes.c_ushort), ("bLeftTrigger", ctypes.c_ubyte),
                ("bRightTrigger", ctypes.c_ubyte), ("sThumbLX", ctypes.c_short),
                ("sThumbLY", ctypes.c_short), ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short),
            ]

        class State(ctypes.Structure):
            _fields_ = [("dwPacketNumber", ctypes.c_uint), ("Gamepad", Gamepad)]

        self.State = State

    def poll(self) -> set:
        pressed = set()
        for index in range(4):
            state = self.State()
            if self.dll.XInputGetState(index, self.ctypes.byref(state)) != 0:
                continue
            pad = state.Gamepad
            for mask, action in self.BUTTONS.items():
                if pad.wButtons & mask:
                    pressed.add(action)
            x, y = pad.sThumbLX / 32767.0, pad.sThumbLY / 32767.0
            if y > STICK_THRESHOLD:
                pressed.add(Action.UP.value)
            elif y < -STICK_THRESHOLD:
                pressed.add(Action.DOWN.value)
            if x > STICK_THRESHOLD:
                pressed.add(Action.RIGHT.value)
            elif x < -STICK_THRESHOLD:
                pressed.add(Action.LEFT.value)
        return pressed


# ------------------------------------------------------------------ Linux

class _JoystickBackend:
    """Linux joystick API.  Button numbers follow the common xpad layout."""

    JS_EVENT_BUTTON = 0x01
    JS_EVENT_AXIS = 0x02
    JS_EVENT_INIT = 0x80
    BUTTONS = {
        0: Action.SELECT.value,   # A
        1: Action.BACK.value,     # B
        4: "channel_down",        # LB
        5: "channel_up",          # RB
        6: Action.BACK.value,     # Back/Select
        7: Action.MENU.value,     # Start
    }
    AXES = {
        0: (Action.LEFT.value, Action.RIGHT.value),   # left stick X
        1: (Action.UP.value, Action.DOWN.value),      # left stick Y
        6: (Action.LEFT.value, Action.RIGHT.value),   # D-pad X
        7: (Action.UP.value, Action.DOWN.value),      # D-pad Y
    }

    def __init__(self, device=None):
        import glob

        candidates = [device] if device else sorted(glob.glob("/dev/input/js*"))
        self.fd = None
        for path in candidates:
            try:
                self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                self.path = path
                break
            except OSError:
                continue
        if self.fd is None:
            raise RuntimeError("No joystick device found under /dev/input/js*")
        self.pressed = set()

    def poll(self) -> set:
        while True:
            try:
                data = os.read(self.fd, 8)
            except BlockingIOError:
                break
            except OSError:
                self.pressed.clear()
                break
            if len(data) < 8:
                break
            _, value, kind, number = struct.unpack("IhBB", data)
            kind &= ~self.JS_EVENT_INIT
            if kind == self.JS_EVENT_BUTTON:
                action = self.BUTTONS.get(number)
                if action:
                    (self.pressed.add if value else self.pressed.discard)(action)
            elif kind == self.JS_EVENT_AXIS and number in self.AXES:
                negative, positive = self.AXES[number]
                self.pressed.discard(negative)
                self.pressed.discard(positive)
                if value < -32767 * STICK_THRESHOLD:
                    self.pressed.add(negative)
                elif value > 32767 * STICK_THRESHOLD:
                    self.pressed.add(positive)
        return set(self.pressed)

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


# ----------------------------------------------------------------- reader

class GamepadReader:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None
        self.backend = None

    def _open_backend(self):
        if os.name == "nt":
            return _XInputBackend()
        return _JoystickBackend()

    def start(self):
        self._thread = threading.Thread(target=self._run, name="fs42-gamepad", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        repeater = _Repeater()
        backend = None
        next_retry = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if backend is None:
                if now < next_retry:
                    time.sleep(0.5)
                    continue
                try:
                    backend = self._open_backend()
                    _l.info("Gamepad connected")
                except Exception as e:
                    _l.debug("No gamepad yet: %s", e)
                    next_retry = now + 5.0
                    continue
            try:
                repeater.update(backend.poll(), now)
            except Exception as e:
                _l.info("Gamepad disconnected: %s", e)
                try:
                    backend.close()
                except Exception:
                    pass
                backend = None
                next_retry = now + 2.0
            time.sleep(POLL_SECONDS)
        if backend is not None and hasattr(backend, "close"):
            backend.close()
