from __future__ import annotations

import math
from pathlib import Path
from typing import Dict

from PySide6.QtCore import Qt, Signal, QObject, Slot, QThread, QTimer, QPoint, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QScrollArea, QFrame, QLineEdit, QSizePolicy, QGraphicsOpacityEffect

from app.icon_manager import IconManager
from app.tool_metadata import discover_tools, ensure_metadata


def _lighten_hex(color: str, factor: float = 0.35) -> str:
    color = color.lstrip('#')
    if len(color) == 3:
        color = ''.join(ch * 2 for ch in color)
    r = int(color[0:2], 16)
    g = int(color[2:4], 16)
    b = int(color[4:6], 16)
    r = min(255, int(r + (255 - r) * factor))
    g = min(255, int(g + (255 - g) * factor))
    b = min(255, int(b + (255 - b) * factor))
    return f"#{r:02x}{g:02x}{b:02x}"


class ToolDiscoveryWorker(QObject):
    finished = Signal(list)

    def __init__(self, tools_dir: Path):
        super().__init__()
        self.tools_dir = tools_dir

    @Slot()
    def run(self):
        discovered = []
        for tool_dir in discover_tools(self.tools_dir):
            data = ensure_metadata(tool_dir)
            discovered.append({
                "path": str(tool_dir),
                "name": data["name"],
                "description": data["description"],
                "icon": data["icon"],
                "color": data["color"],
            })
        self.finished.emit(discovered)


