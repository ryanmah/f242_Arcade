"""On-screen display drawn by mpv itself.

Upstream floated a transparent GLFW/OpenGL window over the video.  That relies
on the window manager compositing one application's output over another's, and
it is created as an *exclusive fullscreen* GL window - a combination Windows
will not composite at all, so the OSD and mpv end up fighting for the display
mode.

mpv can draw this itself on every platform it runs on:

* ``osd-overlay`` takes ASS events, giving positioned and styled text;
* ``overlay-add`` takes a raw BGRA bitmap from a file, giving station logos.

Both are composited into mpv's own video output, so there is no second window,
no z-order problem, and no compositor requirement.
"""

import logging
import os
import time

from fs42 import paths
from fs42.osd.config import (
    HAlignment,
    LogoDisplayConfig,
    StatusDisplayConfig,
    VAlignment,
    VolumeDisplayConfig,
    load_configs,
)
from fs42.osd.backends.base import OSDBackend
from fs42.osd.logo_selector import LogoState

_l = logging.getLogger("OSD.MPV")

# ASS is authored against a fixed virtual canvas and mpv scales it to the real
# video size, so layout maths stay resolution independent.
CANVAS_W = 1920
CANVAS_H = 1080

# Overlay ids. osd-overlay and overlay-add have separate id spaces.
ASS_OVERLAY_ID = 42
LOGO_OVERLAY_ID = 1

MIN_REDRAW_INTERVAL = 1.0 / 30.0


def _ass_color(rgba):
    """ASS wants &HBBGGRR& for colour and 0-255 alpha where 0 is opaque."""
    r, g, b, a = rgba
    return f"&H{b:02X}{g:02X}{r:02X}&", 255 - int(a)


