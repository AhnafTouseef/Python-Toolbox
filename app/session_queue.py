"""The sequential execution queue of one Session.

Lifecycle
    cells are configured while the main process runs  ->  the main process EXITS (real exit event + code)  ->
    cells run strictly one at a time, in displayed order  ->  queue COMPLETED / STOPPED.

* Must Execute off: the queue starts only after a successful main exit (code 0, not stopped by the user) and
  stops at the first failed cell (the rest are marked Skipped).  If the main process failed the cells are
  simply kept (state "not run") for the next run.
* Must Execute on: the queue starts after any main exit, a failed cell never blocks later ones, and a cell
  that is *reliably* known to be blocked on stdin with no predefined answer left is stopped after 5 minutes.

Every cell gets its own ProcessRunner (own pipes, own stdin routing, own stop handling) and its own stdin
probe, so nothing of one cell can reach another cell or the main process.  The next cell is started only
after the previous runner reported ``finished`` (the process has exited and its output has been delivered);
a cell that was told to stop is waited for in exactly the same way.

Input handling (details in stdin_watch.py / stdin_probe/sitecustomize.py)
* Python processes (tool cells; ``python`` inside code cells) report blocking stdin reads, so answers are sent
  one per request, only while the process is really blocked, and "waiting for input" / the timeout are exact.
* Anything else cannot be observed.  Predefined answers are then sent when the output stops on an unfinished
  line (a prompt such as ``Continue? ``); no timeout is ever applied, because that could kill long work.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal

from .config import AppConfig
from .process_manager import ENTRY_NAME, IS_WINDOWS, LaunchError, ProcessRunner, describe_exit, plan_tool_launch
from .queue_model import (INPUT_TIMEOUT_REASON, CellKind, CellStatus, QueueCell, QueueState)
from .stdin_watch import StdinProbe

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- code cells
@dataclass
class ShellPlan:
    argv: list[str] | str
    description: str                 # what the user sees in the cell's output header
    script: Path | None = None       # temporary script file (Windows), inside the cell's probe folder


def build_shell_command(text: str, workdir: Path, windows: bool | None = None) -> ShellPlan:
    """How a code cell's text is executed.

    * POSIX:   ``/bin/sh -c <text>``  (multi-line text works as a shell script).
    * Windows: the text is written to a batch file and run by ``cmd.exe`` - the same as a .bat the user would
      double-click.  A file avoids cmd's notoriously fragile command-line quoting; ``chcp 65001`` makes cmd's
      own output UTF-8, which is what the toolbox decodes.
    """
    windows = IS_WINDOWS if windows is None else windows
    if not windows:
        sh = "/bin/sh" if os.path.exists("/bin/sh") else (shutil.which("sh") or "sh")
        return ShellPlan([sh, "-c", text], f"{sh} -c  (shell script, runs in the tool folder)")
    script = Path(workdir) / "cell.cmd"
    body = "\r\n".join(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    script.write_bytes(("@chcp 65001 >nul\r\n" + body + "\r\n").encode("utf-8"))
    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    # `/s` + doubled quotes is cmd's documented way to keep a quoted path (spaces, parentheses) intact.
    return ShellPlan(f'"{comspec}" /d /s /c ""{script}""', "cmd.exe /c  (batch script, runs in the tool folder)", script)


# --------------------------------------------------------------------------- one run of one cell
@dataclass
class _Run:
    cell: QueueCell
    runner: ProcessRunner
    probe: StdinProbe
    # input flow
    seen: dict[str, float] = field(default_factory=dict)        # wait request -> first seen (monotonic)
    answered: dict[str, float] = field(default_factory=dict)    # wait request -> when we answered it
    unsatisfied: dict[str, float] = field(default_factory=dict)  # wait request -> since when it needs real input
    # prompt heuristic (only for processes the probe cannot see)
    output_gen: int = 0
    gen_at_send: int = -1
    last_output: float = 0.0
    tail_open: bool = False
    exhausted_noted: bool = False


class SessionQueue(QObject):
    structureChanged = Signal()          # cells added / removed / moved
    stateChanged = Signal()              # overall state or Must Execute changed
    cellChanged = Signal(object)         # one cell: status / result / configuration
    cellOutput = Signal(object, str, str)  # cell, kind ("stdout" | "stderr" | "input" | "system"), text
    settled = Signal()                   # no process of this queue is alive (used when a session is torn down)

    # One merged stream (like the main terminal) keeps a traceback below the lines printed before it. With
    # False, stdout and stderr are captured separately (stderr shown in red) but two reader threads race, so
    # their relative order is not guaranteed.
    merge_output = True
    input_timeout_s = 300.0              # Must Execute: how long a cell may stay blocked on input nobody supplies
    poll_ms = 100                        # how often the stdin probe is looked at while a cell runs
    answer_debounce_s = 0.3              # a wait must last this long before it counts (reads that return at once don't)
    prompt_quiet_s = 0.6                 # undetectable processes: an unfinished line must stay unchanged this long

    def __init__(self, session, config: AppConfig, tool_lookup: Callable[[str], object | None] | None = None):
        super().__init__(session)
        self.session = session
        self._config = config
        self._lookup = tool_lookup or (lambda _id: None)
        self.cells: list[QueueCell] = []
        self.must_execute = False
        self.state = QueueState.EMPTY
        self._detail = ""
        self._final: tuple[QueueState, str] | None = None
        self._run: _Run | None = None
        self._user_stop = False
        self._advance_pending = False
        self.discarded = False
        self._poll = QTimer(self)
        self._poll.setInterval(self.poll_ms)
        self._poll.timeout.connect(self._tick)
        session.exited.connect(self._on_main_exited)
        session.statusChanged.connect(self._on_session_status)

    # ------------------------------------------------------------------ queries
    @property
    def executing(self) -> bool:
        return self.state is QueueState.EXECUTING and not self.discarded

    @property
    def busy(self) -> bool:
        """True while the queue is executing or one of its processes is still alive."""
        return self._run is not None or self.executing

    @property
    def current_cell(self) -> QueueCell | None:
        return self._run.cell if self._run else None

    def cell(self, cell_id: str) -> QueueCell | None:
        return next((c for c in self.cells if c.id == cell_id), None)

    def index_of(self, cell: QueueCell) -> int:
        return self.cells.index(cell)

    def pending_cells(self) -> list[QueueCell]:
        return [c for c in self.cells if c.is_pending]

    def tool_of(self, cell: QueueCell):
        """The registered tool a tool cell points to (None for code cells or when the tool disappeared)."""
        return self._lookup(cell.tool_id) if cell.kind is CellKind.TOOL else None

    def tool_name(self, cell: QueueCell) -> str | None:
        tool = self.tool_of(cell)
        return tool.meta.name if tool is not None else None

    def can_move(self, cell: QueueCell, delta: int) -> bool:
        if not cell.is_pending or cell not in self.cells:
            return False
        j = self.cells.index(cell) + delta
        return 0 <= j < len(self.cells) and self.cells[j].is_pending

    @property
    def state_text(self) -> str:
        s = self.state
        if s is QueueState.EMPTY:
            return "No cells yet"
        if s is QueueState.WAITING:
            if self.session.is_running:
                return "Waiting for the main process to finish"
            return "Waiting — starts when the main process finishes (it is not running now)"
        if s is QueueState.EXECUTING:
            cur = self.current_cell
            n = len(self.cells)
            if cur is not None:
                return f"Running cell {self.index_of(cur) + 1} of {n}"
            return "Starting the next cell…"
        return self._detail

    # ------------------------------------------------------------------ state helpers
    def _set_state(self, state: QueueState, detail: str = "") -> None:
        changed = (state, detail) != (self.state, self._detail)
        self.state, self._detail = state, detail
        if state in (QueueState.COMPLETED, QueueState.STOPPED):
            self._final = (state, detail)
        if changed:
            self.stateChanged.emit()

    def _refresh_idle_state(self) -> None:
        """Recompute the resting state after cells changed or the main process started."""
        if self.state is QueueState.EXECUTING:
            return
        pending = bool(self.pending_cells())
        if not self.cells:
            self._set_state(QueueState.EMPTY)
        elif pending:
            if self.state is QueueState.NOT_RUN and not self.session.is_running:
                return                                           # keep explaining why nothing ran
            self._set_state(QueueState.WAITING)
        elif self._final is not None:
            self._set_state(*self._final)
        else:
            self._set_state(QueueState.COMPLETED, "Completed")

    def _on_session_status(self, _session) -> None:
        if self.state is QueueState.NOT_RUN and self.session.is_running:
            self._refresh_idle_state()                           # main process restarted: armed again
        elif self.state is QueueState.WAITING:
            self.stateChanged.emit()                             # wording depends on "main is running"

    # ------------------------------------------------------------------ editing (GUI actions)
    def add_cell(self, kind: CellKind, *, label: str = "", command: str = "", tool_id: str = "",
                 answers_text: str = "") -> QueueCell:
        cell = QueueCell(kind=kind, label=label.strip(), command=command, tool_id=tool_id, answers_text=answers_text)
        self.cells.append(cell)
        self._refresh_idle_state()
        self.structureChanged.emit()
        return cell

    def update_cell(self, cell_id: str, **fields) -> bool:
        """Change a pending cell. Once a cell has started its configuration is frozen (returns False)."""
        cell = self.cell(cell_id)
        if cell is None or not cell.is_pending:
            return False
        for key in ("kind", "label", "command", "tool_id", "answers_text"):
            if key in fields:
                value = fields[key]
                setattr(cell, key, value.strip() if key == "label" else value)
        self.cellChanged.emit(cell)
        return True

    def remove_cell(self, cell_id: str) -> bool:
        cell = self.cell(cell_id)
        if cell is None or cell.is_active:
            return False
        self.cells.remove(cell)
        self._refresh_idle_state()
        self.structureChanged.emit()
        return True

    def move_cell(self, cell_id: str, delta: int) -> bool:
        """Swap a pending cell with its pending neighbour above (-1) or below (+1)."""
        cell = self.cell(cell_id)
        if cell is None or not cell.is_pending or delta not in (-1, 1):
            return False
        i = self.cells.index(cell)
        j = i + delta
        if j < 0 or j >= len(self.cells) or not self.cells[j].is_pending:
            return False
        self.cells[i], self.cells[j] = self.cells[j], self.cells[i]
        self.structureChanged.emit()
        return True

    def set_must_execute(self, value: bool) -> None:
        if value != self.must_execute:
            self.must_execute = value
            self.stateChanged.emit()

    # ------------------------------------------------------------------ main process hook
    def _on_main_exited(self, code, stopped: bool) -> None:
        """The real exit event of the main process (code is None when it could not even be launched)."""
        if self.discarded or self.state is QueueState.EXECUTING:
            return
        if not self.pending_cells():
            return
        success = code == 0 and not stopped
        if success or self.must_execute:
            self._begin()
        else:
            why = "stopped by you" if stopped else ("could not be started" if code is None else f"failed ({describe_exit(code)})")
            self._set_state(QueueState.NOT_RUN,
                            f"Not run — the main process {why} and Must Execute is off. The cells are kept.")

    def _begin(self) -> None:
        self._user_stop = False
        self._set_state(QueueState.EXECUTING)
        self._schedule_advance()

    # ------------------------------------------------------------------ the sequential loop
    def _schedule_advance(self) -> None:
        """Start the next cell from a fresh event-loop turn (never from inside a process-event handler)."""
        if self._advance_pending:
            return
        self._advance_pending = True
        QTimer.singleShot(0, self._run_next)

    def _run_next(self) -> None:
        self._advance_pending = False
        if self.discarded or self.state is not QueueState.EXECUTING or self._run is not None:
            return                                               # duplicate trigger / shutting down: do nothing
        if self._user_stop:
            self._mark_rest(CellStatus.CANCELLED, "queue stopped by you")
            self._finish(QueueState.STOPPED, "Stopped by you")
            return
        cell = next((c for c in self.cells if c.is_pending), None)
        if cell is None:
            failed = sum(1 for c in self.cells if c.status in (CellStatus.FAILED, CellStatus.CANCELLED))
            self._finish(QueueState.COMPLETED,
                         "Completed" if not failed else f"Completed — {failed} cell{'s' if failed != 1 else ''} failed")
            return
        self._start_cell(cell)

    def _finish(self, state: QueueState, detail: str) -> None:
        self._set_state(state, detail)
        self.structureChanged.emit()
        self.settled.emit()

    def _mark_rest(self, status: CellStatus, reason: str) -> None:
        for c in self.cells:
            if c.is_pending:
                c.status, c.reason = status, reason
                c.finished_at = datetime.now()
                self.cellChanged.emit(c)

    # ------------------------------------------------------------------ starting a cell
    def _emit(self, cell: QueueCell, kind: str, text: str) -> None:
        cell.append_output(kind, text)
        self.cellOutput.emit(cell, kind, text)

    def _system(self, cell: QueueCell, text: str) -> None:
        self._emit(cell, "system", text + "\n")

    def _plan(self, cell: QueueCell, workdir: Path) -> tuple[list[str] | str, str, list[str]]:
        """(argv, cwd, header lines) or LaunchError."""
        if cell.kind is CellKind.TOOL:
            tool = self._lookup(cell.tool_id)
            if tool is None:
                raise LaunchError(f"The tool “{cell.tool_id}” is no longer available.")
            argv, interp, source = plan_tool_launch(tool.path, tool.entry, self._config.resolved_global_python())
            return argv, str(tool.path), [f"> {interp} -u {ENTRY_NAME}   ({'tool .venv' if source == 'venv' else 'global Python'})",
                                          f"  in {tool.path}"]
        if not cell.command.strip():
            raise LaunchError("This code cell has no command.")
        cwd = str(self.session.tool.path)
        try:
            plan = build_shell_command(cell.command, workdir)
        except OSError as exc:
            raise LaunchError(f"Could not prepare the command: {exc}") from None
        return plan.argv, cwd, [f"> {plan.description}", f"  in {cwd}"]

    def _start_cell(self, cell: QueueCell) -> None:
        index = self.index_of(cell) + 1
        now = datetime.now()
        cell.status = CellStatus.RUNNING
        cell.started_at = now
        cell.detection = "none"
        kind_name = "Tool" if cell.kind is CellKind.TOOL else "Code"
        self._system(cell, f"──────── Cell {index} · {kind_name} · {now:%Y-%m-%d %H:%M:%S} ────────")
        probe = StdinProbe()
        runner = ProcessRunner(self)
        runner.merge_stderr = self.merge_output
        run = _Run(cell, runner, probe, last_output=time.monotonic())
        try:
            argv, cwd, header = self._plan(cell, probe.dir)
            for line in header:
                self._system(cell, line)
            runner.output.connect(lambda kind, text, r=run: self._on_output(r, kind, text))
            runner.finished.connect(lambda code, stopped, r=run: self._on_cell_finished(r, code, stopped))
            runner.start(argv, cwd, probe.env())
        except LaunchError as exc:
            log.warning("Queue cell could not start: %s", exc)
            self._emit(cell, "stderr", f"Could not start this cell:\n{exc}\n")
            probe.close()
            runner.deleteLater()
            self._end_cell(cell, CellStatus.FAILED, f"could not start: {str(exc).splitlines()[0]}", None)
            return
        except Exception as exc:                                  # never let one cell take the queue down
            log.exception("Unexpected failure while starting a queue cell")
            self._emit(cell, "stderr", f"Unexpected error while starting this cell: {exc}\n")
            probe.close()
            runner.deleteLater()
            self._end_cell(cell, CellStatus.FAILED, f"could not start: {exc}", None)
            return
        cell.pid = runner.pid
        self._run = run
        self.cellChanged.emit(cell)
        self.stateChanged.emit()
        self._poll.start(self.poll_ms)

    # ------------------------------------------------------------------ output + interactive input
    def _on_output(self, run: _Run, kind: str, text: str) -> None:
        if run is not self._run:
            return
        self._emit(run.cell, kind, text)
        run.output_gen += 1
        run.last_output = time.monotonic()
        if kind == "stdout":
            run.tail_open = not text.endswith(("\n", "\r"))

    def send_input(self, cell: QueueCell, text: str) -> bool:
        """The user typed a line for the running cell (the application's normal interactive input)."""
        run = self._run
        if run is None or run.cell is not cell or run.runner.stop_requested:
            return False
        self._emit(cell, "input", text + "\n")
        return run.runner.write(text + "\n")

    def _send_next_answer(self, run: _Run) -> bool:
        cell = run.cell
        answers = cell.answers
        i = len(cell.answers_sent)
        if i >= len(answers):
            return False
        answer = answers[i]
        cell.answers_sent.append(answer)
        run.gen_at_send = run.output_gen
        self._emit(cell, "input", answer + "\n")
        run.runner.write(answer + "\n")
        self.cellChanged.emit(cell)
        return True

    # ------------------------------------------------------------------ polling: waits, answers, timeout
    def _tick(self) -> None:
        run = self._run
        if run is None:
            self._poll.stop()
            return
        cell, runner = run.cell, run.runner
        if runner.stop_requested:
            return                                                # already stopping: no more answers, no more timeouts
        now = time.monotonic()
        snap = run.probe.scan()
        waiting = False
        if snap.active:
            if cell.detection != "python":
                cell.detection = "python"
                self.cellChanged.emit(cell)
            waiting = self._probe_flow(run, snap.waits, now)
        else:
            self._prompt_flow(run, now)
        status = CellStatus.WAITING if waiting else CellStatus.RUNNING
        if cell.status is not status:
            cell.status = status
            self.cellChanged.emit(cell)
        if waiting and self.must_execute and run.unsatisfied:
            if now - min(run.unsatisfied.values()) >= self.input_timeout_s:
                self._time_out(run)

    def _probe_flow(self, run: _Run, waits: frozenset[str], now: float) -> bool:
        """Reliable path. Returns True while a stdin request exists that no predefined answer can satisfy."""
        for table in (run.seen, run.answered, run.unsatisfied):
            for key in [k for k in table if k not in waits]:
                del table[key]                                    # that read returned
        for key in waits:
            run.seen.setdefault(key, now)
        for key, first in sorted(run.seen.items(), key=lambda kv: kv[1]):
            if now - first < self.answer_debounce_s:
                continue                                          # a read that returns at once is not a wait
            if key not in run.answered:
                if self._send_next_answer(run):
                    run.answered[key] = now
                else:
                    run.unsatisfied.setdefault(key, now)
            elif now - run.answered[key] >= self.answer_debounce_s:
                run.unsatisfied.setdefault(key, now)              # still blocked after its answer: needs more
        if run.unsatisfied and not run.exhausted_noted:
            run.exhausted_noted = True
            tail = ("It is stopped after 5 minutes without input (Must Execute)." if self.must_execute
                    else "It keeps waiting (Must Execute is off).")
            self._system(run.cell, "(The cell is waiting for input that no predefined answer can supply — type it in "
                                   "the box below. " + tail + ")")
        return bool(run.unsatisfied)

    def _prompt_flow(self, run: _Run, now: float) -> None:
        """Fallback for processes the probe cannot see: answer a prompt that ends the output without a newline."""
        if run.tail_open and now - run.last_output >= self.prompt_quiet_s and run.output_gen > run.gen_at_send:
            self._send_next_answer(run)

    def _time_out(self, run: _Run) -> None:
        cell = run.cell
        cell.timed_out = True
        self._system(cell, "✖ No input for 5 minutes — stopping this cell.")
        run.runner.request_stop()
        self.cellChanged.emit(cell)

    # ------------------------------------------------------------------ finishing a cell
    def _on_cell_finished(self, run: _Run, code, stopped: bool) -> None:
        if run is not self._run:
            return                                                # duplicate / stale event: never advances twice
        self._poll.stop()
        self._run = None
        cell = run.cell
        run.probe.close()
        run.runner.deleteLater()
        remaining = cell.answers[len(cell.answers_sent):]
        if remaining:
            cell.unused_answers = remaining
            self._system(cell, f"(Not used: {len(remaining)} predefined answer{'s' if len(remaining) != 1 else ''} — "
                               + ", ".join(remaining) + ")")
        if cell.timed_out:
            status, reason, mark = CellStatus.FAILED, INPUT_TIMEOUT_REASON, f"✖ Failed — {INPUT_TIMEOUT_REASON}"
        elif self._user_stop or stopped:
            status, reason, mark = CellStatus.CANCELLED, "stopped by you", "■ Cancelled"
        elif code == 0:
            status, reason, mark = CellStatus.COMPLETED, "", f"✔ Completed ({describe_exit(code)})"
        else:
            reason = describe_exit(code)
            status, mark = CellStatus.FAILED, f"✖ Failed ({reason})"
        self._system(cell, mark)
        self._end_cell(cell, status, reason, code)

    def _end_cell(self, cell: QueueCell, status: CellStatus, reason: str, code) -> None:
        """Record the result, then decide whether the queue goes on (policy) and schedule the next cell."""
        cell.status, cell.reason, cell.exit_code = status, reason, code
        cell.finished_at = datetime.now()
        self.cellChanged.emit(cell)
        self.stateChanged.emit()
        if self.discarded:
            self.settled.emit()
            return
        if self._user_stop:
            self._schedule_advance()                              # _run_next cancels the rest and finishes
            return
        if status in (CellStatus.FAILED, CellStatus.CANCELLED) and not self.must_execute:
            n = self.index_of(cell) + 1
            self._mark_rest(CellStatus.SKIPPED, f"an earlier cell failed (cell {n})")
            self._finish(QueueState.STOPPED, f"Stopped — cell {n} failed ({reason or status.value}). "
                                             f"Later cells were skipped (Must Execute is off).")
            return
        self._schedule_advance()

    # ------------------------------------------------------------------ stopping
    def stop(self) -> None:
        """User: stop the running cell and cancel everything that has not started. A second call forces it."""
        if not self.executing:
            return
        self._user_stop = True
        run = self._run
        if run is None:
            self._schedule_advance()
            return
        if run.runner.stop_requested:
            run.runner.force_stop()
        else:
            self._system(run.cell, "■ Stop requested (Ctrl+C sent)…")
            run.runner.request_stop()
        self.stateChanged.emit()

    @property
    def stopping(self) -> bool:
        return self._run is not None and self._user_stop

    def discard(self) -> None:
        """The session is being closed: nothing may start any more; a live cell is stopped."""
        self.discarded = True
        self._poll.stop()
        run = self._run
        if run is None:
            self.settled.emit()
            return
        run.runner.interrupt_grace = min(run.runner.interrupt_grace, 2.0)
        run.runner.request_stop()

    def kill_now(self) -> None:
        """Application exit: kill the running cell's process tree immediately."""
        self.discarded = True
        self._poll.stop()
        if self._run is not None:
            self._run.runner.kill_now()
