"""Squarified treemap layout: value-weighted rectangles that tile a space
completely, so "which group is my ISK actually in" is answerable by eye.

Kept apart from the view that draws it the same way queries.py, pricing.py
and fitting.py are -- this is arithmetic on (label, value) pairs with no Qt
involved, so the awkward parts (does it tile exactly? do the areas actually
match the values?) can be asserted in unit tests rather than squinted at.

The layout is the squarified algorithm of Bruls, Huizing and van Wijk,
"Squarified Treemaps" (Proc. Joint Eurographics/IEEE TVCG Symposium on
Visualization, 2000). The obvious alternative -- "slice and dice", which
just cuts the strip repeatedly along alternating axes -- is much simpler but
produces slivers: with 50 items you get 1-pixel-wide rectangles whose area
is impossible to judge and whose label cannot be drawn. Squarified instead
grows a row of tiles for as long as adding the next tile improves the row's
worst aspect ratio, then starts a new row in the leftover space, which keeps
tiles near-square. That "worst aspect ratio" test is the `_worst` function
below, straight from section 3 of the paper.

Two deliberate departures from a textbook treemap:

  * Non-positive values are dropped rather than laid out. A lot of EVE assets
    have no price at all (see pricing.py's 'none' source), and a zero-area
    tile is not something a user can click, hover or see -- it would just be
    an invisible participant in the layout.

  * The long tail can be rolled into a single "Other" tile via top_n. Asset
    data is heavily skewed: grouping by item group gives several hundred
    groups of which maybe twenty are visible at any sane window size. Without
    the rollup the remainder is drawn as a band of unlabelled slivers that
    costs paint time and tells nobody anything.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Tile:
    """One laid-out rectangle, in the same coordinate space as the width and
    height handed to layout(). Origin is top-left, y grows downward, which is
    what every GUI toolkit's paint event expects."""

    label: str
    value: float
    x: float
    y: float
    width: float
    height: float

    @property
    def area(self) -> float:
        return self.width * self.height

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px < self.x + self.width and self.y <= py < self.y + self.height


def _worst(row: list[float], side: float) -> float:
    """Worst aspect ratio in a row of areas laid along a strip of the given
    side length -- the paper's worst(R, w). Returns infinity for a degenerate
    row so that any real candidate row beats it."""
    total = math.fsum(row)
    if total <= 0 or side <= 0:
        return math.inf
    side_sq = side * side
    total_sq = total * total
    return max(side_sq * max(row) / total_sq, total_sq / (side_sq * min(row)))


def _squarify(areas: list[float], x: float, y: float, width: float, height: float):
    """Areas (already scaled to fill width*height) -> [(x, y, w, h), ...].

    Iterative rather than the paper's recursion: grouping by item group can
    produce several hundred rows, and one Python stack frame per row is a
    RecursionError waiting for a user with a lot of stuff.
    """
    placed: list[tuple[float, float, float, float]] = []
    index = 0
    count = len(areas)

    while index < count:
        if width <= 0 or height <= 0:
            # Nothing left to fill. Emit the remainder as empty rectangles so
            # the caller still gets one tile per input value and indexes line
            # up, rather than silently returning a shorter list.
            placed.extend((x, y, 0.0, 0.0) for _ in range(count - index))
            break

        side = min(width, height)
        row = [areas[index]]
        end = index + 1
        # Keep adding tiles while doing so makes the row's worst tile squarer.
        while end < count and _worst(row + [areas[end]], side) <= _worst(row, side):
            row.append(areas[end])
            end += 1

        total = math.fsum(row)
        if width >= height:
            # Shorter side is the height, so the row becomes a column strip
            # down the left edge and the leftover space is to its right.
            strip = total / height
            cursor = y
            for area in row:
                tile_height = area / strip if strip > 0 else 0.0
                placed.append((x, cursor, strip, tile_height))
                cursor += tile_height
            x += strip
            width -= strip
        else:
            strip = total / width
            cursor = x
            for area in row:
                tile_width = area / strip if strip > 0 else 0.0
                placed.append((cursor, y, tile_width, strip))
                cursor += tile_width
            y += strip
            height -= strip

        index = end

    return placed


def roll_up(
    items: Sequence[tuple[str, float]],
    top_n: int | None = None,
) -> list[tuple[str, float]]:
    """Drop non-positive values, sort by value descending, and optionally fold
    everything past top_n into a single "Other" entry.

    Split out from layout() because it is the part with a judgement call in
    it, and a caller that wants the numbers (a summary line, a test) should
    not have to lay out rectangles to get them.
    """
    ranked = sorted(
        ((label, float(value)) for label, value in items if value and float(value) > 0),
        key=lambda pair: pair[1],
        reverse=True,
    )
    if top_n is None or top_n <= 0 or len(ranked) <= top_n:
        return ranked

    head = ranked[:top_n]
    tail = ranked[top_n:]
    head.append((f"Other ({len(tail):,} more)", math.fsum(value for _, value in tail)))
    # The rolled-up total can outweigh entries already in the head, and a
    # treemap that is not in descending order lays out badly -- squarified
    # assumes sorted input.
    head.sort(key=lambda pair: pair[1], reverse=True)
    return head


def layout(
    items: Sequence[tuple[str, float]],
    width: float,
    height: float,
    top_n: int | None = None,
) -> list[Tile]:
    """(label, value) pairs -> tiles filling a width x height rectangle.

    Tiles come back largest first, which is also roughly top-left first, so a
    caller drawing them in order draws the important ones first.
    """
    ranked = roll_up(items, top_n)
    if not ranked or width <= 0 or height <= 0:
        return []

    total_value = math.fsum(value for _, value in ranked)
    if total_value <= 0:
        return []

    # Scale values into areas that exactly fill the box, so "share of the
    # rectangle" and "share of the ISK" are the same number by construction.
    scale = (width * height) / total_value
    areas = [value * scale for _, value in ranked]

    rects = _squarify(areas, 0.0, 0.0, width, height)
    return [
        Tile(label=label, value=value, x=rx, y=ry, width=rw, height=rh)
        # strict=True asserts the invariant _squarify() is written to keep:
        # exactly one rectangle per input value, including the degenerate
        # zero-area ones it pads with.
        for (label, value), (rx, ry, rw, rh) in zip(ranked, rects, strict=True)
    ]
