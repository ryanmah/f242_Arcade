"""Gamepad input without a dependency.

Turned on with ``"gamepad": true`` in main_config.json (the menu's *Add
Input* pages do that for you).  Runs as a thread in the player process so a
controller can open the menu when it is closed, not only drive it once open.

* Windows: XInput via ctypes (``xinput1_4.dll``, falling back to older
  versions), polled.  Every XInput pad looks the same, so they share one
  layout ("XInput controller").
* Linux: the joystick API - 8-byte ``struct js_event`` records read from
  every ``/dev/input/js*`` - which needs no library and no udev rules beyond
  what a distribution already ships for controllers.  Each device reports
  its own name, so an arcade stick and an Xbox pad can have different maps.

A backend reports *presses* as ``(device, control)`` pairs - a device name
plus a stable control name such as ``a``, ``dpad_up`` or ``button_3`` - and
a per-device mapping turns controls into the same actions the keyboard and
the phone remote produce.  The default mapping is the usual Xbox layout:

    D-pad / left stick   up, down, left, right
    A (bottom)           select
    B (right)            back
    Start                menu (opens or closes it)
    LB / RB              down / up as well

With the menu closed, up and down change the channel; with it open they
move the cursor.  So one pair of buttons does both jobs.

Controllers set up on the menu are stored in main_config.json as::

    "controllers": [{"name": "Arcade stick", "device": "DragonRise Inc. ...",
                     "map": {"button_3": "menu", ...}}]

A controller whose ``device`` is ``"*"`` applies to any device without an
entry of its own.  The older flat ``gamepad_map`` is read as such a
catch-all.
"""

import json
import logging
import os
import struct
import threading
import time

from fs42 import ipc, paths
from fs42.menu.input import Action

_l = logging.getLogger("GAMEPAD")

POLL_SECONDS = 0.02
REPEAT_DELAY = 0.45
REPEAT_RATE = 0.12
STICK_THRESHOLD = 0.6
ANY_DEVICE = "*"

# Everything a controller button can be assigned to, in the order the Add
# Input page walks through them.
FUNCTIONS = [
    (Action.UP.value, "Up / channel up"),
    (Action.DOWN.value, "Down / channel down"),
    (Action.LEFT.value, "Left / back"),
    (Action.RIGHT.value, "Right / select"),
    (Action.SELECT.value, "Select"),
    (Action.BACK.value, "Back"),
    (Action.MENU.value, "Open / close menu"),
]
FUNCTION_NAMES = [name for name, _ in FUNCTIONS]
# Maps saved by earlier versions had separate channel functions.
LEGACY_FUNCTIONS = {"channel_up": Action.UP.value, "channel_down": Action.DOWN.value}
REPEATING = {Action.UP.value, Action.DOWN.value, Action.LEFT.value, Action.RIGHT.value}


def _menu_open() -> bool:
    try:
        return bool(ipc.get_state(ipc.KEY_MENU_OPEN))
    except Exception:
        return False


_last_channel_change = 0.0
CHANNEL_REPEAT_GAP = 0.4


def emit(action: str):
    """Deliver one action to whoever should have it."""
    global _last_channel_change
    try:
        if ipc.get_state(ipc.KEY_INPUT_CAPTURE):
            return      # the Add Input page is listening to the raw buttons
        if action == Action.MENU.value:
            if _menu_open():
                ipc.push(ipc.TOPIC_MENU_INPUT, {"action": Action.MENU.value})
            else:
                ipc.push(ipc.TOPIC_PLAYER_CMD, {"command": "menu"})
        elif _menu_open():
            ipc.push(ipc.TOPIC_MENU_INPUT, {"action": action})
        elif action in (Action.UP.value, Action.DOWN.value):
            # A held D-pad repeats quickly; a channel change is not quick.
            now = time.monotonic()
            if now - _last_channel_change < CHANNEL_REPEAT_GAP:
                return
            _last_channel_change = now
            ipc.push(ipc.TOPIC_CHANNEL, {"command": "up" if action == Action.UP.value else "down"})
    except Exception as e:
        _l.debug("gamepad emit failed: %s", e)


class _Repeater:
    """Turn held presses into repeated actions, like a keyboard does.

    ``mapping_for(device)`` returns the control -> function map for a device.
    """

    def __init__(self, mapping_for, sink=emit):
        self.mapping_for = mapping_for
        self.sink = sink
        self.held = {}

    def update(self, pressed: set, now: float):
        for press in list(self.held):
            if press not in pressed:
                del self.held[press]
        for press in pressed:
            device, control = press
            action = self.mapping_for(device).get(control)
            if press not in self.held:
                self.held[press] = now + REPEAT_DELAY
                if action:
                    self.sink(action)
            elif action in REPEATING and now >= self.held[press]:
                self.held[press] = now + REPEAT_RATE
                self.sink(action)


