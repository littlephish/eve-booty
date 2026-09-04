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


# Omnibox chip washes -- backgrounds under default theme text, same regime as
# RARITY_TINTS above: the theme's own text colour must stay AA-readable on top
# of the wash, so light theme gets pale washes and dark theme gets deep ones.
# The accent pair marks an including chip; the negated pair leans on the same
# hue family as CRITICAL because an exclusion is the destructive half of the
# grammar. Measured in tests/test_contrast.py.
#                light bg    dark bg
CHIP_ACCENT = ("#DCE9F8", "#1D3350")
CHIP_NEGATED = ("#F8DEDE", "#4A2020")

# One wash per chip kind, so a glance at the omnibox says *what axes* are
# filtered before any prefix is read -- three blue-ish chips reads as "three
# location-flavoured filters", one amber chip stands out as the odd category.
# Hues sit far enough apart to tell neighbours apart, but every pair keeps
# the same discipline as CHIP_ACCENT: a pale wash on light, a dark wash on
# dark, default text AA-readable on both (measured in tests/test_contrast.py,
# which also pins the washes pairwise-distinct so two kinds cannot silently
# converge). Negation is NOT in this table on purpose: an excluding chip
# always wears CHIP_NEGATED's red regardless of kind, because "this filter
# throws things away" is the higher-stakes signal and must not be diluted
# into ten flavours.
#                     light bg    dark bg
CHIP_KIND_TINTS = {
    "location": ("#DCE9F8", "#1D3350"),   # blue -- the most-used kind keeps the familiar accent
    "system":   ("#D8F0F2", "#163B40"),   # teal
    "region":   ("#E2E4F9", "#262B52"),   # periwinkle
    "owner":    ("#DDF0DD", "#1E3423"),   # green
    "category": ("#F4EAD6", "#3E3117"),   # tan
    "group":    ("#F8E5D8", "#45291A"),   # orange
    "meta":     ("#ECE0F5", "#33244A"),   # purple
    "item":     ("#E4E8EC", "#2A3138"),   # slate
    "is":       ("#F6DFEC", "#43213B"),   # pink
    "val":      ("#F2EFC8", "#3A3712"),   # yellow
}


def chip_tint(kind: str, negated: bool = False, palette: QPalette | None = None) -> str:
    """Background hex for one omnibox chip: the kind's wash, the negation red
    for excluding chips, and CHIP_ACCENT for a kind this build has never
    heard of (a saved view from a newer build must still render)."""
    if negated:
        pair = CHIP_NEGATED
    else:
        pair = CHIP_KIND_TINTS.get(kind, CHIP_ACCENT)
    return pair[1] if is_dark(palette) else pair[0]


# The neutral pill: a grey one clear step off the theme background, for a
# secondary button that must read as a button without claiming a hue --
# every hue is already spoken for by a chip kind, a status level or the
# builder's green plus. Same regime as the chip washes: the theme's own text
# stays AA-readable on the fill. Hover and press deepen it toward the text
# colour, the direction the eye reads as "more solid" in both themes.
#                light bg    dark bg
NEUTRAL_PILL = ("#C8CED5", "#454C55")
_PILL_HOVER, _PILL_PRESSED = 0.10, 0.20


def pill_fills(palette: QPalette | None = None) -> tuple[str, str, str]:
    """Rest, hover and pressed fills for a neutral pill in the theme in force."""
    dark = is_dark(palette)
    rest = NEUTRAL_PILL[1] if dark else NEUTRAL_PILL[0]
    toward = "#FFFFFF" if dark else "#000000"
    return rest, _blend(rest, toward, _PILL_HOVER), _blend(rest, toward, _PILL_PRESSED)


def pill_stylesheet(selector: str, palette: QPalette | None = None) -> str:
    """Stylesheet for a QPushButton or QToolButton drawn as a neutral pill.

    Solid fills rather than rgba, for the reason _blend gives: a translucent
    stylesheet background composites against whatever the style paints under
    the button, and that differs between styles."""
    rest, hover, pressed = pill_fills(palette)
    return (
        f"{selector} {{ background: {rest}; border: none; border-radius: 8px;"
        " padding: 2px 9px; }"
        f" {selector}:hover {{ background: {hover}; }}"
        f" {selector}:pressed {{ background: {pressed}; }}"
        # Focus ring for keyboard users, in the pressed grey rather than a
        # stray accent.
        f" {selector}:focus {{ border: 1px solid {pressed}; padding: 1px 8px; }}"
    )


