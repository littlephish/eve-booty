"""The abyssal complex-search card and its range track.

The card is the one place the multi-stat abyssal search is built, so what is
pinned here is what the omnibox relies on: the rows are usable only with
one type picked (no type spans attribute sets that share nothing), a row bounds
a stat in its display units and nothing else, Done emits exactly the stat:
chips the grammar would have parsed from typed text, every other way out
emits cancelled and nothing else, and a set of chips seeded in comes back
out unchanged -- a card that rewrote the user's filter on a
Done-with-nothing-touched would be a filter that drifts every time it is
looked at.

The track runs worst to best whichever way the number runs, so the second
thing pinned is the orientation: on a low-is-good stat the LEFT handle and
field are the numerically larger value, and a chip's one-sidedness is read
off the handles through that mapping, not off the numbers.
"""

from __future__ import annotations

import pytest

from evasset import omni

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from conftest import match_text  # noqa: E402
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from evasset.ui import abyssal_card as ac  # noqa: E402
from evasset.ui.abyssal_card import LEFT, RIGHT, _RangeTrack  # noqa: E402

WEBIFIER = "Abyssal Stasis Webifier"
MWD = "50MN Abyssal Microwarpdrive"
CPU, SPEED, SIG_PCT, SIG_M = 50, 20, 554, 983

# The shapes queries.abyssal_type_columns returns: a plain display name, an
# alias-shadowed one, and the real SDE's shared "Signature Radius Modifier"
# pair already disambiguated by unit. high_is_good is the stored polarity
# (CPU low-is-good; the webifier's speedFactor low-is-good by the
# mutaplasmid's override) and base the type's un-mutated display value,
# unknown for the signature bonus.
ATTRS = [
    {"attribute_id": CPU, "name": "cpu", "label": "CPU usage", "unit_id": 106, "unit": "tf",
     "high_is_good": False, "base": 27.0},
    {"attribute_id": SPEED, "name": "speedFactor", "label": "Maximum Velocity Bonus",
     "unit_id": 124, "unit": "%", "high_is_good": False, "base": -60.0},
    {"attribute_id": SIG_PCT, "name": "signatureRadiusBonus",
     "label": "Signature Radius Modifier (%)", "unit_id": 124, "unit": "%",
     "high_is_good": True, "base": None},
    {"attribute_id": SIG_M, "name": "signatureRadiusAdd",
     "label": "Signature Radius Modifier (m)", "unit_id": 1, "unit": "m", "high_is_good": True},
]
BOUNDS = {CPU: (24.0, 31.0), SPEED: (-65.0, -55.0), SIG_PCT: (10.0, 20.0)}  # SIG_M: none fetched
TYPES = [
    {"type_id": 47702, "name": WEBIFIER, "items": 12, "fetched": 12},
    {"type_id": 47408, "name": MWD, "items": 3, "fetched": 1},
]


@pytest.fixture
def app():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def card(app):
    c = ac.AbyssalCard()
    yield c
    c.hide()
    c.deleteLater()


def record(signal) -> list:
    calls: list = []
    signal.connect(lambda *args: calls.append(args))
    return calls


def pick(card, name: str) -> None:
    """Select a type in the dropdown by its full name."""
    index = card.type_combo.findData(name)
    assert index >= 0, f"{name!r} not in the picker"
    card.type_combo.setCurrentIndex(index)


def clear_type(card) -> None:
    """Empty the type edit and press Enter, the user's way back from a type
    to every abyssal item."""
    card.type_edit.setFocus()
    card.type_edit.selectAll()
    QTest.keyClick(card.type_edit, Qt.Key_Backspace)
    assert card.type_edit.text() == ""
    QTest.keyClick(card.type_edit, Qt.Key_Return)


def entries(card) -> list[tuple[str, str]]:
    """(type name, rendered text) per dropdown entry."""
    combo = card.type_combo
    return [(combo.itemData(i), combo.itemText(i)) for i in range(combo.count())]


def offered(row) -> list[int]:
    """The attribute ids a row's combo lists, in order."""
    return [row.attr_combo.itemData(i) for i in range(row.attr_combo.count())]


def pickable(row) -> list[int]:
    """The attribute ids a row's combo still lets the user pick."""
    model = row.attr_combo.model()
    return [
        row.attr_combo.itemData(i)
        for i in range(row.attr_combo.count())
        if model.item(i).isEnabled()
    ]


def fields(row) -> tuple[str, str]:
    return row.left_field.text(), row.right_field.text()


def labels(row) -> tuple[str, str, str]:
    return tuple(label.text() for label in row.range_labels)


def type_into(field, text: str) -> None:
    field.setFocus()
    field.selectAll()
    QTest.keyClicks(field, text)
    QTest.keyClick(field, Qt.Key_Return)


def type_search(card, text: str) -> None:
    """Type over the type edit's selected label key by key, the way a user
    does -- the completer filters on keystrokes, not on set text."""
    card.type_edit.setFocus()
    card.type_edit.selectAll()
    QTest.keyClicks(card.type_edit, text)


def completions(card) -> list[str]:
    """The labels the type completer's popup is offering, in order."""
    model = card._type_completer.completionModel()
    return [model.index(i, 0).data(Qt.DisplayRole) for i in range(model.rowCount())]


def completion_popup(card):
    return card._type_completer.popup()


WEBIFIER_LABEL, MWD_LABEL = "Stasis Webifier · 12", "50MN Microwarpdrive · 3"


def one_type_card(card):
    card.set_types(TYPES, [WEBIFIER])
    card.set_attributes(ATTRS, BOUNDS)
    return card


# ----------------------------------------------------------------- track
def test_the_track_keeps_handle_fractions_on_a_worst_to_best_axis(app):
    """Fractions, not values, are the state, and the axis may run
    numerically downward: a CPU track goes 31 -> 24, so the value at
    fraction 0 is the big number. A change of axis keeps the handles
    where they were proportionally, and "at the end" is a fraction test
    the card can trust."""
    track = _RangeTrack()
    track.set_axis(31.0, 24.0)
    track.set_values(31.0, 27.5)
    assert (track.low_fraction, track.high_fraction) == (0.0, 0.5)
    assert track.values() == pytest.approx((31.0, 27.5))
    track.set_values(24.0, 30.0)  # any order in: left-to-right out
    assert track.values() == pytest.approx((30.0, 24.0))
    assert (track.low_fraction, track.high_fraction) == pytest.approx((1 / 7, 1.0))
    track.set_axis(0.0, 100.0)
    assert track.values() == pytest.approx((100 / 7, 100.0))
    track.set_values(-5.0, 500.0)  # clamped onto the track
    assert (track.low_fraction, track.high_fraction) == (0.0, 1.0)
    track.set_axis(5.0, 5.0)  # degenerate: one fetched item
    track.set_values(5.0, 5.0)
    assert track.values() == (5.0, 5.0)
    assert (track.low_fraction, track.high_fraction) == (0.0, 0.0)