class ToolCard(QFrame):
    launch_requested = Signal(str)
    settings_requested = Signal(str)

    def __init__(self, tool_name: str, description: str, icon_name: str, accent: str, tool_dir: Path, parent: QWidget | None = None):
        super().__init__(parent)
        self.tool_name = tool_name
        self.description = description
        self.icon_name = icon_name
        self.accent = accent
        self.tool_dir = tool_dir
        self.hover_accent = _lighten_hex(accent, 0.38)
        self._base_pos = self.pos()
        self._hovered = False
        self.setObjectName("toolCard")
        self.setMinimumHeight(160)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setStyleSheet(
            f"QFrame#toolCard {{ background: #171E2A; border: 2px solid {accent}; border-radius: 14px; }} "
            f"QFrame#toolCard:hover {{ border: 2px solid {self.hover_accent}; background: #1C2331; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        self.icon_label = QLabel(self)
        self.icon_label.setPixmap(IconManager().pixmap_for(icon_name, accent, 28))
        self.icon_label.setStyleSheet("background: transparent;")

        self.title = QLabel(tool_name, self)
        self.title.setStyleSheet("font-size: 16px; font-weight: 600; color: #f8fafc; background: transparent; border: none;")
        self.desc = QLabel(description or "", self)
        self.desc.setWordWrap(True)
        self.desc.setStyleSheet("color: #94A3B8; font-size: 12px; background: transparent; border: none;")
        self.desc.setVisible(bool(description))

        self.status = QLabel("Idle", self)
        self.status.setStyleSheet(f"padding: 4px 8px; border-radius: 8px; background: rgba(255,255,255,0.06); color: #e2e8f0; font-weight: 600; border: none;")

        self.settings_button = QPushButton("Settings", self)
        self.settings_button.setObjectName("setButton")
        self.settings_button.setStyleSheet("QPushButton#setButton { background: transparent; border: none; color: #cbd5e1; padding: 4px 8px; } QPushButton#setButton:hover { color: #ffffff; }")
        self.settings_button.clicked.connect(lambda: self.settings_requested.emit(str(tool_dir)))
        self.settings_button.setVisible(True)

        top = QWidget(self); top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addWidget(self.icon_label)
        top_layout.addStretch()
        top_layout.addWidget(self.settings_button)
        top.setStyleSheet("background: transparent; border: none;")

        bottom = QWidget(self); bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.addWidget(self.status)
        bottom_layout.addStretch()
        bottom.setStyleSheet("background: transparent; border: none;")

        layout.addWidget(top)
        layout.addWidget(self.title)
        if description:
            layout.addWidget(self.desc)
        layout.addStretch()
        layout.addWidget(bottom)

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mousePressEvent = self._on_click
        self.installEventFilter(self)

        self._fade_effect = QGraphicsOpacityEffect(self)
        self._fade_effect.setOpacity(0.0)
        self.setGraphicsEffect(self._fade_effect)
        self._reveal_animation = None

    def apply_tool_data(self, tool_name: str, description: str, icon_name: str, accent: str):
        self.tool_name = tool_name
        self.description = description
        self.icon_name = icon_name
        self.accent = accent
        self.hover_accent = _lighten_hex(accent, 0.38)
        self.setStyleSheet(
            f"QFrame#toolCard {{ background: #171E2A; border: 2px solid {accent}; border-radius: 14px; }} "
            f"QFrame#toolCard:hover {{ border: 2px solid {self.hover_accent}; background: #1C2331; }}"
        )
        if hasattr(self, "icon_label"):
            self.icon_label.setPixmap(IconManager().pixmap_for(icon_name, accent, 28))
        if hasattr(self, "title"):
            self.title.setText(tool_name)
        if hasattr(self, "desc"):
            self.desc.setText(description or "")
            self.desc.setVisible(bool(description))

    def animate_in(self, delay_ms: int = 0):
        if self.graphicsEffect() is None:
            self._fade_effect = QGraphicsOpacityEffect(self)
            self.setGraphicsEffect(self._fade_effect)
        self._fade_effect.setOpacity(0.0)
        self.show()
        animation = QPropertyAnimation(self._fade_effect, b"opacity", self)
        animation.setDuration(220)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        if delay_ms > 0:
            QTimer.singleShot(delay_ms, animation.start)
        else:
            animation.start()
        self._reveal_animation = animation

    def eventFilter(self, obj, event):
        if obj is self and event.type() == event.Type.Enter:
            self._hovered = True
            self.move(self.x(), self.y() - 2)
            self.setStyleSheet(
                f"QFrame#toolCard {{ background: #1C2331; border: 2px solid {self.hover_accent}; border-radius: 14px; }}"
            )
        elif obj is self and event.type() == event.Type.Leave:
            self._hovered = False
            self.move(self.x(), self.y() + 2)
            self.setStyleSheet(
                f"QFrame#toolCard {{ background: #171E2A; border: 2px solid {self.accent}; border-radius: 14px; }}"
            )
        return super().eventFilter(obj, event)

    def _on_click(self, event):
        self.launch_requested.emit(str(self.tool_dir))

    def set_status_text(self, text: str, color: str | None = None):
        self.status.setText(text)
        if color:
            self.status.setStyleSheet(f"padding: 4px 8px; border-radius: 8px; background: {color}; color: #0F172A; font-weight: 600;")
        else:
            self.status.setStyleSheet("padding: 4px 8px; border-radius: 8px; background: rgba(255,255,255,0.06); color: #e2e8f0; font-weight: 600; border: none;")

    def set_status_from_state(self, status: str):
        palette = {
            "Idle": ("Idle", "rgba(148, 163, 184, 0.28)"),
            "Running": ("Running", "rgba(34, 197, 94, 0.45)"),
            "Completed": ("Completed", "rgba(96, 165, 250, 0.45)"),
            "Stopped": ("Stopped", "rgba(245, 158, 11, 0.45)"),
            "Failed": ("Failed", "rgba(239, 68, 68, 0.45)"),
            "Launch failed": ("Launch failed", "rgba(239, 68, 68, 0.45)"),
        }
        label, color = palette.get(status, ("Idle", "rgba(148, 163, 184, 0.28)"))
        self.set_status_text(label, color)

    def set_all_idle(self):
        for card in self.card_map.values():
            card.set_status_from_state("Idle")


class ToolLibraryView(QWidget):
    launch_requested = Signal(str)
    settings_requested = Signal(str)

    def __init__(self):
        super().__init__()
        self.session_manager = None
        self.card_map: Dict[str, ToolCard] = {}
        self.all_tools = []
        self.max_columns = 4
        self._search_term = ""
        self._thread: QThread | None = None
        self._worker: ToolDiscoveryWorker | None = None
        self._render_queue: list[str] = []
        self._render_batch_size = 3
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render_next_cards)

        screen = QGuiApplication.primaryScreen()
        scale = screen.devicePixelRatio() if screen is not None and hasattr(screen, "devicePixelRatio") else 1.0
        scale = max(1.0, float(scale))
        self.card_width = max(300, int(round(420 / scale)))
        self.card_height = 180

        self.grid_layout = QGridLayout()
        self.grid_layout.setSpacing(16)
        self.grid_layout.setContentsMargins(12, 12, 12, 12)
        self.grid_layout.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        self.card_container = QWidget(self)
        self.card_container.setLayout(self.grid_layout)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.card_container)
        self.scroll.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.scroll.verticalScrollBar().valueChanged.connect(self._schedule_render)

        self.search_box = QLineEdit(self)
        self.search_box.setPlaceholderText("Search tools")
        self.search_box.textChanged.connect(self.filter_tools)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.search_box)
        layout.addWidget(self.scroll)

    def closeEvent(self, event):
        self.stop_background_discovery()
        super().closeEvent(event)

    def stop_background_discovery(self):
        thread = self._thread
        worker = self._worker
        self._thread = None
        self._worker = None

        if thread is not None:
            try:
                if thread.isRunning():
                    thread.quit()
                    thread.wait(500)
            except RuntimeError:
                pass
            try:
                thread.deleteLater()
            except RuntimeError:
                pass

        if worker is not None:
            try:
                worker.deleteLater()
            except RuntimeError:
                pass

    def start_background_discovery(self, tools_dir: Path):
        if self._thread is not None and self._thread.isRunning():
            return
        self._clear_cards()
        self._thread = QThread(self)
        self._worker = ToolDiscoveryWorker(tools_dir)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_tools_loaded)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    @Slot(list)
    def _on_tools_loaded(self, tools):
        self.all_tools = tools
        self._schedule_render()

    def _clear_cards(self):
        for widget in list(self.card_map.values()):
            self.grid_layout.removeWidget(widget)
            widget.hide()
            widget.setVisible(False)
        self.card_map.clear()
        self._render_queue.clear()

    def _filtered_tools(self):
        if not self._search_term:
            return self.all_tools
        term = self._search_term.lower()
        return [tool for tool in self.all_tools if term in tool["name"].lower()]

    def _calculate_columns(self):
        available_width = self.scroll.viewport().width() if self.scroll.viewport() else max(self.width(), 1)
        available_width = max(1, available_width - 24)
        columns = max(1, min(self.max_columns, available_width // self.card_width))
        return columns

    def _visible_range(self):
        tools = self._filtered_tools()
        if not tools:
            return 0, 0

        columns = self._calculate_columns()
        viewport_h = max(1, self.scroll.viewport().height())
        row_height = self.card_height + self.grid_layout.verticalSpacing()
        visible_rows = max(2, math.ceil(viewport_h / row_height)) + 2
        scroll_row = self.scroll.verticalScrollBar().value() // row_height
        first_row = max(0, scroll_row - 1)
        start = first_row * columns
        end = min(len(tools), start + visible_rows * columns + columns)
        return start, end

    def _tool_key_from_index(self, tools, index):
        if 0 <= index < len(tools):
            return str(tools[index]["path"])
        return None

    def _create_card(self, tool):
        key = str(tool["path"])
        if key in self.card_map:
            return None

        card = ToolCard(
            tool_name=tool["name"],
            description=tool["description"],
            icon_name=tool["icon"],
            accent=tool["color"],
            tool_dir=Path(tool["path"]),
            parent=self.card_container,
        )
        card.setFixedWidth(self.card_width)
        card.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        card.launch_requested.connect(self.launch_requested.emit)
        card.settings_requested.connect(self.settings_requested.emit)
        self.card_map[key] = card
        instance_status = self._status_for_tool_key(key)
        if instance_status:
            card.set_status_from_state(instance_status)
        return card

    def _status_for_tool_key(self, key: str):
        if self.session_manager is None:
            return None
        for instance in self.session_manager.instances.values():
            if str(instance.tool_dir) == key:
                return instance.status
        return None

    def _schedule_render(self):
        if self._render_timer.isActive():
            return
        self._render_timer.start(0)

    def _render_next_cards(self):
        tools = self._filtered_tools()
        if not tools:
            self._clear_cards()
            return

        start_index, end_index = self._visible_range()
        visible_keys = set()
        created = 0

        for idx in range(start_index, end_index):
            if idx >= len(tools):
                break
            key = self._tool_key_from_index(tools, idx)
            if key is None:
                continue
            visible_keys.add(key)
            if key not in self.card_map:
                card = self._create_card(tools[idx])
                if card is not None:
                    card.animate_in(delay_ms=created * 70)
                    self._render_queue.append(key)
                    created += 1
                    if created >= self._render_batch_size:
                        break

        columns = self._calculate_columns()
        for key, card in list(self.card_map.items()):
            if key not in visible_keys:
                if card.parent() is self.card_container:
                    self.grid_layout.removeWidget(card)
                card.hide()
                card.setVisible(False)
                continue

            target_index = next((idx for idx, tool in enumerate(tools) if str(tool["path"]) == key), 0)
            row = target_index // columns
            col = target_index % columns
            if card.parent() is not self.card_container or self.grid_layout.indexOf(card) < 0:
                self.grid_layout.addWidget(card, row, col)
            else:
                self.grid_layout.addWidget(card, row, col)
            card.setVisible(True)
            card.show()
            card.raise_()

        if created >= self._render_batch_size and any(str(tool["path"]) not in self.card_map for tool in tools[start_index:end_index]):
            self._render_timer.start(18)
        elif any(str(tool["path"]) not in self.card_map for tool in tools[start_index:end_index]):
            self._render_timer.start(12)

    def filter_tools(self, term: str):
        self._search_term = term.strip()
        self._schedule_render()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.all_tools:
            self._schedule_render()

    def refresh(self):
        self._schedule_render()

    def refresh_tool_card(self, tool_dir: str | Path, metadata: dict | None = None):
        key = str(Path(tool_dir))
        card = self.card_map.get(key)
        if card is None:
            self._schedule_render()
            return

        if metadata is None:
            metadata = next((tool for tool in self.all_tools if str(tool["path"]) == key), None)
        if metadata is None:
            self._schedule_render()
            return

        if metadata.get("path") is None:
            metadata = {**metadata, "path": key}
        self.all_tools = [
            tool if str(tool.get("path")) != key else {
                **tool,
                "name": metadata.get("name", tool.get("name", "")),
                "description": metadata.get("description", tool.get("description", "")),
                "icon": metadata.get("icon", tool.get("icon", "wrench")),
                "color": metadata.get("color", tool.get("color", "#A78BFA")),
            }
            for tool in self.all_tools
        ]
        card.apply_tool_data(metadata["name"], metadata["description"], metadata["icon"], metadata["color"])
        card.animate_in(0)
