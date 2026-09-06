"""The comparison window: two columns of racks and holds, the verdict
colours, the type icons on the lines, and closing.

Rendered offscreen over the fit corpus with the dialog's own AsyncQuery run
inline, so every line's text, status and colour can be read off the labels
the way a user reads them. The model behind the columns is pinned in
tests/test_fits.py; what is pinned here is that the window draws that model
faithfully -- the same rack at the same height on both sides, the Drones
and Cargo sections under the racks, the deviation colours on the deviating
lines and nowhere else -- and that closing it stops a result in flight from
painting into a window that is gone.

The type icons on the lines come through the same loader the View fit
window uses, which hands the fetch to the thread pool. Every test here
runs with icons.fetch_icons replaced by an in-process fake -- the real one
would go to CCP's image server for the corpus's invented ids -- and the
fake is not undone until the pool has drained, so a fetch started by the
last dialog of a test cannot outlive the fake and reach the network.
"""

from __future__ import annotations

import threading
from pathlib import Path

import fit_corpus as fc
import pytest

from evasset import db, fits, icons

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import Qt, QThreadPool  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QLabel  # noqa: E402

from evasset.ui import palette  # noqa: E402
from evasset.ui.fit_compare_dialog import FitCompareDialog  # noqa: E402
from evasset.ui.type_icons import MODULE_ICON_PX  # noqa: E402

SHIP_NAME = "Test Dominix"

# Opaque and nothing like the translucent grey placeholder, so one pixel
# says whether a fetched icon or the placeholder is on a label.
ICON_COLOUR = QColor(200, 30, 30, 255)


class _FakeIconSource:
    """Stands in for icons.fetch_icons: serves PNG files written under the
    test's tmp_path for the ids it was given, records every call, and can
    hold a call open on a gate so a test can close or reload the window
    while the fetch is still in flight."""

    def __init__(self, root: Path):
        self.root = root
        self.files: dict[int, Path] = {}
        self.calls: list[list[int]] = []
        self.gate: threading.Event | None = None

    def serve(self, *type_ids: int) -> None:
        for tid in type_ids:
            path = self.root / f"{tid}.png"
            image = QImage(icons.ICON_SIZE, icons.ICON_SIZE, QImage.Format_ARGB32)
            image.fill(ICON_COLOUR)
            assert image.save(str(path), "PNG")
            self.files[tid] = path

    def __call__(self, type_ids, settings=None, transport=None):
        self.calls.append(list(type_ids))
        if self.gate is not None:
            self.gate.wait(5)
        return {tid: self.files[tid] for tid in type_ids if tid in self.files}


@pytest.fixture
def app():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def icon_source(monkeypatch, tmp_path):
    source = _FakeIconSource(tmp_path)
    monkeypatch.setattr(icons, "fetch_icons", source)
    yield source
    # A gate left closed would hang the pool; open it, then let every job
    # report before the fake is undone.
    if source.gate is not None:
        source.gate.set()
    QThreadPool.globalInstance().waitForDone(5000)


@pytest.fixture
def conn(tmp_path):
    c = db.init(tmp_path / "compare.sqlite")
    fc.install(c)
    return c


def open_dialog(conn, ship: int, hull: int = fc.DOMINIX, fit_name: str | None = None):
    """A dialog whose query runs inline on the corpus connection.

    AsyncQuery would otherwise hand the fetch to a pool thread that opens
    the *default* database; defer_load leaves a gap to swap run() before
    the first load, the same trick the assets-view tests play.
    """
    dialog = FitCompareDialog(ship, SHIP_NAME, hull, fit_name, defer_load=True)
    dialog._query.run = lambda fn, on_done, on_failed=None: on_done(fn(conn))
    dialog.load(fit_name)
    return dialog


def settle_icons(app) -> None:
    """Let the pool finish and the queued done signal reach the loader."""
    QThreadPool.globalInstance().waitForDone(5000)
    app.processEvents()


def icon_slot(label) -> QtWidgets.QLabel:
    """The icon label to the left of a line's text label."""
    return label.parentWidget().layout().itemAt(0).widget()


def slot_colour(label) -> QColor:
    return icon_slot(label).pixmap().toImage().pixelColor(
        MODULE_ICON_PX // 2, MODULE_ICON_PX // 2
    )


def statuses(labels) -> list[tuple[str, str, int, str]]:
    """(caption, name, quantity, status) per rendered line, read off the
    CompareLine each label was built from and the property it carries."""
    out = []
    for caption, line, label in labels:
        assert label.property("status") == line.status
        out.append((caption, line.name, line.quantity, line.status))
    return out


