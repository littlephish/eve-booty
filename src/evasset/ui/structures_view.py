"""Structures our corporations own: fuel, reinforcement timers, moon drills.

Everything here is quoted in EVE time, which is UTC, because every timer in
the game is. A structure comes out of reinforcement at a wall-clock time that
CCP states in UTC and that fleets form up on in UTC; rendering it in the
viewer's local zone would mean everyone converting it back by hand, and
getting that wrong is how a Fortizar dies.

Each deadline column shows the absolute time and the time remaining together.
Absolute alone makes you do the arithmetic; remaining alone is useless for
arranging to be there.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer
from PySide6.QtGui import QBrush, QColor
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
    QVBoxLayout,
    QWidget,
)

from .. import db, queries
from .assets_view import _SortProxy
from .async_query import AsyncQuery
from .models import fill_combo
from .sort_controller import SortController

# How close to empty before a fuel bay is worth shouting about. Three days is
# roughly "you can still fix this at the weekend"; one day is "today".
FUEL_WARN = timedelta(days=3)
FUEL_CRITICAL = timedelta(days=1)

NORMAL, WARN, CRITICAL = 0, 1, 2

# Reinforcement states worth colouring. The rest of the enum is either normal
# operation or a state nothing can be done about.
REINFORCED_STATES = {"armor_reinforce", "hull_reinforce"}
VULNERABLE_STATES = {
    "anchor_vulnerable", "armor_vulnerable", "deploy_vulnerable",
    "hull_vulnerable", "onlining_vulnerable", "shield_vulnerable",
}


# --------------------------------------------------------------- formatting
def parse_utc(value) -> datetime | None:
    """ESI hands back ISO 8601 with a Z. Python did not accept Z in
    fromisoformat until 3.11 and this project supports 3.10."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fmt_eve(when: datetime | None) -> str:
    return "" if when is None else when.strftime("%Y-%m-%d %H:%M")


def fmt_remaining(delta: timedelta) -> str:
    """Coarse on purpose. Nobody schedules a fleet off the seconds column, and
    a value that changes every second is a value you cannot read."""
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "passed"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return "now"


def fmt_deadline(value, now: datetime | None = None) -> str:
    when = parse_utc(value)
    if when is None:
        return ""
    now = now or datetime.now(timezone.utc)
    return f"{fmt_eve(when)}  ·  {fmt_remaining(when - now)}"


def sort_key(value) -> float:
    """Sort deadline columns chronologically. Empty sorts last rather than
    first -- a structure with no timer is not the most urgent thing on screen."""
    when = parse_utc(value)
    return float("inf") if when is None else when.timestamp()


def state_label(state) -> str:
    if not state:
        return ""
    return str(state).replace("_", " ").capitalize()


def state_severity(state) -> int:
    if state in REINFORCED_STATES:
        return CRITICAL
    if state in VULNERABLE_STATES:
        return WARN
    return NORMAL


def fuel_severity(value, now: datetime | None = None) -> int:
    when = parse_utc(value)
    if when is None:
        return NORMAL
    left = when - (now or datetime.now(timezone.utc))
    if left <= FUEL_CRITICAL:
        return CRITICAL
    if left <= FUEL_WARN:
        return WARN
    return NORMAL


def fmt_vuln_window(reinforce_hour, next_hour=None, next_apply=None) -> str:
    """ESI gives the hour vulnerability starts, not a span. Showing it as an
    hour range would be inventing a duration that varies by structure class."""
    if reinforce_hour is None:
        return ""
    text = f"{int(reinforce_hour):02d}:00"
    if next_hour is not None and next_hour != reinforce_hour:
        applies = parse_utc(next_apply)
        when = f" on {fmt_eve(applies)}" if applies else ""
        text += f"  →  {int(next_hour):02d}:00{when}"
    return text


def fmt_services(raw) -> str:
    try:
        services = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return ""
    if not services:
        return ""
    online = sum(1 for s in services if s.get("state") == "online")
    offline = [s.get("name", "?") for s in services if s.get("state") == "offline"]
    text = f"{online} online"
    if offline:
        text += f" · {len(offline)} offline"
    return text


def services_severity(raw) -> int:
    try:
        services = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return NORMAL
    return WARN if any(s.get("state") == "offline" for s in services) else NORMAL


