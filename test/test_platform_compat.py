"""Tests for the cross-platform shims."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from fs42 import platform_compat


def test_pid_alive_recognizes_this_process():
    assert platform_compat.pid_alive(os.getpid()) is True


def test_pid_alive_rejects_a_dead_process():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    # A pid that has been reaped must read as dead, and - the whole point of
    # this helper - probing it must not terminate anything.
    assert platform_compat.pid_alive(child.pid) is False


def test_pid_alive_handles_nonsense():
    assert platform_compat.pid_alive(0) is False
    assert platform_compat.pid_alive(-1) is False
    assert platform_compat.pid_alive(None) is False


def test_mpv_ipc_name_shape():
    name = platform_compat.mpv_ipc_name("test")
    if os.name == "nt":
        # python-mpv-jsonipc adds the pipe prefix itself; adding it here would
        # produce \\.\pipe\\\.\pipe\... and never connect.
        assert not name.startswith("\\\\.\\pipe\\")
        assert "fs42-mpv" in name
    else:
        assert name.endswith("test.socket")


def test_atomic_write_replaces_whole_file():
    with tempfile.TemporaryDirectory() as scratch:
        target = Path(scratch) / "state.json"
        platform_compat.atomic_write_text(target, "first")
        platform_compat.atomic_write_text(target, "second")
        assert target.read_text() == "second"
        # No temporary files left behind.
        assert [p.name for p in Path(scratch).iterdir()] == ["state.json"]


def test_instance_lock_is_exclusive():
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "app.lock"
        first = platform_compat.InstanceLock(path)
        assert first.acquire() is True
        try:
            second = platform_compat.InstanceLock(path)
            assert second.acquire() is False
        finally:
            first.release()

        third = platform_compat.InstanceLock(path)
        assert third.acquire() is True
        third.release()


def test_run_hidden_captures_output():
    result = platform_compat.run_hidden([sys.executable, "-c", "print('hello')"])
    assert result.returncode == 0
    assert "hello" in result.stdout
