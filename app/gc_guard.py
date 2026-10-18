"""Keep Python's cyclic garbage collector on the GUI thread.

The toolbox runs a few background threads (process pipe readers).  Python's cyclic GC can
run on whichever thread happens to trigger it, and if it collects a cycle that contains a
Qt object (for example a dialog whose signal connections point back at it) on a worker
thread, PySide defers the C++ deletion to the main thread, where it can crash.  Disabling
automatic collection and running it from a timer on the GUI thread makes that impossible.
"""

from __future__ import annotations

import gc

from PySide6.QtCore import QObject, QTimer

_INTERVAL_MS = 1500


def install(parent: QObject) -> QTimer:
    gc.disable()
    timer = QTimer(parent)
    timer.setInterval(_INTERVAL_MS)
    timer.timeout.connect(gc.collect)
    timer.start()
    return timer
