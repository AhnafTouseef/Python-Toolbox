"""The Workflows page: the queue panel on a page of its own (full screen), without a tool behind it.

A workflow is a queue that is not attached to the main process of a tool: it has no "start when the main process
exits" - it runs when you press Run now (or a cell's play button).  Everything else is the queue: the same cells
(Python, shell, tool), shared variables during a run, predefined answers, Must Execute, presets.  Several workflows
can be open as tabs.  A workflow exists while the Toolbox is open (like a queue); "Save as preset" keeps its cells.

The cells run in the workflow's working folder with the interpreter rule of a tool: a ``.venv`` in that folder if it
has one, otherwise the global Python.  The default folder is ``workflows/`` next to config.json.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QPushButton, QSizePolicy, QTabBar, QToolButton,
                               QVBoxLayout, QWidget)

from . import theme
from .icon_manager import IconManager
from .presets import PresetStore
from .queue_drawer import QueueDrawer
from .session_manager import Session, SessionManager
from .tool_registry import ToolRegistry


class WorkflowView(QWidget):
    backRequested = Signal()
    newRequested = Signal()
    closeRequested = Signal(object)          # Session: the tab's close button (asks first when needed)
    quickCloseRequested = Signal(object)     # Session: double-click on the tab (no question)
    currentChanged = Signal(object)          # Session or None

    def __init__(self, manager: SessionManager, registry: ToolRegistry, icons: IconManager,
                 presets: PresetStore | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Page")
        self._manager = manager
        self._icons = icons

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
        self.tabs.setAccessibleName("Workflows")
        self.tabs.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.tabs.setMinimumWidth(120)
        self.plus_button = QToolButton()
        self.plus_button.setObjectName("Flat")
        self.plus_button.setIcon(icons.icon("plus", theme.TEXT, 16))
        self.plus_button.setToolTip("New workflow")
        self.plus_button.setAccessibleName("New workflow")
        self.folder_button = QPushButton(" Folder…")
        self.folder_button.setIcon(icons.icon("folder-open", theme.TEXT, 16))
        self.folder_button.setAccessibleName("Working folder")
        self.folder_button.setToolTip("The folder this workflow's cells run in")
        tl.addWidget(self.back_button, 0, Qt.AlignmentFlag.AlignVCenter)
        tl.addWidget(self.tabs, 1, Qt.AlignmentFlag.AlignBottom)
        tl.addWidget(self.plus_button, 0, Qt.AlignmentFlag.AlignVCenter)
        tl.addWidget(self.folder_button, 0, Qt.AlignmentFlag.AlignVCenter)
        tl.setAlignment(self.tabs, Qt.AlignmentFlag.AlignBottom)

        self.drawer = QueueDrawer(manager, registry, icons, self, presets, docked=True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(top)
        lay.addWidget(self.drawer, 1)

        self.back_button.clicked.connect(self.backRequested)
        self.plus_button.clicked.connect(self.newRequested)
        self.folder_button.clicked.connect(self.choose_folder)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.tabs.tabBarDoubleClicked.connect(self._tab_double_clicked)
        manager.sessionAdded.connect(self.add_session)
        manager.sessionRemoved.connect(self.remove_session)
        manager.sessionChanged.connect(self._on_session_changed)
        self._sync()

    # ------------------------------------------------------------------ tabs
    def _index_of(self, session: Session) -> int:
        for i in range(self.tabs.count()):
            if self.tabs.tabData(i) == session.id:
                return i
        return -1

    def current_session(self) -> Session | None:
        i = self.tabs.currentIndex()
        return self._manager.get(self.tabs.tabData(i)) if i >= 0 else None

    def add_session(self, session: Session) -> None:
        if not session.is_workflow:
            return
        idx = self.tabs.addTab("")
        self.tabs.setTabData(idx, session.id)
        close = QToolButton()
        close.setObjectName("Flat")
        close.setIcon(self._icons.icon("x", theme.TEXT_MUTED, 14))
        close.setIconSize(QSize(14, 14))
        close.setFixedSize(20, 20)
        close.setToolTip("Close this workflow (its cells are discarded - use Save as preset to keep them). "
                         "Double-click the tab to close it without asking.")
        close.setAccessibleName("Close workflow")
        close.clicked.connect(lambda _=False, sid=session.id: self._close_clicked(sid))
        self.tabs.setTabButton(idx, QTabBar.ButtonPosition.RightSide, close)
        self._refresh_tabs()

    def remove_session(self, session: Session) -> None:
        if not session.is_workflow:
            return
        idx = self._index_of(session)
        if idx >= 0:
            self.tabs.removeTab(idx)                 # the tab bar selects a neighbouring tab by itself
        if self.tabs.count() == 0:
            self.drawer.bind(None)
        self._refresh_tabs()
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

    def _tab_double_clicked(self, index: int) -> None:
        if index < 0:
            return
        s = self._manager.get(self.tabs.tabData(index))
        if s is not None:
            self.quickCloseRequested.emit(s)

    def _on_tab_changed(self, index: int) -> None:
        s = self._manager.get(self.tabs.tabData(index)) if index >= 0 else None
        if s is not self.drawer.session:
            self.drawer.bind(s)
        if s is not None:
            self._manager.touch(s)
        self._sync()
        self.currentChanged.emit(s)

    def _on_session_changed(self, session: Session) -> None:
        if session.is_workflow:
            self._refresh_tabs()
            if session is self.current_session():
                self._sync()

    def _refresh_tabs(self) -> None:
        for i in range(self.tabs.count()):
            s = self._manager.get(self.tabs.tabData(i))
            if s is None:
                continue
            m = s.tool.meta
            self.tabs.setTabText(i, self._manager.label(s))
            self.tabs.setTabIcon(i, self._icons.icon("workflow", theme.OK if s.is_active else m.color, 16))
            self.tabs.setTabToolTip(i, f"{self._manager.label(s)} · {s.tool.path}"
                                       + (f" · {s.queue.state_text}" if s.is_active else ""))

    def _sync(self) -> None:
        s = self.current_session()
        self.folder_button.setEnabled(s is not None and not s.is_active)
        self.folder_button.setToolTip(f"Working folder: {s.tool.path}\nChange the folder this workflow's cells run in"
                                      if s is not None else "The folder this workflow's cells run in")

    # ------------------------------------------------------------------ actions
    def choose_folder(self) -> None:
        s = self.current_session()
        if s is None or s.is_active:
            return
        chosen = QFileDialog.getExistingDirectory(self, "Working folder for this workflow", str(s.tool.path))
        if chosen:
            s.set_workflow_folder(chosen)
            self._refresh_tabs()
            self._sync()
