"""Tool settings dialog (name, description, icon, color) and the icon picker."""

from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QModelIndex, QSize, Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListView, QPushButton, QVBoxLayout, QWidget)

from . import theme
from . import tool_metadata as tm
from .color_picker import ColorPickerDialog
from .icon_manager import IconManager
from .tool_card import IconTile
from .tool_registry import ToolInfo, ToolRegistry


class _IconModel(QAbstractListModel):
    """All bundled icons; rendered lazily by the view (only visible rows ask for a decoration)."""

    def __init__(self, icons: IconManager, color: str, parent=None):
        super().__init__(parent)
        self._icons = icons
        self._color = color
        self._names = list(icons.names)

    def set_filter(self, text: str) -> None:
        words = text.casefold().split()
        self.beginResetModel()
        if not words:
            self._names = list(self._icons.names)
        else:
            self._names = [n for n in self._icons.names
                           if all(w in n or any(w in t.casefold() for t in self._icons.tags(n)) for w in words)]
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._names)

    def name_at(self, row: int) -> str:
        return self._names[row]

    def row_of(self, name: str) -> int:
        try:
            return self._names.index(name)
        except ValueError:
            return -1

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        name = self._names[index.row()]
        if role == Qt.ItemDataRole.DecorationRole:
            return self._icons.icon(name, self._color, 26)
        if role == Qt.ItemDataRole.ToolTipRole:
            return name
        if role == Qt.ItemDataRole.UserRole:
            return name
        return None


class IconPickerDialog(QDialog):
    def __init__(self, icons: IconManager, current: str, color: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Choose icon")
        self.setModal(True)
        self.resize(520, 520)
        self._selected = current if icons.has(current) else icons.generic

        self.search = QLineEdit()
        self.search.setPlaceholderText(f"Search {len(icons.names)} icons (name or keyword)")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Search icons")
        self.model = _IconModel(icons, color, self)
        self.view = QListView()
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setUniformItemSizes(True)
        self.view.setIconSize(QSize(26, 26))
        self.view.setGridSize(QSize(50, 50))
        self.view.setSpacing(2)
        self.view.setWordWrap(False)
        self.view.setModel(self.model)
        self.view.setAccessibleName("Icons")
        self.label = QLabel(self._selected)
        self.label.setObjectName("Muted")

        buttons = QDialogButtonBox()
        self.ok = buttons.addButton("Select", QDialogButtonBox.ButtonRole.AcceptRole)
        self.ok.setObjectName("Primary")
        buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 12)
        lay.addWidget(self.search)
        lay.addWidget(self.view, 1)
        row = QHBoxLayout()
        row.addWidget(self.label, 1)
        row.addWidget(buttons)
        lay.addLayout(row)

        self.search.textChanged.connect(self._on_search)
        self.view.selectionModel().currentChanged.connect(self._on_current)
        self.view.doubleClicked.connect(lambda _i: self.accept())
        self._select(self._selected)

    def _select(self, name: str) -> None:
        row = self.model.row_of(name)
        if row >= 0:
            idx = self.model.index(row)
            self.view.setCurrentIndex(idx)
            self.view.scrollTo(idx, QListView.ScrollHint.PositionAtCenter)

    def _on_search(self, text: str) -> None:
        self.model.set_filter(text)
        self._select(self._selected)

    def _on_current(self, current: QModelIndex, _prev: QModelIndex) -> None:
        if current.isValid():
            self._selected = self.model.name_at(current.row())
            self.label.setText(self._selected)
        self.ok.setEnabled(current.isValid() or bool(self._selected))

    def selected_icon(self) -> str:
        return self._selected


class _Swatch(QPushButton):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(34, 26)
        self.setAccessibleName("Current color")

    def set_color(self, hex_color: str) -> None:
        self.setStyleSheet(f"QPushButton {{ background: {hex_color}; border: 1px solid {theme.BORDER_STRONG};"
                           f" border-radius: 6px; padding: 0; }}")


