"""Click-to-sort, driven by hand instead of QTableView's built-in
setSortingEnabled(True) wiring.

The sort itself is asked of the *source model*, not of the proxy in front of
it. Sorting through QSortFilterProxyModel means Qt calls data() on the Python
model twice for every comparison, which measured 12.6 seconds of frozen GUI
per header click on 20,000 rows; RowTableModel.sort() does the same ordering
in about 19 ms by extracting each key once. The long version of why is in
that method's docstring.

A sort is still synchronous -- Qt models are not safe to touch from a worker
thread, so there is nowhere else to run it -- hence the wait cursor still
bracketing the call. It is now almost never visible, which is the point.

Driving the click by hand, rather than letting setSortingEnabled(True) do it,
is about ordering: Qt wires its own internal "click -> sort" connection
inside setSortingEnabled(True), before any handler this module adds, so an
external slot on the same signal is not guaranteed to run first. Doing it by
hand sidesteps that entirely.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QSortFilterProxyModel, Qt
from PySide6.QtWidgets import QApplication, QTableView


class SortController(QObject):
    def __init__(
        self, table: QTableView, proxy: QSortFilterProxyModel, parent: QObject | None = None
    ):
        super().__init__(parent)
        self.table = table
        self.proxy = proxy
        self.column = -1
        self.order = Qt.AscendingOrder

        table.setSortingEnabled(False)  # driven by hand from here, see module docstring
        header = table.horizontalHeader()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self._on_header_clicked)

    def _on_header_clicked(self, column: int) -> None:
        if column == self.column:
            self.order = (
                Qt.DescendingOrder if self.order == Qt.AscendingOrder else Qt.AscendingOrder
            )
        else:
            self.column = column
            self.order = Qt.AscendingOrder
        self._apply()

    def _apply(self) -> None:
        # The source model, not self.proxy -- see the module docstring. The
        # proxy is left unsorted, so its row mapping stays the identity and
        # every mapToSource() call site keeps working unchanged.
        model = self.proxy.sourceModel()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            model.sort(self.column, self.order)
        finally:
            QApplication.restoreOverrideCursor()
        self.table.horizontalHeader().setSortIndicator(self.column, self.order)

    def reset(self) -> None:
        """Back to insertion order, arrow cleared. Wired to View -> Reset
        sort in MainWindow."""
        self.column = -1
        self.order = Qt.AscendingOrder
        self._apply()
