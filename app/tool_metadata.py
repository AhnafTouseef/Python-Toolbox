"""Tool metadata (tool.json): generation, validation, repair and safe saving.

Pure Python, no Qt, so it can be tested without a display.

tool.json holds exactly four application-managed attributes (name, description,
icon, color).  Unknown extra fields are preserved untouched.  Valid
user-customised values are never overwritten; only missing/invalid fields are
repaired.
"""

from __future__ import annotations

import colorsys
import json
import logging
import os
import random
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
MANAGED_KEYS = ("name", "description", "icon", "color")
METADATA_FILENAME = "tool.json"


@dataclass
class ToolMetadata:
    name: str
    description: str
    icon: str
    color: str

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "icon": self.icon, "color": self.color}


@dataclass
class LoadResult:
    metadata: ToolMetadata
    extras: dict = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)   # user-facing notices (warnings/errors)


# --- helpers ------------------------------------------------------------------
def prettify_name(dirname: str) -> str:
    """`VideoProcessor` -> `Video Processor`, `data_analyzer` -> `Data Analyzer`, `PDFTool` -> `PDF Tool`."""
    s = re.sub(r"[_\-.]+", " ", dirname)
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)          # fooBar -> foo Bar
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)        # PDFTool -> PDF Tool
    words = [w[0].upper() + w[1:] if w.islower() else w for w in s.split()]
    return " ".join(words) or dirname


def is_valid_hex(value: object) -> bool:
    return isinstance(value, str) and HEX_RE.match(value) is not None


def normalize_hex(value: str) -> str:
    return value.upper()


def chroma(hex_color: str) -> float:
    """max(r,g,b) - min(r,g,b) in 0..1; ~0 for white/grey/black."""
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return max(r, g, b) - min(r, g, b)


def is_near_neutral(hex_color: str, min_chroma: float = 0.2) -> bool:
    return chroma(hex_color) < min_chroma


def random_accent_color(rng: random.Random | None = None) -> str:
    """Random, clearly coloured, bright-enough accent. Never white/grey/near-neutral."""
    rng = rng or random
    h = rng.random()
    s = rng.uniform(0.50, 0.85)
    v = rng.uniform(0.78, 0.98)
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return "#{:02X}{:02X}{:02X}".format(round(r * 255), round(g * 255), round(b * 255))


def default_metadata(dirname: str, generic_icon: str, rng: random.Random | None = None) -> ToolMetadata:
    return ToolMetadata(name=prettify_name(dirname), description="", icon=generic_icon,
                        color=random_accent_color(rng))


def validate_fields(name: object, description: object, icon: object, color: object,
                    icon_exists: Callable[[object], bool]) -> list[str]:
    """Human-readable problems with a set of field values (empty list == valid)."""
    problems: list[str] = []
    if not isinstance(name, str) or not name.strip():
        problems.append("Name must not be empty.")
    if not isinstance(description, str):
        problems.append("Description must be text.")
    if not icon_exists(icon):
        problems.append(f"Icon {icon!r} is not in the bundled icon library.")
    if not is_valid_hex(color):
        problems.append(f"Color {color!r} is not a valid hex color (expected #RRGGBB, e.g. #A78BFA).")
    return problems


