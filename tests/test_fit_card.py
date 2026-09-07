"""The fit card: the stored-fit list, the paste box, and what Done writes.

The card is the only door to the fit store, so what is pinned here is the
gate on that door -- a paste the parser could not fully resolve cannot be
saved, because a fit stored with a line missing reports every ship carrying
that module as deviating and looks authoritative doing it -- and the two
ways Enter can go wrong in a card whose body is an editor: the paste box
must keep its newline, and the name field must save rather than apply.

The polarity radios are the other half: ``fit:`` and ``-fit:`` are the same
question of the same fit, so the card owns both and Done writes whichever is
ticked.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from evasset import omni

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from conftest import match_text  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from evasset.ui import fit_card as fc  # noqa: E402

RATTING, SHIELD, HAULING = "Ratting", "Shield", "Hauling"

# The shape fits.list_fits returns.
FITS = [
    {"fit_id": 1, "name": RATTING, "hull_type_id": 645, "hull": "Dominix",
     "modules": 14, "created_at": "2026-09-05T00:00:00+00:00"},
    {"fit_id": 2, "name": SHIELD, "hull_type_id": 645, "hull": "Dominix",
     "modules": 12, "created_at": "2026-09-05T00:00:00+00:00"},
    {"fit_id": 3, "name": HAULING, "hull_type_id": 20185, "hull": "Charon",
     "modules": 1, "created_at": "2026-09-05T00:00:00+00:00"},
]


@dataclass
class _Item:
    """fits.FitItem's shape, as far as the card reads it."""

    type_id: int
    name: str
    slot_kind: str
    quantity: int


@dataclass
class _Parsed:
    """fits.ParsedFit's shape: the card renders it and never builds one."""

    hull_type_id: int | None = None
    hull_name: str = ""
    name: str = ""
    items: list = field(default_factory=list)
    unknown: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.hull_type_id is not None and not self.unknown


GOOD = _Parsed(
    hull_type_id=645,
    hull_name="Dominix",
    name=RATTING,
    items=[
        _Item(519, "Drone Damage Amplifier II", "module", 5),
        _Item(3186, "Large Armor Repairer II", "module", 1),
        _Item(31055, "Large Trimark Armor Pump I", "rig", 2),
        _Item(2456, "Hobgoblin II", "drone", 5),
        _Item(2185, "Hammerhead II", "drone", 5),
        _Item(222, "Antimatter Charge M", "cargo", 2000),
    ],
)


@pytest.fixture
def app():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def card(app):
    c = fc.FitCard()
    c.set_fits(FITS)
    yield c
    c.hide()
    c.deleteLater()


def record(signal) -> list:
    calls: list = []
    signal.connect(lambda *args: calls.append(args))
    return calls


def lines(card) -> list[str]:
    """The list's rendered rows, in order."""
    out = []
    for index in range(card.fit_list.count()):
        item = card.fit_list.item(index)
        widget = card.fit_list.itemWidget(item)
        out.append(widget.findChild(QtWidgets.QLabel).text())
    return out


def select(card, name: str) -> None:
    for index in range(card.fit_list.count()):
        if card.fit_list.item(index).data(Qt.UserRole + 1)["name"] == name:
            card.fit_list.setCurrentRow(index)
            return
    raise AssertionError(f"{name} is not in the list")


def test_the_list_names_each_fit_with_its_hull_and_size(card):
    """Two fits of one hull are told apart by their name and their module
    count, which is what makes the list readable without opening either."""
    assert lines(card) == [
        f"{RATTING} · Dominix · 14 modules",
        f"{SHIELD} · Dominix · 12 modules",
        f"{HAULING} · Charon · 1 module",
    ]


def test_the_cross_asks_the_view_to_forget_one_fit(card):
    """The card is database-free: deleting is a request, and the list it
    shows afterwards is whatever the view hands back."""
    deletes = record(card.delete_requested)
    widget = card.fit_list.itemWidget(card.fit_list.item(1))
    widget.findChild(QtWidgets.QToolButton).click()
    assert deletes == [(2,)]
    assert lines(card)[1].startswith(SHIELD), "the card does not delete on its own"


def test_nothing_selected_writes_no_chip_and_disables_done(card):
    assert card.selected_fit() is None
    assert card.chips() == [] and not card.done_btn.isEnabled()


