"""Instances (sessions) of tools: one process, one transcript, one status each.

A Session is UI-free.  It owns a ProcessRunner, tracks status/exit information and
re-publishes the process output (plus its own system lines) as the ``output`` signal.
Panes in the workspace subscribe to it, so output keeps flowing into a session's pane
whether or not that tab is on screen.  SessionManager owns all sessions and answers
the questions the library, the tabs and the drawer ask ("what is running?", "which
instance did the user use last?").
"""

from __future__ import annotations

import itertools
import logging
import time
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from .config import AppConfig
from .process_manager import ENTRY_NAME, LaunchError, ProcessRunner, describe_exit, plan_tool_launch
from .queue_model import parse_answers
from .session_queue import SessionQueue
from .stdin_watch import StdinProbe, advance_waits
from .tool_metadata import ToolMetadata
from .tool_registry import ToolInfo, ToolRegistry

log = logging.getLogger(__name__)


class SessionStatus(str, Enum):
    IDLE = "idle"              # opened, never run
    RUNNING = "running"
    COMPLETED = "completed"    # exit code 0
    FAILED = "failed"          # unexpected non-zero exit
    STOPPED = "stopped"        # the user stopped it
    ERROR = "error"            # could not be launched


STATUS_TEXT = {
    SessionStatus.IDLE: "Ready",
    SessionStatus.RUNNING: "Running",
    SessionStatus.COMPLETED: "Completed",
    SessionStatus.FAILED: "Failed",
    SessionStatus.STOPPED: "Stopped",
    SessionStatus.ERROR: "Launch error",
}


