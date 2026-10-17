from __future__ import annotations

from PySide6.QtCore import Qt, QPropertyAnimation, QEasingCurve, QPoint
from PySide6.QtGui import QPainter, QColor
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QFrame


class ActiveProcessDrawer(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setParent(parent)
        self.setWindowFlags(Qt.WindowType.Widget)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(260)
        self.setMaximumWidth(420)
        self.setContentsMargins(0, 0, 0, 0)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hide()

        self._panel = QFrame(self)
        self._panel.setObjectName("drawerPanel")
        self._panel.setStyleSheet("QFrame#drawerPanel { background: #171E2A; border: 1px solid #2D3748; border-radius: 14px; }")

        layout = QVBoxLayout(self._panel)
        title = QLabel("Active Processes")
        title.setStyleSheet("font-size: 16px; font-weight: 600;")
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.hide_drawer)

        self.list_widget = QListWidget()
        self.list_widget.setAlternatingRowColors(True)
        self.list_widget.setStyleSheet("background: transparent; border: none;")

        layout.addWidget(title)
        layout.addWidget(close_btn)
        layout.addWidget(self.list_widget, 1)

        self._animation = QPropertyAnimation(self, b"pos")
        self._animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._animation.setDuration(220)

    def set_geometry_for_window(self, window_rect):
        self.resize(int(window_rect.width() * 0.33), max(120, window_rect.height() - 20))
        self.setGeometry(window_rect.right() - self.width() - 12, 12, self.width(), self.height())
        self._panel.resize(self.size())

    def show_drawer(self):
        if self.parent() is not None:
            self.set_geometry_for_window(self.parent().geometry())
        self.show()
        self.raise_()

    def hide_drawer(self):
        self.hide()

    def add_running_item(self, instance_id: str, label: str):
        item = QListWidgetItem(label)
        item.setData(Qt.ItemDataRole.UserRole, instance_id)
        self.list_widget.addItem(item)

    def clear(self):
        self.list_widget.clear()
