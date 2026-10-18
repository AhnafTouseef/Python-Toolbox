"""Execution workspace: Back, instance tabs, "+", Run/Stop, terminal, stdin field and Clear.

One tab = one Session (an instance of a tool).  Every session has its own pane (terminal
+ input field) that lives for as long as the session does, so switching tabs, going back
to the library and resizing never lose output, scroll position or half-typed input.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy, QStackedWidget,
                               QTabBar, QToolButton, QVBoxLayout, QWidget)

from . import theme
from .icon_manager import IconManager
from .session_manager import Session, SessionManager, SessionStatus
from .terminal_view import TerminalView
from .tool_registry import ToolRegistry

STATUS_COLORS = {
    SessionStatus.IDLE: theme.TEXT_MUTED,
    SessionStatus.RUNNING: theme.OK,
    SessionStatus.COMPLETED: theme.OK,
    SessionStatus.FAILED: theme.ERR,
    SessionStatus.STOPPED: theme.WARN,
    SessionStatus.ERROR: theme.ERR,
}
DOT_STATUSES = (SessionStatus.RUNNING, SessionStatus.FAILED, SessionStatus.ERROR)


class SessionPane(QWidget):
    """Terminal + input row for one session."""

    def __init__(self, session: Session, parent: QWidget | None = None):
        super().__init__(parent)
        self.session = session
        self.terminal = TerminalView()
        self.input = QLineEdit()
        self.input.setAccessibleName("Script input")
        self.input.setClearButtonEnabled(False)
        self.clear_button = QPushButton("Clear")
        self.clear_button.setToolTip("Clear the displayed output (does not stop the script)")
        self.clear_button.setAccessibleName("Clear output")
        self.clear_button.setFixedHeight(28)
        self.clear_button.setStyleSheet("padding: 2px 12px;")

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.input, 1)
        row.addWidget(self.clear_button)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 10, 16, 14)
        lay.setSpacing(8)
        lay.addWidget(self.terminal, 1)
        lay.addLayout(row)

        session.output.connect(self.terminal.feed)
        session.statusChanged.connect(self.sync)
        self.input.returnPressed.connect(self._send)
        self.clear_button.clicked.connect(self.terminal.clear_output)
        self.sync()

    def sync(self, *_args) -> None:
        running = self.session.is_running and not self.session.stopping
        self.input.setEnabled(running)
        self.input.setPlaceholderText("Type input for the script and press Enter" if running
                                      else "Input is available while the script is running")

    def _send(self) -> None:
        if self.session.send_input(self.input.text()):
            self.input.clear()


class WorkspaceView(QWidget):
    backRequested = Signal()
    newInstanceRequested = Signal()
    closeRequested = Signal(object)        # Session: the user clicked a tab's close button
    currentChanged = Signal(object)        # Session or None

    def __init__(self, manager: SessionManager, registry: ToolRegistry, icons: IconManager,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Page")
        self._manager = manager
        self._registry = registry
        self._icons = icons
        self._panes: dict[str, SessionPane] = {}

        # -- row 1: back, tabs, + ---------------------------------------------------------
        top = QFrame()
        top.setObjectName("Header")
        top.setStyleSheet(f"#Header {{ background: {theme.PANEL}; border: none; border-bottom: 1px solid {theme.BORDER}; }}")
        tl = QHBoxLayout(top)
        tl.setContentsMargins(12, 8, 12, 0)
        tl.setSpacing(8)
        self.back_button = QPushButton(" Library")
        self.back_button.setIcon(icons.icon("arrow-left", theme.TEXT, 16))
        self.back_button.setAccessibleName("Back to library")
        self.back_button.setToolTip("Back to the library (keeps everything running)")
        self.tabs = QTabBar()
        self.tabs.setDocumentMode(True)
        self.tabs.setDrawBase(False)
        self.tabs.setExpanding(False)
        self.tabs.setMovable(False)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self.tabs.setAccessibleName("Instances")
        self.tabs.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.tabs.setMinimumWidth(120)
        self.plus_button = QToolButton()
        self.plus_button.setObjectName("Flat")
        self.plus_button.setIcon(icons.icon("plus", theme.TEXT, 16))
        self.plus_button.setToolTip("New instance of this tool")
        self.plus_button.setAccessibleName("New instance")
        tl.addWidget(self.back_button, 0, Qt.AlignmentFlag.AlignVCenter)
        tl.addWidget(self.tabs, 1, Qt.AlignmentFlag.AlignBottom)
        tl.addWidget(self.plus_button, 0, Qt.AlignmentFlag.AlignVCenter)
        tl.setAlignment(self.tabs, Qt.AlignmentFlag.AlignBottom)

        # -- row 2: title, status, run/stop -----------------------------------------------
        bar = QFrame()
        bar.setObjectName("Bar")
        bar.setStyleSheet(f"#Bar {{ background: {theme.CARD}; border: none; border-bottom: 1px solid {theme.BORDER}; }}")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(16, 8, 16, 8)
        bl.setSpacing(10)
        self.tile = QLabel()
        self.tile.setFixedSize(24, 24)
        self.title = QLabel()
        self.title.setObjectName("Title")
        self.title.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.title.setMinimumWidth(40)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.status = QLabel()
        self.status.setMinimumWidth(40)
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.run_button = QPushButton("Run")
        self.run_button.setObjectName("Primary")
        self.run_button.setMinimumWidth(96)
        bl.addWidget(self.tile)
        bl.addWidget(self.title, 3)
        bl.addWidget(self.status, 2)
        bl.addWidget(self.run_button)

        self.stack = QStackedWidget()
        self._placeholder = QLabel("No instance open.")
        self._placeholder.setObjectName("Muted")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack.addWidget(self._placeholder)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(top)
        lay.addWidget(bar)
        lay.addWidget(self.stack, 1)

        self.back_button.clicked.connect(self.backRequested)
        self.plus_button.clicked.connect(self.newInstanceRequested)
        self.run_button.clicked.connect(self._run_or_stop)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        manager.sessionAdded.connect(self.add_session)
        manager.sessionRemoved.connect(self.remove_session)
        manager.sessionChanged.connect(self._on_session_changed)
        registry.metadataChanged.connect(lambda _tid: self.refresh_all())
        self._sync_header()

    # ------------------------------------------------------------------ session <-> tab bookkeeping
    def _index_of(self, session: Session) -> int:
        for i in range(self.tabs.count()):
            if self.tabs.tabData(i) == session.id:
                return i
        return -1

    def current_session(self) -> Session | None:
        i = self.tabs.currentIndex()
        return self._manager.get(self.tabs.tabData(i)) if i >= 0 else None

    def pane(self, session: Session) -> SessionPane | None:
        return self._panes.get(session.id)

    def add_session(self, session: Session) -> None:
        pane = SessionPane(session)
        self._panes[session.id] = pane
        self.stack.addWidget(pane)
        idx = self.tabs.addTab("")
        self.tabs.setTabData(idx, session.id)
        close = QToolButton()
        close.setObjectName("Flat")
        close.setIcon(self._icons.icon("x", theme.TEXT_MUTED, 14))
        close.setIconSize(QSize(14, 14))
        close.setFixedSize(20, 20)
        close.setToolTip("Close this instance (discards its output)")
        close.setAccessibleName("Close instance")
        close.clicked.connect(lambda _=False, sid=session.id: self._close_clicked(sid))
        self.tabs.setTabButton(idx, QTabBar.ButtonPosition.RightSide, close)
        self.refresh_all()

    def remove_session(self, session: Session) -> None:
        idx = self._index_of(session)
        pane = self._panes.pop(session.id, None)
        if idx >= 0:
            self.tabs.removeTab(idx)               # the tab bar selects a neighbouring tab by itself
        if pane is not None:
            self.stack.removeWidget(pane)
            pane.deleteLater()
        self.refresh_all()
        self._on_tab_changed(self.tabs.currentIndex())

    def select_session(self, session: Session) -> None:
        idx = self._index_of(session)
        if idx >= 0:
            self.tabs.setCurrentIndex(idx)
            self._on_tab_changed(idx)

    def _close_clicked(self, session_id: str) -> None:
        s = self._manager.get(session_id)
        if s is not None:
            self.closeRequested.emit(s)

    # ------------------------------------------------------------------ rendering of tabs / header
    def _tab_icon(self, session: Session) -> QPixmap:
        m = session.tool.meta
        base = self._icons.pixmap(m.icon, m.color, 16)
        if session.status not in DOT_STATUSES:
            return base
        dpr = base.devicePixelRatio()
        px = QPixmap(base.size())
        px.setDevicePixelRatio(dpr)
        px.fill(Qt.GlobalColor.transparent)
        p = QPainter(px)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.drawPixmap(0, 0, base)
        p.setPen(QColor(theme.PANEL))
        p.setBrush(QColor(STATUS_COLORS[session.status]))
        p.drawEllipse(10, 10, 6, 6)
        p.end()
        return px

    def refresh_all(self) -> None:
        for i in range(self.tabs.count()):
            s = self._manager.get(self.tabs.tabData(i))
            if s is None:
                continue
            self.tabs.setTabText(i, self._manager.label(s))
            self.tabs.setTabIcon(i, self._tab_icon(s))
            self.tabs.setTabToolTip(i, f"{s.tool.meta.name} · {s.status_text}")
        self._sync_header()

    def _on_session_changed(self, session: Session) -> None:
        i = self._index_of(session)
        if i >= 0:
            self.tabs.setTabIcon(i, self._tab_icon(session))
            self.tabs.setTabToolTip(i, f"{session.tool.meta.name} · {session.status_text}")
        if session is self.current_session():
            self._sync_header()

    def _on_tab_changed(self, index: int) -> None:
        s = self._manager.get(self.tabs.tabData(index)) if index >= 0 else None
        pane = self._panes.get(s.id) if s else None
        self.stack.setCurrentWidget(pane if pane is not None else self._placeholder)
        if s is not None:
            self._manager.touch(s)
        self._sync_header()
        self.currentChanged.emit(s)

    def _sync_header(self) -> None:
        s = self.current_session()
        self.run_button.setEnabled(s is not None)
        self.plus_button.setEnabled(s is not None)
        if s is None:
            self.title.setText("")
            self.status.setText("")
            self.tile.clear()
            self.run_button.setText("Run")
            return
        m = s.tool.meta
        self.tile.setPixmap(self._icons.pixmap(m.icon, m.color, 24))
        self.title.setText(self._manager.label(s))
        self.title.setToolTip(str(s.tool.path))
        color = STATUS_COLORS[s.status]
        self.status.setText(f"●  {s.status_text}")
        self.status.setStyleSheet(f"color: {color};")
        self.status.setToolTip(s.detail if s.status is SessionStatus.ERROR else "")
        if s.is_running:
            if s.stopping:
                self.run_button.setText("Force stop")
                self.run_button.setIcon(self._icons.icon("octagon-x", theme.TEXT, 16))
                self.run_button.setToolTip("The script did not exit yet: terminate it now")
            else:
                self.run_button.setText("Stop")
                self.run_button.setIcon(self._icons.icon("square", theme.TEXT, 16))
                self.run_button.setToolTip("Interrupt the script (Ctrl+C), then terminate it if it does not exit")
            self.run_button.setObjectName("")
            self.run_button.setStyleSheet("")
        else:
            self.run_button.setText("Run" if s.run_count == 0 else "Run again")
            self.run_button.setIcon(self._icons.icon("play", theme.TEXT, 16))
            self.run_button.setToolTip("Start a new process; its output is appended below")
            self.run_button.setObjectName("Primary")
        self.run_button.style().unpolish(self.run_button)
        self.run_button.style().polish(self.run_button)

    # ------------------------------------------------------------------ actions
    def _run_or_stop(self) -> None:
        s = self.current_session()
        if s is None:
            return
        if s.is_running:
            s.stop()
        else:
            s.run()
            pane = self._panes.get(s.id)
            if pane is not None and s.is_running:
                pane.input.setFocus()
