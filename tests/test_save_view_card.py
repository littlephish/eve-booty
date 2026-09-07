"""The Save card: naming a view, trimming it, keying it, and the two ways out.

The card is not a filter, and everything pinned here follows from that: Done
must ask for a save rather than emit the shell's ``done`` (which would send
the card's empty chip list through the view's chip-replacement path and
clear the very filter being saved), the footer must ask no count, a cross on
a chip in the well must leave the live omnibox alone, the suggestion in the
name field must stop following the filter the moment the user types their
own name, and the posture controls must open on the tab's own posture and
change only what the save stores.
"""

from __future__ import annotations

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from evasset import omni, queries, views  # noqa: E402
from evasset.ui.save_view_card import (  # noqa: E402
    DEFAULT_GROUP_OPTIONS,
    EMPTIED_TEXT,
    SaveViewCard,
)

FILTER = 'owner:Main cat:Ship loc:"Jita IV - Moon 4"'
STATE = views.ViewState(FILTER, "location", "owner", "name")
# suggest_name cuts at forty characters, which this filter passes.
SUGGESTED = "owner Main · cat Ship · loc Jita IV - M…"
LEVEL_KEYS = [key for _label, key in queries.ROLLUP_LEVELS]
# What Done emits for STATE with nothing changed but the name.
POSTURE = ("location", "owner", "name")


def make_view(view_id: int, name: str, slot=None, filter_text: str = "") -> object:
    return views.View(
        view_id=view_id,
        name=name,
        slot=slot,
        state=views.ViewState(filter_text, "", ""),
        created_at="2026-09-05T10:00:00+00:00",
        updated_at="2026-09-05T10:00:00+00:00",
    )


LIBRARY = [
    make_view(1, "Jita ships", 1, FILTER),
    make_view(7, "Short of paste", 3, 'holds:"Nanite Repair Paste"<500'),
    make_view(9, "Main hulls", None, "owner:Main cat:Ship"),
]


@pytest.fixture
def app():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def card(app):
    c = SaveViewCard()
    c.seed([])
    c.set_view(STATE, LIBRARY)
    yield c
    c.hide()
    c.deleteLater()


def record(signal) -> list:
    calls: list = []
    signal.connect(lambda *args: calls.append(args))
    return calls


def pill_texts(card) -> list[str]:
    return [p.prefix_label.text() + p.value_label.text() for p in card.pills()]


def test_the_card_shows_the_filter_as_chips_and_suggests_a_name(card):
    """A view is four things, and three of them are invisible in the
    omnibox: the card opens its group-by, rail level and rail sort controls
    on the tab's own so a save is never a guess about what it captured.
    The chips wear the omnibox's own spelling so the well reads as the
    field does."""
    assert pill_texts(card) == ["owner:Main", "cat:Ship", "loc:Jita IV - Moon 4"]
    assert card.well_text() == ""
    assert card.group_combo.currentText() == "Location"
    assert card.group_by() == "location"
    assert card.rail_bar.level.currentText() == "Owner"
    assert card.rail_level() == "owner"
    assert card.rail_sort() == "name"
    assert card.rail_bar.sort_buttons["name"].isChecked()
    assert not card.rail_bar.sort_buttons["value"].isChecked()
    assert card.name_edit.text() == SUGGESTED
    assert card.name_edit.placeholderText() == SUGGESTED
    assert card.status_label.text() == "3 saved views"
    assert card.done_btn.text() == "Save view"
    assert card.done_btn.isEnabled()


def test_an_unfiltered_view_says_so_and_stays_saveable(app):
    """The one deliberate departure from the design: an unfiltered table is
    a posture worth saving (the pills stay up on an empty field for the
    same reason), so the well says "Unfiltered" and Save stays enabled --
    only Copy as text goes, having nothing to copy."""
    c = SaveViewCard()
    c.seed([])
    c.set_view(views.ViewState(), [])
    assert c.pills() == []
    assert c.well_text() == views.UNFILTERED
    assert c.group_by() == "" and c.group_combo.currentText() == "None"
    assert c.rail_level() == "location" and c.rail_sort() == "value", "the tab's defaults"
    assert c.done_btn.isEnabled()
    assert not c.copy_btn.isEnabled()
    assert c.name_edit.text() == views.UNFILTERED
    assert c.status_label.text() == "0 saved views"
    c.set_view(STATE, [])
    assert c.copy_btn.isEnabled()
    c.deleteLater()


