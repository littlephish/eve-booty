"""The rollup rail that replaces the GroupPanel.

The panel it succeeds required an explicit Apply; the rail applies on a
single click because every applied filter is now a visible, deletable chip.
That contract inversion is what most of these tests pin: a click must emit
exactly one chip request, the pin star must not leak into it, a data refresh
with signals blocked must not fake one, and the type-ahead must stay a
client-side arrangement of rows already delivered rather than a re-query.
Rows are fabricated through an in-memory SQLite SELECT so the widget is
exercised against real sqlite3.Row objects, the same shape
queries.rail_rollups() delivers.
"""

from __future__ import annotations

import sqlite3

import pytest

from evasset import queries

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from evasset.ui.rail import Rail  # noqa: E402

LEVEL_KEYS = [key for _label, key in queries.ROLLUP_LEVELS]

JITA = "Jita IV - Moon 4 - Caldari Navy Assembly Plant"
AMARR = "Amarr VIII (Oris) - Emperor Family Academy"
DODIXIE = "Dodixie IX - Moon 20 - Federation Navy Assembly Plant"

#          label   stacks units volume  sell_value
ROLLUPS = [
    (JITA,    10, 100, 500.0, 4_000_000_000.0),
    (DODIXIE,  2,   5,  50.0, 2_000_000_000.0),
    (AMARR,    4,  40, 200.0, 1_000_000_000.0),
]


def rollup_rows(specs=ROLLUPS) -> list[sqlite3.Row]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE rollup(label TEXT, stacks INTEGER, units INTEGER,"
        " volume REAL, sell_value REAL)"
    )
    conn.executemany("INSERT INTO rollup VALUES (?,?,?,?,?)", specs)
    return list(conn.execute("SELECT * FROM rollup ORDER BY rowid"))


def flip_rows(specs) -> list[sqlite3.Row]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE flip(label TEXT, quantity INTEGER)")
    conn.executemany("INSERT INTO flip VALUES (?,?)", specs)
    return list(conn.execute("SELECT * FROM flip ORDER BY rowid"))


@pytest.fixture
def app():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def rail(app):
    r = Rail()
    # Clicks land on the list viewport by item rectangle, so the widget needs
    # real (offscreen) geometry before visualItemRect() means anything.
    r.resize(320, 480)
    r.show()
    app.processEvents()
    yield r
    r.deleteLater()


def listed(rail: Rail) -> list[dict]:
    """Every list entry's stored role data, captions included, in render order."""
    return [
        rail.rows_list.item(i).data(Qt.UserRole) for i in range(rail.rows_list.count())
    ]


def visible_row_labels(rail: Rail) -> list[str]:
    """Row labels the user can actually see -- the type-ahead hides items in
    place rather than rebuilding the list, so hidden ones must not count."""
    return [
        rail.rows_list.item(i).data(Qt.UserRole)["label"]
        for i in range(rail.rows_list.count())
        if rail.rows_list.item(i).data(Qt.UserRole)["kind"] in ("row", "unknown", "flip")
        and not rail.rows_list.item(i).isHidden()
    ]


def item_for(rail: Rail, label: str):
    matches = [
        rail.rows_list.item(i)
        for i in range(rail.rows_list.count())
        if rail.rows_list.item(i).data(Qt.UserRole)["label"] == label
    ]
    assert len(matches) == 1, f"expected exactly one row labelled {label!r}"
    return matches[0]


def click_row(rail: Rail, label: str) -> None:
    item = item_for(rail, label)
    rect = rail.rows_list.visualItemRect(item)
    assert rect.isValid() and rect.height() > 0  # the click must land on a laid-out row
    QTest.mouseClick(rail.rows_list.viewport(), Qt.LeftButton, Qt.NoModifier, rect.center())


def pick_level(rail: Rail, key: str) -> None:
    rail.level.setCurrentIndex(LEVEL_KEYS.index(key))


# ----------------------------------------------------------------- clicking
def test_clicking_a_row_emits_a_chip_for_the_current_level(rail, app):
    """The click-adds-a-chip contract in one gesture: a plain single click on
    a rollup row must hand the host (level key, label) and nothing else --
    there is no Apply button left to fall back on if this breaks."""
    rail.set_rollups(rollup_rows(), set())
    app.processEvents()
    chips: list[tuple[str, str]] = []
    rail.chip_requested.connect(lambda level, label: chips.append((level, label)))

    click_row(rail, AMARR)

    assert chips == [("location", AMARR)]


