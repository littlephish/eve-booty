"""View fit on a corpse.

A corpse has no fitting slots, so there is nothing for FitDialog to read.
The menu entry is offered anyway and shows the contents instead.

Pinned because nothing else exercises this path, and a corpse row is not
something the rest of the suite happens to produce.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtWidgets")

from evasset.ui.assets_view import _CORPSE_GROUP, _is_corpse  # noqa: E402


def row(**kw):
    base = {"grp": "Battleship", "category": "Ship", "item": "Dominix"}
    base.update(kw)
    return base


# ------------------------------------------------------------- what counts
def test_a_corpse_is_recognised():
    assert _is_corpse(row(grp="Biomass", category="Celestial", item="Corpse Male")) is True


def test_both_corpse_types_share_the_group():
    """SDE group 14 holds exactly Corpse Male (25) and Corpse Female (29148),
    which is why the group is matched rather than the two ids."""
    for name in ("Corpse Male", "Corpse Female"):
        assert _is_corpse(row(grp="Biomass", category="Celestial", item=name))


def test_mission_loot_corpses_are_not_capsuleers():
    """Gallente Admiral's Corpse and friends are Commodities. They are props,
    not people, and should behave like any other item."""
    assert _is_corpse(row(grp="Commodities", category="Commodity",
                          item="Gallente Admiral's Corpse")) is False


def test_a_ship_is_not_a_corpse():
    assert _is_corpse(row()) is False


def test_a_row_with_no_group_does_not_crash():
    """An unimported SDE leaves these columns NULL."""
    assert _is_corpse(row(grp=None, category=None)) is False


def test_the_group_name_is_the_sde_one():
    assert _CORPSE_GROUP == "Biomass"


# ------------------------------------------------------------- the animation
def test_the_animation_ships_and_is_playable(qapp_or_skip):
    """QMovie must be able to read it, or the menu entry does nothing. The
    source .webm cannot be used directly: QMovie does not support webm."""
    from PySide6.QtGui import QMovie

    from evasset.ui import chest_reveal

    assert chest_reveal.available(), "the animated WebP is missing from assets/"
    movie = QMovie(str(chest_reveal.ASSET))
    assert movie.isValid()
    assert movie.frameCount() > 1, "not an animation"


def test_the_animation_has_real_transparency(qapp_or_skip):
    """The source is flattened onto solid black, so the alpha is recovered by
    a colour key at build time. Without it this draws a black square over the
    window."""
    from PySide6.QtGui import QMovie

    from evasset.ui import chest_reveal

    movie = QMovie(str(chest_reveal.ASSET))
    movie.start()
    movie.jumpToFrame(min(20, movie.frameCount() - 1))
    image = movie.currentImage()
    movie.stop()

    assert image.hasAlphaChannel()
    assert image.pixelColor(2, 2).alpha() == 0, "corner should be transparent"
    assert image.pixelColor(128, 140).alpha() > 200, "the chest itself should be opaque"


def test_it_is_dismissible_and_does_not_steal_focus(qapp_or_skip):
    """It must never be something to work out how to close, and it must not
    take focus from whatever the user was doing."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget

    from evasset.ui import chest_reveal

    parent = QWidget()
    reveal = chest_reveal.play(parent)
    try:
        assert reveal is not None
        assert reveal.testAttribute(Qt.WA_TranslucentBackground)
        assert reveal.testAttribute(Qt.WA_ShowWithoutActivating)
        assert reveal.windowFlags() & Qt.FramelessWindowHint
    finally:
        if reveal is not None:
            reveal.close()
        parent.close()


def test_a_missing_asset_degrades_quietly(qapp_or_skip, monkeypatch):
    """A build that shipped without the file should do nothing, not raise a
    traceback out of a context menu."""
    from pathlib import Path

    from evasset.ui import chest_reveal

    monkeypatch.setattr(chest_reveal, "ASSET", Path("does-not-exist.webp"))
    assert chest_reveal.available() is False
    assert chest_reveal.play(None) is None
