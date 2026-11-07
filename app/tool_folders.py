"""Dialog to manage the folders tools are discovered in.

Nothing is built in: the user adds any number of folders (each one holds tool folders, i.e. folders with a
main.py) and they are searched in the order listed.
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QVBoxLayout, QWidget)

from . import theme
from .icon_manager import IconManager
from .tool_registry import ENTRY_NAME, discover_tool_dirs, norm_path


class ToolFoldersDialog(QDialog):
    def __init__(self, paths: list[str], icons: IconManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Tool folders")
        self.setMinimumSize(560, 380)
        self._paths: list[str] = list(paths)
        self._icons = icons

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)
        intro = QLabel("Tools are looked for in these folders. Each one contains tool folders (a folder with a "
                       "main.py). If two tools have the same name, their cards show which path they come from.")
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        self.list = QListWidget()
        self.list.setAccessibleName("Tool folders")
        self.list.setIconSize(QSize(18, 18))
        self.list.setSpacing(2)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setStyleSheet(f"color: {theme.WARN};")
        self.message.hide()

        self.add_button = QPushButton(" Add folder…")
        self.add_button.setIcon(icons.icon("folder-plus", theme.TEXT, 16))
        self.add_button.setAccessibleName("Add folder")
        self.remove_button = QPushButton(" Remove")
        self.remove_button.setIcon(icons.icon("trash-2", theme.TEXT, 16))
        self.remove_button.setAccessibleName("Remove folder")
        side = QVBoxLayout()
        side.setSpacing(8)
        side.addWidget(self.add_button)
        side.addWidget(self.remove_button)
        side.addStretch(1)
        mid = QHBoxLayout()
        mid.addWidget(self.list, 1)
        mid.addLayout(side)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setText("Apply")
        self.ok.setObjectName("Primary")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        lay.addWidget(intro)
        lay.addLayout(mid, 1)
        lay.addWidget(self.message)
        lay.addWidget(self.buttons)

        self.add_button.clicked.connect(self._browse)
        self.remove_button.clicked.connect(self.remove_selected)
        self.list.currentRowChanged.connect(lambda _r: self._sync_buttons())
        self.list.itemDoubleClicked.connect(lambda _i: None)
        self._rebuild()

    # ------------------------------------------------------------------ model
    def paths(self) -> list[str]:
        """The folders, in the order listed."""
        return list(self._paths)

    def add_path(self, path: str) -> str | None:
        """Add a folder. Returns an error message, or None when it was added."""
        path = os.path.normpath(path.strip()) if path and path.strip() else ""
        if not path:
            return "Choose a folder."
        p = Path(path)
        if not p.is_dir():
            return f"“{path}” is not a folder that exists."
        key = norm_path(p)
        for existing in self._paths:
            if norm_path(existing) == key:
                return "That folder is already in the list."
        if os.path.isfile(os.path.join(path, ENTRY_NAME)) and not discover_tool_dirs(p):
            return (f"This folder is a tool itself (it has a {ENTRY_NAME}). Add the folder that contains your tool "
                    "folders instead.")
        self._paths.append(path)
        self._rebuild(select=len(self._paths) - 1)
        return None

    def remove_selected(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self._paths):
            del self._paths[row]
            self._rebuild(select=min(row, len(self._paths) - 1))

    # ------------------------------------------------------------------ view
    def _browse(self) -> None:
        start = self._paths[-1] if self._paths else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Add a folder that contains tool folders", start)
        if chosen:
            self._report(self.add_path(chosen))

    def _report(self, error: str | None) -> None:
        self.message.setVisible(bool(error))
        self.message.setText(error or "")

    def _describe(self, index: int, path: Path) -> tuple[str, str, bool]:
        ok = path.is_dir()
        n = len(discover_tool_dirs(path)) if ok else 0
        name = path.name or str(path)
        head = f"Path {index + 1} · {name}"
        status = (f"{n} tool{'s' if n != 1 else ''}" if ok else "folder not found") + f"   ·   {path}"
        return head, status, ok

    def _rebuild(self, select: int | None = None) -> None:
        self.list.clear()
        for i, path in enumerate(map(Path, self._paths)):
            head, status, ok = self._describe(i, path)
            item = QListWidgetItem(self._icons.icon("folder" if ok else "circle-x", theme.TEXT if ok else theme.ERR, 18),
                                   f"{head}\n{status}")
            item.setToolTip(str(path))
            self.list.addItem(item)
        self.list.setCurrentRow(select if select is not None else (0 if self._paths else -1))
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.remove_button.setEnabled(self.list.currentRow() >= 0)
