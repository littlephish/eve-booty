"""The shared card shell, and the holds card built on it.

The shell's commit model is what every filter card relies on and what a
popover makes easy to get wrong: Qt closes a Qt.Popup on any outside click,
so the only path that may write chips is Done and every other way out has to
read as Cancel. It is pinned once here rather than three times over.

The holds card itself is pinned on the three things that make it different
from the abyssal picker it borrows its shape from: the edit carries the exact
type NAME (free text included, since the interesting ships are often the ones
holding none of it), the picker's list is the selected bay's and a bay click
asks the view for a fresh one, and the chip it writes is one the grammar
parses back into the very term the widgets describe. The list's order is the
query's -- held types first, the rest of the SDE after -- and the completer
has to reach every entry of it, or the SDE half would be decoration.
"""

from __future__ import annotations

import pytest

from evasset import omni

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from conftest import match_text  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from evasset.ui import holds_card as hc  # noqa: E402
from evasset.ui.filter_card import FilterCard  # noqa: E402

PASTE = "Nanite Repair Paste"
AMMO = "Antimatter Charge M"

# The shape queries.held_type_counts returns: the types sitting inside the
# estate's assembled ships, busiest first -- and the fuel bay's list, which
# carries fuels the estate holds none of.
HELD = [
    {"type_id": 222, "name": AMMO, "units": 12400, "ships": 6, "owned": 1},
    {"type_id": 28668, "name": PASTE, "units": 940, "ships": 4, "owned": 1},
]
FUEL = "Helium Fuel Block"
FUELS = [
    {"type_id": 4247, "name": FUEL, "units": 0, "ships": 0, "owned": 0},
    {"type_id": 28668, "name": PASTE, "units": 12, "ships": 1, "owned": 1},
]
# A drone bay's list as the query orders it: what the ships hold, busiest
# first, then every other published drone in the SDE by name.
DRONES = [
    {"type_id": 2456, "name": "Hobgoblin II", "units": 40, "ships": 8, "owned": 1},
    {"type_id": 2185, "name": "Warrior II", "units": 5, "ships": 1, "owned": 1},
    {"type_id": 2454, "name": "Hornet II", "units": 0, "ships": 0, "owned": 0},
    {"type_id": 2488, "name": "Ogre II", "units": 0, "ships": 0, "owned": 0},
]


@pytest.fixture
def app():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def card(app):
    c = hc.HoldsCard()
    c.set_types(HELD)
    yield c
    c.hide()
    c.deleteLater()


def record(signal) -> list:
    calls: list = []
    signal.connect(lambda *args: calls.append(args))
    return calls


def type_name(card, text: str) -> None:
    """Replace the edit's text the way a user does -- select, delete, type --
    so textEdited fires on every step and the card announces. setText and
    clear() are silent, which is exactly the case this must not test."""
    card.type_edit.selectAll()
    QTest.keyClick(card.type_edit, Qt.Key_Backspace)
    QTest.keyClicks(card.type_edit, text)


# --------------------------------------------------------------- the shell
class _ShellCard(FilterCard):
    """The smallest possible card: enough to exercise the shell alone."""

    KINDS = ("owner",)
    TITLE = "Shell"

    def chips(self) -> list:
        return [omni.Chip("owner", "Someone")]


@pytest.fixture
def shell(app):
    c = _ShellCard()
    yield c
    c.hide()
    c.deleteLater()


def test_done_is_the_only_way_out_that_writes_chips(shell):
    """Cancel, Escape and the outside click Qt turns into a hide must all
    read as Cancel: an abandoned edit that rewrote the filter would make
    every look at a card a change to it."""
    done, cancelled = record(shell.done), record(shell.cancelled)

    shell.show()
    shell.done_btn.click()
    assert done == [([omni.Chip("owner", "Someone")],)]
    assert cancelled == [] and not shell.isVisible()

    shell.show()
    shell.cancel_btn.click()
    assert (len(done), len(cancelled)) == (1, 1)

    shell.show()
    QTest.keyClick(shell, Qt.Key_Escape)
    assert (len(done), len(cancelled)) == (1, 2)

    shell.show()
    shell.hide()  # what Qt does on an outside click
    assert (len(done), len(cancelled)) == (1, 3)


