"""The Load card: the library list, the preview pane, its editing, the paste
box, and the keyboard the card owns.

Three of the behaviours pinned here are the ones a list inside a popover
gets wrong by default. Enter has three meanings on this card depending on
where the keyboard is (rename, add an import, load the selection) and only
the last may reach the shell; Esc during a rename or with the key menu open
must close that and not the card; and the list must never take focus,
because QAbstractItemView makes row 0 current on focus-in and Load view here
throws the working filter away.
"""

from __future__ import annotations

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from evasset import views  # noqa: E402
from evasset.ui import palette  # noqa: E402
from evasset.ui.load_view_card import (  # noqa: E402
    EMPTY_TEXT,
    IDENTICAL_TEXT,
    NAME_PLACEHOLDER,
    NO_SLOT,
    LoadViewCard,
)

JITA = 'owner:Main cat:Ship loc:"Jita IV - Moon 4"'


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
    make_view(1, "Jita ships", 1, JITA),
    make_view(7, "Short of paste", None, 'holds:"Nanite Repair Paste"<500'),
    make_view(9, "Everything", None, ""),
]


@pytest.fixture
def app():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def card(app):
    c = LoadViewCard()
    c.seed([])
    c.set_current(views.ViewState("owner:Main", "", ""))
    c.set_views(LIBRARY)
    yield c
    c.hide()
    c.deleteLater()


def record(signal) -> list:
    calls: list = []
    signal.connect(lambda *args: calls.append(args))
    return calls


def row_at(card, index: int):
    return card.view_list.itemWidget(card.view_list.item(index))


def select(card, index: int) -> None:
    card.view_list.setCurrentItem(card.view_list.item(index))


def diff_of(card) -> list[tuple[str, str]]:
    return [
        (p.sign_label.text(), p.prefix_label.text() + p.value_label.text())
        for p in card.diff_pills()
    ]


def test_every_saved_view_renders_as_a_key_cell_and_a_name(card):
    """The row carries only what tells two views apart at a glance; the
    filter itself belongs to the preview, where there is room for it."""
    assert card.view_list.count() == 3
    assert [row_at(card, i).name_label.text() for i in range(3)] == [
        "Jita ships", "Short of paste", "Everything",
    ]
    assert [row_at(card, i).key_cell.label() for i in range(3)] == ["1", NO_SLOT, NO_SLOT]
    assert card.done_btn.text() == "Load view"


def test_the_preview_names_the_posture_a_load_would_set(card):
    """A load moves the group-by, the rail level and the rail sort as well
    as the filter, and the chips show only the filter: the caption says the
    rest, in the rail's own words, and omits whatever the view never
    recorded (an import names nothing; a pre-v8 view names no sort)."""
    posture = make_view(11, "Grouped", None, "cat:Ship")
    posture.state = views.ViewState("cat:Ship", "location", "owner", "value")
    partial = make_view(12, "Old", None, "cat:Ship")
    partial.state = views.ViewState("cat:Ship", "", "region", "")
    card.set_views([*LIBRARY, posture, partial])

    select(card, 3)
    assert not card.posture_label.isHidden()
    assert card.posture_label.text() == "Group by Location · Rail Owner · ISK"
    select(card, 4)
    assert card.posture_label.text() == "Rail Region"
    select(card, 0)
    assert card.posture_label.isHidden()


def test_nothing_selected_shows_the_empty_state_and_arms_nothing(card):
    """Opening the card to see what is saved must not be one keystroke away
    from replacing the filter with whatever sorts first."""
    assert card.selected_view() is None
    assert card.pane.currentIndex() == 0
    assert card.empty_label.text() == EMPTY_TEXT
    assert not card.done_btn.isEnabled()
    assert card.diff() == []