# The rail's per-row value bar. A filled bar, not a background under text, so
# the WCAG requirement is the 3:1 floor for graphical objects (WCAG 2.1,
# 1.4.11 non-text contrast) rather than the 4.5:1 text threshold --
# tests/test_contrast.py holds these to 3:1 against both theme backgrounds.
RAIL_BAR = ("#3B76C4", "#6FA8E8")

# The value-cell heat wash: one accent hue throughout, blended toward the
# theme base by the row's heat fraction. The strength band runs from a
# barely-there 8% at the bottom of the log scale to a clearly-visible 26% at
# the top; past that the wash starts fighting the rarity tints and the
# selection colour for attention, and on light backgrounds it stops reading
# as a wash at all.
_HEAT_BASE = ("#FFFFFF", "#1E1E1E")
_HEAT_ACCENT = ("#1B66C9", "#5C9BE8")
_HEAT_MIN, _HEAT_MAX = 0.08, 0.26


def _blend(a_hex: str, b_hex: str, t: float) -> str:
    """Per-channel linear mix of a toward b (t=0 gives a, t=1 gives b).

    Solid colours on purpose: a translucent QBrush composites against
    whatever the style happens to paint underneath, which differs between
    stylesheet and palette rendering, so the "alpha" is precomputed against
    the known theme base instead of left to the compositor.
    """
    a, b = a_hex.lstrip("#"), b_hex.lstrip("#")
    channels = []
    for offset in (0, 2, 4):
        va = int(a[offset:offset + 2], 16)
        vb = int(b[offset:offset + 2], 16)
        channels.append(round(va + (vb - va) * t))
    return "#{:02X}{:02X}{:02X}".format(*channels)


def _heat_hex(fraction: float, dark: bool) -> str | None:
    """Theme-explicit half of heat_tint, split out so the contrast tests can
    measure both variants without standing up a QApplication."""
    if fraction <= 0:
        return None
    strength = _HEAT_MIN + (_HEAT_MAX - _HEAT_MIN) * min(fraction, 1.0)
    which = 1 if dark else 0
    return _blend(_HEAT_BASE[which], _HEAT_ACCENT[which], strength)


def heat_tint(fraction: float, palette: QPalette | None = None) -> str | None:
    """Background hex for a value cell's heat fraction, or None for no wash.

    Fraction 0 (an unpriced or worthless row) deliberately maps to None
    rather than a 0%-strength blend, so callers can skip creating a brush at
    all for the common cold cell.
    """
    return _heat_hex(fraction, is_dark(palette))


def normalised(base: QPalette) -> QPalette:
    """A copy of the palette with the roles this app's text depends on made
    readable.

    Measured on Windows 11's own dark palette (style "windows11",
    2026-08-28): Shadow is #000000 -- and Shadow is the one role every muted
    caption, hint and meta line here draws in, so the whole secondary text
    layer rendered black on #1e1e1e. AlternateBase was #ffffff, which made
    the value map's empty track glare white. Repairing the roles once at
    startup fixes every present and future palette(shadow) stylesheet and
    QPalette.Shadow paint in one place, instead of retrofitting a colour
    constant into two dozen call sites.

    Substitutes are measured like everything else in this file: #A6A6A6
    clears AA on both dark backgrounds (6.8:1 on #1E1E1E, 5.7:1 on #2D2D2D);
    #6E6E6E clears it on both light ones (5.1:1 on white, 4.7:1 on #F5F5F5).
    A light palette whose Shadow already reads (the classic #767676) passes
    through untouched.
    """
    p = QPalette(base)
    dark = p.color(QPalette.Base).lightness() < 128
    shadow = p.color(QPalette.Shadow).lightness()
    if dark and shadow < 128:
        p.setColor(QPalette.Shadow, QColor("#A6A6A6"))
    elif not dark and shadow < 60:
        p.setColor(QPalette.Shadow, QColor("#6E6E6E"))
    if dark and p.color(QPalette.AlternateBase).lightness() > 128:
        p.setColor(QPalette.AlternateBase, QColor("#3A3A3A"))
    return p


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