def _escape(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")


def _alignment_tag(halign: HAlignment, valign: VAlignment) -> int:
    """ASS \\an numeric alignment (numpad layout)."""
    column = {HAlignment.LEFT: 1, HAlignment.CENTER: 2, HAlignment.RIGHT: 3}[halign]
    row = {VAlignment.BOTTOM: 0, VAlignment.CENTER: 3, VAlignment.TOP: 6}[valign]
    return row + column


def _anchor(halign: HAlignment, valign: VAlignment, x_margin: float, y_margin: float):
    if halign == HAlignment.LEFT:
        x = x_margin / 2.0 * CANVAS_W
    elif halign == HAlignment.RIGHT:
        x = CANVAS_W - (x_margin / 2.0 * CANVAS_W)
    else:
        x = CANVAS_W / 2.0

    if valign == VAlignment.TOP:
        y = y_margin / 2.0 * CANVAS_H
    elif valign == VAlignment.BOTTOM:
        y = CANVAS_H - (y_margin / 2.0 * CANVAS_H)
    else:
        y = CANVAS_H / 2.0
    return x, y


class _StatusElement:
    """A line of text driven by the player's status payload."""

    def __init__(self, config: StatusDisplayConfig):
        self.config = config
        self.text = ""
        self.time_since_change = float("inf")
        self.last_status = None

    def update(self, dt, status):
        self.time_since_change += dt
        if status is None:
            return
        changed = self.last_status is None or status.get("status") != self.last_status.get("status")
        self.last_status = status

        from collections import defaultdict

        rendered = self.config.format_text.format_map(defaultdict(str, status))
        if rendered != self.text or changed:
            self.time_since_change = -self.config.delay
            if rendered:
                self.text = rendered

    def render(self):
        if not self.text or self.time_since_change >= self.config.display_time:
            return None
        if self.time_since_change < 0:
            return None

        config = self.config
        colour, alpha = _ass_color(config.text_color)
        x, y = _anchor(config.halign, config.valign, config.x_margin, config.y_margin)
        # font_size was pixels against the real framebuffer; scale it onto the
        # virtual canvas so it looks the same at any output resolution.
        size = max(1, int(config.font_size * config.expansion_factor * (CANVAS_H / 1080.0)))

        tags = [
            f"\\an{_alignment_tag(config.halign, config.valign)}",
            f"\\pos({x:.1f},{y:.1f})",
            f"\\fs{size}",
            f"\\1c{colour}",
            f"\\1a&H{alpha:02X}&",
            "\\bord2",
            "\\shad1",
        ]
        if config.font:
            tags.append(f"\\fn{os.path.splitext(os.path.basename(config.font))[0]}")
        return "{" + "".join(tags) + "}" + _escape(self.text)


class _VolumeElement:
    """A horizontal meter, drawn with ASS vector commands."""

    def __init__(self, config: VolumeDisplayConfig):
        self.config = config
        self.level = 0.0
        self.time_since_change = float("inf")

    def update(self, dt, volume):
        self.time_since_change += dt
        if not volume:
            return
        raw = volume.get("level")
        if raw is None:
            import re

            match = re.search(r"\d+(?:\.\d+)?", str(volume.get("volume", "")))
            if not match:
                return
            raw = float(match.group())
        self.level = max(0.0, min(1.0, float(raw) / 100.0))
        self.time_since_change = 0.0

    def render(self):
        if self.time_since_change >= self.config.display_time:
            return None

        config = self.config
        width = config.width * CANVAS_W
        height = config.height * CANVAS_H

        if config.halign == HAlignment.LEFT:
            x = config.x_margin / 2.0 * CANVAS_W
        elif config.halign == HAlignment.RIGHT:
            x = CANVAS_W - width - (config.x_margin / 2.0 * CANVAS_W)
        else:
            x = (CANVAS_W - width) / 2.0

        if config.valign == VAlignment.TOP:
            y = config.y_margin / 2.0 * CANVAS_H
        elif config.valign == VAlignment.BOTTOM:
            y = CANVAS_H - height - (config.y_margin / 2.0 * CANVAS_H)
        else:
            y = (CANVAS_H - height) / 2.0

        colour, alpha = _ass_color(config.color)
        border = max(1, int(config.border_thickness))
        padding = config.padding * CANVAS_W
        filled = max(0.0, (width - 2 * padding) * self.level)
        inner_h = max(0.0, height - 2 * padding)

        prefix = "{" + f"\\an7\\pos(0,0)\\1c{colour}\\1a&H{alpha:02X}&\\3c{colour}\\bord{border}\\shad0" + "}"

        # Outline as a stroked rectangle, then the filled portion.
        outline = (
            "{\\p1}"
            f"m {x:.0f} {y:.0f} l {x + width:.0f} {y:.0f} "
            f"l {x + width:.0f} {y + height:.0f} l {x:.0f} {y + height:.0f}"
            "{\\p0}"
        )
        parts = [prefix, "{\\1a&HFF&}", outline]
        if filled > 0 and inner_h > 0:
            fx, fy = x + padding, y + padding
            parts.append("{\\1a&H" + f"{alpha:02X}" + "&\\bord0}{\\p1}")
            parts.append(
                f"m {fx:.0f} {fy:.0f} l {fx + filled:.0f} {fy:.0f} "
                f"l {fx + filled:.0f} {fy + inner_h:.0f} l {fx:.0f} {fy + inner_h:.0f}"
            )
            parts.append("{\\p0}")
        return "".join(parts)


class _LogoElement:
    """Station logo, composited by mpv as a BGRA bitmap."""

    def __init__(self, mpv, config: LogoDisplayConfig, overlay_id):
        self.mpv = mpv
        self.state = LogoState(config)
        self.overlay_id = overlay_id
        self._shown_key = None
        self._visible = False
        self._frames = []
        self._durations = []
        self._frame_index = 0
        self._frame_timer = 0.0

    def update(self, dt, status):
        self.state.update(dt, status)
        if self._frames and len(self._frames) > 1:
            self._frame_timer += dt
            duration = self._durations[self._frame_index] or 0.1
            if self._frame_timer >= duration:
                self._frame_timer -= duration
                self._frame_index = (self._frame_index + 1) % len(self._frames)
                self._shown_key = None  # force a redraw of the new frame

    def draw(self):
        if not self.state.visible:
            self._hide()
            return

        path = self.state.current_logo_path
        width, height, x_margin, y_margin, halign, valign = self.state.geometry()

        try:
            video_w = int(self.mpv.width or CANVAS_W)
            video_h = int(self.mpv.height or CANVAS_H)
        except Exception:
            video_w, video_h = CANVAS_W, CANVAS_H

        target_w = max(1, int(width * video_w))
        target_h = max(1, int(height * video_h))

        if self._prepare(path, target_w, target_h) is False:
            self._hide()
            return

        frame = self._frames[self._frame_index] if self._frames else None
        if frame is None:
            self._hide()
            return

        if halign == HAlignment.LEFT:
            x = int(x_margin / 2.0 * video_w)
        elif halign == HAlignment.RIGHT:
            x = int(video_w - target_w - (x_margin / 2.0 * video_w))
        else:
            x = int((video_w - target_w) / 2)

        if valign == VAlignment.TOP:
            y = int(y_margin / 2.0 * video_h)
        elif valign == VAlignment.BOTTOM:
            y = int(video_h - target_h - (y_margin / 2.0 * video_h))
        else:
            y = int((video_h - target_h) / 2)

        key = (frame, x, y, target_w, target_h)
        if key == self._shown_key:
            return
        try:
            self.mpv.command(
                "overlay-add",
                self.overlay_id,
                x,
                y,
                str(frame),
                0,
                "bgra",
                target_w,
                target_h,
                target_w * 4,
            )
            self._shown_key = key
            self._visible = True
        except Exception as e:
            _l.debug("overlay-add failed: %s", e)

    def _prepare(self, path, target_w, target_h):
        """Render the logo to BGRA files mpv can map, resizing as needed."""
        cache_key = (path, target_w, target_h, self.state.alpha)
        if getattr(self, "_cache_key", None) == cache_key:
            return True

        try:
            from PIL import Image
        except ImportError:
            _l.warning("Pillow is required to draw station logos")
            return False

        try:
            frames = []
            durations = []
            with Image.open(path) as image:
                frame_count = getattr(image, "n_frames", 1)
                for index in range(frame_count):
                    image.seek(index)
                    frame = image.convert("RGBA").resize((target_w, target_h), Image.LANCZOS)
                    if self.state.alpha < 1.0:
                        alpha_band = frame.getchannel("A").point(
                            lambda v: int(v * self.state.alpha)
                        )
                        frame.putalpha(alpha_band)
                    # mpv wants premultiplied BGRA.
                    red, green, blue, alpha = frame.split()
                    premultiplied = Image.merge(
                        "RGBA",
                        (
                            blue.point(lambda v: v),
                            green.point(lambda v: v),
                            red.point(lambda v: v),
                            alpha,
                        ),
                    )
                    target = paths.cache(f"osd_logo_{self.overlay_id}_{index}.bgra")
                    target.write_bytes(premultiplied.tobytes())
                    frames.append(target)
                    durations.append(max(image.info.get("duration", 100) / 1000.0, 0.01))
        except Exception as e:
            _l.warning("Could not prepare logo %s: %s", path, e)
            return False

        self._frames = frames
        self._durations = durations
        self._frame_index = 0
        self._frame_timer = 0.0
        self._cache_key = cache_key
        self._shown_key = None
        return True

    def _hide(self):
        if not self._visible:
            return
        try:
            self.mpv.command("overlay-remove", self.overlay_id)
        except Exception:
            pass
        self._visible = False
        self._shown_key = None

    def close(self):
        self._hide()


class MpvOSD(OSDBackend):
    def __init__(self, mpv, config_entries=None):
        self.mpv = mpv
        self.status_elements = []
        self.volume_elements = []
        self.logo_elements = []
        self._last_tick = time.monotonic()
        self._last_payload = None

        # Consume whatever volume the previous run left behind so the meter
        # does not flash on startup showing stale state.
        try:
            from fs42 import ipc

            ipc.pop_volume()
        except Exception:
            pass

        for index, (kind, config) in enumerate(config_entries or load_configs()):
            if kind == "status":
                self.status_elements.append(_StatusElement(config))
            elif kind == "volume":
                self.volume_elements.append(_VolumeElement(config))
            elif kind == "logo":
                self.logo_elements.append(
                    _LogoElement(mpv, config, LOGO_OVERLAY_ID + len(self.logo_elements))
                )

    def tick(self):
        now = time.monotonic()
        dt = now - self._last_tick
        if dt < MIN_REDRAW_INTERVAL:
            return
        self._last_tick = now

        from fs42 import ipc

        status = ipc.get_status() or None
        volume = ipc.pop_volume()

        for element in self.status_elements:
            element.update(dt, status)
        for element in self.volume_elements:
            element.update(dt, volume)
        for element in self.logo_elements:
            element.update(dt, status)
            element.draw()

        self._draw_ass()

    def _draw_ass(self):
        parts = []
        for element in self.volume_elements:
            rendered = element.render()
            if rendered:
                parts.append(rendered)
        # Status text last so it sits above the meter, matching upstream's
        # explicit "StatusDisplay on top" draw order.
        for element in self.status_elements:
            rendered = element.render()
            if rendered:
                parts.append(rendered)

        payload = "\n".join(parts)
        if payload == self._last_payload:
            return
        self._last_payload = payload
        try:
            self.mpv.command(
                "osd-overlay",
                ASS_OVERLAY_ID,
                "ass-events",
                payload,
                CANVAS_W,
                CANVAS_H,
                0,
            )
        except Exception as e:
            _l.debug("osd-overlay failed: %s", e)

    def close(self):
        for element in self.logo_elements:
            element.close()
        try:
            self.mpv.command("osd-overlay", ASS_OVERLAY_ID, "none", "", 0, 0, 0)
        except Exception:
            pass