def test_the_base_tick_sits_at_the_bases_fraction_or_nowhere(app):
    """A base outside the estate's bounds (every owned roll beat it, or
    every one fell short) draws no tick: clamped to an end it would read
    as "the worst roll owned is the base", which is false."""
    track = _RangeTrack()
    track.set_axis(31.0, 24.0, base=27.0)
    assert track.base_fraction == pytest.approx(4 / 7)
    track.set_axis(31.0, 24.0, base=31.0)
    assert track.base_fraction == 0.0
    track.set_axis(31.0, 24.0, base=22.0)
    assert track.base_fraction is None
    track.set_axis(31.0, 24.0)
    assert track.base_fraction is None
    track.set_axis(5.0, 5.0, base=5.0)
    assert track.base_fraction is None, "a zero-length axis has no position"


def test_arrow_keys_step_a_percent_shift_ten_and_the_handles_never_cross(app):
    track = _RangeTrack()
    track.set_axis(0.0, 100.0)
    track.set_values(20.0, 80.0)
    moves = record(track.moved)
    track.active = RIGHT
    QTest.keyClick(track, Qt.Key_Left)
    assert track.high_fraction == pytest.approx(0.79)
    QTest.keyClick(track, Qt.Key_Left, Qt.ShiftModifier)
    assert track.high_fraction == pytest.approx(0.69)
    track.active = LEFT
    QTest.keyClick(track, Qt.Key_Right)
    assert track.low_fraction == pytest.approx(0.21)
    QTest.keyClick(track, Qt.Key_Right, Qt.ShiftModifier)
    assert track.low_fraction == pytest.approx(0.31)
    QTest.keyClick(track, Qt.Key_Home)
    assert track.low_fraction == 0.0
    assert len(moves) == 5
    # The left handle can never cross the right one: End parks it there.
    QTest.keyClick(track, Qt.Key_End)
    assert track.low_fraction == pytest.approx(0.69)
    assert track.low_fraction == track.high_fraction, "equal is a single-value filter"
    QTest.keyClick(track, Qt.Key_Right)
    assert track.low_fraction == pytest.approx(0.69), "and cannot go past it"
    assert len(moves) == 6, "a step that moved nothing announced nothing"


def test_a_press_grabs_the_nearest_handle(app):
    track = _RangeTrack()
    track.resize(210, _RangeTrack.HEIGHT)  # a 200 px track after the insets
    track.set_axis(0.0, 100.0)
    moves = record(track.moved)
    y = _RangeTrack.HEIGHT // 2
    QTest.mousePress(track, Qt.LeftButton, pos=QPoint(45, y))  # fraction 0.2
    assert track.low_fraction == pytest.approx(0.2)
    assert track.high_fraction == 1.0
    QTest.mouseRelease(track, Qt.LeftButton, pos=QPoint(45, y))
    QTest.mousePress(track, Qt.LeftButton, pos=QPoint(165, y))  # fraction 0.8
    assert track.high_fraction == pytest.approx(0.8)
    assert track.low_fraction == pytest.approx(0.2)
    assert len(moves) == 2
    # Stacked handles: a press to the left of the pair pulls the left one.
    track.set_values(50.0, 50.0)
    QTest.mousePress(track, Qt.LeftButton, pos=QPoint(85, y))  # fraction 0.4
    assert (track.low_fraction, track.high_fraction) == pytest.approx((0.4, 0.5))


def test_the_track_paints_offscreen_at_a_wide_and_a_narrow_width(app):
    """Every branch of the painter -- segments, the accent fill, the base
    tick, both handles -- at a width where the 15 px rhythm does not divide
    evenly and at one narrower than a single segment."""
    track = _RangeTrack()
    track.set_axis(31.0, 24.0, base=27.0)
    track.set_values(30.0, 25.0)
    for width in (283, 9):
        track.resize(width, _RangeTrack.HEIGHT)
        image = track.grab().toImage()
        assert not image.isNull() and image.width() == width


# ----------------------------------------------------------------- rows
def test_stat_rows_are_enabled_only_while_one_type_is_picked(card):
    """Every type rolls a different attribute set, so a track over no type
    would have no honest bounds; the rows go dark rather than lie. The
    dropdown lists the types alone, in the order the counts query returns
    them, and announces one name or none."""
    selections = record(card.selection_changed)
    card.set_types(TYPES, [])
    assert entries(card) == [
        (WEBIFIER, "Stasis Webifier · 12"),
        (MWD, "50MN Microwarpdrive · 3"),
    ]
    assert card.selected_types() == []
    assert not card.rows_box.isEnabled()
    assert not card.rows_hint.isHidden()
    assert not selections, "set_types is silent -- the view already knows the seed"

    pick(card, WEBIFIER)
    assert card.rows_box.isEnabled()
    assert card.rows_hint.isHidden()
    assert selections[-1] == ([WEBIFIER],)

    pick(card, MWD)
    assert card.rows_box.isEnabled(), "one type is still one type"
    assert selections[-1] == ([MWD],)
    assert card.selected_types() == [MWD]

    card.show()
    clear_type(card)
    assert not card.rows_box.isEnabled()
    assert selections[-1] == ([],)
    assert len(selections) == 3


def test_no_type_picked_is_an_empty_edit_dark_rows_and_the_bare_chip_on_done(card):
    """The card opened from a bare ``abyssal`` chip has nothing selected:
    there is no "All abyssal modules" entry to stand for it, so the edit is
    empty under its placeholder with no current entry, the rows are dark
    and Add stat is gone -- a row added now would be about no type -- and
    Done hands back the bare chip, the filter it came in with. The same
    state before the type list lands, since the seed alone decides it."""
    card.seed([omni.Chip("abyssal", "")])
    assert card.seeded_types() == [] and card.selected_types() == []
    assert card.type_combo.currentIndex() == -1
    assert card.type_edit.text() == ""

    card.set_types(TYPES, card.seeded_types())
    card.set_attributes([], {})
    assert card.type_combo.currentIndex() == -1
    assert card.type_edit.text() == ""
    assert card.type_edit.placeholderText() == ac.TYPE_PLACEHOLDER
    assert entries(card) == [(WEBIFIER, WEBIFIER_LABEL), (MWD, MWD_LABEL)]
    assert not card.rows_box.isEnabled()
    assert not card.rows_hint.isHidden()
    assert card.add_row_btn.isHidden()
    assert card.add_row() is None
    assert card.chips() == [omni.Chip("abyssal", "")]

    done = record(card.done)
    card.show()
    assert card.type_edit.hasFocus() and card.type_edit.text() == ""
    card.done_btn.click()
    assert done == [([omni.Chip("abyssal", "")],)]

    pick(card, WEBIFIER)
    assert not card.add_row_btn.isHidden(), "a type brings Add stat back"
    assert not card.add_row_btn.isEnabled(), "disabled until its attributes land"


