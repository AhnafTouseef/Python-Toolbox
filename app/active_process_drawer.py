"""Active-process drawer: a right-anchored overlay listing running instances.

It is a child of the stage widget that holds the pages, stacked above them, so the library
grid underneath is never resized or re-laid-out.  Only the panel's x position is animated
(one cheap move per frame); the dimmed scrim behind it is static.
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QShortcut, QKeySequence
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QToolButton, QVBoxLayout,
                               QWidget)

from . import theme
from .icon_manager import IconManager
from .session_manager import Session, SessionManager

MIN_W, MAX_W = 300, 460
ANIM_MS = 180


def drawer_width(window_width: int) -> int:
    """About one third of the window, within sensible limits (and never wider than the window)."""
    return max(1, min(window_width, max(MIN_W, min(MAX_W, window_width // 3))))


class DrawerItem(QPushButton):
    """One running instance. A button, so it is reachable and activatable from the keyboard."""

    def __init__(self, session: Session, label: str, icons: IconManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.session = session
        self.setObjectName("DrawerItem")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(f"{label}, running")
        self.setFixedHeight(58)
        self.setStyleSheet(
            f"#DrawerItem {{ background: {theme.CARD}; border: 1px solid {theme.BORDER}; border-radius: 10px;"
            f" text-align: left; padding: 0; }}"
            f"#DrawerItem:hover {{ background: {theme.CARD_HOVER}; border-color: {theme.BORDER_STRONG}; }}"
            f"#DrawerItem:focus {{ border-color: {theme.INFO}; }}")
        m = session.tool.meta
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(10)
        tile = QLabel()
        tile.setFixedSize(32, 32)
        tile.setPixmap(icons.pixmap(m.icon, m.color, 22))
        tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tile.setStyleSheet(f"background: {theme.with_alpha(m.color, 0.15).name(QColor.NameFormat.HexArgb)};"
                           f" border-radius: 8px;")
        tile.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        texts = QVBoxLayout()
        texts.setSpacing(1)
        title = QLabel(label)
        title.setStyleSheet("font-weight: 600; background: transparent;")
        started = session.started_at.strftime("%H:%M:%S") if session.started_at else ""
        sub = QLabel(f"PID {session.pid} · started {started}" if session.pid else f"started {started}")
        sub.setObjectName("Muted")
        sub.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 12px; background: transparent;")
        for w in (title, sub):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            w.setMinimumWidth(10)
        texts.addStretch(1)
        texts.addWidget(title)
        texts.addWidget(sub)
        texts.addStretch(1)
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {theme.OK}; background: transparent;")
        dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        lay.addWidget(tile)
        lay.addLayout(texts, 1)
        lay.addWidget(dot)


class ActiveProcessDrawer(QWidget):
    """Full-size transparent layer (catches clicks outside the panel) + the sliding panel."""

    sessionSelected = Signal(object)

    def __init__(self, manager: SessionManager, icons: IconManager, parent: QWidget):
        super().__init__(parent)
        self._manager = manager
        self._icons = icons
        self._open = False
        self.setObjectName("DrawerLayer")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hide()

        self.panel = QFrame(self)
        self.panel.setObjectName("DrawerPanel")
        self.panel.setStyleSheet(f"#DrawerPanel {{ background: {theme.PANEL}; border: none;"
                                 f" border-left: 1px solid {theme.BORDER_STRONG}; }}")
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        self._title = QLabel("Running")
        self._title.setObjectName("Title")
        self.close_button = QToolButton()
        self.close_button.setObjectName("Flat")
        self.close_button.setIcon(icons.icon("x", theme.TEXT, 18))
        self.close_button.setToolTip("Close")
        self.close_button.setAccessibleName("Close panel")
        head.addWidget(self._title, 1)
        head.addWidget(self.close_button)

        self._list = QVBoxLayout()
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(8)
        holder = QWidget()
        holder.setObjectName("Page")
        holder.setStyleSheet(f"#Page {{ background: transparent; }}")
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 4, 0)
        hl.addLayout(self._list)
        hl.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(holder)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._empty = QLabel("Nothing is running.")
        self._empty.setObjectName("Muted")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)

        pl = QVBoxLayout(self.panel)
        pl.setContentsMargins(16, 14, 12, 14)
        pl.setSpacing(12)
        pl.addLayout(head)
        pl.addWidget(self._empty)
        pl.addWidget(self.scroll, 1)

        self._anim = QPropertyAnimation(self.panel, b"pos", self)
        self._anim.setDuration(ANIM_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self._on_anim_finished)

        self.close_button.clicked.connect(self.close_drawer)
        esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        esc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        esc.activated.connect(self.close_drawer)

        for sig in (manager.sessionAdded, manager.sessionRemoved, manager.sessionChanged):
            sig.connect(self._on_manager_event)
        self._dirty = False

    # ------------------------------------------------------------------ geometry
    @property
    def is_open(self) -> bool:
        return self._open

    def fit_to(self, rect) -> None:
        """Called by the stage whenever it is resized: the layer covers it, the panel stays on the right edge."""
        self.setGeometry(rect)
        w = drawer_width(rect.width())
        self.panel.resize(w, rect.height())
        if self._anim.state() != QPropertyAnimation.State.Running:
            self.panel.move(rect.width() - w if self._open else rect.width(), 0)
        else:
            self._anim.setEndValue(QPoint(rect.width() - w if self._open else rect.width(), 0))

    # ------------------------------------------------------------------ open / close
    def open_drawer(self) -> None:
        if self._open:
            return
        self._open = True
        self.rebuild()
        self.show()
        self.raise_()
        w = drawer_width(self.width())
        self.panel.resize(w, self.height())
        self._slide(QPoint(self.width(), 0), QPoint(self.width() - w, 0))
        self.close_button.setFocus()

    def close_drawer(self) -> None:
        if not self._open:
            return
        self._open = False
        self._slide(self.panel.pos(), QPoint(self.width(), 0))

    def _slide(self, start: QPoint, end: QPoint) -> None:
        self._anim.stop()
        self._anim.setStartValue(start)
        self._anim.setEndValue(end)
        self._anim.start()

    def _on_anim_finished(self) -> None:
        if not self._open:
            self.hide()

    # ------------------------------------------------------------------ list
    def _on_manager_event(self, *_args) -> None:
        if self._open:
            self.rebuild()

    def rebuild(self) -> None:
        """Replace the rows (there are few). Selection of a row never changes any process."""
        while self._list.count():
            item = self._list.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        running = self._manager.running()
        for s in running:
            row = DrawerItem(s, self._manager.label(s), self._icons)
            row.clicked.connect(lambda _=False, sess=s: self._select(sess))
            self._list.addWidget(row)
        n = len(running)
        self._title.setText("Running" if n == 0 else f"Running · {n}")
        self._empty.setVisible(n == 0)
        self.scroll.setVisible(n > 0)

    def item_sessions(self) -> list[Session]:
        return [self._list.itemAt(i).widget().session for i in range(self._list.count())
                if self._list.itemAt(i).widget() is not None and isinstance(self._list.itemAt(i).widget(), DrawerItem)]

    def _select(self, session: Session) -> None:
        self.close_drawer()
        self.sessionSelected.emit(session)

    # ------------------------------------------------------------------ painting / input
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))        # static scrim

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self.panel.geometry().contains(event.position().toPoint()):
            self.close_drawer()
        event.accept()

    def wheelEvent(self, event) -> None:
        event.accept()                                       # nothing scrolls underneath while open

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close_drawer()
            event.accept()
        else:
            super().keyPressEvent(event)
