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
    id: str                       # built-in tools/: the directory name; added folders: the full directory path
    path: Path                    # tool directory (subprocess working directory)
    meta: tm.ToolMetadata
    extras: dict = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    root: Path | None = None      # the folder that was scanned to find it (tools/ or an added folder)
    root_index: int = 0           # 0 = the built-in tools/ folder ("Path 1"), then the added folders in order
    tag: str = ""                 # "Path 2 · MyTools" - only set while another tool has the same name

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


@dataclass
class ScanSummary:
    total: int = 0
    added: list[str] = field(default_factory=list)      # tool ids
    removed: list[str] = field(default_factory=list)


def norm_path(p: Path | str) -> str:
    """Key for 'is this the same folder': absolute, normalised, case-folded on Windows."""
    try:
        resolved = os.path.realpath(os.path.expanduser(str(p)))
    except OSError:
        resolved = os.path.abspath(str(p))
    return os.path.normcase(resolved)


class ToolRegistry(QObject):
    metadataChanged = Signal(str)          # tool id

    def __init__(self, tools_dir: Path, icons: IconManager, extra_dirs: list[Path] | list[str] | tuple = (),
                 parent: QObject | None = None):
        super().__init__(parent)
        self.tools_dir = Path(tools_dir)
        self.extra_dirs: list[Path] = [Path(d) for d in extra_dirs]
        self.icons = icons
        self.tools: list[ToolInfo] = []
        self.by_id: dict[str, ToolInfo] = {}
        self.issues: list[str] = []

    # ------------------------------------------------------------------ folders
    @property
    def roots(self) -> list[Path]:
        """Path 1 is the application's own tools/ folder; the folders the user added follow in order."""
        return [self.tools_dir, *self.extra_dirs]

    def set_extra_dirs(self, dirs) -> None:
        self.extra_dirs = [Path(d) for d in dirs]

    def root_label(self, index: int) -> str:
        roots = self.roots
        root = roots[index] if 0 <= index < len(roots) else None
        return f"Path {index + 1} · {root.name or str(root)}" if root is not None else f"Path {index + 1}"

    # ------------------------------------------------------------------ discovery
    def scan(self) -> ScanSummary:
        """(Re)discover tools in every folder. Tools that are still there keep their ToolInfo object (open
        instances and library cards point at it); new ones are added and vanished ones are dropped."""
        issues: list[str] = []
        try:
            self.tools_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            issues.append(f"Could not create the tools folder {self.tools_dir}: {exc}")
        old = self.by_id
        found: list[tuple[str, Path, int]] = []             # (id, dir, root index)
        seen: set[str] = set()
        for index, root in enumerate(self.roots):
            if index > 0 and not root.is_dir():
                issues.append(f"Tool folder not found: {root}")
                continue
            for d in discover_tool_dirs(root):
                key = norm_path(d)
                if key in seen:                              # the same tool reached through two overlapping folders
                    continue
                seen.add(key)
                found.append((d.name if index == 0 else str(d), d, index))
        tools: list[ToolInfo] = []
        changed: list[str] = []
        for tid, d, index in found:
            try:
                res = tm.load_or_create(d, self.icons.has, self.icons.generic)
            except Exception as exc:  # one tool must never break discovery of the others
                log.exception("Metadata load failed for %s", d)
                res = tm.LoadResult(tm.default_metadata(d.name, self.icons.generic), {},
                                    [f"{d.name}: unexpected error while reading metadata ({exc}); defaults used."])
            tool = old.get(tid)
            if tool is None:
                tool = ToolInfo(tid, d, res.metadata, res.extras, res.issues, self.roots[index], index)
            else:
                if tool.meta != res.metadata:
                    changed.append(tid)
                tool.path, tool.meta, tool.extras, tool.issues = d, res.metadata, res.extras, res.issues
                tool.root, tool.root_index = self.roots[index], index
            tools.append(tool)
            issues.extend(res.issues)
        tools.sort(key=lambda t: (t.path.name.casefold(), t.root_index))
        summary = ScanSummary(total=len(tools),
                              added=[t.id for t in tools if t.id not in old],
                              removed=[tid for tid in old if tid not in {t.id for t in tools}])
        self.tools = tools
        self.by_id = {t.id: t for t in tools}
        self.issues = issues
        tag_changes = self._recompute_tags()
        for tid in dict.fromkeys(changed + tag_changes):
            if tid in self.by_id and tid not in summary.added:
                self.metadataChanged.emit(tid)
        return summary

    def _recompute_tags(self) -> list[str]:
        """Tools whose display names collide get a "Path n · folder" tag; returns the ids whose tag changed."""
        counts: dict[str, int] = {}
        for t in self.tools:
            counts[t.meta.name.casefold()] = counts.get(t.meta.name.casefold(), 0) + 1
        changed = []
        for t in self.tools:
            tag = self.root_label(t.root_index) if counts[t.meta.name.casefold()] > 1 else ""
            if tag != t.tag:
                t.tag = tag
                changed.append(t.id)
        return changed

    def get(self, tool_id: str) -> ToolInfo | None:
        return self.by_id.get(tool_id)

    def resolve(self, folder_name: str, source: str = "") -> ToolInfo | None:
        """Find a tool by its folder name (and, preferably, the folder it was found in; "" = the built-in tools/).
        Used for presets, which must keep working on another PC where the added folders live elsewhere."""
        same = [t for t in self.tools if t.path.name == folder_name]
        if not same:
            same = [t for t in self.tools if t.path.name.casefold() == folder_name.casefold()]
        if not same:
            return None
        if source:
            for t in same:
                if t.root is not None and norm_path(t.root) == norm_path(source):
                    return t
        elif source == "":
            for t in same:
                if t.root_index == 0:
                    return t
        return same[0]

    def source_of(self, tool: ToolInfo) -> str:
        """What a preset stores to find the tool again: "" for the built-in tools/ folder, else the folder path."""
        return "" if tool.root_index == 0 or tool.root is None else str(tool.root)

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
        others = [t for t in self._recompute_tags() if t != tool_id]      # a rename can create / end a name clash
        self.metadataChanged.emit(tool_id)
        for tid in others:
            self.metadataChanged.emit(tid)
        return True, ""
