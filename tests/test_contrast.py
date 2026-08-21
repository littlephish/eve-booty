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
