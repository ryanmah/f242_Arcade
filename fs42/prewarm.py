"""Keep the next thing every channel will play warm in the OS file cache.

The first tune to a channel is slower than the second: mpv has to read the
head of the file (and, for MP4s written with the index at the end, the
tail) off the disk before the first frame shows, and on an external drive
that is a seek or two plus possibly a spin-up.  The second time, the OS has
those bytes cached.

This thread does that first read ahead of time.  Every few seconds it asks
the schedule what each channel is playing now and what comes next, and
reads the first and last couple of megabytes of any file it has not
touched yet.  Nothing is decoded and nothing is kept in memory here; the
OS cache does the remembering.  Off with ``"prewarm_media": false``.
"""

import datetime
import logging
import os
import threading
import time
from collections import OrderedDict

from fs42 import paths

_l = logging.getLogger("PREWARM")

HEAD_BYTES = 3 * 1024 * 1024
TAIL_BYTES = 3 * 1024 * 1024
INTERVAL_SECONDS = 15
REMEMBER = 400          # paths already warmed, most recent last


def warm_file(path, head=HEAD_BYTES, tail=TAIL_BYTES) -> bool:
    """Read the head and tail of one file so the next open finds them cached."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb", buffering=0) as f:
            remaining = min(head, size)
            while remaining > 0:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            if size > head:
                f.seek(max(head, size - tail))
                while f.read(1024 * 1024):
                    pass
        return True
    except OSError as e:
        _l.debug("could not warm %s: %s", path, e)
        return False


def upcoming_files(now=None, lookahead=2) -> list:
    """Paths each channel plays now and next, per the schedules in memory."""
    from fs42.liquid_manager import LiquidManager
    from fs42.station_manager import StationManager

    now = now or datetime.datetime.now()
    liquid = LiquidManager()
    found = []
    for station in StationManager().stations:
        if station.get("network_type") not in ("standard", "loop"):
            continue
        name = station["network_name"]
        try:
            point = liquid.get_play_point(name, now)
        except Exception:
            continue
        if point is None or not getattr(point, "plan", None):
            continue
        for entry in point.plan[point.index:point.index + lookahead]:
            path = getattr(entry, "path", None)
            if path and not getattr(entry, "is_stream", False):
                found.append(paths.locate_media(path))
    return found


class Prewarmer:
    def __init__(self, interval=INTERVAL_SECONDS):
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self.warmed = OrderedDict()

    def start(self):
        self._thread = threading.Thread(target=self._run, name="fs42-prewarm", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def once(self) -> int:
        """Warm whatever is new; returns how many files were read."""
        count = 0
        for path in upcoming_files():
            if self._stop.is_set():
                break
            if path in self.warmed:
                self.warmed.move_to_end(path)
                continue
            if warm_file(path):
                count += 1
            self.warmed[path] = time.monotonic()
            while len(self.warmed) > REMEMBER:
                self.warmed.popitem(last=False)
        if count:
            _l.debug("warmed %d file(s)", count)
        return count

    def _run(self):
        # Let the player get its first channel up before competing for the disk.
        time.sleep(3)
        while not self._stop.is_set():
            try:
                self.once()
            except Exception as e:
                _l.debug("prewarm pass failed: %s", e)
            self._stop.wait(self.interval)