def test_done_writes_the_selected_fit_in_the_ticked_polarity(card):
    """One radio pair rather than two chips: the two questions are asked of
    the same fit and the hull scope is the same either way."""
    select(card, RATTING)
    assert card.done_btn.isEnabled()
    assert card.chips() == [omni.Chip(fc.FIT_KIND, RATTING)]

    card.deviate_radio.setChecked(True)
    assert card.chips() == [omni.Chip(fc.FIT_KIND, RATTING, negated=True)]


def test_the_card_owns_both_polarities(card):
    """Unlike the shell's default, a negated fit chip is the card's to
    rewrite: the radios express it, so leaving it standing beside the card's
    own output would filter twice."""
    assert card.owns(omni.Chip(fc.FIT_KIND, RATTING))
    assert card.owns(omni.Chip(fc.FIT_KIND, RATTING, negated=True))
    assert not card.owns(omni.Chip("category", "Ship"))


def test_seeding_selects_the_named_fit_and_sets_the_polarity(card):
    """A card opened on an existing chip and applied untouched hands back the
    same chip -- including the hull it counts against."""
    card.seed([omni.Chip(fc.FIT_KIND, SHIELD, negated=True)])
    card.set_fits(FITS)
    assert card.deviate_radio.isChecked()
    assert card.selected_fit()["fit_id"] == 2
    assert card.chips() == [omni.Chip(fc.FIT_KIND, SHIELD, negated=True)]
    assert card.totals() == ("ships", 645)

    card.seed([omni.Chip(fc.FIT_KIND, HAULING)])
    card.set_fits(FITS)
    assert card.match_radio.isChecked()
    assert card.totals() == ("ships", 20185)


def test_seeding_a_fit_that_is_no_longer_stored_selects_nothing(card):
    """Deleting the fit a chip names leaves the chip standing -- it now
    matches nothing, which is the honest answer -- and the card must open on
    an empty selection rather than on somebody else's fit."""
    card.seed([omni.Chip(fc.FIT_KIND, "Deleted")])
    card.set_fits(FITS)
    assert card.selected_fit() is None
    assert card.chips() == [] and card.totals() == ("ships", None)


def test_an_empty_card_counts_ships_of_no_particular_hull(card):
    assert card.totals() == ("ships", None)
    card.set_match_count(3, 9)
    assert match_text(card) == "3 of 9 match"


def test_a_selection_or_a_polarity_change_asks_for_a_fresh_count(card):
    changes = record(card.filter_changed)
    select(card, RATTING)
    assert len(changes) == 1 and card.match_count_label.text() == "…"
    card.set_match_count(2, 5)
    card.deviate_radio.setChecked(True)
    # Both radios of an exclusive pair report the change; one click is one
    # count, not two.
    assert len(changes) == 2 and card.match_count_label.text() == "…"


# --------------------------------------------------------------- the paste
def test_the_parse_status_says_what_the_paste_amounts_to():
    """Counts rather than a list: the question the line answers is "did it
    read what I pasted", and drones and cargo are counted as TYPES because
    EFT's quantities are the fit's wish, not the ship's contents."""
    assert fc.parse_status(GOOD) == (
        "Dominix · 6 modules · 2 rigs · 2 drone types · 1 cargo type"
    )
    assert fc.parse_status(_Parsed()) == fc.NO_HEADER
    # A header that was read but whose hull is not a ship -- a paste that
    # lost its first line, so the rack's top module became the header --
    # would be misreported by NO_HEADER, which sends the user hunting for a
    # bracket that is already there.
    assert fc.parse_status(_Parsed(hull_name="Focused Pulse Laser")) == (
        "Focused Pulse Laser is not a ship"
    )
    assert fc.parse_status(
        _Parsed(hull_type_id=645, hull_name="Dominix", unknown=["Foo", "Bar II"])
    ) == "Unknown: Foo, Bar II"
    assert fc.parse_status(None) == ""


