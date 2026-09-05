"""Every colour the UI draws text in has to clear WCAG AA in both themes.

This exists because the colours it now checks were all originally picked by
eye and all three failed once measured: the low-fuel amber at 3.68:1 on white,
the net worth gain green at 3.48:1, and the reinforced red at 3.07:1 against a
dark window. A colour that looks obviously red on the developer's machine can
still be unreadable on the other theme, and nothing about running the app will
tell you.

Thresholds are WCAG 2.1: 4.5:1 for normal text, 3:1 for large or bold.
"""

from __future__ import annotations

import pytest

from evasset.ui import palette as pal

AA_NORMAL = 4.5

# Base and the alternating row colour, for each theme. Table text sits on both.
LIGHT_BACKGROUNDS = ["#FFFFFF", "#F5F5F5"]
DARK_BACKGROUNDS = ["#1E1E1E", "#262626"]


def _relative_luminance(hex_colour: str) -> float:
    raw = hex_colour.lstrip("#")
    channels = []
    for offset in (0, 2, 4):
        value = int(raw[offset:offset + 2], 16) / 255
        channels.append(
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        )
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(a: str, b: str) -> float:
    lum_a, lum_b = _relative_luminance(a), _relative_luminance(b)
    lighter, darker = max(lum_a, lum_b), min(lum_a, lum_b)
    return (lighter + 0.05) / (darker + 0.05)


def test_the_contrast_maths_matches_known_values():
    """Guard the helper itself -- a broken ratio function would pass every
    test below while measuring nothing."""
    assert contrast("#FFFFFF", "#000000") == pytest.approx(21.0, abs=0.01)
    assert contrast("#FFFFFF", "#FFFFFF") == pytest.approx(1.0, abs=0.01)
    # The canonical WCAG example: #767676 is the lightest grey passing AA on white.
    assert contrast("#767676", "#FFFFFF") == pytest.approx(4.54, abs=0.02)


@pytest.mark.parametrize("level", [pal.WARN, pal.CRITICAL])
@pytest.mark.parametrize("background", LIGHT_BACKGROUNDS)
def test_status_colours_are_readable_on_light(level, background):
    colour = pal._STATUS[level][0]
    assert contrast(colour, background) >= AA_NORMAL, (
        f"{colour} on {background} is {contrast(colour, background):.2f}:1"
    )


@pytest.mark.parametrize("level", [pal.WARN, pal.CRITICAL])
@pytest.mark.parametrize("background", DARK_BACKGROUNDS)
def test_status_colours_are_readable_on_dark(level, background):
    colour = pal._STATUS[level][1]
    assert contrast(colour, background) >= AA_NORMAL, (
        f"{colour} on {background} is {contrast(colour, background):.2f}:1"
    )


@pytest.mark.parametrize("background", LIGHT_BACKGROUNDS)
def test_positive_delta_is_readable_on_light(background):
    assert contrast(pal.POSITIVE[0], background) >= AA_NORMAL


@pytest.mark.parametrize("background", DARK_BACKGROUNDS)
def test_positive_delta_is_readable_on_dark(background):
    assert contrast(pal.POSITIVE[1], background) >= AA_NORMAL


@pytest.mark.parametrize("meta_group_id", sorted(pal.RARITY_TINTS))
def test_rarity_tints_keep_black_text_readable_on_light(meta_group_id):
    """Rarity tints are backgrounds under default theme text, so the contrast
    requirement runs the other way from the status colours: the theme's black
    text has to stay AA-readable on top of the wash."""
    background = pal.RARITY_TINTS[meta_group_id][0]
    assert contrast("#000000", background) >= AA_NORMAL, (
        f"black on {background} is {contrast('#000000', background):.2f}:1"
    )


@pytest.mark.parametrize("meta_group_id", sorted(pal.RARITY_TINTS))
def test_rarity_tints_keep_white_text_readable_on_dark(meta_group_id):
    background = pal.RARITY_TINTS[meta_group_id][1]
    assert contrast("#FFFFFF", background) >= AA_NORMAL, (
        f"white on {background} is {contrast('#FFFFFF', background):.2f}:1"
    )


def test_most_meta_groups_deliberately_get_no_tint():
    """Only faction, officer, deadspace and abyssal are tinted -- the id most
    items carry (None) and an ordinary meta group like Tech II must map to no
    background at all, or every line in the dialog ends up coloured."""
    assert pal.rarity_hex(None) is None
    assert pal.rarity_hex(2) is None  # Tech II


HEAT_FRACTIONS = [0.25, 0.5, 1.0]