def test_enter_outside_an_edit_is_done_and_the_footer_shows_what_it_is_told(shell):
    done = record(shell.done)
    shell.show()
    shell.cancel_btn.setFocus()
    QTest.keyClick(shell.cancel_btn, Qt.Key_Return)
    assert len(done) == 1 and not shell.isVisible()

    shell._set_counting()
    assert match_text(shell) == "…"
    shell.set_match_count(3, 1234)
    assert match_text(shell) == "3 of 1,234 match"


def test_enter_does_nothing_while_done_is_disabled(shell):
    """A card that cannot write a chip must not apply an empty list on
    Enter: on the holds and fit cards that is the state with nothing named,
    and Done there would silently delete the chip the card was opened on."""
    done, cancelled = record(shell.done), record(shell.cancelled)
    shell.done_btn.setEnabled(False)
    shell.show()
    shell.cancel_btn.setFocus()
    QTest.keyClick(shell.cancel_btn, Qt.Key_Return)
    assert done == [] and cancelled == [] and shell.isVisible()

    shell.done_btn.setEnabled(True)
    QTest.keyClick(shell.cancel_btn, Qt.Key_Return)
    assert len(done) == 1


def test_a_card_that_shows_no_count_drops_the_sentence_and_keeps_the_buttons(app, shell):
    """The save and load cards act on the view library rather than on the
    filter, so their footer has nothing to count -- but it must keep the
    height and the buttons the shell gives every card, or the two command
    cards would read as a different widget. A count that arrives anyway (one
    already in flight when the card opened) must stay off the screen."""

    class _SilentCard(_ShellCard):
        SHOWS_COUNT = False

    silent = _SilentCard()
    silent.show()
    try:
        assert not silent.match_count_label.isVisible()
        assert not silent.match_rest_label.isVisible()
        assert silent.cancel_btn.isVisible() and silent.done_btn.isVisible()
        assert silent.footer.sizeHint().height() == shell.footer.sizeHint().height()

        silent.set_match_count(3, 1234)
        silent._set_counting()
        assert (silent.match_count_label.text(), silent.match_rest_label.text()) == ("…", "")
    finally:
        silent.hide()
        silent.deleteLater()


def test_the_shell_owns_positive_chips_of_its_kinds_only(shell):
    """Done replaces what the card can express and nothing else, so a
    negated chip of the same kind survives it by default."""
    assert shell.owns(omni.Chip("owner", "Someone"))
    assert not shell.owns(omni.Chip("owner", "Someone", negated=True))
    assert not shell.owns(omni.Chip("category", "Ship"))


# ----------------------------------------------------------- the holds card
def picker(card) -> list[str]:
    return [card.type_combo.itemText(i) for i in range(card.type_combo.count())]


def test_the_picker_lists_bare_names_with_the_count_on_the_tooltip(card):
    """An entry is the name and nothing else: the edit holds the value the
    chip carries, and the counted label the first build showed ("· 12,400
    in 6 ships") was a suffix every pick had to strip and every completion
    had to match around. The count still helps choose, so it rides on the
    tooltip; a fuel nobody holds says so there rather than reading "0 in 0
    ships"."""
    assert picker(card) == [AMMO, PASTE]
    assert card.type_combo.itemData(0, Qt.ToolTipRole) == "12,400 in 6 ships"
    card.type_combo.setCurrentIndex(1)
    assert card.type_edit.text() == PASTE
    assert card.type_name() == PASTE

    card.set_types(FUELS)
    assert picker(card) == [FUEL, PASTE]
    assert card.type_combo.itemData(0, Qt.ToolTipRole) == "None aboard the ships in scope"
    assert card.type_combo.itemData(1, Qt.ToolTipRole) == "12 in 1 ships"


def test_the_bay_selector_is_all_then_the_five_bays_in_the_grammars_order(card):
    """One button per bay of fitting.HOLD_BAYS, in its order, after All --
    the selector is built from the dict so a bay the grammar gains cannot
    be missing from the card, and each click announces the bay's key, which
    is what the view hands queries.held_type_counts."""
    assert list(card.bay_buttons) == [None, "cargo", "fuel", "drones", "fighters", "fleet"]
    assert [b.text() for b in card.bay_buttons.values()] == [
        "All", "Cargo", "Fuel", "Drones", "Fighters", "Fleet",
    ]
    assert card.bay_buttons[None].isChecked() and card.bay() is None
    assert all(b.toolTip() for b in card.bay_buttons.values())

    bays = record(card.bay_changed)
    seen = 0
    for key in ("fighters", "fleet", "drones"):
        card.bay_buttons[key].click()
        assert card.bay() == key
        seen += 1
    assert bays == [("fighters",), ("fleet",), ("drones",)] and seen == 3


