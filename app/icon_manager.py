"""Bundled Lucide icon library: name index, tinted rendering and caching.

Icons are plain SVG files in ``assets/icons``.  Lucide icons are stroke-only and
use ``currentColor``; tinting is done by substituting the colour in the SVG text
before rendering, so the shape is independent of the accent colour and nothing
needs the network.
"""

from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

log = logging.getLogger(__name__)

GENERIC_ICON = "wrench"
_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


class IconManager:
    def __init__(self, icons_dir: Path, tags_file: Path | None = None, cache_size: int = 1024):
        self.icons_dir = Path(icons_dir)
        self._tags_file = tags_file
        self._tags: dict[str, list[str]] | None = None
        self._cache: OrderedDict[tuple, QPixmap] = OrderedDict()
        self._cache_size = cache_size
        self._svg_cache: OrderedDict[str, str] = OrderedDict()
        self.names: list[str] = self._scan()
        self._nameset = set(self.names)
        if GENERIC_ICON not in self._nameset:
            log.error("Generic icon %r is missing from %s", GENERIC_ICON, self.icons_dir)

    # -- index -------------------------------------------------------------
    def _scan(self) -> list[str]:
        try:
            return sorted(p.stem for p in self.icons_dir.glob("*.svg"))
        except OSError as exc:
            log.error("Cannot read icon directory %s: %s", self.icons_dir, exc)
            return []

    def has(self, name: object) -> bool:
        return isinstance(name, str) and name in self._nameset

    @property
    def generic(self) -> str:
        if GENERIC_ICON in self._nameset:
            return GENERIC_ICON
        return self.names[0] if self.names else GENERIC_ICON

    def tags(self, name: str) -> list[str]:
        """Search keywords for the picker (loaded lazily from the bundled tags file)."""
        if self._tags is None:
            self._tags = {}
            if self._tags_file and self._tags_file.exists():
                try:
                    self._tags = json.loads(self._tags_file.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    log.warning("Could not load icon tags: %s", exc)
        return self._tags.get(name, [])

    # -- rendering ---------------------------------------------------------
    def _svg_text(self, name: str) -> str | None:
        if name in self._svg_cache:
            self._svg_cache.move_to_end(name)
            return self._svg_cache[name]
        try:
            text = (self.icons_dir / f"{name}.svg").read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("Cannot read icon %r: %s", name, exc)
            return None
        self._svg_cache[name] = text
        if len(self._svg_cache) > 256:
            self._svg_cache.popitem(last=False)
        return text

    @staticmethod
    def _dpr() -> float:
        screen = QGuiApplication.primaryScreen()
        return float(screen.devicePixelRatio()) if screen else 1.0

    def pixmap(self, name: str, color: str, size: int) -> QPixmap:
        """Tinted square pixmap of logical `size` px. Falls back to the generic icon."""
        if not self.has(name):
            name = self.generic
        if not _HEX_RE.match(color or ""):
            color = "#FFFFFF"
        dpr = self._dpr()
        key = (name, color.upper(), size, dpr)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached

        px = QPixmap(int(size * dpr), int(size * dpr))
        px.setDevicePixelRatio(dpr)
        px.fill(Qt.GlobalColor.transparent)
        text = self._svg_text(name)
        if text is not None:
            renderer = QSvgRenderer(QByteArray(text.replace("currentColor", color).encode("utf-8")))
            if renderer.isValid():
                painter = QPainter(px)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                renderer.render(painter, QRectF(0, 0, size, size))
                painter.end()
            else:
                log.warning("Icon %r is not a valid SVG", name)
        self._cache[key] = px
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return px

    def icon(self, name: str, color: str, size: int = 18) -> QIcon:
        return QIcon(self.pixmap(name, color, size))

    def image(self, name: str, color: str, size: int) -> QImage:
        return self.pixmap(name, color, size).toImage()
