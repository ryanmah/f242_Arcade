"""Pages of the in-app menu.

Every page is a list of rows drawn by ``ListPage``: a title, an optional value
on the right, and an action.  Up/Down move the cursor, Select activates, Back
pops the page.  That single interaction model is what makes the menu usable
from a keyboard, the phone remote and a gamepad alike.
"""

import logging
import os
import socket
import threading
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QRect, QRectF, Qt, QThread, Signal, QTimer
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QLineEdit, QSpinBox, QWidget, QFileDialog

from fs42 import ipc, paths
from fs42.menu import theme
from fs42.menu.input import Action, digit_value

_l = logging.getLogger("MENU")


@dataclass
class Row:
    title: str
    value: str = ""
    action: object = None          # callable() -> None, or None for info rows
    enabled: bool = True
    tone: str = "normal"           # normal | muted | accent | good | warn | bad
    checked: object = None         # None, True or False -> draws a checkbox
    data: object = None


# Footer legends, VCR style: one "WHAT:HOW" line each.
HINT_LIST = "SELECT:▲ ▼ KEY\nSET   :► KEY\nEND   :◄ KEY"
HINT_CONFIRM = "SELECT:▲ ▼ KEY\nSET   :► KEY"
HINT_CONTINUE = "SET   :► KEY"
HINT_TYPE = "TYPE  :KEYBOARD\nSELECT:▲ ▼ KEY\nSET   :► KEY"