@pytest.mark.parametrize("fraction", HEAT_FRACTIONS)
def test_heat_tints_keep_black_text_readable_on_light(fraction):
    """The heat wash is a background under default theme text, like the
    rarity tints -- the strongest wash the scale can produce must still leave
    black text at AA, or the most valuable rows become the least readable."""
    background = pal._heat_hex(fraction, dark=False)
    assert background is not None
    assert contrast("#000000", background) >= AA_NORMAL, (
        f"black on {background} (fraction {fraction}) is "
        f"{contrast('#000000', background):.2f}:1"
    )


@pytest.mark.parametrize("fraction", HEAT_FRACTIONS)
def test_heat_tints_keep_white_text_readable_on_dark(fraction):
    background = pal._heat_hex(fraction, dark=True)
    assert background is not None
    assert contrast("#FFFFFF", background) >= AA_NORMAL, (
        f"white on {background} (fraction {fraction}) is "
        f"{contrast('#FFFFFF', background):.2f}:1"
    )


def test_zero_heat_means_no_wash_at_all():
    """Fraction 0 is the unpriced/worthless case; it must map to no colour
    rather than a 0%-strength blend that still repaints every cold cell."""
    assert pal._heat_hex(0.0, dark=False) is None
    assert pal._heat_hex(0.0, dark=True) is None
    assert pal._heat_hex(-0.5, dark=False) is None


_ALL_CHIP_PAIRS = [
    ("accent", pal.CHIP_ACCENT),
    ("negated", pal.CHIP_NEGATED),
    *sorted(pal.CHIP_KIND_TINTS.items()),
]


@pytest.mark.parametrize("pair", [p for _n, p in _ALL_CHIP_PAIRS],
                         ids=[n for n, _p in _ALL_CHIP_PAIRS])
def test_chip_washes_keep_black_text_readable_on_light(pair):
    """Chip washes sit under default theme text like the rarity tints --
    every per-kind wash included, not just the two originals."""
    assert contrast("#000000", pair[0]) >= AA_NORMAL, (
        f"black on {pair[0]} is {contrast('#000000', pair[0]):.2f}:1"
    )


@pytest.mark.parametrize("pair", [p for _n, p in _ALL_CHIP_PAIRS],
                         ids=[n for n, _p in _ALL_CHIP_PAIRS])
def test_chip_washes_keep_white_text_readable_on_dark(pair):
    assert contrast("#FFFFFF", pair[1]) >= AA_NORMAL, (
        f"white on {pair[1]} is {contrast('#FFFFFF', pair[1]):.2f}:1"
    )


def _channel_distance(a: str, b: str) -> int:
    """Sum of per-channel RGB differences -- a blunt but honest measure of
    how far apart two pale (or two deep) washes are to the eye."""
    a, b = a.lstrip("#"), b.lstrip("#")
    return sum(abs(int(a[i:i + 2], 16) - int(b[i:i + 2], 16)) for i in (0, 2, 4))


# The floor every pair of washes clears in both themes; the closest
# pre-existing neighbours (category/group on light, category/val on dark) sit
# at 11 and 15.
WASH_FLOOR = 10
# Washes in one hue family need more room, or an including chip reads as an
# exclusion.
HUE_NEIGHBOUR_FLOOR = 24
HUE_NEIGHBOURS = [("abyssal", "negated"), ("abyssal", "is")]