def test_selecting_a_row_previews_its_chips_its_key_and_the_diff_against_the_filter(card):
    """A load is not additive, so the pane shows the cost first: the chips
    the current filter loses, then the ones it keeps, then what the view
    brings -- each under the wash its sign earns, so the colour says what
    the glyph says."""
    select(card, 0)
    assert card.pane.currentIndex() == 1
    assert card.name_label.text() == "Jita ships"
    assert card.keycap.text() == "1"
    assert "solid" in card.keycap.styleSheet()
    assert [p.prefix_label.text() + p.value_label.text() for p in card.preview_pills()] == [
        "owner:Main", "cat:Ship", "loc:Jita IV - Moon 4",
    ]
    assert diff_of(card) == [("=", "owner:Main"), ("+", "cat:Ship"), ("+", "loc:Jita IV - Moon 4")]
    assert card.identical_label.isHidden()
    assert card.hint_label.text() == "Press Ctrl+1 to load"
    assert card.done_btn.isEnabled()
    assert row_at(card, 0).selected and not row_at(card, 1).selected

    assert card.posture_label.isHidden(), "a view that records no posture says nothing"

    select(card, 1)
    assert card.keycap.text() == NO_SLOT
    assert "dashed" in card.keycap.styleSheet()
    assert diff_of(card) == [("-", "owner:Main"), ("+", "holds:Nanite Repair Paste<500")]
    assert card.hint_label.text() == "Assign a key for one-press access, or use Load view"
    assert row_at(card, 1).selected and not row_at(card, 0).selected

    select(card, 2)
    assert card.preview_pills() == []
    assert diff_of(card) == [("-", "owner:Main")]


def test_the_diff_signs_wear_the_roll_meters_red_and_green(card, app):
    """The washes are the roll meters' verdict pair, measured against
    default text in test_contrast.py, so the diff needs no colour of its
    own to pass the same checks."""
    select(card, 1)
    pal = app.palette()
    dropped, added = card.diff_pills()
    assert palette.quality_tint(0.0, pal).lower() in dropped.styleSheet().lower()
    assert palette.quality_tint(1.0, pal).lower() in added.styleSheet().lower()


def test_a_view_equal_to_the_current_filter_says_so_instead_of_a_diff(card):
    """All "=" is not a diff worth chips; the sentence is quicker to read."""
    card.set_current(views.ViewState(JITA, "", ""))
    select(card, 0)
    assert diff_of(card) == []
    assert not card.identical_label.isHidden()
    assert card.identical_label.text() == IDENTICAL_TEXT
    assert not card.diff_area.isHidden(), "the bottom half keeps its height"


def test_the_key_menu_offers_every_digit_and_marks_the_one_in_force(card):
    """A slot is the only part of a view the digit keys can reach, so the
    row has to say which digit it answers to -- and reassigning takes the
    slot off whatever view held it, which is the store's job, not the
    card's: the card only asks, and picking the digit already held asks
    nothing."""
    asked = record(card.slot_requested)
    cell = row_at(card, 1).key_cell
    cell.open_menu()
    menu = cell.menu
    assert [b.text() for b in menu.buttons.values()] == [NO_SLOT, *[str(n) for n in range(1, 10)]]
    assert menu.buttons[1].toolTip() == "Take from “Jita ships”"
    assert menu.buttons[None].toolTip() == "" and menu.buttons[4].toolTip() == ""

    menu.buttons[4].click()
    assert asked == [(7, 4)]
    assert not menu.isVisible()

    row_at(card, 0).key_cell.pick(None)
    assert asked[-1] == (1, None)
    row_at(card, 0).key_cell.pick(1)
    assert len(asked) == 2, "the digit it already holds is not a request"


def test_clicking_a_row_selects_it_and_never_loads(card):
    """Click selects, only Load view or a digit loads: the pane exists to
    show what a load would do before it is done."""
    loaded = record(card.load_requested)
    card.show()
    QTest.mouseClick(row_at(card, 1), Qt.LeftButton)
    assert card.selected_view().view_id == 7
    assert loaded == []
    assert card.isVisible()


def test_delete_in_the_pane_asks_for_the_view_to_be_forgotten(card):
    """Deletable where it is shown, the fit card's rule: the alternative is
    a settings page for something the list already displays. It is
    immediate -- the view that owns the connection re-lists, and the
    selection then lands on nothing."""
    deleted = record(card.delete_requested)
    select(card, 1)
    card.delete_btn.click()
    assert deleted == [(7,)]

    card.set_views([v for v in LIBRARY if v.view_id != 7])
    assert card.selected_view() is None
    assert card.pane.currentIndex() == 0
    assert not card.done_btn.isEnabled()


def test_rename_swaps_the_name_for_an_edit_and_enter_commits_without_loading(card):
    """Enter in the rename field must stop there. Left to travel it would
    climb to the card, where Enter is Load view, and load the very row
    being renamed -- discarding the filter the user was working in."""
    renamed, loaded, done = (
        record(card.rename_requested), record(card.load_requested), record(card.done)
    )
    select(card, 0)
    card.show()
    card.rename_btn.click()
    assert card.renaming and card.name_edit.text() == "Jita ships"
    assert card.name_label.isHidden() and not card.name_edit.isHidden()

    card.name_edit.selectAll()
    QTest.keyClicks(card.name_edit, "Jita hulls")
    QTest.keyClick(card.name_edit, Qt.Key_Return)

    assert renamed == [(1, "Jita hulls")]
    assert loaded == [] and done == []
    assert not card.renaming, "the label comes back; the re-list carries the new name"
    assert card.isVisible()