def test_add_row_needs_an_attribute_list_and_opens_on_the_whole_range_worst_first(card):
    """A fresh row opens on the whole estate range, laid out worst to best:
    the CPU's LEFT field is the biggest CPU owned, the right the smallest,
    the base ticked between and named under the track. No end of the
    range is a good one to start narrowed from -- a row that opened on the
    upper half would have hidden items for no reason the user chose."""
    card.set_types(TYPES, [WEBIFIER])
    assert card.add_row() is None
    assert not card.add_row_btn.isEnabled()
    card.set_attributes(ATTRS, BOUNDS)
    assert card.add_row_btn.isEnabled()
    row = card.add_row()
    assert row is not None and card.rows() == [row]
    assert row.attribute_id() == CPU, "the first attribute, in label order"
    assert row.numeric_range() == (24.0, 31.0)
    assert (row.track.worst, row.track.best) == (31.0, 24.0)
    assert (row.track.low_fraction, row.track.high_fraction) == (0.0, 1.0)
    assert fields(row) == ("31 tf", "24 tf")
    assert labels(row) == ("31 tf", "base 27 tf", "24 tf")
    assert row.track.base_fraction == pytest.approx(4 / 7)
    assert row.decimals == 1


def test_a_low_is_good_negative_range_puts_the_numerically_larger_worse_value_on_the_left(card):
    """The webifier's velocity bonus runs -63..-51 in the estate and lower
    is better (the mutaplasmid's override), so the left end -- worst -- is
    -51, the numerically larger figure. The signed modifier unit keeps its
    sign in the fields and labels, and a base above the estate's best roll
    still names itself under the track without a tick."""
    card.set_types(TYPES, [WEBIFIER])
    card.set_attributes(ATTRS, {SPEED: (-63.0, -51.0)})
    row = card.add_row(SPEED)
    assert (row.track.worst, row.track.best) == (-51.0, -63.0)
    assert fields(row) == ("-51%", "-63%")
    assert labels(row) == ("-51%", "base -60%", "-63%")
    assert row.track.base_fraction == pytest.approx(0.75)
    row.set_range(-60.0, -55.0)
    assert fields(row) == ("-55%", "-60%")
    assert row.numeric_range() == pytest.approx((-60.0, -55.0))
    assert (row.track.low_fraction, row.track.high_fraction) == pytest.approx((1 / 3, 0.75))

    card.set_attributes(ATTRS, {SPEED: (-63.0, -62.0)})  # the base is worse than every roll
    (row,) = card.rows()
    assert labels(row) == ("-62%", "base -60%", "-63%")
    assert row.track.base_fraction is None


def test_a_high_is_good_signed_range_shows_plus_signs_and_no_base_when_unknown(card):
    row = one_type_card(card).add_row(SIG_PCT)  # bounds 10..20, high-is-good, base unknown
    assert (row.track.worst, row.track.best) == (10.0, 20.0)
    assert fields(row) == ("+10%", "+20%")
    assert labels(row) == ("+10%", "", "+20%")
    assert row.track.base_fraction is None


def test_fields_commit_on_enter_clamp_to_the_bounds_and_the_other_handle_and_revert_garbage(card):
    """The fields accept a bare number or the text they show; Enter (or
    blur) commits it onto the handle, clamped to the estate bounds and
    then to the other handle -- a worst-side bound typed past the
    best-side one lands on it rather than swapping ends -- and text with
    no number in it reverts to what the field showed. Enter in a field
    goes no further: the card stays open and nothing is applied."""
    row = one_type_card(card).add_row(SIG_PCT)  # bounds 10..20, high-is-good
    changes = record(card.filter_changed)
    done, cancelled = record(card.done), record(card.cancelled)
    card.show()

    type_into(row.left_field, "15")
    assert row.track.low_fraction == pytest.approx(0.5)
    assert fields(row) == ("+15%", "+20%")
    assert len(changes) == 1

    type_into(row.right_field, "+17%")  # what the field itself shows is accepted
    assert row.track.high_fraction == pytest.approx(0.7)
    assert fields(row) == ("+15%", "+17%")

    type_into(row.left_field, "19.5")  # past the right handle: lands on it
    assert row.numeric_range() == pytest.approx((17.0, 17.0))
    assert fields(row) == ("+17%", "+17%")

    type_into(row.right_field, "500")  # past the bound: clamped to it
    assert row.track.high_fraction == 1.0
    assert fields(row) == ("+17%", "+20%")

    type_into(row.left_field, "-40")  # below the bound
    assert row.track.low_fraction == 0.0

    type_into(row.left_field, "lots")
    assert fields(row) == ("+10%", "+20%"), "garbage reverts"
    assert row.track.low_fraction == 0.0
    assert len(changes) == 5, "a revert is not a change"

    assert card.isVisible()
    assert done == [] and cancelled == []


def test_a_field_commits_on_blur_too(card):
    row = one_type_card(card).add_row(SIG_PCT)
    card.show()
    row.left_field.setFocus()
    row.left_field.selectAll()
    QTest.keyClicks(row.left_field, "12")
    assert row.track.low_fraction == 0.0, "no commit until the field is left"
    row.right_field.setFocus()
    assert row.track.low_fraction == pytest.approx(0.2)


def test_changing_the_attribute_resets_the_range_and_a_rebind_keeps_it(card):
    """A range picked on one stat is no answer about another, so picking a
    different attribute reopens the row on the full range; fresh bounds
    for the same attribute (a fetch landing) move the ends under the
    handles and leave the handles' fractions alone. An attribute the
    estate holds no bounds for is not offered at all."""
    row = one_type_card(card).add_row(SPEED)
    row.set_range(-60.0, -55.0)
    assert row.select_attribute(CPU)
    assert (row.track.low_fraction, row.track.high_fraction) == (0.0, 1.0)
    assert fields(row) == ("31 tf", "24 tf")

    row.set_range(26.0, 30.0)
    card.set_attributes(ATTRS, {**BOUNDS, CPU: (20.0, 34.0)})
    (row,) = card.rows()
    assert (row.track.worst, row.track.best) == (34.0, 20.0)
    assert (row.track.low_fraction, row.track.high_fraction) == pytest.approx((1 / 7, 5 / 7))
    assert row.numeric_range() == pytest.approx((24.0, 32.0)), "the fractions, not the numbers"

    assert not row.select_attribute(SIG_M)
    assert offered(row) == [CPU, SPEED, SIG_PCT]


def test_used_attributes_are_greyed_in_other_rows_and_add_stat_goes_when_none_are_left(card):
    """Add stat appends the first attribute no row holds; every other row
    greys that attribute out; and once every attribute has a row the
    button is gone rather than disabled -- there is nothing it could add.
    Removing a row brings it back."""
    one_type_card(card)
    assert not card.add_row_btn.isHidden()
    first = card.add_row()
    assert first.attribute_id() == CPU
    assert pickable(first) == [CPU, SPEED, SIG_PCT]
    second = card.add_row()
    assert second.attribute_id() == SPEED
    assert pickable(first) == [CPU, SIG_PCT]
    assert pickable(second) == [SPEED, SIG_PCT]
    assert not card.add_row_btn.isHidden()
    third = card.add_row()
    assert third.attribute_id() == SIG_PCT
    assert card.add_row_btn.isHidden()
    assert card.add_row() is None, "nothing left to add"

    second.remove_btn.click()
    assert card.rows() == [first, third]
    assert not card.add_row_btn.isHidden()
    assert pickable(first) == [CPU, SPEED]
    assert card.add_row().attribute_id() == SPEED


