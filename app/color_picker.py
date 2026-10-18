"""Colour picker: hue/saturation wheel, brightness slider, editable hex field, live preview.

Any valid colour may be chosen, including white, greys and muted tones (the
"no neutral colours" rule applies only to automatic random generation).
"""

from __future__ import annotations

import math
import re

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QColor, QConicalGradient, QKeyEvent, QLinearGradient, QMouseEvent, QPainter,
                           QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from . import theme
from .icon_manager import IconManager

_HEX_INPUT = re.compile(r"^#?[0-9A-Fa-f]{6}$")


def parse_hex(text: str) -> QColor | None:
    """'#A78BFA' or 'A78BFA' -> QColor, otherwise None."""
    t = text.strip()
    if not _HEX_INPUT.match(t):
        return None
    return QColor(t if t.startswith("#") else "#" + t)


def hsv_to_hex(h: float, s: float, v: float) -> str:
    return QColor.fromHsvF(min(max(h, 0.0), 1.0), min(max(s, 0.0), 1.0), min(max(v, 0.0), 1.0)).name().upper()


class ColorWheel(QWidget):
    """Hue (angle) / saturation (radius) wheel. Brightness only dims the display."""
    changed = Signal(float, float)   # hue 0..1, saturation 0..1

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._h, self._s, self._v = 0.0, 1.0, 1.0
        self._cache: QPixmap | None = None
        self._cache_size = 0
        self.setMinimumSize(170, 170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Hue and saturation")
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_hsv(self, h: float, s: float, v: float) -> None:
        self._h, self._s, self._v = h, s, v
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(220, 220)

    def _geometry(self) -> tuple[QPointF, float]:
        side = min(self.width(), self.height()) - 12
        return QPointF(self.width() / 2, self.height() / 2), side / 2

    def _wheel_pixmap(self, diameter: int) -> QPixmap:
        if self._cache is not None and self._cache_size == diameter:
            return self._cache
        dpr = self.devicePixelRatioF()
        px = QPixmap(int(diameter * dpr), int(diameter * dpr))
        px.setDevicePixelRatio(dpr)
        px.fill(Qt.GlobalColor.transparent)
        p = QPainter(px)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = diameter / 2
        cone = QConicalGradient(c, c, 0)
        for i, name in enumerate(("#FF0000", "#FFFF00", "#00FF00", "#00FFFF", "#0000FF", "#FF00FF", "#FF0000")):
            cone.setColorAt(i / 6, QColor(name))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(cone)
        p.drawEllipse(QRectF(0, 0, diameter, diameter))
        rad = QRadialGradient(c, c, c)
        rad.setColorAt(0.0, QColor(255, 255, 255, 255))
        rad.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(rad)
        p.drawEllipse(QRectF(0, 0, diameter, diameter))
        p.end()
        self._cache, self._cache_size = px, diameter
        return px

    def paintEvent(self, _event) -> None:
        center, radius = self._geometry()
        d = int(radius * 2)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.drawPixmap(int(center.x() - radius), int(center.y() - radius), self._wheel_pixmap(d))
        if self._v < 1.0:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, int(255 * (1 - self._v))))
            p.drawEllipse(center, radius, radius)
        ang = self._h * 2 * math.pi
        pos = QPointF(center.x() + radius * self._s * math.cos(ang), center.y() - radius * self._s * math.sin(ang))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(0, 0, 0, 200), 3))
        p.drawEllipse(pos, 7, 7)
        p.setPen(QPen(QColor("#FFFFFF"), 1.5))
        p.drawEllipse(pos, 7, 7)
        if self.hasFocus():
            p.setPen(QPen(QColor(theme.INFO), 1.5, Qt.PenStyle.DashLine))
            p.drawEllipse(center, radius + 4, radius + 4)

    def _pick(self, pos: QPointF) -> None:
        center, radius = self._geometry()
        dx, dy = pos.x() - center.x(), pos.y() - center.y()
        s = min(1.0, math.hypot(dx, dy) / radius) if radius > 0 else 0.0
        h = (math.atan2(-dy, dx) / (2 * math.pi)) % 1.0
        self.changed.emit(h, s)

    def mousePressEvent(self, e: QMouseEvent) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self.setFocus()
            self._pick(e.position())

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        if e.buttons() & Qt.MouseButton.LeftButton:
            self._pick(e.position())

    def keyPressEvent(self, e: QKeyEvent) -> None:
        big = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        dh, ds = (0.05 if big else 0.01), (0.1 if big else 0.02)
        k = e.key()
        if k == Qt.Key.Key_Left:
            self.changed.emit((self._h + dh) % 1.0, self._s)
        elif k == Qt.Key.Key_Right:
            self.changed.emit((self._h - dh) % 1.0, self._s)
        elif k == Qt.Key.Key_Up:
            self.changed.emit(self._h, min(1.0, self._s + ds))
        elif k == Qt.Key.Key_Down:
            self.changed.emit(self._h, max(0.0, self._s - ds))
        else:
            super().keyPressEvent(e)