def test_leaving_the_rename_field_commits_it_and_escape_reverts_it(card, app):
    """Blur commits, the design's rule, because a click on Load view after
    typing a name means both; Esc laddering out of the popover would cost
    the user the whole card over a mistyped letter."""
    renamed = record(card.rename_requested)
    card.show()
    select(card, 1)
    card.begin_rename()
    QTest.keyClicks(card.name_edit, "!!")
    QTest.keyClick(card.name_edit, Qt.Key_Escape)
    assert renamed == []
    assert not card.renaming and card.name_label.text() == "Short of paste"
    assert card.isVisible()

    card.begin_rename()
    card.name_edit.setText("Paste watch")
    card.paste_edit.setFocus()
    app.processEvents()
    assert renamed == [(7, "Paste watch")]
    assert not card.renaming


def test_a_rename_to_nothing_or_to_the_same_name_asks_nothing(card):
    """The store would refuse a blank name anyway; asking for it would only
    put an error in the footer where the user meant to back out. The same
    name is not a rename either."""
    renamed = record(card.rename_requested)
    select(card, 0)
    card.begin_rename()
    card.name_edit.setText("   ")
    QTest.keyClick(card.name_edit, Qt.Key_Return)
    card.begin_rename()
    QTest.keyClick(card.name_edit, Qt.Key_Return)
    assert renamed == [] and not card.renaming


def test_a_double_click_on_a_row_selects_it_and_starts_a_rename(card):
    """The old affordance kept: a name is corrected where it is read."""
    select(card, 0)
    card.show()
    QTest.mouseDClick(row_at(card, 1), Qt.LeftButton)
    assert card.selected_view().view_id == 7
    assert card.renaming and card.name_edit.text() == "Short of paste"


def test_the_paste_box_suggests_a_name_as_the_placeholder_and_enter_adds_rather_than_loads(
    card,
):
    """The add block is the receiving end of Copy as text. The name is
    optional -- the placeholder shows what a blank one saves as, and the
    store suggests the same name from the same line -- and Enter there is
    Add, never Load view, for the rename field's reason."""
    imported, loaded = record(card.import_requested), record(card.load_requested)
    select(card, 0)
    assert not card.add_btn.isEnabled(), "nothing to import yet"
    assert card.import_name_edit.placeholderText() == NAME_PLACEHOLDER

    QTest.keyClicks(card.paste_edit, "cat:Ship -owner:Alt")
    assert card.import_name_edit.text() == ""
    assert card.import_name_edit.placeholderText() == "cat Ship · -owner Alt"
    assert card.add_btn.isEnabled()

    QTest.keyClick(card.paste_edit, Qt.Key_Return)
    assert imported == [("cat:Ship -owner:Alt", "")]
    assert loaded == [], "an import is not a load"


def test_a_typed_import_name_travels_with_the_line(card):
    QTest.keyClicks(card.paste_edit, "cat:Ship")
    QTest.keyClicks(card.import_name_edit, "Fleet hulls")
    imported = record(card.import_requested)
    QTest.keyClick(card.import_name_edit, Qt.Key_Return)
    assert imported == [("cat:Ship", "Fleet hulls")]


def test_add_stays_disabled_without_a_line(card):
    QTest.keyClicks(card.import_name_edit, "Named nothing")
    assert not card.add_btn.isEnabled()
    imported = record(card.import_requested)
    QTest.keyClick(card.import_name_edit, Qt.Key_Return)
    assert imported == [], "Enter cannot add what the button would not"


def test_load_view_loads_the_selected_view_and_never_emits_done(card):
    """The shell's done means "replace my kinds in the filter"; a view
    replaces the whole filter instead, which is the view's own set_spec."""
    loaded, done = record(card.load_requested), record(card.done)
    select(card, 1)
    card.show()
    card.done_btn.click()

    assert loaded == [(7,)]
    assert done == []
    assert not card.isVisible()