class ListPage(QWidget):
    """A vertically scrolling list of rows, painted directly."""

    title = ""
    subtitle = ""
    hint = HINT_LIST

    def __init__(self, window):
        super().__init__(window.panel)
        self.window = window
        self.scale = window.scale * theme.TEXT_SCALE
        self.rows = []
        self.cursor = 0
        self.scroll = 0
        self.message = ""
        self.message_tone = "muted"
        self.setFocusPolicy(Qt.NoFocus)
        self.refresh()

    # ------------------------------------------------------------ overrides

    def build_rows(self):
        return []

    def on_show(self):
        """Called each time the page becomes the top of the stack."""
        self.refresh()

    # ------------------------------------------------------------- helpers

    def refresh(self):
        try:
            self.rows = self.build_rows() or []
        except Exception as e:
            _l.exception(e)
            self.rows = [Row(f"Could not load: {e}", tone="bad")]
        self.cursor = min(self.cursor, max(0, len(self.rows) - 1))
        self._skip_disabled(+1)
        self.update()
        self.publish()

    def publish(self):
        """Mirror this page onto the state bus for the phone remote and tests."""
        try:
            ipc.set_state(ipc.KEY_MENU_PAGE, {
                "page": type(self).__name__,
                "title": self.title,
                "subtitle": self.subtitle,
                "cursor": self.cursor,
                "message": self.message,
                "rows": [
                    {"title": r.title, "value": r.value, "enabled": bool(r.enabled and r.action), "checked": r.checked}
                    for r in self.rows
                ],
            })
        except Exception:
            pass

    def say(self, text, tone="muted"):
        self.message = text
        self.message_tone = tone
        self.update()
        self.publish()

    def _skip_disabled(self, direction):
        if not self.rows:
            return
        for _ in range(len(self.rows)):
            row = self.rows[self.cursor]
            if row.enabled and row.action is not None:
                return
            self.cursor = (self.cursor + direction) % len(self.rows)

    def move(self, delta):
        if not self.rows:
            return
        self.cursor = (self.cursor + delta) % len(self.rows)
        self._skip_disabled(1 if delta > 0 else -1)
        self.update()
        self.publish()

    def current(self):
        return self.rows[self.cursor] if self.rows else None

    def handle(self, action) -> bool:
        """Return True when the action was consumed."""
        if action == Action.UP.value:
            self.move(-1)
        elif action == Action.DOWN.value:
            self.move(+1)
        elif action == Action.PAGE_UP.value:
            self.move(-6)
        elif action == Action.PAGE_DOWN.value:
            self.move(+6)
        elif action == Action.SELECT.value or action == Action.RIGHT.value:
            row = self.current()
            if row and row.enabled and row.action:
                try:
                    row.action()
                except Exception as e:
                    _l.exception(e)
                    self.say(str(e), "bad")
        elif action == Action.BACK.value or action == Action.LEFT.value:
            self.window.pop()
        else:
            return False
        return True

    # ------------------------------------------------------------- painting

    def row_height(self):
        return int(55 * self.scale)

    def _text(self, painter, rect, flags, text, colour):
        """Text with the drop shadow a VCR burns into the picture."""
        s = self.scale
        if theme.SHADOW.alpha():
            painter.setPen(theme.SHADOW)
            painter.drawText(rect.translated(int(3 * s), int(3 * s)), flags, text)
        painter.setPen(colour)
        painter.drawText(rect, flags, text)

    def _dashed_title(self, metrics, width):
        title = (self.title or "MENU").upper()
        dash = max(1, metrics.horizontalAdvance("-"))
        room = width - metrics.horizontalAdvance(f" {title} ")
        count = max(2, room // (2 * dash))
        return f"{'-' * count} {title} {'-' * count}"

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.TextAntialiasing, False)
        s = self.scale
        width = self.width()
        pad = int(36 * s)
        line = width - 2 * pad
        y = int(24 * s)

        painter.setFont(theme.font(30, scale=s))
        metrics = QFontMetrics(painter.font())
        self._text(painter, QRect(pad, y, line, int(44 * s)), Qt.AlignHCenter | Qt.AlignVCenter,
                   self._dashed_title(metrics, line), theme.TEXT)
        y += int(52 * s)
        if self.subtitle:
            painter.setFont(theme.font(20, scale=s))
            self._text(painter, QRect(pad, y, line, int(28 * s)), Qt.AlignLeft | Qt.AlignVCenter,
                       self.subtitle, theme.MUTED)
            y += int(34 * s)
        y += int(8 * s)

        hint_lines = [h for h in (self.hint or "").split("\n") if h]
        footer = int(16 * s) + int(30 * s) + len(hint_lines) * int(30 * s)
        list_top = y
        list_bottom = self.height() - footer
        rh = self.row_height()
        visible = max(1, (list_bottom - list_top) // rh)

        if self.cursor < self.scroll:
            self.scroll = self.cursor
        elif self.cursor >= self.scroll + visible:
            self.scroll = self.cursor - visible + 1

        painter.setFont(theme.font(26, scale=s))
        metrics = QFontMetrics(painter.font())
        indent = int(24 * s)
        for index in range(self.scroll, min(len(self.rows), self.scroll + visible)):
            row = self.rows[index]
            rect = QRect(pad, y, line, rh - int(6 * s))
            selected = index == self.cursor and row.action is not None and row.enabled
            if selected:
                painter.setPen(Qt.NoPen)
                if theme.ROW_SHADOW.alpha():
                    painter.setBrush(theme.ROW_SHADOW)
                    painter.drawRect(rect.translated(int(4 * s), int(4 * s)))
                painter.setBrush(theme.ROW_SELECTED)
                painter.drawRect(rect)

            colour = {
                "muted": theme.MUTED, "accent": theme.ACCENT, "good": theme.GOOD,
                "warn": theme.WARN, "bad": theme.BAD,
            }.get(row.tone, theme.TEXT)
            if not row.enabled:
                colour = theme.MUTED
            if selected:
                # Knocked out of the bar, whatever the tone.
                colour = theme.ROW_SELECTED_TEXT
            # A VCR only had capitals.
            title = row.title.upper()
            value = row.value.upper()
            if row.checked is not None:
                title = ("[X] " if row.checked else "[ ] ") + title
            value_width = metrics.horizontalAdvance(value) + int(24 * s) if value else 0
            title_rect = QRect(rect.left() + indent, rect.top(), rect.width() - indent - value_width - int(12 * s), rect.height())
            self._text(painter, title_rect, Qt.AlignLeft | Qt.AlignVCenter,
                       metrics.elidedText(title, Qt.ElideRight, title_rect.width()), colour)
            if value:
                self._text(painter, QRect(rect.left(), rect.top(), rect.width() - int(12 * s), rect.height()),
                           Qt.AlignRight | Qt.AlignVCenter, value,
                           colour if selected or row.tone != "normal" else theme.MUTED)
            y += rh

        if len(self.rows) > visible:
            painter.setFont(theme.font(18, scale=s))
            self._text(painter, QRect(pad, list_bottom - int(24 * s), line, int(24 * s)), Qt.AlignRight,
                       f"{self.cursor + 1}/{len(self.rows)}", theme.MUTED)

        painter.setFont(theme.font(22, scale=s))
        y = self.height() - footer + int(8 * s)
        if self.message:
            colour = {"good": theme.GOOD, "warn": theme.WARN, "bad": theme.BAD, "accent": theme.ACCENT}.get(self.message_tone, theme.MUTED)
            self._text(painter, QRect(pad, y, line, int(30 * s)), Qt.AlignLeft | Qt.AlignVCenter, self.message.upper(), colour)
        y += int(30 * s)
        for text in hint_lines:
            self._text(painter, QRect(pad, y, line, int(30 * s)), Qt.AlignLeft | Qt.AlignVCenter, text, theme.TEXT)
            y += int(30 * s)
        painter.end()


# ============================================================ data helpers

def _station_summary(station):
    from fs42.catalog_api import CatalogAPI
    from fs42.liquid_manager import LiquidManager

    parts = []
    if station.get("_has_catalog"):
        try:
            summary = CatalogAPI.get_summary(station)
            parts.append(f"{summary.get('entry_count', 0)} clips")
        except Exception:
            parts.append("no catalog")
    if station.get("_has_schedule"):
        try:
            summary = LiquidManager().get_summary_json(network_name=station["network_name"])
            end = summary.get("end")
            parts.append(f"sched to {end[:10]}" if end else "no schedule")
        except Exception:
            parts.append("no schedule")
    if station.get("hidden"):
        parts.append("hidden")
    return " · ".join(parts)


def _station_summaries() -> dict:
    """name -> "142 clips · sched to 2026-10-01", read with two SQL queries.

    The old per-station path loaded every catalog entry and every schedule
    block into memory just to count them, which took seconds.
    """
    import sqlite3

    from fs42.station_manager import StationManager

    manager = StationManager()
    manager.reload_if_changed()
    counts, ends = {}, {}
    db_path = manager.server_conf.get("db_path")
    if db_path and os.path.exists(db_path):
        connection = sqlite3.connect(db_path, timeout=5)
        try:
            try:
                counts = dict(connection.execute(
                    "SELECT station, COUNT(*) FROM catalog_entries GROUP BY station").fetchall())
            except sqlite3.Error:
                pass
            try:
                ends = dict(connection.execute(
                    "SELECT station, MAX(end_time) FROM liquid_blocks GROUP BY station").fetchall())
            except sqlite3.Error:
                pass
        finally:
            connection.close()
    out = {}
    for station in manager.stations:
        name = station["network_name"]
        parts = []
        if station.get("_has_catalog"):
            parts.append(f"{counts.get(name, 0)} clips" if name in counts else "no catalog")
        if station.get("_has_schedule"):
            end = ends.get(name)
            parts.append(f"sched to {str(end)[:10]}" if end else "no schedule")
        if station.get("hidden"):
            parts.append("hidden")
        out[name] = " · ".join(parts)
    return out


def _lan_ip():
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("10.255.255.255", 1))
        address = probe.getsockname()[0]
        probe.close()
        return address
    except Exception:
        return "localhost"


def send_player(command: dict):
    ipc.push(ipc.TOPIC_PLAYER_CMD, command)


# ================================================================== pages

def _read_main_config():
    import json

    try:
        with open(paths.confs("main_config.json")) as f:
            config = json.load(f)
        return config if isinstance(config, dict) else {}
    except Exception:
        return {}


def _save_main_config_key(key, value):
    import json

    from fs42.platform_compat import atomic_write_text

    config = _read_main_config()
    config[key] = value
    atomic_write_text(paths.confs("main_config.json"), json.dumps(config, indent=4))


def _fullscreen() -> bool:
    return bool(_read_main_config().get("fullscreen", True))


def _captions() -> bool:
    return bool(_read_main_config().get("captions", False))


