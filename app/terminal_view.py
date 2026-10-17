from __future__ import annotations

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPlainTextEdit, QLineEdit, QLabel, QApplication


class TerminalView(QWidget):
    def __init__(self):
        super().__init__()
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.output.setUndoRedoEnabled(False)

        self.input_line = QLineEdit()
        self.input_line.setPlaceholderText("Send input to stdin...")

        header = QHBoxLayout()
        self.status_label = QLabel("No session")
        self.status_label.setStyleSheet("color: #F8FAFC; background: #111827; border: 1px solid #2D3748; border-radius: 8px; padding: 4px 8px;")
        header.addWidget(self.status_label)
        header.addStretch()

        input_row = QHBoxLayout()
        input_row.addWidget(QLabel("Input:"))
        input_row.addWidget(self.input_line)

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self.output, 1)
        layout.addLayout(input_row)

    def _render_stream(self, text: str, *, clear: bool = False) -> None:
        if clear:
            self.output.clear()
        if not text:
            return

        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.output.setTextCursor(cursor)

        for ch in text:
            if ch == "\r":
                cursor = self.output.textCursor()
                cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
                cursor.movePosition(QTextCursor.MoveOperation.EndOfLine, QTextCursor.MoveMode.KeepAnchor)
                cursor.removeSelectedText()
                self.output.setTextCursor(cursor)
                self.output.update()
                self.output.repaint()
            elif ch == "\n":
                self.output.insertPlainText("\n")
                self.output.update()
                self.output.repaint()
            else:
                self.output.insertPlainText(ch)
                self.output.update()
                self.output.repaint()

        self.output.ensureCursorVisible()
        self.output.update()
        self.output.repaint()

    def append_text(self, text: str) -> None:
        if not text:
            return
        self._render_stream(text)

    def render_stream(self, text: str, *, clear: bool = False) -> None:
        self._render_stream(text, clear=clear)

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)
        if text.lower() == "running":
            self.status_label.setStyleSheet("color: #0F172A; background: #22C55E; border: 1px solid #22C55E; border-radius: 8px; padding: 4px 8px;")
        elif "fail" in text.lower() or "error" in text.lower():
            self.status_label.setStyleSheet("color: #fff; background: #EF4444; border: 1px solid #EF4444; border-radius: 8px; padding: 4px 8px;")
        else:
            self.status_label.setStyleSheet("color: #F8FAFC; background: #111827; border: 1px solid #2D3748; border-radius: 8px; padding: 4px 8px;")

    def clear_output(self) -> None:
        self.output.clear()
