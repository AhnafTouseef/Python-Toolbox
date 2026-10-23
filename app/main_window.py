"""Main window: library <-> workspace pages with the active-process drawer layered on top."""

from __future__ import annotations

import logging

from PySide6.QtCore import QRect, QTimer, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QDialog, QMainWindow, QMessageBox, QStackedWidget, QWidget

from . import theme
from .active_process_drawer import ActiveProcessDrawer
from .config import AppConfig
from .icon_manager import IconManager
from .library_view import LibraryView
from .queue_drawer import QueueDrawer
from .session_manager import Session, SessionManager
from .editor import open_in_editor
from .presets import PresetStore
from .tool_folders import ToolFoldersDialog
from .tool_registry import ToolRegistry
from .tool_settings import ToolSettingsDialog
from .workspace_view import WorkspaceView

log = logging.getLogger(__name__)


class Stage(QWidget):
    """Holds the pages and the drawer layer; keeps the layer exactly on top of the pages."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.pages = QStackedWidget(self)
        self.drawer: ActiveProcessDrawer | None = None
        self.queue_drawer: QueueDrawer | None = None

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        rect = QRect(0, 0, self.width(), self.height())
        self.pages.setGeometry(rect)                 # pages get the full size; the drawers overlay them
        for d in (self.drawer, self.queue_drawer):
            if d is not None:
                d.fit_to(rect)


class MainWindow(QMainWindow):
    def __init__(self, registry: ToolRegistry, icons: IconManager, config: AppConfig, manager: SessionManager,
                 presets: PresetStore | None = None, tray=None):
        super().__init__()
        self.setWindowTitle("Python Toolbox")
        self.registry, self.icons, self.config, self.manager = registry, icons, config, manager
        self.presets = presets or PresetStore(config.path.parent / "presets")
        self.tray = tray
        self._quitting = False
        self._hinted = False
        self.resize(1100, 720)
        self.setMinimumSize(520, 420)

        self.stage = Stage()
        self.setCentralWidget(self.stage)
        self.library = LibraryView(registry, icons, manager.tool_status)
        self.workspace = WorkspaceView(manager, registry, icons)
        self.stage.pages.addWidget(self.library)
        self.stage.pages.addWidget(self.workspace)
        self.drawer = ActiveProcessDrawer(manager, icons, self.stage)
        self.stage.drawer = self.drawer
        self.queue_drawer = QueueDrawer(manager, registry, icons, self.stage, self.presets)
        self.stage.queue_drawer = self.queue_drawer

        self.library.openRequested.connect(self.open_tool)
        self.library.settingsRequested.connect(self.open_settings)
        self.library.pinRequested.connect(self.toggle_pin)
        self.library.editRequested.connect(self.edit_tool)
        self.workspace.editRequested.connect(lambda sess: sess is not None and self.edit_tool(sess.tool_id))
        self.library.activeRequested.connect(self.drawer.open_drawer)
        self.library.refreshRequested.connect(self.refresh_tools)
        self.library.foldersRequested.connect(self.open_tool_folders)
        self.workspace.backRequested.connect(self.show_library)
        self.workspace.queueRequested.connect(self.open_queue)
        self.workspace.currentChanged.connect(self._on_current_session)
        self.workspace.newInstanceRequested.connect(self.new_instance)
        self.workspace.closeRequested.connect(self.close_session)
        self.workspace.quickCloseRequested.connect(lambda sess: self.close_session(sess, confirm=False))
        self.drawer.sessionSelected.connect(self.show_session)
        manager.sessionChanged.connect(self._on_session_changed)
        manager.sessionRemoved.connect(lambda s: self._after_session_event(s.tool_id))
        manager.sessionAdded.connect(lambda s: self._after_session_event(s.tool_id))

        self.stage.pages.setCurrentWidget(self.library)
        self.drawer.fit_to(QRect(0, 0, self.stage.width(), self.stage.height()))
        self.queue_drawer.fit_to(QRect(0, 0, self.stage.width(), self.stage.height()))
        if registry.issues:
            log.warning("Tool notices:\n%s", "\n".join(registry.issues))
        if config.notice:
            log.warning(config.notice)
        if tray is not None:
            self.setWindowIcon(tray.icon)
            tray.window_in_front = lambda: self.isVisible() and not self.isMinimized() and self.isActiveWindow()
            tray.showRequested.connect(self.show_from_tray)
            tray.quitRequested.connect(self.quit_app)
            tray.sessionRequested.connect(self._show_session_from_tray)

    # ------------------------------------------------------------------ navigation
    def show_library(self) -> None:
        self.queue_drawer.close_now()                # the queue belongs to the cockpit; it keeps running and is kept
        self.stage.pages.setCurrentWidget(self.library)

    def open_queue(self) -> None:
        s = self.workspace.current_session()
        if s is not None:
            self.queue_drawer.open_for(s)

    def _on_current_session(self, session) -> None:
        if self.queue_drawer.is_open and session is not self.queue_drawer.session:
            self.queue_drawer.close_now()

    def show_workspace(self) -> None:
        self.stage.pages.setCurrentWidget(self.workspace)
        s = self.workspace.current_session()
        pane = self.workspace.pane(s) if s else None
        if pane is not None:
            (pane.input if pane.input.isEnabled() else self.workspace.run_button).setFocus()

    def open_tool(self, tool_id: str) -> None:
        """Library card clicked: go to the tool's most recently active instance, or open a first one."""
        s = self.manager.most_recent(tool_id)
        if s is None:
            try:
                s = self.manager.create(tool_id)
            except KeyError:
                QMessageBox.warning(self, "Tool not found", "This tool is no longer available.")
                return
        self.workspace.select_session(s)
        self.show_workspace()

    def show_session(self, session: Session) -> None:
        """Jump to one exact instance (used by the active-process drawer)."""
        self.workspace.select_session(session)
        self.show_workspace()

    def new_instance(self) -> None:
        current = self.workspace.current_session()
        if current is None:
            return
        s = self.manager.create(current.tool_id)
        self.workspace.select_session(s)
        self.show_workspace()

    # ------------------------------------------------------------------ closing instances
    def confirm_stop_and_close(self, session: Session) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Close running instance?")
        box.setText(f"“{self.manager.label(session)}” is still running.")
        info = "Closing the tab stops the process and discards its output."
        n = len(session.queue.pending_cells())
        if session.queue.executing:
            info += f" Its queue is running too: the running cell is stopped and the {n} pending cell{'s' if n != 1 else ''} will not run." if n else " Its queue is running too: the running cell is stopped."
        elif n:
            info += f" Its {n} queued cell{'s' if n != 1 else ''} will not run."
        box.setInformativeText(info)
        stop = box.addButton("Stop and close", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Keep running", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(box.buttons()[1])
        box.exec()
        return box.clickedButton() is stop

    def close_session(self, session: Session, confirm: bool = True) -> None:
        """Destroy an instance. With confirm=False (tab double-click) a running process is stopped without asking."""
        if confirm and session.is_active and not self.confirm_stop_and_close(session):
            return
        self.manager.remove(session)                 # destroys output/state; stops a live process
        if not self.manager.sessions:
            self.show_library()                      # last tab closed -> back to the library
        else:
            self.workspace.refresh_all()

    # ------------------------------------------------------------------ tool discovery
    def refresh_tools(self) -> None:
        """Look for added, removed and changed tools without restarting. Open instances are not touched."""
        summary = self.registry.scan()
        self.library.reload()
        self.workspace.refresh_all()
        bits = []
        if summary.added:
            bits.append(f"{len(summary.added)} new")
        if summary.removed:
            bits.append(f"{len(summary.removed)} removed")
        self.library.show_notice("Refreshed · " + (", ".join(bits) if bits else "no changes"))
        if self.registry.issues:
            log.warning("Tool notices:\n%s", "\n".join(self.registry.issues))

    def open_tool_folders(self) -> None:
        dlg = ToolFoldersDialog(self.registry.tools_dir, self.config.tool_paths, self.icons, self)
        try:
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            new = dlg.paths()
        finally:
            dlg.deleteLater()
        if new == self.config.tool_paths:
            return
        old = self.config.tool_paths
        self.config.tool_paths = new
        error = self.config.save()
        if error:
            self.config.tool_paths = old
            QMessageBox.warning(self, "Could not save", f"{error}\nThe folder list was not changed.")
            return
        self.registry.set_extra_dirs(new)
        self.refresh_tools()

    # ------------------------------------------------------------------ pinning / editor
    def toggle_pin(self, tool_id: str) -> None:
        """Pin a tool to the top of the library (or unpin it). Stored in config.json."""
        if self.registry.get(tool_id) is None:
            return
        old = list(self.config.pinned)
        pins = [t for t in old if t != tool_id] if tool_id in old else [*old, tool_id]
        self.config.pinned = pins
        error = self.config.save()
        if error:
            self.config.pinned = old
            QMessageBox.warning(self, "Could not save", f"{error}\nThe pin was not changed.")
            return
        self.registry.set_pinned(pins)
        self.library.refresh_pins()

    def edit_tool(self, tool_id: str) -> None:
        """Open the tool's main.py in the system's editor (see editor.py for how each OS does it)."""
        tool = self.registry.get(tool_id)
        if tool is None:
            return
        error = open_in_editor(tool.entry)
        if error:
            QMessageBox.warning(self, "Could not open the editor", error)

    # ------------------------------------------------------------------ settings
    def open_settings(self, tool_id: str) -> None:
        tool = self.registry.get(tool_id)
        if tool is None:
            return
        dlg = ToolSettingsDialog(tool, self.registry, self.icons, self)
        try:
            dlg.exec()
        finally:
            dlg.deleteLater()

    # ------------------------------------------------------------------ status plumbing
    def _on_session_changed(self, session: Session) -> None:
        self._after_session_event(session.tool_id)

    def _after_session_event(self, tool_id: str) -> None:
        self.library.refresh_status(tool_id)
        self.library.set_active_count(len(self.manager.running()))

    # ------------------------------------------------------------------ quitting
    def confirm_quit(self, n: int) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Quit Python Toolbox?")
        box.setText(f"{n} process{'es are' if n != 1 else ' is'} still running.")
        box.setInformativeText("Quitting stops them. Queued cells that have not run yet will not run "
                               "(the queue only works while the Toolbox is open).")
        quit_btn = box.addButton("Stop all and quit", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Keep running", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(box.buttons()[1])
        box.exec()
        return box.clickedButton() is quit_btn

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.tray is not None and self.tray.available and not self._quitting:
            event.ignore()                           # the X sends the window to the tray; everything keeps running
            self.hide()
            if not self._hinted:
                self._hinted = True
                self.tray.hint_hidden()
            return
        running = self.manager.running()
        if running and not self.confirm_quit(len(running)):
            self._quitting = False
            event.ignore()
            return
        self.manager.shutdown()
        event.accept()
        if self.tray is not None:
            self.tray.hide()
            QTimer.singleShot(0, QApplication.quit)  # (the app does not quit on "last window closed" with a tray)

    # ------------------------------------------------------------------ tray
    def show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _show_session_from_tray(self, session: Session) -> None:
        self.show_from_tray()
        self.show_session(session)

    def quit_app(self) -> None:
        """Tray "Quit" / real quit: asks first if something is running, then stops everything and exits."""
        self._quitting = True
        self.close()