def test_enter_never_loads_even_with_a_view_picked(card):
    """Loading replaces the whole posture, and Enter also commits a rename
    and a paste import in this card; the user asked for it to do nothing
    here, so only a digit or the Load view button loads."""
    loaded = record(card.load_requested)
    card.show()
    QTest.keyClick(card, Qt.Key_Return)
    assert loaded == []
    assert card.isVisible()

    select(card, 2)
    QTest.keyClick(card, Qt.Key_Return)
    assert loaded == []
    assert card.isVisible()
    card.done_btn.click()
    assert loaded == [(9,)]


def test_the_arrows_move_the_selection_from_nothing_and_stop_at_the_ends(card):
    """The card, not the list, owns the arrows: the list takes no focus, so
    the first Down is the first choice rather than a row Qt picked."""
    card.show()
    QTest.keyClick(card, Qt.Key_Down)
    assert card.selected_view().view_id == 1
    QTest.keyClick(card, Qt.Key_Down)
    QTest.keyClick(card, Qt.Key_Down)
    QTest.keyClick(card, Qt.Key_Down)
    assert card.selected_view().view_id == 9, "clamped at the last row"
    QTest.keyClick(card, Qt.Key_Up)
    assert card.selected_view().view_id == 7

    card.seed([])
    QTest.keyClick(card, Qt.Key_Up)
    assert card.selected_view().view_id == 9, "Up from nothing lands on the last row"


def test_a_digit_selects_and_loads_the_view_on_that_key(card):
    """The one-press path the key exists for. A digit nobody holds does
    nothing rather than loading whatever happened to be selected."""
    loaded = record(card.load_requested)
    select(card, 1)
    card.show()
    QTest.keyClick(card, Qt.Key_5)
    assert loaded == [] and card.isVisible()
    assert card.selected_view().view_id == 7

    QTest.keyClick(card, Qt.Key_1)
    assert loaded == [(1,)]
    assert not card.isVisible()


def test_digits_typed_into_the_paste_box_are_text_not_keys(card):
    loaded = record(card.load_requested)
    card.show()
    card.paste_edit.setFocus()
    QTest.keyClick(card.paste_edit, Qt.Key_1)
    assert card.paste_edit.text() == "1"
    assert loaded == []


def test_escape_closes_an_open_key_menu_before_the_card(card, app):
    cancelled = record(card.cancelled)
    card.show()
    cell = row_at(card, 0).key_cell
    cell.open_menu()
    assert cell.menu.isVisible()
    QTest.keyClick(cell.menu, Qt.Key_Escape)
    assert not cell.menu.isVisible()
    assert card.isVisible() and cancelled == []
    QTest.keyClick(card, Qt.Key_Escape)
    assert not card.isVisible() and len(cancelled) == 1


def test_the_keyboard_lands_on_the_card_itself_and_never_on_the_list(card, app):
    """QAbstractItemView makes row 0 current when it gains focus, which
    would arm Load view for a user who picked nothing; the list takes no
    focus at all, and the card's own key handling covers what the list's
    would have."""
    assert card.first_focus() is card
    assert card.view_list.focusPolicy() == Qt.NoFocus
    # Drained first: the offscreen platform reports an earlier test's
    # destroyed window as the application deactivating, which closes every
    # popup -- including one shown a moment before the drain.
    app.processEvents()
    card.show()
    app.processEvents()
    assert card.isVisible() and card.hasFocus()
    assert card.selected_view() is None
    assert not card.done_btn.isEnabled()

    select(card, 0)
    card.hide()
    card.show()
    app.processEvents()
    assert card.hasFocus()
    QTest.keyClick(card, Qt.Key_Down)
    assert card.selected_view().view_id == 7


def test_a_re_list_keeps_the_selection_the_user_made(card):
    """Every edit re-lists, so a slot change or a rename must not move the
    selection out from under the Load view button."""
    select(card, 1)
    card.set_views(LIBRARY)
    assert card.selected_view().view_id == 7

    card.set_views(LIBRARY, 9)
    assert card.selected_view().view_id == 9

    # A view that is gone leaves nothing selected rather than sliding the
    # selection onto its neighbour.
    card.set_views([v for v in LIBRARY if v.view_id != 9])
    assert card.selected_view() is None
    assert not card.done_btn.isEnabled()


def test_a_re_list_carries_the_digits_other_views_hold_into_every_menu(card):
    """The muted digits and their tooltips come from the list itself, so a
    slot moved elsewhere has to reach every row's menu on the next list."""
    card.set_views([make_view(1, "Jita ships", None, JITA), make_view(7, "Short of paste", 2)])
    cell = row_at(card, 0).key_cell
    cell.open_menu()
    assert cell.menu.buttons[2].toolTip() == "Take from “Short of paste”"
    assert cell.menu.buttons[1].toolTip() == ""
    cell.menu.hide()


