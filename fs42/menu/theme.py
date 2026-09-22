"""Colours and metrics for the in-app menu.

Matches the web console's default theme (fs42_server/static/themes/default.css)
so the two read as one product, and scales with the screen like the
now-playing overlay does.
"""

from PySide6.QtGui import QColor, QFont

BACKDROP = QColor(13, 17, 23, 205)      # #0d1117
PANEL = QColor(22, 27, 34, 250)         # #161b22
ROW = QColor(22, 27, 34, 255)
ROW_SELECTED = QColor(33, 42, 56, 255)
BORDER = QColor(48, 54, 61, 255)        # #30363d
ACCENT = QColor(97, 175, 239, 255)      # #61afef
TEXT = QColor(230, 230, 230, 255)       # #e6e6e6
MUTED = QColor(139, 148, 158, 255)      # #8b949e
GOOD = QColor(126, 211, 33, 255)
WARN = QColor(255, 196, 0, 255)
BAD = QColor(248, 81, 73, 255)


def scale_for(screen_height: int) -> float:
    return max(0.6, screen_height / 1080.0)


def font(size: float, bold=False, scale=1.0) -> QFont:
    f = QFont("Arial", max(8, int(size * scale)))
    f.setBold(bold)
    return f


def css(color: QColor) -> str:
    return f"rgba({color.red()},{color.green()},{color.blue()},{color.alpha() / 255:.3f})"


def stylesheet(scale: float) -> str:
    """Stylesheet for the few stock widgets the wizard uses (line edit, spin box)."""
    px = lambda n: f"{int(n * scale)}px"
    return f"""
    QWidget {{ color: {css(TEXT)}; font-family: Arial; font-size: {px(22)}; }}
    QLineEdit, QSpinBox {{
        background: {css(ROW_SELECTED)}; border: 2px solid {css(BORDER)};
        border-radius: {px(6)}; padding: {px(8)} {px(12)}; selection-background-color: {css(ACCENT)};
    }}
    QLineEdit:focus, QSpinBox:focus {{ border-color: {css(ACCENT)}; }}
    QSpinBox::up-button, QSpinBox::down-button {{ width: 0; }}
    QScrollBar:vertical {{ background: transparent; width: {px(8)}; }}
    QScrollBar::handle:vertical {{ background: {css(BORDER)}; border-radius: {px(4)}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    """