def test_a_type_with_one_attribute_hides_add_stat_after_its_one_row(card):
    card.set_types(TYPES, [WEBIFIER])
    card.set_attributes([ATTRS[0]], {CPU: BOUNDS[CPU]})
    assert not card.add_row_btn.isHidden()
    card.add_row()
    assert card.add_row_btn.isHidden()
    assert card.add_row() is None


# ------------------------------------------------------------------ done
def test_done_emits_the_abyssal_chip_plus_one_stat_chip_per_row(card):
    """The exact grammar: stat: in display units, a range when both handles
    are inside, a one-sided comparison when one rests on its end, the
    internal name when the display name is shared -- and never a roll:
    chip, which the card does not build. On the low-is-good speed row the
    RIGHT handle resting on its end is the numeric low bound left open."""
    one_type_card(card)
    cpu = card.add_row(CPU)
    cpu.set_range(26, 30)
    speed = card.add_row(SPEED)
    speed.track._move(LEFT, 0.5)  # the worst-side handle in to -60: -60 and better
    sig = card.add_row(SIG_PCT)
    sig.set_range(10, 15)
    done, cancelled = record(card.done), record(card.cancelled)
    card.show()

    card.done_btn.click()

    assert cancelled == []
    assert done == [(
        [
            omni.Chip("abyssal", WEBIFIER),
            omni.Chip("stat", "CPU usage=26..30"),
            omni.Chip("stat", "Maximum Velocity Bonus<=-60"),
            omni.Chip("stat", "signatureRadiusBonus<=15"),
        ],
    )]
    assert not card.isVisible()
    # Every emitted value is one the grammar accepts as typed.
    for chip in done[0][0][1:]:
        assert omni.parse(f"{chip.kind}:{omni._quote_value(chip.value)}").chips == [chip]


def test_one_sidedness_follows_the_handles_through_the_axis_orientation(card):
    """On a low-is-good CPU row the left handle is the numeric HIGH end:
    dragging it in leaves the numeric low bound open (``<=``), dragging the
    right handle in leaves the high bound open (``>=``)."""
    row = one_type_card(card).add_row(CPU)  # 31 -> 24
    row.track._move(LEFT, 1 / 7)
    assert row.chip() == omni.Chip("stat", "CPU usage<=30")
    row.track._move(LEFT, 0.0)
    row.track._move(RIGHT, 5 / 7)
    assert row.chip() == omni.Chip("stat", "CPU usage>=26")
    row.track._move(LEFT, 1 / 7)
    assert row.chip() == omni.Chip("stat", "CPU usage=26..30")


def test_done_with_the_type_cleared_is_the_plain_abyssal_chip_and_drops_the_rows(card):
    """Clearing the type after building a row widens the filter back to
    every abyssal item: the row stays on the card for a return to the type,
    but belongs to no type meanwhile and must not leak into the chips."""
    one_type_card(card)
    card.add_row(CPU)
    assert len(card.chips()) == 2
    card.show()
    clear_type(card)
    assert card.selected_types() == []
    assert len(card.rows()) == 1, "the row is kept for a return to the type"
    assert card.chips() == [omni.Chip("abyssal", "")]
    done = record(card.done)
    card.done_btn.click()
    assert done == [([omni.Chip("abyssal", "")],)]


def test_two_rows_on_the_same_attribute_both_emit(card):
    """The grammar allows two chips on one stat and the seed reproduces
    them, so an explicit add on a used attribute is honoured; only the
    button's own pick skips used ones."""
    one_type_card(card)
    first, second = card.add_row(CPU), card.add_row(CPU)
    first.set_range(26, 31)
    second.set_range(24, 30)
    chips = card.chips()
    assert chips[1:] == [omni.Chip("stat", "CPU usage>=26"), omni.Chip("stat", "CPU usage<=30")]
    second.remove_btn.click()
    assert card.rows() == [first]


def admits(term: omni.StatTerm, value: float) -> bool:
    """Would the SQL the grammar builds for this term pass the value."""
    if term.op == "..":
        return term.low <= value <= term.high
    return {
        ">=": value >= term.low, ">": value > term.low,
        "<=": value <= term.low, "<": value < term.low,
    }[term.op]


def stat_term(row) -> omni.StatTerm:
    chip = row.chip()
    assert chip is not None and chip.kind == omni.STAT_KIND
    term = omni.parse_stat(chip.value)
    assert term is not None, chip.value
    return term


def test_bounds_round_outward_so_the_items_that_set_them_still_match(card):
    """Pins a bug that shipped: the estate's one BCS has a CPU of
    25.799999713897705 tf (float32 for 25.8), the fields round it to 25.8,
    and a chip written from the fields -- ``CPU usage=25.8..25.8`` --
    matched nothing, because stat: compares the exact display value. The
    low bound must floor and the high bound ceil, in the row's decimals,
    for the full range and for each one-sided comparison alike."""
    exact = 25.799999713897705
    card.set_types(TYPES, [WEBIFIER])
    card.set_attributes(ATTRS, {CPU: (exact, 31.0)})
    row = card.add_row(CPU)
    assert row.decimals == 1

    row.set_range(exact, 31.0)
    assert fields(row) == ("31 tf", "25.8 tf"), "what the user reads"
    term = stat_term(row)
    assert (term.op, term.low, term.high) == ("..", 25.7, 31.0)
    assert admits(term, exact)

    row.set_range(exact, 28.04)  # the best handle on its end: one-sided <=
    assert row.left_field.text() == "28 tf", "the field rounds to nearest"
    term = stat_term(row)
    assert (term.op, term.low) == ("<=", 28.1) and admits(term, 28.04)

    row.set_range(27.96, 31.0)  # the worst handle on its end: one-sided >=
    term = stat_term(row)
    assert (term.op, term.low) == (">=", 27.9) and admits(term, 27.96)

    # A bound typed into the field comes back from the track's fraction
    # with float noise (25.999999999999996 or 26.000000000000004) and must
    # still read as 26, not as a visibly widened 25.9 or 26.1.
    type_into(row.left_field, "30")
    type_into(row.right_field, "26")
    term = stat_term(row)
    assert (term.op, term.low, term.high) == ("..", 26.0, 30.0)


def test_a_degenerate_one_item_range_still_emits_a_chip_that_admits_the_item(card):
    """One fetched item gives bounds of (v, v): whichever handle is deemed
    to rest on its end, the chip must admit v itself."""
    exact = 25.799999713897705
    card.set_types(TYPES, [WEBIFIER])
    card.set_attributes(ATTRS, {CPU: (exact, exact)})
    row = card.add_row(CPU)
    assert fields(row) == ("25.8 tf", "25.8 tf")
    assert admits(stat_term(row), exact)
    row.set_range(exact, exact)
    assert admits(stat_term(row), exact)
    row.track._move(RIGHT, 1.0)  # a drag on a zero-length track
    assert admits(stat_term(row), exact)


