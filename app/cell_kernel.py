"""The shared Python session of one queue run (runs inside the TOOL's interpreter, not inside the Toolbox).

All Python code cells of a run are executed by ONE long-lived Python process, in ONE namespace, so a variable,
import, function or class defined by an earlier cell is available to the later ones.  The process lives for the
duration of a run only: the queue ends it when the run completes, is stopped or fails, so nothing survives into
the next run.

Protocol (Toolbox <-> this process)
* The Toolbox writes the code of the n-th cell to ``<control dir>/run-<n>.py``.  stdin cannot carry it, because
  stdin belongs to the user's code (``input()``).  This process polls for the file while idle (every 10 ms).
* When a cell has finished, a completion marker is written to stdout AFTER everything the cell printed, so the
  Toolbox knows that all output of the cell has been delivered before it records the result.  The marker holds a
  per-session token, so nothing a cell prints can be mistaken for it, and the Toolbox removes it from the output.
* ``<control dir>/quit`` (or the control directory disappearing) ends the process.

Only the standard library is used and the syntax is kept simple: the tool's own interpreter (possibly an old
one inside a .venv) must be able to run this file.  The Toolbox imports the module for the marker constants only;
``main`` is called solely by the bootstrap that launches the session.
"""

import os
import sys
import time

MARK_PREFIX = "\x1e\x1eTBX:"        # + token + ":" + "<cell number>:<exit code>" + MARK_END
MARK_END = "\x1e"
RUN_FILE = "run-%d.py"
QUIT_FILE = "quit"
POLL_SECONDS = 0.01


def completion_marker(token, number, code):
    return "%s%s:%d:%d%s" % (MARK_PREFIX, token, number, code, MARK_END)


def _flush():
    for stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
        try:
            stream.flush()
        except Exception:
            pass


def _report(token, number, code):
    _flush()
    data = completion_marker(token, number, code).encode("ascii")
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(1, view):]
    except OSError:
        pass


def _print_exception(traceback):
    etype, value, tb = sys.exc_info()
    try:
        # the first frame is this module's own exec call; the user sees only their cell's frames
        traceback.print_exception(etype, value, tb.tb_next if tb is not None else None)
    except Exception:
        try:
            sys.stderr.write("%r\n" % (value,))
        except Exception:
            pass


def _exit_code(exc):
    code = exc.code
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    try:
        sys.stderr.write("%s\n" % (code,))       # same as the interpreter: a non-integer exit value is printed
    except Exception:
        pass
    return 1


def _run_cell(path, namespace, traceback):
    """Run one cell. Returns its exit code: 0 ok, N for sys.exit(N), 1 for an exception, 130 for Ctrl+C."""
    sys.argv = [path]
    try:
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        exec(compile(source, path, "exec"), namespace)
        return 0
    except SystemExit as exc:
        return _exit_code(exc)
    except KeyboardInterrupt:
        _print_exception(traceback)
        return 130
    except BaseException:
        _print_exception(traceback)
        return 1


def main(control_dir, token):
    import builtins
    import traceback
    import types

    # Cells run as __main__ (so ``if __name__ == "__main__"`` works and classes pickle / dataclasses resolve
    # their module), in a module of their own that outlives every cell.
    module = types.ModuleType("__main__")
    module.__dict__["__builtins__"] = builtins
    sys.modules["__main__"] = module
    namespace = module.__dict__

    quit_path = os.path.join(control_dir, QUIT_FILE)
    number = 1
    try:
        while True:
            path = os.path.join(control_dir, RUN_FILE % number)
            if os.path.exists(path):
                _report(token, number, _run_cell(path, namespace, traceback))
                number += 1
            elif os.path.exists(quit_path) or not os.path.isdir(control_dir):
                break
            else:
                time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        pass                                      # Stop while idle: the session simply ends
    _flush()
