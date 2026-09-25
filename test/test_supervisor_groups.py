import os
import sys
import time

import pytest

from fs42.app.supervisor import Child

pytestmark = pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX")

# A "player" that starts a long-lived helper (standing in for mpv) and then
# either waits or ignores SIGTERM the way a busy main loop does.
SCRIPT = r"""
import signal, subprocess, sys, time
helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(sys.argv[1], "w").write(str(helper.pid))
if sys.argv[2] == "stubborn":
    signal.signal(signal.SIGTERM, lambda *a: None)
time.sleep(60)
"""


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); it has stopped running though.
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


def _start(tmp_path, mode):
    pid_file = tmp_path / "helper.pid"
    child = Child("player", [sys.executable, "-c", SCRIPT, str(pid_file), mode])
    child.start()
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text():
            break
        time.sleep(0.05)
    return child, int(pid_file.read_text())


@pytest.mark.parametrize("mode", ["polite", "stubborn"])
def test_stopping_a_child_stops_what_it_started(tmp_path, mode):
    child, helper = _start(tmp_path, mode)
    assert _alive(helper)
    child.stop(timeout=1.0)
    for _ in range(40):
        if not _alive(helper):
            break
        time.sleep(0.05)
    assert not _alive(helper)


def test_a_child_that_dies_does_not_leave_its_helper_running(tmp_path):
    child, helper = _start(tmp_path, "polite")
    child.process.kill()
    code = child.process.wait()
    child.note_exit(code)
    for _ in range(40):
        if not _alive(helper):
            break
        time.sleep(0.05)
    assert not _alive(helper)
