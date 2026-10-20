"""Stdin probe for queued cells (loaded by Python through PYTHONPATH; never used for normal runs).

The Toolbox cannot see from the outside whether a process is blocked reading its stdin.  A Python
process can say so itself, so for cells started by the queue the Toolbox puts this folder on PYTHONPATH
and sets TOOLBOX_STDIN_PROBE to a private state folder.  Python imports ``sitecustomize`` at startup, and
this module then replaces ``sys.stdin`` with an equivalent object whose *blocking OS read* creates an empty
marker file ``wait-<pid>-<n>`` in the state folder before it blocks and removes it when the read returns.
The Toolbox polls that folder.  ``active-<pid>`` exists for as long as the probe is installed in a process.

Design rules
* Transparent: same encoding/errors/newline handling, same fd, same buffering.  The text layer is rebuilt over
  a raw reader that only adds the marker; nothing has been read from fd 0 yet when this runs.
* Safe by default: any problem -> do nothing (no markers means "cannot tell", never "waiting").
* Only the main thread's reads count: a helper thread parked on stdin while the main thread works is not a
  process that is stuck waiting for input.
* Reads through os.read(0) / msvcrt are invisible to it (documented limitation).
"""


def _chain_shadowed_sitecustomize(own_dir):
    """This file shadows any other sitecustomize (e.g. a distribution's); run that one too."""
    import importlib.machinery
    import os
    import sys
    try:
        own = os.path.normcase(os.path.abspath(own_dir))
        rest = [p for p in sys.path if os.path.normcase(os.path.abspath(p or os.getcwd())) != own]
        spec = importlib.machinery.PathFinder.find_spec("sitecustomize", rest)
        if spec is not None and spec.loader is not None and spec.origin not in (None, __file__):
            import importlib.util
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
    except Exception:
        pass


def _install():
    import atexit
    import io
    import itertools
    import os
    import sys
    import threading

    state_dir = os.environ.get("TOOLBOX_STDIN_PROBE")
    if not state_dir or not os.path.isdir(state_dir):
        return
    old = sys.stdin
    if old is None or not hasattr(old, "buffer"):
        return
    try:
        if old.isatty() or old.fileno() != 0:
            return
    except Exception:
        return

    counter = itertools.count(1)
    main_thread = threading.main_thread

    def enter():
        if threading.current_thread() is not main_thread():
            return None
        path = os.path.join(state_dir, "wait-%d-%d" % (os.getpid(), next(counter)))
        try:
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL))
        except OSError:
            return None
        return path

    def leave(path):
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass

    class ProbedRaw(io.FileIO):
        def readinto(self, b):
            token = enter()
            try:
                return super().readinto(b)
            finally:
                leave(token)

        def readall(self):
            token = enter()
            try:
                return super().readall()
            finally:
                leave(token)

    raw = ProbedRaw(0, "rb", closefd=False)
    buffered = io.BufferedReader(raw)
    # CPython creates sys.stdin with universal newlines on Windows and with "\n" elsewhere.
    new = io.TextIOWrapper(buffered, encoding=old.encoding, errors=old.errors,
                           newline=None if os.name == "nt" else "\n", line_buffering=old.line_buffering)
    new.mode = getattr(old, "mode", "r")
    sys.stdin = new

    marker = os.path.join(state_dir, "active-%d" % os.getpid())
    try:
        os.close(os.open(marker, os.O_CREAT | os.O_WRONLY))
    except OSError:
        return

    def cleanup():
        try:
            os.unlink(marker)
        except OSError:
            pass

    atexit.register(cleanup)


try:
    import os as _os
    _chain_shadowed_sitecustomize(_os.path.dirname(_os.path.abspath(__file__)))
    _install()
except Exception:
    pass
