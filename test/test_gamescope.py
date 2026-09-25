import os
import subprocess
import shutil

import pytest

from fs42 import gamescope


def test_inactive_outside_gamescope(monkeypatch):
    monkeypatch.delenv("GAMESCOPE_WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    assert not gamescope.active()
    assert gamescope.mark_overlay(object()) is False


@pytest.mark.skipif(not shutil.which("Xvfb") or not shutil.which("xprop"), reason="needs Xvfb and xprop")
def test_overlay_property_lands_on_the_window(monkeypatch):
    pytest.importorskip("PySide6")
    display = ":93"
    server = subprocess.Popen(["Xvfb", display], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import time

        time.sleep(1)
        script = (
            "import os\n"
            "from PySide6.QtWidgets import QApplication, QWidget\n"
            "app = QApplication([])\n"
            "w = QWidget()\n"
            "from fs42 import gamescope\n"
            "assert gamescope.mark_overlay(w)\n"
            "print(int(w.winId()))\n"
        )
        env = dict(os.environ, DISPLAY=display, QT_QPA_PLATFORM="xcb", XDG_CURRENT_DESKTOP="gamescope")
        env.pop("WAYLAND_DISPLAY", None)
        import sys

        wid = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60)
        assert wid.returncode == 0, wid.stderr
        # The window is gone with its process; re-run and query while alive instead.
        script2 = script.replace("print(int(w.winId()))\n",
                                 "import subprocess\n"
                                 "print(subprocess.run(['xprop', '-id', str(int(w.winId())), 'GAMESCOPE_EXTERNAL_OVERLAY'],"
                                 " capture_output=True, text=True).stdout)\n")
        out = subprocess.run([sys.executable, "-c", script2], env=env, capture_output=True, text=True, timeout=60)
        assert "GAMESCOPE_EXTERNAL_OVERLAY(CARDINAL) = 1" in out.stdout, out.stdout + out.stderr
    finally:
        server.terminate()