class ToolSettingsDialog(QDialog):
    """Edits the four app-managed attributes. Saving writes tool.json; failure is shown, never hidden."""

    def __init__(self, tool: ToolInfo, registry: ToolRegistry, icons: IconManager, parent: QWidget | None = None):
        super().__init__(parent)
        self._tool = tool
        self._registry = registry
        self._icons = icons
        self._icon = tool.meta.icon
        self._color = tool.meta.color
        self.setWindowTitle(f"Settings — {tool.meta.name}")
        self.setModal(True)
        self.setMinimumWidth(440)

        self.tile = IconTile(icons, self)
        self.tile.set_icon(self._icon, self._color)
        folder = QLabel(str(tool.path))
        folder.setObjectName("Muted")
        folder.setWordWrap(True)
        folder.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.name_edit = QLineEdit(tool.meta.name)
        self.name_edit.setAccessibleName("Name")
        self.desc_edit = QLineEdit(tool.meta.description)
        self.desc_edit.setPlaceholderText("Optional")
        self.desc_edit.setMaxLength(240)
        self.desc_edit.setAccessibleName("Description")

        self.icon_label = QLabel(self._icon)
        self.icon_button = QPushButton("Change…")
        icon_row = QHBoxLayout()
        icon_row.addWidget(self.icon_label, 1)
        icon_row.addWidget(self.icon_button)

        self.swatch = _Swatch(self)
        self.color_label = QLabel(self._color)
        self.color_label.setFont(theme.mono_font(10))
        self.color_button = QPushButton("Change…")
        color_row = QHBoxLayout()
        color_row.addWidget(self.swatch)
        color_row.addWidget(self.color_label, 1)
        color_row.addWidget(self.color_button)
        self.swatch.set_color(self._color)

        self.error = QLabel("")
        self.error.setObjectName("Error")
        self.error.setWordWrap(True)
        self.error.hide()

        buttons = QDialogButtonBox()
        self.save_button = buttons.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        self.save_button.setObjectName("Primary")
        self.save_button.setDefault(True)
        buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        head = QHBoxLayout()
        head.setSpacing(14)
        head.addWidget(self.tile)
        head.addWidget(folder, 1)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        form.addRow("Name", self.name_edit)
        form.addRow("Description", self.desc_edit)
        form.addRow("Icon", icon_row)
        form.addRow("Color", color_row)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 16)
        lay.setSpacing(14)
        lay.addLayout(head)
        lay.addLayout(form)
        lay.addWidget(self.error)
        lay.addWidget(buttons)

        self.icon_button.clicked.connect(self._pick_icon)
        self.color_button.clicked.connect(self._pick_color)
        self.swatch.clicked.connect(self._pick_color)

    # -- pickers --------------------------------------------------------------
    def _pick_icon(self) -> None:
        dlg = IconPickerDialog(self._icons, self._icon, self._color, self)
        try:
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self._icon = dlg.selected_icon()
                self.icon_label.setText(self._icon)
                self.tile.set_icon(self._icon, self._color)
        finally:
            dlg.deleteLater()

    def _pick_color(self) -> None:
        dlg = ColorPickerDialog(self._color, self, self._icons, self._icon)
        try:
            if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_hex():
                self._color = dlg.selected_hex()
                self.color_label.setText(self._color)
                self.swatch.set_color(self._color)
                self.tile.set_icon(self._icon, self._color)
        finally:
            dlg.deleteLater()

    # -- save -------------------------------------------------------------------
    def _show_error(self, text: str) -> None:
        self.error.setText(text)
        self.error.setVisible(bool(text))

    def new_metadata(self) -> tm.ToolMetadata:
        return tm.ToolMetadata(self.name_edit.text().strip(), self.desc_edit.text(), self._icon, self._color)

    def _save(self) -> None:
        meta = self.new_metadata()
        ok, err = self._registry.update_metadata(self._tool.id, meta)
        if not ok:
            self._show_error(err)      # dialog stays open; nothing was claimed as saved
            return
        self.accept()