def under(labels, caption: str):
    """The (line, label) pairs drawn under one caption, in grid order."""
    return [(line, label) for c, line, label in labels if c == caption]


def grid_row(dialog: FitCompareDialog, label) -> int:
    """The grid row of a line's text label. Its row widget is what the grid
    holds, the label sitting inside it beside the icon slot."""
    index = dialog.grid.indexOf(label.parentWidget())
    assert index >= 0, "a line's row widget must be a direct child of the grid"
    row, _column, _rows, _columns = dialog.grid.getItemPosition(index)
    return row


def test_the_two_columns_render_the_racks_side_by_side_with_the_deviations_coloured(app, conn):
    """The Solo Dominix against Ratting by name is the busiest corpus case:
    a split line on the ship's side, three missing lines and a placeholder
    on the fit's, matches in both, and under the racks the two stacks the
    bare hull has none of. The extra laser is red, the missing modules
    green, the matches uncoloured and the placeholder muted; the headline
    carries the fit, the hull and the figures; and the rig rack's one line
    sits on the same grid row on both sides."""
    dialog = open_dialog(conn, fc.SHIP_SOLO, fit_name=fc.RATTING)
    assert dialog.windowTitle() == f"Compare with {fc.RATTING}"
    assert dialog.verdict.text() == "5 missing · 1 extra · 2 short"
    assert dialog.note.text() == f"compared with {fc.RATTING} · Dominix"
    # The column heads name their sides in words: the left is the ship under
    # the pointer, the right the stored fit -- two racks of the same modules
    # look alike, and a bare fit name over one column did not say which.
    ship_lines = [w.text() for w in dialog.ship_header.findChildren(QLabel)]
    fit_lines = [w.text() for w in dialog.fit_header.findChildren(QLabel)]
    assert ship_lines == ["<b>This ship</b>", f"Inspecting: {dialog.ship_name}"]
    assert fit_lines == ["<b>Stored fit</b>", f"Compared against: {fc.RATTING}"]
    assert dialog.status.isHidden()
    assert dialog.captions == [
        "High slots", "Mid slots", "Low slots", "Rig slots", "Drones", "Cargo"
    ]

    assert statuses(dialog.ship_labels) == [
        ("High slots", "Focused Pulse Laser", 1, fits.MATCH),
        ("High slots", "Focused Pulse Laser", 1, fits.EXTRA),
        ("Rig slots", "Auxiliary Nano Pump", 1, fits.MATCH),
        ("Drones", fc.DRONE_NAME, 0, fits.SHORT),
        ("Cargo", fc.AMMO_NAME, 0, fits.SHORT),
    ]
    assert statuses(dialog.fit_labels) == [
        ("High slots", "Focused Pulse Laser", 1, fits.MATCH),
        ("High slots", "Rocket Launcher Array", 1, fits.MISSING),
        ("Mid slots", "[Empty Med slot]", 0, fits.EMPTY),
        ("Low slots", "Medium Armor Repair Unit", 1, fits.MISSING),
        ("Low slots", "Magnetic Field Amplifier", 3, fits.MISSING),
        ("Rig slots", "Auxiliary Nano Pump", 1, fits.MATCH),
        ("Drones", fc.DRONE_NAME, 5, fits.MISSING),
        ("Cargo", fc.AMMO_NAME, 100, fits.MISSING),
    ]

    red = palette.delta_hex(False, dialog.palette())
    green = palette.delta_hex(True, dialog.palette())
    assert red != green
    for _caption, line, label in dialog.ship_labels + dialog.fit_labels:
        sheet = label.styleSheet()
        if line.status in (fits.EXTRA, fits.SHORT):
            assert red in sheet and green not in sheet
        elif line.status == fits.MISSING:
            assert green in sheet and red not in sheet
        elif line.status == fits.EMPTY:
            assert palette.SECONDARY_TEXT in sheet
        else:
            assert sheet == "", "a match is drawn in the ordinary text colour"
    assert "1 × Focused Pulse Laser" in dialog.ship_labels[1][2].text()
    assert "3 × Magnetic Field Amplifier" in dialog.fit_labels[4][2].text()
    assert dialog.fit_labels[2][2].text() == "[Empty Med slot]", "no count on a placeholder"
    assert dialog.ship_labels[4][2].text() == f"0 × {fc.AMMO_NAME} (of 100)"
    assert dialog.fit_labels[7][2].text() == f"100 × {fc.AMMO_NAME}"

    # The racks line up: the two rig lines share a grid row, and the ship's
    # first high line sits opposite the fit's first high line; the Cargo
    # lines are opposite each other too, under the racks.
    assert grid_row(dialog, dialog.ship_labels[2][2]) == grid_row(dialog, dialog.fit_labels[5][2])
    assert grid_row(dialog, dialog.ship_labels[0][2]) == grid_row(dialog, dialog.fit_labels[0][2])
    cargo_row = grid_row(dialog, dialog.ship_labels[4][2])
    assert cargo_row == grid_row(dialog, dialog.fit_labels[7][2])
    assert cargo_row > grid_row(dialog, dialog.ship_labels[2][2])
    assert not hasattr(dialog, "short_label"), "the Short paragraph is gone"
    dialog.close()


