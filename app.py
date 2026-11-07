"""Python Toolbox - launcher and session manager for command-line Python scripts.

Run with:  python app.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app import gc_guard, theme  # noqa: E402
from app.error_reporting import ErrorReporter  # noqa: E402
from app.config import AppConfig  # noqa: E402
from app.icon_manager import IconManager  # noqa: E402
from app.main_window import MainWindow  # noqa: E402
from app.session_manager import SessionManager  # noqa: E402
from app.tool_registry import ToolRegistry  # noqa: E402
from app.tray import TrayController  # noqa: E402

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = QApplication(sys.argv)
    reporter = ErrorReporter(app)
    reporter.install()
    app.setApplicationName("Python Toolbox")
    theme.apply_theme(app)
    gc_guard.install(app)

    config = AppConfig.load(ROOT / "config.json")
    icons = IconManager(ROOT / "assets" / "icons", ROOT / "assets" / "lucide-tags.json")
    registry = ToolRegistry(icons, config.tool_paths, pinned=config.pinned)
    registry.scan()
    manager = SessionManager(registry, config)

    tray = TrayController(icons, config, manager)            # the close button sends the window here (if a tray exists)
    app.setQuitOnLastWindowClosed(not tray.available)
    window = MainWindow(registry, icons, config, manager, tray=tray if tray.available else None)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