class ValueSlider(QWidget):
    """Brightness (value) slider, black -> the current hue/saturation at full brightness."""
    changed = Signal(float)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._h, self._s, self._v = 0.0, 1.0, 1.0
        self.setFixedHeight(28)
        self.setMinimumWidth(170)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Brightness")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_hsv(self, h: float, s: float, v: float) -> None:
        self._h, self._s, self._v = h, s, v
        self.update()

    def _track(self) -> QRectF:
        return QRectF(10, 8, self.width() - 20, 12)

    def paintEvent(self, _event) -> None:
        r = self._track()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        grad = QLinearGradient(r.left(), 0, r.right(), 0)
        grad.setColorAt(0, QColor("#000000"))
        grad.setColorAt(1, QColor.fromHsvF(self._h, self._s, 1.0))
        p.setPen(QPen(QColor(theme.BORDER_STRONG), 1))
        p.setBrush(grad)
        p.drawRoundedRect(r, 6, 6)
        x = r.left() + r.width() * self._v
        p.setPen(QPen(QColor(0, 0, 0, 200), 3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(x, r.center().y()), 8, 8)
        p.setPen(QPen(QColor("#FFFFFF"), 1.5))
        p.drawEllipse(QPointF(x, r.center().y()), 8, 8)
        if self.hasFocus():
            p.setPen(QPen(QColor(theme.INFO), 1.5, Qt.PenStyle.DashLine))
            p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 8, 8)

    def _pick(self, x: float) -> None:
        r = self._track()
        self.changed.emit(min(1.0, max(0.0, (x - r.left()) / r.width())))

    def mousePressEvent(self, e: QMouseEvent) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self.setFocus()
            self._pick(e.position().x())

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        if e.buttons() & Qt.MouseButton.LeftButton:
            self._pick(e.position().x())

    def keyPressEvent(self, e: QKeyEvent) -> None:
        k = e.key()
        if k in (Qt.Key.Key_Left, Qt.Key.Key_Down):
            self.changed.emit(max(0.0, self._v - 0.01))
        elif k in (Qt.Key.Key_Right, Qt.Key.Key_Up):
            self.changed.emit(min(1.0, self._v + 0.01))
        elif k == Qt.Key.Key_PageDown:
            self.changed.emit(max(0.0, self._v - 0.1))
        elif k == Qt.Key.Key_PageUp:
            self.changed.emit(min(1.0, self._v + 0.1))
        elif k == Qt.Key.Key_Home:
            self.changed.emit(0.0)
        elif k == Qt.Key.Key_End:
            self.changed.emit(1.0)
        else:
            super().keyPressEvent(e)


class _Preview(QWidget):
    """Live preview: swatch plus a tinted sample of the tool tile."""

    def __init__(self, icons: IconManager | None, icon_name: str | None, parent: QWidget | None = None):
        super().__init__(parent)
        self._icons, self._icon = icons, icon_name
        self._color = QColor("#FFFFFF")
        self.setFixedHeight(64)

    def set_color(self, hex_color: str) -> None:
        self._color = QColor(hex_color)
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(theme.BORDER_STRONG), 1))
        p.setBrush(self._color)
        sw = QRectF(0.5, 0.5, self.width() * 0.45, self.height() - 1)
        p.drawRoundedRect(sw, 10, 10)
        p.setPen(theme.readable_text_on(self._color))
        p.drawText(sw, Qt.AlignmentFlag.AlignCenter, self._color.name().upper())
        # sample tile on card background
        card = QRectF(sw.right() + 10, 0.5, self.width() - sw.right() - 11, self.height() - 1)
        p.setPen(QPen(theme.with_alpha(self._color, 0.7), 1))
        p.setBrush(QColor(theme.CARD))
        p.drawRoundedRect(card, 10, 10)
        tile = QRectF(card.left() + 10, card.top() + 10, 44, 44)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.with_alpha(self._color, 0.15))
        p.drawRoundedRect(tile, 10, 10)
        if self._icons is not None and self._icon:
            px = self._icons.pixmap(self._icon, self._color.name().upper(), 24)
            p.drawPixmap(int(tile.left() + 10), int(tile.top() + 10), px)