def test_negative_bounds_round_toward_minus_infinity_low_and_plus_infinity_high(card):
    """Outward means away from the range, not away from zero: a webifier's
    speed factor runs -62.95..-51.05, so the low bound must become -63 (not
    -62.9) and the high bound -51 (not -51.1). Handles placed inside the
    range widen the same way."""
    card.set_types(TYPES, [WEBIFIER])
    card.set_attributes(ATTRS, {SPEED: (-62.95, -51.05)})
    row = card.add_row(SPEED)
    row.set_range(-62.95, -51.05)
    term = stat_term(row)
    assert (term.op, term.low, term.high) == ("..", -63.0, -51.0)
    assert admits(term, -62.95) and admits(term, -51.05)

    row.set_range(-60.04, -55.96)
    term = stat_term(row)
    assert (term.op, term.low, term.high) == ("..", -60.1, -55.9)
    assert admits(term, -60.04) and admits(term, -55.96)

    # Exact whole numbers stay exactly themselves in both directions.
    card.set_attributes(ATTRS, {SPEED: (-63.0, -51.0)})
    (row,) = card.rows()
    row.set_range(-63.0, -51.0)
    term = stat_term(row)
    assert (term.low, term.high) == (-63.0, -51.0)


def test_cancel_escape_and_an_outside_close_emit_cancelled_and_nothing_else(card):
    """Qt closes a popup on any outside click; that close must read as
    Cancel, or an abandoned exploratory drag would rewrite the filter."""
    one_type_card(card)
    card.add_row(CPU)
    done, cancelled = record(card.done), record(card.cancelled)

    card.show()
    card.cancel_btn.click()
    assert (len(cancelled), len(done)) == (1, 0)
    assert not card.isVisible()

    card.show()
    QTest.keyClick(card, Qt.Key_Escape)
    assert (len(cancelled), len(done)) == (2, 0)

    card.show()
    card.hide()  # what Qt does on an outside click
    assert (len(cancelled), len(done)) == (3, 0)


def test_enter_outside_an_edit_is_done(card):
    """Done is the default action: Enter on the track or on a button applies
    the chips as they stand. The two edits are the exceptions -- a bound
    field commits its number, the type edit its search (pinned below)."""
    row = one_type_card(card).add_row(CPU)
    done, cancelled = record(card.done), record(card.cancelled)
    card.show()
    row.track.setFocus()
    QTest.keyClick(row.track, Qt.Key_Return)
    assert done == [([omni.Chip("abyssal", WEBIFIER), omni.Chip("stat", "CPU usage=24..31")],)]
    assert cancelled == [] and not card.isVisible()

    card.show()
    card.add_row_btn.setFocus()
    QTest.keyClick(card.add_row_btn, Qt.Key_Return)
    assert len(done) == 2 and not card.isVisible()


# ------------------------------------------------------------------ seed
def test_seed_round_trips_chips_back_into_rows_and_out_again(card):
    """A card opened and Done'd untouched must hand back the chips it was
    given: the type picked, one row per stat: chip in order, aliases and
    shared names resolved. Strict operators land on the inclusive handle --
    the track has no "just above" -- and a chip about a stat this type
    does not roll is dropped rather than bound to the wrong attribute."""
    chips = [
        omni.Chip("category", "Module"),
        omni.Chip("abyssal", WEBIFIER),
        omni.Chip("stat", "cpu=26..30"),
        omni.Chip("stat", "Maximum Velocity Bonus>=-60"),
        omni.Chip("stat", "sig<=15"),
        omni.Chip("stat", "falloff>=50"),
        omni.Chip("stat", "Signature Radius Modifier>15"),
        omni.Chip("stat", "cpu<30", negated=True),
    ]
    card.seed(chips)
    assert card.seeded_types() == [WEBIFIER]
    assert card.rows() == [], "rows wait for the attribute list"
    card.set_types(TYPES, card.seeded_types())
    assert card.selected_types() == [WEBIFIER]
    card.set_attributes(ATTRS, BOUNDS)

    rows = card.rows()
    assert [r.attribute_id() for r in rows] == [CPU, SPEED, SIG_PCT, SIG_PCT]
    assert card.chips() == [
        omni.Chip("abyssal", WEBIFIER),
        omni.Chip("stat", "CPU usage=26..30"),
        omni.Chip("stat", "Maximum Velocity Bonus>=-60"),
        omni.Chip("stat", "signatureRadiusBonus<=15"),
        omni.Chip("stat", "signatureRadiusBonus>=15"),
    ]
    assert fields(rows[0]) == ("30 tf", "26 tf"), "worst side left, on a low-is-good stat"
    assert fields(rows[1]) == ("-55%", "-60%")
    assert card.add_row_btn.isHidden(), "every attribute has a row"


def test_a_chip_on_the_exact_display_value_seeds_its_row_and_comes_back_as_typed(card):
    """A chip written against the value the table shows -- ``stat:"CPU
    usage">=25.8`` for the BCS whose CPU is 25.799999713897705 tf -- must
    land on the CPU row with its handle where the user put it, and Done
    must hand the same chip back: the handle sits a hair inside the end,
    not on it, so the comparison stays one-sided and the outward floor
    keeps 25.8 as 25.8."""
    exact = 25.799999713897705
    card.seed([omni.Chip("abyssal", WEBIFIER), omni.Chip("stat", "CPU usage>=25.8")])
    card.set_types(TYPES, card.seeded_types())
    card.set_attributes(ATTRS, {CPU: (exact, 31.0), SPEED: BOUNDS[SPEED]})
    (row,) = card.rows()
    assert row.attribute_id() == CPU
    assert fields(row) == ("31 tf", "25.8 tf")
    assert card.chips() == [omni.Chip("abyssal", WEBIFIER), omni.Chip("stat", "CPU usage>=25.8")]


def test_roll_chips_are_neither_seeded_nor_emitted_and_are_not_the_cards_kinds(card):
    """The card speaks display units only, so a roll: chip -- a quality
    percent -- is nothing it can show or rebuild. Seeding ignores it, Done
    never writes one, and CARD_KINDS leaves it out so the view's
    replacement on Done passes a typed one through (pinned end to end in
    test_assets_integration)."""
    assert ac.CARD_KINDS == ("abyssal", "stat")
    card.seed([
        omni.Chip("abyssal", WEBIFIER),
        omni.Chip("roll", "cpu>=70"),
        omni.Chip("roll", "web=50..100"),
        omni.Chip("stat", "cpu>=26"),
    ])
    card.set_types(TYPES, card.seeded_types())
    card.set_attributes(ATTRS, BOUNDS)
    assert [r.attribute_id() for r in card.rows()] == [CPU]
    assert card.chips() == [omni.Chip("abyssal", WEBIFIER), omni.Chip("stat", "CPU usage>=26")]


def test_a_seeded_chip_about_an_attribute_with_no_bounds_is_dropped_not_zeroed(card):
    """No fetched item of the type rolls signatureRadiusAdd, so it has no
    estate bounds; a stat: chip naming it (typed, or saved before a
    re-fetch) used to be forced onto (0, 0) bounds and re-emitted as
    ``signatureRadiusAdd=0..0`` -- a chip about nothing. It is dropped like
    a chip about an attribute the type does not roll."""
    card.seed([
        omni.Chip("abyssal", WEBIFIER),
        omni.Chip("stat", "signatureRadiusAdd>=5"),
        omni.Chip("stat", "cpu>=26"),
    ])
    card.set_types(TYPES, card.seeded_types())
    card.set_attributes(ATTRS, BOUNDS)
    assert [r.attribute_id() for r in card.rows()] == [CPU]
    assert card.chips() == [omni.Chip("abyssal", WEBIFIER), omni.Chip("stat", "CPU usage>=26")]


