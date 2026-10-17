from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

from PySide6.QtCore import QByteArray, QBuffer, QSize
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


class IconManager:
    """Local Lucide-style SVG icon manager."""

    def __init__(self, assets_dir: Path | None = None):
        self.assets_dir = (assets_dir or Path(__file__).resolve().parent.parent / "assets" / "icons")
        self._cache: Dict[str, str] = {}
        self._names: List[str] = []
        self.reload()

    def reload(self) -> None:
        self._cache = {}
        self._names = []
        for svg_path in sorted(self.assets_dir.glob("*.svg")):
            name = svg_path.stem
            self._cache[name] = svg_path.read_text(encoding="utf-8")
            self._names.append(name)

    @property
    def names(self) -> List[str]:
        return list(self._names)

    def has_icon(self, name: str) -> bool:
        return bool(name) and name in self._cache

    def icon_svg(self, name: str, color: str = "#A78BFA", size: int = 24) -> str:
        if not self.has_icon(name):
            name = "wrench"
        svg = self._cache.get(name, self._cache.get("wrench", ""))
        svg = re.sub(r'fill="[^"]*"', 'fill="currentColor"', svg, count=1)
        svg = re.sub(r'stroke="[^"]*"', 'stroke="currentColor"', svg)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
            f'viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round">'
            f'{svg.split(">", 1)[1].split("</svg>", 1)[0]}'
            f'</svg>'
        )

    def pixmap_for(self, name: str, color: str = "#A78BFA", size: int = 32) -> QPixmap:
        svg = self.icon_svg(name, color, size)
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        image = QImage(size, size, QImage.Format_ARGB32)
        image.fill(0x00000000)
        painter = QPainter(image)
        renderer.render(painter)
        painter.end()
        pixmap = QPixmap.fromImage(image)
        return pixmap


def load_icon_names(assets_dir: Path | None = None) -> List[str]:
    return IconManager(assets_dir).names
