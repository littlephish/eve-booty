"""Asset browser: a searchable flat table across every character and corp,
plus a rollup tab that answers "where is my stuff and what is it worth"."""

from __future__ import annotations

import csv
import sqlite3

from PySide6.QtCore import QSortFilterProxyModel, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .. import queries
from ..config import ASSET_SAFETY_LOCATION_ID
from .async_query import AsyncQuery
from .debounce import Debounce
from .fit_dialog import FitDialog
from .models import RowTableModel, fill_combo, fmt_isk, fmt_short_isk
from .palette import SECONDARY_TEXT
from .sort_controller import SortController

# "View fit" is offered for rows in this SDE category. Scoped to just Ship --
# what a player structure's category is named in the SDE was not checked, so
# it is not being guessed at here.
_FIT_VIEWABLE_CATEGORIES = {"Ship"}


class _SortProxy(QSortFilterProxyModel):
    """Sort on the raw value, not the formatted string, so 1,000,000 does not
    sort before 9.

    That used to be a hand-written lessThan() calling back into
    RowTableModel.data() for both sides of every comparison. Qt's own default
    lessThan does exactly the same thing -- compare whatever sortRole()
    returns -- but in C++, and RowTableModel.data(Qt.UserRole) already hands
    back plain, never-None floats/ints/strings (see RowTableModel.data), so
    there is nothing left for a Python override to add. On a table with tens
    of thousands of rows the extra Python stack frame and try/except on every
    single comparison was the difference between a sort that finishes and one
    that reads as a hung app. Setting sortRole is enough on its own."""