def test_a_cross_takes_the_chip_out_of_the_saved_view_and_not_the_omnibox(card):
    """A view is often the working filter minus the one chip that was a
    detour. The cross trims what the save stores; the state the card was
    handed is untouched, which is what the view's own omnibox goes on
    showing."""
    saved = record(card.save_requested)
    card.pills()[0].close_btn.click()

    assert pill_texts(card) == ["cat:Ship", "loc:Jita IV - Moon 4"]
    assert card.filter_line() == 'cat:Ship loc:"Jita IV - Moon 4"'
    assert STATE.filter == FILTER, "the state handed in is never edited"
    assert card.name_edit.text() == "cat Ship · loc Jita IV - Moon 4", "the suggestion follows"

    card.done_btn.click()
    assert saved == [
        ("cat Ship · loc Jita IV - Moon 4", 'cat:Ship loc:"Jita IV - Moon 4"', None, *POSTURE)
    ]


def test_a_filter_emptied_by_crosses_is_nothing_to_save(card):
    """The user had a filter and crossed all of it out: that is not the
    unfiltered posture they might want to keep, it is a change of mind, and
    Save must not store an empty view under a name derived from nothing."""
    for pill in list(card.pills()):
        pill.close_btn.click()
    assert card.pills() == []
    assert card.well_text() == EMPTIED_TEXT
    assert not card.done_btn.isEnabled()
    assert not card.copy_btn.isEnabled()

    saved = record(card.save_requested)
    card.show()
    QTest.keyClick(card, Qt.Key_Return)
    assert saved == [] and card.isVisible()


def test_bare_search_words_ride_in_the_well_as_chips_and_come_back_out_as_words(app):
    """A bare word is a filter too, and the omnibox carries it in the line
    edit rather than as a chip; the well shows it as a chip with no prefix
    so it can be crossed out like any other, and the line the save stores
    puts it back as a word."""
    c = SaveViewCard()
    c.seed([])
    c.set_view(views.ViewState("paste owner:Main", "", ""), [])
    assert pill_texts(c) == ["owner:Main", "paste"]
    assert c.filter_line() == "owner:Main paste"
    c.pills()[0].close_btn.click()
    assert c.filter_line() == "paste"
    assert c.name_edit.text() == "paste"
    c.deleteLater()


def test_the_suggestion_fills_a_blank_field_and_then_yields_to_a_typed_name(card):
    """Renaming is the point of an editable field: a second fetch (the card
    reopened on a changed filter) or a cross on a chip must not rename the
    view under the user. A blank field saves under the placeholder."""
    card.name_edit.setText("Ratting fleet")
    card.set_view(views.ViewState("cat:Ship owner:Alt"), [])
    assert card.name_edit.text() == "Ratting fleet"
    card.pills()[0].close_btn.click()
    assert card.name_edit.text() == "Ratting fleet"

    card.name_edit.setText("owner Alt")  # still the suggestion
    card.set_view(views.ViewState("cat:Ship"), [])
    assert card.name_edit.text() == "cat Ship"

    card.name_edit.clear()
    card.name_edit.textEdited.emit("")
    assert card.name() == "cat Ship", "a blank field means the placeholder"
    assert card.done_btn.isEnabled()