def _web_port():
    from fs42.station_manager import StationManager

    return StationManager().server_conf.get("server_port", 4242)


class HomePage(ListPage):
    title = "Menu"

    def build_rows(self):
        status = ipc.get_status() or {}
        now = status.get("network_name") or "nothing"
        self.subtitle = f"NOW PLAYING: {now}"
        return [
            Row("Stations", action=lambda: self.window.push(StationsPage(self.window))),
            Row("Rebuild all catalogs", action=lambda: self.window.push(ProgressPage(self.window, "Rebuilding catalogs", lambda log: _jobs().rebuild_catalog("all", log), reload_stations=True))),
            Row("Add a week to all schedules", action=lambda: self.window.push(ProgressPage(self.window, "Adding a week", lambda log: _jobs().add_schedule_time("week", "all", log)))),
            Row("Open web portal", action=self._open_web_portal),
            Row("Remote Controls", action=lambda: self.window.push(ControllersPage(self.window))),
            Row("Video effects", action=lambda: self.window.push(VideoEffectsPage(self.window))),
            Row(f"View: {'Fullscreen' if _fullscreen() else 'Windowed'}", action=self._toggle_view),
            Row(f"Captions: {'On' if _captions() else 'Off'}", action=self._toggle_captions),
            Row("Exit menu", action=self.window.close_menu),
            Row("Shutdown FS42", action=lambda: self.window.push(ConfirmQuitPage(self.window))),
        ]

    def _toggle_view(self):
        """Switch the video between fullscreen and a window, and remember it."""
        fullscreen = not _fullscreen()
        try:
            _save_main_config_key("fullscreen", fullscreen)
        except Exception as e:
            self.say(f"COULD NOT SAVE: {e}", "bad")
            return
        send_player({"command": "view", "fullscreen": fullscreen})
        self.refresh()
        self.say("FULLSCREEN" if fullscreen else "WINDOWED", "good")

    def _toggle_captions(self):
        on = not _captions()
        try:
            _save_main_config_key("captions", on)
        except Exception as e:
            self.say(f"COULD NOT SAVE: {e}", "bad")
            return
        send_player({"command": "captions", "on": on})
        self.refresh()
        self.say("CAPTIONS ON" if on else "CAPTIONS OFF", "good")

    def _open_web_portal(self):
        """Open the web console in the default browser and get out of its way."""
        url = f"http://localhost:{_web_port()}"
        opened = False
        try:
            opened = webbrowser.open(url, new=2)
        except Exception as e:
            _l.warning("Could not open a browser: %s", e)
        if opened:
            # The menu (and the video) sit above everything; the browser is
            # only useful once the menu is gone.
            self.say(f"Opening {url}", "good")
            QTimer.singleShot(600, self.window.close_menu)
        else:
            self.say(f"No browser found - open http://{_lan_ip()}:{_web_port()} on any device", "warn")


class ConfirmQuitPage(ListPage):
    title = "Shutdown FS42"
    hint = HINT_CONFIRM

    def build_rows(self):
        self.subtitle = "STOP THE PLAYER, THE WEB CONSOLE AND THIS MENU?"
        return [
            Row("No - keep watching", action=self.window.pop),
            Row("Yes - close everything", action=self._quit),
        ]

    def _quit(self):
        self.say("Closing...", "warn")
        ipc.request_shutdown("menu")
        QTimer.singleShot(300, self.window.close_menu)


def _jobs():
    from fs42 import build_jobs

    return build_jobs


class StationsPage(ListPage):
    """The channel list.

    Opens at once with a LOADING line and fills in when the per-station
    numbers (clip counts, how far each schedule runs) have been read on a
    background thread - on a big setup that takes a moment, and the menu
    should never look frozen.
    """

    title = "Stations"
    subtitle = "Pick a station, or add one from a folder of media"

    def __init__(self, window):
        self._summaries = None          # name -> summary text, once loaded
        self._loading = False
        self._result = None
        self._lock = threading.Lock()
        super().__init__(window)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._collect)
        self._timer.start(100)

    def on_show(self):
        super().on_show()
        self._start_loading()

    def _start_loading(self):
        if self._loading:
            return
        self._loading = True

        def work():
            try:
                result = _station_summaries()
            except Exception as e:
                _l.exception(e)
                result = {}
            with self._lock:
                self._result = result

        threading.Thread(target=work, name="fs42-stations", daemon=True).start()

    def _collect(self):
        with self._lock:
            result, self._result = self._result, None
        if result is not None:
            self._summaries = result
            self._loading = False
            self.refresh()

    def build_rows(self):
        from fs42.station_manager import StationManager

        if self._summaries is None:
            self.subtitle = "LOADING STATIONS..."
            return [Row("Loading...", tone="muted")]
        self.subtitle = "Pick a station, or add one from a folder of media"
        playing = (ipc.get_status() or {}).get("network_name")
        rows = []
        for station in StationManager().stations:
            name = station["network_name"]
            marker = "▶ " if name == playing else ""
            rows.append(Row(
                f"{station['channel_number']:>3}   {marker}{name}",
                value=self._summaries.get(name, ""),
                action=lambda s=station: self.window.push(StationDetailPage(self.window, s["network_name"])),
                tone="accent" if name == playing else "normal",
                data=station,
            ))
        rows.append(Row("+  Add a station", action=lambda: self.window.push(FolderPage(self.window)), tone="good"))
        return rows