def test_clicking_the_star_pins_without_requesting_a_chip(rail, app):
    """The star sits inside the clickable row, so the failure mode worth
    pinning is a pin click that also filters the table -- the user asked to
    keep a row handy and instead the whole view changed under them."""
    rail.set_rollups(rollup_rows(), {JITA})
    app.processEvents()
    chips: list[tuple[str, str]] = []
    pins: list[tuple[str, str]] = []
    rail.chip_requested.connect(lambda level, label: chips.append((level, label)))
    rail.pin_toggled.connect(lambda level, label: pins.append((level, label)))

    star = rail.rows_list.itemWidget(item_for(rail, AMARR)).star
    QTest.mouseClick(star, Qt.LeftButton)

    assert pins == [("location", AMARR)]
    assert chips == []
    # The already-pinned row's star renders filled, the fresh one's did not.
    assert rail.rows_list.itemWidget(item_for(rail, JITA)).star.isChecked()


def test_a_rollup_refresh_emits_nothing_and_keeps_the_scroll_position(rail, app):
    """set_rollups rebuilds the list with signals blocked -- an unblocked
    rebuild would let Qt's item churn surface as chip_requested and re-filter
    the table on every background refresh. The scroll position must survive
    too, or a refresh mid-browse yanks the user back to the top."""
    many = rollup_rows([(f"Station {i:02d}", i + 1, i, 10.0, 1_000.0 * (i + 1)) for i in range(40)])
    rail.set_rollups(many, set())
    app.processEvents()
    bar = rail.rows_list.verticalScrollBar()
    assert bar.maximum() >= 5  # short list would make the scroll assertion vacuous
    bar.setValue(5)

    fired: list[str] = []
    rail.chip_requested.connect(lambda *_: fired.append("chip"))
    rail.pin_toggled.connect(lambda *_: fired.append("pin"))
    rail.refresh_needed.connect(lambda: fired.append("refresh"))
    rail.set_rollups(many, set())
    app.processEvents()

    assert fired == []
    assert bar.value() == 5


# ------------------------------------------------------------------ ordering
def test_pinned_rows_render_first_under_their_own_caption(rail, app):
    rail.set_rollups(rollup_rows(), {AMARR})
    app.processEvents()

    entries = listed(rail)
    assert [d["kind"] for d in entries] == ["caption", "row", "caption", "row", "row"]
    assert entries[0]["label"] == "Pinned"
    assert entries[1]["label"] == AMARR
    assert entries[2]["label"] == "All · by ISK"
    # Server-side order is preserved within the unpinned remainder.
    assert [d["label"] for d in entries[3:]] == [JITA, DODIXIE]


# --------------------------------------------------------------- type-ahead
def test_the_type_ahead_narrows_rows_client_side_without_a_requery(rail, app):
    """The type-ahead is a name filter over rows already in memory; a
    refresh_needed here would mean a full round trip per keystroke, which is
    exactly what the client-side design exists to avoid."""
    rail.set_rollups(rollup_rows(), set())
    app.processEvents()
    refreshes: list[bool] = []
    rail.refresh_needed.connect(lambda: refreshes.append(True))

    rail.search.setText("dodixie")
    assert visible_row_labels(rail) == [DODIXIE]
    assert refreshes == []

    # And the given rows were kept, not discarded: widening the filter brings
    # every row back without any new set_rollups.
    rail.search.setText("")
    assert visible_row_labels(rail) == [JITA, DODIXIE, AMARR]
    assert rail.filter_text() == ""


def test_the_type_ahead_hides_rows_instead_of_rebuilding_them(rail, app):
    """Performance-audit regression: each keystroke used to rebuild ~300
    widget rows at ~200 ms a press. Hiding in place must keep the very same
    widget objects alive across a filter change."""
    rail.set_rollups(rollup_rows(), set())
    app.processEvents()
    before = rail.rows_list.itemWidget(item_for(rail, JITA))

    rail.search.setText("jita")
    rail.search.setText("")

    assert rail.rows_list.itemWidget(item_for(rail, JITA)) is before, (
        "typing in the filter box must not recreate row widgets"
    )