def test_a_matching_ship_colours_only_the_stack_it_is_short_of(app, conn):
    """The permuted Dominix matches Ratting with its rounds loaded: every
    rack line is a match, the launcher carries the loaded rounds as a muted
    note on its own line rather than as a line of their own, and the only
    thing short is the drones. That shortfall is the one coloured pair on
    the screen -- the red "0 × (of 5)" under Drones opposite the green
    "5 ×" -- while the 650 rounds (the 50 loaded counted in) and the crate
    the fit never asked for sit under Cargo in the ordinary text colour.
    The exact match has nothing short, so the verdict is a bare Matches
    and nothing under the racks is coloured either."""
    dialog = open_dialog(conn, fc.SHIP_PERMUTED)
    assert dialog.verdict.text() == "Matches · 1 short"
    racks = [
        (c, line, label) for c, line, label in dialog.ship_labels + dialog.fit_labels
        if c not in fits.HOLD_DISPLAY_ORDER
    ]
    assert len(racks) == 11
    assert {line.status for _c, line, _l in racks} == {fits.MATCH, fits.EMPTY}
    assert all(label.styleSheet() == "" for _c, line, label in racks if line.status == fits.MATCH)
    launcher = next(label for _c, line, label in dialog.ship_labels if line.type_id == fc.LAUNCHER)
    assert f"loaded: 50 × {fc.AMMO_NAME}" in launcher.text()
    assert dialog._muted_hex() in launcher.text(), "the note is muted, the module is not"
    assert not any(line.name == fc.AMMO_NAME for _c, line, _l in racks)

    red = palette.delta_hex(False, dialog.palette())
    green = palette.delta_hex(True, dialog.palette())
    [(drone, drone_label)] = under(dialog.ship_labels, "Drones")
    assert drone.status == fits.SHORT and drone_label.text() == f"0 × {fc.DRONE_NAME} (of 5)"
    assert red in drone_label.styleSheet()
    [(wanted, wanted_label)] = under(dialog.fit_labels, "Drones")
    assert wanted.status == fits.MISSING and green in wanted_label.styleSheet()
    assert grid_row(dialog, drone_label) == grid_row(dialog, wanted_label)

    cargo = under(dialog.ship_labels, "Cargo")
    assert [(line.name, line.quantity, line.status) for line, _l in cargo] == [
        (fc.AMMO_NAME, 650, fits.MATCH),
        ("Reinforced Cargo Crate", 1, fits.EXTRA),
    ]
    assert all(label.styleSheet() == "" for _line, label in cargo), "surplus is not red"
    assert cargo[1][1].text() == "1 × Reinforced Cargo Crate"
    assert under(dialog.fit_labels, "Cargo")[0][1].text() == f"100 × {fc.AMMO_NAME}"

    exact = open_dialog(conn, fc.SHIP_EXACT)
    assert exact.verdict.text() == "Matches"
    holds = [
        (c, line, label) for c, line, label in exact.ship_labels + exact.fit_labels
        if c in fits.HOLD_DISPLAY_ORDER
    ]
    assert len(holds) == 5, "eight drones, two fighters, the rounds; the fit's drones and rounds"
    assert all(label.styleSheet() == "" for _c, _line, label in holds)
    assert {line.status for _c, line, _l in holds} == {fits.MATCH, fits.EXTRA}
    dialog.close()
    exact.close()


