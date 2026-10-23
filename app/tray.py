"""System tray icon, its menu, and desktop notifications.

* The window's close button hides the window to the tray (processes and queues keep running); "Quit" in the tray
  menu really quits.  If the system has no tray, nothing changes: close quits as before (``available`` is False).
* Notifications (one per finished thing, never a stream):
    - the main process of an instance ended: success, failure, or could not start (not when *you* stopped it);
    - an instance's queue ended: completed, with failures, or stopped by a failure / timeout (not when you stopped it).
  A main process whose queue is about to run is not announced - the queue's end is.
  They are shown only while the window is not in front, and can be turned off from the tray menu (saved in
  config.json as ``"notifications"``).  Clicking one brings the window up on that instance.
"""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from . import theme
from .config import AppConfig
from .icon_manager import IconManager
from .queue_model import QueueState
from .session_manager import Session, SessionManager

log = logging.getLogger(__name__)


def app_icon(icons: IconManager) -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128):
        icon.addPixmap(icons.pixmap("wrench", theme.INFO, size))
    return icon


class TrayController(QObject):
    showRequested = Signal()
    quitRequested = Signal()
    sessionRequested = Signal(object)          # a notification was clicked: show this instance

    def __init__(self, icons: IconManager, config: AppConfig, manager: SessionManager | None = None,
                 parent: QObject | None = None, available: bool | None = None):
        super().__init__(parent)
        self._config = config
        self._manager = manager
        self.available = QSystemTrayIcon.isSystemTrayAvailable() if available is None else available
        self.window_in_front: Callable[[], bool] = lambda: False       # set by the main window
        self._last_session: Session | None = None
        self._queue_prev: dict[str, QueueState] = {}
        self.sent: list[tuple[str, str]] = []                          # (title, text) of every notification shown
        self.icon = app_icon(icons)
        self.tray: QSystemTrayIcon | None = None
        self.running_action: QAction | None = None
        self.notify_action: QAction | None = None
        if self.available:
            self._build()
        if manager is not None:
            self.watch(manager)

    # ------------------------------------------------------------------ tray icon + menu
    def _build(self) -> None:
        self.tray = QSystemTrayIcon(self.icon, self)
        self.tray.setToolTip("Python Toolbox")
        menu = QMenu()
        show = menu.addAction("Show Python Toolbox")
        show.triggered.connect(self.showRequested)
        menu.addSeparator()
        self.running_action = menu.addAction("Nothing is running")
        self.running_action.setEnabled(False)
        self.notify_action = menu.addAction("Notifications")
        self.notify_action.setCheckable(True)
        self.notify_action.setChecked(self._config.notifications)
        self.notify_action.toggled.connect(self.set_notifications)
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self.quitRequested)
        menu.aboutToShow.connect(self._refresh_menu)
        self._menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._activated)
        self.tray.messageClicked.connect(self._message_clicked)
        self.tray.show()

    def _refresh_menu(self) -> None:
        n = len(self._manager.running()) if self._manager is not None else 0
        self.running_action.setText(f"{n} running" if n else "Nothing is running")
        self.notify_action.blockSignals(True)
        self.notify_action.setChecked(self._config.notifications)
        self.notify_action.blockSignals(False)

    def _activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.showRequested.emit()

    def _message_clicked(self) -> None:
        if self._last_session is not None and self._manager is not None and self._last_session in self._manager.sessions:
            self.sessionRequested.emit(self._last_session)
        else:
            self.showRequested.emit()

    def set_notifications(self, enabled: bool) -> None:
        old = self._config.notifications
        self._config.notifications = bool(enabled)
        error = self._config.save()
        if error:
            log.warning(error)
            self._config.notifications = old

    def hide(self) -> None:
        if self.tray is not None:
            self.tray.hide()

    # ------------------------------------------------------------------ notifications
    def watch(self, manager: SessionManager) -> None:
        self._manager = manager
        manager.sessionAdded.connect(self._session_added)
        for s in manager.sessions:
            self._session_added(s)

    def _session_added(self, session: Session) -> None:
        session.exited.connect(lambda code, stopped, s=session: self._main_exited(s, code, stopped))
        q = session.queue
        self._queue_prev[session.id] = q.state
        q.stateChanged.connect(lambda s=session: self._queue_changed(s))

    def _main_exited(self, session: Session, code, stopped: bool) -> None:
        if stopped:
            return                                                     # you stopped it yourself
        q = session.queue
        if q.executing:
            return                                                     # the queue runs next: its end is announced
        name = self._manager.label(session) if self._manager else session.tool.meta.name
        pending = len(q.pending_cells())
        if code is None:
            self.notify(f"{name} could not start", session.detail or "See the output for details.", session, True)
        elif code == 0:
            self.notify(f"{name} finished", "Completed successfully.", session, False)
        else:
            extra = f" {pending} queued cell{'s' if pending != 1 else ''} did not run." if pending else ""
            self.notify(f"{name} failed", f"Exit code {code}.{extra}", session, True)

    def _queue_changed(self, session: Session) -> None:
        q = session.queue
        before = self._queue_prev.get(session.id, QueueState.EMPTY)
        self._queue_prev[session.id] = q.state
        if before is not QueueState.EXECUTING or q.state not in (QueueState.COMPLETED, QueueState.STOPPED):
            return
        if q.discarded or q.user_stopped:
            return
        name = self._manager.label(session) if self._manager else session.tool.meta.name
        bad = q.state is QueueState.STOPPED or "failed" in q.state_text
        self.notify(f"{name}: queue {'stopped' if q.state is QueueState.STOPPED else 'finished'}",
                    q.state_text.replace("Last run: ", "").split(". The cells")[0], session, bad)

    def should_notify(self) -> bool:
        return self.available and self._config.notifications and not self.window_in_front()

    def notify(self, title: str, text: str, session: Session | None = None, failure: bool = False) -> bool:
        """Show a notification if they are on, a tray exists and the window is not in front. Returns whether shown."""
        if not self.should_notify():
            return False
        self._last_session = session
        self.sent.append((title, text))
        self._show(title, text, failure)
        return True

    def _show(self, title: str, text: str, failure: bool) -> None:
        if self.tray is not None:
            icon = QSystemTrayIcon.MessageIcon.Warning if failure else QSystemTrayIcon.MessageIcon.Information
            self.tray.showMessage(title, text, icon, 8000)

    def hint_hidden(self) -> None:
        """One-time message the first time the window goes to the tray."""
        if self.tray is not None:
            self.tray.showMessage("Python Toolbox is still running",
                                  "Running tools and queues keep going. Click the tray icon to open it, or choose "
                                  "Quit from its menu.", QSystemTrayIcon.MessageIcon.Information, 5000)