def test_a_name_already_in_the_library_says_what_it_will_replace(card):
    """A save replaces silently, which is right for the common act of
    refining a view -- so the notice is the only warning there is, and it
    has to fold case the way save-by-name does."""
    card.name_edit.setText("")
    QTest.keyClicks(card.name_edit, "JITA SHIPS")
    assert card.replace_label.text() == "Replaces “Jita ships”", "the stored spelling"
    assert not card.replace_label.isHidden()

    card.name_edit.selectAll()
    QTest.keyClicks(card.name_edit, "Jita ships II")
    assert card.replace_label.text() == ""
    assert card.replace_label.isHidden()


def test_a_filter_another_view_already_holds_is_pointed_out_but_not_blocked(card):
    """A second copy of a line is usually a forgotten name rather than a
    wish, so the card says which view holds it -- in the warning colour,
    since nothing is wrong yet -- and gets out of the way. The notice
    follows the crosses, and goes quiet when the name is that view's own,
    where "Replaces" already says it all."""
    assert card.dupe_label.text() == "Same filter as “Jita ships”"
    assert not card.dupe_label.isHidden()
    assert card.done_btn.isEnabled()

    card.name_edit.setText("Jita ships")
    card.name_edit.textEdited.emit("Jita ships")
    assert card.dupe_label.isHidden(), "replacing that view is not duplicating it"

    card.name_edit.setText("Hulls")
    card.name_edit.textEdited.emit("Hulls")
    card.pills()[2].close_btn.click()
    assert card.dupe_label.text() == "Same filter as “Main hulls”"
    card.pills()[1].close_btn.click()
    assert card.dupe_label.isHidden()


def test_the_key_cell_puts_the_view_on_a_digit_and_names_whose_digit_it_takes(card):
    """A digit holds one view, so picking a taken one moves it -- the store's
    rule -- and the hint says so before Save does it. The menu mutes the
    taken digits and tooltips the holder, so the move is never a surprise.
    A slot the view being replaced already holds is not a conflict."""
    saved = record(card.save_requested)
    assert card.key_cell.label() == "–" and card.slot() is None
    assert card.conflict_label.isHidden()

    card.key_cell.open_menu()
    menu = card.key_cell.menu
    assert menu.isVisible()
    assert menu.buttons[3].toolTip() == "Take from “Short of paste”"
    assert menu.buttons[5].toolTip() == ""
    menu.buttons[3].click()

    assert not menu.isVisible()
    assert card.slot() == 3 and card.key_cell.label() == "3"
    assert card.conflict_label.text() == "3 is currently “Short of paste” — saving moves it to this view"
    assert not card.conflict_label.isHidden()

    card.key_cell.pick(5)
    assert card.conflict_label.isHidden()

    card.key_cell.pick(1)
    card.name_edit.setText("Jita ships")
    card.name_edit.textEdited.emit("Jita ships")
    assert card.conflict_label.isHidden(), "its own digit"

    card.done_btn.click()
    assert saved == [("Jita ships", FILTER, 1, *POSTURE)]


def test_escape_in_the_key_menu_closes_the_menu_and_not_the_card(card, app):
    """Esc ladders out one step at a time: the menu first, then the card."""
    cancelled = record(card.cancelled)
    card.show()
    card.key_cell.open_menu()
    QTest.keyClick(card.key_cell.menu, Qt.Key_Escape)
    assert not card.key_cell.menu.isVisible()
    assert card.isVisible() and cancelled == []
    assert card.slot() is None


def test_enter_in_the_name_field_saves_once_and_never_emits_done(card):
    """Enter is how a name is finished, and it must reach the shell: the
    card deliberately puts no event filter on the field. What it must never
    do is emit the shell's done, which the view would read as "replace my
    kinds" and answer by clearing the filter being saved."""
    saved, done, cancelled = (
        record(card.save_requested), record(card.done), record(card.cancelled)
    )
    card.show()
    card.name_edit.setFocus()
    QTest.keyClick(card.name_edit, Qt.Key_Return)

    assert saved == [(SUGGESTED, FILTER, None, *POSTURE)]
    assert done == []
    assert not card.isVisible()
    assert cancelled == [], "an applied hide is not a cancel"


