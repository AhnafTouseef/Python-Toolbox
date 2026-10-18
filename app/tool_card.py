"""Library card for one tool: icon tile, name, description, status pill, settings button.

Hover feedback is painted (border blend + a few translucent strokes inside a small
margin around the card body) and eased with one lazily-created animation per card.
No QGraphicsEffect is used: those force offscreen buffers and are costly on weak
hardware.
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPointF, QRectF, QSize, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QMouseEvent, QPainter, QPen, QTextLayout, QTextOption
from PySide6.QtWidgets import QMenu, QToolButton, QWidget

from . import theme
from .icon_manager import IconManager
from .tool_registry import ToolInfo

GLOW = 5                 # px of transparent margin around the card body for the glow
CARD_BODY_H = 108        # visual card height (fixed so grid geometry is pure arithmetic)
RADIUS = 12

STATUS_STYLES = {
    "running": (theme.OK, "Running"),
    "completed": (theme.OK, "Completed"),
    "failed": (theme.ERR, "Failed"),
    "stopped": (theme.WARN, "Stopped"),
    "error": (theme.ERR, "Launch error"),
}


def _blend(a: str | QColor, b: str | QColor, t: float) -> QColor:
    ca, cb = QColor(a), QColor(b)
    return QColor.fromRgbF(ca.redF() + (cb.redF() - ca.redF()) * t,
                           ca.greenF() + (cb.greenF() - ca.greenF()) * t,
                           ca.blueF() + (cb.blueF() - ca.blueF()) * t)


class ElidedText(QWidget):
    """Word-wrapped text limited to `max_lines`, with an ellipsis on the last line."""

    def __init__(self, max_lines: int, font: QFont, color: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._text = ""
        self._max_lines = max_lines
        self._color = QColor(color)
        self.setFont(font)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFixedHeight(QFontMetrics(font).lineSpacing() * max_lines + 2)

    def set_text(self, text: str) -> None:
        if text != self._text:
            self._text = text
            self.update()

    def paintEvent(self, _event) -> None:
        if not self._text:
            return
        fm = self.fontMetrics()
        width = max(1, self.width())
        lines: list[tuple[int, int]] = []
        layout = QTextLayout(self._text, self.font())
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(opt)
        layout.beginLayout()
        while len(lines) < self._max_lines:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            lines.append((line.textStart(), line.textLength()))
        layout.endLayout()
        p = QPainter(self)
        p.setPen(self._color)
        p.setFont(self.font())
        for i, (start, length) in enumerate(lines):
            last_allowed = i == self._max_lines - 1
            if last_allowed and start + length < len(self._text):
                s = fm.elidedText(self._text[start:], Qt.TextElideMode.ElideRight, width)
            else:
                s = self._text[start:start + length].rstrip("\n")
            p.drawText(QPointF(0, i * fm.lineSpacing() + fm.ascent() + 1), s)


class IconTile(QWidget):
    SIZE = 44

    def __init__(self, icons: IconManager, parent: QWidget | None = None):
        super().__init__(parent)
        self._icons = icons
        self._name = ""
        self._color = "#FFFFFF"
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_icon(self, name: str, color: str) -> None:
        self._name, self._color = name, color
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.with_alpha(self._color, 0.15))
        p.drawRoundedRect(QRectF(self.rect()), 10, 10)
        px = self._icons.pixmap(self._name, self._color, 24)
        p.drawPixmap((self.width() - 24) // 2, (self.height() - 24) // 2, px)


class StatusPill(QWidget):
    """Dot + label. Independent of hover styling."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._kind: str | None = None
        self._text = ""
        font = QFont(self.font())
        font.setPixelSize(11)
        self.setFont(font)
        self.setFixedHeight(18)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()

    def set_status(self, kind: str | None, count: int = 0) -> None:
        if kind not in STATUS_STYLES:
            kind = None
        text = ""
        if kind:
            text = f"{count} running" if kind == "running" and count > 1 else STATUS_STYLES[kind][1]
        if (kind, text) == (self._kind, self._text):
            return
        self._kind, self._text = kind, text
        if kind:
            self.setFixedWidth(self.fontMetrics().horizontalAdvance(text) + 26)
        self.setVisible(kind is not None)
        self.update()

    @property
    def kind(self) -> str | None:
        return self._kind

    @property
    def text(self) -> str:
        return self._text

    def paintEvent(self, _event) -> None:
        if not self._kind:
            return
        color = STATUS_STYLES[self._kind][0]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.with_alpha(color, 0.14))
        p.drawRoundedRect(QRectF(self.rect()), 9, 9)
        p.setBrush(QColor(color))
        p.drawEllipse(QPointF(10, self.height() / 2), 3, 3)
        p.setPen(QColor(color))
        p.drawText(QRectF(18, 0, self.width() - 20, self.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._text)


class ToolCard(QWidget):
    openRequested = Signal(str)
    settingsRequested = Signal(str)
    navigateRequested = Signal(str, int)     # tool id, Qt.Key

    def __init__(self, tool: ToolInfo, icons: IconManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.tool = tool
        self._icons = icons
        self._hover = 0.0
        self._anim: QVariantAnimation | None = None
        self._pressed = False
        self._kb_focus = False
        self.layout_version = -1          # set by the library; lets it skip redundant geometry work

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

        name_font = QFont(self.font())
        name_font.setPixelSize(14)
        name_font.setWeight(QFont.Weight.DemiBold)
        desc_font = QFont(self.font())
        desc_font.setPixelSize(12)

        self._tile = IconTile(icons, self)
        self._name = ElidedText(1, name_font, theme.TEXT, self)
        self._desc = ElidedText(2, desc_font, theme.TEXT_MUTED, self)
        self._status = StatusPill(self)

        self._btn = QToolButton(self)
        self._btn.setObjectName("Flat")
        self._btn.setFixedSize(28, 28)
        self._btn.setIconSize(QSize(16, 16))
        self._btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)   # keyboard path: Menu key / F2 on the card
        self._btn.setCursor(Qt.CursorShape.ArrowCursor)
        self._btn.setToolTip("Tool settings")
        self._btn.setAccessibleName("Tool settings")
        self._btn.clicked.connect(lambda: self.settingsRequested.emit(self.tool.id))
        self._btn.setIcon(icons.icon("settings", theme.TEXT_MUTED, 16))

        self.apply_metadata()

    # -- data ----------------------------------------------------------------
    def apply_metadata(self) -> None:
        m = self.tool.meta
        self._tile.set_icon(m.icon, m.color)
        self._name.set_text(m.name)
        self._desc.set_text(m.description)
        self._desc.setVisible(bool(m.description))
        self.setToolTip(f"{m.name}\n{self.tool.path}")
        self.setAccessibleName(m.name)
        self.setAccessibleDescription(m.description)
        self.update()

    def set_status(self, kind: str | None, count: int = 0) -> None:
        self._status.set_status(kind, count)

    @property
    def status_kind(self) -> str | None:
        return self._status.kind

    # -- geometry --------------------------------------------------------------
    def resizeEvent(self, _event) -> None:
        w, h = self.width(), self.height()
        body_x, body_y, body_w = GLOW, GLOW, w - 2 * GLOW
        self._tile.move(body_x + 14, body_y + 14)
        self._btn.move(body_x + body_w - 10 - 28, body_y + 8)
        left = body_x + 14 + IconTile.SIZE + 12
        self._name.setGeometry(left, body_y + 14, max(10, self._btn.x() - 6 - left), self._name.height())
        self._desc.setGeometry(left, body_y + 38, max(10, body_x + body_w - 14 - left), self._desc.height())
        self._status.move(left, body_y + CARD_BODY_H - 14 - self._status.height())

    # -- painting ----------------------------------------------------------------
    def _set_hover(self, value: float) -> None:
        self._hover = float(value)
        self.update()

    def paintEvent(self, _event) -> None:
        t = self._hover
        accent = self.tool.meta.color
        body = QRectF(GLOW, GLOW, self.width() - 2 * GLOW, self.height() - 2 * GLOW)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if t > 0.01:
            for i in range(1, GLOW):
                a = 0.20 * t * (1 - i / GLOW) ** 1.5
                p.setPen(QPen(theme.with_alpha(accent, a), 1.2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(body.adjusted(-i, -i, i, i), RADIUS + i, RADIUS + i)
        p.setBrush(_blend(theme.CARD, theme.CARD_HOVER, t))
        border = _blend(theme.BORDER, accent, 0.7 * t)
        if self._kb_focus and self.hasFocus():
            border = QColor(theme.INFO)
        p.setPen(QPen(border, 1.0))
        p.drawRoundedRect(body.adjusted(0.5, 0.5, -0.5, -0.5), RADIUS, RADIUS)

    # -- hover animation ---------------------------------------------------------
    def _animate_to(self, target: float) -> None:
        if self._anim is None:
            self._anim = QVariantAnimation(self)
            self._anim.setDuration(150)
            self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._anim.valueChanged.connect(self._set_hover)
        self._anim.stop()
        self._anim.setStartValue(self._hover)
        self._anim.setEndValue(target)
        self._anim.start()

    def enterEvent(self, event) -> None:
        self._animate_to(1.0)
        self._btn.setIcon(self._icons.icon("settings", theme.TEXT, 16))
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._animate_to(0.0)
        self._btn.setIcon(self._icons.icon("settings", theme.TEXT_MUTED, 16))
        self._pressed = False
        super().leaveEvent(event)

    def hideEvent(self, event) -> None:
        # Never keep a glow on a card that was hidden/moved under a stationary pointer.
        if self._anim is not None:
            self._anim.stop()
        self._hover = 0.0
        self._pressed = False
        super().hideEvent(event)

    # -- interaction ----------------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pressed:
            self._pressed = False
            if self.rect().contains(event.position().toPoint()):
                self.openRequested.emit(self.tool.id)
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.openRequested.emit(self.tool.id)
        elif key == Qt.Key.Key_F2:
            self.settingsRequested.emit(self.tool.id)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down,
                     Qt.Key.Key_Home, Qt.Key.Key_End, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            self.navigateRequested.emit(self.tool.id, int(key))
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.addAction("Open", lambda: self.openRequested.emit(self.tool.id))
        menu.addAction("Settings…", lambda: self.settingsRequested.emit(self.tool.id))
        menu.exec(event.globalPos())

    def focusInEvent(self, event) -> None:
        self._kb_focus = event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                                            Qt.FocusReason.ShortcutFocusReason)
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.update()
