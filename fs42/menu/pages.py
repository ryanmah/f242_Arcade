"""Pages of the in-app menu.

Every page is a list of rows drawn by ``ListPage``: a title, an optional value
on the right, and an action.  Up/Down move the cursor, Select activates, Back
pops the page.  That single interaction model is what makes the menu usable
from a keyboard, the phone remote and a gamepad alike.
"""

import logging
import socket
import threading
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


class ListPage(QWidget):
    """A vertically scrolling list of rows, painted directly."""

    title = ""
    subtitle = ""
    hint = "↑↓ move   ⏎ select   ⌫ back"

    def __init__(self, window):
        super().__init__(window.panel)
        self.window = window
        self.scale = window.scale
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
        return int(56 * self.scale)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        s = self.scale
        width = self.width()
        pad = int(28 * s)
        y = pad

        painter.setPen(theme.TEXT)
        painter.setFont(theme.font(34, bold=True, scale=s))
        painter.drawText(QRect(pad, y, width - 2 * pad, int(48 * s)), Qt.AlignLeft | Qt.AlignVCenter, self.title)
        y += int(50 * s)
        if self.subtitle:
            painter.setPen(theme.MUTED)
            painter.setFont(theme.font(18, scale=s))
            painter.drawText(QRect(pad, y, width - 2 * pad, int(28 * s)), Qt.AlignLeft | Qt.AlignVCenter, self.subtitle)
            y += int(32 * s)
        y += int(10 * s)

        footer = int(70 * s)
        list_top = y
        list_bottom = self.height() - footer
        rh = self.row_height()
        visible = max(1, (list_bottom - list_top) // rh)

        if self.cursor < self.scroll:
            self.scroll = self.cursor
        elif self.cursor >= self.scroll + visible:
            self.scroll = self.cursor - visible + 1

        painter.setFont(theme.font(22, scale=s))
        metrics = QFontMetrics(painter.font())
        for index in range(self.scroll, min(len(self.rows), self.scroll + visible)):
            row = self.rows[index]
            rect = QRect(pad, y, width - 2 * pad, rh - int(6 * s))
            selected = index == self.cursor and row.action is not None
            if selected:
                painter.setBrush(theme.ROW_SELECTED)
                painter.setPen(QPen(theme.ACCENT, max(2, int(3 * s))))
            else:
                painter.setBrush(theme.ROW)
                painter.setPen(QPen(theme.BORDER, 1))
            painter.drawRoundedRect(QRectF(rect), 8 * s, 8 * s)

            text_left = rect.left() + int(18 * s)
            if row.checked is not None:
                box = QRect(text_left, rect.top() + (rect.height() - int(24 * s)) // 2, int(24 * s), int(24 * s))
                painter.setPen(QPen(theme.ACCENT if row.checked else theme.MUTED, 2))
                painter.setBrush(theme.ACCENT if row.checked else Qt.NoBrush)
                painter.drawRoundedRect(QRectF(box), 4 * s, 4 * s)
                text_left += int(40 * s)

            colour = {
                "muted": theme.MUTED, "accent": theme.ACCENT, "good": theme.GOOD,
                "warn": theme.WARN, "bad": theme.BAD,
            }.get(row.tone, theme.TEXT)
            if not row.enabled:
                colour = theme.MUTED
            painter.setPen(colour)
            value_width = metrics.horizontalAdvance(row.value) + int(24 * s) if row.value else 0
            title_rect = QRect(text_left, rect.top(), rect.width() - (text_left - rect.left()) - value_width - int(12 * s), rect.height())
            painter.drawText(title_rect, Qt.AlignLeft | Qt.AlignVCenter, metrics.elidedText(row.title, Qt.ElideRight, title_rect.width()))
            if row.value:
                painter.setPen(theme.MUTED if row.tone == "normal" else colour)
                painter.drawText(QRect(rect.left(), rect.top(), rect.width() - int(18 * s), rect.height()), Qt.AlignRight | Qt.AlignVCenter, row.value)
            y += rh

        if len(self.rows) > visible:
            painter.setPen(theme.MUTED)
            painter.setFont(theme.font(14, scale=s))
            painter.drawText(QRect(pad, list_bottom - int(20 * s), width - 2 * pad, int(20 * s)), Qt.AlignRight, f"{self.cursor + 1} / {len(self.rows)}")

        painter.setFont(theme.font(16, scale=s))
        if self.message:
            colour = {"good": theme.GOOD, "warn": theme.WARN, "bad": theme.BAD, "accent": theme.ACCENT}.get(self.message_tone, theme.MUTED)
            painter.setPen(colour)
            painter.drawText(QRect(pad, self.height() - footer + int(6 * s), width - 2 * pad, int(28 * s)), Qt.AlignLeft | Qt.AlignVCenter, self.message)
        painter.setPen(theme.MUTED)
        painter.drawText(QRect(pad, self.height() - int(34 * s), width - 2 * pad, int(28 * s)), Qt.AlignLeft | Qt.AlignVCenter, self.hint)
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

class HomePage(ListPage):
    title = "FieldStation42"

    def build_rows(self):
        from fs42.station_manager import StationManager

        port = StationManager().server_conf.get("server_port", 4242)
        status = ipc.get_status() or {}
        now = status.get("network_name") or "nothing"
        self.subtitle = f"Now playing: {now}"
        return [
            Row("Stations", action=lambda: self.window.push(StationsPage(self.window))),
            Row("Rebuild all catalogs", action=lambda: self.window.push(ProgressPage(self.window, "Rebuilding catalogs", lambda log: _jobs().rebuild_catalog("all", log), reload_stations=True))),
            Row("Add a week to all schedules", action=lambda: self.window.push(ProgressPage(self.window, "Adding a week", lambda log: _jobs().add_schedule_time("week", "all", log)))),
            Row(f"Web console: http://{_lan_ip()}:{port}", value="for everything else", tone="muted"),
            Row("Close menu", action=self.window.close_menu),
        ]


def _jobs():
    from fs42 import build_jobs

    return build_jobs


class StationsPage(ListPage):
    title = "Stations"
    subtitle = "Pick a station, or add one from a folder of media"

    def build_rows(self):
        from fs42.station_manager import StationManager

        # Pick up anything the web console changed while we were open.
        StationManager().reload_if_changed()
        playing = (ipc.get_status() or {}).get("network_name")
        rows = []
        for station in StationManager().stations:
            name = station["network_name"]
            marker = "▶ " if name == playing else ""
            rows.append(Row(
                f"{station['channel_number']:>3}   {marker}{name}",
                value=_station_summary(station),
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
        rows.append(Row("Delete this station…", tone="bad", action=lambda: self.window.push(ConfirmDeletePage(self.window, station))))
        return rows

    def _toggle_hidden(self):
        from fs42.menu import station_forms

        station = self._station()
        station_forms.set_hidden(self.network_name, not bool(station.get("hidden")))
        send_player({"command": "reload_stations"})
        self.say("Saved", "good")
        self.refresh()


class ConfirmDeletePage(ListPage):
    hint = "⌫ back"

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
    hint = "type to edit   ↑↓ move   ⏎ continue   ⌫ back"

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
        pad = int(28 * s)
        top = int(28 * s) + int(50 * s) + int(32 * s) + int(10 * s)
        rh = self.row_height()
        w = int(self.width() * 0.5)
        x = self.width() - pad - w - int(18 * s)
        self.name_edit.setGeometry(x, top + int(6 * s), w, rh - int(18 * s))
        self.number_edit.setGeometry(x, top + rh + int(6 * s), w, rh - int(18 * s))

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
        self.hint = "⏎ continue"
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
