"""Everything about one structure: Overview, Fit and History.

Opened from the Structures tab. The Fit tab is the same FitPane the Assets
tab puts in a window of its own -- a structure's fit is a different query
(see queries.fetch_structure_fit) but the same widget.

Two things on the Overview are worked out here rather than reported by ESI,
and both are labelled as such on screen:

  Power   /corporations/{id}/structures has no power field at all. A fuelled
          Upwell structure is at full power, so the fuel clock is the whole
          basis for it, and the line says so.
  First seen
          There is no anchored-at anywhere in ESI. The earliest thing this
          app ever recorded about the structure is the closest honest answer,
          and for anything that predates the history feature that is the day
          tracking started -- which is why the label is "First seen" and not
          "Anchored".

History times are when a sync noticed a change, never when it happened in
game. The tab says so once, above the list, instead of hedging every row.
"""

from __future__ import annotations

import json

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import icons, queries, structure_history
from ..evetime import fmt_deadline, fmt_eve, parse_utc
from .async_query import AsyncQuery
from .fit_dialog import FitPane
from .palette import SECONDARY_TEXT
from .workers import Job

_RENDER_PX = 256


class _RenderJob(Job):
    """One render fetch, off the GUI thread. Decoration: a failure leaves the
    placeholder and is never reported."""

    def __init__(self, type_id: int):
        super().__init__()
        self.type_id = type_id

    def run_job(self):
        return icons.fetch_render(self.type_id)


# ------------------------------------------------------------------ history
class _HistoryModel(QAbstractTableModel):
    COLUMNS = ["When (noticed)", "What"]

    def __init__(self, events=None):
        super().__init__()
        self._events: list = []
        self.set_events(events or [])

    def set_events(self, events) -> None:
        self.beginResetModel()
        self._events = list(events)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return len(self._events)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid() or role != Qt.DisplayRole:
            return None
        event = self._events[index.row()]
        if index.column() == 0:
            return fmt_eve(parse_utc(event.at)) or event.at
        return event.text


# ----------------------------------------------------------------- overview
def _power(row) -> str:
    """Full or low power, worked out from the fuel clock.

    ESI does not report power state for a structure, so this is the only way
    to it. Worded as an inference on screen rather than stated as a fact.
    """
    expires = parse_utc(row["fuel_expires"])
    if expires is None:
        return "Unknown — no fuel clock reported"
    from datetime import datetime, timezone

    if expires > datetime.now(timezone.utc):
        return "Full power — inferred from the fuel clock"
    return "Low power — inferred from the fuel clock, which has run out"


def _services(raw) -> str:
    try:
        services = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return ""
    return ", ".join(
        f"{s.get('name', '?')} ({s.get('state', '?')})"
        for s in services
        if isinstance(s, dict)
    )


def _status(row) -> str:
    if row["gone_at"]:
        return f"Unanchored, or ESI stopped reporting it, by {fmt_eve(parse_utc(row['gone_at']))}"
    return "Active"


