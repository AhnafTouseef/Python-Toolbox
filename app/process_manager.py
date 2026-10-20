"""Runs one tool script as a child process and streams its I/O to the UI.

This module knows nothing about widgets.  It uses ``subprocess.Popen`` with small
background threads (stdout reader, stderr reader, stdin writer, waiter) because that
gives exact control over the things a *correct Stop* needs on Windows (creation
flags, console handling, process-tree termination) and behaves identically on every
platform the tests run on.  Threads never touch widgets: they hand data to the GUI
thread through queued Qt signals, and a data signal is only emitted when the previous
one has been consumed, so a script that prints very fast cannot flood the event loop.

Child process conventions (so that existing scripts work unmodified through pipes):

* ``python -u main.py`` plus ``PYTHONUNBUFFERED=1`` -> ``print()`` and ``input()``
  prompts appear immediately instead of when a 4-8 KB buffer fills.
* ``PYTHONIOENCODING=utf-8`` for stdin/stdout/stderr only (``open()`` defaults are
  deliberately left alone so a tool behaves as it does under Command Prompt).
* The tool directory is the working directory; the environment is otherwise inherited.

Stop is staged: interrupt (Ctrl+C / SIGINT) -> terminate -> kill the whole process
tree.  Each stage only happens if the process is still alive after a grace period.
"""

from __future__ import annotations

import codecs
import collections
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from .config import console_python

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000
ENTRY_NAME = "main.py"
VENV_DIR = ".venv"


class LaunchError(Exception):
    """The script could not be started; the message is meant for the user."""


# --------------------------------------------------------------------- interpreter
def resolve_interpreter(tool_dir: Path, global_python: str) -> tuple[str | None, str | None, str]:
    """Pick the interpreter for a tool.  Returns (path, error, source) with source 'venv' or 'global'.

    * ``<tool>/.venv/Scripts/python.exe`` exists            -> use it.
    * a ``.venv`` exists but that file does not              -> error (never fall back silently).
    * otherwise                                              -> the configured global interpreter.
    """
    venv = tool_dir / VENV_DIR
    exe = venv / "Scripts" / "python.exe"
    if exe.is_file():
        return str(exe), None, "venv"
    if venv.exists():
        return None, (f"This tool has a {VENV_DIR} folder, but its interpreter was not found at\n{exe}\n"
                      f"Recreate the virtual environment (python -m venv {VENV_DIR}) or remove the {VENV_DIR} "
                      f"folder to use the global Python. The toolbox does not fall back automatically."), "venv"
    if not global_python or not (os.path.isfile(global_python) or shutil.which(global_python)):
        return None, (f"The global Python interpreter was not found: {global_python or '(not set)'}\n"
                      f"Set \"global_python\" in config.json to the full path of a python.exe."), "global"
    return global_python, None, "global"


def plan_tool_launch(tool_dir: Path, entry: Path, global_python: str) -> tuple[list[str], str, str]:
    """The command line that runs a tool: ``([python, "-u", "main.py"], interpreter, source)``.

    Used by the Run button and by queued tool cells so both launch a tool identically (same interpreter
    rules, same entry-file check).  Raises LaunchError with a user-readable message.
    """
    interp, err, source = resolve_interpreter(tool_dir, global_python)
    if err is None and not entry.is_file():
        err = (f"{ENTRY_NAME} was not found in\n{tool_dir}\n"
               f"The tool folder may have been moved, renamed or emptied since the toolbox started.")
    if err is not None:
        raise LaunchError(err)
    return [interp, "-u", ENTRY_NAME], interp, source


def describe_exit(code: int) -> str:
    """Human-readable exit status. Negative codes are POSIX signals; huge ones are Windows NTSTATUS values."""
    if code < 0:
        try:
            return f"terminated by {signal.Signals(-code).name}"
        except ValueError:
            return f"terminated by signal {-code}"
    if code >= 0x80000000:
        return f"exit code 0x{code:08X}"
    return f"exit code {code}"


_WIN_INTERRUPT_HELPER = (
    "import ctypes, sys, time\n"
    "k = ctypes.windll.kernel32\n"
    "k.FreeConsole()\n"
    "if not k.AttachConsole(int(sys.argv[1])):\n"
    "    sys.exit(3)\n"
    "k.SetConsoleCtrlHandler(None, True)\n"
    "k.GenerateConsoleCtrlEvent(0, 0)\n"       # CTRL_C_EVENT to every process on the child's console
    "time.sleep(0.5)\n"
)