def test_every_chip_kind_has_its_own_wash_and_none_collide():
    """The per-kind colours ARE the feature: every kind the grammar can mint
    must appear in the table, and no two washes -- the kinds, the negation
    red and the accent -- may sit within WASH_FLOOR of each other in either
    theme: two converging would silently un-ship the distinction, and the
    negation red converging with a kind would dress an including chip as an
    exclusion. location alone is allowed to equal CHIP_ACCENT: the most-used
    kind keeps the familiar accent on purpose, and a kind from a newer build
    falls back to it."""
    from evasset import omni

    minted_kinds = {
        *omni.LEVEL_KINDS, "item", "is", "val", omni.STAT_KIND, omni.ROLL_KIND, omni.ABYSSAL_KIND,
    }
    assert set(pal.CHIP_KIND_TINTS) == minted_kinds
    assert pal.CHIP_KIND_TINTS["location"] == pal.CHIP_ACCENT
    named = dict(_ALL_CHIP_PAIRS)
    checked = 0
    for theme in (0, 1):
        for i, (name_a, pair_a) in enumerate(_ALL_CHIP_PAIRS):
            for name_b, pair_b in _ALL_CHIP_PAIRS[i + 1:]:
                if {name_a, name_b} == {"accent", "location"}:
                    continue
                distance = _channel_distance(pair_a[theme], pair_b[theme])
                assert distance >= WASH_FLOOR, (
                    f"{name_a} and {name_b} are {distance} apart in theme {theme}"
                )
                checked += 1
        for name_a, name_b in HUE_NEIGHBOURS:
            distance = _channel_distance(named[name_a][theme], named[name_b][theme])
            assert distance >= HUE_NEIGHBOUR_FLOOR, (
                f"{name_a} and {name_b} are {distance} apart in theme {theme}"
            )
    pairs = len(_ALL_CHIP_PAIRS)
    assert checked == 2 * (pairs * (pairs - 1) // 2 - 1)


def test_the_unpriced_badge_text_inverts_readably_against_its_warning_fill():
    """The estate strip's "SHOW" pill fills with the WARN colour and inverts
    its text: white on the light theme's dark amber, black on the dark
    theme's bright amber. Both directions must clear AA or the one figure
    flagged as untrustworthy becomes the one figure you cannot read."""
    light_fill, dark_fill = pal._STATUS[pal.WARN]
    assert contrast("#FFFFFF", light_fill) >= AA_NORMAL, (
        f"white on {light_fill} is {contrast('#FFFFFF', light_fill):.2f}:1"
    )
    assert contrast("#000000", dark_fill) >= AA_NORMAL, (
        f"black on {dark_fill} is {contrast('#000000', dark_fill):.2f}:1"
    )


def test_chip_tint_prefers_negation_and_survives_unknown_kinds():
    """Negation must out-shout the kind colour (an excluding chip is the
    higher-stakes signal), and a saved view from a newer build with a kind
    this build has never heard of must still render on the neutral accent."""
    assert pal.chip_tint("owner", negated=True) in pal.CHIP_NEGATED
    assert pal.chip_tint("owner") in pal.CHIP_KIND_TINTS["owner"]
    assert pal.chip_tint("from-the-future") in pal.CHIP_ACCENT


def test_the_neutral_pill_keeps_default_text_readable_in_both_themes():
    """The state row's "Clear all" sits default theme text on the pill fill,
    in every state -- the deepened hover and pressed fills included, since a
    button is read while it is being pressed."""
    from PySide6.QtGui import QColor, QPalette

    for dark, text in ((False, "#000000"), (True, "#FFFFFF")):
        theme = QPalette()
        theme.setColor(QPalette.Base, QColor("#1E1E1E" if dark else "#FFFFFF"))
        for fill in pal.pill_fills(theme):
            assert contrast(text, fill) >= AA_NORMAL, (
                f"{text} on {fill} is {contrast(text, fill):.2f}:1"
            )


def test_the_neutral_pill_is_a_visible_step_off_the_window_and_deepens_on_hover():
    """The whole point of the pill is to stop blending in: its rest fill must
    be a measurable step off both theme backgrounds, and hover and press must
    each move it further from the background, not closer -- a hover fill that
    drifted back toward the window would make the button fade under the
    pointer, the exact moment it should look most solid.

    1.4:1 is not a WCAG figure; WCAG has no floor for a fill against its own
    background. It is the floor the first candidate light grey failed:
    #D0D5DB measured 1.35:1 on the light alternate base and still read as
    part of the window, so the light grey was darkened until it cleared 1.4
    (2026-09-01). The dark grey cleared it from the start."""
    from PySide6.QtGui import QColor, QPalette

    for backgrounds in (LIGHT_BACKGROUNDS, DARK_BACKGROUNDS):
        theme = QPalette()
        theme.setColor(QPalette.Base, QColor(backgrounds[0]))
        rest, hover, pressed = pal.pill_fills(theme)
        for background in backgrounds:
            at_rest = contrast(rest, background)
            assert at_rest >= 1.4, f"{rest} on {background} is only {at_rest:.2f}:1"
            assert contrast(hover, background) > at_rest
            assert contrast(pressed, background) > contrast(hover, background)


def test_the_pill_stylesheet_carries_every_state_it_promises():
    sheet = pal.pill_stylesheet("QPushButton")
    for state in ("QPushButton {", "QPushButton:hover", "QPushButton:pressed",
                  "QPushButton:focus"):
        assert state in sheet
    assert "rgba" not in sheet, "fills are precomputed solids, see _blend"


# WCAG 2.1 section 1.4.11: graphical objects need 3:1, not the 4.5:1 text
# threshold. The rail bar is a filled bar drawn on the theme background, with
# no text on top of it, so 3:1 against the background is the requirement.
AA_GRAPHIC = 3.0


@pytest.mark.parametrize("background", LIGHT_BACKGROUNDS)
def test_the_rail_bar_is_visible_on_light(background):
    assert contrast(pal.RAIL_BAR[0], background) >= AA_GRAPHIC, (
        f"{pal.RAIL_BAR[0]} on {background} is "
        f"{contrast(pal.RAIL_BAR[0], background):.2f}:1"
    )


@pytest.mark.parametrize("background", DARK_BACKGROUNDS)
def test_the_rail_bar_is_visible_on_dark(background):
    assert contrast(pal.RAIL_BAR[1], background) >= AA_GRAPHIC, (
        f"{pal.RAIL_BAR[1]} on {background} is "
        f"{contrast(pal.RAIL_BAR[1], background):.2f}:1"
    )


def test_the_old_hand_picked_colours_would_have_failed():
    """Characterises what this module fixed. If someone reintroduces one of
    these, the numbers here say why not to."""
    assert contrast("#b9770e", "#FFFFFF") < AA_NORMAL      # low fuel amber
    assert contrast("#3a9d23", "#FFFFFF") < AA_NORMAL      # net worth gain
    assert contrast("#c0392b", "#1E1E1E") < AA_NORMAL      # reinforced, dark theme


def test_secondary_text_points_at_a_role_that_passes():
    """palette(mid) measured 1.99:1 against a white Base and was what every
    hint and footer in the app used. palette(shadow) is 4.54:1 -- on the
    classic light palette; Windows 11's dark palette ships it as #000000,
    which is why normalised() (tested below) repairs the role at startup."""
    assert pal.SECONDARY_TEXT == "palette(shadow)"


def test_normalised_repairs_the_windows_dark_palette_roles():
    """Measured on the real Windows 11 dark palette (style "windows11",
    2026-08-28): Shadow -- the role every muted caption, hint and meta line
    draws in -- is #000000, and AlternateBase is #ffffff. That rendered the
    entire secondary text layer black on #1e1e1e and the value map's track
    glaring white. The repair must land AA substitutes on the dark roles and
    leave a healthy light palette untouched."""
    from PySide6.QtGui import QColor, QPalette

    win_dark = QPalette()
    win_dark.setColor(QPalette.Base, QColor("#2d2d2d"))
    win_dark.setColor(QPalette.Shadow, QColor("#000000"))
    win_dark.setColor(QPalette.AlternateBase, QColor("#ffffff"))
    fixed = pal.normalised(win_dark)
    shadow = fixed.color(QPalette.Shadow).name().upper()
    for background in [*DARK_BACKGROUNDS, "#2D2D2D"]:
        assert contrast(shadow, background) >= AA_NORMAL, (
            f"{shadow} on {background} is {contrast(shadow, background):.2f}:1"
        )
    assert fixed.color(QPalette.AlternateBase).lightness() < 128, (
        "a white AlternateBase must not survive into a dark palette"
    )

    healthy_light = QPalette()
    healthy_light.setColor(QPalette.Base, QColor("#ffffff"))
    healthy_light.setColor(QPalette.Shadow, QColor("#767676"))
    assert pal.normalised(healthy_light).color(QPalette.Shadow).name() == "#767676"

    harsh_light = QPalette()
    harsh_light.setColor(QPalette.Base, QColor("#ffffff"))
    harsh_light.setColor(QPalette.Shadow, QColor("#000000"))
    repaired = pal.normalised(harsh_light).color(QPalette.Shadow).name().upper()
    for background in LIGHT_BACKGROUNDS:
        assert contrast(repaired, background) >= AA_NORMAL


def test_no_view_still_uses_the_failing_role():
    """Matches palette(mid) however it is spelled. The first version of this
    test looked for "color: palette(mid)" with exactly that spacing and missed
    two inline-HTML spans written "color:palette(mid)" -- so the sweep it was
    guarding reported success while two call sites were still at 1.99:1."""
    import pathlib
    import re

    offenders = [
        path.name
        for path in pathlib.Path("src/evasset/ui").glob("*.py")
        if path.name != "palette.py"
        and re.search(r"palette\(mid\)", path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"palette(mid) is 1.99:1 -- still used in {offenders}"


# --------------------------------------------------------------- treemap
# The treemap draws its label directly on the tile, so the pair that has to
# clear AA is (fill, whatever readable_text_on picked for it) -- not the
# fill against the window background.


@pytest.mark.parametrize("fill", pal.TREEMAP_FILLS + [pal.TREEMAP_OTHER_FILL])
def test_treemap_labels_are_readable_on_their_own_tile(fill):
    text = pal.readable_text_on(fill)
    assert contrast(text, fill) >= AA_NORMAL, (
        f"{text} on {fill} is {contrast(text, fill):.2f}:1"
    )


@pytest.mark.parametrize("fill", pal.TREEMAP_FILLS + [pal.TREEMAP_OTHER_FILL])
def test_readable_text_picks_the_better_of_black_and_white(fill):
    """Guards the chooser itself: a version that always returned white would
    still pass the test above for most of the palette."""
    chosen = pal.readable_text_on(fill)
    other = "#FFFFFF" if chosen == "#000000" else "#000000"
    assert contrast(chosen, fill) >= contrast(other, fill)


def test_the_palettes_own_contrast_maths_agrees_with_this_files():
    """palette.contrast_ratio is used at runtime to pick label colours; this
    file's independent implementation is what checks it."""
    for a, b in [("#FFFFFF", "#000000"), ("#767676", "#FFFFFF"), ("#E69F00", "#000000")]:
        assert pal.contrast_ratio(a, b) == pytest.approx(contrast(a, b), abs=0.001)


def test_treemap_fills_are_distinct():
    """Two tiles the same colour side by side read as one tile."""
    assert len(set(pal.TREEMAP_FILLS)) == len(pal.TREEMAP_FILLS)
    assert pal.TREEMAP_OTHER_FILL not in pal.TREEMAP_FILLS


def test_treemap_fill_cycles_rather_than_running_out():
    """More groups than colours is the normal case, not an error."""
    assert pal.treemap_fill(0) == pal.TREEMAP_FILLS[0]
    assert pal.treemap_fill(len(pal.TREEMAP_FILLS)) == pal.TREEMAP_FILLS[0]
    assert pal.treemap_fill(len(pal.TREEMAP_FILLS) + 3) == pal.TREEMAP_FILLS[3]


# ---------------------------------------------------------- quality wash
# The roll columns' diverging wash: a background under default theme text,
# like the heat wash, so the same AA requirement applies at both ends of the
# scale in both themes. Ends and quarter points; the middle is tested apart.
QUALITY_POINTS = [0.0, 0.25, 0.75, 1.0]


@pytest.mark.parametrize("quality", QUALITY_POINTS)
def test_quality_tints_keep_black_text_readable_on_light(quality):
    background = pal._quality_hex(quality, dark=False)
    assert background is not None
    assert contrast("#000000", background) >= AA_NORMAL, (
        f"black on {background} (quality {quality}) is "
        f"{contrast('#000000', background):.2f}:1"
    )


@pytest.mark.parametrize("quality", QUALITY_POINTS)
def test_quality_tints_keep_white_text_readable_on_dark(quality):
    background = pal._quality_hex(quality, dark=True)
    assert background is not None
    assert contrast("#FFFFFF", background) >= AA_NORMAL, (
        f"white on {background} (quality {quality}) is "
        f"{contrast('#FFFFFF', background):.2f}:1"
    )


def test_the_middle_of_the_quality_scale_paints_nothing():
    """A middling roll is the common case and must stay on the plain row
    background -- a table where every cell carries a faint tint says
    nothing. The band is 0.5 +/- 0.05, closed."""
    for quality in (0.45, 0.5, 0.55, 0.47, 0.53):
        assert pal._quality_hex(quality, dark=False) is None, quality
        assert pal._quality_hex(quality, dark=True) is None, quality
    assert pal._quality_hex(0.44, dark=False) is not None
    assert pal._quality_hex(0.56, dark=True) is not None
    assert pal.quality_tint(None) is None


def test_the_quality_tint_diverges_and_deepens_toward_the_ends():
    """Below the middle the wash must lean red (the CRITICAL family), above
    it green (POSITIVE), and each side must get stronger further out -- a
    wash that read the same at 25% and 0% would hide the difference the
    column exists to show."""
    def channels(hex_colour):
        raw = hex_colour.lstrip("#")
        return tuple(int(raw[i:i + 2], 16) for i in (0, 2, 4))

    low_r, low_g, _b = channels(pal._quality_hex(0.0, dark=False))
    high_r, high_g, _b = channels(pal._quality_hex(1.0, dark=False))
    assert low_r > low_g, "a bad roll should lean red"
    assert high_g > high_r, "a good roll should lean green"
    for dark in (False, True):
        faint = pal._quality_hex(0.25, dark=dark)
        strong = pal._quality_hex(0.0, dark=dark)
        base = "#1E1E1E" if dark else "#FFFFFF"
        assert contrast(base, strong) > contrast(base, faint), (
            "the wash must deepen toward the end of the scale"
        )
