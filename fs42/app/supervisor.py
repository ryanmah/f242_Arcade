"""Process supervisor.

The executable launches itself again, once per role, and keeps those children
alive.  Under PyInstaller onefile this is deliberate: a child launched from
the same frozen executable reuses the parent's extraction directory rather
than unpacking its own copy, so the environment must be inherited rather than
rebuilt.
"""

import logging
import os
import subprocess
import sys
import threading
import time

from fs42 import ipc
from fs42 import paths
from fs42 import platform_compat

_l = logging.getLogger("SUPERVISOR")

POLL_INTERVAL = 0.5
GRACE_SECONDS = 5.0
TERMINATE_SECONDS = 2.0
BACKOFF_START = 1.0
BACKOFF_CAP = 30.0
HEALTHY_AFTER = 60.0


class Child:
    def __init__(self, role, argv, restart="always", max_failures=None):
        self.role = role
        self.argv = argv
        self.restart = restart
        self.max_failures = max_failures
        self.process = None
        self.pgid = None
        self.started_at = 0.0
        self.backoff = BACKOFF_START
        self.retry_at = 0.0
        self.failures = 0
        self.stopped = False

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self):
        _l.info("Starting %s", self.role)
        # env=None on purpose: inherit, so PyInstaller's onefile bootloader
        # sees _PYI_PARENT_PROCESS_LEVEL and reuses this process's extraction
        # directory instead of unpacking the whole payload again.
        kwargs = {}
        if platform_compat.IS_WINDOWS:
            # Stay in the parent's console so Ctrl+C still reaches everyone,
            # but do not pop a window for the headless workers.
            if self.role in ("api", "cli"):
                kwargs["creationflags"] = platform_compat.CREATE_NO_WINDOW
        else:
            # Each child leads its own process group, and everything it
            # starts (mpv, the menu, the guide, the overlays) joins that
            # group.  Stopping the child then stops all of them: before this
            # a player that was killed rather than exiting left mpv behind,
            # fullscreen and on top of everything (seen on SteamOS).
            kwargs["start_new_session"] = True
        self.process = subprocess.Popen(self.argv, **kwargs)
        self.pgid = self.process.pid if not platform_compat.IS_WINDOWS else None
        self.started_at = time.monotonic()

    def _signal_group(self, sig) -> bool:
        """Send a signal to the child's whole process group (POSIX only).

        Returns False once nothing is left in the group."""
        if not self.pgid:
            return False
        try:
            os.killpg(self.pgid, sig)
            return True
        except ProcessLookupError:
            return False
        except Exception as e:
            _l.debug("Could not signal %s's process group: %s", self.role, e)
            return False

    def reap_group(self, timeout=TERMINATE_SECONDS):
        """Stop anything the child left running after it went away."""
        if not self.pgid:
            return
        import signal

        if not self._signal_group(signal.SIGTERM):
            self.pgid = None
            return
        _l.info("Stopping processes %s left behind", self.role)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.1)
            if not self._signal_group(0):
                self.pgid = None
                return
        self._signal_group(signal.SIGKILL)
        self.pgid = None

    def note_exit(self, code):
        uptime = time.monotonic() - self.started_at
        if uptime >= HEALTHY_AFTER:
            self.backoff = BACKOFF_START
            self.failures = 0
        if code == 0:
            _l.info("%s exited cleanly after %.0fs", self.role, uptime)
        else:
            self.failures += 1
            _l.warning("%s exited with code %s after %.0fs", self.role, code, uptime)
        self.process = None
        # A player that crashed must not leave its mpv running under the
        # one that replaces it.
        self.reap_group()

    def should_restart(self, code) -> bool:
        if self.stopped or self.restart == "never":
            return False
        if self.restart == "on-failure":
            if code == 0:
                return False
            if self.max_failures is not None and self.failures >= self.max_failures:
                _l.error(
                    "%s failed %d times; giving up on it (the rest of the app keeps running)",
                    self.role,
                    self.failures,
                )
                return False
        return True

    def schedule_restart(self):
        self.retry_at = time.monotonic() + self.backoff
        self.backoff = min(self.backoff * 2, BACKOFF_CAP)

    def stop(self, timeout=TERMINATE_SECONDS):
        self.stopped = True
        if not self.alive:
            self.reap_group(timeout)
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _l.warning("%s did not stop; killing it", self.role)
            try:
                self.process.kill()
                self.process.wait(timeout=timeout)
            except Exception:
                pass
        except Exception as e:
            _l.debug("Error stopping %s: %s", self.role, e)
        self.reap_group(timeout)