def test_save_is_armed_only_by_a_clean_parse_with_a_name(card):
    """An unknown name is fatal rather than skippable, and a fit stored
    without a name would be unaddressable by a chip."""
    assert not card.save_btn.isEnabled()

    card.set_parsed(_Parsed(hull_type_id=645, hull_name="Dominix", name=RATTING,
                            unknown=["Rubbish II"]))
    assert not card.save_btn.isEnabled()
    assert card.status_label.text() == "Unknown: Rubbish II"

    card.set_parsed(_Parsed())
    assert not card.save_btn.isEnabled()

    card.set_parsed(GOOD)
    assert card.save_btn.isEnabled()
    assert card.name_edit.text() == RATTING

    # A fit stored under no name would be unaddressable by a chip.
    card.name_edit.selectAll()
    QTest.keyClick(card.name_edit, Qt.Key_Backspace)
    assert not card.save_btn.isEnabled()


def test_the_name_follows_the_header_until_the_user_names_it_themselves(card):
    """Renaming is the point of an editable field, so a fresh parse must not
    overwrite what the user typed -- but an untouched field must keep up
    with the header they just pasted."""
    card.set_parsed(GOOD)
    assert card.name_edit.text() == RATTING

    card.name_edit.setText("")
    QTest.keyClicks(card.name_edit, "My Ratting")
    card.set_parsed(_Parsed(hull_type_id=645, hull_name="Dominix", name="Other",
                            items=list(GOOD.items)))
    assert card.name_edit.text() == "My Ratting"


def test_saving_asks_the_view_with_the_name_and_the_raw_paste(card):
    saves = record(card.save_requested)
    card.paste_edit.setPlainText("[Dominix, Ratting]\nDrone Damage Amplifier II")
    card.set_parsed(GOOD)
    card.save_btn.click()
    assert saves == [(RATTING, "[Dominix, Ratting]\nDrone Damage Amplifier II")]


def test_a_settled_paste_asks_for_one_parse_and_an_emptied_box_clears_it(card):
    """The parse runs a query per line, so it waits out the paste the way
    every search box in the app waits out typing."""
    parses = record(card.parse_requested)
    card.paste_edit.setPlainText("[Dominix, Ratting]")
    assert parses == [], "debounced, not per keystroke"
    card._parse_debounce.flush()
    assert parses == [("[Dominix, Ratting]",)]

    card.set_parsed(GOOD)
    card.paste_edit.setPlainText("")
    card._parse_debounce.flush()
    assert len(parses) == 1, "an empty box has nothing to parse"
    assert card.status_label.text() == "" and not card.save_btn.isEnabled()


def test_enter_in_the_paste_box_is_a_newline_and_never_done(card):
    """A fit is many lines. Enter applying the card mid-paste would throw a
    half-pasted fit away."""
    done = record(card.done)
    select(card, RATTING)
    card.show()
    card.paste_edit.setFocus()
    QTest.keyClicks(card.paste_edit, "[Dominix, Ratting]")
    QTest.keyClick(card.paste_edit, Qt.Key_Return)
    QTest.keyClicks(card.paste_edit, "Large Armor Repairer II")
    assert done == [] and card.isVisible()
    assert card.paste_edit.toPlainText().splitlines() == [
        "[Dominix, Ratting]", "Large Armor Repairer II"
    ]


def test_enter_in_the_name_field_saves_rather_than_applying(card):
    """A QLineEdit ignores Return, so left to travel it would climb to the
    card and apply a chip while the user was naming a fit to store."""
    done, saves = record(card.done), record(card.save_requested)
    select(card, RATTING)
    card.show()
    card.paste_edit.setPlainText("[Dominix, Ratting]")
    card.set_parsed(GOOD)
    card.name_edit.setFocus()
    QTest.keyClick(card.name_edit, Qt.Key_Return)
    assert done == [] and card.isVisible()
    assert saves == [(RATTING, "[Dominix, Ratting]")]


def test_the_card_is_the_shared_width_and_opens_on_the_paste_box_until_a_fit_is_picked(card):
    """A blank card must not land focus on the list: QAbstractItemView makes
    row 0 current on focus-in, which once pre-selected the first stored fit
    for a user who had picked nothing. With a selection the list is home."""
    card.show()
    assert card.width() == fc.FilterCard.WIDTH
    assert card.selected_fit() is None
    assert card.first_focus() is card.paste_edit
    card.fit_list.setCurrentRow(0)
    assert card.selected_fit() is not None
    assert card.first_focus() is card.fit_list
    image = card.grab().toImage()
    assert not image.isNull() and image.width() == fc.FilterCard.WIDTH
