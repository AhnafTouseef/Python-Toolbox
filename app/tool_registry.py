"""Tool discovery and the in-memory tool list.

Discovery only looks at directory names and the presence of main.py: it never
imports or executes a tool.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from . import tool_metadata as tm
from .icon_manager import IconManager

log = logging.getLogger(__name__)

ENTRY_NAME = "main.py"


@dataclass
class ToolInfo:
    id: str                       # directory name, unique inside tools/
    path: Path                    # tool directory (subprocess working directory)
    meta: tm.ToolMetadata
    extras: dict = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)

    @property
    def entry(self) -> Path:
        return self.path / ENTRY_NAME


def discover_tool_dirs(tools_dir: Path) -> list[Path]:
    """Immediate subdirectories of tools_dir that contain main.py (no recursion)."""
    found: list[Path] = []
    try:
        with os.scandir(tools_dir) as it:
            for entry in it:
                try:
                    if entry.is_dir() and os.path.isfile(os.path.join(entry.path, ENTRY_NAME)):
                        found.append(Path(entry.path))
                except OSError as exc:
                    log.warning("Skipping %s: %s", entry.path, exc)
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.error("Cannot scan %s: %s", tools_dir, exc)
        return []
    found.sort(key=lambda p: p.name.casefold())
    return found


class ToolRegistry(QObject):
    metadataChanged = Signal(str)          # tool id

    def __init__(self, tools_dir: Path, icons: IconManager, parent: QObject | None = None):
        super().__init__(parent)
        self.tools_dir = Path(tools_dir)
        self.icons = icons
        self.tools: list[ToolInfo] = []
        self.by_id: dict[str, ToolInfo] = {}
        self.issues: list[str] = []

    def scan(self) -> None:
        issues: list[str] = []
        try:
            self.tools_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            issues.append(f"Could not create the tools folder {self.tools_dir}: {exc}")
        tools: list[ToolInfo] = []
        for d in discover_tool_dirs(self.tools_dir):
            try:
                res = tm.load_or_create(d, self.icons.has, self.icons.generic)
            except Exception as exc:  # one tool must never break discovery of the others
                log.exception("Metadata load failed for %s", d)
                res = tm.LoadResult(tm.default_metadata(d.name, self.icons.generic), {},
                                    [f"{d.name}: unexpected error while reading metadata ({exc}); defaults used."])
            tools.append(ToolInfo(d.name, d, res.metadata, res.extras, res.issues))
            issues.extend(res.issues)
        self.tools = tools
        self.by_id = {t.id: t for t in tools}
        self.issues = issues

    def get(self, tool_id: str) -> ToolInfo | None:
        return self.by_id.get(tool_id)

    def update_metadata(self, tool_id: str, new_meta: tm.ToolMetadata) -> tuple[bool, str]:
        """Validate and save. Returns (ok, error message). Memory is updated only after a successful save."""
        tool = self.by_id.get(tool_id)
        if tool is None:
            return False, "This tool no longer exists."
        problems = tm.validate_fields(new_meta.name, new_meta.description, new_meta.icon,
                                      new_meta.color, self.icons.has)
        if problems:
            return False, " ".join(problems)
        new_meta = tm.ToolMetadata(new_meta.name.strip(), new_meta.description,
                                   new_meta.icon, tm.normalize_hex(new_meta.color))
        try:
            tm.save_metadata(tool.path, new_meta)
        except OSError as exc:
            return False, f"Could not save {tm.METADATA_FILENAME}: {exc}. Your changes were not saved."
        tool.meta = new_meta
        self.metadataChanged.emit(tool_id)
        return True, ""
