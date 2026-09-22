"""Small cross-platform shims.

Everything here exists because a call upstream makes is either unavailable or
silently wrong on Windows.  Keeping them in one place makes it obvious what
the porting surface actually is.
"""

import errno
import logging
import os
import subprocess
import sys

_l = logging.getLogger("PLATFORM")

IS_WINDOWS = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"

# subprocess flag that keeps a console window from flashing over fullscreen
# video every time we shell out to ffprobe.
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


def no_window_kwargs() -> dict:
    """subprocess kwargs that suppress a console flash on Windows."""
    if not IS_WINDOWS:
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {"creationflags": CREATE_NO_WINDOW, "startupinfo": startupinfo}


def run_hidden(cmd, **kwargs):
    """subprocess.run with no console flash and text output by default."""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.update(no_window_kwargs())
    return subprocess.run(cmd, **kwargs)


def popen_hidden(cmd, **kwargs):
    kwargs.update(no_window_kwargs())
    return subprocess.Popen(cmd, **kwargs)


def pid_alive(pid: int) -> bool:
    """Is this process id currently running?

    ``os.kill(pid, 0)`` is the POSIX idiom, but on Windows ``os.kill`` maps to
    ``TerminateProcess`` for every signal except CTRL_C_EVENT/CTRL_BREAK_EVENT
    - so the usual liveness probe *kills* the process it was meant to test.
    Upstream does exactly that in the ticker and now-playing single-instance
    guards.
    """
    if pid is None or pid <= 0:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
        except OSError as e:
            return e.errno == errno.EPERM
        return True

    import ctypes
    from ctypes import wintypes

    SYNCHRONIZE = 0x00100000
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(
        SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return False
    finally:
        kernel32.CloseHandle(handle)


def mpv_ipc_name(suffix=None) -> str:
    """Endpoint name to hand to python-mpv-jsonipc.

    python_mpv_jsonipc prefixes ``\\\\.\\pipe\\`` itself on Windows, so we must
    pass a bare name there - passing a full pipe path yields a doubled prefix
    and a connection that never opens.  On POSIX it wants a real filesystem
    path.
    """
    token = suffix if suffix is not None else os.getpid()
    if IS_WINDOWS:
        return f"fs42-mpv-{token}"
    from fs42 import paths

    return str(paths.cache(f"mpv-{token}.socket"))


def terminate_ipc_endpoint(endpoint: str):
    """Clean up a stale unix socket.  No-op for Windows named pipes."""
    if IS_WINDOWS or not endpoint:
        return
    try:
        os.unlink(endpoint)
    except OSError:
        pass


class InstanceLock:
    """Advisory single-instance lock backed by a real file lock.

    Replaces the tempdir PID files upstream uses, which race and (see
    ``pid_alive``) probe liveness with a call that kills processes on Windows.
    """

    def __init__(self, path):
        self.path = str(path)
        self._handle = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            self._handle = open(self.path, "a+")
        except OSError as e:
            _l.warning("Could not open instance lock %s: %s", self.path, e)
            return True  # fail open - a lock problem should not block playback

        try:
            if IS_WINDOWS:
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._handle.close()
            self._handle = None
            return False

        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(str(os.getpid()))
        self._handle.flush()
        return True

    def release(self):
        if self._handle is None:
            return
        try:
            if IS_WINDOWS:
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self):
        self.acquired = self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def install_shutdown_handlers(callback):
    """Register the platform's 'please stop' signals.

    SIGTERM can be registered on Windows but is never delivered; SIGBREAK is
    the closest equivalent and does arrive for console apps.
    """
    import signal

    names = ["SIGINT"]
    names.append("SIGBREAK" if IS_WINDOWS else "SIGTERM")
    for name in names:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, lambda s, f: callback(s))
        except (ValueError, OSError) as e:
            # Not the main thread, or unsupported on this platform.
            _l.debug("Could not install handler for %s: %s", name, e)


def atomic_write_text(path, text: str, encoding="utf-8"):
    """Write a file so readers never observe a partial result.

    ``os.replace`` is atomic on both NTFS and POSIX.  Upstream truncates and
    rewrites in place, which produces torn reads on Linux and PermissionError
    on Windows when a reader has the file open.
    """
    import tempfile

    path = str(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".fs42-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
