"""The stockpile add-item picker, whose type search runs off the GUI thread.

It used to call stockpile.search_types() inline from textChanged, so every
keystroke ran a LIKE '%...%' across the whole types table on the GUI thread
and the window stopped repainting until it came back. It now goes through
AsyncQuery like every other read in the app.

Two things here are easy to get wrong and are covered deliberately: the
search text has to be read on the GUI thread rather than inside the closure
that runs on the pool, and a dialog closed while a search is still running
must not have its result delivered into widgets that no longer exist.
"""

from __future__ import annotations

import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import Qt, QThreadPool  # noqa: E402

from evasset import db  # noqa: E402
from evasset.ui.stockpile_view import AddItemDialog  # noqa: E402

SEED = """
INSERT OR REPLACE INTO sde_categories VALUES (4,'Material',1);
INSERT OR REPLACE INTO sde_groups VALUES (18,4,'Mineral',1);
INSERT OR REPLACE INTO sde_types (type_id,name,group_id,volume,portion_size,published) VALUES
  (34,'Tritanium',18,0.01,1,1),
  (35,'Pyerite',18,0.01,1,1),
  (36,'Mexallon',18,0.01,1,1),
  (37,'Tritanium Reaction Formula',18,0.01,1,1),
  (38,'Unpublished Thing',18,0.01,1,0);
"""


@pytest.fixture
def app():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def dialog(app):
    """Seeds the default database: AsyncQuery runs on a pool thread against
    db.connect(), which always opens the configured one. conftest points that
    at a scratch directory."""
    conn = db.init()
    conn.executescript(SEED)
    conn.commit()
    d = AddItemDialog()
    yield d
    d.close()


def settle(dialog, want_results=True, timeout=3.0):
    """Pump until the pooled search has landed."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        QThreadPool.globalInstance().waitForDone(20)
        QtWidgets.QApplication.processEvents()
        if (dialog.results.count() > 0) == want_results:
            return
        time.sleep(0.005)


def names(dialog):
    return [dialog.results.item(i).text() for i in range(dialog.results.count())]


def test_the_search_finds_matching_types(dialog):
    dialog.search.setText("Tritanium")
    dialog._debounce.flush()
    settle(dialog)
    assert "Tritanium" in names(dialog)


def test_results_carry_the_type_id(dialog):
    """chosen() reads it back out of the item, so it has to be there."""
    dialog.search.setText("Pyerite")
    dialog._debounce.flush()
    settle(dialog)
    item = dialog.results.item(0)
    assert item.data(Qt.UserRole) == 35


def test_a_one_letter_needle_matches_nothing(dialog):
    """search_types refuses anything under two characters -- a single letter
    would drag most of the SDE back."""
    dialog.search.setText("T")
    dialog._debounce.flush()
    settle(dialog, want_results=False)
    assert names(dialog) == []


def test_unpublished_types_are_not_offered(dialog):
    dialog.search.setText("Unpublished")
    dialog._debounce.flush()
    settle(dialog, want_results=False)
    assert names(dialog) == []


def test_the_needle_is_read_on_the_gui_thread(dialog, monkeypatch):
    """The closure handed to AsyncQuery runs on a pool thread, where touching
    a QLineEdit is not safe. The text must be captured before it is handed
    over -- so changing the box after _search() returns must not change what
    the pooled query looks for."""
    seen = []
    import evasset.stockpile as stockpile_mod

    real = stockpile_mod.search_types

    def spy(conn, needle, *a, **kw):
        seen.append(needle)
        return real(conn, needle, *a, **kw)

    monkeypatch.setattr(stockpile_mod, "search_types", spy)

    dialog.search.setText("Mexallon")
    dialog._search()                     # hands the closure to the pool
    dialog.search.setText("something else entirely")   # after the handoff
    settle(dialog)

    assert seen == ["Mexallon"], f"the pooled query read the box late: {seen}"


def test_closing_the_dialog_drops_an_in_flight_search(dialog):
    """A dialog can be destroyed while its search is still on the pool. The
    result must be discarded rather than delivered into dead widgets."""
    delivered = []
    dialog._show_matches = lambda matches: delivered.append(matches)

    dialog.search.setText("Tritanium")
    dialog._search()
    dialog.done(0)                        # user hit Cancel straight away

    deadline = time.time() + 1.0
    while time.time() < deadline:
        QThreadPool.globalInstance().waitForDone(20)
        QtWidgets.QApplication.processEvents()
        time.sleep(0.005)

    assert delivered == [], "a cancelled search still called back into the dialog"


def test_a_failed_search_says_so_instead_of_staying_blank(dialog):
    dialog._on_search_failed("no such table: sde_types")
    assert dialog.results.count() == 0
    assert "no such table" in dialog.status.text()
    assert dialog.status.isVisibleTo(dialog)


def test_the_dialog_takes_no_connection():
    """It reads through AsyncQuery now, so there is nothing for a caller to
    hand it -- same as every view since the connection passing was removed."""
    import inspect

    params = list(inspect.signature(AddItemDialog.__init__).parameters)
    assert params == ["self", "parent"]
