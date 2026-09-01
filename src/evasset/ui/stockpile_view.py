"""Stockpile tab: what you want on hand against what you have.

Targets are edited in place in the table rather than behind a dialog -- the
whole job is "that number is wrong, make it 40", and making someone open a
window to do it is the difference between keeping a stockpile current and
abandoning it.

Reads go through AsyncQuery like every other view. Writes are small and
immediate (one row of one table) so they run on the GUI thread; a target
edit that took a visible moment to land would feel broken.
"""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .. import db, stockpile
from .assets_view import _SortProxy
from .async_query import AsyncQuery
from .debounce import Debounce
from .models import fmt_isk, fmt_num
from .palette import CRITICAL, SECONDARY_TEXT, WARN, status_brush
from .sort_controller import SortController

SCOPE_LABELS = [
    ("Anywhere", stockpile.ANY),
    ("Region", stockpile.REGION),
    ("System", stockpile.SYSTEM),
    ("Station or structure", stockpile.STATION),
]


class _StockpileModel(QAbstractTableModel):
    target_edited = Signal(int, float)      # type_id, new target

    COLUMNS = [
        ("item", "Item"),
        ("target", "Target"),
        ("have", "Have"),
        ("shortfall", "Short"),
        ("percent", "%"),
        ("shortfall_isk", "Short ISK"),
        ("shortfall_m3", "Short m³"),
    ]
    NUMERIC = {"target", "have", "shortfall", "percent", "shortfall_isk", "shortfall_m3"}

    def __init__(self, rows: list[dict] | None = None):
        super().__init__()
        self._keys = [k for k, _ in self.COLUMNS]
        self._rows: list[dict] = list(rows or [])

    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def rows(self) -> list[dict]:
        return self._rows

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return self.COLUMNS[section][1]
        return section + 1

    def flags(self, index: QModelIndex):
        base = super().flags(index)
        if index.isValid() and self._keys[index.column()] == "target":
            return base | Qt.ItemIsEditable
        return base

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        key = self._keys[index.column()]
        value = row[key]

        if role == Qt.EditRole and key == "target":
            return float(value)
        if role == Qt.DisplayRole:
            if key == "percent":
                return f"{value:.0f}%"
            if key == "shortfall_isk":
                return fmt_isk(value) if value else ""
            if key == "shortfall_m3":
                return f"{value:,.1f}" if value else ""
            if key in self.NUMERIC:
                return fmt_num(value)
            return str(value)
        if role == Qt.TextAlignmentRole and key in self.NUMERIC:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.UserRole:
            return value
        if role == Qt.ForegroundRole and key in ("shortfall", "percent") and row["shortfall"] > 0:
            # Holding none of something is a different problem from holding
            # nearly enough of it, and the number alone does not say which.
            return status_brush(CRITICAL if row["have"] <= 0 else WARN)
        if role == Qt.ToolTipRole:
            parts = [
                f"{label}: {fmt_num(row[key])}"
                for key, label in (
                    ("have_assets", "In assets"),
                    ("have_orders", "On the market"),
                    ("have_jobs", "In production"),
                    ("have_contracts", "In contracts"),
                )
                if row[key]
            ]
            return "\n".join(parts) if parts else "Nothing held"
        return None

    def setData(self, index: QModelIndex, value, role=Qt.EditRole) -> bool:  # noqa: N802
        if role != Qt.EditRole or self._keys[index.column()] != "target":
            return False
        try:
            target = max(0.0, float(value))
        except (TypeError, ValueError):
            return False
        self.target_edited.emit(self._rows[index.row()]["type_id"], target)
        return True