# -------------------------------------------------------------------- model
class _StructuresModel(QAbstractTableModel):
    """Columns here are derived, not raw table columns, so this formats and
    sorts them itself rather than going through RowTableModel."""

    COLUMNS = [
        ("name", "Structure"),
        ("type_name", "Type"),
        ("system_name", "System"),
        ("region_name", "Region"),
        ("state", "State"),
        ("state_timer_end", "Timer"),
        ("fuel_expires", "Fuel expires"),
        ("reinforce_hour", "Vuln (EVE)"),
        ("chunk_arrival_time", "Next chunk"),
        ("services", "Services"),
    ]
    DEADLINES = {"state_timer_end", "fuel_expires", "chunk_arrival_time"}

    def __init__(self, rows=None):
        super().__init__()
        self._keys = [k for k, _ in self.COLUMNS]
        self._rows: list = []
        self.set_rows(rows or [])

    def set_rows(self, rows) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def rows(self) -> list:
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

    def display(self, row, key: str) -> str:
        if key in self.DEADLINES:
            return fmt_deadline(row[key])
        if key == "state":
            return state_label(row["state"])
        if key == "reinforce_hour":
            return fmt_vuln_window(
                row["reinforce_hour"], row["next_reinforce_hour"], row["next_reinforce_apply"]
            )
        if key == "services":
            return fmt_services(row["services"])
        value = row[key]
        return "" if value is None else str(value)

    def severity(self, row) -> int:
        return max(
            state_severity(row["state"]),
            fuel_severity(row["fuel_expires"]),
            services_severity(row["services"]),
        )

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        key = self._keys[index.column()]

        if role == Qt.DisplayRole:
            return self.display(row, key)

        if role == Qt.UserRole:            # what the sort proxy compares
            if key in self.DEADLINES:
                return sort_key(row[key])
            if key == "reinforce_hour":
                hour = row["reinforce_hour"]
                return -1 if hour is None else int(hour)
            value = row[key]
            return "" if value is None else str(value)

        # Colour reinforces what the text already says -- an empty fuel bay
        # reads as "passed", a reinforced structure says so in the State
        # column -- rather than being the only thing carrying the meaning.
        if role == Qt.ForegroundRole:
            level = NORMAL
            if key == "state":
                level = state_severity(row["state"])
            elif key == "fuel_expires":
                level = fuel_severity(row["fuel_expires"])
            elif key == "services":
                level = services_severity(row["services"])
            if level == CRITICAL:
                return QBrush(QColor("#c0392b"))
            if level == WARN:
                return QBrush(QColor("#b9770e"))

        if role == Qt.ToolTipRole:
            return self.tooltip(row, key)
        return None

    def tooltip(self, row, key: str) -> str | None:
        if key == "fuel_expires" and row["fuel_expires"]:
            return "Fuel runs out " + fmt_eve(parse_utc(row["fuel_expires"])) + " EVE time"
        if key == "state" and row["state_timer_end"]:
            return "Timer ends " + fmt_eve(parse_utc(row["state_timer_end"])) + " EVE time"
        if key == "services":
            return fmt_services(row["services"]) or "No services reported"
        if key == "chunk_arrival_time" and row["natural_decay_time"]:
            return (
                "Chunk auto-fractures "
                + fmt_eve(parse_utc(row["natural_decay_time"]))
                + " EVE time"
            )
        return None


