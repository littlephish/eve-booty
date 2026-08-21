"""Click-to-sort, driven by hand instead of QTableView's built-in
setSortingEnabled(True) wiring.

QSortFilterProxyModel.sort() is synchronous -- Qt gives no way to run a model
sort off the GUI thread -- so on a table with tens of thousands of rows,
clicking a header genuinely pauses the app for as long as the sort takes.
That is not fixable by threading it (Qt models are not thread-safe to touch
from a worker thread); the best available fix is telling the user it is
happening, the same way any native app would for a moment of unavoidable
synchronous work: a wait cursor bracketing the call.

Doing that reliably needs full control over when the sort actually runs.
Connecting to signals QHeaderView already emits around a built-in sort is
order-dependent -- Qt's own internal "click -> sort" connection was wired up
inside setSortingEnabled(True) before any handler this module adds, so an
external slot connected to the same click signal is not guaranteed to run
before Qt's own sort does. Driving the whole thing by hand sidesteps that
entirely.
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
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.proxy.sort(self.column, self.order)
        finally:
            QApplication.restoreOverrideCursor()
        self.table.horizontalHeader().setSortIndicator(self.column, self.order)

    def reset(self) -> None:
        """Back to insertion order, arrow cleared. Wired to View -> Reset
        sort in MainWindow."""
        self.column = -1
        self.order = Qt.AscendingOrder
        self._apply()