def _dump(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def atomic_write_json(path: Path, data: dict) -> None:
    """Write via a temp file + replace. Raises OSError on failure (caller reports it)."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(_dump(data))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# --- load / repair ------------------------------------------------------------
def load_or_create(tool_dir: Path, icon_exists: Callable[[object], bool], generic_icon: str,
                   rng: random.Random | None = None) -> LoadResult:
    path = tool_dir / METADATA_FILENAME
    dirname = tool_dir.name
    issues: list[str] = []

    if not path.exists():
        meta = default_metadata(dirname, generic_icon, rng)
        try:
            atomic_write_json(path, meta.to_dict())
        except OSError as exc:
            issues.append(f"{dirname}: could not save a new tool.json ({exc}). "
                          f"Defaults are used for this session only and were NOT saved.")
        return LoadResult(meta, {}, issues)

    # Read + parse -----------------------------------------------------------
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        # Unreadable is not the same as malformed: never overwrite what we could not read.
        issues.append(f"{dirname}: tool.json could not be read ({exc}). "
                      f"Defaults are used for this session; the file was left untouched.")
        return LoadResult(default_metadata(dirname, generic_icon, rng), {}, issues)
    except UnicodeDecodeError as exc:
        return _regenerate_malformed(tool_dir, path, generic_icon, rng, f"not valid UTF-8 text ({exc})")

    try:
        data = json.loads(raw)
    except ValueError as exc:
        return _regenerate_malformed(tool_dir, path, generic_icon, rng, f"malformed JSON ({exc})")
    if not isinstance(data, dict):
        return _regenerate_malformed(tool_dir, path, generic_icon, rng,
                                     f"expected a JSON object, found {type(data).__name__}")

    # Validate field by field; repair only what is missing/invalid -------------
    defaults = default_metadata(dirname, generic_icon, rng)
    repaired = dict(data)
    changed = False

    def check(key: str, ok: bool, fallback: str) -> str:
        nonlocal changed
        if key not in data:
            log.info("%s: tool.json is missing %r; adding it", dirname, key)
            repaired[key] = fallback
            changed = True
            return fallback
        if not ok:
            issues.append(f"{dirname}: tool.json field '{key}' has an invalid value ({data[key]!r}); "
                          f"replaced with {fallback!r}.")
            repaired[key] = fallback
            changed = True
            return fallback
        return data[key]

    name = check("name", isinstance(data.get("name"), str) and bool(data["name"].strip()), defaults.name)
    desc = check("description", isinstance(data.get("description"), str), defaults.description)
    icon = check("icon", icon_exists(data.get("icon")), defaults.icon)
    color = check("color", is_valid_hex(data.get("color")), defaults.color)

    if changed:
        try:
            atomic_write_json(path, repaired)
        except OSError as exc:
            issues.append(f"{dirname}: repaired tool.json could not be saved ({exc}). "
                          f"The repair applies for this session only.")

    extras = {k: v for k, v in data.items() if k not in MANAGED_KEYS}
    return LoadResult(ToolMetadata(name, desc, icon, color), extras, issues)


def _regenerate_malformed(tool_dir: Path, path: Path, generic_icon: str,
                          rng: random.Random | None, reason: str) -> LoadResult:
    dirname = tool_dir.name
    meta = default_metadata(dirname, generic_icon, rng)
    backup = path.with_name(f"{path.name}.malformed-{time.strftime('%Y%m%d-%H%M%S')}.bak")
    n = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.malformed-{time.strftime('%Y%m%d-%H%M%S')}-{n}.bak")
        n += 1
    try:
        shutil.copy2(path, backup)
    except OSError as exc:
        # Cannot preserve the user's file -> do not destroy it.
        return LoadResult(meta, {}, [
            f"{dirname}: tool.json is unusable ({reason}) and no backup could be made ({exc}). "
            f"The file was left untouched; defaults are used for this session."])
    issues = [f"{dirname}: tool.json is unusable ({reason}). A backup was saved as {backup.name} "
              f"and default metadata was generated."]
    try:
        atomic_write_json(path, meta.to_dict())
    except OSError as exc:
        issues.append(f"{dirname}: default tool.json could not be written ({exc}); defaults apply for this session only.")
    return LoadResult(meta, {}, issues)


def save_metadata(tool_dir: Path, meta: ToolMetadata) -> None:
    """Persist edited metadata, keeping unknown fields from the current file. Raises OSError."""
    path = tool_dir / METADATA_FILENAME
    merged: dict = {}
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(current, dict):
            merged = {k: v for k, v in current.items() if k not in MANAGED_KEYS}
    except (OSError, ValueError):
        pass
    data = meta.to_dict()
    data.update(merged)
    atomic_write_json(path, data)
