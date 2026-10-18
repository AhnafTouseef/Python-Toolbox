"""Application-level settings (not tool metadata).

config.json lives next to app.py:

    { "global_python": null }

`null` means "use the interpreter that is running the toolbox". Set it to the full
path of a python.exe to use a different global interpreter for tools without a .venv.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


def console_python(executable: str | None = None) -> str:
    """The python used to run tools: never pythonw.exe (it has no stdout/stderr/stdin)."""
    exe = executable or sys.executable
    if os.name == "nt" and exe.lower().endswith("pythonw.exe"):
        candidate = exe[:-len("pythonw.exe")] + "python.exe"
        if os.path.exists(candidate):
            return candidate
    return exe


@dataclass
class AppConfig:
    path: Path
    global_python: str | None = None
    notice: str | None = None  # set when the file was unreadable/invalid

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        cfg = cls(path=path)
        if not path.exists():
            try:
                path.write_text(json.dumps({"global_python": None}, indent=2) + "\n", encoding="utf-8")
            except OSError as exc:
                log.info("Could not create %s: %s", path, exc)
            return cfg
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            value = data.get("global_python")
            if value is None or (isinstance(value, str) and not value.strip()):
                cfg.global_python = None
            elif isinstance(value, str):
                cfg.global_python = value.strip()
            else:
                raise ValueError("'global_python' must be a string or null")
        except (OSError, ValueError) as exc:
            cfg.notice = f"config.json could not be used ({exc}); falling back to the interpreter running the toolbox."
            log.warning(cfg.notice)
        return cfg

    def resolved_global_python(self) -> str:
        return self.global_python or console_python()