class Session(QObject):
    statusChanged = Signal(object)            # self
    output = Signal(str, str)                 # kind ("stdout" | "stderr" | "input" | "system"), text
    # The main process is over: (exit code, stopped by the user). The code is None when it could not be launched.
    # The session's queue listens to this - it is the real exit event, not a guess from output or timing.
    exited = Signal(object, bool)
    answersChanged = Signal(object)           # self: the predefined answers, or how many of them were typed in, changed

    poll_ms = 100                             # how often the stdin probe is looked at while answers are pending
    answer_debounce_s = 0.3                   # a read must block this long before it counts as a wait

    def __init__(self, tool: ToolInfo, ordinal: int, config: AppConfig, parent: QObject | None = None,
                 registry: ToolRegistry | None = None, workflow: bool = False):
        super().__init__(parent)
        self.id = uuid.uuid4().hex[:10]
        self.is_workflow = workflow           # a queue on its own: no main process, ``tool`` only carries its working folder
        self.tool = tool
        self.ordinal = ordinal                # stable per-tool instance number
        self.status = SessionStatus.IDLE
        self.exit_code: int | None = None
        self.detail = ""                      # short extra text for the status line
        self.run_count = 0
        self.started_at: datetime | None = None
        self.last_active = 0                  # set by SessionManager.touch (monotonic counter)
        self._config = config
        self.runner = ProcessRunner(self)
        self.runner.output.connect(self.output)
        self.runner.finished.connect(self._on_finished)
        self.runner.stdinFailed.connect(lambda: self._system("(The script is no longer reading input.)"))
        self.queue = SessionQueue(self, config, registry.get if registry is not None else None)
        # Predefined answers for the main process (like the paperclip of a queue cell). A new instance starts with
        # the answers the user chose to keep for this tool.
        self.answers_text = config.tool_answers.get(tool.id, "")
        self.keep_answers = tool.id in config.tool_answers
        self.answers_sent: list[str] = []
        self.unused_answers: list[str] = []
        self._run_answers: list[str] = []
        self._probe: StdinProbe | None = None
        self._waits: tuple[dict, dict, dict] = ({}, {}, {})          # seen / answered / unsatisfied (see advance_waits)
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._tick)

    # ------------------------------------------------------------------ queries
    @property
    def tool_id(self) -> str:
        return self.tool.id

    @property
    def is_running(self) -> bool:
        return self.status is SessionStatus.RUNNING

    @property
    def is_active(self) -> bool:
        """The main process is running, or this instance's queue is executing / still has a live process."""
        return self.is_running or self.queue.busy

    @property
    def stopping(self) -> bool:
        return self.is_running and self.runner.stop_requested

    @property
    def pid(self) -> int | None:
        return self.runner.pid

    @property
    def status_text(self) -> str:
        s = self.status
        if s is SessionStatus.RUNNING:
            return "Stopping…" if self.stopping else (f"Running · PID {self.pid}" if self.pid else "Running")
        if s in (SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.STOPPED) and self.exit_code is not None:
            return f"{STATUS_TEXT[s]} · {describe_exit(self.exit_code)}"
        return STATUS_TEXT[s]

    @property
    def answers(self) -> list[str]:
        return parse_answers(self.answers_text)

    @property
    def run_answer_count(self) -> int:
        """How many predefined answers the current (or last) run started with."""
        return len(self._run_answers)

    # ------------------------------------------------------------------ predefined answers
    def set_answers(self, text: str) -> bool:
        """Change the predefined answers. Frozen while the main process runs (a run uses the answers it started
        with). With "keep" ticked the new text is also what new instances of this tool start with."""
        if self.is_running:
            return False
        self.answers_text = text
        if self.keep_answers:
            self._config.set_tool_answers(self.tool.id, text)
        self.answersChanged.emit(self)
        return True

    def set_keep_answers(self, keep: bool) -> str | None:
        """Tick / untick "Keep for this tool". Returns an error message when config.json could not be written."""
        self.keep_answers = keep
        self.answersChanged.emit(self)
        return self._config.set_tool_answers(self.tool.id, self.answers_text if keep else None)

    def _send_next_answer(self) -> bool:
        i = len(self.answers_sent)
        if i >= len(self._run_answers):
            return False
        answer = self._run_answers[i]
        self.answers_sent.append(answer)
        self.output.emit("input", answer + "\n")            # a pipe has no echo: show what was typed for you
        self.runner.write(answer + "\n")
        self.answersChanged.emit(self)
        return True

    def _tick(self) -> None:
        if self._probe is None or not self.is_running or self.runner.stop_requested:
            return                                          # stopping: nothing more is typed
        snap = self._probe.scan()
        if snap.active:
            advance_waits(*self._waits, snap.waits, time.monotonic(), self.answer_debounce_s, self._send_next_answer)

    def _end_answers(self) -> None:
        self._poll.stop()
        if self._probe is not None:
            self._probe.close()
            self._probe = None
        remaining = self._run_answers[len(self.answers_sent):]
        if remaining:
            self.unused_answers = remaining
            self._system(f"(Not used: {len(remaining)} predefined answer{'s' if len(remaining) != 1 else ''} — "
                         + ", ".join(remaining) + ")")
            self.answersChanged.emit(self)

    # ------------------------------------------------------------------ helpers
    def _system(self, text: str) -> None:
        self.output.emit("system", text + "\n")

    def _set_status(self, status: SessionStatus) -> None:
        self.status = status
        self.statusChanged.emit(self)

    # ------------------------------------------------------------------ run / stop
    def run(self) -> None:
        """Start a fresh process for this instance; output is appended below the previous runs."""
        if self.is_workflow or self.is_running or self.queue.busy:   # (a workflow has no main process; a running
            return                                                  # queue owns its instance until it is done)
        self.run_count += 1
        now = datetime.now()
        if self.run_count > 1:
            self.output.emit("system", "\n")
        self._system(f"──────── Run {self.run_count} · {now:%Y-%m-%d %H:%M:%S} ────────")
        self.exit_code = None
        self._run_answers = self.answers
        self.answers_sent, self.unused_answers = [], []
        self._waits = ({}, {}, {})
        probe = StdinProbe() if self._run_answers else None      # only runs with answers are observed; others stay as before
        try:
            argv, interp, source = plan_tool_launch(self.tool.path, self.tool.entry, self._config.resolved_global_python())
            self._system(f"> {interp} -u {ENTRY_NAME}   ({'tool .venv' if source == 'venv' else 'global Python'})")
            self._system(f"  in {self.tool.path}")
            if probe is not None:
                n = len(self._run_answers)
                self._system(f"  {n} predefined answer{'s' if n != 1 else ''}: typed in one by one when the script asks for input")
            self.runner.start(argv, str(self.tool.path), probe.env() if probe is not None else None)
        except LaunchError as exc:
            if probe is not None:
                probe.close()
            log.warning("Launch failed for %s: %s", self.tool.id, exc)
            self.detail = str(exc).splitlines()[0]
            self.output.emit("stderr", f"Could not start “{self.tool.meta.name}”:\n{exc}\n")
            self._set_status(SessionStatus.ERROR)
            self.exited.emit(None, False)
            return
        except Exception as exc:                    # never let one tool take the UI down
            if probe is not None:
                probe.close()
            log.exception("Unexpected failure while starting %s", self.tool.id)
            self.detail = str(exc)
            self.output.emit("stderr", f"Unexpected error while starting “{self.tool.meta.name}”: {exc}\n")
            self._set_status(SessionStatus.ERROR)
            self.exited.emit(None, False)
            return
        self.started_at = now
        self._probe = probe
        if probe is not None:
            self._poll.start(self.poll_ms)
        self.answersChanged.emit(self)
        self._set_status(SessionStatus.RUNNING)

    def stop(self) -> None:
        """First call asks the script to stop; a second call while it is stopping forces termination."""
        if not self.is_running:
            return
        if self.runner.stop_requested:
            self.runner.force_stop()
        else:
            self.runner.request_stop()
            self._system("■ Stop requested (Ctrl+C sent)…")
        self.statusChanged.emit(self)               # the button turns into "Force stop"

    def send_input(self, text: str) -> bool:
        if not self.is_running:
            return False
        self.output.emit("input", text + "\n")      # a pipe has no echo: show what was sent
        return self.runner.write(text + "\n")

    def _on_finished(self, code: int, stop_requested: bool) -> None:
        self.exit_code = code
        self._end_answers()
        if stop_requested:
            status, mark = SessionStatus.STOPPED, "■ Stopped"
        elif code == 0:
            status, mark = SessionStatus.COMPLETED, "✔ Completed"
        else:
            status, mark = SessionStatus.FAILED, "✖ Failed"
        self._system(f"{mark} ({describe_exit(code)})")
        self._set_status(status)
        self.exited.emit(code, stop_requested)

    def set_workflow_folder(self, folder: str | Path) -> bool:
        """Change the working folder of a workflow (not while something of it runs). False when refused."""
        path = Path(folder)
        if not self.is_workflow or self.is_active or not path.is_dir():
            return False
        self.tool.path = path
        self.queue.stateChanged.emit()
        return True

    def kill_now(self) -> None:
        self.runner.kill_now()
        self._poll.stop()
        if self._probe is not None:
            self._probe.close()
            self._probe = None


