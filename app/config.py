"""Application-level settings (not tool metadata).

config.json lives next to app.py:

    { "global_python": null, "tool_paths": [], "pinned": [], "notifications": true, "tool_answers": {} }

`null` means "use the interpreter that is running the toolbox". Set it to the full
path of a python.exe to use a different global interpreter for tools without a .venv.
`tool_paths` lists the folders to look for tools in (each holds tool folders, i.e. folders with a main.py);
nothing is built in, the user sets them.  `pinned` lists the tools pinned to the top of the library (a tool's
id is its folder name, or its full folder path when an earlier folder already has a tool of that name).
`tool_answers` maps a tool id to the predefined answers ("1920, 1080, yes") that new runs of that tool start with;
an entry exists only for tools whose "Keep for this tool" box is ticked.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
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
    tool_paths: list[str] = field(default_factory=list)
    pinned: list[str] = field(default_factory=list)
    notifications: bool = True
    tool_answers: dict[str, str] = field(default_factory=dict)
    notice: str | None = None  # set when the file was unreadable/invalid
    _other: dict = field(default_factory=dict, repr=False)      # unknown keys, written back untouched

    def to_json(self) -> dict:
        data = dict(self._other)
        data["global_python"] = self.global_python
        data["tool_paths"] = list(self.tool_paths)
        data["pinned"] = list(self.pinned)
        data["notifications"] = self.notifications
        data["tool_answers"] = dict(self.tool_answers)
        return data

    def set_tool_answers(self, tool_id: str, text: str | None) -> str | None:
        """Keep (text) or forget (None / blank) a tool's predefined answers, then save. Returns an error or None."""
        if text is not None and text.strip():
            self.tool_answers[tool_id] = text.strip()
        else:
            self.tool_answers.pop(tool_id, None)
        return self.save()

    def save(self) -> str | None:
        """Write config.json. Returns an error message, or None when it was saved."""
        try:
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.to_json(), indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            return f"Could not save {self.path.name}: {exc}"
        return None

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        cfg = cls(path=path)
        if not path.exists():
            cfg.save()
            return cfg
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            cfg._other = {k: v for k, v in data.items() if k not in ("global_python", "tool_paths", "pinned", "notifications",
                                                                  "tool_answers")}
            value = data.get("global_python")
            if value is None or (isinstance(value, str) and not value.strip()):
                cfg.global_python = None
            elif isinstance(value, str):
                cfg.global_python = value.strip()
            else:
                raise ValueError("'global_python' must be a string or null")
            paths = data.get("tool_paths", [])
            if paths is None:
                paths = []
            if not isinstance(paths, list):
                raise ValueError("'tool_paths' must be a list of folder paths")
            seen: set[str] = set()
            for entry in paths:
                if not isinstance(entry, str) or not entry.strip():
                    cfg.notice = "config.json: an invalid entry in 'tool_paths' was ignored."
                    continue
                key = os.path.normcase(os.path.normpath(entry.strip()))
                if key not in seen:
                    seen.add(key)
                    cfg.tool_paths.append(entry.strip())
            if isinstance(data.get("notifications"), bool):
                cfg.notifications = data["notifications"]
            saved = data.get("tool_answers", {})
            if isinstance(saved, dict):
                for key, value in saved.items():
                    if isinstance(key, str) and isinstance(value, str) and value.strip():
                        cfg.tool_answers[key] = value.strip()
                    else:
                        cfg.notice = "config.json: an invalid entry in 'tool_answers' was ignored."
            elif saved is not None:
                cfg.notice = "config.json: 'tool_answers' must be an object; ignored."
            pins = data.get("pinned", [])
            if isinstance(pins, list):
                for entry in pins:
                    if isinstance(entry, str) and entry.strip() and entry not in cfg.pinned:
                        cfg.pinned.append(entry)
            elif pins is not None:
                cfg.notice = "config.json: 'pinned' must be a list; ignored."
        except (OSError, ValueError) as exc:
            cfg.notice = f"config.json could not be used ({exc}); falling back to the interpreter running the toolbox."
            log.warning(cfg.notice)
        return cfg

    def resolved_global_python(self) -> str:
        return self.global_python or console_python()
