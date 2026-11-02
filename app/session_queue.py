"""The sequential execution queue of one Session.

Lifecycle
    cells are configured while the main process runs  ->  the main process EXITS (real exit event + code)  ->
    cells run strictly one at a time, in displayed order  ->  queue COMPLETED / STOPPED.

* Must Execute off: the queue starts only after a successful main exit (code 0, not stopped by the user) and
  stops at the first failed cell (the rest are marked Skipped).  If the main process failed the cells are
  simply kept (state "not run") for the next run.
* Must Execute on: the queue starts after any main exit, a failed cell never blocks later ones, and a cell
  that is *reliably* known to be blocked on stdin with no predefined answer left is stopped after 5 minutes.

Every tool cell and shell cell gets its own ProcessRunner (own pipes, own stdin routing, own stop handling) and
its own stdin probe, so nothing of one cell can reach another cell or the main process.  The next cell is started
only after the previous runner reported ``finished`` (the process has exited and its output has been delivered);
a cell that was told to stop is waited for in exactly the same way.

Python code cells are the exception on purpose: all of them run in ONE Python process per run (cell_kernel.py),
so a variable defined by one cell is available to the later ones.  That process is started by the first Python
code cell of a run and ended when the run ends - completed, stopped or failed - so no variable ever survives
into the next run.  Tool cells and shell cells are separate programs and cannot see those variables.

Input handling (details in stdin_watch.py / stdin_probe/sitecustomize.py)
* Python processes (tool cells; ``python`` inside code cells) report blocking stdin reads, so answers are sent
  one per request, only while the process is really blocked, and "waiting for input" / the timeout are exact.
* Anything else cannot be observed.  Predefined answers are then sent when the output stops on an unfinished
  line (a prompt such as ``Continue? ``); no timeout is ever applied, because that could kill long work.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal

from . import cell_kernel
from .config import AppConfig
from .process_manager import (ENTRY_NAME, IS_WINDOWS, LaunchError, ProcessRunner, describe_exit, plan_tool_launch,
                              resolve_interpreter)
from .queue_model import (INPUT_TIMEOUT_REASON, LANG_PYTHON, LANG_SHELL, CellKind, CellStatus, QueueCell, QueueState)
from .stdin_watch import StdinProbe, advance_waits

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


# Starts the shared session: runs cell_kernel.py (which must not become importable by user code, hence exec instead
# of a path on sys.path) with the tool folder as the import root (``-c`` puts "" first on sys.path).
KERNEL_PATH = Path(cell_kernel.__file__).resolve()
_KERNEL_BOOTSTRAP = ("import sys;p=sys.argv[1];g={'__name__':'toolbox_cell_kernel','__file__':p};"
                     "exec(compile(open(p,encoding='utf-8').read(),p,'exec'),g);g['main'](sys.argv[2],sys.argv[3])")


def is_python_cell(cell: QueueCell) -> bool:
    return cell.kind is CellKind.CODE and cell.language == LANG_PYTHON


def _partial_marker_len(text: str, marker: str) -> int:
    """Length of the longest tail of ``text`` that is the beginning of ``marker`` (it may be completed by the next chunk)."""
    for k in range(min(len(marker) - 1, len(text)), 0, -1):
        if text.endswith(marker[:k]):
            return k
    return 0


# --------------------------------------------------------------------------- one run of one cell
@dataclass
class _Kernel:
    """The shared Python process of one queue run (see cell_kernel.py)."""
    runner: ProcessRunner
    probe: StdinProbe                     # its folder is also the control folder (cell files, quit file)
    token: str
    number: int = 0                       # cells handed to it so far
    current: "_Run | None" = None         # the cell being executed in it right now
    last_cell: QueueCell | None = None    # where late output (a background thread) is shown
    pending: str = ""                     # output held back because it may be the start of a completion marker
    ending: bool = False                  # the queue asked it to quit
    alive: bool = True

    @property
    def marker(self) -> str:
        return f"{cell_kernel.MARK_PREFIX}{self.token}:"

    def feed(self, text: str, final: bool = False) -> list[tuple[str, object]]:
        """Split process output into ("text", str) and ("done", (cell number, exit code)) events.

        The completion marker is removed from the text; a marker (or the start of one) split across two chunks
        is held back until it is complete.  ``final``: the process is gone, release whatever is still held."""
        events: list[tuple[str, object]] = []
        buf, self.pending = self.pending + text, ""
        marker = self.marker
        while buf:
            i = buf.find(marker)
            if i < 0:
                keep = 0 if final else _partial_marker_len(buf, marker)
                if len(buf) > keep:
                    events.append(("text", buf[:len(buf) - keep]))
                self.pending = buf[len(buf) - keep:] if keep else ""
                return events
            if i:
                events.append(("text", buf[:i]))
            j = buf.find(cell_kernel.MARK_END, i + len(marker))
            if j < 0:                                                   # marker not complete yet
                if final:
                    events.append(("text", buf[i:]))
                else:
                    self.pending = buf[i:]
                return events
            try:
                number, code = (int(part) for part in buf[i + len(marker):j].split(":"))
                events.append(("done", (number, code)))
            except ValueError:
                pass
            buf = buf[j + 1:]
        return events


@dataclass
class _Run:
    cell: QueueCell
    runner: ProcessRunner
    probe: StdinProbe
    kernel: _Kernel | None = None        # set for a Python code cell: it runs in the shared session
    number: int = 0                      # ... as this numbered cell of it
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
    kernel_exit_grace_s = 3.0            # how long the shared session may take to quit before it is stopped

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
        self._scope: set[str] | None = None        # ids of the cells of a manual run (None: every pending cell)
        self._kernel: _Kernel | None = None        # the shared Python session of the run in progress
        self._finishing: tuple[QueueState, str] | None = None   # the run is over; waiting for the session to exit
        self._kernel_lost = False                  # the session died unexpectedly earlier in this run
        self._settled_sent = False
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
        return self._run is not None or self.executing or self._kernel is not None

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

    @property
    def can_start(self) -> bool:
        """A run can be started by hand: nothing of this instance is running and there is something to run."""
        return (not self.discarded and not self.busy and not self.session.is_active and bool(self.pending_cells()))

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
        if s is QueueState.NOT_RUN:
            return self._detail
        if s is QueueState.WAITING:
            if self.session.is_workflow:
                return "Ready — press Run now, or a cell's play button"
            if self.session.is_running:
                return "Waiting for the main process to finish"
            return "Waiting — starts when the main process finishes (it is not running now)"
        if s is QueueState.EXECUTING:
            cur = self.current_cell
            n = len(self.cells)
            if cur is not None:
                return f"Running cell {self.index_of(cur) + 1} of {n}"
            return "Starting the next cell…"
        return f"Last run: {self._detail}. The cells are ready for the next run."

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
        if not self.cells:
            self._set_state(QueueState.EMPTY)
        elif self.state in (QueueState.NOT_RUN, QueueState.COMPLETED, QueueState.STOPPED):
            if self.session.is_running:
                self._set_state(QueueState.WAITING)              # a new main run: armed again
            # otherwise keep showing how the last run went; the cells are pending (ready) anyway
        else:
            self._set_state(QueueState.WAITING)

    def _on_session_status(self, _session) -> None:
        if self.state in (QueueState.NOT_RUN, QueueState.COMPLETED, QueueState.STOPPED) and self.session.is_running:
            self._refresh_idle_state()
        else:
            self.stateChanged.emit()                             # wording / "can run now" depend on the main process

    # ------------------------------------------------------------------ editing (GUI actions)
    def add_cell(self, kind: CellKind, *, label: str = "", command: str = "", tool_id: str = "",
                 answers_text: str = "", language: str = LANG_PYTHON, index: int | None = None) -> QueueCell:
        """Add a cell at ``index`` (default: the end). Cells that are running are never displaced."""
        cell = QueueCell(kind=kind, label=label.strip(), command=command, tool_id=tool_id, answers_text=answers_text,
                         language=language if language in (LANG_PYTHON, LANG_SHELL) else LANG_PYTHON)
        if index is None or index >= len(self.cells):
            self.cells.append(cell)
        else:
            self.cells.insert(max(0, index), cell)
        self._refresh_idle_state()
        self.structureChanged.emit()
        return cell

    def add_cells(self, specs: list[dict]) -> list[QueueCell]:
        """Append several cells at once (a preset): exactly as if they were added one by one, announced once."""
        added = []
        for spec in specs:
            lang = spec.get("language", LANG_PYTHON)
            cell = QueueCell(kind=spec["kind"], label=spec.get("label", "").strip(), command=spec.get("command", ""),
                             tool_id=spec.get("tool_id", ""), answers_text=spec.get("answers_text", ""),
                             language=lang if lang in (LANG_PYTHON, LANG_SHELL) else LANG_PYTHON)
            self.cells.append(cell)
            added.append(cell)
        if added:
            self._refresh_idle_state()
            self.structureChanged.emit()
        return added

    def duplicate_cell(self, cell_id: str) -> QueueCell | None:
        """A copy of the cell's configuration (never its results or output), inserted right below it."""
        src = self.cell(cell_id)
        if src is None:
            return None
        copy = QueueCell(kind=src.kind, label=src.label, command=src.command, language=src.language,
                         tool_id=src.tool_id, answers_text=src.answers_text)
        self.cells.insert(self.cells.index(src) + 1, copy)
        self._refresh_idle_state()
        self.structureChanged.emit()
        return copy

    def update_cell(self, cell_id: str, **fields) -> bool:
        """Change a pending cell. Once a cell has started its configuration is frozen (returns False)."""
        cell = self.cell(cell_id)
        if cell is None or not cell.is_pending:
            return False
        for key in ("kind", "label", "command", "tool_id", "answers_text", "language"):
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

    def _begin(self, scope: set[str] | None = None) -> None:
        self._user_stop = False
        self._kernel_lost = False
        self._scope = scope
        self._set_state(QueueState.EXECUTING)
        self._schedule_advance()

    # ------------------------------------------------------------------ starting by hand
    def run_now(self) -> bool:
        """Run every pending cell now, without waiting for the main process (which is not running)."""
        if not self.can_start:
            return False
        self._begin()
        return True

    def run_cell(self, cell_id: str) -> bool:
        """Run only this cell, now."""
        cell = self.cell(cell_id)
        if cell is None or not cell.is_pending or not self.can_start:
            return False
        self._begin({cell.id})
        return True

    def run_from(self, cell_id: str) -> bool:
        """Run this cell and every pending cell below it, now."""
        cell = self.cell(cell_id)
        if cell is None or not cell.is_pending or not self.can_start:
            return False
        i = self.cells.index(cell)
        self._begin({c.id for c in self.cells[i:] if c.is_pending})
        return True

    def _in_scope(self, cell: QueueCell) -> bool:
        return cell.is_pending and (self._scope is None or cell.id in self._scope)

    # ------------------------------------------------------------------ the sequential loop
    def _schedule_advance(self) -> None:
        """Start the next cell from a fresh event-loop turn (never from inside a process-event handler)."""
        if self._advance_pending:
            return
        self._advance_pending = True
        QTimer.singleShot(0, self._run_next)

    def _run_next(self) -> None:
        self._advance_pending = False
        if (self.discarded or self.state is not QueueState.EXECUTING or self._run is not None
                or self._finishing is not None):
            return                                               # duplicate trigger / shutting down: do nothing
        if self._user_stop:
            self._mark_rest(CellStatus.CANCELLED, "queue stopped by you")
            self._finish(QueueState.STOPPED, "Stopped by you")
            return
        cell = next((c for c in self.cells if self._in_scope(c)), None)
        if cell is None:
            failed = sum(1 for c in self.cells if c.status in (CellStatus.FAILED, CellStatus.CANCELLED))
            self._finish(QueueState.COMPLETED,
                         "Completed" if not failed else f"Completed — {failed} cell{'s' if failed != 1 else ''} failed")
            return
        self._start_cell(cell)

    def _finish(self, state: QueueState, detail: str) -> None:
        """The run is over. Its Python session (and with it every variable) ends first; the queue is then
        reported as finished, so "finished" always means that no process of the run is alive any more."""
        if self._kernel is not None:
            self._finishing = (state, detail)
            self._end_kernel()
            return
        self._complete(state, detail)

    def _complete(self, state: QueueState, detail: str) -> None:
        self._finishing = None
        for c in self.cells:
            c.rearm()                                            # editable again; the result stays readable
        self._scope = None
        self._set_state(state, detail)
        self.structureChanged.emit()
        self.settled.emit()

    def _mark_rest(self, status: CellStatus, reason: str) -> None:
        for c in self.cells:
            if self._in_scope(c):
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
        """(argv, cwd, header lines) of a tool cell or a shell cell, or LaunchError. (Python code cells run in
        the shared session instead, see _start_in_kernel.)"""
        if cell.kind is CellKind.TOOL:
            tool = self._lookup(cell.tool_id)
            if tool is None:
                raise LaunchError(f"The tool “{cell.tool_id}” is no longer available.")
            argv, interp, source = plan_tool_launch(tool.path, tool.entry, self._config.resolved_global_python())
            return argv, str(tool.path), [f"> {interp} -u {ENTRY_NAME}   ({'tool .venv' if source == 'venv' else 'global Python'})",
                                          f"  in {tool.path}"]
        cwd = str(self.session.tool.path)
        try:
            plan = build_shell_command(cell.command, workdir)
        except OSError as exc:
            raise LaunchError(f"Could not prepare the command: {exc}") from None
        return plan.argv, cwd, [f"> {plan.description}", f"  in {cwd}"]

    def _start_cell(self, cell: QueueCell) -> None:
        index = self.index_of(cell) + 1
        now = datetime.now()
        cell.reset_run_state()                                   # forget the previous run's output / result
        cell.status = CellStatus.RUNNING
        cell.started_at = now
        cell.detection = "none"
        self.cellChanged.emit(cell)                              # views drop the previous run's output now
        kind_name = "Tool" if cell.kind is CellKind.TOOL else "Code"
        self._system(cell, f"──────── Cell {index} · {kind_name} · {now:%Y-%m-%d %H:%M:%S} ────────")
        if cell.kind is CellKind.CODE and not cell.command.strip():
            self._system(cell, "(Empty cell — nothing to run.)")
            self._end_cell(cell, CellStatus.COMPLETED, "", 0)
            return
        if is_python_cell(cell):
            self._start_in_kernel(cell)
            return
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

    # ------------------------------------------------------------------ the shared Python session
    def _start_in_kernel(self, cell: QueueCell) -> None:
        """Run a Python code cell in the session of this run (started by the first such cell)."""
        try:
            # The session uses the interpreter of the tool this queue belongs to (its .venv, else the global one).
            interp, err, source = resolve_interpreter(self.session.tool.path, self._config.resolved_global_python())
            if err is not None:
                raise LaunchError(err)
            cwd = str(self.session.tool.path)
            self._system(cell, f"> {interp} -u   (Python code, {'tool .venv' if source == 'venv' else 'global Python'})")
            self._system(cell, f"  in {cwd}")
            self._system(cell, "  variables are shared with the other Python code cells of this run and are gone "
                               "when the run ends")
            if self._kernel_lost and self._kernel is None:
                self._system(cell, "  (the Python session ended unexpectedly earlier in this run: this is a fresh "
                                   "one, earlier variables are gone)")
            kernel = self._kernel or self._launch_kernel(interp, cwd)
            number = kernel.number + 1
            control = Path(kernel.probe.dir)
            try:                                  # written under another name first, so it is never read half written
                (control / f"run-{number}.tmp").write_text(cell.command.replace("\r\n", "\n"), encoding="utf-8",
                                                           newline="\n")
                os.replace(control / f"run-{number}.tmp", control / (cell_kernel.RUN_FILE % number))
            except OSError as exc:
                raise LaunchError(f"Could not prepare the code: {exc}") from None
        except LaunchError as exc:
            log.warning("Python cell could not start: %s", exc)
            self._emit(cell, "stderr", f"Could not start this cell:\n{exc}\n")
            self._end_cell(cell, CellStatus.FAILED, f"could not start: {str(exc).splitlines()[0]}", None)
            return
        except Exception as exc:                                  # never let one cell take the queue down
            log.exception("Unexpected failure while starting a Python cell")
            self._emit(cell, "stderr", f"Unexpected error while starting this cell: {exc}\n")
            self._end_cell(cell, CellStatus.FAILED, f"could not start: {exc}", None)
            return
        kernel.number = number
        run = _Run(cell, kernel.runner, kernel.probe, kernel=kernel, number=number, last_output=time.monotonic())
        kernel.current = run
        kernel.last_cell = cell
        cell.pid = kernel.runner.pid
        self._run = run
        self.cellChanged.emit(cell)
        self.stateChanged.emit()
        self._poll.start(self.poll_ms)

    def _launch_kernel(self, interp: str, cwd: str) -> _Kernel:
        probe = StdinProbe()
        runner = ProcessRunner(self)
        runner.merge_stderr = self.merge_output
        kernel = _Kernel(runner, probe, secrets.token_hex(6))
        runner.output.connect(lambda kind, text, k=kernel: self._on_kernel_output(k, kind, text))
        runner.finished.connect(lambda code, stopped, k=kernel: self._on_kernel_finished(k, code, stopped))
        try:
            runner.start([interp, "-u", "-c", _KERNEL_BOOTSTRAP, str(KERNEL_PATH), str(probe.dir), kernel.token],
                         cwd, probe.env())
        except Exception:
            probe.close()
            runner.deleteLater()
            raise
        self._kernel = kernel
        self._kernel_lost = False
        return kernel

    def _on_kernel_output(self, kernel: _Kernel, kind: str, text: str) -> None:
        events = kernel.feed(text) if kind == "stdout" else [("text", text)]
        for what, value in events:
            if what == "text":
                self._kernel_text(kernel, kind, value)
            else:
                self._on_kernel_cell_done(kernel, *value)

    def _kernel_text(self, kernel: _Kernel, kind: str, text: str) -> None:
        run = kernel.current
        if run is not None and run is self._run:
            self._on_output(run, kind, text)
        elif kernel.last_cell is not None:                        # e.g. a background thread printing after its cell
            self._emit(kernel.last_cell, kind, text)

    def _on_kernel_cell_done(self, kernel: _Kernel, number: int, code: int) -> None:
        run = kernel.current
        if run is None or run is not self._run or run.number != number:
            return                                                # stale / duplicate: never concludes a cell twice
        kernel.current = None
        self._conclude(run, code, False)

    def _on_kernel_finished(self, kernel: _Kernel, code, stopped: bool) -> None:
        """The session process is gone: at the end of a run (expected), or because something killed it."""
        if not kernel.alive:
            return
        kernel.alive = False
        if self._kernel is kernel:
            self._kernel = None
        for what, value in kernel.feed("", final=True):           # release output still held back
            if what == "text":
                self._kernel_text(kernel, "stdout", value)
        run, kernel.current = kernel.current, None
        kernel.probe.close()
        kernel.runner.deleteLater()
        unexpected = not kernel.ending
        if unexpected and not self.discarded:
            self._kernel_lost = True
        if run is not None and run is self._run:
            cell = run.cell
            if not (self._user_stop or cell.timed_out or self.discarded):
                self._system(cell, "(The Python session ended with this cell — its variables are gone.)")
            self._conclude(run, code, stopped)                    # the cell's result; may advance or finish the queue
        elif unexpected and not (self._user_stop or self.discarded) and kernel.last_cell is not None:
            self._system(kernel.last_cell, "(The Python session ended unexpectedly — its variables are gone.)")
        if self._finishing is not None:
            state, detail = self._finishing
            self._finishing = None
            if not self.discarded:
                self._complete(state, detail)
        if self.discarded:
            self._maybe_settled()

    def _end_kernel(self) -> None:
        """Ask the session to quit; if it has not done so shortly (a non-daemon thread of user code, say) stop it."""
        kernel = self._kernel
        if kernel is None or kernel.ending:
            return
        kernel.ending = True
        try:
            (Path(kernel.probe.dir) / cell_kernel.QUIT_FILE).write_bytes(b"")
        except OSError:
            kernel.runner.force_stop()
            return
        QTimer.singleShot(int(self.kernel_exit_grace_s * 1000),
                          lambda k=kernel: k.runner.force_stop() if k.alive else None)

    def _live_runners(self) -> list[ProcessRunner]:
        runners = []
        if self._run is not None:
            runners.append(self._run.runner)
        if self._kernel is not None and self._kernel.alive and self._kernel.runner not in runners:
            runners.append(self._kernel.runner)
        return runners

    def _maybe_settled(self) -> None:
        """After discard(): announce (once) that nothing of this queue is alive any more."""
        if self.discarded and self._run is None and self._kernel is None and not self._settled_sent:
            self._settled_sent = True
            self.settled.emit()

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
        advance_waits(run.seen, run.answered, run.unsatisfied, waits, now, self.answer_debounce_s,
                      lambda: self._send_next_answer(run))
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
        if run.kernel is not None:
            # A read blocked on a pipe cannot be interrupted reliably on every platform, so the session is ended.
            self._system(cell, "✖ No input for 5 minutes — stopping this cell (its Python session ends, variables "
                               "are lost).")
            run.runner.force_stop()
        else:
            self._system(cell, "✖ No input for 5 minutes — stopping this cell.")
            run.runner.request_stop()
        self.cellChanged.emit(cell)

    # ------------------------------------------------------------------ finishing a cell
    def _on_cell_finished(self, run: _Run, code, stopped: bool) -> None:
        if run is not self._run:
            return                                                # duplicate / stale event: never advances twice
        run.probe.close()
        run.runner.deleteLater()
        self._conclude(run, code, stopped)

    def _conclude(self, run: _Run, code, stopped: bool) -> None:
        """Record how a cell ended (its process, or for a Python cell its turn in the shared session, is over)."""
        self._poll.stop()
        self._run = None
        cell = run.cell
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
            self._maybe_settled()
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
            kernel = self._kernel
            if kernel is not None and kernel.ending:
                kernel.runner.force_stop()                        # the run is over, only its session is still quitting
            else:
                self._schedule_advance()
            return
        if run.runner.stop_requested:
            run.runner.force_stop()
        else:
            self._system(run.cell, "■ Stop requested (Ctrl+C sent)…")
            run.runner.request_stop()
        self.stateChanged.emit()

    @property
    def user_stopped(self) -> bool:
        """The last run was ended by the user (Stop), not by a failure or by finishing."""
        return self._user_stop

    @property
    def stopping(self) -> bool:
        return self._run is not None and self._user_stop

    def discard(self) -> None:
        """The session is being closed: nothing may start any more; a live cell is stopped."""
        self.discarded = True
        self._poll.stop()
        runners = self._live_runners()
        if not runners:
            self._maybe_settled()
            return
        for runner in runners:
            runner.interrupt_grace = min(runner.interrupt_grace, 2.0)
            runner.request_stop()

    def kill_now(self) -> None:
        """Application exit: kill the running cell's process tree immediately."""
        self.discarded = True
        self._poll.stop()
        for runner in self._live_runners():
            runner.kill_now()
