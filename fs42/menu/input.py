"""The menu's input vocabulary.

Every way of driving the menu - the keyboard (whichever window has focus),
the phone remote, a gamepad - reduces to one of these actions.  Sources that
are not the menu window itself push ``{"action": ...}`` onto
``ipc.TOPIC_MENU_INPUT``; the window drains that queue on a timer and treats
the result exactly like a local key press.
"""

import enum


class Action(str, enum.Enum):
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"
    SELECT = "select"
    BACK = "back"
    MENU = "menu"          # toggle the whole menu
    PAGE_UP = "page_up"
    PAGE_DOWN = "page_down"
    DELETE = "delete"      # backspace in a text field, "remove" elsewhere
    # DIGIT_0 .. DIGIT_9 are produced by digit(n) below.


def digit(n: int) -> str:
    return f"digit_{int(n)}"


def digit_value(action: str):
    """The integer for a digit action, or None."""
    if isinstance(action, str) and action.startswith("digit_"):
        try:
            return int(action[6:])
        except ValueError:
            return None
    return None


ALL_ACTIONS = {a.value for a in Action} | {digit(n) for n in range(10)}


def is_action(value) -> bool:
    return value in ALL_ACTIONS


# mpv key names (as used by bind_key_press) -> action.  Kept here so the player
# and the menu agree on the vocabulary without importing Qt.
MPV_KEY_ACTIONS = {
    "ESC": Action.BACK.value,
    "UP": Action.UP.value,
    "DOWN": Action.DOWN.value,
    "LEFT": Action.LEFT.value,
    "RIGHT": Action.RIGHT.value,
    "ENTER": Action.SELECT.value,
    "KP_ENTER": Action.SELECT.value,
    "SPACE": Action.SELECT.value,
    "BS": Action.DELETE.value,
    "PGUP": Action.PAGE_UP.value,
    "PGDWN": Action.PAGE_DOWN.value,
    "DEL": Action.DELETE.value,
    "MENU": Action.MENU.value,
}
for _n in range(10):
    MPV_KEY_ACTIONS[str(_n)] = digit(_n)
    MPV_KEY_ACTIONS[f"KP{_n}"] = digit(_n)


def qt_key_to_action(key, text=""):
    """Map a Qt key code to an action, or None.

    Imported lazily so this module stays importable without PySide6 (the
    player process uses the mpv table above and never loads Qt).
    """
    from PySide6.QtCore import Qt

    table = {
        Qt.Key_Up: Action.UP.value,
        Qt.Key_Down: Action.DOWN.value,
        Qt.Key_Left: Action.LEFT.value,
        Qt.Key_Right: Action.RIGHT.value,
        Qt.Key_Return: Action.SELECT.value,
        Qt.Key_Enter: Action.SELECT.value,
        Qt.Key_Space: Action.SELECT.value,
        Qt.Key_Backspace: Action.DELETE.value,
        Qt.Key_Delete: Action.DELETE.value,
        Qt.Key_Escape: Action.BACK.value,
        Qt.Key_PageUp: Action.PAGE_UP.value,
        Qt.Key_PageDown: Action.PAGE_DOWN.value,
        Qt.Key_Menu: Action.MENU.value,
        Qt.Key_F1: Action.MENU.value,
    }
    if key in table:
        return table[key]
    if Qt.Key_0 <= key <= Qt.Key_9:
        return digit(key - Qt.Key_0)
    return None