class StationDetailPage(ListPage):
    def __init__(self, window, network_name):
        self.network_name = network_name
        super().__init__(window)

    def _station(self):
        from fs42.station_manager import StationManager

        return StationManager().station_by_name(self.network_name)

    def build_rows(self):
        station = self._station()
        if station is None:
            self.title = self.network_name
            return [Row("This station no longer exists", tone="bad")]
        self.title = f"{station['channel_number']}  {station['network_name']}"
        self.subtitle = f"{station.get('network_type', 'standard')} · {station.get('content_dir', '')}"
        hidden = bool(station.get("hidden"))
        rows = [
            Row("Tune to this channel", action=lambda: (send_player({"command": "tune", "channel": station["channel_number"]}), self.window.close_menu())),
            Row("Show in channel list" if hidden else "Hide from channel list", checked=not hidden, action=self._toggle_hidden),
        ]
        if station.get("_has_catalog"):
            rows.append(Row("Rebuild catalog", value=_station_summary(station), action=lambda: self.window.push(ProgressPage(self.window, f"Rebuilding {self.network_name}", lambda log: _jobs().rebuild_catalog(self.network_name, log), reload_stations=True))))
        if station.get("_has_schedule"):
            rows.append(Row("Add a week to the schedule", action=lambda: self.window.push(ProgressPage(self.window, f"Scheduling {self.network_name}", lambda log: _jobs().add_schedule_time("week", self.network_name, log)))))
            rows.append(Row("Reset the schedule", action=lambda: self.window.push(ProgressPage(self.window, f"Resetting {self.network_name}", lambda log: _jobs().reset_schedule(self.network_name, log)))))
        if station.get("network_type") not in ("guide", "web"):
            from fs42 import picture

            current = picture.from_station(station)
            rows.append(Row("Picture", value=f"{current['mode'].upper()}  ZOOM {int(round(current['zoom'] * 100))}%",
                            action=lambda: self.window.push(PicturePage(self.window, self.network_name))))
        rows.append(Row("Delete this station…", tone="bad", action=lambda: self.window.push(ConfirmDeletePage(self.window, station))))
        return rows

    def _toggle_hidden(self):
        from fs42.menu import station_forms

        station = self._station()
        station_forms.set_hidden(self.network_name, not bool(station.get("hidden")))
        send_player({"command": "reload_stations"})
        self.say("Saved", "good")
        self.refresh()


class PicturePage(ListPage):
    """Scaling and zoom for one channel, previewed on screen as you dial."""

    hint = HINT_LIST

    def __init__(self, window, network_name):
        from fs42 import picture
        from fs42.station_manager import StationManager

        self.picture = picture
        self.network_name = network_name
        station = StationManager().station_by_name(network_name) or {}
        self.saved = picture.from_station(station)
        self.values = dict(self.saved)
        self.adjusting = None
        self.before_adjust = None
        super().__init__(window)
        self.title = f"Picture - {network_name}"

    def _on_screen(self):
        status = ipc.get_status() or {}
        return status.get("network_name") == self.network_name

    def build_rows(self):
        self.subtitle = ("CHANGES SHOW ON SCREEN AS YOU DIAL" if self._on_screen()
                         else "TUNE TO THIS CHANNEL TO SEE CHANGES LIVE")
        mode = self.values["mode"].upper()
        zoom = f"{int(round(self.values['zoom'] * 100))}%"
        rows = [
            Row("Scaling", value=f"◄ {mode} ►" if self.adjusting == "mode" else mode,
                action=lambda: self._start_adjust("mode"), tone="accent" if self.adjusting == "mode" else "normal"),
            Row("Zoom", value=f"◄ {zoom} ►" if self.adjusting == "zoom" else zoom,
                action=lambda: self._start_adjust("zoom"), tone="accent" if self.adjusting == "zoom" else "normal"),
            Row("Back to normal", action=self._reset, enabled=self.values != {"mode": "fit", "zoom": 1.0}),
            Row("Save", tone="good", action=self._save, enabled=self.values != self.saved),
        ]
        return rows

    def _preview(self, values=None):
        send_player({"command": "picture", "network_name": self.network_name,
                     "values": self.values if values is None else values})
        self.refresh()

    def _start_adjust(self, key):
        self.adjusting = key
        self.before_adjust = self.values[key]
        self.hint = HINT_ADJUST
        self.refresh()
        text = {"mode": "FIT = BARS  FILL = CROP EDGES  STRETCH = FILL THE SCREEN",
                "zoom": "▲ ▼ ZOOM IN OR OUT IN 5% STEPS"}[key]
        self.say(text, "accent")

    def _step(self, direction):
        if self.adjusting == "mode":
            modes = self.picture.MODES
            self.values["mode"] = modes[(modes.index(self.values["mode"]) + direction) % len(modes)]
        else:
            self.values["zoom"] = self.values["zoom"] + direction * self.picture.ZOOM_STEP
        self.values = self.picture.clean(self.values)
        self._preview()

    def _finish_adjust(self, keep):
        if not keep:
            self.values[self.adjusting] = self.before_adjust
        self.adjusting = None
        self.hint = HINT_LIST
        self._preview()
        self.say("" if keep else "CANCELLED", "muted")

    def _reset(self):
        self.values = {"mode": "fit", "zoom": 1.0}
        self._preview()
        self.say("PREVIEWING - CHOOSE SAVE TO KEEP IT", "muted")

    def _save(self):
        from fs42.menu import station_forms

        try:
            station_forms.set_picture(self.network_name, self.values)
        except Exception as e:
            self.say(f"COULD NOT SAVE: {e}", "bad")
            return
        self.saved = dict(self.values)
        send_player({"command": "reload_stations"})
        self.refresh()
        self.say("SAVED FOR THIS CHANNEL", "good")

    def hideEvent(self, event):
        if self not in self.window.stack and self.values != self.saved:
            # Leaving without saving: back to what the station config says.
            self.values = dict(self.saved)
            send_player({"command": "picture", "network_name": self.network_name, "values": None})
        super().hideEvent(event)

    def handle(self, action):
        if self.adjusting:
            if action == Action.UP.value:
                self._step(+1)
            elif action == Action.DOWN.value:
                self._step(-1)
            elif action in (Action.SELECT.value, Action.RIGHT.value):
                self._finish_adjust(keep=True)
            elif action in (Action.BACK.value, Action.LEFT.value):
                self._finish_adjust(keep=False)
            return True
        return super().handle(action)


class ConfirmDeletePage(ListPage):
    hint = HINT_CONFIRM

    def __init__(self, window, station):
        self.station = station
        super().__init__(window)

    def build_rows(self):
        self.title = f"Delete {self.station['network_name']}?"
        self.subtitle = "Removes the station config, its catalog and schedule. Media files are not touched."
        return [
            Row("Keep it", action=self.window.pop),
            Row("Delete it", tone="bad", action=self._delete),
        ]

    def _delete(self):
        from fs42.menu import station_forms

        station_forms.delete_station(self.station, log=lambda line: _l.info(line))
        send_player({"command": "reload_stations"})
        self.window.pop_to(StationsPage)


