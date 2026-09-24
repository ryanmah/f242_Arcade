"""The menu's copy of the CRT effect: scanlines and noise on a layer above
the pages.  Same numbers as the player's shader (``fs42.video_effects``)."""

import random

import numpy as np
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtWidgets import QWidget

from fs42 import video_effects

NOISE_TILE = 256
NOISE_FPS = 12


class EffectsOverlay(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.NoFocus)
        self.values = video_effects.load()
        self._lines = None
        self._noise = []
        self._noise_index = 0
        self._offset = (0, 0)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.apply(self.values)

    def apply(self, values):
        """Show these settings now (they need not be saved)."""
        self.values = video_effects.clean(values)
        self._lines = None
        self._noise = []
        if self.values["noise_opacity"] > 0:
            self.timer.start(int(1000 / NOISE_FPS))
        else:
            self.timer.stop()
        self.update()

    # ------------------------------------------------------------ tiles

    def _line_tile(self):
        """One period of the line pattern, from the same profile as the shader."""
        if self._lines is None:
            profile = np.array(video_effects.line_profile(self.values), dtype=np.float32)
            n = len(profile)
            pattern = self.values["scanline_pattern"]
            if pattern == "horizontal":
                dark = np.repeat(profile[:, None], 8, axis=1)           # n rows x 8 cols
            elif pattern == "vertical":
                dark = np.repeat(profile[None, :], 8, axis=0)           # 8 rows x n cols
            else:
                dark = np.maximum(profile[:, None], profile[None, :])   # n x n
            alpha = (dark * self.values["scanline_opacity"] * 255).astype(np.uint8)
            h, w = alpha.shape
            zeros = np.zeros_like(alpha)
            bgra = np.dstack([zeros, zeros, zeros, alpha]).copy()
            image = QImage(bgra.data, w, h, w * 4, QImage.Format_ARGB32)
            self._lines = QPixmap.fromImage(image.copy())
        return self._lines

    def _noise_tile(self):
        if not self._noise:
            grain = max(1, self.values["noise_grain"])
            cells = NOISE_TILE // grain
            alpha = self.values["noise_opacity"]
            for _ in range(4):
                # Light and dark speckle, like the shader's (n - 0.5): white
                # where the sample is above the middle, black below, with
                # the alpha growing with the distance from the middle.
                v = np.random.random((cells, cells)).astype(np.float32)
                light = (v > 0.5).astype(np.uint8) * 255
                a = (np.abs(v - 0.5) * 2 * alpha * 255).astype(np.uint8)
                bgra = np.dstack([light, light, light, a]).astype(np.uint8).copy()
                image = QImage(bgra.data, cells, cells, cells * 4, QImage.Format_ARGB32)
                pixmap = QPixmap.fromImage(image.copy())
                if grain > 1:
                    pixmap = pixmap.scaled(NOISE_TILE, NOISE_TILE, Qt.IgnoreAspectRatio, Qt.FastTransformation)
                self._noise.append(pixmap)
        return self._noise

    def _tick(self):
        self._noise_index = (self._noise_index + 1) % 4
        self._offset = (random.randrange(NOISE_TILE), random.randrange(NOISE_TILE))
        self.update()

    # ------------------------------------------------------------ paint

    def paintEvent(self, event):
        v = self.values
        if v["scanline_opacity"] <= 0 and v["noise_opacity"] <= 0:
            return
        painter = QPainter(self)
        if v["scanline_opacity"] > 0:
            painter.drawTiledPixmap(self.rect(), self._line_tile())
        if v["noise_opacity"] > 0:
            tiles = self._noise_tile()
            painter.drawTiledPixmap(self.rect(), tiles[self._noise_index], QPoint(*self._offset))
        painter.end()