def completions(card, prefix: str) -> list[str]:
    """What the completer offers for a prefix, in its own order."""
    completer = card._completer
    completer.setCompletionPrefix(prefix)
    return [
        completer.currentCompletion()
        for row in range(completer.completionCount())
        if completer.setCurrentRow(row)
    ]


def test_the_picker_keeps_the_held_first_order_and_completes_every_entry(card):
    """The query puts the drones the ships fly ahead of the SDE's hundreds,
    and the card must show them in that order rather than re-sort by name
    -- but the unowned half is not decoration: any of its entries must come
    up in the completer, by any part of its name, or the promise that every
    drone is searchable would only hold for the ones already owned."""
    card.set_types(DRONES)
    assert picker(card) == ["Hobgoblin II", "Warrior II", "Hornet II", "Ogre II"]
    assert card.type_combo.itemData(0, Qt.ToolTipRole) == "40 in 8 ships"
    assert card.type_combo.itemData(2, Qt.ToolTipRole) == "None aboard the ships in scope"

    seen = 0
    for row in DRONES:
        name = row["name"]
        assert completions(card, name.lower()) == [name], name
        # A fragment from the middle of the name reaches it too.
        assert name in completions(card, name[3:7].lower()), name
        seen += 1
    assert seen == len(DRONES) == 4
    assert completions(card, " ii") == ["Hobgoblin II", "Warrior II", "Hornet II", "Ogre II"]

    # Picking an unowned entry writes its name like any other.
    card.type_combo.setCurrentIndex(3)
    assert card.type_name() == "Ogre II"
    card.bay_buttons["drones"].click()
    assert card.chips() == [omni.Chip(hc.HOLDS_KIND, "drones/Ogre II>=1")]


def test_a_bay_click_announces_the_bay_and_the_typed_name_survives_the_new_list(card):
    """The list is the bay's, so the view must hear every click to refetch
    it -- but seeding is silent, since the view fetches on open anyway. A
    name already in the edit is the user's, whichever list arrives: it is
    re-selected when the new list offers it and stands as free text when it
    does not, so switching to Fuel never blanks a half-built chip."""
    bays = record(card.bay_changed)
    card.bay_buttons["fuel"].click()
    card.bay_buttons["cargo"].click()
    card.bay_buttons[None].click()
    assert bays == [("fuel",), ("cargo",), (None,)]

    card.seed([omni.Chip(hc.HOLDS_KIND, f"drones/{PASTE}>=1")])
    assert card.bay() == "drones" and len(bays) == 3, "seeding is not a click"

    type_name(card, PASTE)
    card.set_types(FUELS)
    assert card.type_name() == PASTE and card.type_combo.currentIndex() == 1
    card.set_types(HELD)
    assert card.type_name() == PASTE and card.type_combo.currentIndex() == 1

    type_name(card, "Republic Fleet EMP M")
    card.set_types(FUELS)
    assert card.type_name() == "Republic Fleet EMP M"
    assert card.type_combo.currentIndex() == -1, "free text selects no entry"
    assert card.chips() == [omni.Chip(hc.HOLDS_KIND, "drones/Republic Fleet EMP M>=1")]


def test_free_text_is_a_valid_name_and_an_empty_one_disables_done(card):
    """Any exact SDE name is a fair question -- the ships short of it are
    often the ones holding none at all, which no picker built from the
    estate's contents can offer. An empty name is the one state Done cannot
    write a chip from."""
    assert not card.done_btn.isEnabled()
    assert card.term() is None and card.chips() == []

    type_name(card, "Republic Fleet EMP M")
    assert card.done_btn.isEnabled()
    assert card.term().name == "Republic Fleet EMP M"

    type_name(card, "")
    assert not card.done_btn.isEnabled() and card.chips() == []
    # And Enter cannot get past that either: applying [] would delete the
    # holds chip the card was opened on.
    done = record(card.done)
    card.show()
    card.op_combo.setFocus()
    QTest.keyClick(card.op_combo, Qt.Key_Return)
    assert done == [] and card.isVisible()


