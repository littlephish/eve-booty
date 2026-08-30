"""Qt table models over plain sqlite3.Row lists."""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtWidgets import QComboBox

from ..queries import DATE_COLUMNS, ISK_COLUMNS, NUMERIC_COLUMNS


def fill_combo(box: QComboBox, items: list[str], all_label: str) -> None:
    """Repopulate a filter dropdown from a plain list of strings, keeping
    whatever was selected before if it is still present.

    Shared by every async filter refresh (see AsyncQuery) so the "preserve
    selection, block signals while rebuilding" logic only exists once.
    """
    current = box.currentText()
    box.blockSignals(True)
    box.clear()
    box.addItem(all_label)
    for item in items:
        box.addItem(item)
    idx = box.findText(current)
    box.setCurrentIndex(max(idx, 0))
    box.blockSignals(False)


def fmt_date(v) -> str:
    """ESI hands back '2026-08-04T00:00:00Z'. Nobody wants to read the T."""
    if not v:
        return ""
    text = str(v).replace("T", " ")
    for suffix in ("+00:00", "Z"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return text[:16]


def fmt_isk(v: float) -> str:
    if v is None:
        return ""
    return f"{v:,.2f}"


def fmt_short_isk(v: float) -> str:
    """Compact ISK for headline labels: 1.24b, 812.4m, 55.0k."""
    if v is None:
        return "0"
    a = abs(v)
    for cutoff, suffix in ((1e12, "t"), (1e9, "b"), (1e6, "m"), (1e3, "k")):
        if a >= cutoff:
            return f"{v / cutoff:,.2f}{suffix}"
    return f"{v:,.0f}"


def fmt_num(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:,.2f}"
    return f"{v:,}"


def order_is_descending(order) -> bool:
    """Qt.SortOrder -> bool for Python's sort(reverse=...)."""
    return order == Qt.DescendingOrder


class RowTableModel(QAbstractTableModel):
    """Generic model over a list of sqlite3.Row plus a (key, header) column spec."""

    def __init__(self, columns: list[tuple[str, str]], rows: list[sqlite3.Row] | None = None):
        super().__init__()
        self._columns = columns
        self._keys = [key for key, _ in columns]
        self._source_rows: list[sqlite3.Row] = []   # the order the query returned
        self._rows: list[sqlite3.Row] = []          # the order on screen
        self._values: list[tuple | None] = []
        self._perm: list[int] = []                  # screen row -> source row
        self._sort_column = -1
        self._sort_order = Qt.AscendingOrder
        self.set_rows(rows or [])

    # ---- data plumbing
    def set_rows(self, rows: list[sqlite3.Row]) -> None:
        self.beginResetModel()
        self._source_rows = list(rows)
        # sqlite3.Row looks up a column by name with a linear scan every time
        # it is indexed, which is what made sorting a large table slow: a
        # sort touches every row several times over. Converting to a plain
        # tuple fixes that -- but doing the conversion for every row right
        # here, eagerly, turned out to be worse: set_rows() runs on every
        # reload, and reload() runs on every keystroke in the search box and
        # every filter change, not just on sort. A QTableView only ever
        # paints the handful of rows actually on screen, so eagerly
        # converting all of them defeated the point for the much more common
        # case. Instead each row is converted at most once, the first time
        # anything actually asks for it -- see value_at(). A sort still ends
        # up touching every row, so the cache still fills the same way, just
        # without paying for rows nothing ever looks at.
        # Re-apply whatever column the header arrow is still pointing at.
        # Every reload lands here -- a keystroke in search, a filter change --
        # and without this the table would silently fall back to query order
        # while the header went on claiming it was sorted.
        self._apply_sort()
        self.endResetModel()

    # ---- sorting
    def sort(self, column: int, order=Qt.AscendingOrder) -> None:
        """Sort here, in the model, rather than in a QSortFilterProxyModel.

        A proxy sorts by asking the source model for data() on both sides of
        every comparison, so an n-row sort costs about 2 n log n round trips
        from Qt's C++ across into Python -- roughly 570,000 of them for
        20,000 rows, measured at 12.6 seconds of completely frozen GUI per
        header click. Nothing about that is fixable from inside lessThan():
        the comparison is not the expensive part, the two data() calls
        wrapping it are.

        Extracting each row's key once and handing the list to Python's own
        (C-implemented, stable) sort is the same O(n log n) comparisons but
        pays the Python cost n times instead of 2 n log n. Same 20,000 rows,
        same machine: 19 ms.
        """
        self.layoutAboutToBeChanged.emit()
        stale = self.persistentIndexList()
        was = self._perm
        self._sort_column = column
        self._sort_order = order
        self._apply_sort()
        if stale:
            # Keep whatever was selected selected. A row's identity here is
            # its source index, which survives the reorder; its screen row
            # does not.
            landed = [0] * len(self._perm)
            for screen_row, source_row in enumerate(self._perm):
                landed[source_row] = screen_row
            self.changePersistentIndexList(
                stale,
                [
                    self.index(landed[was[ix.row()]], ix.column())
                    if 0 <= ix.row() < len(was) else QModelIndex()
                    for ix in stale
                ],
            )
        self.layoutChanged.emit()

    def _apply_sort(self) -> None:
        """Rebuild the on-screen order (and the value cache alongside it)
        from the source rows. Always from source, never from the current
        screen order, so "reset sort" is just column -1."""
        count = len(self._source_rows)
        column = self._sort_column
        if not 0 <= column < len(self._keys):
            self._rows = list(self._source_rows)
            self._values = [None] * count
            self._perm = list(range(count))
            return

        numeric = self._keys[column] in NUMERIC_COLUMNS
        keys = self._keys
        decorated = []
        for source_row, row in enumerate(self._source_rows):
            values = tuple(row[k] for k in keys)
            raw = values[column]
            if numeric:
                # Coerced rather than compared raw: one stray text value in a
                # numeric column would otherwise raise mid-sort, and a
                # half-sorted model is worse than a wrongly sorted one.
                try:
                    sort_key = float(raw)
                except (TypeError, ValueError):
                    sort_key = 0.0
            else:
                sort_key = "" if raw is None else str(raw)
            decorated.append((sort_key, source_row, values))

        # Stable, so equal keys stay in query order -- and stay that way when
        # reversed, which is what makes clicking a header twice feel sane.
        decorated.sort(key=lambda item: item[0], reverse=order_is_descending(self._sort_order))
        self._rows = [self._source_rows[item[1]] for item in decorated]
        self._values = [item[2] for item in decorated]
        self._perm = [item[1] for item in decorated]

    def rows(self) -> list[sqlite3.Row]:
        return self._rows

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._columns)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return self._columns[section][1]
        return section + 1

    def value_at(self, row: int, col: int):
        try:
            cached = self._values[row]
        except IndexError:
            return None
        if cached is None:
            cached = self._values[row] = tuple(self._rows[row][k] for k in self._keys)
        return cached[col]

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        key = self._keys[index.column()]
        raw = self.value_at(index.row(), index.column())

        if role == Qt.DisplayRole:
            if raw is None:
                return ""
            if key in DATE_COLUMNS:
                return fmt_date(raw)
            if key in ISK_COLUMNS:
                return fmt_isk(raw)
            if key in NUMERIC_COLUMNS:
                return fmt_num(raw)
            return str(raw)
        if role == Qt.TextAlignmentRole and key in NUMERIC_COLUMNS:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        # Sorting hook: proxies read this so numbers sort numerically.
        if role == Qt.UserRole:
            return raw if raw is not None else (0 if key in NUMERIC_COLUMNS else "")
        return None

    def column_sum(self, key: str) -> float:
        total = 0.0
        for r in self._rows:
            try:
                total += float(r[key] or 0)
            except (KeyError, TypeError, ValueError):
                pass
        return total
