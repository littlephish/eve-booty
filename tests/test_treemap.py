"""Treemap layout maths. No Qt, no database.

The three properties worth asserting about a treemap are the ones a reader
trusts without being told: the tiles fill the box, they do not overlap, and a
tile twice the area is worth twice the ISK. Everything else here is an edge
case that would otherwise crash the paint event.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from evasset import treemap

BOX = (800.0, 500.0)


def _tiles(values, **kwargs):
    items = [(f"item {i}", v) for i, v in enumerate(values)]
    return treemap.layout(items, *BOX, **kwargs)


def test_tiles_fill_the_box_exactly():
    tiles = _tiles([100, 60, 40, 30, 20, 10, 5, 5, 3, 1])
    covered = sum(t.area for t in tiles)
    assert covered == pytest.approx(BOX[0] * BOX[1], rel=1e-9)


def test_tiles_do_not_overlap():
    tiles = _tiles([100, 60, 40, 30, 20, 10, 5, 5, 3, 1])
    for i, a in enumerate(tiles):
        for b in tiles[i + 1:]:
            overlap_x = min(a.x + a.width, b.x + b.width) - max(a.x, b.x)
            overlap_y = min(a.y + a.height, b.y + b.height) - max(a.y, b.y)
            assert overlap_x <= 1e-9 or overlap_y <= 1e-9, f"{a.label} overlaps {b.label}"


def test_tiles_stay_inside_the_box():
    for t in _tiles([50, 25, 12, 6, 3, 2, 1]):
        assert t.x >= -1e-9 and t.y >= -1e-9
        assert t.x + t.width <= BOX[0] + 1e-9
        assert t.y + t.height <= BOX[1] + 1e-9


def test_area_is_proportional_to_value():
    """The whole point of the picture: a tile's share of the box is its share
    of the ISK."""
    values = [100, 60, 40, 30, 20, 10]
    tiles = _tiles(values)
    box_area = BOX[0] * BOX[1]
    for t in tiles:
        assert t.area / box_area == pytest.approx(t.value / sum(values), rel=1e-9)


def test_tiles_come_back_largest_first():
    tiles = _tiles([3, 100, 20, 7])
    assert [t.value for t in tiles] == [100, 20, 7, 3]


def test_squarified_beats_slice_and_dice_on_aspect_ratio():
    """The reason for the algorithm. Twenty equal values across a 800x500 box
    laid out naively would be 40px-wide slivers (aspect 12.5:1); squarified
    should keep every tile within spitting distance of square."""
    tiles = _tiles([1] * 20)
    worst = max(max(t.width / t.height, t.height / t.width) for t in tiles)
    assert worst < 2.0, f"worst aspect ratio {worst:.2f}"


def test_values_that_cannot_be_drawn_are_dropped():
    """Unpriced assets are common -- pricing.py leaves them at 0. A zero-area
    tile cannot be seen, hovered or clicked, so it is not laid out at all."""
    tiles = _tiles([100, 0, 50, -10, 25])
    assert [t.value for t in tiles] == [100, 50, 25]


def test_empty_and_degenerate_input_lay_out_to_nothing():
    """Guards the paint event: an empty database, a collapsed splitter and a
    zero-width tab all reach here."""
    assert treemap.layout([], *BOX) == []
    assert treemap.layout([("a", 0)], *BOX) == []
    assert treemap.layout([("a", 10)], 0, 500) == []
    assert treemap.layout([("a", 10)], 800, 0) == []


def test_a_single_value_takes_the_whole_box():
    # approx, not equality: the areas are scaled through a float division, so
    # the last tile lands a rounding error short of the edge. Sub-pixel, and
    # the paint event rounds to ints anyway.
    (tile,) = treemap.layout([("Ships", 42)], *BOX)
    assert (tile.x, tile.y) == (0.0, 0.0)
    assert tile.width == pytest.approx(BOX[0])
    assert tile.height == pytest.approx(BOX[1])


def test_top_n_rolls_the_tail_into_one_other_tile():
    values = [100, 90, 80, 5, 4, 3, 2, 1]
    tiles = _tiles(values, top_n=3)
    assert [t.label for t in tiles[:3]] == ["item 0", "item 1", "item 2"]
    other = tiles[-1]
    assert other.label == "Other (5 more)"
    assert other.value == pytest.approx(5 + 4 + 3 + 2 + 1)
    assert sum(t.area for t in tiles) == pytest.approx(BOX[0] * BOX[1], rel=1e-9)


def test_a_heavy_other_tile_is_still_placed_in_value_order():
    """Squarified assumes descending input; a rolled-up tail that outweighs
    the head has to be re-sorted or the layout degrades."""
    rolled = treemap.roll_up([("a", 10), ("b", 9), ("c", 8), ("d", 8)], top_n=2)
    assert [label for label, _ in rolled] == ["Other (2 more)", "a", "b"]
    assert rolled[0][1] == pytest.approx(16)


def test_top_n_is_a_no_op_when_everything_already_fits():
    tiles = _tiles([5, 4, 3], top_n=10)
    assert [t.label for t in tiles] == ["item 0", "item 1", "item 2"]
    assert not any("Other" in t.label for t in tiles)


def test_contains_hit_tests_a_point():
    (tile,) = treemap.layout([("Ships", 1)], 100, 50)
    assert tile.contains(0, 0)
    assert tile.contains(99.9, 49.9)
    assert not tile.contains(100, 25)
    assert not tile.contains(-1, 25)


def test_a_long_tail_does_not_recurse_into_a_stack_overflow():
    """Grouping by item group on a real account produces hundreds of rows.
    The layout is iterative precisely so this does not raise."""
    tiles = treemap.layout([(f"g{i}", 1000 - i) for i in range(900)], *BOX)
    assert len(tiles) == 900
    assert sum(t.area for t in tiles) == pytest.approx(BOX[0] * BOX[1], rel=1e-9)