def test_the_bay_operator_and_quantity_widgets_drive_the_chip(card):
    """What the card writes is exactly what the widgets say, in the grammar
    the chip parses back: the bay as a prefix, the operator verbatim, and
    the range as `=lo..hi`."""
    type_name(card, AMMO)
    card.bay_buttons["cargo"].click()
    card.op_combo.setCurrentIndex(card.op_combo.findData("<"))
    card.low_spin.setValue(500)
    assert card.bay() == "cargo"
    assert card.chips() == [omni.Chip(hc.HOLDS_KIND, f"cargo/{AMMO}<500")]

    card.bay_buttons[None].click()
    card.op_combo.setCurrentIndex(card.op_combo.findData(".."))
    card.low_spin.setValue(100)
    card.high_spin.setValue(400)
    assert card.chips() == [omni.Chip(hc.HOLDS_KIND, f"{AMMO}=100..400")]
    # And the high field belongs to the range form alone.
    card.op_combo.setCurrentIndex(card.op_combo.findData(">="))
    assert card.high_spin.isHidden() and card.range_label.isHidden()
    assert card.chips() == [omni.Chip(hc.HOLDS_KIND, f"{AMMO}>=100")]


def test_a_decimal_threshold_survives_the_seed_and_the_fields_show_whole_counts_bare(card):
    """The grammar takes any number, so a typed `<1.5` must come back out of
    Done as `<1.5`: seeded into an integer spin it once became `<1`, a
    filter the user never wrote. The fields keep their decimals for that
    and show none they do not need -- "500", never "500.00" -- since a hold
    count is whole in practice and the ".00" would read as a mistake."""
    card.seed([omni.Chip(hc.HOLDS_KIND, f"{PASTE}<1.5")])
    assert card.low_spin.value() == 1.5
    assert card.low_spin.text() == "1.5"
    assert card.chips() == [omni.Chip(hc.HOLDS_KIND, f"{PASTE}<1.5")]

    card.low_spin.setValue(500)
    assert card.low_spin.text() == "500"
    card.low_spin.setValue(12400)
    assert card.low_spin.text() == "12,400", "the group separator stays"
    assert card.chips() == [omni.Chip(hc.HOLDS_KIND, f"{PASTE}<12400")]


def test_a_range_can_never_be_written_the_wrong_way_round(card):
    """parse_stat rejects a reversed range outright, so a chip written from
    one would degrade to bare text and lose the filter without saying so."""
    type_name(card, PASTE)
    card.op_combo.setCurrentIndex(card.op_combo.findData(".."))
    card.high_spin.setValue(50)
    card.low_spin.setValue(200)
    assert card.high_spin.value() >= card.low_spin.value()
    term = card.term()
    assert term.low <= term.high
    # And the value it writes parses back to the very term the widgets hold.
    assert hc.parse_holds(hc.holds_value(term)) == term


@pytest.mark.parametrize(
    "value,bay,name,op,low,high",
    [
        (f"{AMMO}<500", None, AMMO, "<", 500, None),
        (f"cargo/{AMMO}>=100", "cargo", AMMO, ">=", 100, None),
        (f"fuel/{PASTE}<=9", "fuel", PASTE, "<=", 9, None),
        (f"drones/{PASTE}=10..20", "drones", PASTE, "..", 10, 20),
        ("fighters/Templar II>1", "fighters", "Templar II", ">", 1, None),
        (f"fleet/{AMMO}>=4000", "fleet", AMMO, ">=", 4000, None),
        (f"{PASTE}<1.5", None, PASTE, "<", 1.5, None),
        (f"cargo/{AMMO}=0.25..2.5", "cargo", AMMO, "..", 0.25, 2.5),
    ],
)
def test_seeding_a_chip_in_every_form_comes_back_out_unchanged(card, value, bay, name, op,
                                                               low, high):
    """A card opened and applied untouched must hand back the chip it was
    given, in every bay and every operator: a card that rewrote the filter
    on a Done with nothing touched would be a filter that drifts every time
    it is looked at."""
    card.seed([omni.Chip(hc.HOLDS_KIND, value)])
    term = card.term()
    assert (term.bay, term.name, term.op, term.low, term.high) == (
        bay, name, op, float(low), None if high is None else float(high)
    )
    assert card.bay() == bay
    assert card.chips() == [omni.Chip(hc.HOLDS_KIND, value)]


