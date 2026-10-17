from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget, QStackedLayout, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QTabBar, QMessageBox, QToolButton

from app.active_process_drawer import ActiveProcessDrawer
from app.library_view import ToolLibraryView
from app.process_manager import ProcessManager
from app.session_manager import SessionManager, ToolInstance
from app.terminal_view import TerminalView
from app.theme import AppTheme
from app.tool_metadata import discover_tools, ensure_metadata, write_metadata


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Python Toolbox")
        self.resize(1200, 760)
        self.setStyleSheet(AppTheme.stylesheet())

        self.tools_dir = Path(__file__).resolve().parent.parent / "tools"
        self.global_python = "C:/Users/lenovo/AppData/Local/Programs/Python/Python312/python.exe"
        self.registry = {}
        self.session_manager = SessionManager()
        self.process_manager = ProcessManager()

        self.central = QWidget()
        self.stack = QStackedLayout(self.central)
        self.setCentralWidget(self.central)

        self.library_view = ToolLibraryView()
        self.library_view.setParent(self)
        self.library_view.session_manager = self.session_manager
        self.library_view.setObjectName("library")
        self.library_view.launch_requested.connect(self.open_tool_by_path)
        self.library_view.settings_requested.connect(self.open_settings_for_path)

        self.workspace = QWidget()
        self.workspace_layout = QVBoxLayout(self.workspace)
        self.workspace_layout.setContentsMargins(12, 12, 12, 12)

        self.top_bar = QWidget(); self.top_bar_layout = QHBoxLayout(self.top_bar)
        self.back_button = QPushButton("Back")
        self.back_button.clicked.connect(self.return_to_library)
        self.tab_bar = QTabBar()
        self.tab_bar.setExpanding(False)
        self.tab_bar.setAutoHide(False)
        self.tab_bar.setTabsClosable(True)
        self.tab_bar.tabCloseRequested.connect(self.close_instance_tab)
        self.tab_bar.tabBarClicked.connect(self.on_tab_selected)
        self.tab_bar.tabBarDoubleClicked.connect(self.on_tab_double_click)
        self._tab_instance_map = {}
        self._refreshing_tabs = False
        self.new_instance_button = QPushButton("+")
        self.new_instance_button.clicked.connect(self.create_new_instance)

        self.top_bar_layout.addWidget(self.back_button)
        self.top_bar_layout.addWidget(self.tab_bar, 1)
        self.top_bar_layout.addWidget(self.new_instance_button)

        self.run_button = QPushButton("Run")
        self.stop_button = QPushButton("Stop")
        self.clear_button = QPushButton("Clear")
        self.run_button.clicked.connect(self.run_active_instance)
        self.stop_button.clicked.connect(self.stop_active_instance)
        self.clear_button.clicked.connect(self.clear_output)

        self.controls = QWidget(); self.controls_layout = QHBoxLayout(self.controls)
        self.controls_layout.addWidget(self.run_button)
        self.controls_layout.addWidget(self.stop_button)
        self.controls_layout.addWidget(self.clear_button)

        self.terminal = TerminalView()
        self.terminal.input_line.returnPressed.connect(self.send_input)

        self.process_manager.output_received.connect(self.handle_process_output)
        self.process_manager.status_changed.connect(self.handle_process_status)

        self.workspace_layout.addWidget(self.top_bar)
        self.workspace_layout.addWidget(self.controls)
        self.workspace_layout.addWidget(self.terminal)

        self.drawer = ActiveProcessDrawer(self)
        self.drawer.list_widget.itemActivated.connect(self.open_instance_from_drawer)
        self.drawer.hide()

        self.stack.addWidget(self.library_view)
        self.stack.addWidget(self.workspace)
        self.stack.setCurrentIndex(0)

        self._library_load_timer = QTimer(self)
        self._library_load_timer.setSingleShot(True)
        self._library_load_timer.timeout.connect(self.load_library_tools)

        self.bind_drawer_button()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._library_load_timer.isActive():
            self._library_load_timer.start(0)

    def bind_drawer_button(self):
        self.active_process_button = QToolButton(self)
        self.active_process_button.setToolTip("Active processes")
        self.active_process_button.setText("◉")
        self.active_process_button.setFixedSize(32, 32)
        self.active_process_button.setStyleSheet(
            "QToolButton { border: 1px solid #334155; border-radius: 8px; background: #111827; color: #e2e8f0; font-size: 16px; } "
            "QToolButton:hover { background: #1f2937; border-color: #475569; } "
            "QToolButton:pressed { background: #0f172a; }"
        )
        self.active_process_button.clicked.connect(self.toggle_drawer)
        self.menuBar().setCornerWidget(self.active_process_button, Qt.TopRightCorner)

    def toggle_drawer(self):
        if self.drawer.isVisible():
            self.drawer.hide()
        else:
            self.drawer.set_geometry_for_window(self.geometry())
            self.drawer.show_drawer()

    def load_library_tools(self):
        self.library_view.start_background_discovery(self.tools_dir)
        self.refresh_drawer()

    def open_tool_by_path(self, path: str):
        tool_dir = Path(path)
        metadata = self.registry.get(path) or ensure_metadata(tool_dir)
        for instance in self.session_manager.instances.values():
            if instance.tool_dir == tool_dir:
                self.session_manager.set_active_instance(instance.instance_id)
                self.show_workspace_for_instance(instance.instance_id)
                return
        interpreter = self.resolve_interpreter(tool_dir)
        instance = self.session_manager.create_instance(metadata["name"], tool_dir, interpreter)
        self.show_workspace_for_instance(instance.instance_id)
        self.refresh_tabs()

    def open_settings_for_path(self, path: str):
        tool_dir = Path(path)
        metadata = ensure_metadata(tool_dir)
        from app.tool_settings import ToolSettingsDialog
        dialog = ToolSettingsDialog(tool_dir, metadata)
        if dialog.exec() == 1:
            data = dialog.metadata
            write_metadata(tool_dir / "tool.json", data)
            self.registry[str(tool_dir)] = data
            self.library_view.refresh_tool_card(tool_dir, data)
            self.load_library_tools()

    def resolve_interpreter(self, tool_dir: Path) -> str:
        venv_python = tool_dir / ".venv" / "Scripts" / "python.exe"
        if venv_python.exists():
            return str(venv_python)
        if (tool_dir / ".venv").exists():
            raise FileNotFoundError(f"Missing interpreter at {venv_python}. Please repair the tool environment.")
        return self.global_python

    def show_workspace_for_instance(self, instance_id: str):
        self.session_manager.set_active_instance(instance_id)
        instance = self.session_manager.get_active_instance()
        if not instance:
            return
        self.setWindowTitle(f"Python Toolbox - {instance.tool_name}")
        self.stack.setCurrentIndex(1)
        self.update_terminal_for_instance(instance)
        if not self._refreshing_tabs:
            self.refresh_tabs()

    def refresh_tabs(self):
        self._refreshing_tabs = True
        self._tab_instance_map = {}
        while self.tab_bar.count() > 0:
            self.tab_bar.removeTab(0)
        for instance_id in self.session_manager.tabs:
            instance = self.session_manager.get_instance(instance_id)
            if not instance:
                continue
            self.tab_bar.addTab(instance.tool_name)
            self._tab_instance_map[self.tab_bar.count() - 1] = instance_id
            close_button = QToolButton()
            close_button.setText("×")
            close_button.setToolTip("Close tab")
            close_button.setStyleSheet("QToolButton { border: none; background: transparent; color: #cbd5e1; padding: 0 4px; font-size: 14px; } QToolButton:hover { color: #ffffff; }")
            close_button.clicked.connect(lambda checked=False, tab_id=instance_id: self.close_instance_by_id(tab_id))
            self.tab_bar.setTabButton(self.tab_bar.count() - 1, self.tab_bar.ButtonPosition.RightSide, close_button)
            if instance_id == self.session_manager.active_instance_id:
                self.tab_bar.setCurrentIndex(self.tab_bar.count() - 1)
        self._refreshing_tabs = False

    def on_tab_selected(self, index):
        if self._refreshing_tabs or index < 0 or index not in self._tab_instance_map:
            return
        instance_id = self._tab_instance_map[index]
        if instance_id != self.session_manager.active_instance_id:
            self.show_workspace_for_instance(instance_id)

    def on_tab_double_click(self, index):
        instance_id = self._tab_instance_map.get(index)
        if instance_id:
            self.close_instance_by_id(instance_id)

    def close_instance_by_id(self, instance_id: str):
        if instance_id in self.session_manager.instances:
            self.session_manager.close_instance(instance_id)
            self.refresh_tabs()
            if not self.session_manager.tabs:
                self.return_to_library()
            else:
                self.show_workspace_for_instance(self.session_manager.tabs[-1])

    def close_instance_tab(self, index):
        instance_id = self._tab_instance_map.get(index)
        if instance_id:
            self.close_instance_by_id(instance_id)

    def run_active_instance(self):
        instance = self.session_manager.get_active_instance()
        if not instance:
            return
        main_script = instance.tool_dir / "main.py"
        if not main_script.exists():
            self.terminal.set_status("Main script missing")
            QMessageBox.warning(self, "Tool launch failed", f"{instance.tool_name} is missing main.py in its directory.")
            return
        try:
            result = self.process_manager.start_process(instance, main_script, instance.interpreter)
            if result == "started":
                self.terminal.set_status("Running")
                self.update_terminal_for_instance(instance)
                self.refresh_drawer()
        except Exception as exc:
            self.terminal.set_status("Launch failed")
            QMessageBox.warning(self, "Tool launch failed", str(exc))

    def stop_active_instance(self):
        instance = self.session_manager.get_active_instance()
        if not instance:
            return
        self.process_manager.stop_process(instance)
        self.update_terminal_for_instance(instance)
        self.refresh_drawer()

    def send_input(self):
        instance = self.session_manager.get_active_instance()
        if not instance:
            return
        text = self.terminal.input_line.text()
        if not text:
            return
        self.process_manager.write_stdin(instance, text)
        self.terminal.input_line.clear()
        self.terminal.append_text(f"{text}\n")

    def clear_output(self):
        instance = self.session_manager.get_active_instance()
        if instance is not None:
            instance.output.clear()
        self.terminal.output.clear()

    def return_to_library(self):
        self.stack.setCurrentIndex(0)
        self.setWindowTitle("Python Toolbox")
        self.refresh_drawer()

    def update_terminal_for_instance(self, instance: ToolInstance):
        self.terminal.output.clear()
        if instance.output:
            self.terminal.output.insertPlainText("\n".join(instance.output))
        self.terminal.set_status(instance.status)
        self.terminal.input_line.setText(instance.input_text)

    def refresh_drawer(self):
        self.drawer.clear()
        for instance_id, instance in self.session_manager.instances.items():
            if instance.status in {"Running", "Starting", "Idle"}:
                self.drawer.add_running_item(instance_id, f"{instance.tool_name} ({instance.instance_id[:8]})")

    def open_instance_from_drawer(self, item):
        instance_id = item.data(Qt.ItemDataRole.UserRole)
        if instance_id:
            self.show_workspace_for_instance(instance_id)
            self.drawer.hide()

    def handle_process_output(self, instance_id: str, text: str):
        instance = self.session_manager.get_instance(instance_id)
        if not instance:
            return
        instance.append_output(text)
        if self.session_manager.active_instance_id == instance_id:
            self.terminal.append_text(text)
            self.terminal.set_status(instance.status)

    def handle_process_status(self, instance_id: str, status: str):
        instance = self.session_manager.get_instance(instance_id)
        if not instance:
            return
        instance.status = status
        if self.session_manager.active_instance_id == instance_id:
            self.terminal.set_status(status)
        self.refresh_drawer()
        for key, card in self.library_view.card_map.items():
            if str(Path(card.tool_dir)) == str(instance.tool_dir):
                card.set_status_from_state(status)
                break

    def create_new_instance(self):
        """Create another tab for the currently active tool, or fall back to the library when nothing is open."""
        active = self.session_manager.get_active_instance()
        if not active:
            self.return_to_library()
            return

        new_instance = self.session_manager.create_instance(
            active.tool_name,
            active.tool_dir,
            active.interpreter,
        )
        self.show_workspace_for_instance(new_instance.instance_id)
        self.refresh_tabs()
        self.refresh_drawer()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.drawer.isVisible():
            self.drawer.set_geometry_for_window(self.geometry())

    def closeEvent(self, event):
        try:
            self.library_view.stop_background_discovery()
        except RuntimeError:
            pass

        if self.process_manager is not None:
            for proc in list(self.process_manager.running.values()):
                try:
                    proc.kill()
                    proc.waitForFinished(500)
                except RuntimeError:
                    pass
        super().closeEvent(event)