class SessionManager(QObject):
    sessionAdded = Signal(object)
    sessionRemoved = Signal(object)
    sessionChanged = Signal(object)           # any status change of any session

    def __init__(self, registry: ToolRegistry, config: AppConfig, parent: QObject | None = None):
        super().__init__(parent)
        self._registry = registry
        self._config = config
        self.sessions: list[Session] = []     # tab order
        self._ordinals: dict[str, itertools.count] = {}
        self._clock = itertools.count(1)
        self._workflow_numbers = itertools.count(1)
        self._dying: set[Session] = set()     # removed while still running; kept until the process ends
        self.workflows_dir = config.path.parent / "workflows"   # the default working folder of a workflow

    # ------------------------------------------------------------------ lifecycle
    def create(self, tool_id: str) -> Session:
        tool = self._registry.get(tool_id)
        if tool is None:
            raise KeyError(tool_id)
        ordinal = next(self._ordinals.setdefault(tool_id, itertools.count(1)))
        return self._register(Session(tool, ordinal, self._config, registry=self._registry))

    def create_workflow(self, folder: str | Path | None = None) -> Session:
        """A workflow: the queue panel on a page of its own, without a tool. ``tool`` is a stand-in that only
        supplies the working folder (and the interpreter rule: a .venv in that folder wins, else the global Python)."""
        n = next(self._workflow_numbers)
        path = Path(folder) if folder else self.workflows_dir
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("Could not create %s: %s", path, exc)        # the run reports "folder does not exist"
        info = ToolInfo(id=f"workflow:{n}", path=path, meta=ToolMetadata(
            name=f"Workflow {n}", description="A queue without a tool", icon="workflow", color="#60A5FA"))
        return self._register(Session(info, 1, self._config, registry=self._registry, workflow=True))

    def _register(self, s: Session) -> Session:
        s.statusChanged.connect(self._on_status)
        s.queue.stateChanged.connect(lambda sess=s: self.sessionChanged.emit(sess))   # queue started / finished: refresh badges
        self.sessions.append(s)
        self.touch(s)
        self.sessionAdded.emit(s)
        return s

    def _on_status(self, session: Session) -> None:
        if session.is_running and not session.stopping:
            self.touch(session)                  # starting a run counts as activity for "most recent instance"
        self.sessionChanged.emit(session)

    def remove(self, session: Session) -> None:
        """Destroy an instance: its output and state are discarded; a live process is stopped."""
        if session not in self.sessions:
            return
        self.sessions.remove(session)
        session.queue.discard()                      # nothing queued may start any more; a live cell is stopped
        if session.is_active:
            self._dying.add(session)
            session.runner.finished.connect(lambda *_: self._reap(session))
            session.queue.settled.connect(lambda *_: self._reap(session))
            if session.is_running:
                session.runner.interrupt_grace = min(session.runner.interrupt_grace, 2.0)
                session.stop()
        else:
            session.deleteLater()
        self.sessionRemoved.emit(session)

    def _reap(self, session: Session) -> None:
        if session.is_active or session not in self._dying:      # main process or a queue cell is still alive
            return
        self._dying.discard(session)
        session.deleteLater()

    def shutdown(self) -> None:
        """Application exit: kill everything immediately."""
        for s in list(self.sessions) + list(self._dying):
            if s.is_running:
                s.kill_now()
            s.queue.kill_now()

    # ------------------------------------------------------------------ queries
    def get(self, session_id: str) -> Session | None:
        return next((s for s in self.sessions if s.id == session_id), None)

    def tool_sessions(self) -> list[Session]:
        """Instances of tools (the tabs of the workspace), in tab order."""
        return [s for s in self.sessions if not s.is_workflow]

    def workflows(self) -> list[Session]:
        return [s for s in self.sessions if s.is_workflow]

    def for_tool(self, tool_id: str) -> list[Session]:
        return [s for s in self.sessions if s.tool_id == tool_id]

    def most_recent(self, tool_id: str) -> Session | None:
        mine = self.for_tool(tool_id)
        return max(mine, key=lambda s: s.last_active) if mine else None

    def touch(self, session: Session) -> None:
        session.last_active = next(self._clock)

    def running(self) -> list[Session]:
        """Instances with something alive: the main process, or a queued cell that is executing."""
        return sorted((s for s in self.sessions if s.is_active), key=lambda s: s.started_at or datetime.min)

    def label(self, session: Session) -> str:
        tool = self._registry.get(session.tool_id)
        name = tool.meta.name if tool else session.tool.meta.name
        if tool is not None and tool.tag:
            name = f"{name} ({tool.tag})"                    # another tool has the same name: say which path
        return f"{name} #{session.ordinal}" if len(self.for_tool(session.tool_id)) > 1 else name

    def tool_status(self, tool_id: str) -> tuple[str | None, int]:
        """What a library card shows: running (with count) wins; otherwise the latest finished outcome."""
        mine = self.for_tool(tool_id)
        n = sum(1 for s in mine if s.is_active)
        if n:
            return SessionStatus.RUNNING.value, n
        done = [s for s in mine if s.status is not SessionStatus.IDLE]
        if not done:
            return None, 0
        return max(done, key=lambda s: s.last_active).status.value, 0
