"""Queue drawer: the right-side overlay of the operating cockpit that shows and edits one session's queue.

Notebook-style: every cell is a card with its own content (a code cell shows its code right in the card), its
own predefined-answers area (paperclip), and - once it has run - its own output with, while it is running, an
input line directly below it (what Jupyter does for ``input()``).  New cells are added with the "+ Code" /
"+ Tool" buttons that appear between cells on hover and at the end of the list.

It is a SlideDrawer, so it overlays the cockpit exactly like the active-process drawer (the cockpit underneath is
never resized).  The queue itself lives in the Session (session_queue.py); this module only displays it and
forwards the user's actions.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QKeyEvent, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
                               QToolButton, QVBoxLayout, QWidget)

from . import theme
from .icon_manager import IconManager
from .process_manager import IS_WINDOWS
from .queue_model import (CELL_STATUS_TEXT, LANG_PYTHON, LANG_SHELL, LANGUAGE_NAMES, CellKind, CellStatus, QueueCell,
                          QueueState, parse_answers)
from .session_manager import Session, SessionManager
from .session_queue import SessionQueue
from .slide_drawer import SlideDrawer, drawer_width
from .terminal_view import TerminalView
from .tool_registry import ToolRegistry

CELL_COLORS = {
    CellStatus.PENDING: theme.TEXT_MUTED,
    CellStatus.RUNNING: theme.OK,
    CellStatus.WAITING: theme.WARN,
    CellStatus.COMPLETED: theme.OK,
    CellStatus.FAILED: theme.ERR,
    CellStatus.SKIPPED: theme.TEXT_DIM,
    CellStatus.CANCELLED: theme.WARN,
}
STATE_COLORS = {
    QueueState.EMPTY: theme.TEXT_MUTED,
    QueueState.WAITING: theme.INFO,
    QueueState.EXECUTING: theme.OK,
    QueueState.COMPLETED: theme.OK,
    QueueState.STOPPED: theme.WARN,
    QueueState.NOT_RUN: theme.WARN,
}
ACCENT = theme.INFO
OUTPUT_HEIGHT = 150            # inline output area of a cell
REPLAY_CHARS = 200_000         # an inline view replays at most the tail of a very long output
CODE_MIN_LINES, CODE_MAX_LINES = 3, 12


def rgba(color: str, alpha: float) -> str:
    c = theme.with_alpha(color, alpha)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {c.alpha()})"


def format_elapsed(start: datetime | None, end: datetime | None) -> str:
    if start is None:
        return ""
    secs = max(0, int(((end or datetime.now()) - start).total_seconds()))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class ElidedLabel(QLabel):
    """A label that shows “…” instead of forcing the panel wider."""

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(10)
        self.setFullText(text)

    def setFullText(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self._elide()

    def fullText(self) -> str:
        return self._full

    def _elide(self) -> None:
        super().setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, max(10, self.width())))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()


def flat_button(icons: IconManager, icon: str, tip: str, name: str, color: str = theme.TEXT_MUTED, size: int = 24) -> QToolButton:
    b = QToolButton()
    b.setObjectName("Flat")
    b.setIcon(icons.icon(icon, color, 14))
    b.setIconSize(QSize(14, 14))
    b.setFixedSize(size, size)
    b.setToolTip(tip)
    b.setAccessibleName(name)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


# =============================================================================== small widgets
class Switch(QCheckBox):
    """A clearly visible on/off switch (still a QCheckBox: isChecked / toggled / click behave as usual)."""

    TRACK_W, TRACK_H = 38, 20
    ON_FILL, OFF_BORDER, OFF_FILL = theme.OK, "#7A7F8C", "#2A2D35"      # the "off" outline must stand out from the card

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        fm = QFontMetrics(self.font())
        return QSize(self.TRACK_W + 10 + fm.horizontalAdvance(self.text()) + 4, max(self.TRACK_H + 6, fm.height() + 6))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def hitButton(self, pos) -> bool:
        return self.rect().contains(pos)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self.isChecked()
        y = (self.height() - self.TRACK_H) / 2
        track = QRectF(1, y, self.TRACK_W, self.TRACK_H)
        if on:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(self.ON_FILL))
        else:
            p.setPen(QPen(QColor(self.OFF_BORDER), 1.5))
            p.setBrush(QColor(self.OFF_FILL))
        p.drawRoundedRect(track, self.TRACK_H / 2, self.TRACK_H / 2)
        d = self.TRACK_H - 6
        kx = track.right() - d - 3 if on else track.left() + 3
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#10231A") if on else QColor("#C9CCD6"))
        p.drawEllipse(QRectF(kx, y + 3, d, d))
        if self.hasFocus():
            p.setPen(QPen(QColor(theme.INFO), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(track.adjusted(-2, -2, 2, 2), self.TRACK_H / 2 + 2, self.TRACK_H / 2 + 2)
        p.setPen(QColor(theme.TEXT))
        font = self.font()
        font.setWeight(font.Weight.DemiBold)
        p.setFont(font)
        p.drawText(self.rect().adjusted(self.TRACK_W + 12, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   self.text())


class CodeEdit(QPlainTextEdit):
    """The code of a code cell: monospace, no wrapping, Tab = four spaces, grows with its content."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("CodeEdit")
        self.setFont(theme.mono_font(10))
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabChangesFocus(False)
        self.setAccessibleName("Cell code")
        self.setStyleSheet(f"#CodeEdit {{ background: {theme.TERM_BG}; border: 1px solid {theme.BORDER};"
                           f" border-radius: 8px; padding: 4px 6px; }}"
                           f"#CodeEdit:focus {{ border: 1px solid {ACCENT}; }}")
        self.document().contentsChanged.connect(self.fit_height)
        self.fit_height()

    def fit_height(self) -> None:
        lines = min(CODE_MAX_LINES, max(CODE_MIN_LINES, self.document().blockCount()))
        self.setFixedHeight(lines * QFontMetrics(self.font()).lineSpacing() + 22)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Tab and not self.isReadOnly():
            self.insertPlainText("    ")
            event.accept()
            return
        super().keyPressEvent(event)


