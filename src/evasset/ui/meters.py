"""The segmented track the inspector's roll meter and the search card's range track share.

One painter for the 14 px on / 1 px gap rhythm, so the meter and the track
cannot drift apart: the card filters the very rolls the meter shows and is
meant to read as the same instrument. Antialiasing is the caller's to
leave off -- the rhythm reads as crisp pixels or not at all.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPainter


def paint_segments(
    painter: QPainter,
    x: int,
    y: int,
    length: int,
    height: int,
    colour: QColor,
    segment: int = 14,
    gap: int = 1,
) -> None:
    """Fill `segment` px blocks `gap` px apart from x across `length` px. The
    last block is clipped to the end rather than dropped, so the track
    always reaches its right edge."""
    end = x + length
    for start in range(x, end, segment + gap):
        painter.fillRect(start, y, min(segment, end - start), height, colour)