class AssetsView(QWidget):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        defer_load: bool = False,
    ):
        super().__init__(parent)

        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(
            "Search item, group, container name, station, system, region or owner…"
        )
        self.search.setClearButtonEnabled(True)
        bar.addWidget(self.search, 3)

        self.owner_filter = QComboBox()
        self.owner_filter.setMinimumWidth(160)
        bar.addWidget(QLabel("Owner"))
        bar.addWidget(self.owner_filter, 1)

        self.category_filter = QComboBox()
        self.category_filter.setMinimumWidth(150)
        bar.addWidget(QLabel("Category"))
        bar.addWidget(self.category_filter, 1)

        self.region_filter = QComboBox()
        self.region_filter.setMinimumWidth(150)
        bar.addWidget(QLabel("Region"))
        bar.addWidget(self.region_filter, 1)

        self.export_btn = QPushButton("Export CSV…")
        bar.addWidget(self.export_btn)
        root.addLayout(bar)

        bar2 = QHBoxLayout()
        self.hide_fitted = QCheckBox("Hide items on a ship (fitted/cargo/drones)")
        self.hide_fitted.setToolTip(
            "Hides anything whose direct container is a ship -- fitted modules, "
            "cargo, drone bay, fleet hangar, any hold -- so what is left is "
            "loose in a station or structure hangar."
        )
        bar2.addWidget(self.hide_fitted)

        self.safety_only = QCheckBox("Asset Safety only")
        self.safety_only.setToolTip(
            "Shows only items CCP repackaged into Asset Safety after an "
            "eviction or pod loss."
        )
        bar2.addWidget(self.safety_only)
        bar2.addStretch(1)

        self.chip_label = QLabel("")
        self.chip_label.setStyleSheet(f"color: {SECONDARY_TEXT};")
        self.chip_label.setVisible(False)
        bar2.addWidget(self.chip_label)
        self.chip_clear = QPushButton("Clear")
        self.chip_clear.setVisible(False)
        self.chip_clear.clicked.connect(self.clear_extra_filter)
        bar2.addWidget(self.chip_clear)
        root.addLayout(bar2)

        self._extra_filter: tuple[str, str] | None = None

        self.model = RowTableModel(queries.ASSET_COLUMNS)
        self.proxy = _SortProxy()
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(Qt.UserRole)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        root.addWidget(self.table, 1)
        self.sorter = SortController(self.table, self.proxy, self)

        self.summary = QLabel("")
        root.addWidget(self.summary)

        self._debounce = Debounce(self, self.reload)
        self.search.textChanged.connect(self._debounce.trigger)
        for box in (self.owner_filter, self.category_filter, self.region_filter):
            box.currentIndexChanged.connect(self.reload)
        self.hide_fitted.toggled.connect(self.reload)
        self.safety_only.toggled.connect(self.reload)
        self.export_btn.clicked.connect(self.export_csv)

        self._query = AsyncQuery(self)
        self._filter_query = AsyncQuery(self)
        self._sized_once = False

        if not defer_load:
            self.first_load()

    def reset_sort(self) -> None:
        self.sorter.reset()

    def first_load(self) -> None:
        """Kick off the first real query.

        Split out from __init__ so MainWindow can skip this for every tab
        that is not on screen yet -- with five tabs each running their own
        multi-join query, doing all of that before the window even paints is
        most of what "opening the app is slow" meant. Each tab now only pays
        this cost the first time it is actually looked at, and even then it
        no longer blocks anything (see reload()).
        """
        self.refresh_filters()
        self.reload()

    # ---------------------------------------------------------------- filters
    def refresh_filters(self) -> None:
        # The category and region lists are DISTINCT over a join against the
        # full assets table -- there is no index that lets SQLite shortcut
        # that, so it is a real scan-and-join over everything you own, not a
        # cheap lookup. This ran inline on the GUI thread until now, which is
        # exactly what made the window freeze on every launch (first_load()
        # calls this before reload() even starts) and on every character
        # add/sync (_after_data_change() calls it again). Same fix as
        # reload(): run it in the background, apply whichever result is still
        # current when it lands.
        owner_sql = (
            "SELECT name FROM characters WHERE enabled=1 "
            "UNION SELECT name FROM corporations WHERE name IS NOT NULL ORDER BY 1"
        )
        category_sql = (
            "SELECT DISTINCT c.name FROM assets a JOIN sde_types t USING(type_id) "
            "JOIN sde_groups g USING(group_id) JOIN sde_categories c USING(category_id) "
            "ORDER BY 1"
        )
        region_sql = "SELECT DISTINCT r.name FROM assets a JOIN sde_regions r USING(region_id) ORDER BY 1"

        def fetch(conn: sqlite3.Connection) -> tuple[list[str], list[str], list[str]]:
            owners = [r[0] for r in conn.execute(owner_sql) if r[0]]
            categories = [r[0] for r in conn.execute(category_sql) if r[0]]
            regions = [r[0] for r in conn.execute(region_sql) if r[0]]
            return owners, categories, regions

        self._filter_query.run(fetch, self._on_filters)

    def _on_filters(self, payload: tuple[list[str], list[str], list[str]]) -> None:
        owners, categories, regions = payload
        fill_combo(self.owner_filter, owners, "All owners")
        fill_combo(self.category_filter, categories, "All categories")
        fill_combo(self.region_filter, regions, "All regions")

    def _where(self) -> tuple[str, tuple]:
        clauses, params = [], []
        text_clause, text_params = queries.search_clause(self.search.text())
        if text_clause:
            clauses.append(text_clause)
            params.extend(text_params)
        if self.owner_filter.currentIndex() > 0:
            clauses.append("COALESCE(ch.name, co.name) = ?")
            params.append(self.owner_filter.currentText())
        if self.category_filter.currentIndex() > 0:
            clauses.append("cat.name = ?")
            params.append(self.category_filter.currentText())
        if self.region_filter.currentIndex() > 0:
            clauses.append("reg.name = ?")
            params.append(self.region_filter.currentText())
        if self.hide_fitted.isChecked():
            clauses.append(queries.HIDE_SHIP_CONTENTS_CLAUSE)
        if self.safety_only.isChecked():
            clauses.append(f"a.root_location_id = {ASSET_SAFETY_LOCATION_ID}")
        if self._extra_filter is not None:
            level, value = self._extra_filter
            clauses.append(f"{queries.OVERVIEW_FILTER_EXPR[level]} = ?")
            params.append(value)
        return " AND ".join(clauses), tuple(params)

    # ------------------------------------------------------- cross-tab filter
    def apply_external_filter(self, level: str, value: str) -> None:
        """Entry point for OverviewView's right-click "Filter Assets to
        this" -- see MainWindow._filter_assets_from_overview()."""
        self._extra_filter = (level, value)
        label = {key: display for display, key in OverviewView.LEVELS}.get(level, level.title())
        self.chip_label.setText(f"{label}: {value}")
        self.chip_label.setVisible(True)
        self.chip_clear.setVisible(True)
        self.reload()

    def clear_extra_filter(self) -> None:
        self._extra_filter = None
        self.chip_label.setVisible(False)
        self.chip_clear.setVisible(False)
        self.reload()

    # ----------------------------------------------------------------- reload
    def reload(self) -> None:
        where, params = self._where()

        def fetch(conn: sqlite3.Connection) -> list[sqlite3.Row]:
            return queries.fetch_assets(conn, where, params)

        self._query.run(fetch, self._on_rows, self._on_query_failed)

    def _on_rows(self, rows: list[sqlite3.Row]) -> None:
        self.model.set_rows(rows)
        self._update_summary()
        if not self._sized_once:
            # Column widths are sized once, off whatever the first load looks
            # like, then left alone -- resizeColumnsToContents() measures
            # every cell in every row, and running it after every reload used
            # to be most of the freeze on its own. The columns stay
            # Interactive (see the table setup above), so dragging one wider
            # by hand still works same as always.
            self.table.resizeColumnsToContents()
            self._sized_once = True

    def _on_query_failed(self, message: str) -> None:
        self.summary.setText(f"Query failed: {message}")

    # ------------------------------------------------------------- context menu
    def _show_context_menu(self, pos) -> None:
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        source_row = self.proxy.mapToSource(index).row()
        # model.rows() holds the full sqlite3.Row per row, not just the
        # columns actually displayed -- item_id and category ride along even
        # though neither is a visible column (see queries.ASSET_COLUMNS).
        row = self.model.rows()[source_row]

        menu = QMenu(self)
        if (row["category"] or "") in _FIT_VIEWABLE_CATEGORIES:
            action = menu.addAction("View fit…")
            action.triggered.connect(lambda: self._open_fit_dialog(row))
        if row["root_location_id"] == ASSET_SAFETY_LOCATION_ID and not self.safety_only.isChecked():
            action = menu.addAction("Show only Asset Safety")
            action.triggered.connect(lambda: self.safety_only.setChecked(True))
        if not menu.actions():
            return
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _open_fit_dialog(self, row: sqlite3.Row) -> None:
        name = row["custom_name"] or row["item"]
        dialog = FitDialog(row["item_id"], name, ship_type_id=row["type_id"], parent=self)
        dialog.exec()

    def _update_summary(self) -> None:
        buy = self.model.column_sum("buy_value")
        sell = self.model.column_sum("sell_value")
        volume = self.model.column_sum("volume")
        units = self.model.column_sum("quantity")
        rows = self.model.rowCount()
        unpriced = sum(1 for r in self.model.rows() if (r["price_source"] or "none") == "none")
        note = f" · {unpriced} unpriced" if unpriced else ""
        spread = f" · spread {100 * (sell - buy) / sell:.1f}%" if sell else ""
        self.summary.setText(
            f"{rows:,} stacks · {units:,.0f} units · "
            f"buy <b>{fmt_short_isk(buy)}</b> · sell <b>{fmt_short_isk(sell)}</b> "
            f"({fmt_isk(sell)} ISK){spread} · {volume:,.0f} m³{note}"
        )

    # ----------------------------------------------------------------- export
    def export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export assets", "assets.csv", "CSV files (*.csv)"
        )
        if not path:
            return
        keys = [k for k, _ in queries.ASSET_COLUMNS]
        headers = [h for _, h in queries.ASSET_COLUMNS]
        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(headers)
                for r in self.model.rows():
                    writer.writerow([r[k] for k in keys])
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self.summary.setText(f"Exported {self.model.rowCount():,} rows to {path}")