def test_the_type_ahead_text_survives_a_rollup_refresh(rail, app):
    rail.set_rollups(rollup_rows(), set())
    rail.search.setText("amarr")
    assert visible_row_labels(rail) == [AMARR]

    rail.set_rollups(rollup_rows(), set())

    assert rail.filter_text() == "amarr"
    assert visible_row_labels(rail) == [AMARR]


# --------------------------------------------------------------------- sort
def test_changing_sort_emits_refresh_needed_and_current_sort_tracks_it(rail):
    """Sort is server-side: the host re-runs rail_rollups with current_sort(),
    so a button that changes state without emitting leaves the list silently
    mis-captioned, and one that emits without changing state re-queries for
    the identical order."""
    refreshes: list[bool] = []
    rail.refresh_needed.connect(lambda: refreshes.append(True))
    assert rail.current_sort() == "value"

    rail.sort_buttons["name"].click()
    assert rail.current_sort() == "name"
    assert len(refreshes) == 1

    rail.sort_buttons["volume"].click()
    assert rail.current_sort() == "volume"
    assert len(refreshes) == 2

    # Clicking the already-active segment is a no-op, not a re-query.
    rail.sort_buttons["volume"].click()
    assert rail.current_sort() == "volume"
    assert len(refreshes) == 2


# ------------------------------------------------------------------- levels
def test_the_level_combo_announces_the_key_not_the_label(rail):
    """The host re-queries rollups keyed by the level key; a display label
    emitted instead would silently break every level but the default."""
    seen: list[str] = []
    rail.level_changed.connect(seen.append)

    assert len(LEVEL_KEYS) == 6  # the loop below must really iterate
    for index, key in enumerate(LEVEL_KEYS):
        rail.level.setCurrentIndex(index)
        assert rail.current_level() == key

    # Index 0 was already current so it never re-fires; every move must have.
    assert seen == LEVEL_KEYS[1:]


# -------------------------------------------------------- unknown locations
UNKNOWN_SPECS = [
    (JITA, 10, 100, 500.0, 4_000_000_000.0),
    ("Unknown location 1038457641", 1, 1, 10.0, 100.0),
    ("Unknown location 1038457642", 2, 2, 20.0, 200.0),
    ("Unknown location 1038457643", 3, 3, 30.0, 300.0),
]


def test_unknown_locations_collapse_into_one_summed_row_at_location_level(rail, app):
    """Unresolved structure ids would otherwise fill the rail with rows whose
    labels mean nothing and whose chips filter to nothing recognisable."""
    rail.set_rollups(rollup_rows(UNKNOWN_SPECS), set())
    app.processEvents()

    assert visible_row_labels(rail) == [JITA, "Unknown locations (3)"]
    item = item_for(rail, "Unknown locations (3)")
    assert item.data(Qt.UserRole)["kind"] == "unknown"
    widget = rail.rows_list.itemWidget(item)
    assert widget.sub.text() == "6 stacks · 60 m³"
    assert widget.isk.text() == "600"  # 100 + 200 + 300 summed
    assert widget.star is None  # nothing stable to pin
    assert "next sync" in widget.toolTip()


def test_a_structure_actually_named_unknown_location_is_not_swallowed(rail, app):
    """Adversarial-review regression: the fold used a bare prefix match, so a
    player structure someone really named "Unknown location HQ" vanished into
    the synthetic row and could never be filtered to. Only the machine
    pattern -- the prefix plus a bare numeric id -- collapses."""
    named = "Unknown location HQ of Bob"
    rail.set_rollups(
        rollup_rows([(named, 1, 1, 5.0, 100.0), ("Unknown location 1041669946862", 2, 2, 10.0, 200.0)]),
        set(),
    )
    app.processEvents()

    labels = visible_row_labels(rail)
    assert named in labels, "a real structure name must stay its own clickable row"
    assert "Unknown locations (1)" in labels