def test_reseeding_starts_over_and_a_vanished_type_lists_with_count_zero(card):
    """A saved view can outlive its items: the type must still be visible
    (and replaceable) in the dropdown rather than silently filtering to
    nothing -- before the fresh type list lands as well as after, so the
    card shown between the two never flashes last time's pick. And a
    second open forgets the first open's rows."""
    one_type_card(card)
    card.add_row(CPU)
    selections = record(card.selection_changed)
    changes = record(card.filter_changed)
    card.seed([omni.Chip("abyssal", "Abyssal Warp Disruptor")])
    assert card.rows() == []
    assert card.selected_types() == ["Abyssal Warp Disruptor"], "selected from the old list"
    card.set_types(TYPES, card.seeded_types())
    assert entries(card) == [
        (WEBIFIER, "Stasis Webifier · 12"),
        (MWD, "50MN Microwarpdrive · 3"),
        ("Abyssal Warp Disruptor", "Warp Disruptor · 0"),
    ]
    assert card.selected_types() == ["Abyssal Warp Disruptor"]
    assert selections == [], "seeding and listing are both silent"
    assert changes == [], "the view fetches after a seed and that fetch announces"


def test_seeding_a_chip_with_several_types_picks_the_first_and_done_writes_only_it(card):
    """The grammar can OR several types inside one chip and the dropdown
    cannot: the card shows the first and Done narrows the chip to it. The
    rows stay usable, since one type is what they need."""
    card.seed([
        omni.Chip("abyssal", f"{MWD}, {WEBIFIER}"),
        omni.Chip("stat", "cpu>=26"),
    ])
    assert card.seeded_types() == [MWD]
    card.set_types(TYPES, card.seeded_types())
    card.set_attributes(ATTRS, BOUNDS)
    assert card.selected_types() == [MWD]
    assert card.rows_box.isEnabled()
    done = record(card.done)
    card.show()
    card.done_btn.click()
    assert done == [([omni.Chip("abyssal", MWD), omni.Chip("stat", "CPU usage>=26")],)]


def test_the_type_dropdowns_own_list_picks_without_taking_the_card_down(card):
    """The dropdown's list is a popup of its own inside the card's popup:
    opening it, walking it with the keys and closing it by Enter, Escape or
    a click must change the pick without taking the card down, and
    without reaching Done -- the list consumes its own Enter. With no type
    picked the list opens on its first entry (Qt's own rule for a combo
    without a current index), so Down then Enter takes the second."""
    card.set_types(TYPES, [])
    done, cancelled = record(card.done), record(card.cancelled)
    card.show()
    combo = card.type_combo

    combo.showPopup()
    assert combo.view().isVisible() and card.isVisible()
    QTest.keyClick(combo.view(), Qt.Key_Down)
    QTest.keyClick(combo.view(), Qt.Key_Return)
    assert not combo.view().isVisible()
    assert card.isVisible() and card.selected_types() == [MWD]

    combo.showPopup()
    QTest.keyClick(combo.view(), Qt.Key_Escape)
    assert not combo.view().isVisible() and card.isVisible()

    combo.showPopup()
    view = combo.view()
    QTest.mouseClick(
        view.viewport(), Qt.LeftButton, pos=view.visualRect(view.model().index(0, 0)).center()
    )
    assert not view.isVisible() and card.isVisible()
    assert card.selected_types() == [WEBIFIER]
    assert done == [] and cancelled == []


def test_switching_type_keeps_rows_the_new_type_rolls_and_drops_the_rest(card):
    """A row survives a switch only while the new type's estate still
    bounds its attribute: an attribute the list names but holds no bounds
    for is as good as unrolled, since a track over nothing could only emit
    a chip matching nothing."""
    one_type_card(card)
    card.add_row(CPU)
    card.add_row(SIG_PCT)
    card.set_attributes([], {})  # No type picked meanwhile: rows only disabled
    assert len(card.rows()) == 2
    card.set_attributes(ATTRS, {CPU: BOUNDS[CPU]})  # SIG_PCT listed, its bounds gone
    assert [r.attribute_id() for r in card.rows()] == [CPU]
    card.set_attributes([ATTRS[0]], {CPU: BOUNDS[CPU]})
    assert [r.attribute_id() for r in card.rows()] == [CPU]


# ------------------------------------------------------------ type search
def test_the_card_opens_with_the_type_edit_focused_and_its_label_selected(card):
    """The first keystroke must start a search, not append to the current
    label -- both when the card opens on a list it already has and when the
    list lands after the card is up, which is the real order (the fetch
    answers after show) and which rewrites the edit's text."""
    card.set_types(TYPES, [WEBIFIER])
    card.show()
    edit = card.type_edit
    assert edit.placeholderText() == ac.TYPE_PLACEHOLDER
    assert edit.text() == WEBIFIER_LABEL
    assert edit.hasFocus() and edit.selectedText() == WEBIFIER_LABEL

    card.set_types(TYPES, [MWD])
    assert edit.text() == MWD_LABEL and edit.selectedText() == MWD_LABEL

    card.hide()
    card.set_types(TYPES, [WEBIFIER])
    card.show()
    assert edit.hasFocus() and edit.selectedText() == WEBIFIER_LABEL, "every open, not the first"


def test_typing_in_the_type_edit_offers_the_matching_types_and_enter_takes_the_first(card):
    """"web" narrows the list to the webifier and Enter -- arriving through
    the completer's popup, as it does on a desktop -- selects it the way
    the dropdown would: the rows come alive, selection_changed fires once
    with the type, the edit shows the entry's label, and the card stays up
    with nothing applied. Down highlights a match (Qt writes its label into
    the edit) and Enter then takes that one."""
    card.set_types(TYPES, [])
    selections, done = record(card.selection_changed), record(card.done)
    card.show()
    edit, popup = card.type_edit, completion_popup(card)

    type_search(card, "web")
    assert popup.isVisible() and card.isVisible()
    assert completions(card) == [WEBIFIER_LABEL]
    assert not popup.currentIndex().isValid(), "nothing highlighted until Down"
    QTest.keyClick(popup, Qt.Key_Return)
    assert card.selected_types() == [WEBIFIER]
    assert selections == [([WEBIFIER],)]
    assert card.rows_box.isEnabled()
    assert edit.text() == WEBIFIER_LABEL
    assert not popup.isVisible() and card.isVisible() and done == []
    QtWidgets.QApplication.processEvents()
    assert edit.selectedText() == WEBIFIER_LABEL, "ready for the next search"

    type_search(card, "50mn abyssal")  # the full name, prefix and all, not the label
    assert completions(card) == [MWD_LABEL]
    QTest.keyClick(popup, Qt.Key_Down)
    assert edit.text() == MWD_LABEL
    QTest.keyClick(popup, Qt.Key_Return)
    assert card.selected_types() == [MWD]
    assert selections == [([WEBIFIER],), ([MWD],)]
    assert card.isVisible() and done == []