class Supervisor:
    def __init__(self, args, passthrough):
        self.args = args
        self.passthrough = [a for a in passthrough if a not in ("run",)]
        self.children = []
        self.shutdown = threading.Event()
        self.lock = None

    # ---------------------------------------------------------------- setup

    def _self_argv(self, role):
        extra = list(self.passthrough)
        if paths.IS_FROZEN:
            base = [sys.executable]
        else:
            base = [sys.executable, "-m", "fs42.app"]
        return base + [f"--fs42-role={role}"] + extra

    def _preflight(self) -> bool:
        paths.first_run_seed()
        ipc.init_db()
        ipc.clear_shutdown()

        mpv = paths.bin_path("mpv")
        if mpv is None:
            _l.error(
                "Could not find mpv. Install it, or set \"mpv_path\" in %s",
                paths.confs("main_config.json"),
            )
        else:
            _l.info("Using mpv at %s", mpv)

        stations = [p.name for p in paths.confs().glob("*.json") if p.name != "main_config.json"]
        if not stations:
            _l.warning(
                "No channels configured yet. Open http://localhost:%s to set one up; "
                "examples are in %s",
                self._server_port(),
                paths.confs("examples"),
            )
        return True

    def _server_port(self):
        try:
            from fs42.station_manager import StationManager

            return StationManager().server_conf.get("server_port", 4242)
        except Exception:
            return 4242

    # ------------------------------------------------------------- run loop

    def run(self) -> int:
        self.lock = platform_compat.InstanceLock(paths.runtime("fs42.lock"))
        if not self.lock.acquire():
            _l.error("FieldStation42 is already running.")
            return 1

        try:
            self._preflight()
        except Exception as e:
            _l.exception(e)
            _l.error("Startup checks failed.")
            self.lock.release()
            return 1

        platform_compat.install_shutdown_handlers(self._on_signal)

        self.children.append(Child("player", self._self_argv("player"), restart="always"))
        if not getattr(self.args, "no_server", False):
            self.children.append(Child("api", self._self_argv("api"), restart="always"))

        for child in self.children:
            child.start()

        _l.info("FieldStation42 is running. Web console: http://localhost:%s", self._server_port())

        last_prune = time.monotonic()
        try:
            while not self.shutdown.is_set():
                time.sleep(POLL_INTERVAL)

                if ipc.shutdown_requested():
                    _l.info("Shutdown requested by a running component.")
                    break

                restart = ipc.restart_requested()
                if restart:
                    self._restart_everything(restart.get("reason", "requested"))
                    continue

                now = time.monotonic()
                if now - last_prune > 30:
                    ipc.prune()
                    last_prune = now

                for child in self.children:
                    if child.alive or child.stopped:
                        continue
                    if child.process is not None:
                        code = child.process.poll()
                        child.note_exit(code)
                        if not child.should_restart(code):
                            child.stopped = True
                            continue
                        child.schedule_restart()
                    if child.retry_at and now >= child.retry_at:
                        child.retry_at = 0.0
                        child.start()

                if all(c.stopped for c in self.children):
                    _l.info("All components have stopped.")
                    break
        except KeyboardInterrupt:
            _l.info("Interrupted.")
        finally:
            self._shutdown_children()
            self.lock.release()
        return 0

    def _restart_everything(self, reason):
        """Stop every component and start them again, re-reading the data
        folder setting on the way - the one thing a running process cannot
        change about itself."""
        _l.info("Restart requested (%s).", reason)
        ipc.clear_restart()
        self._shutdown_children()
        ipc.close()
        paths.reset_cached_roots()
        try:
            self._preflight()
        except Exception as e:
            _l.exception(e)
            _l.error("Startup checks failed after restart; carrying on with the previous data folder.")
        info = paths.data_root_info()
        _l.info("Data folder: %s (%s)", info["current"], info["source"])
        for child in self.children:
            child.stopped = False
            child.retry_at = 0.0
            child.backoff = BACKOFF_START
            child.start()
        _l.info("FieldStation42 restarted. Web console: http://localhost:%s", self._server_port())

    def _on_signal(self, signum):
        _l.info("Received signal %s - shutting down", signum)
        self.shutdown.set()

    def _shutdown_children(self):
        # Cooperative first: tell everyone to wind down and give them a moment.
        ipc.request_shutdown("supervisor")
        deadline = time.monotonic() + GRACE_SECONDS
        while time.monotonic() < deadline:
            if all(not c.alive for c in self.children):
                break
            time.sleep(0.2)

        for child in self.children:
            child.stop()
        ipc.set_status({"status": "stopped", "network_name": "", "channel_number": -1})
        _l.info("Shutdown complete.")
