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

from PySide6.QtCore import QObject, Signal

from .config import AppConfig
from .process_manager import ENTRY_NAME, LaunchError, ProcessRunner, describe_exit, resolve_interpreter
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

    def __init__(self, tool: ToolInfo, ordinal: int, config: AppConfig, parent: QObject | None = None):
        super().__init__(parent)
        self.id = uuid.uuid4().hex[:10]
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

    # ------------------------------------------------------------------ queries
    @property
    def tool_id(self) -> str:
        return self.tool.id

    @property
    def is_running(self) -> bool:
        return self.status is SessionStatus.RUNNING

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

    # ------------------------------------------------------------------ helpers
    def _system(self, text: str) -> None:
        self.output.emit("system", text + "\n")

    def _set_status(self, status: SessionStatus) -> None:
        self.status = status
        self.statusChanged.emit(self)

    # ------------------------------------------------------------------ run / stop
    def run(self) -> None:
        """Start a fresh process for this instance; output is appended below the previous runs."""
        if self.is_running:
            return
        self.run_count += 1
        now = datetime.now()
        if self.run_count > 1:
            self.output.emit("system", "\n")
        self._system(f"──────── Run {self.run_count} · {now:%Y-%m-%d %H:%M:%S} ────────")
        self.exit_code = None
        try:
            interp, err, source = resolve_interpreter(self.tool.path, self._config.resolved_global_python())
            if err is None and not self.tool.entry.is_file():
                err = (f"{ENTRY_NAME} was not found in\n{self.tool.path}\n"
                       f"The tool folder may have been moved, renamed or emptied since the toolbox started.")
            if err is not None:
                raise LaunchError(err)
            argv = [interp, "-u", ENTRY_NAME]
            self._system(f"> {interp} -u {ENTRY_NAME}   ({'tool .venv' if source == 'venv' else 'global Python'})")
            self._system(f"  in {self.tool.path}")
            self.runner.start(argv, str(self.tool.path))
        except LaunchError as exc:
            log.warning("Launch failed for %s: %s", self.tool.id, exc)
            self.detail = str(exc).splitlines()[0]
            self.output.emit("stderr", f"Could not start “{self.tool.meta.name}”:\n{exc}\n")
            self._set_status(SessionStatus.ERROR)
            return
        except Exception as exc:                    # never let one tool take the UI down
            log.exception("Unexpected failure while starting %s", self.tool.id)
            self.detail = str(exc)
            self.output.emit("stderr", f"Unexpected error while starting “{self.tool.meta.name}”: {exc}\n")
            self._set_status(SessionStatus.ERROR)
            return
        self.started_at = now
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
        if stop_requested:
            status, mark = SessionStatus.STOPPED, "■ Stopped"
        elif code == 0:
            status, mark = SessionStatus.COMPLETED, "✔ Completed"
        else:
            status, mark = SessionStatus.FAILED, "✖ Failed"
        self._system(f"{mark} ({describe_exit(code)})")
        self._set_status(status)

    def kill_now(self) -> None:
        self.runner.kill_now()


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
        self._dying: set[Session] = set()     # removed while still running; kept until the process ends

    # ------------------------------------------------------------------ lifecycle
    def create(self, tool_id: str) -> Session:
        tool = self._registry.get(tool_id)
        if tool is None:
            raise KeyError(tool_id)
        ordinal = next(self._ordinals.setdefault(tool_id, itertools.count(1)))
        s = Session(tool, ordinal, self._config)
        s.statusChanged.connect(self._on_status)
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
        if session.is_running:
            self._dying.add(session)
            session.runner.finished.connect(lambda *_: self._reap(session))
            session.runner.interrupt_grace = min(session.runner.interrupt_grace, 2.0)
            session.stop()
        else:
            session.deleteLater()
        self.sessionRemoved.emit(session)

    def _reap(self, session: Session) -> None:
        self._dying.discard(session)
        session.deleteLater()

    def shutdown(self) -> None:
        """Application exit: kill everything immediately."""
        for s in list(self.sessions) + list(self._dying):
            if s.is_running:
                s.kill_now()

    # ------------------------------------------------------------------ queries
    def get(self, session_id: str) -> Session | None:
        return next((s for s in self.sessions if s.id == session_id), None)

    def for_tool(self, tool_id: str) -> list[Session]:
        return [s for s in self.sessions if s.tool_id == tool_id]

    def most_recent(self, tool_id: str) -> Session | None:
        mine = self.for_tool(tool_id)
        return max(mine, key=lambda s: s.last_active) if mine else None

    def touch(self, session: Session) -> None:
        session.last_active = next(self._clock)

    def running(self) -> list[Session]:
        return sorted((s for s in self.sessions if s.is_running), key=lambda s: s.started_at or datetime.min)

    def label(self, session: Session) -> str:
        tool = self._registry.get(session.tool_id)
        name = tool.meta.name if tool else session.tool.meta.name
        return f"{name} #{session.ordinal}" if len(self.for_tool(session.tool_id)) > 1 else name

    def tool_status(self, tool_id: str) -> tuple[str | None, int]:
        """What a library card shows: running (with count) wins; otherwise the latest finished outcome."""
        mine = self.for_tool(tool_id)
        n = sum(1 for s in mine if s.is_running)
        if n:
            return SessionStatus.RUNNING.value, n
        done = [s for s in mine if s.status is not SessionStatus.IDLE]
        if not done:
            return None, 0
        return max(done, key=lambda s: s.last_active).status.value, 0
