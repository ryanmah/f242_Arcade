"""Thin ffprobe/ffmpeg wrappers.

Replaces the ``ffmpeg-python`` dependency.  Two reasons:

* ``ffmpeg-python`` offers no way to pass ``creationflags``, so every probe
  popped a console window over fullscreen video on Windows;
* its absence made ``fs42/media_processor.py`` call ``sys.exit(1)`` at import
  time, which in a windowed build means the app vanishes with no explanation.

Binaries are located through ``fs42.paths.bin_path`` so a bundled copy is
preferred over whatever happens to be on PATH.
"""

import json
import logging
import re

from fs42 import paths
from fs42 import platform_compat

_l = logging.getLogger("FFMPEG")


class FFmpegError(RuntimeError):
    """A probe or transcode failed.  ``stderr`` carries ffmpeg's own message."""

    def __init__(self, message, stderr=""):
        super().__init__(message)
        self.stderr = stderr or ""


class FFmpegMissing(FFmpegError):
    """Neither a bundled nor a system binary could be found."""


def _tool(name) -> str:
    found = paths.bin_path(name)
    if found is None:
        raise FFmpegMissing(
            f"Could not find {name}. Install ffmpeg, or set \"{name}_path\" in "
            f"{paths.confs('main_config.json')}"
        )
    return str(found)


def available(name="ffprobe") -> bool:
    try:
        _tool(name)
        return True
    except FFmpegMissing:
        return False


def probe(path, timeout=60) -> dict:
    """ffprobe -show_format -show_streams, as a dict."""
    command = [
        _tool("ffprobe"),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = platform_compat.run_hidden(command, timeout=timeout)
    if result.returncode != 0:
        raise FFmpegError(f"ffprobe failed for {path}", result.stderr)
    try:
        return json.loads(result.stdout or "{}")
    except ValueError as e:
        raise FFmpegError(f"Could not parse ffprobe output for {path}: {e}", result.stderr)


def probe_duration(path, timeout=30):
    """Container duration in seconds, or None."""
    command = [
        _tool("ffprobe"),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = platform_compat.run_hidden(command, timeout=timeout)
        if result.returncode != 0:
            return None
        duration = float((result.stdout or "").strip())
        return duration if duration > 0 else None
    except (FFmpegMissing, ValueError, OSError):
        return None


def probe_chapters(path, timeout=60) -> list:
    command = [
        _tool("ffprobe"),
        "-v", "quiet",
        "-print_format", "json",
        "-show_chapters",
        str(path),
    ]
    result = platform_compat.run_hidden(command, timeout=timeout)
    try:
        return json.loads(result.stdout or "{}").get("chapters", [])
    except ValueError:
        return []


_BLACKDETECT = re.compile(
    r"black_start:(?P<start>[\d.]+)\s+black_end:(?P<end>[\d.]+)\s+black_duration:(?P<duration>[\d.]+)"
)


def detect_black_frames(path, min_duration=0.05, pixel_threshold=0.10, ratio_threshold=0.98, timeout=None):
    """Return the midpoint (seconds) of every detected black segment.

    Equivalent to the ffmpeg-python filter graph upstream built, but shelled
    directly so the console window can be suppressed.
    """
    command = [
        _tool("ffmpeg"),
        "-hide_banner",
        "-nostdin",
        "-i", str(path),
        "-vf", f"blackdetect=d={min_duration}:pix_th={pixel_threshold}:pic_th={ratio_threshold}",
        "-f", "null",
        "-",
    ]
    result = platform_compat.run_hidden(command, timeout=timeout)
    midpoints = []
    for match in _BLACKDETECT.finditer(result.stderr or ""):
        start = float(match.group("start"))
        end = float(match.group("end"))
        midpoints.append((start + end) / 2)
    return midpoints