# ------------------------------------------------------------- add-station

class FolderPage(ListPage):
    """Browse for the folder that holds the new station's media."""

    title = "Add a station"

    def __init__(self, window, folder=None):
        self.folder = Path(folder) if folder else paths.catalog_root()
        super().__init__(window)

    def build_rows(self):
        from fs42.menu import station_forms

        self.subtitle = str(self.folder)
        info = station_forms.describe_folder(self.folder)
        rows = []
        if info["kind"] == "standard":
            extras = [k for k in ("bump_dir", "commercial_dir") if info[k]]
            note = f"{len(info['tags'])} show folders" + (" + " + " + ".join(info[k] for k in extras) if extras else "")
            rows.append(Row(f"Use this folder  ({note})", tone="good",
                            action=lambda: self.window.push(TagsPage(self.window, self.folder, info["tags"]))))
        elif info["kind"] == "loop":
            rows.append(Row(f"Use this folder  ({info['loose_files']} files, plays on a loop)", tone="good",
                            action=lambda: self.window.push(NamePage(self.window, self.folder, "loop", []))))
        else:
            rows.append(Row("No media here - open a folder below", tone="muted"))
        if self.folder.parent != self.folder:
            rows.append(Row("..  (up one level)", action=lambda: self._open(self.folder.parent)))
        for sub in station_forms.subfolders(self.folder):
            summary = station_forms.describe_folder(sub)
            value = {"standard": f"{len(summary['tags'])} show folders", "loop": f"{summary['loose_files']} files"}.get(summary["kind"], "")
            rows.append(Row(sub.name + "/", value=value, action=lambda s=sub: self._open(s)))
        rows.append(Row("Browse anywhere…", tone="muted", action=self._browse))
        return rows

    def _open(self, folder):
        self.folder = Path(folder)
        self.cursor = 0
        self.refresh()

    def _browse(self):
        chosen = QFileDialog.getExistingDirectory(self.window, "Choose a media folder", str(self.folder))
        if chosen:
            self._open(chosen)


class TagsPage(ListPage):
    title = "Which folders should this channel play?"
    subtitle = "Select toggles a folder.  Continue when done."

    def __init__(self, window, folder, tags):
        self.folder = folder
        self.tags = list(tags)
        self.chosen = set(tags)
        super().__init__(window)

    def build_rows(self):
        rows = [Row(tag, checked=tag in self.chosen, action=lambda t=tag: self._toggle(t)) for tag in self.tags]
        rows.append(Row("Continue", tone="good", enabled=bool(self.chosen),
                        action=lambda: self.window.push(NamePage(self.window, self.folder, "standard", [t for t in self.tags if t in self.chosen]))))
        return rows

    def _toggle(self, tag):
        if tag in self.chosen:
            self.chosen.discard(tag)
        else:
            self.chosen.add(tag)
        keep = self.cursor
        self.refresh()
        self.cursor = keep
        self.update()