class OverviewView(QWidget):
    """Same assets, rolled up. Answers "which station is holding 40b of my
    stuff" without scrolling a 20,000 row table."""

    # Shared with the Treemap tab; see queries.ROLLUP_LEVELS.
    LEVELS = queries.ROLLUP_LEVELS

    # (level_key, value) -- picked up by MainWindow and handed to
    # AssetsView.apply_external_filter() so a right-click here can switch to
    # and filter the Assets tab. Kept as a signal rather than importing
    # AssetsView directly so this view does not need to know who is
    # listening.
    filter_assets_requested = Signal(str, str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        defer_load: bool = False,
    ):
        super().__init__(parent)

        root = QVBoxLayout(self)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Group by"))
        self.level = QComboBox()
        for label, _ in self.LEVELS:
            self.level.addItem(label)
        bar.addWidget(self.level)
        bar.addStretch(1)
        self.summary = QLabel("")
        bar.addWidget(self.summary)
        root.addLayout(bar)

        self.model = RowTableModel(queries.OVERVIEW_COLUMNS)
        self.proxy = _SortProxy()
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(Qt.UserRole)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        root.addWidget(self.table, 1)
        self.sorter = SortController(self.table, self.proxy, self)

        self.level.currentIndexChanged.connect(self.reload)
        self._query = AsyncQuery(self)
        self._sized_once = False
        if not defer_load:
            self.first_load()

    def reset_sort(self) -> None:
        self.sorter.reset()

    def first_load(self) -> None:
        self.reload()

    def reload(self) -> None:
        level = self.LEVELS[self.level.currentIndex()][1]

        def fetch(conn: sqlite3.Connection) -> list[sqlite3.Row]:
            return queries.location_totals(conn, level)

        self._query.run(fetch, self._on_rows, self._on_query_failed)

    def _on_rows(self, rows: list[sqlite3.Row]) -> None:
        self.model.set_rows(rows)
        buy = self.model.column_sum("buy_value")
        sell = self.model.column_sum("sell_value")
        self.summary.setText(
            f"{len(rows):,} groups · buy <b>{fmt_short_isk(buy)}</b> · "
            f"sell <b>{fmt_short_isk(sell)}</b> ({fmt_isk(sell)} ISK)"
        )
        if not self._sized_once:
            self.table.resizeColumnsToContents()  # once; see AssetsView for why
            self._sized_once = True

    def _on_query_failed(self, message: str) -> None:
        self.summary.setText(f"Query failed: {message}")

    def _show_context_menu(self, pos) -> None:
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        source_row = self.proxy.mapToSource(index).row()
        label = self.model.rows()[source_row]["label"]
        _display, level = self.LEVELS[self.level.currentIndex()]

        menu = QMenu(self)
        action = menu.addAction(f'Filter Assets to "{label}"')
        action.triggered.connect(lambda: self.filter_assets_requested.emit(level, label))
        menu.exec(self.table.viewport().mapToGlobal(pos))