def test_a_pasted_full_name_is_offered_and_enter_straight_into_the_edit_selects_it(card):
    """A name pasted in one go (insert is what QLineEdit's paste does) is
    matched whole, "Abyssal" prefix and any case, and Enter -- arriving
    straight at the edit rather than through the popup, which is how a
    harness sends it -- selects the entry and takes the popup down too."""
    card.set_types(TYPES, [])
    selections, done = record(card.selection_changed), record(card.done)
    card.show()
    edit, popup = card.type_edit, completion_popup(card)

    edit.setFocus()
    edit.selectAll()
    edit.insert(WEBIFIER.upper())
    assert popup.isVisible() and completions(card) == [WEBIFIER_LABEL]
    QTest.keyClick(edit, Qt.Key_Return)
    assert card.selected_types() == [WEBIFIER] and edit.text() == WEBIFIER_LABEL
    assert selections == [([WEBIFIER],)] and not popup.isVisible()

    type_search(card, "MICRO")
    assert popup.isVisible() and completions(card) == [MWD_LABEL]
    QTest.keyClick(edit, Qt.Key_Return)
    assert card.selected_types() == [MWD] and not popup.isVisible()
    assert selections == [([WEBIFIER],), ([MWD],)]
    assert card.isVisible() and done == []


def test_text_matching_no_type_reverts_on_enter_and_on_blur(card):
    """The edit searches the list and never adds to it: garbage plus Enter,
    and garbage left behind on the way to another widget, both put the
    current entry's label back, leave the selection alone, announce nothing
    and apply nothing -- with a type picked and with none, where the label
    to put back is the empty edit."""
    card.set_types(TYPES, [WEBIFIER])
    selections, done = record(card.selection_changed), record(card.done)
    card.show()
    edit = card.type_edit

    type_search(card, "zzz")
    assert not completion_popup(card).isVisible(), "no match, no popup"
    QTest.keyClick(edit, Qt.Key_Return)
    assert edit.text() == WEBIFIER_LABEL and card.selected_types() == [WEBIFIER]

    type_search(card, "zzz")
    card.done_btn.setFocus()
    assert edit.text() == WEBIFIER_LABEL
    assert selections == [] and done == [] and card.isVisible()

    card.set_types(TYPES, [])
    type_search(card, "zzz")
    QTest.keyClick(edit, Qt.Key_Return)
    assert edit.text() == "" and card.type_combo.currentIndex() == -1
    type_search(card, "zzz")
    card.done_btn.setFocus()
    assert edit.text() == "" and card.type_combo.currentIndex() == -1
    assert selections == [] and done == [] and card.isVisible()


def test_emptying_the_type_edit_deselects_the_type_on_enter_and_on_blur(card):
    """The one way back from a type to every abyssal item without leaving
    the card: an emptied edit plus Enter, or an emptied edit left behind,
    deselects the type -- announced once, like a pick -- so the rows go dark
    and Done writes the bare chip. Emptying an edit that is already empty is
    no change and announces nothing; picking a type again brings the rows
    back with their ranges."""
    one_type_card(card)
    row = card.add_row(CPU)
    row.set_range(26, 30)
    selections, done = record(card.selection_changed), record(card.done)
    card.show()
    edit = card.type_edit

    clear_type(card)
    assert card.type_combo.currentIndex() == -1 and card.selected_types() == []
    assert edit.text() == "" and edit.placeholderText() == ac.TYPE_PLACEHOLDER
    assert selections == [([],)]
    assert not card.rows_box.isEnabled() and card.add_row_btn.isHidden()
    assert card.chips() == [omni.Chip("abyssal", "")]
    assert card.isVisible() and done == []

    clear_type(card)
    assert selections == [([],)], "already none: nothing to announce"

    pick(card, WEBIFIER)
    assert selections == [([],), ([WEBIFIER],)]
    assert card.rows_box.isEnabled()
    assert card.chips()[1] == omni.Chip("stat", "CPU usage=26..30"), "the row kept its range"

    edit.setFocus()
    edit.selectAll()
    QTest.keyClick(edit, Qt.Key_Backspace)
    assert card.selected_types() == [WEBIFIER], "no deselect until the edit is left"
    card.done_btn.setFocus()
    assert card.selected_types() == [] and card.type_combo.currentIndex() == -1
    assert selections == [([],), ([WEBIFIER],), ([],)]

    card.done_btn.click()
    assert done == [([omni.Chip("abyssal", "")],)]


def test_the_completer_offers_types_alone_and_never_an_all_entry(card):
    """"all" finds nothing to complete to and "abyssal", the word on every
    dynamic type, offers exactly the types -- there is no entry standing for
    every module for a search to land on by accident."""
    card.set_types(TYPES, [WEBIFIER])
    selections = record(card.selection_changed)
    card.show()
    popup = completion_popup(card)

    type_search(card, "all")
    assert completions(card) == [] and not popup.isVisible()
    QTest.keyClick(card.type_edit, Qt.Key_Return)
    assert card.selected_types() == [WEBIFIER] and selections == []
    assert card.type_edit.text() == WEBIFIER_LABEL

    type_search(card, "abyssal")
    assert completions(card) == [WEBIFIER_LABEL, MWD_LABEL]
    QTest.keyClick(popup, Qt.Key_Escape)
    assert all(name for name, _label in entries(card)), "every entry is a type"


def test_escape_in_the_type_edit_closes_the_completions_before_it_cancels_the_card(card):
    """The completion popup is a popup inside the card's popup: the first
    Escape -- through the popup as on a desktop, or straight into the edit
    as a harness sends it -- takes only the popup down; the next cancels
    the card as it always did."""
    card.set_types(TYPES, [])
    cancelled = record(card.cancelled)
    card.show()
    edit, popup = card.type_edit, completion_popup(card)

    type_search(card, "web")
    assert popup.isVisible()
    QTest.keyClick(popup, Qt.Key_Escape)
    assert not popup.isVisible() and card.isVisible() and cancelled == []

    type_search(card, "web")
    assert popup.isVisible()
    QTest.keyClick(edit, Qt.Key_Escape)
    assert not popup.isVisible() and card.isVisible() and cancelled == []

    QTest.keyClick(edit, Qt.Key_Escape)
    assert not card.isVisible() and len(cancelled) == 1


