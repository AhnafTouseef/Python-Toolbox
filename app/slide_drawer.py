"""Right-anchored overlay drawer: the behaviour shared by the active-process drawer and the queue drawer.

A drawer is a child of the stage widget that holds the pages, stacked above them, so the page underneath is
never resized or re-laid-out.  Only the panel's x position is animated (one cheap move per frame); the dimmed
scrim behind it is static.  Clicking the scrim, the close button or pressing Esc closes it.

``docked=True`` turns the same widget into an ordinary page: no scrim, no sliding, no closing; the panel simply
fills the widget (used by the full-screen workflow page, which is the queue panel on its own page).
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, Qt
from PySide6.QtGui import QColor, QKeyEvent, QKeySequence, QMouseEvent, QPainter, QShortcut
from PySide6.QtWidgets import QFrame, QWidget

from . import theme

MIN_W, MAX_W = 300, 460
ANIM_MS = 180


def drawer_width(window_width: int, min_w: int = MIN_W, max_w: int = MAX_W, divisor: int = 3) -> int:
    """About one third of the window, within sensible limits (and never wider than the window)."""
    return max(1, min(window_width, max(min_w, min(max_w, window_width // divisor))))


class SlideDrawer(QWidget):
    """Full-size transparent layer (catches clicks outside the panel) + the sliding panel."""

    def __init__(self, parent: QWidget, docked: bool = False):
        super().__init__(parent)
        self.docked = docked
        self._open = docked                                  # a docked panel is always "open"
        self.setObjectName("DrawerLayer")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if not docked:
            self.hide()
        self.panel = QFrame(self)
        self.panel.setObjectName("DrawerPanel")
        edge = "" if docked else f" border-left: 1px solid {theme.BORDER_STRONG};"
        self.panel.setStyleSheet(f"#DrawerPanel {{ background: {theme.PANEL}; border: none;{edge} }}")
        self._anim = QPropertyAnimation(self.panel, b"pos", self)
        self._anim.setDuration(ANIM_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self._on_anim_finished)
        if not docked:
            esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
            esc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            esc.activated.connect(self.close_drawer)

    # ------------------------------------------------------------------ hooks for subclasses
    def panel_width(self, window_width: int) -> int:
        return drawer_width(window_width)

    def prepare_open(self) -> None:
        """Called just before the drawer slides in (refresh the content here)."""

    def focus_on_open(self) -> None:
        """Called after the slide-in started."""

    def on_closed(self) -> None:
        """Called once the drawer is fully hidden."""

    # ------------------------------------------------------------------ geometry
    @property
    def is_open(self) -> bool:
        return self._open

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.docked:
            self.panel.setGeometry(0, 0, self.width(), self.height())

    def fit_to(self, rect) -> None:
        """Called by the stage whenever it is resized: the layer covers it, the panel stays on the right edge."""
        if self.docked:
            return
        self.setGeometry(rect)
        w = self.panel_width(rect.width())
        self.panel.resize(w, rect.height())
        if self._anim.state() != QPropertyAnimation.State.Running:
            self.panel.move(rect.width() - w if self._open else rect.width(), 0)
        else:
            self._anim.setEndValue(QPoint(rect.width() - w if self._open else rect.width(), 0))

    # ------------------------------------------------------------------ open / close
    def open_drawer(self) -> None:
        if self._open or self.docked:
            return
        self._open = True
        self.prepare_open()
        self.show()
        self.raise_()
        w = self.panel_width(self.width())
        self.panel.resize(w, self.height())
        self._slide(QPoint(self.width(), 0), QPoint(self.width() - w, 0))
        self.focus_on_open()

    def close_drawer(self) -> None:
        if not self._open or self.docked:
            return
        self._open = False
        self._slide(self.panel.pos(), QPoint(self.width(), 0))

    def close_now(self) -> None:
        """Hide immediately without animating (the page underneath is changing)."""
        if self.docked:
            return
        self._anim.stop()
        was_open, self._open = self._open, False
        self.panel.move(self.width(), 0)
        self.hide()
        if was_open:
            self.on_closed()

    def _slide(self, start: QPoint, end: QPoint) -> None:
        self._anim.stop()
        self._anim.setStartValue(start)
        self._anim.setEndValue(end)
        self._anim.start()

    def _on_anim_finished(self) -> None:
        if not self._open:
            self.hide()
            self.on_closed()

    # ------------------------------------------------------------------ painting / input
    def paintEvent(self, _event) -> None:
        if self.docked:
            return
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))        # static scrim

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self.panel.geometry().contains(event.position().toPoint()):
            self.close_drawer()
        event.accept()

    def wheelEvent(self, event) -> None:
        if not self.docked:
            event.accept()                                   # nothing scrolls underneath while open

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close_drawer()
            event.accept()
        else:
            super().keyPressEvent(event)
