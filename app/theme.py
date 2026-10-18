"""Dark theme: colour constants, fonts and the global stylesheet."""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

# --- Palette ---------------------------------------------------------------
BG = "#16171A"            # application background (dark charcoal)
PANEL = "#1C1D21"         # header bars, drawer
CARD = "#212328"          # cards / raised surfaces
CARD_HOVER = "#262930"
BORDER = "#2E3138"        # subtle borders
BORDER_STRONG = "#3A3E47"
TEXT = "#E7E8EC"
TEXT_MUTED = "#8E929C"
TEXT_DIM = "#5F636D"

OK = "#4ADE80"            # running / completed
WARN = "#FBBF24"          # stopped
ERR = "#F87171"           # failed / error
INFO = "#8EA2FF"

TERM_BG = "#121316"
TERM_STDOUT = "#D9DBE1"
TERM_STDERR = "#FF8A80"
TERM_INPUT = "#8BE9A8"
TERM_SYSTEM = "#6B7080"

UI_FONT_FAMILIES = ["Segoe UI", "Inter", "Noto Sans", "DejaVu Sans", "Arial"]
MONO_FONT_FAMILIES = ["Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Courier New"]


def mono_font(point_size: float = 10.0) -> QFont:
    font = QFont()
    font.setFamilies(MONO_FONT_FAMILIES)
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setFixedPitch(True)
    font.setPointSizeF(point_size)
    return font


def readable_text_on(color: QColor) -> QColor:
    """Black or white, whichever reads better on `color`."""
    lum = 0.2126 * color.redF() + 0.7152 * color.greenF() + 0.0722 * color.blueF()
    return QColor("#101114") if lum > 0.55 else QColor("#FFFFFF")


def with_alpha(color: QColor | str, alpha: float) -> QColor:
    c = QColor(color)
    c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


STYLESHEET = f"""
* {{
    font-family: "Segoe UI", "Inter", "Noto Sans", "DejaVu Sans", sans-serif;
    font-size: 13px;
    color: {TEXT};
}}
QMainWindow, QDialog {{ background: {BG}; }}
QWidget#Page {{ background: {BG}; }}
QToolTip {{
    background: {CARD_HOVER}; color: {TEXT}; border: 1px solid {BORDER_STRONG};
    padding: 4px 6px;
}}
QLabel {{ background: transparent; }}
QLabel#Muted {{ color: {TEXT_MUTED}; }}
QLabel#Title {{ font-size: 18px; font-weight: 600; }}
QLabel#Error {{ color: {ERR}; }}

QLineEdit, QPlainTextEdit, QTextEdit {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px;
    padding: 6px 10px; selection-background-color: #3C4A7A;
}}
QLineEdit:focus, QPlainTextEdit:focus {{ border: 1px solid {BORDER_STRONG}; }}
QLineEdit:disabled {{ color: {TEXT_DIM}; background: {PANEL}; }}
QLineEdit[invalid="true"] {{ border: 1px solid {ERR}; }}

QPushButton, QToolButton {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px;
    padding: 6px 14px;
}}
QPushButton:hover, QToolButton:hover {{ background: {CARD_HOVER}; border-color: {BORDER_STRONG}; }}
QPushButton:pressed, QToolButton:pressed {{ background: {PANEL}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {TEXT_DIM}; background: {PANEL}; }}
QPushButton:focus, QToolButton:focus {{ border-color: {INFO}; }}
QPushButton#Primary {{ background: #3B4A8C; border-color: #4C5FB0; }}
QPushButton#Primary:hover {{ background: #4558A5; }}
QPushButton#Primary:disabled {{ background: {PANEL}; border-color: {BORDER}; }}
QToolButton#Flat {{ background: transparent; border: 1px solid transparent; padding: 4px; }}
QToolButton#Flat:hover {{ background: {CARD_HOVER}; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER_STRONG}; border-radius: 4px; min-height: 36px; }}
QScrollBar::handle:vertical:hover {{ background: #4B505B; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER_STRONG}; border-radius: 4px; min-width: 36px; }}

QPlainTextEdit#Terminal {{
    background: {TERM_BG}; border: 1px solid {BORDER}; border-radius: 8px; padding: 8px;
}}

QTabBar {{ background: transparent; }}
QTabBar::tab {{
    background: {PANEL}; border: 1px solid {BORDER}; border-bottom: none;
    border-top-left-radius: 8px; border-top-right-radius: 8px;
    padding: 6px 10px; margin-right: 3px; min-width: 90px; max-width: 220px;
    color: {TEXT_MUTED};
}}
QTabBar::tab:selected {{ background: {CARD}; color: {TEXT}; border-color: {BORDER_STRONG}; }}
QTabBar::tab:hover:!selected {{ background: {CARD_HOVER}; }}
QTabBar::close-button {{ subcontrol-position: right; margin: 2px; }}
QTabBar::scroller {{ width: 24px; }}

QMenu {{ background: {CARD}; border: 1px solid {BORDER_STRONG}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: {CARD_HOVER}; }}
QListView {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; outline: none; }}
QListView::item:selected {{ background: #2C3350; border-radius: 6px; }}
QListView::item:hover {{ background: {CARD_HOVER}; border-radius: 6px; }}
QMessageBox {{ background: {BG}; }}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(BG))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Base, QColor(CARD))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(PANEL))
    pal.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Button, QColor(CARD))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(CARD_HOVER))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Highlight, QColor("#3C4A7A"))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(TEXT_DIM))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(TEXT_DIM))
    app.setPalette(pal)
    app.setStyleSheet(STYLESHEET)
    # Prefer a Windows UI font when present; QSS font-family falls back otherwise.
    families = set(QFontDatabase.families())
    for fam in UI_FONT_FAMILIES:
        if fam in families:
            font = app.font()
            font.setFamily(fam)
            app.setFont(font)
            break