class InsertBar(QWidget):
    """The gap between cells (and after the last one): a hairline with "+ Code" and "+ Tool" when it is open."""

    addCode = Signal(int)
    addTool = Signal(int)
    HEIGHT = 30

    def __init__(self, index: int, icons: IconManager, always_open: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self.index = index
        self.always_open = always_open
        self._open = always_open
        self.setFixedHeight(self.HEIGHT)
        self.setAccessibleName("Add a cell here")
        self.code_button = QPushButton(" Code")
        self.tool_button = QPushButton(" Tool")
        for b, icon, tip in ((self.code_button, "plus", "Add a code cell here"), (self.tool_button, "plus", "Add a tool cell here")):
            b.setIcon(icons.icon(icon, theme.TEXT, 13))
            b.setIconSize(QSize(13, 13))
            b.setFixedHeight(24)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(tip)
            b.setStyleSheet(f"QPushButton {{ padding: 0 12px; border-radius: 12px; background: {theme.PANEL};"
                            f" border: 1px solid {theme.BORDER_STRONG}; }}"
                            f"QPushButton:hover {{ background: {theme.CARD_HOVER}; border-color: {ACCENT}; }}")
        self.code_button.setAccessibleName("Add code cell")
        self.tool_button.setAccessibleName("Add tool cell")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addStretch(1)
        lay.addWidget(self.code_button)
        lay.addWidget(self.tool_button)
        lay.addStretch(1)
        self.code_button.clicked.connect(lambda: self.addCode.emit(self.index))
        self.tool_button.clicked.connect(lambda: self.addTool.emit(self.index))
        self.set_open(always_open)

    @property
    def is_open(self) -> bool:
        return self._open

    def set_open(self, value: bool) -> None:
        value = value or self.always_open
        self._open = value
        self.code_button.setVisible(value)
        self.tool_button.setVisible(value)
        self.update()

    def paintEvent(self, _event) -> None:
        if not self._open:
            return
        p = QPainter(self)
        p.setPen(QPen(QColor(theme.BORDER_STRONG), 1, Qt.PenStyle.DashLine))
        y = self.height() // 2
        p.drawLine(4, y, self.width() - 4, y)


# =============================================================================== one cell in the list
class CellCard(QFrame):
    editRequested = Signal(object)
    removeRequested = Signal(object)
    moveRequested = Signal(object, int)
    expandedChanged = Signal(object, bool)
    outputToggled = Signal(object, bool)
    inputSubmitted = Signal(object, str)

    def __init__(self, queue: SessionQueue, cell: QueueCell, icons: IconManager, expanded: bool, output_hidden: bool,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.queue, self.cell, self._icons = queue, cell, icons
        self._prev_status = cell.status
        self._shown = 0                                     # characters currently displayed in the inline output
        self.setObjectName("CellCard")

        # -- header: tile, title + status, paperclip (right) --------------------------------------
        self.tile = QLabel()
        self.tile.setFixedSize(32, 32)
        self.tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title = ElidedLabel()
        self.title.setStyleSheet("font-weight: 600;")
        self.subtitle = ElidedLabel()
        texts = QVBoxLayout()
        texts.setSpacing(1)
        texts.addWidget(self.title)
        texts.addWidget(self.subtitle)
        self.clip = QToolButton()
        self.clip.setObjectName("Flat")
        self.clip.setCheckable(True)
        self.clip.setFixedSize(28, 28)
        self.clip.setIconSize(QSize(16, 16))
        self.clip.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clip.setAccessibleName("Predefined answers")
        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(self.tile)
        top.addLayout(texts, 1)
        top.addWidget(self.clip, 0, Qt.AlignmentFlag.AlignTop)

        # -- code (code cells only) ------------------------------------------------------------
        self.code = CodeEdit()
        self.code.setPlainText(cell.command)
        self.code_note = QLabel()
        self.code_note.setWordWrap(True)
        self.code_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")

        # -- action row ----------------------------------------------------------------------
        self.up = flat_button(icons, "arrow-up", "Move up", "Move cell up")
        self.down = flat_button(icons, "arrow-down", "Move down", "Move cell down")
        self.edit = flat_button(icons, "pencil", "Change the tool / name of this cell", "Edit cell")
        self.remove = flat_button(icons, "trash-2", "Remove this cell", "Remove cell")
        self.language = QComboBox()
        self.language.setAccessibleName("Code language")
        self.language.setToolTip("Python: runs with this tool's interpreter.  Shell: a command or script for "
                                 + ("cmd.exe" if IS_WINDOWS else "/bin/sh") + ".")
        self.language.addItem("Python", LANG_PYTHON)
        self.language.addItem("Shell", LANG_SHELL)
        self.language.setFixedHeight(26)
        self.language.setMinimumWidth(92)
        self.output_toggle = QPushButton(" Output")
        self.output_toggle.setCheckable(True)
        self.output_toggle.setIcon(icons.icon("terminal", theme.TEXT, 14))
        self.output_toggle.setIconSize(QSize(14, 14))
        self.output_toggle.setFixedHeight(24)
        self.output_toggle.setStyleSheet("padding: 0 10px;")
        self.output_toggle.setAccessibleName("Show or hide the cell output")
        self.output_toggle.setToolTip("Show or hide what this cell printed")
        actions = QHBoxLayout()
        actions.setSpacing(2)
        for w in (self.up, self.down, self.edit, self.remove):
            actions.addWidget(w)
        actions.addSpacing(6)
        actions.addWidget(self.language)
        actions.addStretch(1)
        actions.addWidget(self.output_toggle)

        # -- predefined answers (opened with the paperclip), directly below the cell ---------------------
        self.pane = QFrame()
        self.pane.setObjectName("AnswerPane")
        self.pane.setStyleSheet(f"#AnswerPane {{ background: {rgba(ACCENT, 0.10)}; border: 1px solid {rgba(ACCENT, 0.35)};"
                                f" border-radius: 8px; }}")
        pl = QVBoxLayout(self.pane)
        pl.setContentsMargins(10, 8, 10, 8)
        pl.setSpacing(6)
        cap = QLabel("Predefined answers, in order — one is typed each time the cell asks for input")
        cap.setWordWrap(True)
        cap.setStyleSheet(f"color: {ACCENT}; font-size: 12px;")
        self.answers = QLineEdit()
        self.answers.setPlaceholderText("1920, 1080, yes")
        self.answers.setAccessibleName("Predefined answers")
        self.answers.setStyleSheet(f"background: {rgba('#000000', 0.25)};")
        self.answer_hint = QLabel()
        self.answer_hint.setWordWrap(True)
        self.answer_hint.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 12px;")
        pl.addWidget(cap)
        pl.addWidget(self.answers)
        pl.addWidget(self.answer_hint)

        # -- output + input line (below the cell, like a notebook) -----------------------------------
        self.out_box = QWidget()
        ol = QVBoxLayout(self.out_box)
        ol.setContentsMargins(0, 0, 0, 0)
        ol.setSpacing(4)
        self.out_caption = QLabel()
        self.out_caption.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        self.terminal = TerminalView()
        self.terminal.setFixedHeight(OUTPUT_HEIGHT)
        self.terminal.setPlaceholderText("")
        self.terminal.setAccessibleName("Cell output")
        self.input = QLineEdit()
        self.input.setAccessibleName("Cell input")
        ol.addWidget(self.out_caption)
        ol.addWidget(self.terminal)
        ol.addWidget(self.input)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 10, 8)
        lay.setSpacing(6)
        lay.addLayout(top)
        lay.addWidget(self.code)
        lay.addWidget(self.code_note)
        lay.addLayout(actions)
        lay.addWidget(self.pane)
        lay.addWidget(self.out_box)

        self.clip.toggled.connect(self._on_clip)
        self.output_toggle.toggled.connect(self._on_output_toggle)
        self.answers.textEdited.connect(self._on_answers_edited)
        self.code.textChanged.connect(self._on_code_changed)
        self.language.activated.connect(self._on_language)
        self.input.returnPressed.connect(self._on_input)
        self.up.clicked.connect(lambda: self.moveRequested.emit(self.cell, -1))
        self.down.clicked.connect(lambda: self.moveRequested.emit(self.cell, 1))
        self.edit.clicked.connect(lambda: self.editRequested.emit(self.cell))
        self.remove.clicked.connect(lambda: self.removeRequested.emit(self.cell))

        self.answers.setText(cell.answers_text)
        for w, v in ((self.clip, expanded), (self.output_toggle, not output_hidden)):
            w.blockSignals(True)
            w.setChecked(v)
            w.blockSignals(False)
        self.pane.setVisible(expanded)
        self.refresh()
        self.replay()

    # ------------------------------------------------------------------ interaction
    def _on_clip(self, checked: bool) -> None:
        self.pane.setVisible(checked)
        self.expandedChanged.emit(self.cell, checked)
        if checked and self.answers.isEnabled():
            self.answers.setFocus()

    def _on_output_toggle(self, checked: bool) -> None:
        self.outputToggled.emit(self.cell, not checked)
        self._refresh_output_visibility()

    def _on_answers_edited(self, text: str) -> None:
        if not self.queue.update_cell(self.cell.id, answers_text=text):      # the cell has started meanwhile
            self.answers.setText(self.cell.answers_text)
        self._refresh_hint()
        self._refresh_clip()
        self._refresh_status_line()

    def _on_code_changed(self) -> None:
        if self.code.isReadOnly():
            return
        if not self.queue.update_cell(self.cell.id, command=self.code.toPlainText()):
            self.code.blockSignals(True)
            self.code.setPlainText(self.cell.command)
            self.code.blockSignals(False)
        self._refresh_status_line()

    def _on_language(self, _index: int) -> None:
        lang = self.language.currentData()
        if not self.queue.update_cell(self.cell.id, language=lang):
            self._sync_language()
        self._refresh_code_note()

    def _on_input(self) -> None:
        self.inputSubmitted.emit(self.cell, self.input.text())

    # ------------------------------------------------------------------ inline output
    def feed(self, kind: str, text: str) -> None:
        self.terminal.feed(kind, text)
        self._shown += len(text)
        self._refresh_output_visibility()

    def replay(self) -> None:
        """Show the recorded output of the cell (its tail if it is very long)."""
        self.terminal.clear_output()
        self._shown = 0
        total = 0
        parts: list[tuple[str, str]] = []
        for kind, text in reversed(self.cell.output):
            parts.append((kind, text))
            total += len(text)
            if total > REPLAY_CHARS:
                break
        if total > REPLAY_CHARS or self.cell.output_truncated:
            self.terminal.feed("system", "(Only the end of a very long output is shown here.)\n")
        for kind, text in reversed(parts):
            self.feed(kind, text)
        self._refresh_output_visibility()

    def _has_output(self) -> bool:
        return bool(self.cell.output)

    def _refresh_output_visibility(self) -> None:
        has = self._has_output()
        self.output_toggle.setVisible(has)
        show = has and self.output_toggle.isChecked()
        self.out_box.setVisible(show)
        if not show:
            return
        c = self.cell
        self.out_caption.setText("Output" if not c.is_pending else "Output of the last run")
        live = c.is_active and not self.queue.stopping
        self.input.setVisible(live)
        waiting = c.status is CellStatus.WAITING
        self.input.setPlaceholderText("The cell is waiting for input — type it here and press Enter" if waiting
                                      else "Type input for this cell and press Enter")
        self.input.setStyleSheet(f"border: 1px solid {theme.WARN};" if waiting else "")

    # ------------------------------------------------------------------ display
    @property
    def is_expanded(self) -> bool:
        return self.clip.isChecked()

    def _tile(self) -> None:
        cell = self.cell
        tool = self.queue.tool_of(cell)
        if cell.kind is CellKind.TOOL:
            icon, color = (tool.meta.icon, tool.meta.color) if tool is not None else ("circle-x", theme.ERR)
        else:
            icon, color = ("terminal" if cell.language == LANG_SHELL else "code"), ACCENT
        self.tile.setPixmap(self._icons.pixmap(icon, color, 20))
        self.tile.setStyleSheet(f"background: {rgba(color, 0.15)}; border-radius: 8px;")
        self.tile.setToolTip("Tool cell: launches an existing tool" if cell.kind is CellKind.TOOL
                             else f"Code cell ({LANGUAGE_NAMES.get(cell.language, 'Python')})")

    def status_line(self) -> str:
        c = self.cell
        s = c.status
        text = CELL_STATUS_TEXT[s]
        if s in (CellStatus.RUNNING, CellStatus.WAITING):
            return f"{text} · {format_elapsed(c.started_at, None)}"
        if s is CellStatus.PENDING:
            n = len(c.answers)
            now = ("Pending · empty — nothing to run" if c.kind is CellKind.CODE and not c.command.strip()
                   else f"Pending · {n} predefined answer{'s' if n != 1 else ''}" if n else "Pending")
            if c.last_status is None:
                return now
            return f"{now} · last run: {self._outcome(c.last_status)}"
        return self._outcome(s)

    def _outcome(self, s: CellStatus) -> str:
        c = self.cell
        text = CELL_STATUS_TEXT[s]
        if s is CellStatus.COMPLETED:
            extra = f" in {format_elapsed(c.started_at, c.finished_at)}" if c.started_at and c.finished_at else ""
            note = f" ({len(c.unused_answers)} answer{'s' if len(c.unused_answers) != 1 else ''} unused)" if c.unused_answers else ""
            return f"{text}{extra}{note}"
        if s in (CellStatus.FAILED, CellStatus.SKIPPED, CellStatus.CANCELLED):
            return f"{text} — {c.reason}" if c.reason else text
        return text

    def _refresh_clip(self) -> None:
        n = len(self.cell.answers)
        color = ACCENT if n else theme.TEXT_MUTED
        self.clip.setIcon(self._icons.icon("paperclip", color, 16))
        self.clip.setToolTip(f"Predefined answers ({n}) — click to show or hide" if n
                             else "Attach predefined answers (optional) — click to show or hide")

    def _refresh_hint(self) -> None:
        c = self.cell
        if c.is_pending:
            answers = parse_answers(self.answers.text())
            self.answer_hint.setText(f"{len(answers)} answer{'s' if len(answers) != 1 else ''}: " + " · ".join(answers)
                                     if answers else "No answers — the cell asks you (or waits) whenever it needs input.")
        else:
            answers = c.answers
            self.answer_hint.setText(f"{len(c.answers_sent)} of {len(answers)} sent" if answers
                                     else "No predefined answers were configured.")

    def _refresh_status_line(self) -> None:
        self.subtitle.setFullText(self.status_line())

    def _sync_language(self) -> None:
        self.language.blockSignals(True)
        self.language.setCurrentIndex(max(0, self.language.findData(self.cell.language)))
        self.language.blockSignals(False)

    def _refresh_code_note(self) -> None:
        c = self.cell
        if c.language == LANG_SHELL:
            where = "cmd.exe (as a batch script)" if IS_WINDOWS else "/bin/sh"
            self.code_note.setText(f"Shell command or script, run with {where} in the tool folder.")
            self.code.setPlaceholderText("shutdown /s /t 60" if IS_WINDOWS else "echo done")
        else:
            self.code_note.setText("Python code, run with this tool's interpreter (its .venv, else the global Python) "
                                   "in the tool folder.")
            self.code.setPlaceholderText("# Python code — e.g.\nimport time\ntime.sleep(5)\nprint('done')")

    def refresh(self) -> None:
        c = self.cell
        is_code = c.kind is CellKind.CODE
        self._tile()
        tool_name = self.queue.tool_name(c)
        title = c.summary(tool_name)
        if c.kind is CellKind.TOOL and not c.label.strip() and tool_name is None:
            title = f"{c.tool_id} (tool not available)"
        self.title.setFullText(title)
        self._refresh_status_line()
        color = CELL_COLORS[c.result]
        self.subtitle.setStyleSheet(f"color: {color}; font-size: 12px;")
        self.setStyleSheet(f"#CellCard {{ background: {theme.CARD}; border: 1px solid {theme.BORDER};"
                           f" border-left: 3px solid {color}; border-radius: 10px; }}")
        pending = c.is_pending
        # code (code cells)
        self.code.setVisible(is_code)
        self.code_note.setVisible(is_code)
        self.language.setVisible(is_code)
        self.code.setReadOnly(not pending)
        if is_code:
            if self.code.toPlainText() != c.command and not self.code.hasFocus():
                self.code.blockSignals(True)
                self.code.setPlainText(c.command)
                self.code.blockSignals(False)
            self._sync_language()
            self._refresh_code_note()
        # actions
        self.up.setVisible(pending)
        self.down.setVisible(pending)
        self.edit.setVisible(pending and not is_code)
        self.up.setEnabled(self.queue.can_move(c, -1))
        self.down.setEnabled(self.queue.can_move(c, 1))
        self.remove.setVisible(not c.is_active)
        self.language.setEnabled(pending)
        self.answers.setEnabled(pending)
        if self.answers.text() != c.answers_text and not self.answers.hasFocus():
            self.answers.setText(c.answers_text)
        # a new run starts (or the output was reset): show exactly what the cell has recorded
        if c.output_chars < self._shown or (not c.output and self._shown):
            self.replay()
        self._refresh_output_visibility()
        self._refresh_clip()
        self._refresh_hint()
        self.setAccessibleName(f"{title}, {CELL_STATUS_TEXT[c.result]}")
        self._prev_status = c.status


# =============================================================================== tool picker
class ToolCellDialog(QDialog):
    """Choose the tool of a tool cell (and, optionally, a name). Answers are edited on the cell itself."""

    def __init__(self, registry: ToolRegistry, icons: IconManager, cell: QueueCell | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._registry = registry
        editing = cell is not None
        self.setWindowTitle("Edit tool cell" if editing else "Add tool cell")
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(8)

        self.tool_combo = QComboBox()
        self.tool_info = QLabel()
        for t in sorted(registry.tools, key=lambda t: t.meta.name.casefold()):
            self.tool_combo.addItem(icons.icon(t.meta.icon, t.meta.color, 16), t.meta.name, t.id)
        if cell and cell.tool_id and self.tool_combo.findData(cell.tool_id) < 0:
            self.tool_combo.addItem(f"{cell.tool_id} (not available)", cell.tool_id)
        if cell and cell.tool_id:
            self.tool_combo.setCurrentIndex(max(0, self.tool_combo.findData(cell.tool_id)))
        self.tool_combo.setAccessibleName("Tool")
        self.tool_info.setWordWrap(True)
        self.tool_info.setObjectName("Muted")
        hint = QLabel("Launched exactly like the Run button does: the tool's own .venv interpreter if it has one, "
                      "otherwise the global Python.")
        hint.setWordWrap(True)
        hint.setObjectName("Muted")
        self.name = QLineEdit(cell.label if cell else "")
        self.name.setPlaceholderText("Optional — shown instead of the tool's name")
        self.name.setAccessibleName("Cell name")
        lay.addWidget(QLabel("Tool"))
        lay.addWidget(self.tool_combo)
        lay.addWidget(self.tool_info)
        lay.addWidget(hint)
        lay.addSpacing(4)
        lay.addWidget(QLabel("Name (optional)"))
        lay.addWidget(self.name)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setText("Save" if editing else "Add")
        self.ok.setObjectName("Primary")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addSpacing(6)
        lay.addWidget(self.buttons)
        self.tool_combo.currentIndexChanged.connect(self._tool_changed)
        self._tool_changed()

    def _tool_changed(self) -> None:
        tool = self._registry.get(self.tool_combo.currentData() or "")
        self.tool_info.setText(tool.meta.description.strip() if tool is not None and tool.meta.description.strip()
                               else ("No description." if tool is not None else "This tool is not available."))
        self.ok.setEnabled(tool is not None)

    def values(self) -> dict:
        return {"label": self.name.text().strip(), "tool_id": self.tool_combo.currentData() or ""}


# =============================================================================== the drawer
class QueueDrawer(SlideDrawer):
    def __init__(self, manager: SessionManager, registry: ToolRegistry, icons: IconManager, parent: QWidget):
        super().__init__(parent)
        self._manager, self._registry, self._icons = manager, registry, icons
        self.session: Session | None = None
        self.queue: SessionQueue | None = None
        self._expanded: dict[str, set[str]] = {}          # session id -> cells whose paperclip area is open
        self._out_hidden: dict[str, set[str]] = {}        # session id -> cells whose output the user collapsed
        self._cards: dict[str, CellCard] = {}
        self._bars: list[InsertBar] = []
        self._focus_new: str | None = None
        self._hover_card: int | None = None
        self._hover_bar: int | None = None
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(140)
        self._hover_timer.timeout.connect(self._hover_cleared)

        pl = QVBoxLayout(self.panel)
        pl.setContentsMargins(16, 14, 12, 14)
        pl.setSpacing(10)
        self._build(pl)

        self._clock = QTimer(self)                         # elapsed time of the running cell, only while visible
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._tick_clock)
        manager.sessionRemoved.connect(self._on_session_removed)

    # ------------------------------------------------------------------ geometry
    def panel_width(self, window_width: int) -> int:
        return drawer_width(window_width, 380, 600, 2)

    # ------------------------------------------------------------------ layout
    def _build(self, lay: QVBoxLayout) -> None:
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title = QLabel("Queue")
        self.title.setObjectName("Title")
        self.subtitle = ElidedLabel()
        self.subtitle.setObjectName("Muted")
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        self.close_button = QToolButton()
        self.close_button.setObjectName("Flat")
        self.close_button.setIcon(self._icons.icon("x", theme.TEXT, 18))
        self.close_button.setToolTip("Close")
        self.close_button.setAccessibleName("Close panel")
        self.close_button.clicked.connect(self.close_drawer)
        head.addLayout(titles, 1)
        head.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(head)

        # overall state + Stop
        self.banner = QFrame()
        self.banner.setObjectName("Banner")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(12, 8, 8, 8)
        bl.setSpacing(8)
        self.state_label = QLabel()
        self.state_label.setWordWrap(True)
        self.state_label.setAccessibleName("Queue state")
        self.stop_button = QPushButton(" Stop queue")
        self.stop_button.setIcon(self._icons.icon("square", theme.TEXT, 14))
        self.stop_button.setFixedHeight(26)
        self.stop_button.setStyleSheet("padding: 0 10px;")
        self.stop_button.clicked.connect(self._stop)
        bl.addWidget(self.state_label, 1)
        bl.addWidget(self.stop_button)
        lay.addWidget(self.banner)

        # Must Execute
        box = QFrame()
        box.setObjectName("MustBox")
        box.setStyleSheet(f"#MustBox {{ background: {theme.CARD}; border: 1px solid {theme.BORDER_STRONG}; border-radius: 10px; }}")
        ml = QVBoxLayout(box)
        ml.setContentsMargins(12, 8, 12, 8)
        ml.setSpacing(4)
        row = QHBoxLayout()
        self.must = Switch("Must Execute")
        self.must.setAccessibleName("Must Execute")
        self.must.setToolTip(
            "Off: the queue runs only if the main process succeeds and stops at the first failed cell.\n"
            "On: the queue runs even if the main process fails, a failed cell never blocks the later ones, and a "
            "Python program that is blocked waiting for input is stopped after 5 minutes (other programs are never "
            "timed out: their input waits cannot be detected reliably).\n"
            "Best effort: it only works while the Python Toolbox application keeps running.")
        self.must.toggled.connect(self._must_toggled)
        self.must_state = QLabel()
        self.must_state.setStyleSheet("font-weight: 700; font-size: 12px;")
        row.addWidget(self.must)
        row.addStretch(1)
        row.addWidget(self.must_state)
        self.must_note = QLabel()
        self.must_note.setWordWrap(True)
        self.must_note.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 12px;")
        ml.addLayout(row)
        ml.addWidget(self.must_note)
        lay.addWidget(box)

        # the cells: [bar] card [bar] card ... [always-open bar]
        self._list = QVBoxLayout()
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(0)
        holder = QWidget()
        holder.setObjectName("Page")
        holder.setStyleSheet("#Page { background: transparent; }")
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 4, 0)
        self.empty = QLabel("Add code cells and tool cells below.\nThey run one after another as soon as the main "
                            "process has finished.")
        self.empty.setObjectName("Muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        hl.addWidget(self.empty)
        hl.addLayout(self._list)
        hl.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(holder)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        lay.addWidget(self.scroll, 1)

    # ------------------------------------------------------------------ binding to a session
    def open_for(self, session: Session) -> None:
        self.bind(session)
        self.open_drawer()

    def bind(self, session: Session | None) -> None:
        if self.queue is not None:
            for sig, slot in ((self.queue.structureChanged, self.rebuild), (self.queue.stateChanged, self._on_state),
                              (self.queue.cellChanged, self._on_cell), (self.queue.cellOutput, self._on_cell_output)):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self.session = session
        self.queue = session.queue if session is not None else None
        if self.queue is not None:
            self.queue.structureChanged.connect(self.rebuild)
            self.queue.stateChanged.connect(self._on_state)
            self.queue.cellChanged.connect(self._on_cell)
            self.queue.cellOutput.connect(self._on_cell_output)
        self.rebuild()

    def _on_session_removed(self, session: Session) -> None:
        if session is self.session:
            self.close_now()
            self.bind(None)

    # ------------------------------------------------------------------ SlideDrawer hooks
    def prepare_open(self) -> None:
        self.rebuild()
        self._clock.start()

    def focus_on_open(self) -> None:
        self.close_button.setFocus()

    def on_closed(self) -> None:
        self._clock.stop()

    # ------------------------------------------------------------------ rendering
    def rebuild(self, *_args) -> None:
        """Re-create the cards and insert bars (structure changed). Typed text lives in the cells, nothing is lost."""
        bar = self.scroll.verticalScrollBar()
        keep = bar.value()
        while self._list.count():
            item = self._list.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()                           # gone from the screen at once; the object is deleted later
                w.setParent(None)
                w.deleteLater()
        self._cards.clear()
        self._bars = []
        self._hover_card = self._hover_bar = None
        q = self.queue
        if q is None or self.session is None:
            self.title.setText("Queue")
            self.subtitle.setFullText("")
            self.empty.show()
            return
        opened = self._expanded.setdefault(self.session.id, set())
        hidden = self._out_hidden.setdefault(self.session.id, set())
        for i, cell in enumerate(q.cells):
            self._add_bar(i)
            card = CellCard(q, cell, self._icons, cell.id in opened, cell.id in hidden)
            card.editRequested.connect(self.edit_cell)
            card.removeRequested.connect(self.remove_cell)
            card.moveRequested.connect(lambda c, d: q.move_cell(c.id, d))
            card.expandedChanged.connect(self._remember_expanded)
            card.outputToggled.connect(self._remember_output_hidden)
            card.inputSubmitted.connect(self._send_input)
            card.installEventFilter(self)
            self._cards[cell.id] = card
            self._list.addWidget(card)
        self._add_bar(len(q.cells), always_open=True)
        self.empty.setVisible(not q.cells)
        QTimer.singleShot(0, lambda: bar.setValue(keep))
        self._refresh_header()
        if self._focus_new in self._cards:
            card = self._cards[self._focus_new]
            if card.code.isVisibleTo(card):
                card.code.setFocus()
                QTimer.singleShot(0, lambda c=card: self.scroll.ensureWidgetVisible(c))
        self._focus_new = None

    def _add_bar(self, index: int, always_open: bool = False) -> None:
        b = InsertBar(index, self._icons, always_open)
        b.addCode.connect(self.insert_code)
        b.addTool.connect(self.insert_tool)
        b.installEventFilter(self)
        self._bars.append(b)
        self._list.addWidget(b)

    def _remember_expanded(self, cell: QueueCell, expanded: bool) -> None:
        if self.session is not None:
            s = self._expanded.setdefault(self.session.id, set())
            (s.add if expanded else s.discard)(cell.id)

    def _remember_output_hidden(self, cell: QueueCell, hidden: bool) -> None:
        if self.session is not None:
            s = self._out_hidden.setdefault(self.session.id, set())
            (s.add if hidden else s.discard)(cell.id)

    def _refresh_header(self) -> None:
        q, session = self.queue, self.session
        if q is None or session is None:
            return
        self.title.setText("Queue")
        self.subtitle.setFullText(f"for {self._manager.label(session)}")
        color = STATE_COLORS[q.state]
        self.state_label.setText(f"<span style='color:{color}'>●</span>&nbsp; {q.state_text}")
        self.banner.setStyleSheet(f"#Banner {{ background: {rgba(color, 0.10)}; border: 1px solid {rgba(color, 0.35)};"
                                  f" border-radius: 10px; }}")
        self.stop_button.setVisible(q.executing)
        self.stop_button.setText(" Force stop" if q.stopping else " Stop queue")
        self.stop_button.setToolTip("Stop the running cell and cancel the cells that have not started"
                                    if not q.stopping else "The cell did not exit yet: terminate it now")
        self.must.blockSignals(True)
        self.must.setChecked(q.must_execute)
        self.must.blockSignals(False)
        self.must.update()
        on = q.must_execute
        self.must_state.setText("ON" if on else "OFF")
        self.must_state.setStyleSheet(f"font-weight: 700; font-size: 12px; color: {theme.OK if on else theme.TEXT_MUTED};")
        self.must_note.setText("Runs even if the main process fails, continues after a failed cell, and stops a Python cell "
                               "that waits for input for 5 minutes." if on else
                               "Runs only if the main process succeeds, and stops at the first failed cell.")

    def _on_state(self) -> None:
        self._refresh_header()
        for card in self._cards.values():                       # e.g. the input line follows "stopping"
            card._refresh_output_visibility()

    def _on_cell(self, cell: QueueCell) -> None:
        card = self._cards.get(cell.id)
        if card is None:
            return
        before = card._prev_status
        card.refresh()
        if (cell.status is CellStatus.WAITING and before is not CellStatus.WAITING and self.isVisible()
                and not isinstance(QApplication.focusWidget(), (QLineEdit, QPlainTextEdit))):
            card.input.setFocus()                                # like a notebook: the input is ready for typing
            QTimer.singleShot(0, lambda c=card: self.scroll.ensureWidgetVisible(c, 0, 20))

    def _tick_clock(self) -> None:
        q = self.queue
        if q is None:
            return
        for cell in q.cells:
            if cell.is_active and cell.id in self._cards:
                self._cards[cell.id]._refresh_status_line()

    def _on_cell_output(self, cell: QueueCell, kind: str, text: str) -> None:
        card = self._cards.get(cell.id)
        if card is not None:
            card.feed(kind, text)

    # ------------------------------------------------------------------ hover: the "+ Code / + Tool" bars
    def eventFilter(self, obj, event) -> bool:
        t = event.type()
        if t == QEvent.Type.Enter:
            if isinstance(obj, CellCard):
                self._hover_timer.stop()
                self.hover_card(self.queue.cells.index(obj.cell) if self.queue and obj.cell in self.queue.cells else None)
            elif isinstance(obj, InsertBar):
                self._hover_timer.stop()
                self.hover_bar(obj.index)
        elif t == QEvent.Type.Leave and isinstance(obj, (CellCard, InsertBar)):
            self._hover_timer.start()
        return False

    def hover_card(self, index: int | None) -> None:
        """The pointer is over cell ``index``: its bar below opens (and the one above the first cell)."""
        self._hover_card, self._hover_bar = index, None
        self._apply_hover()

    def hover_bar(self, index: int | None) -> None:
        self._hover_card, self._hover_bar = None, index
        self._apply_hover()

    def _hover_cleared(self) -> None:
        self._hover_card = self._hover_bar = None
        self._apply_hover()

    def _apply_hover(self) -> None:
        wanted: set[int] = set()
        if self._hover_card is not None:
            wanted.add(self._hover_card + 1)
            if self._hover_card == 0:
                wanted.add(0)
        if self._hover_bar is not None:
            wanted.add(self._hover_bar)
        for b in self._bars:
            b.set_open(b.index in wanted)

    # ------------------------------------------------------------------ actions
    def _must_toggled(self, checked: bool) -> None:
        if self.queue is not None:
            self.queue.set_must_execute(checked)

    def _stop(self) -> None:
        if self.queue is not None:
            self.queue.stop()

    def insert_code(self, index: int) -> None:
        """"+ Code": a new empty Python cell at ``index``; its editor gets the keyboard focus."""
        if self.queue is None:
            return
        cell = self.queue.add_cell(CellKind.CODE, language=LANG_PYTHON, index=index)
        self._focus_new = cell.id
        if cell.id in self._cards:                                   # (add_cell already rebuilt the list)
            self._focus_new = None
            self._cards[cell.id].code.setFocus()
            self.scroll.ensureWidgetVisible(self._cards[cell.id])

    def insert_tool(self, index: int) -> None:
        """"+ Tool": choose the tool, then insert the cell at ``index``."""
        if self.queue is None or self.session is None:
            return
        dlg = ToolCellDialog(self._registry, self._icons, None, self)
        if self._run_dialog(dlg):
            self.queue.add_cell(CellKind.TOOL, index=index, **dlg.values())

    def edit_cell(self, cell: QueueCell) -> None:
        if self.queue is None or self.session is None or not cell.is_pending or cell.kind is not CellKind.TOOL:
            return
        dlg = ToolCellDialog(self._registry, self._icons, cell, self)
        if self._run_dialog(dlg):
            if not self.queue.update_cell(cell.id, **dlg.values()):
                QMessageBox.information(self, "Cell already started",
                                        "This cell has already started, so it can no longer be changed.")

    def _run_dialog(self, dlg: QDialog) -> bool:
        try:
            return dlg.exec() == QDialog.DialogCode.Accepted
        finally:
            dlg.deleteLater()

    def remove_cell(self, cell: QueueCell) -> None:
        if self.queue is not None:
            self.queue.remove_cell(cell.id)

    def _send_input(self, cell: QueueCell, text: str) -> None:
        card = self._cards.get(cell.id)
        if self.queue is not None and self.queue.send_input(cell, text) and card is not None:
            card.input.clear()