def test_seeding_ignores_negated_and_malformed_chips_and_opens_blank(card):
    """A negated holds chip is the complement question, which the card has
    no polarity control for; it rides through Done rather than being
    swallowed, so it must not seed the card either."""
    card.seed([
        omni.Chip(hc.HOLDS_KIND, f"{AMMO}<500", negated=True),
        omni.Chip(hc.HOLDS_KIND, "no operator here"),
        omni.Chip("category", "Ship"),
    ])
    assert card.type_name() == "" and card.chips() == []
    assert not card.owns(omni.Chip(hc.HOLDS_KIND, f"{AMMO}<500", negated=True))
    # A card that seeded from nothing owns nothing: two holds chips AND, so
    # Done from a blank card adds beside them rather than replacing one.
    assert not card.owns(omni.Chip(hc.HOLDS_KIND, f"{AMMO}<500"))
    card.seed([omni.Chip(hc.HOLDS_KIND, f"{AMMO}<500"), omni.Chip(hc.HOLDS_KIND, f"cargo/{AMMO}<9")])
    assert card.owns(omni.Chip(hc.HOLDS_KIND, f"{AMMO}<500")), "the seeded term is the card's"
    assert card.owns(omni.Chip(hc.HOLDS_KIND, f"{AMMO}>=1")), "same type and bay, other threshold"
    assert not card.owns(omni.Chip(hc.HOLDS_KIND, f"cargo/{AMMO}<9")), "another bay stands"


def test_reseeding_starts_over_from_a_previous_pick(card):
    card.seed([omni.Chip(hc.HOLDS_KIND, f"cargo/{AMMO}=10..20")])
    card.seed([])
    assert card.type_name() == ""
    assert card.bay() is None
    assert card.op_combo.currentData() == hc.DEFAULT_OP
    assert card.low_spin.value() == hc.DEFAULT_LOW


def test_enter_in_the_type_edit_commits_the_text_and_is_never_done(card):
    """Enter here answers the question the edit asked. Left to travel it
    would climb to the card, where Enter is Done, and apply whatever
    half-typed name was standing."""
    done = record(card.done)
    card.show()
    type_name(card, "antimatter charge m")
    QTest.keyClick(card.type_edit, Qt.Key_Return)
    assert done == [] and card.isVisible()
    # The entry it names is selected, and the edit now reads as the SDE
    # spells it rather than as it was typed.
    assert card.type_edit.text() == AMMO
    assert card.type_combo.currentIndex() == 0


def test_a_change_asks_for_a_fresh_count_and_the_footer_counts_ships(card):
    """The card cannot count, so it announces and shows "…" until the view
    answers -- and what it counts is assembled ships, since both polarities
    of the chip are ship-scoped."""
    changes = record(card.filter_changed)
    type_name(card, AMMO)
    assert changes and card.match_count_label.text() == "…"
    card.set_match_count(4, 11)
    assert match_text(card) == "4 of 11 match"

    before = len(changes)
    card.bay_buttons["fuel"].click()
    card.low_spin.setValue(7)
    assert len(changes) > before
    assert card.totals() == ("ships", None)


def test_the_card_is_the_shared_width_and_opens_on_the_type_edit(card):
    card.show()
    assert card.width() == hc.FilterCard.WIDTH
    assert card.first_focus() is card.type_edit
    image = card.grab().toImage()
    assert not image.isNull() and image.width() == hc.FilterCard.WIDTH


def test_the_column_key_and_header_name_the_type_and_the_bay():
    """Two chips on the same type and bay must share one column, so the key
    is case-folded; the header is what the user sees over the counts."""
    whole = omni.parse_holds(f"{AMMO}<500")
    cargo = omni.parse_holds(f"cargo/{AMMO}<500")
    shouted = omni.parse_holds(f"CARGO/{AMMO.upper()}>=1")
    assert omni.holds_column_key(whole) != omni.holds_column_key(cargo)
    assert omni.holds_column_key(cargo) == omni.holds_column_key(shouted)
    assert omni.holds_column_header(whole) == AMMO
    assert omni.holds_column_header(cargo) == f"{AMMO} · cargo"


def test_the_view_gate_lists_positive_holds_terms_once_each():
    """The columns follow the positive chips in the order they were typed,
    deduplicated by column key -- two chips bounding the same count from
    both ends are one column, not two identical ones."""
    spec = omni.parse(
        f'holds:"{AMMO}<500" holds:"{AMMO}>=100" holds:"cargo/{PASTE}<10"'
        f' -holds:"{PASTE}>=1" cat:Ship'
    )
    terms = omni.single_holds_terms(spec)
    assert [omni.holds_column_key(t) for t in terms] == [
        f"holds:all/{AMMO.casefold()}", f"holds:cargo/{PASTE.casefold()}"
    ]
