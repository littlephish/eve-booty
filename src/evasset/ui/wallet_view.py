"""Wallet journal and market transactions.

Both tables accumulate locally. ESI only serves about the last 30 days, so a
few weeks after you start syncing, this holds history you can no longer get
back from CCP.
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import db, queries
from .assets_view import _SortProxy
from .async_query import AsyncQuery
from .models import RowTableModel, fill_combo, fmt_isk, fmt_short_isk
from .sort_controller import SortController

RANGES = [
    ("Last 7 days", 7),
    ("Last 30 days", 30),
    ("Last 90 days", 90),
    ("Everything", 0),
]


class _HistoryTable(QWidget):
    """Shared chrome for the two tables: search, owner and date filters, export."""

    def __init__(self, conn, columns, placeholder: str):
        super().__init__()
        self.conn = conn if conn is not None else db.init()

        root = QVBoxLayout(self)
        bar = QHBoxLayout()

        self.search = QLineEdit()
        self.search.setPlaceholderText(placeholder)
        self.search.setClearButtonEnabled(True)
        bar.addWidget(self.search, 3)

        bar.addWidget(QLabel("Owner"))
        self.owner_filter = QComboBox()
        self.owner_filter.setMinimumWidth(160)
        bar.addWidget(self.owner_filter, 1)

        bar.addWidget(QLabel("Range"))
        self.range_filter = QComboBox()
        for label, _ in RANGES:
            self.range_filter.addItem(label)
        self.range_filter.setCurrentIndex(1)
        bar.addWidget(self.range_filter)

        self.extra_label = QLabel("")
        self.extra = QComboBox()
        self.extra.setMinimumWidth(150)
        self.extra_label.setVisible(False)
        self.extra.setVisible(False)
        bar.addWidget(self.extra_label)
        bar.addWidget(self.extra)

        self.export_btn = QPushButton("Export CSV…")
        bar.addWidget(self.export_btn)
        root.addLayout(bar)

        self.columns = columns
        self.model = RowTableModel(columns)
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
        root.addWidget(self.table, 1)
        self.sorter = SortController(self.table, self.proxy, self)

        self.summary = QLabel("")
        root.addWidget(self.summary)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(220)
        self._debounce.timeout.connect(self.reload)
        self.search.textChanged.connect(self._debounce.start)
        for box in (self.owner_filter, self.range_filter, self.extra):
            box.currentIndexChanged.connect(self.reload)
        self.export_btn.clicked.connect(self.export_csv)

        self._query = AsyncQuery(self)
        # A separate AsyncQuery per independent dropdown -- each call to
        # run() on one bumps its generation and discards whatever that same
        # instance had in flight. Sharing one between the owner filter and
        # (in JournalTable) the ref-type filter would mean firing one query
        # silently cancels the other's result rather than just superseding
        # its own previous request.
        self._owner_query = AsyncQuery(self)
        self._sized_once = False

    # ------------------------------------------------------------- filtering
    def refresh_filters(self) -> None:
        def fetch(conn: sqlite3.Connection) -> list[str]:
            return [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM characters WHERE enabled=1 "
                    "UNION SELECT name FROM corporations WHERE name IS NOT NULL ORDER BY 1"
                )
                if r[0]
            ]

        self._owner_query.run(fetch, self._on_owners)

    def _on_owners(self, owners: list[str]) -> None:
        fill_combo(self.owner_filter, owners, "All owners")

    def _date_clause(self, column: str) -> tuple[str, tuple]:
        days = RANGES[self.range_filter.currentIndex()][1]
        if not days:
            return "", ()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return f"{column} >= ?", (cutoff,)

    def _owner_clause(self) -> tuple[str, tuple]:
        if self.owner_filter.currentIndex() <= 0:
            return "", ()
        return "COALESCE(ch.name, co.name) = ?", (self.owner_filter.currentText(),)

    @staticmethod
    def _combine(parts: list[tuple[str, tuple]]) -> tuple[str, tuple]:
        clauses = [c for c, _ in parts if c]
        params: list = []
        for _, p in parts:
            params.extend(p)
        return " AND ".join(clauses), tuple(params)

    def reset_sort(self) -> None:
        self.sorter.reset()

    # ------------------------------------------------------------------ hooks
    def reload(self) -> None:
        raise NotImplementedError

    def export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export", "wallet.csv", "CSV files (*.csv)"
        )
        if not path:
            return
        keys = [k for k, _ in self.columns]
        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow([h for _, h in self.columns])
                for r in self.model.rows():
                    writer.writerow([r[k] for k in keys])
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self.summary.setText(f"Exported {self.model.rowCount():,} rows to {path}")


class JournalTable(_HistoryTable):
    def __init__(self, conn=None, *, defer_load: bool = False):
        super().__init__(
            conn,
            queries.JOURNAL_COLUMNS,
            "Search type, description, reason or counterparty…",
        )
        self.extra_label.setText("Ref type")
        self.extra_label.setVisible(True)
        self.extra.setVisible(True)
        # Its own AsyncQuery, separate from the base class's _owner_query --
        # refresh_filters() fires both the owner and ref-type queries close
        # together, and sharing one instance would mean the second run()
        # call bumps the generation out from under the first, silently
        # discarding its result before it ever reaches the owner dropdown.
        self._ref_type_query = AsyncQuery(self)
        if not defer_load:
            self.first_load()

    def first_load(self) -> None:
        self.refresh_filters()
        self.reload()

    def refresh_filters(self) -> None:
        super().refresh_filters()

        def fetch(conn: sqlite3.Connection) -> list[str]:
            return list(queries.journal_ref_types(conn))

        self._ref_type_query.run(fetch, self._on_ref_types)

    def _on_ref_types(self, refs: list[str]) -> None:
        fill_combo(self.extra, refs, "All ref types")

    def reload(self) -> None:
        parts = [
            queries.journal_search_clause(self.search.text()),
            self._owner_clause(),
            self._date_clause("j.date"),
        ]
        if self.extra.currentIndex() > 0:
            parts.append(("j.ref_type = ?", (self.extra.currentText(),)))
        where, params = self._combine(parts)

        def fetch(conn: sqlite3.Connection) -> list[sqlite3.Row]:
            return queries.fetch_journal(conn, where, params)

        self._query.run(fetch, self._on_rows, self._on_query_failed)

    def _on_rows(self, rows: list[sqlite3.Row]) -> None:
        self.model.set_rows(rows)

        credits = sum(float(r["amount"] or 0) for r in rows if (r["amount"] or 0) > 0)
        debits = sum(float(r["amount"] or 0) for r in rows if (r["amount"] or 0) < 0)
        self.summary.setText(
            f"{len(rows):,} entries · in {fmt_short_isk(credits)} · "
            f"out {fmt_short_isk(abs(debits))} · "
            f"net <b>{fmt_isk(credits + debits)} ISK</b>"
        )
        if not self._sized_once:
            self.table.resizeColumnsToContents()  # once; see AssetsView for why
            self._sized_once = True

    def _on_query_failed(self, message: str) -> None:
        self.summary.setText(f"Query failed: {message}")


class TransactionTable(_HistoryTable):
    def __init__(self, conn=None, *, defer_load: bool = False):
        super().__init__(
            conn,
            queries.TRANSACTION_COLUMNS,
            "Search item, station or counterparty…",
        )
        self.extra_label.setText("Side")
        self.extra_label.setVisible(True)
        self.extra.setVisible(True)
        self.extra.addItems(["Buy and sell", "Buy", "Sell"])
        if not defer_load:
            self.first_load()

    def first_load(self) -> None:
        self.refresh_filters()
        self.reload()

    def reload(self) -> None:
        parts = [
            queries.transaction_search_clause(self.search.text()),
            self._owner_clause(),
            self._date_clause("t.date"),
        ]
        if self.extra.currentIndex() == 1:
            parts.append(("t.is_buy = 1", ()))
        elif self.extra.currentIndex() == 2:
            parts.append(("t.is_buy = 0", ()))
        where, params = self._combine(parts)

        def fetch(conn: sqlite3.Connection) -> tuple[list[sqlite3.Row], dict]:
            rows = queries.fetch_transactions(conn, where, params)
            totals = queries.trade_summary(conn, where, params)
            return rows, totals

        self._query.run(fetch, self._on_rows, self._on_query_failed)

    def _on_rows(self, payload: tuple[list[sqlite3.Row], dict]) -> None:
        rows, totals = payload
        self.model.set_rows(rows)
        self.summary.setText(
            f"{totals['trades']:,} trades · bought {fmt_short_isk(totals['bought'])} · "
            f"sold {fmt_short_isk(totals['sold'])} · "
            f"net <b>{fmt_isk(totals['net'])} ISK</b> "
            "<span style='color:palette(mid)'>(cash in minus cash out, not profit)</span>"
        )
        if not self._sized_once:
            self.table.resizeColumnsToContents()  # once; see AssetsView for why
            self._sized_once = True

    def _on_query_failed(self, message: str) -> None:
        self.summary.setText(f"Query failed: {message}")


class WalletView(QWidget):
    def __init__(
        self,
        conn=None,
        parent: QWidget | None = None,
        *,
        defer_load: bool = False,
    ):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        # Both inner tables are always constructed deferred: this widget has
        # its own two-tab split (Transactions, Journal), and only one of them
        # is ever on screen at a time, so there is no reason to run both
        # queries just because the Wallet tab itself got shown.
        self.journal = JournalTable(conn, defer_load=True)
        self.transactions = TransactionTable(conn, defer_load=True)
        self.tabs.addTab(self.transactions, "Transactions")
        self.tabs.addTab(self.journal, "Journal")
        layout.addWidget(self.tabs)

        self._loaded: set = set()
        self._dirty = {self.journal, self.transactions}
        self.tabs.currentChanged.connect(self._ensure_inner_tab_loaded)

        if not defer_load:
            self.first_load()

    def first_load(self) -> None:
        self._ensure_inner_tab_loaded(self.tabs.currentIndex())

    def _ensure_inner_tab_loaded(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if widget is None:
            return
        if widget not in self._loaded:
            widget.first_load()
            self._loaded.add(widget)
            self._dirty.discard(widget)
        elif widget in self._dirty:
            widget.refresh_filters()
            widget.reload()
            self._dirty.discard(widget)

    def reload(self) -> None:
        """Refresh whichever inner tab is on screen now; the other one picks
        up the change lazily the next time someone switches to it -- same
        reasoning as MainWindow's tab-level laziness, one level down."""
        self._dirty = {self.journal, self.transactions}
        self._ensure_inner_tab_loaded(self.tabs.currentIndex())

    def reset_sort(self) -> None:
        """Only the visible inner table's arrow/order is meaningful to a user
        clicking Reset sort right now; reset it, not the hidden one."""
        widget = self.tabs.currentWidget()
        if widget is not None:
            widget.reset_sort()
