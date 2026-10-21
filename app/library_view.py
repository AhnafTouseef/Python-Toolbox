"""Responsive tool library with incremental ("lazy") card rendering.

Design (stable scrolling is the priority):

* One scroll area, one canvas widget.  The canvas is sized from pure arithmetic
  (rows * fixed card height), so the scroll range is correct from the first frame
  and never changes while cards are created.  The vertical scrollbar is always on
  so the viewport width (and therefore the column count) cannot flip-flop.
* Card geometry is computed from the item index; there is no QLayout to
  recompute.  Scrolling itself does no geometry work.
* A scroll event only starts a coalescing timer.  The render pass then creates
  the *missing* cards of the visible rows plus a small buffer, within a small time
  budget per pass.  Existing cards are never destroyed or recreated.
* Render state per card (NOT_RENDERED / RENDERING / RENDERED / FAILED) is tracked
  separately from process state.  A failed card shows a placeholder with an
  explicit Retry button; nothing is retried automatically.
* Resize: only the cards that fall in the render window are repositioned, and a
  row anchor keeps the same content in view when the column count changes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from PySide6.QtCore import QEvent, QObject, QRect, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QKeyEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
                               QScrollArea, QToolButton, QVBoxLayout, QWidget)

from . import theme
from .icon_manager import IconManager
from .tool_card import CARD_BODY_H, GLOW, ToolCard
from .tool_registry import ToolInfo, ToolRegistry

log = logging.getLogger(__name__)

StatusProvider = Callable[[str], "tuple[str | None, int]"]

MIN_CARD_W = 240
RENDER_BUDGET_S = 0.008      # per render pass; remaining work continues on the next event-loop turn
BUFFER_ROWS = 2


class RenderState(Enum):
    NOT_RENDERED = "not rendered"
    RENDERING = "rendering"
    RENDERED = "rendered"
    FAILED = "failed"


@dataclass(frozen=True)
class Metrics:
    width: int
    margin: int
    gap: int
    cols: int
    card_w: int

    @property
    def top(self) -> int:
        return self.margin

    @property
    def pitch(self) -> int:
        return CARD_BODY_H + self.gap


def compute_metrics(viewport_width: int) -> Metrics:
    small = viewport_width < 640
    margin = 12 if small else 24
    gap = 10 if small else 16
    avail = max(1, viewport_width - 2 * margin)
    cols = max(1, (avail + gap) // (MIN_CARD_W + gap))
    card_w = max(60, (avail - gap * (cols - 1)) // cols)
    return Metrics(viewport_width, margin, gap, cols, card_w)


class _FailedSlot(QFrame):
    def __init__(self, tool: ToolInfo, error: str, on_retry: Callable[[str], None], parent: QWidget):
        super().__init__(parent)
        self.setObjectName("FailedSlot")
        self.setStyleSheet(f"#FailedSlot {{ background: {theme.CARD}; border: 1px dashed {theme.ERR}; border-radius: 12px; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(GLOW + 12, GLOW + 8, GLOW + 12, GLOW + 8)
        title = QLabel(f"Couldn't display “{tool.meta.name}”")
        title.setObjectName("Error")
        detail = QLabel(error)
        detail.setObjectName("Muted")
        detail.setWordWrap(True)
        retry = QPushButton("Retry")
        retry.clicked.connect(lambda: on_retry(tool.id))
        lay.addWidget(title)
        lay.addWidget(detail, 1)
        lay.addWidget(retry, 0, Qt.AlignmentFlag.AlignLeft)
        self.retry_button = retry


class IssueBanner(QFrame):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("IssueBanner")
        self.setStyleSheet("#IssueBanner { background: #2B2418; border: none; border-bottom: 1px solid #4A3B1C; }")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(24, 6, 16, 6)
        self._label = QLabel()
        self._label.setStyleSheet(f"color: {theme.WARN};")
        self._details = QPushButton("Details")
        self._dismiss = QPushButton("Dismiss")
        for b in (self._details, self._dismiss):
            b.setObjectName("Flat")
            b.setStyleSheet("padding: 3px 10px;")
        lay.addWidget(self._label, 1)
        lay.addWidget(self._details)
        lay.addWidget(self._dismiss)
        self._issues: list[str] = []
        self._details.clicked.connect(self._show_details)
        self._dismiss.clicked.connect(self.hide)
        self.hide()

    def set_issues(self, issues: list[str]) -> None:
        self._issues = list(issues)
        if issues:
            n = len(issues)
            self._label.setText(f"{n} notice{'s' if n != 1 else ''} while loading tool metadata.")
            self.show()
        else:
            self.hide()

    def _show_details(self) -> None:
        box = QMessageBox(self.window())
        box.setWindowTitle("Tool metadata notices")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("Some tool.json files needed attention:")
        box.setInformativeText("\n\n".join(self._issues))
        box.exec()


class LibraryView(QWidget):
    openRequested = Signal(str)
    settingsRequested = Signal(str)
    activeRequested = Signal()
    refreshRequested = Signal()
    foldersRequested = Signal()

    def __init__(self, registry: ToolRegistry, icons: IconManager, status_provider: StatusProvider,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Page")
        self._registry = registry
        self._icons = icons
        self._status_provider = status_provider

        self._items: list[ToolInfo] = []
        self._item_index: dict[str, int] = {}
        self._states: dict[str, RenderState] = {}
        self._cards: dict[str, ToolCard] = {}
        self._slots: dict[str, QWidget] = {}          # card or failed placeholder, by tool id
        self._metrics: Metrics | None = None
        self._version = 0
        self._filter = ""
        # Row anchor (item index, pixel offset into its row). Derived from the viewport the first
        # time a resize needs it, then kept until the *user* scrolls, so a series of resizes through
        # different column counts cannot walk the view away from the content the user was looking at.
        self._anchor: tuple[int, int] | None = None
        self._programmatic_scroll = False
        self.card_factory: Callable[[ToolInfo], ToolCard] = lambda t: ToolCard(t, self._icons, self._canvas)
        self.stats = {"cards_created": 0, "render_passes": 0, "relayouts": 0, "reposition_calls": 0}

        # -- header ---------------------------------------------------------
        header = QFrame()
        header.setObjectName("Header")
        header.setStyleSheet(f"#Header {{ background: {theme.PANEL}; border: none; border-bottom: 1px solid {theme.BORDER}; }}")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(24, 12, 16, 12)
        hl.setSpacing(12)
        title = QLabel("Python Toolbox")
        title.setObjectName("Title")
        self._count = QLabel()
        self._count.setObjectName("Muted")
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search tools")
        self._search.setClearButtonEnabled(True)
        self._search.setMinimumWidth(160)
        self._search.setMaximumWidth(360)
        self._search.setAccessibleName("Search tools")
        self._search.addAction(icons.icon("search", theme.TEXT_MUTED, 16), QLineEdit.ActionPosition.LeadingPosition)
        self._active_btn = QPushButton(" Active")
        self._active_btn.setIcon(icons.icon("activity", theme.TEXT, 16))
        self._active_btn.setToolTip("Show running processes")
        self._active_btn.setAccessibleName("Active processes")
        self._notice = QLabel()
        self._notice.setStyleSheet(f"color: {theme.INFO}; font-size: 12px;")
        self._notice.setAccessibleName("Refresh result")
        self._notice.hide()
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.setInterval(6000)
        self._notice_timer.timeout.connect(self._notice.hide)
        self._refresh_btn = QToolButton()
        self._refresh_btn.setObjectName("Flat")
        self._refresh_btn.setIcon(icons.icon("refresh-cw", theme.TEXT, 18))
        self._refresh_btn.setIconSize(QSize(18, 18))
        self._refresh_btn.setFixedSize(32, 32)
        self._refresh_btn.setToolTip("Refresh: look for added, removed or changed tools (F5)")
        self._refresh_btn.setAccessibleName("Refresh tools")
        self._refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._folders_btn = QToolButton()
        self._folders_btn.setObjectName("Flat")
        self._folders_btn.setIcon(icons.icon("folder-open", theme.TEXT, 18))
        self._folders_btn.setIconSize(QSize(18, 18))
        self._folders_btn.setFixedSize(32, 32)
        self._folders_btn.setToolTip("Tool folders: add more places to look for tools")
        self._folders_btn.setAccessibleName("Tool folders")
        self._folders_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        hl.addWidget(title)
        hl.addWidget(self._count)
        hl.addWidget(self._notice)
        hl.addStretch(1)
        hl.addWidget(self._search, 2)
        hl.addWidget(self._refresh_btn)
        hl.addWidget(self._folders_btn)
        hl.addWidget(self._active_btn)

        self._banner = IssueBanner()

        # -- scroll area / canvas ------------------------------------------------
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(False)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)   # stable viewport width
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._canvas = QWidget()
        self._canvas.setObjectName("Page")
        self._scroll.setWidget(self._canvas)
        self._scroll.viewport().installEventFilter(self)
        self._sb = self._scroll.verticalScrollBar()
        self._sb.setSingleStep(48)

        self._empty = QLabel(self._scroll.viewport())
        self._empty.setObjectName("Muted")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        self._empty.hide()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(header)
        lay.addWidget(self._banner)
        lay.addWidget(self._scroll, 1)

        # -- scheduling -------------------------------------------------------------
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(0)
        self._render_timer.timeout.connect(self._render_pass)

        self._sb.valueChanged.connect(self._on_scroll)             # scroll => scheduling only
        self._search.textChanged.connect(self._on_search)
        self._active_btn.clicked.connect(self.activeRequested)
        self._refresh_btn.clicked.connect(self.refreshRequested)
        self._folders_btn.clicked.connect(self.foldersRequested)
        QShortcut(QKeySequence(Qt.Key.Key_F5), self, self.refreshRequested.emit)
        self._registry.metadataChanged.connect(self._on_metadata_changed)

        self.reload()

    # ------------------------------------------------------------------ data
    def reload(self) -> None:
        """(Re)read the tool list from the registry. Existing cards are kept when still valid."""
        self._banner.set_issues(self._registry.issues)
        known = {t.id for t in self._registry.tools}
        for tid in [t for t in self._slots if t not in known]:
            self._drop_slot(tid)
        self._apply_filter(self._filter, reset_scroll=False)

    def show_notice(self, text: str) -> None:
        """A short message next to the tool count (e.g. the result of a refresh); it fades after a few seconds."""
        self._notice.setText(text)
        self._notice.setVisible(bool(text))
        if text:
            self._notice_timer.start()

    def set_active_count(self, n: int) -> None:
        self._active_btn.setText(" Active" if n == 0 else f" Active · {n}")

    def refresh_status(self, tool_id: str) -> None:
        card = self._cards.get(tool_id)
        if card is not None:
            kind, count = self._status_provider(tool_id)
            card.set_status(kind, count)

    def card(self, tool_id: str) -> ToolCard | None:
        return self._cards.get(tool_id)

    def render_state(self, tool_id: str) -> RenderState:
        return self._states.get(tool_id, RenderState.NOT_RENDERED)

    @property
    def visible_tool_ids(self) -> list[str]:
        return [t.id for t in self._items]

    def focus_search(self) -> None:
        self._search.setFocus()
        self._search.selectAll()

    def _on_metadata_changed(self, tool_id: str) -> None:
        card = self._cards.get(tool_id)
        if card is not None:
            card.apply_metadata()

    # --------------------------------------------------------------- filtering
    def _on_search(self, text: str) -> None:
        self._apply_filter(text, reset_scroll=True)

    def _apply_filter(self, text: str, reset_scroll: bool) -> None:
        self._filter = text
        self._anchor = None
        words = text.casefold().split()
        if words:
            items = []
            for t in self._registry.tools:
                hay = f"{t.meta.name} {t.meta.description} {t.id}".casefold()
                if all(w in hay for w in words):
                    items.append(t)
        else:
            items = list(self._registry.tools)
        self._items = items
        self._item_index = {t.id: i for i, t in enumerate(items)}
        total = len(self._registry.tools)
        self._count.setText(f"{total} tool{'s' if total != 1 else ''}" if len(items) == total
                            else f"{len(items)} of {total}")
        # Cards of filtered-out tools are hidden, not destroyed (identity is preserved).
        for tid, w in self._slots.items():
            if tid not in self._item_index and not w.isHidden():
                w.hide()
        if not items:
            self._empty.setText("No tools found.\nAdd a folder with a main.py to the tools directory (or add another place with the folder button), then press Refresh."
                                if not self._registry.tools else f"No tools match “{text}”.")
        self._empty.setVisible(not items)
        self._position_empty()
        self._relayout(force=True, keep_position=not reset_scroll)
        if reset_scroll:
            self._sb.setValue(0)

    def _on_scroll(self, _value: int) -> None:
        if not self._programmatic_scroll:
            self._anchor = None            # the user moved: the next resize re-derives its anchor
        self._schedule_render()

    def _set_scroll(self, value: int) -> None:
        self._programmatic_scroll = True
        try:
            self._sb.setValue(value)
        finally:
            self._programmatic_scroll = False

    # ------------------------------------------------------------------ geometry
    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self._scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._relayout(force=False, keep_position=True)
            self._position_empty()
            self._schedule_render()
        return super().eventFilter(obj, event)

    def _position_empty(self) -> None:
        vp = self._scroll.viewport()
        self._empty.setGeometry(24, 24, max(10, vp.width() - 48), 80)

    def _rect_for(self, idx: int, m: Metrics) -> QRect:
        row, col = divmod(idx, m.cols)
        x = m.margin + col * (m.card_w + m.gap) - GLOW
        y = m.top + row * m.pitch - GLOW
        return QRect(x, y, m.card_w + 2 * GLOW, CARD_BODY_H + 2 * GLOW)

    def _content_height(self, m: Metrics) -> int:
        n = len(self._items)
        if n == 0:
            return 1
        rows = (n + m.cols - 1) // m.cols
        return m.top + rows * CARD_BODY_H + (rows - 1) * m.gap + m.margin

    def _relayout(self, force: bool, keep_position: bool) -> None:
        vw = self._scroll.viewport().width()
        if vw <= 1:
            return
        new = compute_metrics(vw)
        old = self._metrics
        height = self._content_height(new)
        if not force and old == new and self._canvas.height() == height:
            return
        self.stats["relayouts"] += 1

        # Row anchor: which item/offset is at the top of the viewport (persisted across resizes).
        anchor_idx = anchor_off = None
        if keep_position and old is not None and self._items:
            if self._anchor is None or self._anchor[0] >= len(self._items):
                y0 = self._sb.value()
                row = max(0, (y0 - old.top) // old.pitch)
                self._anchor = (min(row * old.cols, len(self._items) - 1),
                                y0 - (old.top + row * old.pitch))
            anchor_idx, anchor_off = self._anchor

        self._metrics = new
        self._version += 1
        self._programmatic_scroll = True       # canvas resize may clamp the scrollbar range/value
        try:
            self._canvas.resize(vw, height)
        finally:
            self._programmatic_scroll = False

        # Only cards inside the render window are touched; any other rendered card is hidden
        # so a stale position can never show through.
        lo, hi = self._window_range()
        for tid, w in self._slots.items():
            idx = self._item_index.get(tid)
            if idx is not None and lo <= idx < hi:
                self._place(tid, w, idx)
            elif not w.isHidden():
                w.hide()

        if anchor_idx is not None:
            target = new.top + (anchor_idx // new.cols) * new.pitch + anchor_off
            self._set_scroll(max(0, min(target, self._sb.maximum())))
        self._schedule_render()

    def _place(self, tid: str, w: QWidget, idx: int) -> None:
        m = self._metrics
        if m is None:
            return
        if getattr(w, "layout_version", None) != self._version or w.isHidden():
            rect = self._rect_for(idx, m)
            if w.geometry() != rect:
                w.setGeometry(rect)
                self.stats["reposition_calls"] += 1
            w.layout_version = self._version
            if w.isHidden():
                w.show()

    def _rows(self) -> tuple[int, int, int, int]:
        """(first visible row, last visible row, first window row, last window row)."""
        m = self._metrics
        n = len(self._items)
        if m is None or n == 0:
            return 0, -1, 0, -1
        rows = (n + m.cols - 1) // m.cols
        y0 = self._sb.value()
        vh = self._scroll.viewport().height()
        fv = max(0, (y0 - m.top) // m.pitch)
        lv = min(rows - 1, max(0, (y0 + vh - m.top) // m.pitch))
        return fv, lv, max(0, fv - BUFFER_ROWS), min(rows - 1, lv + BUFFER_ROWS)

    def _window_range(self) -> tuple[int, int]:
        m = self._metrics
        _, _, fw, lw = self._rows()
        if m is None or lw < fw:
            return 0, 0
        return fw * m.cols, min(len(self._items), (lw + 1) * m.cols)

    # ------------------------------------------------------------------ rendering
    def _schedule_render(self, *_args) -> None:
        if not self._render_timer.isActive():
            self._render_timer.start()

    def _render_pass(self) -> None:
        self.stats["render_passes"] += 1
        m = self._metrics
        if m is None or not self._items:
            return
        fv, lv, fw, lw = self._rows()
        order = sorted(range(fw, lw + 1),
                       key=lambda r: 0 if fv <= r <= lv else (fv - r if r < fv else r - lv))
        deadline = time.perf_counter() + RENDER_BUDGET_S
        created = 0
        n = len(self._items)
        for row in order:
            on_screen = fv <= row <= lv
            for idx in range(row * m.cols, min(n, (row + 1) * m.cols)):
                tool = self._items[idx]
                state = self._states.get(tool.id, RenderState.NOT_RENDERED)
                if state is RenderState.NOT_RENDERED:
                    # Cards that are on screen are always built in this pass (a blank gap is worse than
                    # a short hitch); only the pre-render buffer is time-budgeted.
                    if not on_screen and created and time.perf_counter() > deadline:
                        self._schedule_render()          # continue on the next turn; input stays responsive
                        return
                    self._create_card(tool, idx)
                    created += 1
                elif state in (RenderState.RENDERED, RenderState.FAILED):
                    w = self._slots.get(tool.id)
                    if w is not None:
                        self._place(tool.id, w, idx)
                # RENDERING: construction is in progress -> never start a duplicate

    def _create_card(self, tool: ToolInfo, idx: int) -> None:
        tid = tool.id
        if self._states.get(tid, RenderState.NOT_RENDERED) is not RenderState.NOT_RENDERED:
            return                                           # already rendering/rendered/failed
        self._states[tid] = RenderState.RENDERING
        card = None
        try:
            card = self.card_factory(tool)
            card.openRequested.connect(self.openRequested)
            card.settingsRequested.connect(self.settingsRequested)
            card.navigateRequested.connect(self._on_navigate)
            kind, count = self._status_provider(tid)
            card.set_status(kind, count)
            self._cards[tid] = card
            self._slots[tid] = card
            self._place(tid, card, idx)
            card.show()
            self._states[tid] = RenderState.RENDERED
            self.stats["cards_created"] += 1
        except Exception as exc:
            log.exception("Rendering card for %s failed", tid)
            self._cards.pop(tid, None)
            self._slots.pop(tid, None)
            if card is not None:
                card.hide()
                card.deleteLater()
            self._states[tid] = RenderState.FAILED
            try:
                slot = _FailedSlot(tool, f"{type(exc).__name__}: {exc}", self.retry_card, self._canvas)
                self._slots[tid] = slot
                self._place(tid, slot, idx)
                slot.show()
            except Exception:
                log.exception("Could not even build a failure placeholder for %s", tid)

    def retry_card(self, tool_id: str) -> None:
        """Explicit, user-triggered retry of a failed card (never automatic)."""
        if self._states.get(tool_id) is not RenderState.FAILED:
            return
        slot = self._slots.pop(tool_id, None)
        if slot is not None:
            slot.hide()
            slot.deleteLater()
        self._states[tool_id] = RenderState.NOT_RENDERED
        self._schedule_render()

    def _drop_slot(self, tool_id: str) -> None:
        w = self._slots.pop(tool_id, None)
        self._cards.pop(tool_id, None)
        self._states.pop(tool_id, None)
        if w is not None:
            w.hide()
            w.deleteLater()

    # ------------------------------------------------------------------ keyboard
    def _on_navigate(self, tool_id: str, key: int) -> None:
        m = self._metrics
        idx = self._item_index.get(tool_id)
        if m is None or idx is None:
            return
        n = len(self._items)
        per_page = max(1, self._scroll.viewport().height() // m.pitch)
        K = Qt.Key
        target = {K.Key_Left: idx - 1, K.Key_Right: idx + 1, K.Key_Up: idx - m.cols, K.Key_Down: idx + m.cols,
                  K.Key_Home: 0, K.Key_End: n - 1,
                  K.Key_PageUp: idx - m.cols * per_page, K.Key_PageDown: idx + m.cols * per_page}.get(K(key), idx)
        if key == K.Key_Down and target >= n and idx // m.cols < (n - 1) // m.cols:
            target = n - 1                       # partial last row: go to its last item
        elif key == K.Key_PageDown:
            target = min(target, n - 1)
        elif key == K.Key_PageUp:
            target = max(target, 0)
        if not (0 <= target < n) or target == idx:
            return
        self.focus_index(target)

    def focus_index(self, idx: int) -> None:
        """Make sure the card at `idx` exists, scroll it into view and focus it."""
        m = self._metrics
        if m is None or not (0 <= idx < len(self._items)):
            return
        tool = self._items[idx]
        if self._states.get(tool.id, RenderState.NOT_RENDERED) is RenderState.NOT_RENDERED:
            self._create_card(tool, idx)
        top = m.top + (idx // m.cols) * m.pitch
        bottom = top + CARD_BODY_H
        y0, vh = self._sb.value(), self._scroll.viewport().height()
        if top - m.margin < y0:
            self._sb.setValue(max(0, top - m.margin))
        elif bottom + m.margin > y0 + vh:
            self._sb.setValue(bottom + m.margin - vh)
        w = self._slots.get(tool.id)
        if w is not None:
            self._place(tool.id, w, idx)
            target = getattr(w, "retry_button", w)
            target.setFocus(Qt.FocusReason.TabFocusReason)
