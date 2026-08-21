"""Net worth over time, per character or across the account."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from PySide6.QtCharts import QChart, QChartView, QDateTimeAxis, QLineSeries, QValueAxis
from PySide6.QtCore import QDateTime, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import db, networth
from .async_query import AsyncQuery
from .models import fmt_isk, fmt_short_isk
from .palette import delta_hex

RANGES = [
    ("Last 30 days", 30),
    ("Last 90 days", 90),
    ("Last year", 365),
    ("All time", 0),
]

# Basis affects the item-valued buckets only. Wallet, sell orders and escrow
# are already ISK, so they are the same on both.
BASES = [
    ("Jita sell", "sell"),
    ("Jita buy", "buy"),
    ("Both totals", "both"),
]


def series_keys(basis: str) -> list[tuple[str, str]]:
    if basis == "both":
        return [
            ("total_sell_isk", "Total at Jita sell"),
            ("total_buy_isk", "Total at Jita buy"),
            ("wallet_isk", "Wallet"),
        ]
    return [
        (f"total_{basis}_isk", "Total"),
        (f"assets_{basis}_isk", "Assets"),
        ("wallet_isk", "Wallet"),
        ("orders_isk", "Sell orders"),
        ("escrow_isk", "Buy escrow"),
        (f"contracts_{basis}_isk", "Contracts"),
        (f"jobs_{basis}_isk", "In production"),
    ]


TABLE_COLUMNS = [
    ("total_sell_isk", "Total (sell)"),
    ("total_buy_isk", "Total (buy)"),
    ("assets_sell_isk", "Assets (sell)"),
    ("assets_buy_isk", "Assets (buy)"),
    ("wallet_isk", "Wallet"),
    ("orders_isk", "Sell orders"),
    ("escrow_isk", "Buy escrow"),
    ("contracts_sell_isk", "Contracts"),
    ("jobs_sell_isk", "In production"),
]


class NetWorthView(QWidget):
    def __init__(
        self,
        conn=None,
        parent: QWidget | None = None,
        *,
        defer_load: bool = False,
    ):
        super().__init__(parent)
        self.conn = conn if conn is not None else db.init()

        root = QVBoxLayout(self)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Owner"))
        self.owner_box = QComboBox()
        self.owner_box.setMinimumWidth(240)
        controls.addWidget(self.owner_box)

        controls.addWidget(QLabel("Range"))
        self.range_box = QComboBox()
        for label, _ in RANGES:
            self.range_box.addItem(label)
        self.range_box.setCurrentIndex(1)
        controls.addWidget(self.range_box)

        controls.addWidget(QLabel("Value at"))
        self.basis_box = QComboBox()
        for label, _ in BASES:
            self.basis_box.addItem(label)
        self.basis_box.setToolTip(
            "Jita sell is what you get listing everything and waiting. "
            "Jita buy is what you get dumping it into standing orders today."
        )
        controls.addWidget(self.basis_box)

        self.breakdown = QCheckBox("Show breakdown")
        self.breakdown.setChecked(False)
        controls.addWidget(self.breakdown)
        controls.addStretch(1)

        self.headline = QLabel("")
        self.headline.setStyleSheet("font-size: 15px;")
        controls.addWidget(self.headline)
        root.addLayout(controls)

        self.chart = QChart()
        self.chart.setAnimationOptions(QChart.NoAnimation)
        self.chart.legend().setAlignment(Qt.AlignBottom)
        self.chart_view = QChartView(self.chart)
        self.chart_view.setRenderHint(QPainter.Antialiasing)
        self.chart_view.setMinimumHeight(280)
        root.addWidget(self.chart_view, 3)

        self.table = QTableWidget(0, len(TABLE_COLUMNS) + 2)
        self.table.setHorizontalHeaderLabels(
            ["Owner", "Snapshot"] + [label for _, label in TABLE_COLUMNS]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        root.addWidget(self.table, 2)

        self.owner_box.currentIndexChanged.connect(self.redraw)
        self.range_box.currentIndexChanged.connect(self.redraw)
        self.basis_box.currentIndexChanged.connect(self.redraw)
        self.breakdown.stateChanged.connect(self.redraw)

        # Three independent queries -- owner list, chart history, latest-per-
        # owner table -- each get their own AsyncQuery. One shared instance
        # would mean firing one (e.g. the table refresh at the end of
        # redraw()) bumps the generation out from under whichever one is
        # still in flight, silently dropping its result before it renders.
        self._owners_query = AsyncQuery(self)
        self._history_query = AsyncQuery(self)
        self._table_query = AsyncQuery(self)

        if not defer_load:
            self.first_load()

    def first_load(self) -> None:
        self.refresh()

    # ------------------------------------------------------------------ data
    def refresh(self) -> None:
        def fetch(conn) -> list[tuple]:
            return list(networth.owners_with_history(conn))

        self._owners_query.run(fetch, self._on_owners)

    def _on_owners(self, owners: list[tuple]) -> None:
        current = self.owner_box.currentData()
        self.owner_box.blockSignals(True)
        self.owner_box.clear()
        self.owner_box.addItem("Everything", None)
        for owner_type, owner_id, name in owners:
            self.owner_box.addItem(name, (owner_type, owner_id))
        if current is not None:
            idx = self.owner_box.findData(current)
            if idx >= 0:
                self.owner_box.setCurrentIndex(idx)
        self.owner_box.blockSignals(False)
        self.redraw()

    # ----------------------------------------------------------------- chart
    def redraw(self) -> None:
        owner = self.owner_box.currentData()
        days = RANGES[self.range_box.currentIndex()][1]

        def fetch(conn) -> list:
            rows = networth.history(conn) if owner is None else networth.history(conn, owner[0], owner[1])
            if days:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
                rows = [r for r in rows if r["taken_at"] >= cutoff]
            return rows

        self._history_query.run(fetch, self._render_chart)

    def _render_chart(self, rows: list) -> None:
        # Rebuild the chart wholesale. Reusing one and swapping series/axes
        # leaves stale axes behind, which shows up as doubled-up tick labels.
        self.chart = QChart()
        self.chart.setAnimationOptions(QChart.NoAnimation)
        self.chart.legend().setAlignment(Qt.AlignBottom)
        self.chart_view.setChart(self.chart)

        if not rows:
            self.chart.setTitle("No snapshots yet — run a sync to record the first one.")
            self.headline.setText("")
            self.table.setRowCount(0)
            return

        basis = BASES[self.basis_box.currentIndex()][1]
        keys = series_keys(basis)
        if basis == "both":
            # The point of this mode is the gap between the two totals, so the
            # breakdown checkbox does not apply.
            keys = keys[:2] if not self.breakdown.isChecked() else keys
        elif not self.breakdown.isChecked():
            keys = keys[:1]

        peak = max((abs(float(r[key] or 0)) for r in rows for key, _ in keys), default=0.0)
        scale, unit = _axis_scale(peak)

        y_min, y_max = 0.0, 0.0
        for key, label in keys:
            series = QLineSeries()
            series.setName(label)
            for r in rows:
                val = float(r[key] or 0) / scale
                series.append(_to_msecs(r["taken_at"]), val)
                y_max = max(y_max, val)
                y_min = min(y_min, val)
            self.chart.addSeries(series)

        axis_x = QDateTimeAxis()
        axis_x.setFormat("dd MMM")
        axis_x.setTickCount(min(max(len(rows), 2), 8))
        self.chart.addAxis(axis_x, Qt.AlignBottom)

        axis_y = QValueAxis()
        axis_y.setLabelFormat("%.2f")
        pad = (y_max - y_min) * 0.08 or max(abs(y_max) * 0.08, 1.0)
        axis_y.setRange(min(0.0, y_min - pad), y_max + pad)
        axis_y.setTitleText(f"ISK ({unit})" if unit else "ISK")
        self.chart.addAxis(axis_y, Qt.AlignLeft)

        for series in self.chart.series():
            series.attachAxis(axis_x)
            series.attachAxis(axis_y)

        headline_key = "total_buy_isk" if basis == "buy" else "total_sell_isk"
        first = float(rows[0][headline_key] or 0)
        last = float(rows[-1][headline_key] or 0)
        delta = last - first
        sign = "+" if delta >= 0 else "−"
        pct = f" ({delta / first * 100:+.1f}%)" if first else ""
        self.chart.setTitle(f"{self.owner_box.currentText()} — {len(rows)} snapshot(s)")

        other_key = "total_sell_isk" if headline_key == "total_buy_isk" else "total_buy_isk"
        other = float(rows[-1][other_key] or 0)
        aside = (
            f" &nbsp;<span style='color:palette(mid)'>"
            f"({'buy' if other_key.startswith('total_buy') else 'sell'} "
            f"{fmt_short_isk(other)})</span>"
            if other
            else ""
        )
        self.headline.setText(
            f"<b>{fmt_short_isk(last)} ISK</b> &nbsp; "
            f"<span style='color:{delta_hex(delta >= 0)}'>"
            f"{sign}{fmt_short_isk(abs(delta))}{pct}</span>{aside}"
        )
        self._fill_table()

    def _fill_table(self) -> None:
        """Latest snapshot per owner, so you can see who holds what."""

        def fetch(conn) -> list:
            return list(networth.latest_per_owner(conn))

        self._table_query.run(fetch, self._render_table)

    def _render_table(self, rows: list) -> None:
        self.table.setRowCount(len(rows) + (1 if rows else 0))
        totals = dict.fromkeys([k for k, _ in TABLE_COLUMNS], 0.0)
        for i, r in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(r["owner"]))
            self.table.setItem(
                i, 1, QTableWidgetItem((r["taken_at"] or "")[:16].replace("T", " "))
            )
            for j, (key, _) in enumerate(TABLE_COLUMNS):
                val = float(r[key] or 0)
                totals[key] += val
                item = QTableWidgetItem(fmt_isk(val))
                item.setTextAlignment(int(Qt.AlignRight | Qt.AlignVCenter))
                self.table.setItem(i, j + 2, item)
        if rows:
            last = len(rows)
            bold = QTableWidgetItem("All owners")
            font = bold.font()
            font.setBold(True)
            bold.setFont(font)
            self.table.setItem(last, 0, bold)
            self.table.setItem(last, 1, QTableWidgetItem(""))
            for j, (key, _) in enumerate(TABLE_COLUMNS):
                item = QTableWidgetItem(fmt_isk(totals[key]))
                item.setFont(font)
                item.setTextAlignment(int(Qt.AlignRight | Qt.AlignVCenter))
                self.table.setItem(last, j + 2, item)


def _axis_scale(peak: float) -> tuple[float, str]:
    """Raw ISK on an axis label is unreadable at 22,210,378,000. Scale the
    whole series and say so in the axis title instead."""
    for cutoff, unit in ((1e12, "trillions"), (1e9, "billions"), (1e6, "millions"), (1e3, "thousands")):
        if peak >= cutoff:
            return cutoff, unit
    return 1.0, ""


def _to_msecs(iso: str) -> float:
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return QDateTime.fromSecsSinceEpoch(int(dt.timestamp())).toMSecsSinceEpoch()
