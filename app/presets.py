"""Queue presets: a queue's cells saved for later, global (usable from any tool's queue).

Each preset is one JSON file in the presets/ folder next to config.json:

    {"format": 1, "name": "Render and clean", "saved": "2026-10-21T12:00:00",
     "cells": [{"kind": "code", "language": "python", "label": "", "command": "print(1)", "answers": "1, 2"},
               {"kind": "tool", "tool": "Render", "source": "", "label": "", "answers": ""}]}

A preset holds cells only (never the Must Execute switch, never results or output). Tool cells name the tool by
its folder name plus the folder it was found in (``source``), so a preset keeps working on another PC where
the folder lives somewhere else (an empty ``source`` just means "any folder").  No Qt in here.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .queue_model import LANG_PYTHON, LANG_SHELL, CellKind, QueueCell

log = logging.getLogger(__name__)

FORMAT = 1
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


@dataclass
class PresetCell:
    kind: CellKind
    label: str = ""
    command: str = ""
    language: str = LANG_PYTHON
    tool: str = ""                  # tool folder name
    source: str = ""                # folder the tool was found in ("" = unknown: match by name only)
    answers: str = ""

    def to_json(self) -> dict:
        d = {"kind": self.kind.value, "label": self.label, "answers": self.answers}
        if self.kind is CellKind.CODE:
            d.update(language=self.language, command=self.command)
        else:
            d.update(tool=self.tool, source=self.source)
        return d

    @classmethod
    def from_json(cls, d) -> "PresetCell":
        if not isinstance(d, dict):
            raise ValueError("a cell must be an object")
        try:
            kind = CellKind(d.get("kind"))
        except ValueError:
            raise ValueError(f"unknown cell kind {d.get('kind')!r}") from None
        def text(key: str) -> str:
            v = d.get(key, "")
            if not isinstance(v, str):
                raise ValueError(f"'{key}' must be text")
            return v
        lang = text("language") or LANG_PYTHON
        if lang not in (LANG_PYTHON, LANG_SHELL):
            raise ValueError(f"unknown language {lang!r}")
        cell = cls(kind, text("label"), text("command"), lang, text("tool"), text("source"), text("answers"))
        if kind is CellKind.TOOL and not cell.tool.strip():
            raise ValueError("a tool cell needs a tool")
        return cell


@dataclass
class Preset:
    name: str
    cells: list[PresetCell] = field(default_factory=list)
    saved: str = ""
    file: Path | None = None

    def summary(self) -> str:
        n = len(self.cells)
        return f"{n} cell{'s' if n != 1 else ''}"


def cell_to_preset(cell: QueueCell, registry=None) -> PresetCell:
    """Configuration only: results, output and status are never saved."""
    if cell.kind is CellKind.CODE:
        return PresetCell(CellKind.CODE, cell.label, cell.command, cell.language, answers=cell.answers_text)
    tool = registry.get(cell.tool_id) if registry is not None else None
    if tool is not None:
        return PresetCell(CellKind.TOOL, cell.label, tool=tool.path.name, source=registry.source_of(tool),
                          answers=cell.answers_text)
    return PresetCell(CellKind.TOOL, cell.label, tool=cell.tool_id, answers=cell.answers_text)


def safe_stem(name: str) -> str:
    stem = _BAD.sub("_", name.strip()).strip(" .") or "preset"
    if stem.split(".")[0].lower() in _RESERVED:
        stem = "_" + stem
    return stem[:80]


class PresetStore:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.problems: list[str] = []          # files that could not be read, from the last list()

    # ------------------------------------------------------------------ reading
    def list(self) -> list[Preset]:
        self.problems = []
        out: list[Preset] = []
        try:
            files = sorted(p for p in self.folder.glob("*.json") if p.is_file())
        except OSError as exc:
            self.problems.append(f"Could not read the presets folder {self.folder}: {exc}")
            return []
        for f in files:
            try:
                out.append(self._read(f))
            except (OSError, ValueError) as exc:
                self.problems.append(f"{f.name}: {exc}")
                log.warning("Skipping preset %s: %s", f, exc)
        out.sort(key=lambda p: p.name.casefold())
        return out

    @staticmethod
    def _read(f: Path) -> Preset:
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("the preset has no name")
        cells = data.get("cells")
        if not isinstance(cells, list):
            raise ValueError("'cells' must be a list")
        saved = data.get("saved")
        return Preset(name.strip(), [PresetCell.from_json(c) for c in cells], saved if isinstance(saved, str) else "", f)

    def find(self, name: str) -> Preset | None:
        key = name.strip().casefold()
        return next((p for p in self.list() if p.name.casefold() == key), None)

    # ------------------------------------------------------------------ writing
    def save(self, name: str, cells: list[PresetCell], overwrite: bool = False) -> str | None:
        """Save under ``name``. Returns an error message or None. An existing preset of that name is only replaced
        with overwrite=True (the UI asks first)."""
        name = name.strip()
        if not name:
            return "Give the preset a name."
        if not cells:
            return "There are no cells to save."
        existing = self.find(name)
        if existing is not None and not overwrite:
            return f"A preset named “{existing.name}” already exists."
        target = existing.file if existing is not None and existing.file is not None else self._free_file(name)
        data = {"format": FORMAT, "name": name, "saved": datetime.now().replace(microsecond=0).isoformat(),
                "cells": [c.to_json() for c in cells]}
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{uuid.uuid4().hex[:8]}.tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp, target)
        except OSError as exc:
            return f"Could not save the preset: {exc}"
        return None

    def _free_file(self, name: str) -> Path:
        stem = safe_stem(name)
        taken = {p.name.casefold() for p in self.folder.glob("*")} if self.folder.is_dir() else set()
        candidate, n = f"{stem}.json", 2
        while candidate.casefold() in taken:
            candidate = f"{stem} ({n}).json"
            n += 1
        return self.folder / candidate

    def delete(self, preset: Preset) -> str | None:
        try:
            if preset.file is not None:
                preset.file.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            return f"Could not delete the preset: {exc}"
        return None

    def rename(self, preset: Preset, new_name: str) -> str | None:
        new_name = new_name.strip()
        if not new_name:
            return "Give the preset a name."
        clash = self.find(new_name)
        if clash is not None and clash.file != preset.file:
            return f"A preset named “{clash.name}” already exists."
        return self.save_as_same_file(preset, new_name)

    def save_as_same_file(self, preset: Preset, new_name: str) -> str | None:
        data = {"format": FORMAT, "name": new_name, "saved": preset.saved,
                "cells": [c.to_json() for c in preset.cells]}
        try:
            tmp = preset.file.with_name(f".{uuid.uuid4().hex[:8]}.tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp, preset.file)
        except OSError as exc:
            return f"Could not rename the preset: {exc}"
        return None


@dataclass
class ResolvedCell:
    spec: dict                      # keyword arguments for SessionQueue.add_cell
    missing_tool: str = ""          # the tool's folder name when it cannot be found here, else ""


def resolve_cells(preset: Preset, registry) -> list[ResolvedCell]:
    """Turn a preset into cells to add. A tool cell whose tool is missing is still returned (flagged): the caller
    decides whether to add it (it then shows as an unavailable tool) or to skip it."""
    out: list[ResolvedCell] = []
    for c in preset.cells:
        if c.kind is CellKind.CODE:
            out.append(ResolvedCell(dict(kind=CellKind.CODE, label=c.label, command=c.command, language=c.language,
                                         answers_text=c.answers)))
        else:
            tool = registry.resolve(c.tool, c.source)
            out.append(ResolvedCell(dict(kind=CellKind.TOOL, label=c.label, tool_id=tool.id if tool else c.tool,
                                         answers_text=c.answers), "" if tool else c.tool))
    return out
