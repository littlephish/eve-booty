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


# Categorical fills for treemap tiles. Unlike everything above, these are one
# set for both themes rather than a light/dark pair: they are data ink, the
# same way a chart's series colours are, and recolouring the data when the OS
# theme flips would make two screenshots of the same assets incomparable.
#
# What does adapt is the text drawn on top -- readable_text_on() measures each
# fill and picks black or white, so the label's contrast is a computed result
# rather than a hope. Every fill here clears WCAG AA against whichever it
# picks; tests/test_contrast.py measures that independently.
#
# The first seven are the Okabe-Ito colourblind-safe palette ("Color Universal
# Design", Okabe & Ito 2008), which is the reason for the slightly odd hues --
# they stay distinguishable under the common forms of colour blindness. The
# rest are added to get enough distinct tiles for a treemap and are checked
# for the same contrast property, not for colourblind separability.
TREEMAP_FILLS = [
    "#E69F00",  # orange
    "#56B4E9",  # sky blue
    "#009E73",  # bluish green
    "#F0E442",  # yellow
    "#0072B2",  # blue
    "#D55E00",  # vermillion
    "#CC79A7",  # reddish purple
    "#9467BD",  # purple
    "#8C564B",  # brown
    "#17BECF",  # cyan
    "#BCBD22",  # olive
    "#7F7F7F",  # grey
]

# The long tail rolled up by treemap.roll_up() gets a deliberately flat grey
# instead of the next colour in the rotation -- it is not a category, and
# giving it a hue of its own invites reading it as one.
TREEMAP_OTHER_FILL = "#9E9E9E"

_BLACK, _WHITE = "#000000", "#FFFFFF"


def _relative_luminance(hex_colour: str) -> float:
    """WCAG 2.1 relative luminance."""
    raw = hex_colour.lstrip("#")
    channels = []
    for offset in (0, 2, 4):
        value = int(raw[offset:offset + 2], 16) / 255
        channels.append(
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        )
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(a: str, b: str) -> float:
    """WCAG 2.1 contrast ratio between two hex colours, 1.0 to 21.0."""
    lum_a, lum_b = _relative_luminance(a), _relative_luminance(b)
    lighter, darker = max(lum_a, lum_b), min(lum_a, lum_b)
    return (lighter + 0.05) / (darker + 0.05)


def treemap_fill(index: int) -> str:
    """Fill for the nth tile, cycling. Index rather than a hash of the label:
    tiles arrive sorted by value, so cycling gives neighbouring tiles
    different colours, where hashing would happily give two adjacent tiles the
    same one."""
    return TREEMAP_FILLS[index % len(TREEMAP_FILLS)]


def readable_text_on(background_hex: str) -> str:
    """Black or white, whichever has more contrast against the given fill."""
    if contrast_ratio(_BLACK, background_hex) >= contrast_ratio(_WHITE, background_hex):
        return _BLACK
    return _WHITE