class StockpileDialog(QDialog):
    """Create or edit the rule: whose stock, where, and what counts."""

    def __init__(self, conn, pile: stockpile.Stockpile | None = None, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.pile = pile
        self.setWindowTitle("Edit stockpile" if pile else "New stockpile")
        self.setMinimumWidth(430)

        form = QFormLayout(self)

        self.name = QLineEdit(pile.name if pile else "")
        self.name.setPlaceholderText("Doctrine spares, Jita buffer, …")
        form.addRow("Name", self.name)

        self.owner = QComboBox()
        self.owner.addItem("Any owner", None)
        for r in conn.execute(
            "SELECT character_id, name FROM characters ORDER BY name COLLATE NOCASE"
        ):
            self.owner.addItem(r["name"], ("character", r["character_id"]))
        for r in conn.execute(
            "SELECT corporation_id, COALESCE(name,'Corp '||corporation_id) name "
            "FROM corporations ORDER BY name COLLATE NOCASE"
        ):
            self.owner.addItem(r["name"], ("corporation", r["corporation_id"]))
        form.addRow("Owner", self.owner)

        self.scope = QComboBox()
        for label, value in SCOPE_LABELS:
            self.scope.addItem(label, value)
        self.scope.currentIndexChanged.connect(self._reload_places)
        form.addRow("Location", self.scope)

        self.place = QComboBox()
        self.place.setToolTip("Only places you are already holding things are listed")
        form.addRow("", self.place)

        self.multiplier = QDoubleSpinBox()
        self.multiplier.setRange(0.1, 1000.0)
        self.multiplier.setDecimals(2)
        self.multiplier.setSingleStep(1.0)
        self.multiplier.setValue(pile.multiplier if pile else 1.0)
        self.multiplier.setToolTip("Scales every target — 3 for three fleets' worth")
        form.addRow("Multiplier", self.multiplier)

        self.orders = QCheckBox("Sell orders on the market")
        self.jobs = QCheckBox("Manufacturing jobs still running")
        self.contracts = QCheckBox("Items in outstanding contracts")
        hint = QLabel("Assets are always counted. These add to what counts as held.")
        hint.setStyleSheet(f"color: {SECONDARY_TEXT};")
        hint.setWordWrap(True)
        form.addRow("Also count", self.orders)
        form.addRow("", self.jobs)
        form.addRow("", self.contracts)
        form.addRow("", hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        if pile:
            idx = self.owner.findData(
                (pile.owner_type, pile.owner_id) if pile.owner_type else None
            )
            self.owner.setCurrentIndex(max(idx, 0))
            self.scope.setCurrentIndex(max(self.scope.findData(pile.location_scope), 0))
            self.orders.setChecked(pile.include_orders)
            self.jobs.setChecked(pile.include_jobs)
            self.contracts.setChecked(pile.include_contracts)
        self._reload_places()
        if pile and pile.location_id is not None:
            self.place.setCurrentIndex(max(self.place.findData(pile.location_id), 0))

    def _reload_places(self) -> None:
        scope = self.scope.currentData()
        self.place.clear()
        places = stockpile.places_for_scope(self.conn, scope)
        self.place.setEnabled(bool(places))
        if not places:
            self.place.addItem("Not applicable" if scope == stockpile.ANY else "Nothing held yet", None)
            return
        for place_id, name in places:
            self.place.addItem(name, place_id)

    def _accept(self) -> None:
        if not self.name.text().strip():
            QMessageBox.warning(self, "Name needed", "Give the stockpile a name.")
            return
        self.accept()

    def values(self) -> dict:
        owner = self.owner.currentData()
        return {
            "name": self.name.text().strip(),
            "owner_type": owner[0] if owner else None,
            "owner_id": owner[1] if owner else None,
            "location_scope": self.scope.currentData(),
            "location_id": self.place.currentData(),
            "multiplier": self.multiplier.value(),
            "include_orders": self.orders.isChecked(),
            "include_jobs": self.jobs.isChecked(),
            "include_contracts": self.contracts.isChecked(),
        }


class AddItemDialog(QDialog):
    """Type-ahead picker over every published type in the SDE.

    The search runs through AsyncQuery like every other read in the app. It
    used to run inline on the GUI thread, which on a LIKE '%...%' across the
    whole types table is long enough to feel: the window stopped repainting
    for the duration of every keystroke. Debouncing hid most of it, but the
    query was still landing on the wrong thread.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add item")
        self.setMinimumWidth(380)
        layout = QVBoxLayout(self)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Type at least two letters")
        self.search.setClearButtonEnabled(True)
        self._query = AsyncQuery(self)
        # Debounce as well as async: the generation counter stops a stale
        # result being shown, but only the timer stops the query running.
        self._debounce = Debounce(self, self._search)
        self.search.textChanged.connect(self._debounce.trigger)
        layout.addWidget(self.search)

        self.results = QListWidget()
        self.results.itemDoubleClicked.connect(lambda _: self._accept())
        layout.addWidget(self.results, 1)

        row = QHBoxLayout()
        row.addWidget(QLabel("Target"))
        self.target = QDoubleSpinBox()
        self.target.setRange(0, 1_000_000_000)
        self.target.setDecimals(0)
        self.target.setValue(1)
        row.addWidget(self.target, 1)
        layout.addLayout(row)

        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {SECONDARY_TEXT};")
        self.status.hide()
        layout.addWidget(self.status)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _search(self) -> None:
        # Read the box here, on the GUI thread, and close over the result.
        # The closure below runs on a pool thread, where touching a widget is
        # not safe.
        needle = self.search.text()

        def fetch(conn: sqlite3.Connection) -> list[tuple[int, str]]:
            return stockpile.search_types(conn, needle)

        self._query.run(fetch, self._show_matches, self._on_search_failed)

    def _show_matches(self, matches: list[tuple[int, str]]) -> None:
        # Cleared here rather than when the search starts: clearing up front
        # made the list flash empty between every keystroke and its results.
        self.results.clear()
        self.status.hide()
        for type_id, name in matches:
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, type_id)
            self.results.addItem(item)

    def _on_search_failed(self, message: str) -> None:
        self.results.clear()
        self.status.setText(f"Search failed: {message}")
        self.status.show()

    def done(self, result: int) -> None:
        """exec() is about to return and this dialog is about to be dropped.
        Two things have to be called off, not one: the search already on the
        pool, and any keystroke still sitting on the debounce clock -- that
        one would otherwise start a brand new query a fifth of a second after
        the dialog was gone."""
        self._debounce.stop()
        self._query.cancel()
        super().done(result)

    def _accept(self) -> None:
        if self.results.currentItem() is None:
            QMessageBox.information(self, "Pick an item", "Choose one from the list.")
            return
        self.accept()

    def chosen(self) -> tuple[int, float] | None:
        item = self.results.currentItem()
        if item is None:
            return None
        return int(item.data(Qt.UserRole)), float(self.target.value())


class StockpileView(QWidget):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        defer_load: bool = False,
    ):
        super().__init__(parent)
        # Unlike the other tabs this one writes, and edits land on the GUI
        # thread (see the module docstring), so it does need a connection of
        # its own. db.connect() returns this thread's cached one -- the very
        # object MainWindow's db.init() created, not a second connection.
        self.conn = db.connect()
        self._query = AsyncQuery(self)

        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Stockpile"))
        self.picker = QComboBox()
        self.picker.setMinimumWidth(220)
        self.picker.currentIndexChanged.connect(self.reload)
        bar.addWidget(self.picker, 2)

        self.btn_new = QPushButton("New…")
        self.btn_edit = QPushButton("Edit…")
        self.btn_delete = QPushButton("Delete")
        self.btn_add = QPushButton("Add item…")
        self.btn_copy = QPushButton("Copy shopping list")
        self.btn_copy.setToolTip("EVE multibuy text for everything short")
        for b in (self.btn_new, self.btn_edit, self.btn_delete, self.btn_add, self.btn_copy):
            bar.addWidget(b)
        bar.addStretch(1)
        root.addLayout(bar)

        self.table = QTableView()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.model = _StockpileModel()
        self.model.target_edited.connect(self._on_target_edited)
        self.proxy = _SortProxy()
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(Qt.UserRole)
        self.table.setModel(self.proxy)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.sorter = SortController(self.table, self.proxy, self)
        root.addWidget(self.table, 1)

        self.empty = QLabel(
            "No stockpiles yet.\n\n"
            "A stockpile is a list of what you want to keep on hand — doctrine "
            "spares, production inputs, a Jita buffer — and it tells you what is "
            "missing and what it would cost to top up.\n\n"
            "Press New… to make one."
        )
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet(f"color: {SECONDARY_TEXT};")
        self.empty.setVisible(False)
        root.addWidget(self.empty)

        self.footer = QLabel("")
        self.footer.setStyleSheet(f"color: {SECONDARY_TEXT};")
        root.addWidget(self.footer)

        self.btn_new.clicked.connect(self.new_stockpile)
        self.btn_edit.clicked.connect(self.edit_stockpile)
        self.btn_delete.clicked.connect(self.delete_stockpile)
        self.btn_add.clicked.connect(self.add_item)
        self.btn_copy.clicked.connect(self.copy_shopping_list)

        if not defer_load:
            self.first_load()

    # ------------------------------------------------------------ lifecycle
    def reset_sort(self) -> None:
        self.sorter.reset()

    def first_load(self) -> None:
        self.refresh_filters()

    def refresh_filters(self) -> None:
        """Repopulate the picker, keeping the current selection if it survives."""
        current = self.picker.currentData()
        piles = stockpile.list_all(self.conn)
        self.picker.blockSignals(True)
        self.picker.clear()
        for pile in piles:
            self.picker.addItem(pile.name, pile.stockpile_id)
        if current is not None:
            idx = self.picker.findData(current)
            if idx >= 0:
                self.picker.setCurrentIndex(idx)
        self.picker.blockSignals(False)
        self._set_enabled(bool(piles))
        self.reload()

    def _set_enabled(self, has_any: bool) -> None:
        for b in (self.btn_edit, self.btn_delete, self.btn_add, self.btn_copy):
            b.setEnabled(has_any)
        self.picker.setEnabled(has_any)
        self.empty.setVisible(not has_any)
        self.table.setVisible(has_any)

    def current_id(self) -> int | None:
        data = self.picker.currentData()
        return None if data is None else int(data)

    def reload(self) -> None:
        pile_id = self.current_id()
        if pile_id is None:
            self.model.set_rows([])
            self.footer.setText("")
            return

        self._query.run(
            lambda conn, pid=pile_id: stockpile.rows(conn, pid),
            self._render,
        )

    def _render(self, rows: list[dict]) -> None:
        self.model.set_rows(rows)
        if not rows:
            self.footer.setText("Nothing tracked yet — press Add item…")
            return
        summary = stockpile.totals(rows)
        text = (
            f"{summary['items']} item(s) · {summary['percent']:.0f}% stocked"
        )
        if summary["short"]:
            text += (
                f" · {summary['short']} short"
                f" · {fmt_isk(summary['shortfall_isk'])} ISK"
                f" · {summary['shortfall_m3']:,.0f} m³ to buy"
            )
        else:
            text += " · complete"
        self.footer.setText(text)

    # -------------------------------------------------------------- actions
    def new_stockpile(self) -> None:
        dialog = StockpileDialog(self.conn, None, self)
        if not dialog.exec():
            return
        values = dialog.values()
        pile_id = stockpile.create(self.conn, values.pop("name"), **values)
        self.refresh_filters()
        idx = self.picker.findData(pile_id)
        if idx >= 0:
            self.picker.setCurrentIndex(idx)

    def edit_stockpile(self) -> None:
        pile_id = self.current_id()
        if pile_id is None:
            return
        pile = stockpile.get(self.conn, pile_id)
        dialog = StockpileDialog(self.conn, pile, self)
        if not dialog.exec():
            return
        stockpile.update(self.conn, pile_id, **dialog.values())
        self.refresh_filters()

    def delete_stockpile(self) -> None:
        pile_id = self.current_id()
        if pile_id is None:
            return
        name = self.picker.currentText()
        confirm = QMessageBox.question(
            self, "Delete stockpile",
            f"Delete “{name}” and everything tracked in it?\n\n"
            "Your assets are not touched — only the list of what you wanted.",
        )
        if confirm != QMessageBox.Yes:
            return
        stockpile.delete(self.conn, pile_id)
        self.refresh_filters()

    def add_item(self) -> None:
        pile_id = self.current_id()
        if pile_id is None:
            return
        dialog = AddItemDialog(self)
        if not dialog.exec():
            return
        chosen = dialog.chosen()
        if chosen is None:
            return
        type_id, target = chosen
        stockpile.set_item(self.conn, pile_id, type_id, target)
        self.reload()

    def _on_target_edited(self, type_id: int, target: float) -> None:
        pile_id = self.current_id()
        if pile_id is None:
            return
        stockpile.set_item(self.conn, pile_id, type_id, target)
        self.reload()

    def _context_menu(self, pos) -> None:
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        row = self.model.rows()[self.proxy.mapToSource(index).row()]
        menu = QMenu(self)
        remove = menu.addAction(f"Remove {row['item']}")
        remove.triggered.connect(lambda: self._remove(row["type_id"]))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _remove(self, type_id: int) -> None:
        pile_id = self.current_id()
        if pile_id is None:
            return
        stockpile.remove_item(self.conn, pile_id, type_id)
        self.reload()

    def copy_shopping_list(self) -> None:
        text = stockpile.shopping_list(self.model.rows())
        if not text:
            self.footer.setText("Nothing short — no shopping list to copy.")
            return
        QGuiApplication.clipboard().setText(text)
        lines = len(text.splitlines())
        self.footer.setText(
            f"Copied {lines} line(s). In EVE: open the market, right-click → Multibuy, paste."
        )
