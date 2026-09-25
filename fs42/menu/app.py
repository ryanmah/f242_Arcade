"""The menu window and its process entry point.

One fullscreen, frameless, always-on-top window with a centred panel.  Pages
are stacked inside the panel.  Input arrives two ways and is treated the
same: Qt key events when this window has focus, and actions on the state bus
when a key landed on the mpv window instead, or came from the phone remote or
a gamepad.
"""

import logging
import multiprocessing
import signal
import sys

from fs42 import ipc, paths

_l = logging.getLogger("MENU")

BUS_POLL_MS = 50
PLAYER_POLL_MS = 1000


def _build_window():
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QColor, QPainter
    from PySide6.QtWidgets import QApplication, QWidget

    from fs42.menu import theme
    from fs42.menu.input import Action, qt_key_to_action
    from fs42.menu.pages import HomePage
    from fs42.menu.effects import EffectsOverlay

    class MenuWindow(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("FieldStation42 Menu")
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
            self.setAttribute(Qt.WA_TranslucentBackground)
            screen = QApplication.primaryScreen().geometry()
            self.setGeometry(screen)
            self.scale = theme.scale_for(screen.height())
            theme.load_fonts()
            self.setStyleSheet(theme.stylesheet(self.scale * theme.TEXT_SCALE))

            # The "screen" of the VCR: a centred column of text with the
            # generous margins a CRT's overscan would have needed.
            self.panel = QWidget(self)
            width = int(min(screen.width() * 0.74, 1440 * self.scale))
            height = int(min(screen.height() * 0.93, 1040 * self.scale))
            self.panel.setGeometry((screen.width() - width) // 2, (screen.height() - height) // 2, width, height)

            # Anything queued before this window existed was aimed at a menu
            # that is gone (or at nothing); start from a clean queue.  This
            # must happen before the first page is published, because the
            # remote treats a published page as "ready for input".
            while ipc.pop(ipc.TOPIC_MENU_INPUT, "menu"):
                pass

            # CRT scanlines and noise over the whole menu, matching what the
            # player puts over the video.
            self.effects = EffectsOverlay(self)
            self.effects.setGeometry(self.rect())

            self.stack = []
            self.push(HomePage(self))

            self.bus_timer = QTimer(self)
            self.bus_timer.timeout.connect(self._drain_bus)
            self.bus_timer.start(BUS_POLL_MS)

            self.player_timer = QTimer(self)
            self.player_timer.timeout.connect(self._check_player)
            self.player_timer.start(PLAYER_POLL_MS)

            # Game Mode: draw over the video instead of replacing it.
            from fs42 import gamescope

            gamescope.mark_overlay(self)

        # ------------------------------------------------------ navigation

        def push(self, page):
            if self.stack:
                self.stack[-1].hide()
            page.setGeometry(0, 0, self.panel.width(), self.panel.height())
            self.stack.append(page)
            page.on_show()
            page.show()
            self.effects.raise_()

        def replace(self, page):
            if self.stack:
                old = self.stack.pop()
                old.hide()
                old.deleteLater()
            self.push(page)

        def pop(self):
            if len(self.stack) <= 1:
                self.close_menu()
                return
            page = self.stack.pop()
            page.hide()
            page.deleteLater()
            self.stack[-1].on_show()
            self.stack[-1].show()

        def pop_to(self, page_type):
            while len(self.stack) > 1 and not isinstance(self.stack[-1], page_type):
                page = self.stack.pop()
                page.hide()
                page.deleteLater()
            self.stack[-1].on_show()
            self.stack[-1].show()

        def close_menu(self):
            top = self.stack[-1] if self.stack else None
            if getattr(top, "busy", False):
                top.say("Still working - please wait", "warn")
                return
            QApplication.quit()

        # ----------------------------------------------------------- input

        def dispatch(self, action):
            if action == Action.MENU.value:
                self.close_menu()
                return
            if self.stack:
                self.stack[-1].handle(action)

        def keyPressEvent(self, event):
            action = qt_key_to_action(event.key(), event.text())
            if action is None:
                return super().keyPressEvent(event)
            self.dispatch(action)
            event.accept()

        def _drain_bus(self):
            for _ in range(8):
                message = ipc.pop(ipc.TOPIC_MENU_INPUT, "menu")
                if not message:
                    break
                action = message.get("action")
                if action:
                    self.dispatch(action)

        def _check_player(self):
            # If the player went away (or asked everything to stop), so do we.
            if ipc.shutdown_requested():
                self.close_menu()

        # --------------------------------------------------------- painting

        def paintEvent(self, event):
            # One flat VCR blue over the whole picture; no panel outline.
            painter = QPainter(self)
            painter.fillRect(self.rect(), theme.BACKDROP)
            painter.end()

    return MenuWindow


def run_menu_app() -> int:
    """Body of the menu process."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    signal.signal(signal.SIGINT, lambda *_: app.quit())

    # Whatever the player has seen, we need our own view of the stations.
    paths.first_run_seed()
    from fs42.station_manager import StationManager

    StationManager()

    MenuWindow = _build_window()
    window = MenuWindow()
    window.show()
    window.raise_()
    window.activateWindow()
    try:
        return app.exec()
    finally:
        ipc.set_state(ipc.KEY_MENU_OPEN, False)
        ipc.set_state(ipc.KEY_INPUT_CAPTURE, False)


def _menu_entry():
    """Module-level process target (closures cannot be pickled under spawn)."""
    sys.exit(run_menu_app())


def run_menu():
    """Start the menu in its own process; returns the Process."""
    process = multiprocessing.Process(target=_menu_entry, name="fs42-menu")
    process.start()
    return process


if __name__ == "__main__":
    sys.exit(run_menu_app())
