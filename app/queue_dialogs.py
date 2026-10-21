"""Dialogs of the queue drawer: the tool picker (searchable list) and the preset dialogs (save / import)."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QSize, Qt
from PySide6.QtGui import QColor, QKeyEvent
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout, QWidget)

from . import theme
from .icon_manager import IconManager
from .presets import Preset, PresetCell, PresetStore, ResolvedCell, resolve_cells
from .queue_model import LANG_SHELL, CellKind, QueueCell, parse_answers
from .tool_registry import ToolRegistry

ID_ROLE = Qt.ItemDataRole.UserRole


# =============================================================================== tool picker
class ToolCellDialog(QDialog):
    """Pick the tool of a tool cell from a searchable list (and, optionally, give the cell a name).

    Typing filters the list at once (name, description and folder name), Up/Down move the selection while the
    search box keeps the focus, Enter or a double-click confirms.  Tools with the same name show which path they
    come from.  Answers are edited on the cell itself.
    """

    def __init__(self, registry: ToolRegistry, icons: IconManager, cell: QueueCell | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._registry, self._icons = registry, icons
        editing = cell is not None
        self.setWindowTitle("Edit tool cell" if editing else "Add tool cell")
        self.setMinimumSize(460, 440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(8)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search tools by name or description")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Search tools")
        self.search.addAction(icons.icon("search", theme.TEXT_MUTED, 16), QLineEdit.ActionPosition.LeadingPosition)
        self.list = QListWidget()
        self.list.setAccessibleName("Tools")
        self.list.setIconSize(QSize(20, 20))
        self.list.setSpacing(1)
        self.list.setUniformItemSizes(True)
        self.empty = QLabel("No tool matches.")
        self.empty.setObjectName("Muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.hide()
        self.tool_info = QLabel()
        self.tool_info.setWordWrap(True)
        self.tool_info.setObjectName("Muted")
        self.tool_info.setMinimumHeight(40)
        self.tool_info.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        hint = QLabel("Launched exactly like the Run button does: the tool's own .venv interpreter if it has one, "
                      "otherwise the global Python.")
        hint.setWordWrap(True)
        hint.setObjectName("Muted")
        self.name = QLineEdit(cell.label if cell else "")
        self.name.setPlaceholderText("Optional — shown instead of the tool's name")
        self.name.setAccessibleName("Cell name")

        lay.addWidget(self.search)
        lay.addWidget(self.list, 1)
        lay.addWidget(self.empty)
        lay.addWidget(self.tool_info)
        lay.addWidget(hint)
        lay.addSpacing(2)
        lay.addWidget(QLabel("Name (optional)"))
        lay.addWidget(self.name)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setText("Save" if editing else "Add")
        self.ok.setObjectName("Primary")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addSpacing(4)
        lay.addWidget(self.buttons)

        tools = sorted(registry.tools, key=lambda t: (t.meta.name.casefold(), t.root_index))
        for t in tools:
            text = f"{t.meta.name}   ·   {t.tag}" if t.tag else t.meta.name
            item = QListWidgetItem(icons.icon(t.meta.icon, t.meta.color, 20), text)
            item.setData(ID_ROLE, t.id)
            item.setToolTip(f"{t.meta.description.strip()}\n{t.path}".strip())
            item.setData(Qt.ItemDataRole.UserRole + 1, f"{t.meta.name} {t.meta.description} {t.path.name}".casefold())
            self.list.addItem(item)
        if cell and cell.tool_id and registry.get(cell.tool_id) is None:      # the cell's tool is gone: show it, disabled
            item = QListWidgetItem(icons.icon("circle-x", theme.ERR, 20), f"{cell.tool_id} (not available)")
            item.setData(ID_ROLE, cell.tool_id)
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.insertItem(0, item)

        self.search.textChanged.connect(self._filter)
        self.list.currentItemChanged.connect(lambda *_: self._sync())
        self.list.itemDoubleClicked.connect(lambda _i: self.ok.click() if self.ok.isEnabled() else None)
        self.search.installEventFilter(self)
        self.search.returnPressed.connect(lambda: self.ok.click() if self.ok.isEnabled() else None)
        self.search.setFocus()
        if cell and cell.tool_id:
            self._select(cell.tool_id)
        else:
            self._first_visible()
        self._sync()

    # ------------------------------------------------------------------ list behaviour
    def _visible_items(self) -> list[QListWidgetItem]:
        return [self.list.item(i) for i in range(self.list.count())
                if not self.list.item(i).isHidden() and self.list.item(i).flags() & Qt.ItemFlag.ItemIsEnabled]

    def _first_visible(self) -> None:
        items = self._visible_items()
        self.list.setCurrentItem(items[0] if items else None)

    def _select(self, tool_id: str) -> None:
        for i in range(self.list.count()):
            if self.list.item(i).data(ID_ROLE) == tool_id and self.list.item(i).flags() & Qt.ItemFlag.ItemIsEnabled:
                self.list.setCurrentRow(i)
                self.list.scrollToItem(self.list.item(i))
                return

    def _filter(self, text: str) -> None:
        words = text.casefold().split()
        for i in range(self.list.count()):
            item = self.list.item(i)
            hay = item.data(Qt.ItemDataRole.UserRole + 1)
            item.setHidden(bool(words) and (hay is None or not all(w in hay for w in words)))
        current = self.list.currentItem()
        if current is None or current.isHidden():
            self._first_visible()
        self.empty.setVisible(not self._visible_items() and bool(words))
        self._sync()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.search and event.type() == QEvent.Type.KeyPress:
            key = event.key() if isinstance(event, QKeyEvent) else None
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
                items = self._visible_items()
                if items:
                    cur = self.list.currentItem()
                    idx = items.index(cur) if cur in items else -1
                    step = {Qt.Key.Key_Down: 1, Qt.Key.Key_Up: -1, Qt.Key.Key_PageDown: 8, Qt.Key.Key_PageUp: -8}[key]
                    nxt = items[max(0, min(len(items) - 1, idx + step if idx >= 0 else 0))]
                    self.list.setCurrentItem(nxt)
                    self.list.scrollToItem(nxt)
                return True
        return super().eventFilter(obj, event)

    def _sync(self) -> None:
        tool = self.selected_tool()
        item = self.list.currentItem()
        if tool is not None:
            desc = tool.meta.description.strip() or "No description."
            self.tool_info.setText(f"{desc}\n{tool.path}")
        else:
            self.tool_info.setText("Select a tool." if item is None else "This tool is not available.")
        self.ok.setEnabled(tool is not None)

    # ------------------------------------------------------------------ result
    def selected_tool(self):
        item = self.list.currentItem()
        if item is None or item.isHidden() or not item.flags() & Qt.ItemFlag.ItemIsEnabled:
            return None
        return self._registry.get(item.data(ID_ROLE) or "")

    def values(self) -> dict:
        tool = self.selected_tool()
        return {"label": self.name.text().strip(), "tool_id": tool.id if tool is not None else ""}

    def visible_names(self) -> list[str]:
        return [i.text() for i in self._visible_items()]


# =============================================================================== presets
class SavePresetDialog(QDialog):
    """Name the preset. An existing name is only replaced after the user confirms (see ``confirm_overwrite``)."""

    def __init__(self, store: PresetStore, n_cells: int, parent: QWidget | None = None):
        super().__init__(parent)
        self._store = store
        self.setWindowTitle("Save queue as preset")
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(8)
        info = QLabel(f"Saves the {n_cells} cell{'s' if n_cells != 1 else ''} of this queue (code, tools, names and "
                      "predefined answers) so it can be added to any queue later. Must Execute and results are not saved.")
        info.setWordWrap(True)
        info.setObjectName("Muted")
        self.name = QLineEdit()
        self.name.setPlaceholderText("Preset name, e.g. Render and clean up")
        self.name.setAccessibleName("Preset name")
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setStyleSheet(f"color: {theme.WARN};")
        self.message.hide()
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setText("Save")
        self.ok.setObjectName("Primary")
        self.ok.setEnabled(False)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(info)
        lay.addWidget(QLabel("Name"))
        lay.addWidget(self.name)
        lay.addWidget(self.message)
        lay.addSpacing(4)
        lay.addWidget(self.buttons)
        self.name.textChanged.connect(self._changed)
        self.overwrite = False

    def _changed(self, text: str) -> None:
        self.ok.setEnabled(bool(text.strip()))
        self.message.hide()

    def confirm_overwrite(self, name: str) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Replace preset?")
        box.setText(f"A preset named “{name}” already exists.")
        box.setInformativeText("Replace it with the cells of this queue?")
        yes = box.addButton("Replace", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is yes

    def _accept(self) -> None:
        name = self.name.text().strip()
        existing = self._store.find(name)
        if existing is not None:
            if not self.confirm_overwrite(existing.name):
                return
            self.overwrite = True
        self.accept()

    def preset_name(self) -> str:
        return self.name.text().strip()


class PresetsDialog(QDialog):
    """Browse the saved presets; **Add to queue** returns the cells to append (as if added by hand)."""

    def __init__(self, store: PresetStore, registry: ToolRegistry, icons: IconManager, parent: QWidget | None = None):
        super().__init__(parent)
        self._store, self._registry, self._icons = store, registry, icons
        self.result_specs: list[dict] = []
        self.setWindowTitle("Queue presets")
        self.setMinimumSize(560, 420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(8)
        info = QLabel("Presets are shared by every tool. Adding one appends its cells to the end of the current queue, "
                      "as if you had added them yourself.")
        info.setWordWrap(True)
        info.setObjectName("Muted")
        self.list = QListWidget()
        self.list.setAccessibleName("Presets")
        self.list.setMaximumHeight(150)
        self.preview = QListWidget()
        self.preview.setAccessibleName("Cells of the preset")
        self.preview.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.preview.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.empty = QLabel("No presets yet.\nUse “Save as preset” in the queue to create one.")
        self.empty.setObjectName("Muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.problems = QLabel()
        self.problems.setWordWrap(True)
        self.problems.setStyleSheet(f"color: {theme.WARN}; font-size: 12px;")
        self.problems.hide()

        self.add_button = QPushButton(" Add to queue")
        self.add_button.setObjectName("Primary")
        self.add_button.setIcon(icons.icon("list-plus", theme.TEXT, 16))
        self.rename_button = QPushButton("Rename…")
        self.delete_button = QPushButton(" Delete")
        self.delete_button.setIcon(icons.icon("trash-2", theme.TEXT, 16))
        close = QPushButton("Close")
        row = QHBoxLayout()
        row.addWidget(self.rename_button)
        row.addWidget(self.delete_button)
        row.addStretch(1)
        row.addWidget(close)
        row.addWidget(self.add_button)

        lay.addWidget(info)
        lay.addWidget(self.list)
        lay.addWidget(self.empty)
        lay.addWidget(QLabel("Cells"))
        lay.addWidget(self.preview, 1)
        lay.addWidget(self.problems)
        lay.addLayout(row)

        self.list.currentRowChanged.connect(lambda _r: self._show_selected())
        self.list.itemDoubleClicked.connect(lambda _i: self.add_selected())
        self.add_button.clicked.connect(self.add_selected)
        self.rename_button.clicked.connect(self.rename_selected)
        self.delete_button.clicked.connect(self.delete_selected)
        close.clicked.connect(self.reject)
        self.reload()

    # ------------------------------------------------------------------ list
    def reload(self, select: str | None = None) -> None:
        presets = self._store.list()
        self._presets = presets
        self.list.clear()
        for p in presets:
            item = QListWidgetItem(self._icons.icon("bookmark", theme.TEXT, 16), f"{p.name}   ·   {p.summary()}")
            item.setToolTip(f"Saved {p.saved.replace('T', ' ')}" if p.saved else p.name)
            self.list.addItem(item)
        self.list.setVisible(bool(presets))
        self.empty.setVisible(not presets)
        self.problems.setVisible(bool(self._store.problems))
        self.problems.setText("Could not read: " + "; ".join(self._store.problems) if self._store.problems else "")
        row = next((i for i, p in enumerate(presets) if select and p.name.casefold() == select.casefold()), 0)
        self.list.setCurrentRow(row if presets else -1)
        self._show_selected()

    def selected_preset(self) -> Preset | None:
        row = self.list.currentRow()
        return self._presets[row] if 0 <= row < len(self._presets) else None

    def describe(self, cell: PresetCell) -> str:
        n = len(parse_answers(cell.answers))
        extra = f"  ·  {n} answer{'s' if n != 1 else ''}" if n else ""
        if cell.kind is CellKind.CODE:
            first = next((ln.strip() for ln in cell.command.splitlines() if ln.strip()), "(empty)")
            if len(first) > 70:
                first = first[:69] + "…"
            lang = "Shell" if cell.language == LANG_SHELL else "Python"
            title = cell.label or first
            return f"{lang} code — {title}{extra}"
        tool = self._registry.resolve(cell.tool, cell.source)
        name = cell.label or (tool.meta.name if tool is not None else cell.tool)
        return f"Tool — {name}{extra}" if tool is not None else f"Tool — {name}  (not found here){extra}"

    def _show_selected(self) -> None:
        self.preview.clear()
        p = self.selected_preset()
        for i, c in enumerate(p.cells if p else [], 1):
            item = QListWidgetItem(f"{i}.  {self.describe(c)}")
            if c.kind is CellKind.TOOL and self._registry.resolve(c.tool, c.source) is None:
                item.setForeground(QColor(theme.WARN))
            self.preview.addItem(item)
        for b in (self.add_button, self.rename_button, self.delete_button):
            b.setEnabled(p is not None and (b is not self.add_button or bool(p.cells)))

    # ------------------------------------------------------------------ actions
    def choose_missing(self, missing: list[str]) -> str:
        """Some tools of the preset are not available here: 'add' them anyway, 'skip' them, or 'cancel'."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Some tools were not found")
        names = ", ".join(dict.fromkeys(missing))
        box.setText(f"{len(missing)} tool cell{'s' if len(missing) != 1 else ''} use tools that are not available here: {names}.")
        box.setInformativeText("Add the cells anyway (they will fail when they run), or skip them? Adding a folder that "
                               "contains the tool and pressing Refresh makes them usable.")
        add = box.addButton("Add anyway", QMessageBox.ButtonRole.AcceptRole)
        skip = box.addButton("Skip those cells", QMessageBox.ButtonRole.ActionRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return "add" if box.clickedButton() is add else "skip" if box.clickedButton() is skip else "cancel"

    def add_selected(self) -> None:
        p = self.selected_preset()
        if p is None or not p.cells:
            return
        cells: list[ResolvedCell] = resolve_cells(p, self._registry)
        missing = [c.missing_tool for c in cells if c.missing_tool]
        if missing:
            choice = self.choose_missing(missing)
            if choice == "cancel":
                return
            if choice == "skip":
                cells = [c for c in cells if not c.missing_tool]
        self.result_specs = [c.spec for c in cells]
        if not self.result_specs:
            return
        self.accept()

    def _ask_name(self, title: str, label: str, text: str = "") -> str | None:
        name, ok = QInputDialog.getText(self, title, label, QLineEdit.EchoMode.Normal, text)
        return name.strip() if ok else None

    def rename_selected(self) -> None:
        p = self.selected_preset()
        if p is None:
            return
        name = self._ask_name("Rename preset", "New name", p.name)
        if not name or name == p.name:
            return
        error = self._store.rename(p, name)
        if error:
            QMessageBox.warning(self, "Could not rename", error)
            return
        self.reload(select=name)

    def confirm_delete(self, p: Preset) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Delete preset?")
        box.setText(f"Delete the preset “{p.name}”?")
        box.setInformativeText("Queues that already used it are not affected.")
        yes = box.addButton("Delete", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Keep", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is yes

    def delete_selected(self) -> None:
        p = self.selected_preset()
        if p is None or not self.confirm_delete(p):
            return
        error = self._store.delete(p)
        if error:
            QMessageBox.warning(self, "Could not delete", error)
        self.reload()