def test_seeding_clears_the_selection_the_paste_row_and_any_rename(card):
    """One card instance serves the tab's whole life: a fresh open must
    start from nothing chosen and nothing half-typed."""
    select(card, 0)
    QTest.keyClicks(card.paste_edit, "cat:Ship")
    card.begin_rename()

    card.seed([])

    assert card.selected_view() is None
    assert card.paste_edit.text() == "" and card.import_name_edit.text() == ""
    assert card.import_name_edit.placeholderText() == NAME_PLACEHOLDER
    assert not card.add_btn.isEnabled()
    assert not card.renaming
    assert not card.done_btn.isEnabled()


def test_a_long_name_elides_rather_than_widening_the_list(card):
    """The list column has one width; a name has no upper bound."""
    long_name = "A view whose name goes on for far longer than the column can show"
    card.set_views([make_view(1, long_name, None, JITA)])
    row = row_at(card, 0)
    assert row.name_label.text() == long_name, "the full name survives for the tests and tooltips"
    assert row.name_label.minimumSizeHint().width() <= 24
    assert card.width() == 640


def test_the_card_asks_the_footer_nothing_and_writes_no_chips(card):
    assert card.chips() == []
    assert card.totals() == ("none", None)
    assert not card.SHOWS_COUNT
    assert not card.owns(object())


def relisted(library: list, *changed) -> list:
    """The library as the store would list it after an edit, `changed` being
    the views as they now stand: the slotted views first in digit order,
    then the rest by name -- list_views' rule."""
    replacements = {v.view_id: v for v in changed}
    updated = [replacements.get(v.view_id, v) for v in library]
    return sorted(updated, key=lambda v: (v.slot is None, v.slot or 0, v.name.casefold()))


def test_editing_a_selected_row_leaves_the_card_up(card, app):
    """A rename, a key pick and a delete each end in a re-list, and with a
    row selected the re-list also rebuilds the pane's pills. Pills torn out
    with setParent(None) become top-level widgets for a turn, the focus
    round trip that closes every popup (the omnibox chip incident); the
    card must still be up once the event loop has had its pass."""
    app.processEvents()
    card.show()
    app.processEvents()
    select(card, 0)

    card.begin_rename()
    card.name_edit.setText("Jita hulls")
    QTest.keyClick(card.name_edit, Qt.Key_Return)
    jita_hulls = make_view(1, "Jita hulls", 1, JITA)
    card.set_views(relisted(LIBRARY, jita_hulls))
    app.processEvents()
    assert card.isVisible(), "a rename's re-list closed the card"
    assert card.name_label.text() == "Jita hulls"

    row_at(card, 1).key_cell.open_menu()
    card.key_menu.buttons[4].click()
    paste_slotted = make_view(7, "Short of paste", 4, LIBRARY[1].state.filter)
    card.set_views(relisted(LIBRARY, jita_hulls, paste_slotted))
    app.processEvents()
    assert card.isVisible(), "a key pick's re-list closed the card"
    assert card.selected_view().view_id == 1

    card.delete_btn.click()
    card.set_views([paste_slotted, LIBRARY[2]])
    app.processEvents()
    assert card.isVisible(), "a delete's re-list closed the card"
    assert card.selected_view() is None and card.pane.currentIndex() == 0


def test_a_re_list_of_the_same_views_refreshes_the_rows_in_place(card):
    """Every row used to be rebuilt on every edit, menus and all, which cost
    a 200-view library as much per rename as opening the card did. The same
    set of views is refreshed in the widgets already there -- reordered as
    the store now lists them -- and only a changed set rebuilds."""
    before = card.rows()
    assert len(before) == 3, "nothing to reuse otherwise"

    renamed = make_view(7, "Almost no paste", None, LIBRARY[1].state.filter)
    card.set_views(relisted(LIBRARY, renamed))
    after = card.rows()
    assert [a is b for a, b in zip(before, after, strict=True)] == [True, True, True]
    assert [r.name_label.text() for r in after] == ["Jita ships", "Almost no paste", "Everything"]

    slotted = make_view(9, "Everything", 2, "")
    card.set_views(relisted(LIBRARY, renamed, slotted))
    after = card.rows()
    assert [a is b for a, b in zip(before, after, strict=True)] == [True, True, True]
    assert [(r.name_label.text(), r.key_cell.label()) for r in after] == [
        ("Jita ships", "1"), ("Everything", "2"), ("Almost no paste", NO_SLOT),
    ]
    assert [r.view_id for r in after] == [1, 9, 7], "each row now answers for the view it shows"
    row_at(card, 1).key_cell.open_menu()
    assert card.key_menu.buttons[1].toolTip() == "Take from “Jita ships”", (
        "the taken digits reached the reused menu state"
    )
    card.key_menu.hide()

    card.set_views([v for v in relisted(LIBRARY, renamed, slotted) if v.view_id != 7])
    assert len(card.rows()) == 2
    assert not any(a is b for a in before for b in card.rows()), "a changed set rebuilds"