def test_done_trims_the_name_it_asks_to_save(card):
    saved = record(card.save_requested)
    card.name_edit.setText("  Jita ships  ")
    card.done_btn.click()
    assert saved == [("Jita ships", FILTER, None, *POSTURE)]


# ------------------------------------------------------------------ posture
def test_the_posture_controls_change_what_done_stores_and_nothing_else(card):
    """The controls are the crosses' equivalent for the other three columns:
    a view is sometimes "this filter, grouped by owner, rail on region by
    volume" while the table is still flat from the last question, and
    setting the tab up first only to put it back is the detour the card
    exists to spare. What they change is the emitted posture; the state
    handed in, like the omnibox, is never touched."""
    saved = record(card.save_requested)
    card.group_combo.setCurrentIndex(card.group_combo.findData("owner"))
    card.rail_bar.level.setCurrentIndex(LEVEL_KEYS.index("region"))
    card.rail_bar.sort_buttons["volume"].click()

    assert (card.group_by(), card.rail_level(), card.rail_sort()) == ("owner", "region", "volume")
    assert STATE == views.ViewState(FILTER, "location", "owner", "name"), "never edited"

    card.done_btn.click()
    assert saved == [(SUGGESTED, FILTER, None, "owner", "region", "volume")]


def test_the_flat_table_is_a_grouping_the_card_can_store(card):
    """"None" is a posture, not the absence of one: a view saved flat must
    load flat, which needs the combo's "" key to travel rather than be
    dropped as falsy somewhere between the combo and the signal."""
    saved = record(card.save_requested)
    card.group_combo.setCurrentIndex(0)
    assert card.group_combo.currentText() == "None" and card.group_by() == ""
    card.done_btn.click()
    assert saved == [(SUGGESTED, FILTER, None, "", "owner", "name")]


def test_the_group_by_choices_are_the_tabs_own_when_it_hands_them_over(app):
    """The card must never offer a grouping the tab cannot show: the view
    passes its combo's own (label, key) pairs, and the card takes them --
    keeping the current state's selection -- while a card fed nothing keeps
    the same construction the tab uses."""
    c = SaveViewCard()
    c.seed([])
    options = [(label, key) for label, key in DEFAULT_GROUP_OPTIONS]
    assert [
        (c.group_combo.itemText(i), c.group_combo.itemData(i))
        for i in range(c.group_combo.count())
    ] == options

    two = [("Flat", ""), ("By owner", "owner")]
    c.set_view(views.ViewState("cat:Ship", "owner", "region", "value"), [], two)
    assert c.group_combo.count() == 2
    assert c.group_combo.currentText() == "By owner" and c.group_by() == "owner"

    c.set_view(views.ViewState("cat:Ship", "region", "region", "value"), [], two)
    assert c.group_by() == "", "a grouping the tab does not offer falls back to flat"
    c.deleteLater()


def test_the_rail_row_is_the_rails_own_header(card):
    """Drawn by the rail's LevelSortBar rather than restyled by hand, so the
    two cannot drift apart: the same levels in the same order, the same
    three segments with the same captions, and the same no-op on a click of
    the segment already in force."""
    from evasset.ui.rail import LevelSortBar, Rail

    assert isinstance(card.rail_bar, LevelSortBar)
    rail = Rail()
    assert [card.rail_bar.level.itemText(i) for i in range(card.rail_bar.level.count())] == [
        rail.level.itemText(i) for i in range(rail.level.count())
    ]
    assert [b.text() for b in card.rail_bar.sort_buttons.values()] == [
        b.text() for b in rail.sort_buttons.values()
    ] == ["ISK", "A-Z", "m³"]
    changes = record(card.rail_bar.sort_changed)
    card.rail_bar.sort_buttons["name"].click()
    assert changes == [] and card.rail_sort() == "name", "already in force: no change"
    card.rail_bar.sort_buttons["value"].click()
    assert changes == [("value",)]
    rail.deleteLater()