class StructureDialog(QDialog):
    def __init__(
        self,
        structure_id: int,
        name: str,
        parent: QWidget | None = None,
        *,
        tab: str = "Overview",
        defer_load: bool = False,
    ):
        super().__init__(parent)
        self.structure_id = structure_id
        self.setWindowTitle(name)
        self.resize(820, 640)
        self._render_job: _RenderJob | None = None

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        self.tabs.addTab(self._build_overview(), "Overview")
        self.fit = FitPane(
            structure_id,
            name,
            parent=self,
            fetch=lambda conn: queries.fetch_structure_fit(conn, structure_id),
            empty_text=(
                "Nothing fitted on this structure.\n\n"
                "Fittings come from corporation assets, so this stays empty "
                "without the corp assets scope."
            ),
        )
        self.tabs.addTab(self.fit, "Fit")
        self.tabs.addTab(self._build_history(), "History")

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        for i in range(self.tabs.count()):
            if self.tabs.tabText(i) == tab:
                self.tabs.setCurrentIndex(i)
                break

        self._query = AsyncQuery(self)
        if not defer_load:
            self.reload()

    # ------------------------------------------------------------- building
    def _build_overview(self) -> QWidget:
        page = QWidget()
        outer = QHBoxLayout(page)

        self.render = QLabel()
        self.render.setFixedSize(_RENDER_PX, _RENDER_PX)
        self.render.setAlignment(Qt.AlignCenter)
        self.render.setText("")
        outer.addWidget(self.render, 0, Qt.AlignTop)

        self.overview = QWidget()
        self.form = QFormLayout(self.overview)
        self.form.setLabelAlignment(Qt.AlignRight)
        outer.addWidget(self.overview, 1)
        return page

    def _build_history(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)

        # Said once, here, rather than qualifying every row. Every timestamp
        # in this table is when a sync saw the change, which can be hours
        # after it happened -- and these are timers people plan around.
        self.history_note = QLabel(
            "Times are when a sync noticed the change, not when it happened in "
            "game. History begins the first time EVE Booty saw this structure; "
            "nothing before that can be recovered."
        )
        self.history_note.setWordWrap(True)
        self.history_note.setStyleSheet(f"color: {SECONDARY_TEXT};")
        box.addWidget(self.history_note)

        self.history = QTableView()
        self.history.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.history.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.history.verticalHeader().setVisible(False)
        self.history.setAlternatingRowColors(True)
        self.history_model = _HistoryModel()
        self.history.setModel(self.history_model)
        self.history.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents
        )
        self.history.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        box.addWidget(self.history, 1)

        self.history_empty = QLabel(
            "Nothing recorded yet.\n\n"
            "Changes are noticed by comparing one sync against the last, so "
            "this fills in from here rather than showing anything from before."
        )
        self.history_empty.setAlignment(Qt.AlignCenter)
        self.history_empty.setWordWrap(True)
        self.history_empty.setStyleSheet(f"color: {SECONDARY_TEXT};")
        box.addWidget(self.history_empty)
        return page

    # ----------------------------------------------------------------- data
    def reload(self) -> None:
        sid = self.structure_id
        self._query.run(
            lambda conn: (
                queries.fetch_structure(conn, sid),
                queries.fetch_structure_changes(conn, sid),
            ),
            lambda result: (self.show_structure(result[0]), self.show_history(result[1])),
        )

    def show_structure(self, row) -> None:
        while self.form.count():
            item = self.form.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if row is None:
            self.form.addRow(QLabel("This structure is no longer in the database."))
            return

        for label, value in self._overview_fields(row):
            if value:
                self.form.addRow(f"{label}:", QLabel(str(value)))
        self._load_render(row["type_id"])

    def _overview_fields(self, row) -> list[tuple[str, str]]:
        moon = ""
        if row["chunk_arrival_time"]:
            moon = fmt_deadline(row["chunk_arrival_time"])
        return [
            ("Structure ID", row["structure_id"]),
            ("Name", row["name"]),
            ("Type", row["type_name"]),
            ("System", row["system_name"]),
            ("Region", row["region_name"]),
            ("Corporation", row["owner_name"]),
            ("Alliance", row["alliance_name"]),
            ("Power", _power(row)),
            ("State", row["state"]),
            ("Reinforcement exits", fmt_deadline(row["state_timer_end"])),
            ("Fuel expires", fmt_deadline(row["fuel_expires"])),
            ("Unanchors at", fmt_deadline(row["unanchors_at"])),
            ("Next moon extraction", moon),
            ("Natural decay", fmt_deadline(row["natural_decay_time"])),
            ("Services", _services(row["services"])),
            ("Status", _status(row)),
            ("First seen", fmt_eve(parse_utc(row["first_seen"]))),
            ("Last seen", fmt_eve(parse_utc(row["updated_at"]))),
        ]

    def show_history(self, rows) -> None:
        events = structure_history.group_events(rows)
        self.history_model.set_events(events)
        self.history.setVisible(bool(events))
        self.history_empty.setVisible(not events)

    # --------------------------------------------------------------- render
    def _load_render(self, type_id) -> None:
        """Off the GUI thread, and entirely optional -- the panel reads fine
        without a picture, so a failure is never reported."""
        if not type_id:
            return
        cached = icons.render_path(int(type_id))
        if cached is not None:
            self._show_render(cached)
            return
        job = _RenderJob(int(type_id))
        self._render_job = job  # strong ref until it reports back
        job.signals.finished.connect(self._show_render)
        from PySide6.QtCore import QThreadPool

        QThreadPool.globalInstance().start(job)

    def _show_render(self, path) -> None:
        self._render_job = None
        if not path:
            return
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            return
        self.render.setPixmap(
            pixmap.scaled(
                _RENDER_PX, _RENDER_PX, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        )
