"""The structure detail dialog: Overview, Fit and History in one window.

Overview and History both render from rows handed straight to them, so these
tests never touch the thread pool -- the real dialog loads through AsyncQuery
against the app's own database, which a test has no business reaching into.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QLabel  # noqa: E402

from evasset.ui.structure_dialog import StructureDialog  # noqa: E402

SID = 1048466934500


def structure(**kw):
    row = {
        "structure_id": SID,
        "name": "Ikami - 0064 p11m1",
        "type_id": 35835,
        "type_name": "Athanor",
        "system_name": "Ikami",
        "security": 0.5,
        "region_name": "The Forge",
        "owner_name": "Work Hard for My Money",
        "owner_ticker": "DOLRS",
        "alliance_id": 99000001,
        "alliance_name": "Krill Issue Holdings",
        "state": "shield_vulnerable",
        "state_timer_start": None,
        "state_timer_end": None,
        "fuel_expires": "2126-09-24T04:00:00Z",
        "reinforce_hour": 18,
        "next_reinforce_hour": None,
        "next_reinforce_apply": None,
        "unanchors_at": None,
        "services": json.dumps([{"name": "Moon Drilling", "state": "online"}]),
        "updated_at": "2026-09-05T13:42:00Z",
        "gone_at": None,
        "moon_id": 40000001,
        "chunk_arrival_time": "2026-09-16T18:01:00Z",
        "natural_decay_time": "2026-09-16T21:01:00Z",
        "extraction_start_time": "2026-09-16T18:01:00Z",
        "first_seen": "2026-08-21T13:43:00Z",
    }
    row.update(kw)
    return row


@pytest.fixture
def dialog(qapp_or_skip):
    d = StructureDialog(SID, "Ikami - 0064 p11m1", defer_load=True)
    yield d


def overview_text(dialog) -> str:
    return " | ".join(
        label.text() for label in dialog.overview.findChildren(QLabel)
    )


def history_texts(dialog) -> list[str]:
    model = dialog.history_model
    return [
        model.data(model.index(r, 1))
        for r in range(model.rowCount())
    ]


# ------------------------------------------------------------------- tabs
def test_the_dialog_has_the_three_tabs(dialog):
    labels = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
    assert labels == ["Overview", "Fit", "History"]


def test_it_opens_on_the_overview(dialog):
    assert dialog.tabs.tabText(dialog.tabs.currentIndex()) == "Overview"


def test_it_can_be_asked_to_open_on_the_fit(qapp_or_skip):
    """The Structures tab keeps a "View fit" entry, and it should land where
    it says it will rather than making you find the tab."""
    d = StructureDialog(SID, "x", defer_load=True, tab="Fit")
    assert d.tabs.tabText(d.tabs.currentIndex()) == "Fit"


# --------------------------------------------------------------- overview
def test_the_overview_shows_what_it_was_given(dialog):
    dialog.show_structure(structure())
    text = overview_text(dialog)
    for expected in ("Athanor", "Ikami", "The Forge", "Work Hard for My Money",
                     "Krill Issue Holdings"):
        assert expected in text


def test_the_overview_shows_the_structure_id(dialog):
    dialog.show_structure(structure())
    assert str(SID) in overview_text(dialog)


def test_a_fuelled_structure_reads_as_full_power(dialog):
    dialog.show_structure(structure())
    assert "Full power" in overview_text(dialog)


def test_an_unfuelled_structure_does_not(dialog):
    """The fuel clock is the only thing we have to go on -- ESI does not
    report power state -- so an expired one is the whole signal."""
    dialog.show_structure(structure(fuel_expires="2020-01-01T00:00:00Z"))
    assert "Full power" not in overview_text(dialog)


def test_power_is_worded_as_something_we_worked_out(dialog):
    """ESI never says "full power". Presenting the inference as a reported
    fact would be the kind of quiet fiction this app avoids."""
    dialog.show_structure(structure())
    assert "Inferred" in overview_text(dialog) or "from the fuel" in overview_text(dialog)


def test_first_seen_is_labelled_as_first_seen_not_anchored(dialog):
    """It is the earliest thing recorded, which for anything predating the
    history feature is the day tracking started."""
    dialog.show_structure(structure())
    text = overview_text(dialog)
    assert "First seen" in text
    assert "Anchored" not in text


def test_an_unanchored_structure_says_so(dialog):
    dialog.show_structure(structure(gone_at="2026-09-05T13:42:00Z"))
    assert "nanchored" in overview_text(dialog)


def test_a_structure_that_went_away_is_not_an_error(dialog):
    """A sync can mark it gone while the dialog is open."""
    dialog.show_structure(None)
    assert overview_text(dialog)


# ---------------------------------------------------------------- history
def change(field, old=None, new=None, at="2026-09-05T13:42:00Z"):
    return {"observed_at": at, "field": field, "old_value": old, "new_value": new}


def test_the_history_lists_what_happened(dialog):
    dialog.show_history([change("state", "shield_vulnerable", "armor_reinforce")])
    assert any("Armor reinforce" in t for t in history_texts(dialog))


def test_the_history_puts_the_newest_first(dialog):
    dialog.show_history([
        change("name", "Old", "New", at="2026-09-01T00:00:00Z"),
        change("state", "a", "b", at="2026-09-05T00:00:00Z"),
    ])
    assert "→" in history_texts(dialog)[0]


def test_an_empty_history_says_why_rather_than_showing_nothing(dialog):
    """On day one every structure's history is empty, and an empty grid reads
    as broken rather than as "nothing has happened yet"."""
    dialog.show_history([])
    assert dialog.history_empty.isVisible() or dialog.history_empty.text()


def test_the_history_says_its_times_are_when_a_sync_noticed(dialog):
    """The one piece of wording the whole design leans on: these are not game
    times, and a reader planning around them needs to know that."""
    assert "noticed" in dialog.history_note.text().lower()
