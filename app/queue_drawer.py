"""Queue drawer: the right-side overlay of the operating cockpit that shows and edits one session's queue.

It is a SlideDrawer, so it overlays the cockpit exactly like the active-process drawer (the cockpit underneath is
never resized).  Two pages inside the panel: the list of cells, and the output of one cell (terminal + the
normal interactive input line).  The queue itself lives in the Session (session_queue.py); this module only
displays it and forwards the user's actions.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
                               QStackedWidget, QToolButton, QVBoxLayout, QWidget)

from . import theme
from .icon_manager import IconManager
from .process_manager import IS_WINDOWS
from .queue_model import CELL_STATUS_TEXT, CellKind, CellStatus, QueueCell, QueueState, parse_answers
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


# =============================================================================== one cell in the list
class CellCard(QFrame):
    editRequested = Signal(object)
    removeRequested = Signal(object)
    moveRequested = Signal(object, int)
    outputRequested = Signal(object)
    expandedChanged = Signal(object, bool)

    def __init__(self, queue: SessionQueue, cell: QueueCell, icons: IconManager, expanded: bool,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.queue, self.cell, self._icons = queue, cell, icons
        self.setObjectName("CellCard")

        # -- top row: tile, title + status, paperclip (right) -------------------------------
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

        # -- action row ----------------------------------------------------------------------
        self.up = flat_button(icons, "arrow-up", "Move up", "Move cell up")
        self.down = flat_button(icons, "arrow-down", "Move down", "Move cell down")
        self.edit = flat_button(icons, "pencil", "Edit this cell", "Edit cell")
        self.remove = flat_button(icons, "trash-2", "Remove this cell", "Remove cell")
        self.output = QPushButton(" Output")
        self.output.setIcon(icons.icon("terminal", theme.TEXT, 14))
        self.output.setIconSize(QSize(14, 14))
        self.output.setFixedHeight(24)
        self.output.setStyleSheet("padding: 0 10px;")
        self.output.setAccessibleName("Show cell output")
        self.output.setToolTip("Show what this cell printed")
        actions = QHBoxLayout()
        actions.setSpacing(2)
        for b in (self.up, self.down, self.edit, self.remove):
            actions.addWidget(b)
        actions.addStretch(1)
        actions.addWidget(self.output)

        # -- predefined answers (opened with the paperclip) -------------------------------------
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

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 10, 8)
        lay.setSpacing(6)
        lay.addLayout(top)
        lay.addLayout(actions)
        lay.addWidget(self.pane)

        self.clip.toggled.connect(self._on_clip)
        self.answers.textEdited.connect(self._on_answers_edited)
        self.up.clicked.connect(lambda: self.moveRequested.emit(self.cell, -1))
        self.down.clicked.connect(lambda: self.moveRequested.emit(self.cell, 1))
        self.edit.clicked.connect(lambda: self.editRequested.emit(self.cell))
        self.remove.clicked.connect(lambda: self.removeRequested.emit(self.cell))
        self.output.clicked.connect(lambda: self.outputRequested.emit(self.cell))

        self.answers.setText(cell.answers_text)
        self.clip.blockSignals(True)
        self.clip.setChecked(expanded)
        self.clip.blockSignals(False)
        self.pane.setVisible(expanded)
        self.refresh()

    # ------------------------------------------------------------------ interaction
    def _on_clip(self, checked: bool) -> None:
        self.pane.setVisible(checked)
        self.expandedChanged.emit(self.cell, checked)
        if checked and self.answers.isEnabled():
            self.answers.setFocus()

    def _on_answers_edited(self, text: str) -> None:
        if not self.queue.update_cell(self.cell.id, answers_text=text):      # the cell has started meanwhile
            self.answers.setText(self.cell.answers_text)
        self._refresh_hint()
        self._refresh_clip()

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
            icon, color = "terminal", ACCENT
        self.tile.setPixmap(self._icons.pixmap(icon, color, 20))
        self.tile.setStyleSheet(f"background: {rgba(color, 0.15)}; border-radius: 8px;")
        self.tile.setToolTip("Tool cell: launches an existing tool" if cell.kind is CellKind.TOOL
                             else "Code cell: runs a command or script")

    def status_line(self) -> str:
        c = self.cell
        s = c.status
        text = CELL_STATUS_TEXT[s]
        if s in (CellStatus.RUNNING, CellStatus.WAITING):
            return f"{text} · {format_elapsed(c.started_at, None)}"
        if s is CellStatus.COMPLETED:
            extra = f" · {format_elapsed(c.started_at, c.finished_at)}" if c.started_at else ""
            note = f" · {len(c.unused_answers)} answer{'s' if len(c.unused_answers) != 1 else ''} unused" if c.unused_answers else ""
            return f"{text}{extra}{note}"
        if s is CellStatus.FAILED:
            return f"{text} — {c.reason}" if c.reason else text
        if s in (CellStatus.SKIPPED, CellStatus.CANCELLED):
            return f"{text} — {c.reason}" if c.reason else text
        n = len(c.answers)
        return f"{text} · {n} predefined answer{'s' if n != 1 else ''}" if n else text

    def _refresh_clip(self) -> None:
        n = len(self.cell.answers)
        color = ACCENT if n else theme.TEXT_MUTED
        self.clip.setIcon(self._icons.icon("paperclip", color, 16))
        self.clip.setToolTip(f"Predefined answers ({n}) — click to show or hide" if n
                             else "Attach predefined answers (optional) — click to show or hide")

    def _refresh_hint(self) -> None:
        c = self.cell
        answers = parse_answers(self.answers.text()) if c.is_pending else c.answers
        if c.is_pending:
            self.answer_hint.setText(f"{len(answers)} answer{'s' if len(answers) != 1 else ''}: " + " · ".join(answers)
                                     if answers else "No answers — the cell asks you (or waits) whenever it needs input.")
        else:
            sent = len(c.answers_sent)
            unused = f" · unused: {', '.join(c.unused_answers)}" if c.unused_answers and c.is_final else ""
            self.answer_hint.setText(f"{sent} of {len(answers)} sent{unused}" if answers
                                     else "No predefined answers were configured.")

    def refresh(self) -> None:
        c = self.cell
        self._tile()
        tool_name = self.queue.tool_name(c)
        title = c.summary(tool_name)
        if c.kind is CellKind.TOOL and not c.label.strip() and tool_name is None:
            title = f"{c.tool_id} (tool not available)"
        self.title.setFullText(title)
        self.subtitle.setFullText(self.status_line())
        color = CELL_COLORS[c.status]
        self.subtitle.setStyleSheet(f"color: {color}; font-size: 12px;")
        self.setStyleSheet(f"#CellCard {{ background: {theme.CARD}; border: 1px solid {theme.BORDER};"
                           f" border-left: 3px solid {color}; border-radius: 10px; }}")
        pending = c.is_pending
        self.up.setVisible(pending)
        self.down.setVisible(pending)
        self.edit.setVisible(pending)
        self.up.setEnabled(self.queue.can_move(c, -1))
        self.down.setEnabled(self.queue.can_move(c, 1))
        self.remove.setVisible(not c.is_active)
        self.output.setVisible(not pending)
        self.answers.setEnabled(pending)
        if self.answers.text() != c.answers_text and not self.answers.hasFocus():
            self.answers.setText(c.answers_text)
        self._refresh_clip()
        self._refresh_hint()
        self.setAccessibleName(f"{title}, {CELL_STATUS_TEXT[c.status]}")


# =============================================================================== add / edit dialog
class CellEditorDialog(QDialog):
    """Configure a code cell or a tool cell (the predefined answers are edited on the cell itself)."""

    def __init__(self, kind: CellKind, registry: ToolRegistry, icons: IconManager, workdir: Path,
                 cell: QueueCell | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.kind = kind
        self._registry = registry
        editing = cell is not None
        self.setWindowTitle(("Edit " if editing else "Add ") + ("code cell" if kind is CellKind.CODE else "tool cell"))
        self.setMinimumWidth(460)

        self.name = QLineEdit(cell.label if cell else "")
        self.name.setPlaceholderText("Optional — shown in the list instead of the command / tool name")
        self.name.setAccessibleName("Cell name")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(8)
        lay.addWidget(QLabel("Name (optional)"))
        lay.addWidget(self.name)

        self.command = QPlainTextEdit()
        self.tool_combo = QComboBox()
        self.tool_info = QLabel()
        if kind is CellKind.CODE:
            self.command.setPlainText(cell.command if cell else "")
            self.command.setFont(theme.mono_font(10))
            self.command.setMinimumHeight(130)
            self.command.setAccessibleName("Command or script")
            self.command.setPlaceholderText("python process_renders.py --all\n" if not IS_WINDOWS
                                            else "python process_renders.py --all\r\ndel /q temp\\*.*")
            where = ("Windows Command Prompt (cmd.exe), as a batch script" if IS_WINDOWS else "the system shell (/bin/sh)")
            lay.addSpacing(4)
            lay.addWidget(QLabel("Command or script"))
            lay.addWidget(self.command)
            info = QLabel(f"Runs with {where}, in the tool folder:\n{workdir}\n"
                          "Python programs started from here get the same input handling as tool cells "
                          "(predefined answers, input timeout). Other programs are never timed out.")
            info.setWordWrap(True)
            info.setObjectName("Muted")
            lay.addWidget(info)
            self.command.textChanged.connect(self._validate)
        else:
            tools = sorted(registry.tools, key=lambda t: t.meta.name.casefold())
            for t in tools:
                self.tool_combo.addItem(icons.icon(t.meta.icon, t.meta.color, 16), t.meta.name, t.id)
            if cell and cell.tool_id and self.tool_combo.findData(cell.tool_id) < 0:
                self.tool_combo.addItem(f"{cell.tool_id} (not available)", cell.tool_id)
            if cell and cell.tool_id:
                self.tool_combo.setCurrentIndex(max(0, self.tool_combo.findData(cell.tool_id)))
            self.tool_combo.setAccessibleName("Tool")
            self.tool_info.setWordWrap(True)
            self.tool_info.setObjectName("Muted")
            lay.addSpacing(4)
            lay.addWidget(QLabel("Tool"))
            lay.addWidget(self.tool_combo)
            lay.addWidget(self.tool_info)
            hint = QLabel("Launched exactly like the Run button does: the tool's own .venv interpreter if it has one, "
                          "otherwise the global Python.")
            hint.setWordWrap(True)
            hint.setObjectName("Muted")
            lay.addWidget(hint)
            self.tool_combo.currentIndexChanged.connect(self._tool_changed)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setText("Save" if editing else "Add")
        self.ok.setObjectName("Primary")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addSpacing(6)
        lay.addWidget(self.buttons)
        if kind is CellKind.TOOL:
            self._tool_changed()
        self._validate()

    def _tool_changed(self) -> None:
        tool = self._registry.get(self.tool_combo.currentData() or "")
        self.tool_info.setText(tool.meta.description.strip() if tool is not None and tool.meta.description.strip()
                               else ("No description." if tool is not None else "This tool is not available."))
        self._validate()

    def _validate(self) -> None:
        if self.kind is CellKind.CODE:
            ok = bool(self.command.toPlainText().strip())
        else:
            ok = self._registry.get(self.tool_combo.currentData() or "") is not None
        self.ok.setEnabled(ok)

    def values(self) -> dict:
        if self.kind is CellKind.CODE:
            return {"label": self.name.text().strip(), "command": self.command.toPlainText()}
        return {"label": self.name.text().strip(), "tool_id": self.tool_combo.currentData() or ""}


# =============================================================================== the drawer
class QueueDrawer(SlideDrawer):
    PAGE_LIST, PAGE_OUTPUT = 0, 1

    def __init__(self, manager: SessionManager, registry: ToolRegistry, icons: IconManager, parent: QWidget):
        super().__init__(parent)
        self._manager, self._registry, self._icons = manager, registry, icons
        self.session: Session | None = None
        self.queue: SessionQueue | None = None
        self._expanded: dict[str, set[str]] = {}          # session id -> ids of cells whose paperclip area is open
        self._cards: dict[str, CellCard] = {}
        self._output_cell: QueueCell | None = None

        self.pages = QStackedWidget()
        pl = QVBoxLayout(self.panel)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.addWidget(self.pages)
        self.pages.addWidget(self._build_list_page())
        self.pages.addWidget(self._build_output_page())

        self._clock = QTimer(self)                         # elapsed time of the running cell, only while visible
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._tick_clock)
        manager.sessionRemoved.connect(self._on_session_removed)

    # ------------------------------------------------------------------ geometry
    def panel_width(self, window_width: int) -> int:
        return drawer_width(window_width, 340, 520, 2)

    # ------------------------------------------------------------------ list page
    def _build_list_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 14, 12, 14)
        lay.setSpacing(10)

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
        box.setStyleSheet(f"#MustBox {{ background: {theme.CARD}; border: 1px solid {theme.BORDER}; border-radius: 10px; }}")
        ml = QVBoxLayout(box)
        ml.setContentsMargins(12, 8, 12, 8)
        ml.setSpacing(2)
        self.must = QCheckBox("Must Execute")
        self.must.setStyleSheet("font-weight: 600;")
        self.must.setAccessibleName("Must Execute")
        self.must.setToolTip(
            "Off: the queue runs only if the main process succeeds and stops at the first failed cell.\n"
            "On: the queue runs even if the main process fails, a failed cell never blocks the later ones, and a "
            "Python program that is blocked waiting for input is stopped after 5 minutes (other programs are never "
            "timed out: their input waits cannot be detected reliably).\n"
            "Best effort: it only works while the Python Toolbox application keeps running.")
        self.must.toggled.connect(self._must_toggled)
        self.must_note = QLabel()
        self.must_note.setWordWrap(True)
        self.must_note.setObjectName("Muted")
        self.must_note.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 12px;")
        ml.addWidget(self.must)
        ml.addWidget(self.must_note)
        lay.addWidget(box)

        # the cells
        self._list = QVBoxLayout()
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(8)
        holder = QWidget()
        holder.setObjectName("Page")
        holder.setStyleSheet("#Page { background: transparent; }")
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 4, 0)
        hl.addLayout(self._list)
        hl.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(holder)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.empty = QLabel("Add code cells and tool cells here.\nThey run one after another as soon as the main "
                            "process has finished.")
        self.empty.setObjectName("Muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        lay.addWidget(self.empty)
        lay.addWidget(self.scroll, 1)

        self.add_button = QPushButton(" Add cell")
        self.add_button.setIcon(self._icons.icon("plus", theme.TEXT, 16))
        self.add_button.setObjectName("Primary")
        menu = QMenu(self.add_button)
        self.add_code_action = menu.addAction(self._icons.icon("terminal", theme.TEXT, 16), "Code cell — run a command or script")
        self.add_tool_action = menu.addAction(self._icons.icon("wrench", theme.TEXT, 16), "Tool cell — launch an existing tool")
        self.add_code_action.triggered.connect(lambda: self.add_cell(CellKind.CODE))
        self.add_tool_action.triggered.connect(lambda: self.add_cell(CellKind.TOOL))
        self.add_button.setMenu(menu)
        lay.addWidget(self.add_button)
        return page

    # ------------------------------------------------------------------ output page
    def _build_output_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 14, 12, 14)
        lay.setSpacing(8)
        head = QHBoxLayout()
        self.back_button = QPushButton(" Cells")
        self.back_button.setIcon(self._icons.icon("arrow-left", theme.TEXT, 16))
        self.back_button.setAccessibleName("Back to the cell list")
        self.back_button.clicked.connect(self.show_list)
        self.out_title = ElidedLabel()
        self.out_title.setStyleSheet("font-weight: 600; font-size: 14px;")
        self.out_close = QToolButton()
        self.out_close.setObjectName("Flat")
        self.out_close.setIcon(self._icons.icon("x", theme.TEXT, 18))
        self.out_close.setToolTip("Close")
        self.out_close.clicked.connect(self.close_drawer)
        head.addWidget(self.back_button)
        head.addWidget(self.out_title, 1)
        head.addWidget(self.out_close)
        lay.addLayout(head)
        self.out_status = ElidedLabel()
        lay.addWidget(self.out_status)
        self.terminal = TerminalView()
        self.terminal.setPlaceholderText("This cell has not printed anything yet.")
        lay.addWidget(self.terminal, 1)
        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setAccessibleName("Cell input")
        self.clear_button = QPushButton("Clear")
        self.clear_button.setFixedHeight(28)
        self.clear_button.setStyleSheet("padding: 2px 12px;")
        self.clear_button.setToolTip("Clear the displayed output (the recorded output is kept)")
        row.addWidget(self.input, 1)
        row.addWidget(self.clear_button)
        lay.addLayout(row)
        self.input.returnPressed.connect(self._send_input)
        self.clear_button.clicked.connect(self.terminal.clear_output)
        return page

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
        self._output_cell = None
        if self.queue is not None:
            self.queue.structureChanged.connect(self.rebuild)
            self.queue.stateChanged.connect(self._on_state)
            self.queue.cellChanged.connect(self._on_cell)
            self.queue.cellOutput.connect(self._on_cell_output)
        self.pages.setCurrentIndex(self.PAGE_LIST)
        self.rebuild()

    def _on_session_removed(self, session: Session) -> None:
        if session is self.session:
            self.close_now()
            self.bind(None)

    # ------------------------------------------------------------------ SlideDrawer hooks
    def prepare_open(self) -> None:
        self.pages.setCurrentIndex(self.PAGE_LIST)
        self.rebuild()
        self._clock.start()

    def focus_on_open(self) -> None:
        self.add_button.setFocus()

    def on_closed(self) -> None:
        self._clock.stop()
        self._output_cell = None

    # ------------------------------------------------------------------ rendering
    def rebuild(self, *_args) -> None:
        """Re-create the cards (structure changed). Typed text lives in the cells, so nothing is lost."""
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
        q = self.queue
        if q is None or self.session is None:
            self.title.setText("Queue")
            self.subtitle.setFullText("")
            self.empty.show()
            self.scroll.hide()
            return
        opened = self._expanded.setdefault(self.session.id, set())
        for cell in q.cells:
            card = CellCard(q, cell, self._icons, cell.id in opened)
            card.editRequested.connect(self.edit_cell)
            card.removeRequested.connect(self.remove_cell)
            card.moveRequested.connect(lambda c, d: q.move_cell(c.id, d))
            card.outputRequested.connect(self.show_output)
            card.expandedChanged.connect(self._remember_expanded)
            self._cards[cell.id] = card
            self._list.addWidget(card)
        self.empty.setVisible(not q.cells)
        self.scroll.setVisible(bool(q.cells))
        QTimer.singleShot(0, lambda: bar.setValue(keep))
        self._refresh_header()

    def _remember_expanded(self, cell: QueueCell, expanded: bool) -> None:
        if self.session is not None:
            s = self._expanded.setdefault(self.session.id, set())
            (s.add if expanded else s.discard)(cell.id)

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
        executing = q.executing
        self.stop_button.setVisible(executing)
        self.stop_button.setText(" Force stop" if q.stopping else " Stop queue")
        self.stop_button.setToolTip("Stop the running cell and cancel the cells that have not started"
                                    if not q.stopping else "The cell did not exit yet: terminate it now")
        self.must.blockSignals(True)
        self.must.setChecked(q.must_execute)
        self.must.blockSignals(False)
        self.must_note.setText("Runs even if the main process fails, continues after a failed cell, and stops a Python cell "
                               "that waits for input for 5 minutes." if q.must_execute else
                               "Off: runs only if the main process succeeds and stops at the first failed cell.")

    def _on_state(self) -> None:
        self._refresh_header()

    def _on_cell(self, cell: QueueCell) -> None:
        card = self._cards.get(cell.id)
        if card is not None:
            card.refresh()
        if cell is self._output_cell:
            self._refresh_output_header()

    def _tick_clock(self) -> None:
        q = self.queue
        if q is None:
            return
        for cell in q.cells:
            if cell.is_active and cell.id in self._cards:
                self._cards[cell.id].refresh()
        if self._output_cell is not None and self._output_cell.is_active:
            self._refresh_output_header()

    # ------------------------------------------------------------------ actions
    def _must_toggled(self, checked: bool) -> None:
        if self.queue is not None:
            self.queue.set_must_execute(checked)

    def _stop(self) -> None:
        if self.queue is not None:
            self.queue.stop()

    def add_cell(self, kind: CellKind) -> None:
        if self.queue is None or self.session is None:
            return
        dlg = CellEditorDialog(kind, self._registry, self._icons, self.session.tool.path, None, self)
        if self._run_dialog(dlg):
            v = dlg.values()
            cell = self.queue.add_cell(kind, **v)
            self._remember_expanded(cell, False)

    def edit_cell(self, cell: QueueCell) -> None:
        if self.queue is None or self.session is None or not cell.is_pending:
            return
        dlg = CellEditorDialog(cell.kind, self._registry, self._icons, self.session.tool.path, cell, self)
        if self._run_dialog(dlg):
            if not self.queue.update_cell(cell.id, **dlg.values()):
                QMessageBox.information(self, "Cell already started",
                                        "This cell has already started, so it can no longer be changed.")

    def _run_dialog(self, dlg: CellEditorDialog) -> bool:
        try:
            return dlg.exec() == QDialog.DialogCode.Accepted
        finally:
            dlg.deleteLater()

    def remove_cell(self, cell: QueueCell) -> None:
        if self.queue is not None:
            self.queue.remove_cell(cell.id)

    # ------------------------------------------------------------------ output page
    def show_output(self, cell: QueueCell) -> None:
        self._output_cell = cell
        self.terminal.clear_output()
        for kind, text in cell.output:
            self.terminal.feed(kind, text)
        self.pages.setCurrentIndex(self.PAGE_OUTPUT)
        self._refresh_output_header()
        if self.input.isEnabled():
            self.input.setFocus()

    def show_list(self) -> None:
        self._output_cell = None
        self.pages.setCurrentIndex(self.PAGE_LIST)

    def _refresh_output_header(self) -> None:
        cell, q = self._output_cell, self.queue
        if cell is None or q is None:
            return
        idx = q.index_of(cell) + 1 if cell in q.cells else 0
        self.out_title.setFullText(f"Cell {idx} · {cell.summary(q.tool_name(cell))}")
        card = self._cards.get(cell.id)
        line = card.status_line() if card is not None else CELL_STATUS_TEXT[cell.status]
        self.out_status.setFullText(line)
        self.out_status.setStyleSheet(f"color: {CELL_COLORS[cell.status]};")
        live = q.current_cell is cell and not q.stopping
        self.input.setEnabled(live)
        self.input.setPlaceholderText("Type input for this cell and press Enter" if live
                                      else "Input is available while the cell is running")

    def _on_cell_output(self, cell: QueueCell, kind: str, text: str) -> None:
        if cell is self._output_cell:
            self.terminal.feed(kind, text)

    def _send_input(self) -> None:
        cell, q = self._output_cell, self.queue
        if cell is not None and q is not None and q.send_input(cell, self.input.text()):
            self.input.clear()