class ColorPickerDialog(QDialog):
    def __init__(self, initial_hex: str, parent: QWidget | None = None,
                 icons: IconManager | None = None, icon_name: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Choose accent color")
        self.setModal(True)
        self.setMinimumWidth(340)

        c = parse_hex(initial_hex) or QColor("#A78BFA")
        self._h = max(c.hsvHueF(), 0.0)
        self._s = c.hsvSaturationF()
        self._v = c.valueF()

        self.wheel = ColorWheel()
        self.slider = ValueSlider()
        self.hex_edit = QLineEdit()
        self.hex_edit.setMaxLength(7)
        self.hex_edit.setPlaceholderText("#A78BFA")
        self.hex_edit.setAccessibleName("Hex color")
        self.hex_edit.setFont(theme.mono_font(10.5))
        self.error = QLabel("")
        self.error.setObjectName("Error")
        self.error.setFixedHeight(18)
        self.preview = _Preview(icons, icon_name)

        buttons = QDialogButtonBox()
        self.save_button = buttons.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        self.save_button.setObjectName("Primary")
        self.save_button.setDefault(True)
        self.cancel_button = buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        hex_row = QHBoxLayout()
        hex_row.addWidget(QLabel("Hex"))
        hex_row.addWidget(self.hex_edit, 1)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 18, 18, 14)
        lay.setSpacing(10)
        lay.addWidget(self.wheel, 1)
        lay.addWidget(self.slider)
        lay.addLayout(hex_row)
        lay.addWidget(self.error)
        lay.addWidget(self.preview)
        lay.addWidget(buttons)

        self.wheel.changed.connect(lambda h, s: self._set_hsv(h, s, self._v))
        self.slider.changed.connect(lambda v: self._set_hsv(self._h, self._s, v))
        self.hex_edit.textEdited.connect(self._on_hex_edited)

        self._set_hsv(self._h, self._s, self._v)
        self.resize(360, 560)

    # -- state ------------------------------------------------------------
    def _set_hsv(self, h: float, s: float, v: float, rewrite_hex: bool = True) -> None:
        self._h, self._s, self._v = h, s, v
        self.wheel.set_hsv(h, s, v)
        self.slider.set_hsv(h, s, v)
        hex_color = hsv_to_hex(h, s, v)
        self.preview.set_color(hex_color)
        if rewrite_hex:
            self.hex_edit.setText(hex_color)
            self._mark_valid(True)

    def _mark_valid(self, ok: bool) -> None:
        self.hex_edit.setProperty("invalid", not ok)
        self.hex_edit.style().unpolish(self.hex_edit)
        self.hex_edit.style().polish(self.hex_edit)
        self.error.setText("" if ok else "Enter a color like #A78BFA (6 hex digits).")
        self.save_button.setEnabled(ok)

    def _on_hex_edited(self, text: str) -> None:
        c = parse_hex(text)
        if c is None:
            self._mark_valid(False)
            return
        h = c.hsvHueF()
        if h < 0:                    # achromatic (white/grey/black): keep the current hue for the wheel
            h = self._h
        self._mark_valid(True)
        self._set_hsv(h, c.hsvSaturationF(), c.valueF(), rewrite_hex=False)

    # -- result -------------------------------------------------------------
    def selected_hex(self) -> str | None:
        c = parse_hex(self.hex_edit.text())
        return c.name().upper() if c else None

    def accept(self) -> None:
        if self.selected_hex() is None:
            self._mark_valid(False)
            return
        super().accept()