class _Handle:
    """State of one launched process (one Run)."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.buf: collections.deque[tuple[str, str]] = collections.deque()
        self.size = 0                     # characters currently queued in buf
        self.waiting = 0                  # readers blocked on backpressure (UI is behind)
        self.exit_code: int | None = None  # set (GUI thread) once the process has exited
        self.pending = False              # a wake signal is outstanding / data remains queued
        self.closed = False               # finished delivered: late data from stray readers is dropped
        self.stop_requested = False
        self.stdin_q: queue.Queue[bytes | None] = queue.Queue()
        self.readers: list[threading.Thread] = []


class ProcessRunner(QObject):
    output = Signal(str, str)          # kind ("stdout"; "stderr" only when merge_stderr is False), decoded text
    started = Signal(int)              # pid
    finished = Signal(object, bool)    # exit code (Python int: Windows codes such as 0xC000013A exceed C int), stop requested
    stdinFailed = Signal()             # the process is no longer reading stdin

    _wake = Signal(object)
    _exited = Signal(object, object)    # handle, exit code (Python int, see above)
    _stdin_broke = Signal(object)

    interrupt_grace = 3.0              # seconds between interrupt and terminate
    terminate_grace = 2.0              # seconds between terminate and kill
    reader_join_timeout = 1.5          # how long to wait for pipes to drain after the process exits
    # stderr shares the stdout pipe (like a real console), so the text appears in exactly the order the script
    # wrote it. With separate pipes the two reader threads race and a traceback can show up before the lines
    # printed just ahead of it; set False only if distinguishing stderr (shown in red) matters more than order.
    merge_stderr = True
    throttle_ms = 16                   # minimum interval between UI deliveries (leading-edge)
    max_delivery_chars = 64 * 1024     # one UI delivery handles at most this much text (bounded latency)
    high_water_chars = 2 * 1024 * 1024  # readers block above this: the child then blocks on its pipe, like a terminal

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._handle: _Handle | None = None
        self._stage = 0                # 0 none, 1 interrupt sent, 2 terminate sent, 3 kill sent
        self._deferred = False
        self._escalation = QTimer(self)
        self._escalation.setSingleShot(True)
        self._escalation.timeout.connect(self._escalate)
        self._throttle = QTimer(self)
        self._throttle.setSingleShot(True)
        self._throttle.timeout.connect(self._on_throttle)
        self._wake.connect(self._on_wake)
        self._exited.connect(self._on_exited)
        self._stdin_broke.connect(self._on_stdin_broke)

    # ------------------------------------------------------------------ state
    @property
    def running(self) -> bool:
        return self._handle is not None

    @property
    def pid(self) -> int | None:
        return self._handle.proc.pid if self._handle else None

    @property
    def stop_requested(self) -> bool:
        return bool(self._handle and self._handle.stop_requested)

    # ------------------------------------------------------------------ start
    def start(self, argv: list[str] | str, cwd: str, extra_env: dict[str, str] | None = None) -> None:
        """Start a process. ``argv`` is a list, or (Windows only, used for cmd.exe command lines whose quoting
        must be passed through untouched) a complete command line string."""
        program = argv if isinstance(argv, str) else argv[0]
        if self._handle is not None:
            raise LaunchError("This instance is already running.")
        if not os.path.isdir(cwd):
            raise LaunchError(f"The tool folder does not exist: {cwd}")
        env = os.environ.copy()
        env.update(extra_env or {})
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        kwargs: dict = dict(cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT if self.merge_stderr else subprocess.PIPE, bufsize=0)
        if IS_WINDOWS:
            # Own hidden console: Ctrl+C can be delivered to this child alone, and no window flashes up.
            kwargs["creationflags"] = CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True      # own process group: signals/kill reach the whole tree
        try:
            proc = subprocess.Popen(argv, **kwargs)
        except FileNotFoundError:
            raise LaunchError(f"Could not start the interpreter: file not found\n{program}") from None
        except PermissionError as exc:
            raise LaunchError(f"Permission denied when starting {program}: {exc}") from None
        except (OSError, ValueError) as exc:
            raise LaunchError(f"Could not start {program}: {exc}") from None

        h = _Handle(proc)
        self._handle = h
        self._stage = 0
        self._deferred = False
        pipes = [(proc.stdout, "stdout")] + ([] if proc.stderr is None else [(proc.stderr, "stderr")])
        for stream, kind in pipes:
            t = threading.Thread(target=self._reader, args=(h, stream, kind), daemon=True,
                                 name=f"toolbox-{kind}-{proc.pid}")
            h.readers.append(t)
            t.start()
        threading.Thread(target=self._writer, args=(h,), daemon=True, name=f"toolbox-stdin-{proc.pid}").start()
        threading.Thread(target=self._waiter, args=(h,), daemon=True, name=f"toolbox-wait-{proc.pid}").start()
        self.started.emit(proc.pid)

    # ------------------------------------------------------------------ worker threads
    def _push(self, h: _Handle, kind: str, text: str) -> None:
        with h.cond:
            if h.size > self.high_water_chars and not h.closed:
                h.waiting += 1
                try:
                    while h.size > self.high_water_chars and not h.closed:
                        h.cond.wait(0.1)          # backpressure: the UI is behind, stop reading the pipe
                finally:
                    h.waiting -= 1
            if h.closed:
                return
            h.buf.append((kind, text))
            h.size += len(text)
            need_wake = not h.pending
            h.pending = True
        if need_wake:
            self._wake.emit(h)

    def _reader(self, h: _Handle, stream, kind: str) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                data = stream.read(65536)          # raw pipe: returns as soon as some data is available
                if not data:
                    break
                text = decoder.decode(data)
                if text:
                    self._push(h, kind, text)
            tail = decoder.decode(b"", final=True)
            if tail:
                self._push(h, kind, tail)
        except (OSError, ValueError):
            pass                                   # pipe closed underneath us

    def _writer(self, h: _Handle) -> None:
        stdin = h.proc.stdin
        try:
            while True:
                item = h.stdin_q.get()
                if item is None:
                    break
                view = memoryview(item)
                while view:
                    written = stdin.write(view)
                    view = view[written or len(view):]
        except (OSError, ValueError):
            self._stdin_broke.emit(h)
        finally:
            try:
                stdin.close()
            except (OSError, ValueError):
                pass

    def _waiter(self, h: _Handle) -> None:
        code = -1
        try:
            code = h.proc.wait()
            # Wait for the pipes to drain. Readers that are merely waiting for the UI (backpressure) are given
            # all the time they need; readers stuck in read() because a grandchild inherited the pipe are
            # abandoned after reader_join_timeout.
            end = time.monotonic() + self.reader_join_timeout
            while any(t.is_alive() for t in h.readers):
                for t in h.readers:
                    t.join(0.02)
                if time.monotonic() > end:
                    with h.lock:
                        waiting = h.waiting
                    if waiting == 0:
                        break
                    end = time.monotonic() + self.reader_join_timeout
            if not any(t.is_alive() for t in h.readers):
                for stream in (h.proc.stdout, h.proc.stderr):
                    if stream is None:
                        continue
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
        except Exception:
            log.exception("Error while waiting for process %s", h.proc.pid)
        finally:
            h.stdin_q.put(None)
            self._exited.emit(h, code)          # without this the instance would stay "running" forever

    # ------------------------------------------------------------------ GUI-thread delivery
    def _on_wake(self, h: _Handle) -> None:
        if h is self._handle:
            self._request_delivery(h)

    def _request_delivery(self, h: _Handle) -> None:
        """Deliver now if the throttle is idle, otherwise on its next tick."""
        if self._throttle.isActive():
            self._deferred = True
        else:
            self._deliver(h)

    def _on_throttle(self) -> None:
        h = self._handle
        if self._deferred and h is not None:
            self._deferred = False
            self._deliver(h)

    def _deliver(self, h: _Handle) -> None:
        """One bounded slice of output; follows up on the next tick while data remains or an exit is waiting."""
        more = self._drain(h)
        if not more and h.exit_code is not None:
            self._finalize(h)                      # exited and everything printed has been delivered
            return
        if more or h.exit_code is not None:
            self._deferred = True
        self._throttle.start(self.throttle_ms)

    def _drain(self, h: _Handle) -> bool:
        """Emit up to max_delivery_chars of queued output. Returns True if more is still queued."""
        taken: list[tuple[str, str]] = []
        n = 0
        with h.cond:
            while h.buf and n < self.max_delivery_chars:
                kind, text = h.buf.popleft()
                taken.append((kind, text))
                n += len(text)
                h.size -= len(text)
            more = bool(h.buf)
            h.pending = more                       # while data remains no new wake is sent; we follow up ourselves
            h.cond.notify_all()
        merged: list[tuple[str, list[str]]] = []
        for kind, text in taken:
            if merged and merged[-1][0] == kind:
                merged[-1][1].append(text)
            else:
                merged.append((kind, [text]))
        for kind, parts in merged:
            self.output.emit(kind, "".join(parts))
        return more

    def _on_exited(self, h: _Handle, code: int) -> None:
        if h is not self._handle:
            return
        h.exit_code = code
        self._escalation.stop()
        self._request_delivery(h)

    def _finalize(self, h: _Handle) -> None:
        self._throttle.stop()
        self._deferred = False
        with h.cond:
            h.closed = True
            h.cond.notify_all()
        self._handle = None
        self._stage = 0
        self.finished.emit(h.exit_code, h.stop_requested)

    def _on_stdin_broke(self, h: _Handle) -> None:
        if h is self._handle:
            self.stdinFailed.emit()

    # ------------------------------------------------------------------ stdin
    def write(self, text: str) -> bool:
        h = self._handle
        if h is None:
            return False
        h.stdin_q.put(text.encode("utf-8", errors="replace"))
        return True

    def close_stdin(self) -> None:
        if self._handle is not None:
            self._handle.stdin_q.put(None)

    # ------------------------------------------------------------------ stopping
    def request_stop(self) -> None:
        """First call: ask the script to stop (Ctrl+C). Escalates automatically if it does not exit."""
        h = self._handle
        if h is None or h.stop_requested:
            return
        h.stop_requested = True
        self._stage = 1
        self._interrupt(h)
        self._escalation.start(int(self.interrupt_grace * 1000))

    def force_stop(self) -> None:
        """Skip the remaining grace period and move to the next, harsher stage."""
        h = self._handle
        if h is None:
            return
        h.stop_requested = True
        self._escalate()

    def _escalate(self) -> None:
        h = self._handle
        if h is None:
            return
        if self._stage <= 1:
            self._stage = 2
            self._terminate(h)
            self._escalation.start(int(self.terminate_grace * 1000))
        elif self._stage == 2:
            self._stage = 3
            self._kill(h)

    def kill_now(self) -> None:
        """Immediately kill the process tree (used when the application quits)."""
        h = self._handle
        if h is not None:
            h.stop_requested = True
            self._kill(h, wait=True)

    def _signal_group(self, h: _Handle, sig: int) -> None:
        try:
            os.killpg(h.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        except OSError as exc:
            log.debug("killpg failed: %s", exc)

    def _interrupt(self, h: _Handle) -> None:
        if not IS_WINDOWS:
            self._signal_group(h, signal.SIGINT)
            return
        # Windows: deliver Ctrl+C to the child's own console from a short-lived helper process (the GUI
        # process must not attach to that console itself).
        try:
            helper = subprocess.Popen([console_python(), "-c", _WIN_INTERRUPT_HELPER, str(h.proc.pid)],
                                      creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            threading.Thread(target=helper.wait, daemon=True).start()
        except OSError as exc:
            log.warning("Could not start the Ctrl+C helper: %s", exc)
        # A script blocked in input() on a pipe only sees Ctrl+C after the read returns. Closing stdin
        # shortly afterwards lets that read finish (EOF) so the script can exit instead of waiting for
        # the terminate stage.
        QTimer.singleShot(1000, lambda: self.close_stdin() if self._handle is h else None)

    def _terminate(self, h: _Handle) -> None:
        if IS_WINDOWS:
            self._taskkill(h.proc.pid)
        else:
            self._signal_group(h, signal.SIGTERM)

    def _kill(self, h: _Handle, wait: bool = False) -> None:
        if IS_WINDOWS:
            self._taskkill(h.proc.pid, wait=wait)
            try:
                h.proc.kill()
            except OSError:
                pass
        else:
            self._signal_group(h, signal.SIGKILL)
            try:
                h.proc.kill()
            except OSError:
                pass

    @staticmethod
    def _taskkill(pid: int, wait: bool = False) -> None:
        try:
            p = subprocess.Popen(["taskkill", "/PID", str(pid), "/T", "/F"], creationflags=CREATE_NO_WINDOW,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if wait:
                p.wait(timeout=5)
            else:
                threading.Thread(target=p.wait, daemon=True).start()
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("taskkill failed: %s", exc)