# --------------------------------------------------------------------- view
class StructuresView(QWidget):
    def __init__(
        self,
        conn: sqlite3.Connection | None = None,
        parent: QWidget | None = None,
        *,
        defer_load: bool = False,
    ):
        super().__init__(parent)
        self.conn = conn if conn is not None else db.init()
        self._query = AsyncQuery(self)

        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search structure, system or region")
        self.search.setClearButtonEnabled(True)
        bar.addWidget(self.search, 3)

        bar.addWidget(QLabel("Owner"))
        self.owner = QComboBox()
        bar.addWidget(self.owner, 1)

        bar.addWidget(QLabel("Show"))
        self.attention = QComboBox()
        self.attention.addItems(["Everything", "Needs attention"])
        self.attention.setToolTip(
            "Needs attention: reinforced, vulnerable, low on fuel, or a service offline"
        )
        bar.addWidget(self.attention, 1)

        self.export_btn = QPushButton("Export CSV...")
        bar.addWidget(self.export_btn)
        root.addLayout(bar)

        self.table = QTableView()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.model = _StructuresModel()
        self.proxy = _SortProxy()
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(Qt.UserRole)
        self.table.setModel(self.proxy)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.sorter = SortController(self.table, self.proxy, self)
        root.addWidget(self.table, 1)

        # ESI only returns corp structures to a character who holds the role,
        # so "empty" is far more often a permissions answer than an absence of
        # structures. Saying so beats an empty grid that reads as broken.
        self.empty = QLabel(
            "No corporation structures.\n\n"
            "This tab lists structures owned by a corporation you have linked. "
            "Tick \"Corp data\" for a character in File -> Characters..., and note "
            "that ESI only returns structures to a character holding the in-game "
            "role -- without it this stays empty even though everything else syncs."
        )
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet("color: palette(mid);")
        self.empty.setVisible(False)   # until a query says the list is empty
        root.addWidget(self.empty)

        self.footer = QLabel("")
        self.footer.setStyleSheet("color: palette(mid);")
        root.addWidget(self.footer)

        self.search.textChanged.connect(self.reload)
        self.owner.currentIndexChanged.connect(self.reload)
        self.attention.currentIndexChanged.connect(self.reload)
        self.export_btn.clicked.connect(self.export_csv)

        # Every deadline column is a countdown, so the table goes stale just by
        # being looked at. A minute is enough: nothing is shown finer than
        # minutes, and repainting more often would only burn cycles.
        self._tick = QTimer(self)
        self._tick.setInterval(60_000)
        self._tick.timeout.connect(self._repaint_times)
        self._tick.start()

        if not defer_load:
            self.first_load()

    # ----------------------------------------------------------------- data
    def reset_sort(self) -> None:
        self.sorter.reset()

    def first_load(self) -> None:
        self.refresh_filters()
        self.reload()

    def refresh_filters(self) -> None:
        self._query.run(
            queries.structure_owners,
            lambda owners: fill_combo(self.owner, owners, "All owners"),
        )

    def reload(self) -> None:
        needle = self.search.text().strip().lower()
        owner = self.owner.currentText()
        only_attention = self.attention.currentIndex() == 1

        def render(rows):
            keep = [
                row for row in rows
                if self._matches(row, needle, owner, only_attention)
            ]
            self.model.set_rows(keep)
            self._render_footer(keep, len(rows))

        self._query.run(queries.fetch_structures, render)

    def _matches(self, row, needle: str, owner: str, only_attention: bool) -> bool:
        if owner and not owner.startswith("All") and row["owner_name"] != owner:
            return False
        if needle:
            haystack = " ".join(
                str(row[k] or "").lower()
                for k in ("name", "system_name", "region_name", "type_name")
            )
            if needle not in haystack:
                return False
        if only_attention and self.model.severity(row) == NORMAL:
            return False
        return True

    def _render_footer(self, shown, total) -> None:
        has_any = total > 0
        self.empty.setVisible(not has_any)
        self.table.setVisible(has_any)
        if not has_any:
            self.footer.setText("")
            return
        attention = sum(1 for r in shown if self.model.severity(r) != NORMAL)
        text = f"{len(shown)} of {total} structure(s)"
        if attention:
            text += f" - {attention} need attention"
        self.footer.setText(text + " - times are EVE time (UTC)")

    def _repaint_times(self) -> None:
        """Countdowns move on their own; the rows behind them have not changed,
        so this repaints what is on screen rather than re-running the query."""
        if not self.model.rowCount():
            return
        top = self.model.index(0, 0)
        bottom = self.model.index(self.model.rowCount() - 1, self.model.columnCount() - 1)
        self.model.dataChanged.emit(top, bottom, [Qt.DisplayRole, Qt.ForegroundRole])

    def export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export structures", "structures.csv", "CSV files (*.csv)"
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow([header for _, header in _StructuresModel.COLUMNS])
                for row in self.model.rows():
                    writer.writerow(
                        [self.model.display(row, key) for key, _ in _StructuresModel.COLUMNS]
                    )
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
