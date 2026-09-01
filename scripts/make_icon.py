#!/usr/bin/env python3
"""Draw the EVE Booty app icon and pack it into a multi-resolution .ico.

    uv run python scripts/make_icon.py

Committed as a script rather than only as the binary so the mark can be
adjusted without anyone needing a copy of a design tool, and so a diff to the
icon is a readable diff rather than a blob.

The mark is a treasure chest: "booty", drawn in ISK gold on a deep-space
ground. Detail is dropped below 32 px rather than scaled down -- a lock plate
and coin sit at 256 px and turn to mud at 16, so the small sizes are drawn as
their own simplified geometry instead of being a resampled big one. That is
also why every size is rendered natively rather than downscaled from one
master: a 16 px chest wants thicker strokes than a shrunk 256 px chest has.

Qt can write a single-image .ico, but Windows wants several sizes in the file
so the shell can pick per context (16 px tree view, 32 px taskbar, 256 px
tile). The ICO container is assembled here by hand: it is a 6-byte header, a
16-byte directory entry per image, then the payloads, and since Vista those
payloads may simply be PNGs.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QBuffer, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QBrush,
    QColor,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QApplication  # noqa: E402

OUT_DIR = ROOT / "src" / "evasset" / "assets"
SIZES = (16, 32, 48, 64, 128, 256)

# Deep space behind, ISK gold in front.
SPACE_TOP = "#16243A"
SPACE_BOTTOM = "#0B1119"
GOLD_LIGHT = "#FFD46B"
GOLD_MID = "#F2B33D"
GOLD_DEEP = "#C8871B"
BAND = "#5C3E12"
OUTLINE = "#241703"
LOCK = "#FFEFC0"


def _ground(painter: QPainter, size: int) -> None:
    """Rounded-square field. Not a full-bleed square: at small sizes Windows
    draws icons hard against other UI, and the rounded inset is what stops it
    reading as a screenshot of something rather than an icon."""
    gradient = QLinearGradient(0, 0, 0, size)
    gradient.setColorAt(0.0, QColor(SPACE_TOP))
    gradient.setColorAt(1.0, QColor(SPACE_BOTTOM))
    painter.setBrush(QBrush(gradient))
    painter.setPen(Qt.NoPen)
    radius = size * 0.22
    painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)


def _chest(painter: QPainter, size: int, detailed: bool) -> None:
    u = size / 100.0  # work in percentages of the icon, then scale

    body = QRectF(18 * u, 46 * u, 64 * u, 34 * u)
    lid = QRectF(18 * u, 26 * u, 64 * u, 26 * u)

    stroke = QPen(QColor(OUTLINE), max(1.0, size * 0.028))
    stroke.setJoinStyle(Qt.RoundJoin)

    # Lid: a dome, so the silhouette is a chest and not a suitcase.
    lid_path = QPainterPath()
    lid_path.moveTo(lid.left(), lid.bottom())
    lid_path.lineTo(lid.left(), lid.top() + lid.height() * 0.45)
    lid_path.quadTo(
        QPointF(lid.center().x(), lid.top() - lid.height() * 0.35),
        QPointF(lid.right(), lid.top() + lid.height() * 0.45),
    )
    lid_path.lineTo(lid.right(), lid.bottom())
    lid_path.closeSubpath()

    lid_fill = QLinearGradient(0, lid.top(), 0, lid.bottom())
    lid_fill.setColorAt(0.0, QColor(GOLD_LIGHT))
    lid_fill.setColorAt(1.0, QColor(GOLD_MID))
    painter.setBrush(QBrush(lid_fill))
    painter.setPen(stroke)
    painter.drawPath(lid_path)

    body_fill = QLinearGradient(0, body.top(), 0, body.bottom())
    body_fill.setColorAt(0.0, QColor(GOLD_MID))
    body_fill.setColorAt(1.0, QColor(GOLD_DEEP))
    painter.setBrush(QBrush(body_fill))
    painter.drawRoundedRect(body, 3 * u, 3 * u)

    # The band between lid and body is what makes it read as "opens".
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(BAND))
    painter.drawRect(QRectF(body.left(), body.top() - 3 * u, body.width(), 6 * u))

    if not detailed:
        # 16/32 px: a lock here would be three muddy pixels. The band alone
        # carries the read.
        return

    painter.setBrush(QColor(LOCK))
    painter.setPen(QPen(QColor(OUTLINE), max(1.0, size * 0.02)))
    plate = QRectF(44 * u, 44 * u, 12 * u, 14 * u)
    painter.drawRoundedRect(plate, 2 * u, 2 * u)
    painter.setBrush(QColor(OUTLINE))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(QPointF(50 * u, 50 * u), 2.0 * u, 2.0 * u)

    # Vertical straps, only where there is room to see them.
    painter.setBrush(QColor(BAND))
    for x in (26, 68):
        painter.drawRect(QRectF(x * u, lid.top() + 6 * u, 5 * u, 40 * u))


def render(size: int) -> QImage:
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    _ground(painter, size)
    _chest(painter, size, detailed=size >= 48)
    painter.end()
    return image


def png_bytes(image: QImage) -> bytes:
    # QBuffer() with its own internal buffer, not QBuffer(QByteArray()).
    # The latter keeps a pointer to a Python temporary that is collected the
    # moment the constructor returns, and the process dies inside Qt with no
    # traceback to explain it.
    buffer = QBuffer()
    buffer.open(QBuffer.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(buffer.data())


def write_ico(images: list[QImage], path: Path) -> None:
    """ICONDIR + one ICONDIRENTRY per image + PNG payloads."""
    payloads = [png_bytes(im) for im in images]
    header = struct.pack("<HHH", 0, 1, len(images))  # reserved, type=icon, count
    offset = len(header) + 16 * len(images)
    entries, blob = b"", b""
    for image, payload in zip(images, payloads, strict=True):
        side = image.width()
        entries += struct.pack(
            "<BBBBHHII",
            0 if side >= 256 else side,   # 0 means 256 in the ICO format
            0 if side >= 256 else side,
            0, 0, 1, 32,
            len(payload),
            offset,
        )
        blob += payload
        offset += len(payload)
    path.write_bytes(header + entries + blob)


def main() -> int:
    QApplication(["make_icon", "-platform", "offscreen"])
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    images = [render(size) for size in SIZES]
    for size, image in zip(SIZES, images, strict=True):
        if size in (256, 64):
            image.save(str(OUT_DIR / f"booty-{size}.png"), "PNG")
    images[-1].save(str(OUT_DIR / "booty.png"), "PNG")
    write_ico(images, OUT_DIR / "booty.ico")

    for name in ("booty.png", "booty-64.png", "booty-256.png", "booty.ico"):
        path = OUT_DIR / name
        print(f"  {path.relative_to(ROOT)}  {path.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
