"""Active-process drawer: a right-anchored overlay listing running instances.

The overlay behaviour (layer above the pages, sliding panel, scrim, Esc) lives in SlideDrawer, which the
queue drawer shares; this class only adds the list of running instances.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QScrollArea, QToolButton, QVBoxLayout, QWidget)

from . import theme
from .icon_manager import IconManager
from .session_manager import Session, SessionManager
from .slide_drawer import ANIM_MS, MAX_W, MIN_W, SlideDrawer, drawer_width  # noqa: F401  (re-exported)


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
        if session.queue.busy and not session.is_running:
            sub = QLabel(f"Queue · {session.queue.state_text}")
        else:
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


class ActiveProcessDrawer(SlideDrawer):
    sessionSelected = Signal(object)

    def __init__(self, manager: SessionManager, icons: IconManager, parent: QWidget):
        super().__init__(parent)
        self._manager = manager
        self._icons = icons
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

        self.close_button.clicked.connect(self.close_drawer)
        for sig in (manager.sessionAdded, manager.sessionRemoved, manager.sessionChanged):
            sig.connect(self._on_manager_event)
        self._dirty = False

    # ------------------------------------------------------------------ open / close hooks
    def prepare_open(self) -> None:
        self.rebuild()

    def focus_on_open(self) -> None:
        self.close_button.setFocus()

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
