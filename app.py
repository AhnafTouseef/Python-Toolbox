"""Python Toolbox - launcher and session manager for command-line Python scripts.

Run with:  python app.py
"""

from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app import gc_guard, theme  # noqa: E402
from app.config import AppConfig  # noqa: E402
from app.icon_manager import IconManager  # noqa: E402
from app.main_window import MainWindow  # noqa: E402
from app.session_manager import SessionManager  # noqa: E402
from app.tool_registry import ToolRegistry  # noqa: E402

log = logging.getLogger("toolbox")
_reported = False


def _excepthook(exc_type, exc, tb) -> None:
    """An unexpected error in the UI is reported once; the application keeps running."""
    global _reported
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    log.error("Unhandled exception:\n%s", text)
    if not _reported and QApplication.instance() is not None:
        _reported = True
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("Unexpected error")
        box.setText("Something went wrong in Python Toolbox. Your running tools are not affected.")
        box.setInformativeText("Further errors of this kind are written to the console only.")
        box.setDetailedText(text)
        box.exec()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.excepthook = _excepthook
    app = QApplication(sys.argv)
    app.setApplicationName("Python Toolbox")
    theme.apply_theme(app)
    gc_guard.install(app)

    config = AppConfig.load(ROOT / "config.json")
    icons = IconManager(ROOT / "assets" / "icons", ROOT / "assets" / "lucide-tags.json")
    registry = ToolRegistry(ROOT / "tools", icons)
    registry.scan()
    manager = SessionManager(registry, config)

    window = MainWindow(registry, icons, config, manager)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
