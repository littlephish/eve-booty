"""Sorting a table, which lives in RowTableModel rather than in the proxy.

The bug this guards against: sorting used to go through
QSortFilterProxyModel, which asks the source model for data() on both sides
of every comparison. That is ~2 n log n round trips from Qt's C++ into
Python -- about 570,000 of them for 20,000 rows -- and it froze the whole
window for 12.6 seconds on a single header click. RowTableModel.sort() does
the ordering itself, extracting each key once, and measured 19 ms on the
same data.

test_sorting_does_not_call_data_at_all is the regression guard, and it is
deliberately a call count rather than a stopwatch: if sorting is ever moved
back behind the proxy the count goes from zero to hundreds of thousands,
which is exactly the thing that made the app hang, and it fails the same way
on a fast machine as on a slow one.
"""

from __future__ import annotations

import sqlite3

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QPersistentModelIndex, Qt  # noqa: E402

from evasset.ui.models import RowTableModel  # noqa: E402

COLUMNS = [("item", "Item"), ("quantity", "Qty"), ("sell_value", "Sell value")]


def rows_from(values):
    """Real sqlite3.Row objects -- the model indexes rows by column name, and
    sqlite3.Row's name lookup is part of what sorting has to pay for."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE r (item TEXT, quantity INT, sell_value REAL)")
    conn.executemany("INSERT INTO r VALUES (?,?,?)", values)
    return list(conn.execute("SELECT * FROM r"))


def displayed(model, key="item"):
    col = [k for k, _ in COLUMNS].index(key)
    return [model.value_at(r, col) for r in range(model.rowCount())]


@pytest.fixture
def model():
    return RowTableModel(COLUMNS, rows_from([
        ("Charon", 9, 30.0),
        ("Tritanium", 1_000_000, 10.0),
        ("EMP S", 50, 20.0),
    ]))


def test_numbers_sort_numerically_not_as_text(model):
    """The original reason for a sort role at all: as text, 1,000,000 sorts
    before 9."""
    model.sort(1, Qt.AscendingOrder)
    assert displayed(model, "quantity") == [9, 50, 1_000_000]


def test_descending_reverses(model):
    model.sort(2, Qt.DescendingOrder)
    assert displayed(model, "sell_value") == [30.0, 20.0, 10.0]


def test_text_sorts_alphabetically(model):
    model.sort(0, Qt.AscendingOrder)
    assert displayed(model) == ["Charon", "EMP S", "Tritanium"]


def test_reset_restores_the_order_the_query_returned(model):
    before = list(model.rows())
    model.sort(0, Qt.AscendingOrder)
    assert list(model.rows()) != before
    model.sort(-1, Qt.AscendingOrder)
    assert list(model.rows()) == before


def test_the_active_sort_survives_a_reload():
    """Every search keystroke calls set_rows. Without re-applying, the table
    would quietly revert to query order while the header arrow still claimed
    a column was sorted."""
    model = RowTableModel(COLUMNS, rows_from([("B", 2, 2.0), ("A", 1, 1.0)]))
    model.sort(0, Qt.AscendingOrder)
    assert displayed(model) == ["A", "B"]

    model.set_rows(rows_from([("D", 4, 4.0), ("C", 3, 3.0)]))
    assert displayed(model) == ["C", "D"]


def test_rows_match_what_is_on_screen(model):
    """Views index model.rows() by the clicked row, so the two must agree."""
    model.sort(0, Qt.AscendingOrder)
    assert [r["item"] for r in model.rows()] == displayed(model)


def test_a_stray_text_value_in_a_numeric_column_does_not_raise():
    """A half-sorted model is worse than a wrongly sorted one, so the numeric
    key is coerced rather than compared raw."""
    model = RowTableModel(COLUMNS, rows_from([
        ("A", 5, 1.0), ("B", None, 2.0), ("C", 3, 3.0),
    ]))
    model.sort(1, Qt.AscendingOrder)
    assert displayed(model, "quantity") == [None, 3, 5]  # None coerces to 0, sorts first


def test_missing_text_sorts_as_empty_not_as_the_word_none():
    model = RowTableModel(COLUMNS, rows_from([("B", 1, 1.0), (None, 2, 2.0)]))
    model.sort(0, Qt.AscendingOrder)
    assert displayed(model) == [None, "B"]


def test_equal_keys_keep_query_order_in_both_directions():
    """Stability is what makes clicking a header twice feel like a reversal
    rather than a reshuffle."""
    model = RowTableModel(COLUMNS, rows_from([
        ("same", 1, 1.0), ("same", 2, 2.0), ("same", 3, 3.0),
    ]))
    model.sort(0, Qt.AscendingOrder)
    assert displayed(model, "quantity") == [1, 2, 3]
    model.sort(0, Qt.DescendingOrder)
    assert displayed(model, "quantity") == [1, 2, 3]


def test_a_selected_row_stays_selected_across_a_sort(model):
    """Sorting emits layoutChanged, which invalidates nothing on its own --
    the persistent indexes Qt hands out for the selection have to be moved to
    where their rows landed, or the selection silently jumps to another row."""
    tritanium = QPersistentModelIndex(model.index(1, 0))
    assert model.value_at(tritanium.row(), 0) == "Tritanium"

    model.sort(0, Qt.AscendingOrder)      # Tritanium moves from row 1 to row 2
    assert tritanium.row() == 2
    assert model.value_at(tritanium.row(), 0) == "Tritanium"


def test_sorting_does_not_call_data_at_all():
    """The regression guard. Sorting through a proxy means Qt calls data()
    twice per comparison; sorting in the model means it is not called at all
    (only painting calls it, and nothing is painted here)."""
    calls = []

    class Counting(RowTableModel):
        def data(self, index, role=Qt.DisplayRole):
            calls.append(role)
            return super().data(index, role)

    model = Counting(COLUMNS, rows_from([(f"item {i}", i, float(i)) for i in range(500)]))
    calls.clear()
    model.sort(1, Qt.DescendingOrder)

    assert calls == [], f"sorting made {len(calls)} data() calls; it should make none"
    assert displayed(model, "quantity")[:3] == [499, 498, 497]
