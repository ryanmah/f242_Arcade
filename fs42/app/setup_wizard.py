"""Graphical installer and uninstaller for Linux and SteamOS.

The self-extracting ``.run`` unpacks the folder build and, when a display is
available, starts this wizard *from the bundled executable itself*:

    FieldStation42/FieldStation42 install --payload <unpacked dir>

so the installer needs nothing from the host - Qt and Python come along in
the payload.  The wizard only collects choices; the actual work is done by
the same ``install.sh`` / ``uninstall.sh`` the terminal path uses, driven
non-interactively, with their output streamed into the progress page.  One
implementation, two front ends.

``FieldStation42 uninstall`` (also the "Uninstall" action of the application
menu entry) runs the same wizard in removal mode from the installed copy.

Windows has Inno Setup for this; here the wizard just says so.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

APP = "FieldStation42"


# ------------------------------------------------------------------ helpers

def _default_prefix() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(data_home) / "fieldstation42" / "app"


def _data_dir() -> Path:
    return _default_prefix().parent


def _is_steamos() -> bool:
    try:
        with open("/etc/os-release") as handle:
            for line in handle:
                if line.startswith("ID="):
                    return line.strip().split("=", 1)[1].strip('"') in ("steamos", "bazzite", "chimeraos", "holoiso")
    except OSError:
        pass
    return False


def _steam_installed() -> bool:
    home = Path.home()
    return any((home / p / "userdata").is_dir() for p in (
        ".local/share/Steam", ".steam/steam", ".steam/debian-installation",
        ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    ))


def _steam_running() -> bool:
    try:
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                try:
                    with open(f"/proc/{pid}/comm") as handle:
                        if handle.read().strip() == "steam":
                            return True
                except OSError:
                    continue
    except OSError:
        pass
    return False


def _find_payload(explicit) -> Path | None:
    """The unpacked release folder: install.sh next to a FieldStation42/ dir."""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent.parent)
    for candidate in candidates:
        if (candidate / "install.sh").is_file() and (candidate / APP / APP).is_file():
            return candidate
    return None


def _find_installed_prefix() -> Path | None:
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent)
    candidates.append(_default_prefix())
    for candidate in candidates:
        if (candidate / "uninstall.sh").is_file():
            return candidate
    return None


def _icon_path():
    from fs42 import paths

    for candidate in (paths.resources("installer", "fieldstation42.png"),
                      paths.resources("packaging", "linux", "fieldstation42.png")):
        if candidate.exists():
            return candidate
    return None


# ------------------------------------------------------------------- wizard

def _build_wizard(mode, payload, prefix):
    from PySide6.QtCore import QProcess, Qt
    from PySide6.QtGui import QFont, QIcon, QPixmap
    from PySide6.QtWidgets import (
        QCheckBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
        QRadioButton, QVBoxLayout, QWizard, QWizardPage,
    )

    from fs42 import paths

    steamos = _is_steamos()
    icon = _icon_path()
    version = paths.app_version()

    class Welcome(QWizardPage):
        def __init__(self):
            super().__init__()
            self.setTitle(f"Install {APP}" if mode == "install" else f"Remove {APP}")
            self.setSubTitle(f"Version {version}")
            layout = QVBoxLayout(self)
            if mode == "install":
                text = (
                    f"<p>{APP} turns this computer into a broadcast TV simulator: "
                    "channels, schedules, commercials, station bumps - you just flip channels.</p>"
                    "<p>Everything it needs is in this installer - the player (mpv), ffmpeg, "
                    "and the on-screen menu. Nothing else gets installed and nothing needs "
                    "administrator rights.</p>"
                )
                if steamos:
                    text += ("<p><b>Steam Deck detected.</b> Gamepad control will be switched on "
                             "and you can add it to your Steam library to launch it from Gaming Mode.</p>")
            else:
                text = (f"<p>This removes {APP} from <code>{prefix}</code>, along with its menu entry, "
                        "autostart entry and Steam library entry.</p>"
                        "<p>Your channel configs, catalog and schedules can be kept for a future install.</p>")
            label = QLabel(text)
            label.setWordWrap(True)
            layout.addWidget(label)
            layout.addStretch()

    class Options(QWizardPage):
        def __init__(self):
            super().__init__()
            self.setTitle("Options")
            self.setSubTitle("Where to put it and how to launch it. The defaults are fine for most people.")
            layout = QVBoxLayout(self)

            layout.addWidget(QLabel("Install location"))
            row = QHBoxLayout()
            self.prefix = QLineEdit(str(prefix))
            browse = QPushButton("Browse...")
            browse.clicked.connect(self._browse)
            row.addWidget(self.prefix)
            row.addWidget(browse)
            layout.addLayout(row)
            hint = QLabel(f"Your channels and schedules live in <code>{_data_dir()}</code> regardless.")
            hint.setWordWrap(True)
            layout.addWidget(hint)
            layout.addSpacing(12)

            self.autostart = QCheckBox("Start when I log in")
            layout.addWidget(self.autostart)

            self.gamepad = QCheckBox("Enable gamepad control (menu navigation, channel up/down)")
            self.gamepad.setChecked(steamos)
            layout.addWidget(self.gamepad)

            self.steam = QCheckBox("Add to my Steam library (launch from Gaming Mode / Big Picture)")
            have_steam = _steam_installed()
            self.steam.setEnabled(have_steam)
            self.steam.setChecked(steamos and have_steam)
            if not have_steam:
                self.steam.setText(self.steam.text() + "  - Steam not found")
            layout.addWidget(self.steam)
            self.steam_note = QLabel("Steam is running. It will be closed while the library entry is "
                                     "added, then reopened.")
            self.steam_note.setStyleSheet("color: #b58900; margin-left: 24px;")
            self.steam_note.setWordWrap(True)
            self.steam_note.setVisible(False)
            layout.addWidget(self.steam_note)
            self.steam.toggled.connect(self._steam_toggled)
            self._steam_toggled(self.steam.isChecked())

            layout.addStretch()
            self.registerField("prefix", self.prefix)
            # Pressing "Install" here is the point of no return.
            self.setCommitPage(True)
            self.setButtonText(QWizard.CommitButton, "Install")

        def _steam_toggled(self, checked):
            self.steam_note.setVisible(bool(checked) and _steam_running())

        def _browse(self):
            chosen = QFileDialog.getExistingDirectory(self, "Install into which folder?", self.prefix.text())
            if chosen:
                self.prefix.setText(str(Path(chosen) / "app") if not chosen.rstrip("/").endswith("/app") else chosen)

        def validatePage(self):
            target = Path(self.prefix.text()).expanduser()
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                probe = target.parent / ".fs42-write-test"
                probe.write_text("ok")
                probe.unlink()
            except OSError as e:
                from PySide6.QtWidgets import QMessageBox

                QMessageBox.warning(self, "Cannot write there", f"{target.parent}\n\n{e}")
                return False
            return True

    class RemoveOptions(QWizardPage):
        def __init__(self):
            super().__init__()
            self.setTitle("Your data")
            self.setSubTitle("What to do with your channels, catalog and schedules.")
            layout = QVBoxLayout(self)
            self.setCommitPage(True)
            self.setButtonText(QWizard.CommitButton, "Remove")
            self.keep = QRadioButton(f"Keep my channels, catalog and schedules in\n{_data_dir()}")
            self.purge = QRadioButton("Delete everything, including my data folder\n"
                                      "(video files stored elsewhere are never touched)")
            self.keep.setChecked(True)
            layout.addWidget(self.keep)
            layout.addWidget(self.purge)
            layout.addStretch()

    class Progress(QWizardPage):
        def __init__(self):
            super().__init__()
            self.setTitle("Installing..." if mode == "install" else "Removing...")
            self.setSubTitle("This takes a moment.")
            # Also a commit page, so the final page offers no way back.
            self.setCommitPage(True)
            self.setButtonText(QWizard.CommitButton, "Next >")
            layout = QVBoxLayout(self)
            self.status = QLabel("")
            layout.addWidget(self.status)
            self.log = QPlainTextEdit()
            self.log.setReadOnly(True)
            self.log.setFont(QFont("Monospace"))
            self.log.setStyleSheet("background: #0d1117; color: #e6e6e6;")
            layout.addWidget(self.log)
            self.done = False
            self.ok = False
            self.process = None

        def initializePage(self):
            self.done = self.ok = False
            self.log.clear()
            wizard = self.wizard()
            if mode == "install":
                options = wizard.page(1)
                chosen = Path(options.prefix.text()).expanduser()
                wizard.final_prefix = chosen
                command = ["sh", str(payload / "install.sh"), "--yes", "--prefix", str(chosen),
                           "--autostart" if options.autostart.isChecked() else "--no-autostart",
                           "--steam" if options.steam.isChecked() else "--no-steam"]
                if options.gamepad.isChecked():
                    command.append("--gamepad")
                if steamos:
                    command.append("--steamos")
            else:
                options = wizard.page(1)
                command = ["sh", str(prefix / "uninstall.sh"), "--yes",
                           "--purge" if options.purge.isChecked() else "--keep-data"]
            self.status.setText("Working...")
            self.process = QProcess(self)
            self.process.setProcessChannelMode(QProcess.MergedChannels)
            self.process.readyReadStandardOutput.connect(self._read)
            self.process.finished.connect(self._finished)
            self.process.setWorkingDirectory(str(Path.home()))
            self.process.start(command[0], command[1:])

        def _read(self):
            data = bytes(self.process.readAllStandardOutput()).decode("utf-8", "replace")
            self.log.moveCursor(self.log.textCursor().MoveOperation.End)
            self.log.insertPlainText(data)
            self.log.moveCursor(self.log.textCursor().MoveOperation.End)

        def _finished(self, code, _status):
            self._read()
            self.done = True
            self.ok = code == 0
            self.status.setText("Done." if self.ok else f"Something went wrong (exit code {code}). "
                                                        "The log above says what.")
            self.completeChanged.emit()

        def isComplete(self):
            return self.done

    class Finished(QWizardPage):
        def __init__(self):
            super().__init__()
            self.setTitle("All set" if mode == "install" else "Removed")
            self.setSubTitle(" ")
            layout = QVBoxLayout(self)
            self.label = QLabel("")
            self.label.setWordWrap(True)
            self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(self.label)
            self.launch = QCheckBox(f"Start {APP} now")
            self.launch.setChecked(True)
            layout.addWidget(self.launch)
            layout.addStretch()
            self.setFinalPage(True)

        def initializePage(self):
            wizard = self.wizard()
            ok = wizard.page(2).ok
            if mode == "install":
                final = wizard.final_prefix
                if ok:
                    self.label.setText(
                        f"<p>{APP} is installed in <code>{final}</code> and in your application menu.</p>"
                        "<p>Press <b>Escape</b> in the video window to add channels, or open "
                        "<code>http://localhost:4242</code> in a browser.</p>"
                        + ("<p>On the Deck: switch to Gaming Mode and look under <b>Non-Steam</b> games.</p>"
                           if steamos and wizard.page(1).steam.isChecked() else "")
                    )
                else:
                    self.label.setText("<p>The install did not finish. Nothing was changed on your system "
                                       "beyond what the log shows.</p>")
                self.launch.setVisible(ok)
                self.launch.setChecked(ok)
            else:
                self.label.setText(f"<p>{APP} has been removed.</p>" if ok else
                                   "<p>The uninstall reported a problem; see the log.</p>")
                self.launch.setVisible(False)
                self.launch.setChecked(False)

    class Wizard(QWizard):
        def __init__(self):
            super().__init__()
            self.setWindowTitle(f"{APP} Setup")
            self.setWizardStyle(QWizard.ModernStyle)
            self.setOption(QWizard.NoBackButtonOnStartPage, True)
            self.setOption(QWizard.NoCancelButtonOnLastPage, True)
            self.final_prefix = prefix
            if icon:
                pix = QPixmap(str(icon))
                self.setWindowIcon(QIcon(pix))
                self.setPixmap(QWizard.LogoPixmap, pix.scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.addPage(Welcome())
            self.addPage(Options() if mode == "install" else RemoveOptions())
            self.addPage(Progress())
            self.addPage(Finished())
            self.resize(640, 480)

        def autopilot(self):
            """Click through with the defaults - for CI and for `--auto`."""
            from PySide6.QtCore import QTimer

            def step():
                page = self.currentPage()
                if isinstance(page, Progress):
                    if not page.done:
                        return
                elif isinstance(page, Finished):
                    page.launch.setChecked(False)
                    self.accept()
                    return
                self.next()

            self._auto_timer = QTimer(self)
            self._auto_timer.timeout.connect(step)
            self._auto_timer.start(300)

        def accept(self):
            finished = self.page(3)
            if mode == "install" and finished.launch.isChecked():
                exe = self.final_prefix / APP
                if exe.exists():
                    subprocess.Popen([str(exe)], cwd=str(Path.home()), start_new_session=True,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            super().accept()

        def reject(self):
            progress = self.page(2)
            if progress.process is not None and progress.process.state() != QProcess.NotRunning:
                return  # don't abandon a half-done install
            super().reject()

    return Wizard


# ------------------------------------------------------------------- entry

def run(args, passthrough) -> int:
    mode = "uninstall" if args.command == "uninstall" or "--uninstall" in passthrough else "install"
    explicit = None
    for index, token in enumerate(passthrough):
        if token == "--payload" and index + 1 < len(passthrough):
            explicit = passthrough[index + 1]
        elif token.startswith("--payload="):
            explicit = token.split("=", 1)[1]

    if os.name == "nt":
        print("On Windows, use the FieldStation42-<version>-windows-x64-setup.exe installer.", file=sys.stderr)
        return 2

    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication.instance() or QApplication([])

    if mode == "install":
        payload = _find_payload(explicit)
        if payload is None:
            QMessageBox.critical(None, f"{APP} Setup",
                                 "This copy is not inside an unpacked release folder, so there is "
                                 "nothing to install from.\n\nRun the FieldStation42-*-installer.run "
                                 "file you downloaded instead.")
            return 2
        prefix = _default_prefix()
    else:
        payload = None
        prefix = _find_installed_prefix()
        if prefix is None:
            QMessageBox.information(None, f"{APP} Setup", f"{APP} does not appear to be installed "
                                    f"(no uninstall.sh next to this executable or in {_default_prefix()}).")
            return 2

    if shutil.which("sh") is None:
        QMessageBox.critical(None, f"{APP} Setup", "No /bin/sh on this system; cannot run the installer script.")
        return 2

    wizard = _build_wizard(mode, payload, prefix)()
    wizard.show()
    if "--auto" in passthrough:
        wizard.autopilot()
    accepted = wizard.exec()
    if not accepted:
        return 1
    return 0 if wizard.page(2).ok else 3