# ---------------------------------------------------------------- Windows

class _XInputBackend:
    NAME = "xinput"
    DEVICE = "XInput controller"
    BUTTONS = {
        0x0001: "dpad_up", 0x0002: "dpad_down", 0x0004: "dpad_left", 0x0008: "dpad_right",
        0x0010: "start", 0x0020: "back", 0x0040: "left_thumb", 0x0080: "right_thumb",
        0x0100: "lb", 0x0200: "rb", 0x1000: "a", 0x2000: "b", 0x4000: "x", 0x8000: "y",
    }
    DEFAULT_MAP = {
        "dpad_up": Action.UP.value, "dpad_down": Action.DOWN.value,
        "dpad_left": Action.LEFT.value, "dpad_right": Action.RIGHT.value,
        "lstick_up": Action.UP.value, "lstick_down": Action.DOWN.value,
        "lstick_left": Action.LEFT.value, "lstick_right": Action.RIGHT.value,
        "start": Action.MENU.value, "back": Action.BACK.value,
        "lb": Action.DOWN.value, "rb": Action.UP.value,
        "a": Action.SELECT.value, "b": Action.BACK.value,
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
        self.connected = 0

    def devices(self) -> list:
        return [self.DEVICE] if self.connected else []

    def poll(self) -> set:
        pressed = set()
        connected = 0
        for index in range(4):
            state = self.State()
            if self.dll.XInputGetState(index, self.ctypes.byref(state)) != 0:
                continue
            connected += 1
            pad = state.Gamepad
            for mask, control in self.BUTTONS.items():
                if pad.wButtons & mask:
                    pressed.add((self.DEVICE, control))
            for prefix, x, y in (("lstick", pad.sThumbLX, pad.sThumbLY), ("rstick", pad.sThumbRX, pad.sThumbRY)):
                x, y = x / 32767.0, y / 32767.0
                if y > STICK_THRESHOLD:
                    pressed.add((self.DEVICE, f"{prefix}_up"))
                elif y < -STICK_THRESHOLD:
                    pressed.add((self.DEVICE, f"{prefix}_down"))
                if x > STICK_THRESHOLD:
                    pressed.add((self.DEVICE, f"{prefix}_right"))
                elif x < -STICK_THRESHOLD:
                    pressed.add((self.DEVICE, f"{prefix}_left"))
            if pad.bLeftTrigger > 128:
                pressed.add((self.DEVICE, "lt"))
            if pad.bRightTrigger > 128:
                pressed.add((self.DEVICE, "rt"))
        self.connected = connected
        return pressed

    def close(self):
        pass


# ------------------------------------------------------------------ Linux

def _joystick_name(fd, fallback) -> str:
    """The kernel's name for a joystick device (JSIOCGNAME)."""
    try:
        import fcntl

        buf = bytearray(128)
        # _IOC(_IOC_READ, 'j', 0x13, len(buf))
        request = 0x80000000 | (len(buf) << 16) | (ord("j") << 8) | 0x13
        fcntl.ioctl(fd, request, buf)
        name = buf.split(b"\0", 1)[0].decode("utf-8", "replace").strip()
        return name or fallback
    except Exception:
        return fallback


class _JoystickBackend:
    """Linux joystick API.  Controls are ``button_N`` and ``axis_N_neg/pos``.

    Every ``/dev/input/js*`` is opened, so several controllers work at once
    and each press is tagged with its device's name.  New devices are picked
    up on the next rescan.
    """

    NAME = "joystick"
    JS_EVENT_BUTTON = 0x01
    JS_EVENT_AXIS = 0x02
    JS_EVENT_INIT = 0x80
    RESCAN_SECONDS = 3.0
    # The common xpad layout.
    DEFAULT_MAP = {
        "button_0": Action.SELECT.value,   # A
        "button_1": Action.BACK.value,     # B
        "button_4": Action.DOWN.value,     # LB
        "button_5": Action.UP.value,       # RB
        "button_6": Action.BACK.value,     # Back/Select
        "button_7": Action.MENU.value,     # Start
        "axis_0_neg": Action.LEFT.value, "axis_0_pos": Action.RIGHT.value,   # left stick X
        "axis_1_neg": Action.UP.value, "axis_1_pos": Action.DOWN.value,      # left stick Y
        "axis_6_neg": Action.LEFT.value, "axis_6_pos": Action.RIGHT.value,   # D-pad X
        "axis_7_neg": Action.UP.value, "axis_7_pos": Action.DOWN.value,      # D-pad Y
    }

    def __init__(self, device=None):
        self.only = device
        self.fds = {}           # path -> fd
        self.names = {}         # path -> device name
        self.pressed = {}       # path -> set of controls
        self.next_scan = 0.0
        self.scan()
        if not self.fds:
            raise RuntimeError("No joystick device found under /dev/input/js*")

    def scan(self):
        import glob

        self.next_scan = time.monotonic() + self.RESCAN_SECONDS
        candidates = [self.only] if self.only else sorted(glob.glob("/dev/input/js*"))
        for path in candidates:
            if path in self.fds:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue
            self.fds[path] = fd
            self.names[path] = _joystick_name(fd, os.path.basename(path))
            self.pressed[path] = set()
            _l.info("Gamepad connected: %s (%s)", self.names[path], path)

    def devices(self) -> list:
        return [self.names[p] for p in self.fds]

    def _drop(self, path):
        try:
            os.close(self.fds.pop(path))
        except OSError:
            pass
        _l.info("Gamepad disconnected: %s", self.names.pop(path, path))
        self.pressed.pop(path, None)

    def poll(self) -> set:
        if time.monotonic() >= self.next_scan:
            self.scan()
        for path in list(self.fds):
            fd = self.fds[path]
            pressed = self.pressed[path]
            while True:
                try:
                    data = os.read(fd, 8)
                except BlockingIOError:
                    break
                except OSError:
                    self._drop(path)
                    break
                if len(data) < 8:
                    break
                _, value, kind, number = struct.unpack("IhBB", data)
                initial = bool(kind & self.JS_EVENT_INIT)
                kind &= ~self.JS_EVENT_INIT
                if kind == self.JS_EVENT_BUTTON:
                    control = f"button_{number}"
                    (pressed.add if value and not initial else pressed.discard)(control)
                elif kind == self.JS_EVENT_AXIS:
                    negative, positive = f"axis_{number}_neg", f"axis_{number}_pos"
                    pressed.discard(negative)
                    pressed.discard(positive)
                    if initial:
                        continue        # a resting axis reported at open time
                    if value < -32767 * STICK_THRESHOLD:
                        pressed.add(negative)
                    elif value > 32767 * STICK_THRESHOLD:
                        pressed.add(positive)
        return {(self.names[p], c) for p in self.fds for c in self.pressed[p]}

    def close(self):
        for path in list(self.fds):
            try:
                os.close(self.fds.pop(path))
            except OSError:
                pass
        self.names.clear()
        self.pressed.clear()


# ---------------------------------------------------------------- mapping

def open_backend():
    if os.name == "nt":
        return _XInputBackend()
    return _JoystickBackend()


def default_mapping() -> dict:
    return dict(_XInputBackend.DEFAULT_MAP if os.name == "nt" else _JoystickBackend.DEFAULT_MAP)


def describe_control(control: str) -> str:
    """A label for a control name, for the Add Input page."""
    names = {
        "a": "A", "b": "B", "x": "X", "y": "Y", "lb": "LB", "rb": "RB", "lt": "LT", "rt": "RT",
        "start": "START", "back": "BACK", "left_thumb": "L-STICK CLICK", "right_thumb": "R-STICK CLICK",
    }
    if control in names:
        return names[control]
    if control.startswith("dpad_"):
        return "D-PAD " + control[5:].upper()
    if control.startswith("lstick_") or control.startswith("rstick_"):
        return ("L" if control[0] == "l" else "R") + "-STICK " + control[7:].upper()
    if control.startswith("button_"):
        return "BUTTON " + control[7:]
    if control.startswith("axis_"):
        _, number, sign = control.split("_")
        return f"AXIS {number} {'-' if sign == 'neg' else '+'}"
    return control.upper()


def apply_custom(mapping: dict, custom: dict) -> dict:
    """Lay a custom map over a default one.

    A custom map is authoritative for the controls it names *and* frees any
    default control that was pointing at a function the user has since put
    somewhere else, so one button never does two things.
    """
    mapping = dict(mapping)
    custom = {c: LEGACY_FUNCTIONS.get(a, a) for c, a in (custom or {}).items()}
    if custom:
        reassigned = {a for a in custom.values() if a in FUNCTION_NAMES}
        mapping = {c: a for c, a in mapping.items() if a not in reassigned}
        for control, action in custom.items():
            if action in FUNCTION_NAMES:
                mapping[control] = action
    return mapping


def controls_for(mapping: dict, function: str) -> list:
    return sorted(c for c, a in mapping.items() if a == function)


def _main_config_path():
    return paths.confs("main_config.json")


def _read_config() -> dict:
    try:
        with open(_main_config_path()) as f:
            config = json.load(f)
        return config if isinstance(config, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        _l.warning("Could not read main_config.json: %s", e)
        return {}


def _clean_controller(entry) -> dict:
    custom = {c: LEGACY_FUNCTIONS.get(a, a) for c, a in (entry.get("map") or {}).items()}
    return {
        "name": str(entry.get("name") or entry.get("device") or "Controller"),
        "device": str(entry.get("device") or ANY_DEVICE),
        "map": {c: a for c, a in custom.items() if a in FUNCTION_NAMES},
    }


def load_controllers() -> list:
    """Controllers from main_config.json; an old flat map becomes a catch-all."""
    config = _read_config()
    controllers = [_clean_controller(e) for e in (config.get("controllers") or []) if isinstance(e, dict)]
    legacy = config.get("gamepad_map") or {}
    if legacy and not controllers:
        controllers.append(_clean_controller({"name": "Controller", "device": ANY_DEVICE, "map": legacy}))
    return controllers


def save_controllers(controllers: list):
    """Persist the controllers and switch the gamepad on in main_config.json."""
    path = _main_config_path()
    config = _read_config()
    config["gamepad"] = True
    config["controllers"] = [_clean_controller(e) for e in controllers]
    config.pop("gamepad_map", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    from fs42.platform_compat import atomic_write_text

    atomic_write_text(path, json.dumps(config, indent=4))
    _l.info("Saved %d controller(s) to %s", len(config["controllers"]), path)


def standard_layout(device: str) -> bool:
    """Pads that always report the standard (xpad) button numbering.

    Steam Input's virtual controller is one: in Steam Deck Game Mode, and
    whenever Steam Input is on for the shortcut, the app sees
    "Microsoft X-Box 360 pad N" (or "Steam Virtual Gamepad") instead of the
    physical controller, laid out like an Xbox pad whatever the hardware.
    """
    name = (device or "").lower()
    return name.startswith("microsoft x-box 360 pad") or "steam virtual gamepad" in name


def controller_for(device: str, controllers: list):
    """The entry for a device: an exact match first, then a catch-all.

    A catch-all map was learned on some real controller's numbering; laid
    over a standard-layout pad (Steam's virtual one in Game Mode) it would
    scramble buttons that the default map already gets right, so those only
    use an entry made for them.
    """
    for entry in controllers:
        if entry.get("device") == device:
            return entry
    if standard_layout(device):
        return None
    for entry in controllers:
        if entry.get("device") == ANY_DEVICE:
            return entry
    return None


class MappingTable:
    """Per-device maps, built once from the saved controllers."""

    def __init__(self, controllers=None):
        self.controllers = controllers if controllers is not None else load_controllers()
        self._cache = {}

    def for_device(self, device: str) -> dict:
        if device not in self._cache:
            entry = controller_for(device, self.controllers)
            self._cache[device] = apply_custom(default_mapping(), entry["map"] if entry else {})
        return self._cache[device]


def load_mapping(device: str = ANY_DEVICE) -> dict:
    """The effective map for one device (the default plus its saved entry)."""
    return MappingTable().for_device(device)


# ----------------------------------------------------------------- reader

class GamepadReader:
    """Polls the controllers on a thread.

    By default the mapped actions go to the state bus (``emit``).  With
    ``raw_sink`` set, every newly pressed ``(device, control)`` is handed to
    that callable instead and nothing reaches the bus - the Add Input page
    uses this to learn which button the user pressed.
    """

    def __init__(self, mapping=None, raw_sink=None):
        self._stop = threading.Event()
        self._thread = None
        self.backend = None
        self.table = mapping if isinstance(mapping, MappingTable) else MappingTable(mapping)
        self.raw_sink = raw_sink
        self.connected = False
        self.devices = []

    def start(self):
        self._thread = threading.Thread(target=self._run, name="fs42-gamepad", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)

    def _run(self):
        repeater = _Repeater(self.table.for_device)
        backend = None
        previous = set()
        next_retry = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if backend is None:
                if now < next_retry:
                    time.sleep(0.25)
                    continue
                try:
                    backend = open_backend()
                    self.backend = backend
                    previous = set()
                except Exception as e:
                    _l.debug("No gamepad yet: %s", e)
                    next_retry = now + 2.0
                    continue
            try:
                pressed = backend.poll()
                self.devices = backend.devices()
                self.connected = bool(self.devices)
                if self.raw_sink is not None:
                    for press in sorted(pressed - previous):
                        self.raw_sink(*press)
                    previous = pressed
                else:
                    repeater.update(pressed, now)
            except Exception as e:
                _l.info("Gamepad reader restarting: %s", e)
                try:
                    backend.close()
                except Exception:
                    pass
                backend = None
                self.backend = None
                self.connected = False
                self.devices = []
                next_retry = now + 2.0
            time.sleep(POLL_SECONDS)
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass
        self.connected = False