def test_one_key_menu_serves_every_row_and_answers_to_the_cell_that_opened_it(card):
    """A menu per row was eleven widgets and a popup window per view, built
    for rows nobody would ever click; one menu is enough as long as the pick
    reaches the row that asked and the store's rules still hold there."""
    asked = record(card.slot_requested)
    assert {row.key_cell.menu for row in card.rows()} == {card.key_menu}

    row_at(card, 1).key_cell.open_menu()
    assert card.key_menu.asker is row_at(card, 1).key_cell
    card.key_menu.buttons[4].click()
    assert asked == [(7, 4)]

    row_at(card, 0).key_cell.open_menu()
    card.key_menu.buttons[1].click()
    assert asked == [(7, 4)], "the digit the row already holds is not a request"
    row_at(card, 0).key_cell.open_menu()
    card.key_menu.buttons[None].click()
    assert asked == [(7, 4), (1, None)]


def test_ctrl_digit_inside_the_card_loads_through_the_card_and_closes_it(card, app):
    """The tab binds Ctrl+digit with a WidgetWithChildrenShortcut context,
    and Qt's context check walks up through a Qt.Popup to its parent: with
    the card up, the tab's binding won the key, so the view loaded while the
    card stayed open over it with a diff against a filter that was gone.
    The card claims the digits in ShortcutOverride, so the press reaches its
    own handler -- from the card itself and from its edits alike -- while a
    bare digit typed into the paste box is still text."""
    from PySide6.QtGui import QKeySequence, QShortcut

    host = QtWidgets.QWidget()
    fired = []
    shortcut = QShortcut(QKeySequence("Ctrl+1"), host)
    shortcut.setContext(Qt.WidgetWithChildrenShortcut)
    shortcut.activated.connect(lambda: fired.append("tab"))
    card.setParent(host, Qt.Popup)
    loaded = record(card.load_requested)
    host.show()
    app.processEvents()
    card.show()
    app.processEvents()
    assert card.hasFocus()

    QTest.keyClick(card, Qt.Key_1, Qt.ControlModifier)
    assert loaded == [(1,)]
    assert fired == [], "the tab's binding must not see the key"
    assert not card.isVisible(), "the card's own handler selected, loaded and closed"

    card.show()
    app.processEvents()
    card.paste_edit.setFocus()
    QTest.keyClick(card.paste_edit, Qt.Key_1)
    assert card.paste_edit.text() == "1" and loaded == [(1,)]
    QTest.keyClick(card.paste_edit, Qt.Key_1, Qt.ControlModifier)
    assert loaded == [(1,), (1,)] and fired == []
    assert not card.isVisible()
    host.deleteLater()


def test_selecting_another_row_commits_a_rename_to_the_row_it_began_on(card, app):
    """Clicking another row mid-rename used to drop the typed name on the
    floor, although leaving the field is documented as a commit. The click
    commits -- and to the row the rename began on, since by the time the
    commit runs the selection is already the row that was clicked."""
    renamed = record(card.rename_requested)
    card.show()
    app.processEvents()
    select(card, 0)
    card.begin_rename()
    card.name_edit.setText("Jita hulls")

    QTest.mouseClick(row_at(card, 1), Qt.LeftButton)

    assert renamed == [(1, "Jita hulls")]
    assert not card.renaming
    assert card.selected_view().view_id == 7
    assert card.isVisible()


def test_the_key_cell_and_load_view_tooltips_say_what_the_keys_now_do(card):
    """The cell once described bare-digit recall, which the tab retired for
    Ctrl+digit, and Done's stock tooltip promises Enter, which this card
    deliberately swallows."""
    tip = row_at(card, 0).key_cell.toolTip()
    assert "Ctrl+" in tip
    assert "Enter" not in card.done_btn.toolTip()
