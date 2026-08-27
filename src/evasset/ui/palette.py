"""Theme-aware status colours.

Two problems this solves, both measured rather than eyeballed.

A hardcoded hex cannot satisfy both themes. #c0392b reads at 5.44:1 on white
-- fine -- and 3.07:1 on a dark window, which is below the 4.5:1 WCAG AA
threshold for normal text. Pick a red that works on dark and it washes out on
light. So each level carries two colours and the right one is chosen from the
palette actually in force.

Qt's own "subdued text" roles are worse than they look. palette(mid) is
#b8b8b8 against a white Base: 1.99:1, which does not even clear the 3:1 floor
for large text, and it was what every hint, footer and caption in this app was
drawn in. palette(shadow) measures 4.54:1 and moves with the theme, so
SECONDARY_TEXT points at that instead.

Ratios below are against Base (#ffffff light, #1e1e1e dark) and the
alternating row colour half a step off it, since table text sits on both.
"""

from __future__ import annotations

from PySide6.QtGui import QBrush, QColor, QPalette
from PySide6.QtWidgets import QApplication

NORMAL, WARN, CRITICAL = 0, 1, 2

# Qt stylesheet colour for anything secondary -- hints, footers, captions.
# palette(mid) is 1.99:1 and fails; this is 4.54:1 and passes AA.
SECONDARY_TEXT = "palette(shadow)"

#                     light bg   ratio   dark bg    ratio
_STATUS = {
    WARN:     ("#8A5A00",  # 5.93:1   "#E8A33D",  # 7.73:1
               "#E8A33D"),
    CRITICAL: ("#B3261E",  # 6.54:1   "#F87171",  # 6.03:1
               "#F87171"),
}

# Not a status level -- used for gains in the net worth delta.
POSITIVE = ("#2E7D1E", "#7BD96A")   # 5.16:1 light, 9.51:1 dark

# Background tints for the fit dialog's slot lines, keyed by SDE meta group
# id (sde_types.meta_group_id). The mapping follows the game client's own
# colour language -- faction green, officer purple, deadspace blue, abyssal
# red -- because that is what an EVE player's eye is already trained on;
# every other meta group (T1/T2/T3, storyline, structure, ...) deliberately
# gets no tint at all.
#
# These are backgrounds under default theme text, not text colours, so the
# contrast requirement runs the other way from _STATUS above: the theme's
# text colour must stay AA-readable on top of them. That means pale washes
# on light and dark washes on dark -- a colour saturated enough to read as
# "green text" would fail as "green background". Measured in
# tests/test_contrast.py like everything else here.
META_FACTION, META_OFFICER, META_DEADSPACE, META_ABYSSAL = 4, 5, 6, 15

#                          light bg    dark bg
RARITY_TINTS = {
    META_FACTION:   ("#DFF0DF", "#1E3423"),
    META_OFFICER:   ("#ECE0F5", "#33244A"),
    META_DEADSPACE: ("#DFEAF7", "#1D2C47"),
    META_ABYSSAL:   ("#F9E2DE", "#46211F"),
}


def is_dark(palette: QPalette | None = None) -> bool:
    """Dark themes are detected from the palette rather than from a setting,
    so following the OS theme keeps working without anything to configure."""
    palette = palette or QApplication.palette()
    return palette.color(QPalette.Base).lightness() < 128


def status_hex(level: int, palette: QPalette | None = None) -> str | None:
    """Hex for a severity level, or None when nothing should be coloured."""
    pair = _STATUS.get(level)
    if pair is None:
        return None
    return pair[1] if is_dark(palette) else pair[0]


def status_brush(level: int, palette: QPalette | None = None) -> QBrush | None:
    colour = status_hex(level, palette)
    return None if colour is None else QBrush(QColor(colour))


def rarity_hex(meta_group_id: int | None, palette: QPalette | None = None) -> str | None:
    """Background hex for a meta group's rarity tint, or None for the many
    meta groups (and the None most items carry) that stay on the plain theme
    background."""
    pair = RARITY_TINTS.get(meta_group_id)
    if pair is None:
        return None
    return pair[1] if is_dark(palette) else pair[0]


def delta_hex(positive: bool, palette: QPalette | None = None) -> str:
    if not positive:
        return status_hex(CRITICAL, palette) or "#B3261E"
    return POSITIVE[1] if is_dark(palette) else POSITIVE[0]
