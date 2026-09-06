"""Structures our corporations own: fuel, reinforcement timers, moon drills.

Double-click a row, or right-click it for "Open structure", to get the detail
dialog: an Overview, the Fit (service modules, slots, rigs, fuel bay, quantum
core) and a History of everything a sync has noticed changing.

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
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer
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

# Formatting lives in evasset.evetime so structure_history can use it without
# importing Qt. Imported here rather than moved away because this is where
# these were defined, and the tab and its tests still reach for them by these
# names -- fmt_remaining is re-exported for exactly that reason and is not
# called in this module.
from ..evetime import (  # noqa: F401
    fmt_deadline,
    fmt_eve,
    fmt_remaining,
    parse_utc,
    sort_key,
)
from .assets_view import _SortProxy
from .async_query import AsyncQuery
from .debounce import Debounce
from .models import fill_combo
from .palette import CRITICAL, NORMAL, SECONDARY_TEXT, WARN, status_brush
from .sort_controller import SortController
from .structure_dialog import StructureDialog

# How close to empty before a fuel bay is worth shouting about. Three days is
# roughly "you can still fix this at the weekend"; one day is "today".
FUEL_WARN = timedelta(days=3)
FUEL_CRITICAL = timedelta(days=1)

# Reinforcement states worth colouring. The rest of the enum is either normal
# operation or a state nothing can be done about.
REINFORCED_STATES = {"armor_reinforce", "hull_reinforce"}
VULNERABLE_STATES = {
    "anchor_vulnerable", "armor_vulnerable", "deploy_vulnerable",
    "hull_vulnerable", "onlining_vulnerable", "shield_vulnerable",
}


def is_unanchored(row) -> bool:
    """Two things mean "not there any more", and both belong behind one
    switch: ESI actively reporting the unanchored state, and ESI having
    stopped reporting the structure at all (sync sets gone_at -- see
    Syncer._mark_unanchored). The second is the common one; a structure that
    finishes unanchoring simply vanishes from the response.
    """
    if row["gone_at"]:
        return True
    return str(row["state"] or "").lower() == "unanchored"


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
        # What "Reset sort" goes back to. A fresh query result is an order in
        # its own right, so it is kept rather than re-derived.
        self._unsorted = list(self._rows)
        self.endResetModel()

    def rows(self) -> list:
        return self._rows

    def sort_value(self, row, key: str):
        """What a column sorts by, as opposed to what it displays.

        The two differ on every column that is not plain text. "Fuel expires"
        renders as "2026-09-24 04:00  ·  19d 2h", and ordering that string
        sorts by the rendered date text with the remaining-time suffix as a
        tiebreak -- close enough to look right and not a chronological order.
        The vuln hour renders as "09:00" and is a number. Text is folded
        because every name ordering in the SQL says COLLATE NOCASE, and a raw
        str comparison would file "apple" after "Zebra".
        """
        if key in self.DEADLINES:
            return sort_key(row[key])
        if key == "reinforce_hour":
            hour = row["reinforce_hour"]
            return -1 if hour is None else int(hour)
        value = row[key]
        return "" if value is None else str(value).casefold()

    def sort(self, column: int, order=Qt.AscendingOrder) -> None:
        """Reorder the rows themselves.

        SortController asks the *source* model to sort rather than the proxy
        in front of it, for the reasons in its module docstring. Without this
        method that call reached QAbstractTableModel.sort(), which is a
        base-class no-op: the header highlighted, the sort arrow flipped, and
        the rows never moved. Everything else about the click looked like it
        had worked, which is why it survived a first report.

        A column outside the table is the reset case -- SortController.reset()
        passes -1 -- and means "back to the order the query returned".
        """
        before = self._rows
        self.layoutAboutToBeChanged.emit()
        stale = self.persistentIndexList()
        if 0 <= column < len(self._keys):
            key = self._keys[column]
            self._rows = sorted(
                before,
                key=lambda row: self.sort_value(row, key),
                reverse=order == Qt.DescendingOrder,
            )
        else:
            self._rows = list(self._unsorted)
        if stale:
            # Keep the selection on the structure it was on, not on whatever
            # row number it happened to occupy.
            landed = {id(row): i for i, row in enumerate(self._rows)}
            self.changePersistentIndexList(
                stale,
                [
                    self.index(landed[id(before[ix.row()])], ix.column())
                    if 0 <= ix.row() < len(before) else QModelIndex()
                    for ix in stale
                ],
            )
        self.layoutChanged.emit()

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
            # A fuel clock frozen in the past counts down to a date nothing
            # will refresh, and reads as an imminent deadline. Blank it.
            if is_unanchored(row):
                return ""
            return fmt_deadline(row[key])
        if key == "state":
            # Say what it is now, not what it was when the lights went out.
            if is_unanchored(row):
                return "Unanchored"
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
        # An unanchored structure is not an emergency, whatever its last
        # reported state said. Everything the severity is computed from --
        # the state, the fuel clock, the service list -- froze on the day it
        # stopped being reported, so scoring it would put a permanent red row
        # in the list demanding action nobody can take.
        if is_unanchored(row):
            return NORMAL
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

        if role == Qt.UserRole:            # what a sort compares
            return self.sort_value(row, key)

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
            return status_brush(level)

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
        parent: QWidget | None = None,
        *,
        defer_load: bool = False,
    ):
        super().__init__(parent)
        self._query = AsyncQuery(self)
        self._hidden_unanchored = 0

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

        # Off by default: an unanchored structure keeps whatever state and
        # fuel clock it had the day it went away, so leaving them in means a
        # frozen timer sitting in the list reading like a live one.
        # "Show unanchored", not "Unanchored": a bare adjective on a tick box
        # does not say which way it goes -- filter *to* them, or include them.
        self.show_unanchored = QCheckBox("Show unanchored")
        self.show_unanchored.setToolTip(
            "Include structures that have been unanchored, or that ESI has "
            "stopped reporting. Their state and fuel times are frozen at "
            "whenever they were last seen, so they are hidden by default."
        )
        bar.addWidget(self.show_unanchored)

        self.export_btn = QPushButton("Export CSV...")
        bar.addWidget(self.export_btn)
        root.addLayout(bar)

        self.table = QTableView()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.doubleClicked.connect(self._open_double_clicked)
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
        self.empty.setStyleSheet(f"color: {SECONDARY_TEXT};")
        self.empty.setVisible(False)   # until a query says the list is empty
        root.addWidget(self.empty)

        self.footer = QLabel("")
        self.footer.setStyleSheet(f"color: {SECONDARY_TEXT};")
        root.addWidget(self.footer)

        self._debounce = Debounce(self, self.reload)
        self.search.textChanged.connect(self._debounce.trigger)
        self.owner.currentIndexChanged.connect(self.reload)
        self.attention.currentIndexChanged.connect(self.reload)
        self.show_unanchored.toggled.connect(self.reload)
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

    # --------------------------------------------------------- context menu
    def _context_menu(self, pos) -> None:
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        row = self.model.rows()[self.proxy.mapToSource(index).row()]
        self.menu_for_structure(row).exec(self.table.viewport().mapToGlobal(pos))

    def _open_double_clicked(self, index) -> None:
        if index.isValid():
            self.open_structure(self.model.rows()[self.proxy.mapToSource(index).row()])

    def menu_for_structure(self, row) -> QMenu:
        """Built and returned rather than exec'd here so the entries can be
        asserted without a modal event loop -- same split as
        treemap_view.menu_for_tile."""
        menu = QMenu(self)
        menu.addAction("Open structure…", lambda: self.open_structure(row))
        menu.addAction("View fit…", lambda: self.open_structure(row, tab="Fit"))
        return menu

    def open_structure(self, row, tab: str = "Overview") -> None:
        """The detail dialog: Overview, Fit and History for one structure.

        "View fit" opens that same window on its Fit tab rather than a
        separate one. There is one place everything about a structure lives,
        and the menu entry is a shortcut into it rather than a second feature
        that happens to show the same data.
        """
        StructureDialog(row["structure_id"], row["name"], parent=self, tab=tab).exec()

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
        with_unanchored = self.show_unanchored.isChecked()

        def render(rows):
            keep = [
                row for row in rows
                if self._matches(row, needle, owner, only_attention, with_unanchored)
            ]
            self._hidden_unanchored = (
                0 if with_unanchored else sum(1 for r in rows if is_unanchored(r))
            )
            self.model.set_rows(keep)
            self._render_footer(keep, len(rows))

        self._query.run(queries.fetch_structures, render)

    def _matches(
        self, row, needle: str, owner: str, only_attention: bool, with_unanchored: bool
    ) -> bool:
        if not with_unanchored and is_unanchored(row):
            return False
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
        # Named rather than just subtracted from the count: "125 of 128" makes
        # someone go hunting for three structures they cannot see.
        hidden = self._hidden_unanchored
        if hidden:
            text += f" - {hidden} unanchored hidden"
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