def test_unknown_locations_stay_separate_at_group_level(rail, app):
    """The fold keys on a label prefix, so it must be scoped to the one level
    where that prefix is machine-generated -- an item group really named
    "Unknown location ..." elsewhere must not be swallowed."""
    pick_level(rail, "group")
    rail.set_rollups(rollup_rows(UNKNOWN_SPECS), set())
    app.processEvents()

    labels = visible_row_labels(rail)
    assert len(labels) == 4
    assert "Unknown locations (3)" not in labels


def test_clicking_the_synthetic_unknown_row_emits_nothing(rail, app):
    """There is no single chip that selects every unresolved location, so a
    click here must not fabricate one."""
    rail.set_rollups(rollup_rows(UNKNOWN_SPECS), set())
    app.processEvents()
    chips: list[tuple[str, str]] = []
    rail.chip_requested.connect(lambda level, label: chips.append((level, label)))

    click_row(rail, "Unknown locations (3)")
    assert chips == []

    click_row(rail, JITA)  # the wiring itself works; only the synthetic row is inert
    assert chips == [("location", JITA)]


# ---------------------------------------------------------------- flip mode
def test_set_flip_renders_quantities_and_clear_flip_restores_rollups(rail, app):
    rail.set_rollups(rollup_rows(), {JITA})
    app.processEvents()

    rail.set_flip(flip_rows([(JITA, 12), (AMARR, 3)]))
    app.processEvents()

    entries = listed(rail)
    assert entries[0] == {"kind": "caption", "label": "Where is it · by quantity"}
    assert [d["kind"] for d in entries[1:]] == ["flip", "flip"]
    jita = rail.rows_list.itemWidget(item_for(rail, JITA))
    assert jita.quantity.text() == "12"
    assert not hasattr(jita, "star")  # flip rows carry no pin control at all

    # The type-ahead keeps working over the flip rows.
    rail.search.setText("amarr")
    assert visible_row_labels(rail) == [AMARR]
    rail.search.setText("")

    rail.clear_flip()
    app.processEvents()
    assert visible_row_labels(rail) == [JITA, DODIXIE, AMARR]
    # And the pin star came back with the rollup rendering.
    assert rail.rows_list.itemWidget(item_for(rail, JITA)).star is not None


# --------------------------------------------------------------- value bars
def test_the_value_rides_the_meta_line_and_rows_track_the_rail_width(rail, app):
    """Regression for two reported defects: the ISK value crowded long
    station names on the top line, and item size hints froze at each
    widget's natural width, so a full-width value bar ran past the rail's
    visible edge instead of ending at it."""
    rail.set_rollups(rollup_rows(), set())
    app.processEvents()

    widget = rail.rows_list.itemWidget(item_for(rail, JITA))
    assert widget.isk.geometry().top() > widget.name.geometry().top(), (
        "the compact ISK must sit on the meta line, below the name"
    )

    viewport = rail.rows_list.viewport().width()
    assert viewport > 0
    hints = [rail.rows_list.item(i).sizeHint().width() for i in range(rail.rows_list.count())]
    assert hints and all(w <= viewport for w in hints)

    rail.resize(210, 480)
    app.processEvents()
    narrower = rail.rows_list.viewport().width()
    assert narrower < viewport, "the resize must actually narrow the viewport"
    hints = [rail.rows_list.item(i).sizeHint().width() for i in range(rail.rows_list.count())]
    assert hints and all(w <= narrower for w in hints), (
        "rows must re-fit when the splitter narrows the rail"
    )


def test_value_bar_fractions_scale_to_the_largest_visible_row(rail, app):
    """The bar is the rail's only at-a-glance value comparison; the fraction
    is stored on the widget precisely so proportionality is testable
    offscreen instead of by measuring pixels nothing painted."""
    rail.set_rollups(rollup_rows(), set())
    app.processEvents()

    fractions = {
        label: rail.rows_list.itemWidget(item_for(rail, label)).bar.fraction
        for label in (JITA, DODIXIE, AMARR)
    }
    assert len(fractions) == 3  # all three rows really carried a bar
    assert fractions[JITA] == pytest.approx(1.0)  # 4b is the max
    assert fractions[DODIXIE] == pytest.approx(0.5)  # 2b / 4b
    assert fractions[AMARR] == pytest.approx(0.25)  # 1b / 4b