def test_clicking_a_completion_selects_it_and_the_arrow_still_lists_every_type(card):
    """The mouse path through the nested popup, and the dropdown's own list
    unfiltered by whatever was typed: the search narrows the completions,
    never the list behind the arrow."""
    card.set_types(TYPES, [])
    selections = record(card.selection_changed)
    card.show()
    popup = completion_popup(card)

    type_search(card, "micro")
    assert popup.isVisible()
    QTest.mouseClick(
        popup.viewport(), Qt.LeftButton, pos=popup.visualRect(popup.model().index(0, 0)).center()
    )
    assert card.selected_types() == [MWD] and selections == [([MWD],)]
    assert not popup.isVisible() and card.isVisible()
    assert card.type_edit.text() == MWD_LABEL

    type_search(card, "web")
    assert completions(card) == [WEBIFIER_LABEL] and popup.isVisible()
    combo = card.type_combo
    combo.showPopup()
    assert not popup.isVisible(), "the list takes the completions down rather than stack on them"
    listed = combo.view().model()
    assert [listed.index(i, 0).data() for i in range(listed.rowCount())] == [
        WEBIFIER_LABEL, MWD_LABEL,
    ]
    QTest.keyClick(combo.view(), Qt.Key_Escape)
    assert card.isVisible() and card.selected_types() == [MWD]


def test_seed_shows_the_seeded_types_label_in_the_edit(card):
    """The edit is the dropdown's face: whatever the seed selects -- from
    the old list before the fresh one lands, from the fresh one after, or a
    type the estate no longer holds -- is what the edit reads."""
    one_type_card(card)
    card.seed([omni.Chip("abyssal", MWD)])
    assert card.type_edit.text() == MWD_LABEL
    card.set_types(TYPES, card.seeded_types())
    assert card.type_edit.text() == MWD_LABEL
    card.seed([omni.Chip("abyssal", "Abyssal Warp Disruptor")])
    assert card.type_edit.text() == "Warp Disruptor · 0"
    card.seed([])
    assert card.type_edit.text() == "" and card.type_combo.currentIndex() == -1


# ------------------------------------------------------------ live count
def test_the_footer_shows_the_count_the_view_delivers_and_asks_again_on_every_change(card):
    """The footer is "N of TOTAL match": the card cannot count, so it
    announces filter_changed on every change to what Done would write --
    a type pick, the attribute list landing, a row added or removed, a
    handle moved, a field committed -- shows "…" until the view answers,
    and shows exactly what set_match_count says."""
    changes = record(card.filter_changed)
    card.set_types(TYPES, [WEBIFIER])
    assert changes == [], "listing types is silent"
    card.set_attributes(ATTRS, BOUNDS)
    assert len(changes) == 1
    assert match_text(card) == "…"
    card.set_match_count(12, 12)
    assert match_text(card) == "12 of 12 match"

    row = card.add_row(CPU)
    assert len(changes) == 2 and match_text(card) == "… of 12 match"
    card.set_match_count(12, 12)
    row.track._move(LEFT, 0.3)
    assert len(changes) == 3 and card.match_count_label.text() == "…"
    card.set_match_count(7, 12)
    type_into(row.right_field, "26")
    assert len(changes) == 4 and card.match_count_label.text() == "…"
    card.set_match_count(4, 12)
    assert match_text(card) == "4 of 12 match"
    row.remove_btn.click()
    assert len(changes) == 5
    pick(card, MWD)
    assert len(changes) == 6
    card.set_match_count(1234, 5678)
    assert match_text(card) == "1,234 of 5,678 match"


# ---------------------------------------------------------------- banner
def test_the_banner_shows_the_pending_count_and_fetch_asks_for_the_picked_type(card):
    fetches = record(card.fetch_requested)
    card.set_types(TYPES, [WEBIFIER])
    card.set_pending(0)
    assert card.banner.isHidden()
    card.set_pending(7)
    assert not card.banner.isHidden()
    assert card.banner_label.text() == "7 abyssal items not fetched —"
    card.set_pending(1)
    assert card.banner_label.text() == "1 abyssal item not fetched —"

    card.fetch_btn.click()
    assert fetches == [([WEBIFIER],)]
    assert not card.fetch_btn.isEnabled(), "one click, one job"
    card.set_pending(3)  # the next look re-arms it
    assert card.fetch_btn.isEnabled()


def test_the_card_is_the_designs_width_and_lays_three_rows_out_offscreen(card):
    """The popover is 440 wide however many rows it holds, and a card with
    three rows paints without error at that width -- the grid's 76 | track
    | 76 columns leave the track room to be a track."""
    one_type_card(card)
    for _ in range(3):
        card.add_row()
    card.show()
    assert card.width() == ac.CARD_WIDTH
    image = card.grab().toImage()
    assert not image.isNull() and image.width() == ac.CARD_WIDTH
    for row in card.rows():
        assert row.left_field.width() == ac.FIELD_WIDTH
        assert row.track.width() >= 150, row.track.width()


# --------------------------------------------------------------- helpers
def test_attribute_chip_names_use_the_internal_name_only_when_the_label_is_ambiguous():
    assert ac.attribute_chip_name(ATTRS[0]) == "CPU usage"
    assert ac.attribute_chip_name(ATTRS[2]) == "signatureRadiusBonus"
    assert ac.attribute_chip_name(ATTRS[3]) == "signatureRadiusAdd"
    # queries falls back to "(internal name)" when units are missing or shared.
    assert ac.attribute_chip_name({"name": "x", "label": "Foo (x)", "unit": None}) == "x"
    # An alias that points at this very attribute leaves the label usable...
    assert ac.attribute_chip_name({"name": "shieldBonus", "label": "Shield", "unit": None}) == "Shield"
    # ...but one that points elsewhere would be resolved first by the grammar.
    assert ac.attribute_chip_name({"name": "armorHP", "label": "Armor", "unit": None}) == "armorHP"


def test_bounds_are_written_the_way_the_fields_show_them():
    assert ac._number(60.0, 0) == "60"
    assert ac._number(27.50, 1) == "27.5"
    assert ac._number(-63.2, 1) == "-63.2"
    assert ac._number(-0.0, 2) == "0"
    assert ac._number(1.10, 2) == "1.1"
    assert ac._decimals(24.0, 31.0) == 1
    assert ac._decimals(-65.0, -55.0) == 1
    assert ac._decimals(0.8, 1.2) == 2
    assert ac._decimals(1200.0, 3400.0) == 0


def test_field_text_carries_the_unit_and_the_modifier_sign_and_parses_back():
    """The fields show what the game shows -- "-60%", "+11%", "26 tf",
    "3,318 HP" -- and read back their own text, a bare number, or a
    number with junk around it; text with no number is None."""
    assert ac._fmt(-60.0, 1, ATTRS[1]) == "-60%"
    assert ac._fmt(11.0, 1, ATTRS[2]) == "+11%"
    assert ac._fmt(0.0, 1, ATTRS[2]) == "0%"
    assert ac._fmt(25.8, 1, ATTRS[0]) == "25.8 tf"
    assert ac._fmt(1.115, 2, {"unit_id": 105, "unit": "x"}) == "1.11x"
    assert ac._fmt(3318.0, 0, {"unit_id": 9, "unit": "HP"}) == "3318 HP"
    assert ac._parse_number("-60%") == -60.0
    assert ac._parse_number("+11%") == 11.0
    assert ac._parse_number(" 26 tf") == 26.0
    assert ac._parse_number("3,318 HP") == 3318.0
    assert ac._parse_number(".5") == 0.5
    assert ac._parse_number("tf") is None
    assert ac._parse_number("") is None