def test_escape_and_the_close_button_both_close_and_cancel_the_query(app, conn):
    """Esc is the reflex for a window like this and the button is there for
    the mouse; both must end in the same place -- hidden, flagged closed,
    and the query's generation bumped so anything still running is dropped
    rather than delivered."""
    by_key = open_dialog(conn, fc.SHIP_MISSING)
    by_key.show()
    before = by_key._query._generation
    QTest.keyClick(by_key, Qt.Key_Escape)
    assert not by_key.isVisible() and by_key._closed
    assert by_key._query._generation == before + 1

    by_button = open_dialog(conn, fc.SHIP_MISSING)
    by_button.show()
    before = by_button._query._generation
    by_button.close_btn.click()
    assert not by_button.isVisible() and by_button._closed
    assert by_button._query._generation == before + 1
    assert not by_button.close_btn.autoDefault(), "Enter must not close the window by accident"


def test_a_stale_result_arriving_after_close_paints_nothing_and_does_not_crash(app, conn):
    """The generation guard drops a superseded result, but a result handed
    straight to the callback -- which is what a test, or a race the guard
    was not built for, does -- must still find nothing to paint into. The
    failure path is held to the same rule."""
    dialog = FitCompareDialog(fc.SHIP_MISSING, SHIP_NAME, fc.DOMINIX, defer_load=True)
    pending: list[tuple] = []
    dialog._query.run = lambda fn, on_done, on_failed=None: pending.append((fn, on_done, on_failed))
    dialog.load(None)
    dialog.show()
    assert len(pending) == 1
    dialog.close()
    fetch, deliver, fail = pending[0]
    deliver(fetch(conn))
    assert dialog.comparison is None and dialog.ship_labels == [] and dialog.fit_labels == []
    assert dialog.grid.count() == 0
    fail("boom")
    assert "boom" not in dialog.status.text()


def test_a_hull_with_no_stored_fit_says_so_instead_of_an_empty_grid(app, conn):
    """The menu disables the action for such a hull, so this is the fit
    having been deleted between the click and the fetch -- rare, and the
    one case where a blank window would read as a broken one."""
    dialog = open_dialog(conn, fc.SHIP_NO_FIT, hull=fc.SOLSTICE)
    assert dialog.comparison is None
    assert not dialog.status.isHidden()
    assert dialog.status.text() == "No stored fit for this hull."
    assert dialog.grid.count() == 0
    dialog.close()


def test_reloading_against_another_fit_rebuilds_the_columns_from_scratch(app, conn):
    """The host re-runs an open window when the ship is compared again, so
    a fit chip added since the first open changes what is shown. The grid
    must be rebuilt, not appended to: a leftover label from the previous
    fit would sit under the new columns as a line the new fit never had."""
    dialog = open_dialog(conn, fc.SHIP_SOLO)
    assert dialog.windowTitle() == f"Compare with {fc.SOLO}"
    assert dialog.verdict.text() == "Matches"
    solo_count = dialog.grid.count()

    dialog.load(fc.RATTING)
    assert dialog.windowTitle() == f"Compare with {fc.RATTING}"
    assert dialog.verdict.text() == "5 missing · 1 extra · 2 short"
    assert len(dialog.ship_labels) == 5 and len(dialog.fit_labels) == 8
    # Two headers, two captions per section over four racks and two holds,
    # thirteen line rows (each an icon slot beside its text).
    assert dialog.grid.count() == 2 + 6 * 2 + 13
    assert dialog.grid.count() != solo_count

    dialog.load(fc.SOLO)
    assert dialog.grid.count() == solo_count, "a reload back is the same grid again"
    dialog.close()


def test_every_line_has_an_icon_slot_and_only_the_empty_placeholder_a_blank_one(
    app, conn, icon_source
):
    """The slot is what lines the text up: a line with a type behind it
    shows the placeholder until the fetch lands, and the fit's own "[Empty
    Med slot]" note, which has no type, keeps the same-sized slot empty so
    its text starts where the others' does rather than sliding left. The
    fetch asks for each type once across both columns and never for the
    placeholder's missing id."""
    dialog = open_dialog(conn, fc.SHIP_SOLO, fit_name=fc.RATTING)
    dialog.show()
    app.processEvents()
    lines = dialog.ship_labels + dialog.fit_labels
    assert len(lines) == 13
    text_x = set()
    for _caption, line, label in lines:
        slot = icon_slot(label)
        assert slot.width() == slot.height() == MODULE_ICON_PX
        if line.status == fits.EMPTY:
            assert slot.pixmap().isNull(), "no placeholder for a slot the fit leaves empty"
        else:
            assert not slot.pixmap().isNull()
            assert slot_colour(label) != ICON_COLOUR, "nothing has been served yet"
        text_x.add(label.mapTo(dialog.body, label.rect().topLeft()).x())
    assert len(text_x) == 2, "one text column per side, the placeholder in line with the rest"

    settle_icons(app)
    [asked] = icon_source.calls
    assert None not in asked
    assert len(asked) == len(set(asked))
    assert set(asked) == {line.type_id for _c, line, _l in lines if line.type_id is not None}
    dialog.close()