def test_cancel_and_escape_forget_the_posture_along_with_everything_else(card, app):
    """Every way out but Done is Cancel, and the next visit opens on the
    tab's posture again rather than on the last visit's unsent choices."""
    saved, cancelled = record(card.save_requested), record(card.cancelled)
    card.group_combo.setCurrentIndex(card.group_combo.findData("owner"))
    card.rail_bar.sort_buttons["volume"].click()
    card.show()
    QTest.keyClick(card, Qt.Key_Escape)
    assert saved == [] and len(cancelled) == 1

    card.seed([])
    assert (card.group_by(), card.rail_level(), card.rail_sort()) == ("", "location", "value")
    card.set_view(STATE, LIBRARY)
    assert (card.group_by(), card.rail_level(), card.rail_sort()) == POSTURE
    card.cancel_btn.click()
    assert saved == []


def test_escape_leaves_the_library_untouched(card):
    """Every way out but Done is Cancel, the shell's rule: a card opened to
    look at what a save would capture must not save anything."""
    saved, cancelled = record(card.save_requested), record(card.cancelled)
    card.show()
    QTest.keyClick(card, Qt.Key_Escape)
    assert saved == [] and len(cancelled) == 1


def test_copy_as_text_puts_the_trimmed_filter_line_on_the_clipboard(card):
    """The line is the whole shareable half of a view, and the Load card's
    paste box is the other end of it. What is copied is what would be
    saved, crosses included."""
    QGuiApplication.clipboard().setText("something else")
    card.copy_btn.click()
    assert QGuiApplication.clipboard().text() == FILTER
    assert card.status_label.text() == "Copied."
    card.pills()[0].close_btn.click()
    card.copy_btn.click()
    assert QGuiApplication.clipboard().text() == 'cat:Ship loc:"Jita IV - Moon 4"'


def test_the_card_asks_the_footer_nothing_and_writes_no_chips(card):
    """A command card is not a filter: it owns no chip of any kind, hands
    Done nothing to write, and names the "none" totals shape so the view
    runs no count query at all."""
    assert card.chips() == []
    assert card.totals() == ("none", None)
    assert not card.SHOWS_COUNT
    assert not card.owns(object())


def test_opening_the_card_focuses_the_name_with_the_suggestion_selected(card, app):
    """The fast path is Enter alone; the second-fastest is typing a name
    straight over the suggestion, which needs it selected, not appended to."""
    card.show()
    app.processEvents()
    assert card.first_focus() is card.name_edit
    assert card.name_edit.hasFocus()
    assert card.name_edit.selectedText() == SUGGESTED


def test_seeding_forgets_the_last_visit(card):
    """One card instance serves the tab's whole life, so a fresh open must
    not offer the name, the key, the crossed-out chips or the status line
    of the last one -- Cancel restores everything by way of the next seed."""
    card.name_edit.setText("Ratting fleet")
    card.key_cell.pick(4)
    card.pills()[0].close_btn.click()
    card.status_label.setText("Copied.")

    card.seed([])

    assert card.name_edit.text() == ""
    assert card.slot() is None and card.key_cell.label() == "–"
    assert card.pills() == []
    assert card.status_label.text() == ""

    card.set_view(STATE, LIBRARY)
    assert pill_texts(card) == ["owner:Main", "cat:Ship", "loc:Jita IV - Moon 4"]
    assert card.name_edit.text() == SUGGESTED


def test_chips_of_every_kind_wear_the_omnibox_spelling(app):
    """The well is the field in miniature: a negated chip carries its minus,
    the abyssal chip its summary label and no prefix, so a pilot reads the
    well the way they read the omnibox."""
    c = SaveViewCard()
    c.seed([])
    c.set_view(views.ViewState("-owner:Alt abyssal is:bpc"), [])
    assert pill_texts(c) == ["-owner:Alt", "Abyssal", "is:bpc"]
    assert [p.token for p in c.pills()] == omni.parse("-owner:Alt abyssal is:bpc").chips
    c.deleteLater()
