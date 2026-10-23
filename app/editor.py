"""Open a tool's main.py in the system's editor.

Windows: the shell's "edit" verb (what Explorer's "Edit" does) - NOT "open", which for .py files usually *runs*
the script.  When no editor is registered for the verb, Notepad is used.  macOS: ``open -t`` (the default text
editor).  Linux: ``xdg-open``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def open_in_editor(path: Path | str) -> str | None:
    """Returns an error message, or None when the editor was started."""
    path = str(path)
    if not os.path.isfile(path):
        return f"The file does not exist any more:\n{path}"
    try:
        if sys.platform.startswith("win"):
            try:
                os.startfile(path, "edit")                      # type: ignore[attr-defined]
            except OSError:
                subprocess.Popen(["notepad.exe", path], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-t", path])
        else:
            opener = shutil.which("xdg-open")
            if opener is None:
                return "No editor could be started (xdg-open was not found)."
            subprocess.Popen([opener, path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        return f"Could not open the editor: {exc}"
    return None
