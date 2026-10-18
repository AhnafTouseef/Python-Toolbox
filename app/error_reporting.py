"""Unexpected-error reporting that is safe to trigger from any thread.

Qt widgets may only be created on the GUI thread.  An exception raised in a worker thread
(or any thread other than the main one) is therefore logged where it happens and the dialog
is shown later by the GUI thread through a queued signal.
"""

from __future__ import annotations

import logging
import sys
import threading
import traceback

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox

log = logging.getLogger("toolbox")


class ErrorReporter(QObject):
    _show = Signal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._reported = False
        self._show.connect(self._display)          # queued automatically when emitted from another thread

    def install(self) -> None:
        sys.excepthook = self.hook
        threading.excepthook = lambda a: self.hook(a.exc_type, a.exc_value, a.exc_traceback)

    def hook(self, exc_type, exc, tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log.error("Unhandled exception:\n%s", text)
        self._show.emit(text)

    def _display(self, text: str) -> None:
        """Runs on the GUI thread. Only the first error opens a dialog; the rest are in the log."""
        if self._reported:
            return
        self._reported = True
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("Unexpected error")
        box.setText("Something went wrong in Python Toolbox. Your running tools are not affected.")
        box.setInformativeText("Further errors of this kind are written to the console only.")
        box.setDetailedText(text)
        box.exec()
