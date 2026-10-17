from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QDialog, QFormLayout, QLineEdit, QPushButton, QVBoxLayout, QLabel, QWidget, QComboBox, QHBoxLayout, QColorDialog

from app.icon_manager import IconManager
from app.tool_metadata import valid_hex_color


class ColorPreview(QWidget):
    def __init__(self, color: str = "#A78BFA"):
        super().__init__()
        self.color = QColor(color)
        self.setMinimumSize(48, 24)
        self.setStyleSheet("background: transparent; border: 1px solid #374151; border-radius: 6px;")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.color)

    def set_color(self, color: str):
        self.color = QColor(color)
        self.update()


class ToolSettingsDialog(QDialog):
    def __init__(self, tool_dir: Path, metadata: dict):
        super().__init__()
        self.tool_dir = tool_dir
        self.metadata = metadata
        self.setWindowTitle(f"Settings - {metadata.get('name', tool_dir.name)}")
        self.resize(520, 340)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.name_edit = QLineEdit(metadata.get("name", tool_dir.name))
        self.description_edit = QLineEdit(metadata.get("description", ""))

        self.icon_combo = QComboBox()
        self.icon_combo.addItems(sorted(IconManager().names))
        self.icon_combo.setCurrentText(metadata.get("icon", "wrench"))

        self.color_edit = QLineEdit(metadata.get("color", "#A78BFA"))
        self.color_preview = ColorPreview(metadata.get("color", "#A78BFA"))
        self.color_picker_button = QPushButton("Pick color")
        self.color_picker_button.clicked.connect(self._pick_color)
        self.color_edit.textChanged.connect(self._sync_preview)

        form.addRow("Name", self.name_edit)
        form.addRow("Description", self.description_edit)
        form.addRow("Icon", self.icon_combo)

        color_row = QWidget()
        color_layout = QHBoxLayout(color_row)
        color_layout.setContentsMargins(0, 0, 0, 0)
        color_layout.setSpacing(8)
        color_layout.addWidget(self.color_edit)
        color_layout.addWidget(self.color_preview)
        color_layout.addWidget(self.color_picker_button)
        form.addRow("Color", color_row)

        save_button = QPushButton("Save")
        cancel_button = QPushButton("Cancel")
        save_button.clicked.connect(self.save)
        cancel_button.clicked.connect(self.reject)

        buttons = QWidget(); buttons_layout = QHBoxLayout(buttons)
        buttons_layout.addStretch()
        buttons_layout.addWidget(save_button)
        buttons_layout.addWidget(cancel_button)

        layout.addLayout(form)
        layout.addWidget(buttons)

        self.color_edit.setStyleSheet("border: 1px solid #2D3748;")
        self.color_picker_button.setStyleSheet(
            "QPushButton { background: #1F2937; color: #E2E8F0; border: 1px solid #374151; border-radius: 8px; padding: 6px 10px; }"
            "QPushButton:hover { background: #2A3443; border-color: #60A5FA; }"
        )

    def _sync_preview(self, value: str):
        if valid_hex_color(value):
            self.color_preview.set_color(value)
            self.color_edit.setStyleSheet("border: 1px solid #2D3748;")

    def _pick_color(self):
        current = self.color_edit.text().strip() or "#A78BFA"
        color = QColorDialog.getColor(QColor(current), self, "Choose a color")
        if color.isValid():
            hex_value = color.name()
            self.color_edit.setText(hex_value)
            self.color_preview.set_color(hex_value)
            self.color_edit.setStyleSheet("border: 1px solid #2D3748;")

    def save(self):
        name = self.name_edit.text().strip() or self.tool_dir.name
        description = self.description_edit.text()
        icon = self.icon_combo.currentText().strip()
        color = self.color_edit.text().strip()

        if not valid_hex_color(color):
            self.color_edit.setStyleSheet("border: 1px solid #EF4444;")
            return

        if not IconManager().has_icon(icon):
            self.icon_combo.setStyleSheet("border: 1px solid #EF4444;")
            return

        self.metadata = {
            "name": name,
            "description": description,
            "icon": icon,
            "color": color,
        }
        self.accept()
