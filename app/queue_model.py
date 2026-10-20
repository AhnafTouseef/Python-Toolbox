"""Data side of the sequential execution queue (no widgets, no processes).

A queue belongs to one Session.  It is a list of cells that run one after another once that session's
main process has exited.  Cells are plain records; SessionQueue (session_queue.py) changes them and
announces every change, the drawer only displays them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class CellKind(str, Enum):
    CODE = "code"      # a shell command / script run in the tool folder
    TOOL = "tool"      # an existing tool, launched exactly like the Run button launches it


class CellStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"        # running, and reliably known to be blocked reading stdin
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"        # not attempted because the queue stopped after an earlier failure
    CANCELLED = "cancelled"    # stopped by the user


ACTIVE_STATUSES = (CellStatus.RUNNING, CellStatus.WAITING)
FINAL_STATUSES = (CellStatus.COMPLETED, CellStatus.FAILED, CellStatus.SKIPPED, CellStatus.CANCELLED)
NOT_SUCCESS_STATUSES = (CellStatus.FAILED, CellStatus.CANCELLED)

CELL_STATUS_TEXT = {
    CellStatus.PENDING: "Pending",
    CellStatus.RUNNING: "Running",
    CellStatus.WAITING: "Waiting for input",
    CellStatus.COMPLETED: "Completed",
    CellStatus.FAILED: "Failed",
    CellStatus.SKIPPED: "Skipped",
    CellStatus.CANCELLED: "Cancelled",
}


class QueueState(str, Enum):
    EMPTY = "empty"            # no cells
    WAITING = "waiting"        # cells are pending; they start when the main process exits
    EXECUTING = "executing"
    COMPLETED = "completed"    # every cell was attempted
    STOPPED = "stopped"        # a failure (policy) or the user ended the run early
    NOT_RUN = "not-run"        # the main process failed and Must Execute is off: cells are kept for later


INPUT_TIMEOUT_REASON = "input timeout (5 minutes)"
OUTPUT_LIMIT_CHARS = 2_000_000     # per cell; the oldest text is dropped beyond this


def parse_answers(text: str) -> list[str]:
    """``"1920, 1080, yes"`` -> ``["1920", "1080", "yes"]``.

    Whitespace around each answer is trimmed, so ``1920,1080,yes`` is identical.  An empty entry between
    two commas is an answer too (an empty line: "just press Enter"); empty entries at the end are ignored
    so that a trailing comma is harmless.  Commas cannot be part of an answer.
    """
    if not text or not text.strip():
        return []
    parts = [p.strip() for p in text.split(",")]
    while parts and parts[-1] == "":
        parts.pop()
    return parts


@dataclass
class QueueCell:
    kind: CellKind
    label: str = ""                    # optional name chosen by the user
    command: str = ""                  # CODE: the command / script text
    tool_id: str = ""                  # TOOL: the registered tool
    answers_text: str = ""             # predefined answers, comma separated (as typed)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    # -- results (filled in while / after the cell runs) ------------------------------
    status: CellStatus = CellStatus.PENDING
    reason: str = ""                   # why it failed / was skipped / cancelled
    exit_code: int | None = None
    pid: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    timed_out: bool = False
    detection: str = ""                # "python" (stdin waits are reliably seen) | "none" | "" (not started)
    answers_sent: list[str] = field(default_factory=list)
    unused_answers: list[str] = field(default_factory=list)
    output: list[tuple[str, str]] = field(default_factory=list)    # (kind, text) as delivered
    output_chars: int = 0
    output_truncated: bool = False

    @property
    def answers(self) -> list[str]:
        return parse_answers(self.answers_text)

    @property
    def is_pending(self) -> bool:
        return self.status is CellStatus.PENDING

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_STATUSES

    def append_output(self, kind: str, text: str) -> None:
        """Keep the transcript, bounded so that a runaway cell cannot exhaust memory."""
        if not text:
            return
        self.output.append((kind, text))
        self.output_chars += len(text)
        while self.output_chars > OUTPUT_LIMIT_CHARS and len(self.output) > 1:
            _, dropped = self.output.pop(0)
            self.output_chars -= len(dropped)
            self.output_truncated = True

    def output_text(self) -> str:
        return "".join(t for k, t in self.output if k != "input")

    def reset_run_state(self) -> None:
        self.status = CellStatus.PENDING
        self.reason = ""
        self.exit_code = None
        self.pid = None
        self.started_at = self.finished_at = None
        self.timed_out = False
        self.detection = ""
        self.answers_sent = []
        self.unused_answers = []
        self.output = []
        self.output_chars = 0
        self.output_truncated = False

    # -- display ----------------------------------------------------------------------
    def summary(self, tool_name: str | None = None) -> str:
        """Short title for the list: the user's name, else the tool name / first line of the command."""
        if self.label.strip():
            return self.label.strip()
        if self.kind is CellKind.TOOL:
            return tool_name or self.tool_id or "(no tool)"
        first = next((ln.strip() for ln in self.command.splitlines() if ln.strip()), "")
        return first or "(empty command)"
