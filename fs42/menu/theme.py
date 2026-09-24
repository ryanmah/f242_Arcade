"""Colours, font and metrics for the in-app menu.

The menu is drawn like a VCR's on-screen display: white monospace text on
black, a dashed title bar and a solid white bar behind the selected line
with its text knocked out.  The font is VCR OSD Mono, bundled in ``fs42/menu/fonts``; when it
cannot be loaded the system's monospace font stands in.
"""

import logging
from pathlib import Path

from PySide6.QtGui import QColor, QFont

_l = logging.getLogger("MENU")

# A VCR display: white text on black, a white bar under the selected line.
SCREEN = QColor(0, 0, 0, 255)
BACKDROP = QColor(0, 0, 0, 148)          # 60% black: the picture shows through
PANEL = SCREEN
GREEN = QColor(96, 255, 64, 255)
WHITE = QColor(255, 255, 255, 255)
ROW = QColor(0, 0, 0, 0)                 # rows have no box of their own
ROW_SELECTED = WHITE                     # the bar behind the selected line
ROW_SELECTED_TEXT = SCREEN               # ...with the text knocked out of it
ROW_SHADOW = QColor(0, 0, 0, 0)
BORDER = WHITE
ACCENT = WHITE
TEXT = WHITE
SHADOW = QColor(0, 0, 0, 0)              # no drop shadow on a black screen
MUTED = QColor(176, 176, 176, 255)
GOOD = WHITE
WARN = QColor(255, 232, 90, 255)
BAD = QColor(255, 120, 96, 255)

# Everything the pages draw is sized against a 1080p screen and then
# scaled by this on top; 1.2 is "a fifth bigger than the first cut".
TEXT_SCALE = 1.2

FONT_FILE = Path(__file__).parent / "fonts" / "VCR_OSD_MONO.ttf"
FALLBACK_FAMILY = "Monospace"
_family = None


def load_fonts() -> str:
    """Register the bundled font with Qt (once) and return its family name."""
    global _family
    if _family is not None:
        return _family
    from PySide6.QtGui import QFontDatabase

    _family = FALLBACK_FAMILY
    try:
        font_id = QFontDatabase.addApplicationFont(str(FONT_FILE))
        families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
        if families:
            _family = families[0]
        else:
            _l.warning("Could not load the menu font from %s; using %s", FONT_FILE, FALLBACK_FAMILY)
    except Exception as e:
        _l.warning("Could not load the menu font: %s", e)
    return _family


def family() -> str:
    return _family or load_fonts()


def scale_for(screen_height: int) -> float:
    return max(0.6, screen_height / 1080.0)


def font(size: float, bold=False, scale=1.0) -> QFont:
    # The VCR face has one weight; "bold" only matters for the fallback.
    f = QFont(family(), max(8, int(size * scale)))
    f.setBold(bold and family() == FALLBACK_FAMILY)
    f.setStyleHint(QFont.Monospace)
    return f


def css(color: QColor) -> str:
    return f"rgba({color.red()},{color.green()},{color.blue()},{color.alpha() / 255:.3f})"


def stylesheet(scale: float) -> str:
    """Stylesheet for the few stock widgets the wizard uses (line edit, spin box)."""
    px = lambda n: f"{int(n * scale)}px"
    return f"""
    QWidget {{ color: {css(TEXT)}; font-family: "{family()}"; font-size: {px(24)}; }}
    QLineEdit, QSpinBox {{
        background: {css(SCREEN)}; border: 2px solid {css(MUTED)};
        border-radius: 0; padding: {px(4)} {px(10)}; selection-background-color: {css(TEXT)};
        selection-color: {css(SCREEN)};
    }}
    QLineEdit:focus, QSpinBox:focus {{ border-color: {css(TEXT)}; }}
    QSpinBox::up-button, QSpinBox::down-button {{ width: 0; }}
    QScrollBar:vertical {{ background: transparent; width: {px(8)}; }}
    QScrollBar::handle:vertical {{ background: {css(MUTED)}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    """
