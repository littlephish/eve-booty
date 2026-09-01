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


def test_the_old_hand_picked_colours_would_have_failed():
    """Characterises what this module fixed. If someone reintroduces one of
    these, the numbers here say why not to."""
    assert contrast("#b9770e", "#FFFFFF") < AA_NORMAL      # low fuel amber
    assert contrast("#3a9d23", "#FFFFFF") < AA_NORMAL      # net worth gain
    assert contrast("#c0392b", "#1E1E1E") < AA_NORMAL      # reinforced, dark theme


def test_secondary_text_points_at_a_role_that_passes():
    """palette(mid) measured 1.99:1 against a white Base and was what every
    hint and footer in the app used. palette(shadow) is 4.54:1."""
    assert pal.SECONDARY_TEXT == "palette(shadow)"


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
