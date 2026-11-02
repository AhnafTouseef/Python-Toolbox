"""Toolbox side of the stdin probe (see stdin_probe/sitecustomize.py for the process side).

One StdinProbe belongs to one queued cell run.  It owns a private temporary folder; Python processes of that
cell report blocking stdin reads there as marker files, and the queue engine polls ``scan()``.

* ``active-<pid>``      a Python process with the probe installed is alive: stdin waits of this cell ARE visible.
* ``wait-<pid>-<n>``    that process's main thread is blocked in a stdin read right now (n = request number).

Without an ``active-*`` file the cell is "undetectable" (not Python, ``python -I``, ...): the engine then never
claims the cell is waiting and never applies a timeout.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

PROBE_DIR = Path(__file__).resolve().parent / "stdin_probe"     # contains only sitecustomize.py (it goes on PYTHONPATH)
ENV_VAR = "TOOLBOX_STDIN_PROBE"


@dataclass(frozen=True)
class ProbeSnapshot:
    active: bool                  # the probe is installed in at least one live process of the cell
    waits: frozenset[str]         # names of the outstanding "blocked on stdin" requests


class StdinProbe:
    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="toolbox-cell-"))

    def env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """Environment additions that switch the probe on for Python processes started by the cell."""
        existing = (base if base is not None else os.environ).get("PYTHONPATH", "")
        return {ENV_VAR: str(self.dir),
                "PYTHONPATH": str(PROBE_DIR) + (os.pathsep + existing if existing else "")}

    def scan(self) -> ProbeSnapshot:
        active = False
        waits: set[str] = set()
        try:
            with os.scandir(self.dir) as it:
                for entry in it:
                    name = entry.name
                    if name.startswith("wait-"):
                        waits.add(name)
                    elif name.startswith("active-"):
                        active = True
        except OSError:
            pass
        return ProbeSnapshot(active, frozenset(waits))

    def close(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def advance_waits(seen: dict[str, float], answered: dict[str, float], unsatisfied: dict[str, float],
                  waits: frozenset[str], now: float, debounce_s: float, send_next: Callable[[], bool]) -> None:
    """The rule for answering reads of a probed Python process (queue cells and main processes share it).

    ``waits`` are the reads blocked right now.  A read counts only once it has lasted ``debounce_s`` (reads that
    return at once are not waits); each counted read gets at most ONE predefined answer (``send_next()`` returns
    False when none is left); a read that is still blocked after its answer, or that found no answer, goes into
    ``unsatisfied`` (it needs real input).  The three dicts are the caller's per-run state, keyed by wait name.
    """
    for table in (seen, answered, unsatisfied):
        for key in [k for k in table if k not in waits]:
            del table[key]                                        # that read returned
    for key in waits:
        seen.setdefault(key, now)
    for key, first in sorted(seen.items(), key=lambda kv: kv[1]):
        if now - first < debounce_s:
            continue                                              # a read that returns at once is not a wait
        if key not in answered:
            if send_next():
                answered[key] = now
            else:
                unsatisfied.setdefault(key, now)
        elif now - answered[key] >= debounce_s:
            unsatisfied.setdefault(key, now)                      # still blocked after its answer: needs more
