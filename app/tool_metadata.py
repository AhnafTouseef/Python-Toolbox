from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

from app.icon_manager import IconManager


REQUIRED_FIELDS = {"name", "description", "icon", "color"}
HEX_COLOR_RE = re.compile(r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")


def prettify_folder_name(folder_name: str) -> str:
    name = folder_name.replace("_", " ").replace("-", " ")
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", name).split()
    return " ".join(word.capitalize() for word in words if word)


def generate_random_color() -> str:
    while True:
        hue = random.randint(0, 359)
        sat = random.randint(45, 95)
        val = random.randint(45, 85)
        r, g, b = __hsv_to_rgb(hue, sat / 100, val / 100)
        if (r, g, b) == (255, 255, 255):
            continue
        if max(r, g, b) < 180 and min(r, g, b) > 130:
            continue
        if abs(r - g) < 8 and abs(g - b) < 8 and abs(r - b) < 8:
            continue
        return f"#{r:02X}{g:02X}{b:02X}"


def __hsv_to_rgb(h: int, s: float, v: float) -> Tuple[int, int, int]:
    c = v * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = v - c

    if 0 <= h < 60:
        r, g, b = c, x, 0
    elif 60 <= h < 120:
        r, g, b = x, c, 0
    elif 120 <= h < 180:
        r, g, b = 0, c, x
    elif 180 <= h < 240:
        r, g, b = 0, x, c
    elif 240 <= h < 300:
        r, g, b = x, 0, c
    else:
        r, g, b = c, 0, x
    return tuple(int((channel + m) * 255) for channel in (r, g, b))


def get_icon_names() -> Iterable[str]:
    return IconManager().names


def valid_hex_color(value: Any) -> bool:
    return isinstance(value, str) and bool(HEX_COLOR_RE.match(value))


def valid_icon_name(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return value in set(get_icon_names())


def validate_tool_metadata(metadata: Dict[str, Any], tool_dir: Path) -> Dict[str, Any]:
    safe = {"name": prettify_folder_name(tool_dir.name), "description": "", "icon": "wrench", "color": generate_random_color()}
    if isinstance(metadata, dict):
        for key, value in metadata.items():
            if key not in REQUIRED_FIELDS:
                continue
            if key == "name":
                if isinstance(value, str) and value.strip():
                    safe["name"] = value.strip()
                else:
                    safe["name"] = prettify_folder_name(tool_dir.name)
            elif key == "description":
                safe["description"] = value if isinstance(value, str) else ""
            elif key == "icon":
                safe["icon"] = value if valid_icon_name(value) else "wrench"
            elif key == "color":
                safe["color"] = value if valid_hex_color(value) else generate_random_color()
    return safe


def ensure_metadata(tool_dir: Path) -> Dict[str, Any]:
    metadata_path = tool_dir / "tool.json"
    if not metadata_path.exists():
        defaults = {
            "name": prettify_folder_name(tool_dir.name),
            "description": "",
            "icon": "wrench",
            "color": generate_random_color(),
        }
        write_metadata(metadata_path, defaults)
        return defaults

    try:
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        backup_path = metadata_path.with_suffix(".json.bak")
        try:
            if metadata_path.exists():
                backup_path.write_bytes(metadata_path.read_bytes())
        except OSError:
            backup_path = None
        defaults = {
            "name": prettify_folder_name(tool_dir.name),
            "description": "",
            "icon": "wrench",
            "color": generate_random_color(),
        }
        write_metadata(metadata_path, defaults)
        return defaults

    valid = validate_tool_metadata(raw, tool_dir)
    if valid != raw:
        write_metadata(metadata_path, valid)
    return valid


def write_metadata(path: Path, metadata: Dict[str, Any]) -> None:
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"name": metadata.get("name", prettify_folder_name(path.parent.name)),
               "description": metadata.get("description", ""),
               "icon": metadata.get("icon", "wrench"),
               "color": metadata.get("color", generate_random_color())}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def discover_tools(tools_dir: Path) -> list[Path]:
    if not tools_dir.exists():
        return []
    discovered = []
    for child in sorted(tools_dir.iterdir()):
        if child.is_dir() and (child / "main.py").exists():
            discovered.append(child)
    return discovered
