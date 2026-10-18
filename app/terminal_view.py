"""Terminal-style output pane: a read-only QPlainTextEdit driven by TerminalModel."""

from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor, QTextOption
from PySide6.QtWidgets import QPlainTextEdit, QWidget

from . import theme
from .terminal_model import INPUT, STDERR, STDOUT, SYSTEM, TerminalModel

MAX_BLOCKS = 100_000      # oldest lines are dropped beyond this, so a runaway script cannot exhaust memory


def _format(color: str, italic: bool = False) -> QTextCharFormat:
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    f.setFontItalic(italic)
    return f


class TerminalView(QPlainTextEdit):
    """Shows what a script prints.  Updates are incremental: only the tail of the document is edited."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Terminal")
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)               # programmatic edits must not build an undo history
        self.setMaximumBlockCount(MAX_BLOCKS)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.setFont(theme.mono_font(10))
        self.setAccessibleName("Script output")
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse |
                                     Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.setPlaceholderText("Press Run to start the script.\n\n"
                                "Output appears here. Anything you type in the input box below is sent to the "
                                "script's standard input.\n"
                                "The script runs through pipes, not a real console: full-screen / cursor-addressed "
                                "programs (curses, vim-style UIs) are not supported.")
        self._fmt = {STDOUT: _format(theme.TERM_STDOUT), STDERR: _format(theme.TERM_STDERR),
                     INPUT: _format(theme.TERM_INPUT), SYSTEM: _format(theme.TERM_SYSTEM, italic=True)}
        self._model = TerminalModel()
        self._stick = True                           # follow new output while the user is at the bottom
        self._internal_scroll = False
        self.verticalScrollBar().valueChanged.connect(self._on_scrolled)

    # ------------------------------------------------------------------ public
    def feed(self, kind: str, text: str) -> None:
        if not text:
            return
        if kind == "system" and not self._model.line_is_empty:
            text = "\n" + text                       # system messages always start on their own line
        self._model.feed(kind, text)
        self._flush()

    def clear_output(self) -> None:
        """Clear what is displayed. Does not touch the process or its pipes."""
        self._model.reset()
        self.clear()
        self._stick = True

    # ------------------------------------------------------------------ rendering
    def _flush(self) -> None:
        if not self._model.dirty:
            return
        finished, active = self._model.take()
        segs: list[tuple[int, list[str]]] = []

        def add(kind: int, text: str) -> None:
            if segs and segs[-1][0] == kind:
                segs[-1][1].append(text)
            else:
                segs.append((kind, [text]))

        for runs in finished:
            last = STDOUT
            for kind, text in runs:
                add(kind, text)
                last = kind
            add(last, "\n")
        for kind, text in active:
            add(kind, text)

        cur = QTextCursor(self.document())            # a private cursor: the user's selection is untouched
        cur.beginEditBlock()
        cur.movePosition(QTextCursor.MoveOperation.End)
        cur.movePosition(QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor)
        cur.removeSelectedText()                      # the previous active line is rewritten below
        for kind, parts in segs:
            cur.insertText("".join(parts), self._fmt[kind])
        cur.endEditBlock()
        if self._stick:
            self._scroll_to_bottom()

    # ------------------------------------------------------------------ scrolling
    def _on_scrolled(self, value: int) -> None:
        if not self._internal_scroll and self.isVisible():
            sb = self.verticalScrollBar()
            self._stick = value >= sb.maximum() - 2

    def _scroll_to_bottom(self) -> None:
        sb = self.verticalScrollBar()
        self._internal_scroll = True
        try:
            sb.setValue(sb.maximum())
        finally:
            self._internal_scroll = False

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._stick:
            self._scroll_to_bottom()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._stick:
            QTimer.singleShot(0, self._scroll_to_bottom)   # layout of a hidden pane settles after show