class NamePage(ListPage):
    """Name and channel number, with real text widgets for keyboard users
    and digit/delete actions for everyone else."""

    title = "Name and channel number"
    hint = HINT_TYPE

    def __init__(self, window, folder, kind, tags):
        from fs42.menu import station_forms
        from fs42.station_manager import StationManager

        self.folder = folder
        self.kind = kind
        self.tags = tags
        super().__init__(window)
        s = self.scale
        self.name_edit = QLineEdit(self)
        self.name_edit.setText(station_forms.suggest_name(folder))
        self.number_edit = QSpinBox(self)
        self.number_edit.setRange(0, 9999)
        self.number_edit.setValue(station_forms.next_channel_number(StationManager().stations))
        for widget in (self.name_edit, self.number_edit):
            widget.setFont(theme.font(22, scale=s))
        self.subtitle = f"{kind} station from {folder.name}"
        self._number_touched = False
        self._layout_widgets()
        self.name_edit.setFocus()

    def build_rows(self):
        return [
            Row("Name", action=lambda: self.name_edit.setFocus()),
            Row("Channel number", action=lambda: self.number_edit.setFocus()),
            Row("Create station", tone="good", action=self._create),
        ]

    def _layout_widgets(self):
        s = self.scale
        pad = int(36 * s)
        # Mirrors the title/subtitle stack in ListPage.paintEvent.
        top = int(24 * s) + int(52 * s) + int(34 * s) + int(8 * s)
        rh = self.row_height()
        w = int(self.width() * 0.5)
        x = self.width() - pad - w - int(12 * s)
        self.name_edit.setGeometry(x, top + int(4 * s), w, rh - int(14 * s))
        self.number_edit.setGeometry(x, top + rh + int(4 * s), w, rh - int(14 * s))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "name_edit"):
            self._layout_widgets()

    def handle(self, action):
        # Digits and delete go to whichever field the cursor is on, so the
        # phone remote can set a channel number without a keyboard.
        value = digit_value(action)
        if self.cursor == 1 and value is not None:
            # The first digit replaces the suggested number; later ones append.
            current = self.number_edit.value() if self._number_touched else 0
            self._number_touched = True
            self.number_edit.setValue(min(9999, current * 10 + value) if current < 1000 else value)
            return True
        if self.cursor == 1 and action == Action.DELETE.value:
            self.number_edit.setValue(self.number_edit.value() // 10)
            return True
        if self.cursor == 0 and action == Action.DELETE.value:
            self.name_edit.backspace()
            return True
        if action in (Action.UP.value, Action.DOWN.value):
            consumed = super().handle(action)
            (self.name_edit if self.cursor == 0 else self.number_edit if self.cursor == 1 else self).setFocus()
            return consumed
        if action == Action.SELECT.value and self.cursor < 2:
            self.move(+1)
            return True
        return super().handle(action)

    def _create(self):
        from fs42.menu import station_forms

        try:
            config = station_forms.build_station_config(
                self.folder, self.name_edit.text(), self.number_edit.value(), self.kind, self.tags
            )
            message, file_path = station_forms.create_station(config)
        except station_forms.FormError as e:
            self.say(str(e), "bad")
            return
        send_player({"command": "reload_stations"})
        name = config["station_conf"]["network_name"]
        if config["station_conf"]["network_type"] == "standard":
            self.window.replace(ProgressPage(
                self.window, f"Building {name}",
                lambda log: (_jobs().rebuild_catalog(name, log), _jobs().add_schedule_time("week", name, log)),
                reload_stations=True, done_page=StationsPage,
            ))
        else:
            self.window.pop_to(StationsPage)


# ---------------------------------------------------------- video effects

HINT_ADJUST = "ADJUST:▲ ▼ KEY\nSET   :► KEY\nCANCEL:◄ KEY"


class VideoEffectsPage(ListPage):
    """CRT scanlines and noise, dialled in live over the menu and the video."""

    title = "Video effects"
    hint = HINT_LIST

    # (key, label, step) - a step of None means "cycle through the choices".
    FIELDS = [
        ("scanline_opacity", "Scanlines", 0.05),
        ("scanline_pattern", "Screen style", None),
        ("scanline_style", "Scanline style", None),
        ("scanline_thickness", "Scanline thickness", 1),
        ("scanline_size", "Scanline spacing", 1),
        ("noise_opacity", "Noise", 0.05),
        ("noise_grain", "Noise grain", 1),
    ]

    def __init__(self, window):
        from fs42 import video_effects

        self.vfx = video_effects
        self.saved = video_effects.load()
        self.values = dict(self.saved)
        self.adjusting = None
        self.before_adjust = None
        super().__init__(window)

    # ------------------------------------------------------------ rows

    def _describe(self, key):
        value = self.values[key]
        if key.endswith("_opacity"):
            return "OFF" if value <= 0 else f"{int(round(value * 100))}%"
        if key in self.vfx.CHOICES:
            return str(value).upper()
        if key == "scanline_size":
            return f"EVERY {value} PX"
        return f"{value} PX"

    def build_rows(self):
        self.subtitle = "OVER THE PICTURE AND THIS MENU"
        rows = []
        for key, label, _ in self.FIELDS:
            value = self._describe(key)
            if self.adjusting == key:
                value = f"◄ {value} ►"
            rows.append(Row(label, value=value, action=lambda k=key: self._start_adjust(k),
                            tone="accent" if self.adjusting == key else "normal"))
        rows.append(Row("Presets", value="subtle / classic / heavy / grid",
                        action=lambda: self.window.push(EffectPresetsPage(self.window, self))))
        rows.append(Row("Save", tone="good", action=self._save, enabled=self.values != self.saved))
        return rows

    # ---------------------------------------------------------- preview

    def _preview(self):
        self.values = self.vfx.clean(self.values)
        try:
            self.window.effects.apply(self.values)
        except Exception:
            pass
        send_player({"command": "video_effects", "values": self.values})
        self.refresh()

    def _start_adjust(self, key):
        self.adjusting = key
        self.before_adjust = self.values[key]
        self.hint = HINT_ADJUST
        self.refresh()
        self.say("▲ ▼ TO DIAL IT IN - YOU CAN SEE IT CHANGE", "accent")

    def _step(self, direction):
        key = self.adjusting
        step = dict((k, s) for k, _, s in self.FIELDS)[key]
        if step is None:
            options = self.vfx.CHOICES[key]
            index = options.index(self.values[key]) if self.values[key] in options else 0
            self.values[key] = options[(index + direction) % len(options)]
        else:
            low, high = self.vfx.LIMITS[key]
            self.values[key] = max(low, min(high, round(self.values[key] + direction * step, 3)))
        self._preview()

    def _finish_adjust(self, keep):
        if not keep:
            self.values[self.adjusting] = self.before_adjust
        self.adjusting = None
        self.hint = HINT_LIST
        self._preview()
        self.say("" if keep else "CANCELLED", "muted")

    def apply_preset(self, preset):
        self.values.update(preset)
        self._preview()
        self.say("PREVIEWING - CHOOSE SAVE TO KEEP IT", "muted")

    # Kept for older callers and tests.
    _apply_preset = apply_preset

    def _save(self):
        try:
            self.saved = self.vfx.save(self.values)
        except Exception as e:
            self.say(f"COULD NOT SAVE: {e}", "bad")
            return
        self.values = dict(self.saved)
        self.refresh()
        self.say("SAVED", "good")

    def hideEvent(self, event):
        # Leaving the page without saving puts things back - but not when it
        # is only hidden under the presets page, which is still in the stack.
        if self not in self.window.stack and self.values != self.saved:
            self.values = dict(self.saved)
            self._preview()
        super().hideEvent(event)

    def handle(self, action):
        if self.adjusting:
            if action == Action.UP.value:
                self._step(+1)
            elif action == Action.DOWN.value:
                self._step(-1)
            elif action in (Action.SELECT.value, Action.RIGHT.value):
                self._finish_adjust(keep=True)
            elif action in (Action.BACK.value, Action.LEFT.value):
                self._finish_adjust(keep=False)
            return True
        return super().handle(action)


class EffectPresetsPage(ListPage):
    title = "Presets"
    hint = HINT_LIST

    PRESETS = [
        ("Subtle CRT", {"scanline_opacity": 0.35, "scanline_pattern": "horizontal", "scanline_style": "soft",
                        "scanline_thickness": 1, "scanline_size": 3, "noise_opacity": 0.06, "noise_grain": 1}),
        ("Classic CRT", {"scanline_opacity": 0.55, "scanline_pattern": "horizontal", "scanline_style": "medium",
                         "scanline_thickness": 2, "scanline_size": 4, "noise_opacity": 0.1, "noise_grain": 2}),
        ("Heavy CRT", {"scanline_opacity": 0.75, "scanline_pattern": "horizontal", "scanline_style": "hard",
                       "scanline_thickness": 3, "scanline_size": 6, "noise_opacity": 0.18, "noise_grain": 2}),
        ("Arcade monitor", {"scanline_opacity": 0.5, "scanline_pattern": "grid", "scanline_style": "medium",
                            "scanline_thickness": 1, "scanline_size": 4, "noise_opacity": 0.05, "noise_grain": 1}),
        ("Off", {"scanline_opacity": 0.0, "noise_opacity": 0.0}),
    ]

    def __init__(self, window, parent_page):
        self.parent_page = parent_page
        super().__init__(window)

    def build_rows(self):
        self.subtitle = "PICK ONE, THEN SAVE ON THE PREVIOUS PAGE"
        return [Row(label, action=lambda p=preset: self._pick(p)) for label, preset in self.PRESETS]

    def _pick(self, preset):
        self.parent_page.apply_preset(preset)
        self.window.pop()


# -------------------------------------------------------------- add input

class ControllersPage(ListPage):
    """The controllers that have been set up, and a way to add another."""

    title = "Remote Controls"
    hint = HINT_LIST

    def __init__(self, window):
        from fs42.menu import gamepad

        self.gamepad = gamepad
        super().__init__(window)

    def build_rows(self):
        controllers = self.gamepad.load_controllers()
        self.subtitle = ("EACH CONTROLLER HAS ITS OWN BUTTON LAYOUT" if controllers
                         else "NO CONTROLLERS SET UP YET")
        rows = []
        for index, entry in enumerate(controllers):
            device = "" if entry["device"] == self.gamepad.ANY_DEVICE else entry["device"]
            rows.append(Row(entry["name"], value=device,
                            action=lambda i=index: self.window.push(InputPage(self.window, controller_index=i))))
        rows.append(Row("+  Add a controller", tone="good",
                        action=lambda: self.window.push(InputPage(self.window))))
        return rows


class InputPage(ListPage):
    """Learn which button on one controller does what.

    The page runs its own gamepad reader in *raw* mode: every newly pressed
    control arrives with the name of the device it came from.  A new
    controller is identified by the first button pressed on it; after that
    only presses from that device count.  Selecting a function then pressing
    a button assigns it; "Map every button" walks through all of them in
    order.  The player's own reader is muted meanwhile (KEY_INPUT_CAPTURE)
    so the press being learned does not also move the cursor.
    """

    title = "Controller"
    hint = HINT_LIST

    def __init__(self, window, controller_index=None):
        from fs42.menu import gamepad

        self.gamepad = gamepad
        self.controllers = gamepad.load_controllers()
        self.index = controller_index if controller_index is not None and controller_index < len(self.controllers) else None
        if self.index is None:
            self.entry = {"name": "", "device": "", "map": {}}
        else:
            self.entry = dict(self.controllers[self.index])
            self.entry["map"] = dict(self.entry["map"])
        self.capturing = None       # function being learned, or None
        self.queue = []             # functions still to learn in "map every button"
        self.pending = []           # (device, control) from the reader thread
        self.lock = threading.Lock()
        self.reader = None
        self.confirm_delete = False
        super().__init__(window)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(80)
        self._listen(True)

    # ----------------------------------------------------------- reader

    @property
    def is_new(self):
        return self.index is None

    @property
    def device_known(self):
        return bool(self.entry["device"])

    def _listen(self, on: bool):
        try:
            ipc.set_state(ipc.KEY_INPUT_CAPTURE, bool(on))
        except Exception:
            pass
        if on and self.reader is None:
            self.reader = self.gamepad.GamepadReader(mapping=self.gamepad.MappingTable([]), raw_sink=self._raw)
            self.reader.start()
        elif not on and self.reader is not None:
            reader, self.reader = self.reader, None
            threading.Thread(target=reader.stop, daemon=True).start()

    def _raw(self, device, control):
        # Reader thread: hand the press to the Qt thread via the timer.
        with self.lock:
            self.pending.append((device, control))

    def hideEvent(self, event):
        self._listen(False)
        super().hideEvent(event)

    def on_show(self):
        self._listen(True)
        super().on_show()

    # ------------------------------------------------------------ rows

    def build_rows(self):
        effective = self.gamepad.apply_custom(self.gamepad.default_mapping(), self.entry["map"])
        if not self.device_known:
            self.title = "New controller"
            self.subtitle = "PRESS ANY BUTTON ON THE CONTROLLER TO ADD"
        else:
            self.title = self.entry["name"] or "Controller"
            plugged = self.reader is not None and self.entry["device"] in (self.reader.devices or [])
            self.subtitle = self.entry["device"].upper() + (" - CONNECTED" if plugged else " - NOT CONNECTED")
        rows = []
        for function, label in self.gamepad.FUNCTIONS:
            controls = self.gamepad.controls_for(effective, function)
            value = "  ".join(self.gamepad.describe_control(c) for c in controls) or "---"
            if self.capturing == function:
                value = "PRESS A BUTTON..."
            rows.append(Row(label, value=value, action=lambda f=function: self._capture(f),
                            enabled=self.device_known,
                            tone="accent" if self.capturing == function else "normal"))
        rows.append(Row("Map every button in order", action=self._map_all, enabled=self.device_known))
        rows.append(Row("Back to defaults", action=self._reset, enabled=bool(self.entry["map"])))
        rows.append(Row("Save", tone="good", action=self._save, enabled=self.device_known))
        if not self.device_known:
            rows.append(Row("Cancel", action=self.window.pop))
        if not self.is_new:
            if self.confirm_delete:
                rows.append(Row("Really delete this controller?", tone="bad", action=self._delete))
            else:
                rows.append(Row("Delete this controller", tone="bad", action=self._ask_delete))
        return rows

    def _capture(self, function):
        self.capturing = function
        self.refresh()
        label = dict(self.gamepad.FUNCTIONS)[function].upper()
        self.say(f"PRESS THE BUTTON FOR {label}  (◄ CANCELS)", "accent")

    def _map_all(self):
        self.queue = list(self.gamepad.FUNCTION_NAMES)
        self.entry["map"] = {}
        self._next_in_queue()

    def _next_in_queue(self):
        if self.queue:
            function = self.queue.pop(0)
            self.cursor = self.gamepad.FUNCTION_NAMES.index(function)
            self._capture(function)
        else:
            self.capturing = None
            self.refresh()
            self.cursor = next(i for i, r in enumerate(self.rows) if r.title == "Save")
            self.update()
            self.publish()
            self.say("ALL SET - CHOOSE SAVE TO KEEP IT", "good")

    def _assign(self, control):
        function = self.capturing
        # One function, one button: drop what used to point at it.
        self.entry["map"] = {c: a for c, a in self.entry["map"].items() if a != function}
        self.entry["map"][control] = function
        self.capturing = None
        label = dict(self.gamepad.FUNCTIONS)[function].upper()
        self.refresh()
        self.say(f"{self.gamepad.describe_control(control)} = {label}", "good")
        if self.queue:
            QTimer.singleShot(500, self._next_in_queue)
        elif self.cursor < len(self.gamepad.FUNCTIONS) - 1:
            self.move(+1)

    def _adopt_device(self, device):
        self.entry["device"] = device
        if not self.entry["name"]:
            self.entry["name"] = device
        existing = self.gamepad.controller_for(device, [c for c in self.controllers if c["device"] == device])
        if existing is not None:
            # This device already has an entry: edit that one rather than
            # making a second which would never be used.
            self.index = self.controllers.index(existing)
            self.entry = {"name": existing["name"], "device": device, "map": dict(existing["map"])}
            self.refresh()
            self.say(f"{device.upper()} IS ALREADY SET UP - EDITING IT", "warn")
        else:
            self.refresh()
            self.say(f"ADDED {device.upper()} - NOW SET ITS BUTTONS", "good")
        self.cursor = 0
        self.update()
        self.publish()

    def _poll(self):
        with self.lock:
            presses, self.pending = self.pending, []
        if self.reader is not None and self.device_known:
            plugged = self.entry["device"] in (self.reader.devices or [])
            if ("- CONNECTED" in self.subtitle) != plugged:
                self.refresh()
        for device, control in presses:
            if not self.device_known:
                self._adopt_device(device)
                continue
            if device != self.entry["device"]:
                self.say(f"THAT IS A DIFFERENT CONTROLLER ({device.upper()})", "warn")
                continue
            if self.capturing:
                self._assign(control)
            else:
                effective = self.gamepad.apply_custom(self.gamepad.default_mapping(), self.entry["map"])
                function = effective.get(control)
                label = dict(self.gamepad.FUNCTIONS).get(function, "NOT ASSIGNED").upper()
                self.say(f"{self.gamepad.describe_control(control)} = {label}", "muted")

    def _reset(self):
        self.entry["map"] = {}
        self.queue = []
        self.capturing = None
        self.refresh()
        self.say("DEFAULT LAYOUT - CHOOSE SAVE TO KEEP IT", "muted")

    def _save(self):
        controllers = list(self.controllers)
        if self.index is None:
            controllers.append(self.entry)
        else:
            controllers[self.index] = self.entry
        try:
            self.gamepad.save_controllers(controllers)
        except Exception as e:
            self.say(f"COULD NOT SAVE: {e}", "bad")
            return
        send_player({"command": "reload_input"})
        self.say("SAVED - CONTROLLER IS ON", "good")
        QTimer.singleShot(700, self.window.pop)

    def _ask_delete(self):
        self.confirm_delete = True
        self.refresh()
        self.say("SELECT AGAIN TO DELETE, OR ◄ TO KEEP IT", "warn")

    def _delete(self):
        controllers = [c for i, c in enumerate(self.controllers) if i != self.index]
        try:
            self.gamepad.save_controllers(controllers)
        except Exception as e:
            self.say(f"COULD NOT SAVE: {e}", "bad")
            return
        send_player({"command": "reload_input"})
        self.say("DELETED", "warn")
        QTimer.singleShot(500, self.window.pop)

    def handle(self, action):
        if self.capturing:
            if action in (Action.BACK.value, Action.LEFT.value):
                self.capturing = None
                self.queue = []
                self.refresh()
                self.say("CANCELLED", "muted")
            return True
        if self.confirm_delete and action in (Action.BACK.value, Action.LEFT.value, Action.UP.value, Action.DOWN.value):
            self.confirm_delete = False
            self.refresh()
            if action in (Action.BACK.value, Action.LEFT.value):
                self.say("KEPT", "muted")
                return True
        return super().handle(action)


# ---------------------------------------------------------------- progress

class _JobRunner(QObject):
    line = Signal(str)
    finished = Signal(bool, str)

    def __init__(self, job):
        super().__init__()
        self.job = job

    def run(self):
        try:
            self.job(self.line.emit)
            self.finished.emit(True, "")
        except Exception as e:
            _l.exception(e)
            self.finished.emit(False, str(e))


class ProgressPage(ListPage):
    hint = ""

    def __init__(self, window, title, job, reload_stations=False, done_page=None):
        self.title = title
        self.lines = []
        self.done = False
        self.ok = None
        self.reload_stations = reload_stations
        self.done_page = done_page
        super().__init__(window)
        self.subtitle = "Working…"
        self.thread = QThread()
        self.runner = _JobRunner(job)
        self.runner.moveToThread(self.thread)
        self.thread.started.connect(self.runner.run)
        self.runner.line.connect(self._line)
        self.runner.finished.connect(self._finished)
        self.thread.start()

    def build_rows(self):
        rows = [Row(line, tone="muted") for line in self.lines[-12:]]
        if self.done:
            rows.append(Row("Done" if self.ok else "Back", tone="good" if self.ok else "bad", action=self._leave))
        return rows

    def _line(self, text):
        self.lines.append(text)
        self.refresh()

    def _finished(self, ok, error):
        self.done = True
        self.ok = ok
        self.subtitle = "Finished" if ok else f"Failed: {error}"
        self.hint = HINT_CONTINUE
        # The menu process built it; tell the player so it sees the result.
        from fs42.liquid_manager import LiquidManager

        try:
            LiquidManager().reload_schedules()
        except Exception:
            pass
        send_player({"command": "reload_data"})
        if self.reload_stations:
            send_player({"command": "reload_stations"})
        self.thread.quit()
        self.thread.wait(2000)
        self.refresh()
        self.cursor = len(self.rows) - 1
        self.update()

    @property
    def busy(self):
        return not self.done

    def _leave(self):
        if self.done_page is not None:
            self.window.pop_to(self.done_page)
            self.window.stack[-1].cursor = 0
            self.window.stack[-1].refresh()
        else:
            self.window.pop()

    def handle(self, action):
        if not self.done and action in (Action.BACK.value, Action.LEFT.value):
            self.say("Still working - please wait", "warn")
            return True
        return super().handle(action)