def test_a_fetched_icon_lands_on_every_line_of_its_type_and_nowhere_else(app, conn, icon_source):
    """The laser is on both sides of the high rack -- a match line and an
    extra line on the ship's, a match on the fit's -- so one fetched file
    must reach three labels, while the rest keep their placeholders (a 404
    or an offline fetch is the same shape). The fetch is one pooled call
    for the whole window, not one per line."""
    icon_source.serve(fc.PULSE_LASER)
    dialog = open_dialog(conn, fc.SHIP_SOLO, fit_name=fc.RATTING)
    dialog.show()
    settle_icons(app)

    assert len(icon_source.calls) == 1
    assert set(icon_source.calls[0]) == set(dialog._icons.type_ids)
    landed = [
        line for _c, line, label in dialog.ship_labels + dialog.fit_labels
        if line.type_id is not None and slot_colour(label) == ICON_COLOUR
    ]
    assert len(landed) == 3
    assert {line.type_id for line in landed} == {fc.PULSE_LASER}
    others = [
        label for _c, line, label in dialog.ship_labels + dialog.fit_labels
        if line.type_id not in (None, fc.PULSE_LASER)
    ]
    assert others and all(slot_colour(label) != ICON_COLOUR for label in others)
    assert not dialog._icons._inflight, "the job is released once it has reported"
    dialog.close()


def test_an_icon_arriving_after_the_window_closed_or_reloaded_is_dropped(
    app, conn, icon_source, monkeypatch
):
    """The host deleteLater()s a closed comparison window and load() deletes
    the previous grid's labels, so a fetch that outlives either would paint
    into labels whose C++ side is gone. The loader must drop such a result
    unread -- and still release the job, which it had to hold until then."""
    icon_source.serve(fc.PULSE_LASER)
    icon_source.gate = threading.Event()
    applied: list[dict] = []

    closed = open_dialog(conn, fc.SHIP_SOLO, fit_name=fc.RATTING)
    monkeypatch.setattr(closed._icons, "apply", applied.append)
    closed.show()
    late_job = closed._icons._job
    assert late_job is not None and late_job in closed._icons._inflight
    closed.close()

    reloaded = open_dialog(conn, fc.SHIP_SOLO, fit_name=fc.RATTING)
    monkeypatch.setattr(reloaded._icons, "apply", applied.append)
    stale_job = reloaded._icons._job
    reloaded.load(fc.SOLO)
    fresh_job = reloaded._icons._job
    assert stale_job is not None and fresh_job is not None and fresh_job is not stale_job

    icon_source.gate.set()
    settle_icons(app)
    assert late_job not in closed._icons._inflight
    assert not reloaded._icons._inflight
    assert len(applied) == 1, "only the reloaded grid's own fetch is painted"
    assert applied[0] == {fc.PULSE_LASER: icon_source.files[fc.PULSE_LASER]}
    reloaded.close()


def test_copy_shopping_list_puts_the_missing_items_on_the_clipboard_and_says_so(app, conn):
    """The button is the window's hand-off to the market: enabled only when
    something is missing, and the text it copies is the comparison's own
    multibuy list, so the two can never disagree."""
    from PySide6.QtGui import QGuiApplication

    dialog = open_dialog(conn, fc.SHIP_SOLO, fit_name=fc.RATTING)
    assert dialog.shopping_btn.isEnabled()
    QGuiApplication.clipboard().setText("")
    dialog.shopping_btn.click()
    text = QGuiApplication.clipboard().text()
    assert text == dialog.comparison.shopping_list() and text
    assert dialog.copied.text() == f"Copied {text.count(chr(10)) + 1} lines for multibuy."
    dialog.close()

    exact = open_dialog(conn, fc.SHIP_EXACT, fit_name=fc.RATTING)
    assert not exact.shopping_btn.isEnabled(), "nothing to buy for a matching ship"
    exact.close()

