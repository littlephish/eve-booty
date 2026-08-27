"""What's on a ship: fitted modules and charges, drones, fighters, cargo,
fleet hangar, and every specialized hold -- everything whose location_id is
that ship's item_id.

The grouping/labelling logic (and the EFT/Pyfa export) lives in
evasset.fitting (Qt-free, unit tested); this file is just the dialog chrome
around it.

Slot-rack lines carry the module's type icon and a background tint by rarity
(faction green, officer purple, deadspace blue, abyssal red -- the game
client's own colour language, see palette.RARITY_TINTS). Which lines get
that treatment is decided by fitting.FitLine, not here: a line with a
type_id is a slot line, a line without one is hold/bay/cargo text. Icons
come from CCP's image service via evasset.icons, fetched off the GUI thread
and cached on disk, so the dialog opens instantly with placeholders and the
icons drop in when the fetch lands (immediately, once cached).
"""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import icons, queries
from ..fitting import group_fit, to_eft
from .async_query import AsyncQuery
from .palette import rarity_hex

_MODULE_ICON_PX = 24
_SHIP_ICON_PX = 32


class _IconSignals(QObject):
    # Signal(object), not Signal(dict): dict maps to QVariantMap, whose keys
    # must be strings -- an int-keyed {type_id: Path} fails the C++ conversion
    # at emit time (silently, from a worker thread) and the slot never runs.
    done = Signal(object)  # {type_id: Path}


class _IconFetchJob(QRunnable):
    """One batch fetch per dialog open. Same lifetime rules as every other
    QRunnable in this app (see async_query.py's docstring for the long
    version): the signals live on a QObject, and the dialog holds a strong
    reference to the job until it reports back."""

    def __init__(self, type_ids: list[int]):
        super().__init__()
        self.type_ids = type_ids
        self.signals = _IconSignals()
        self.setAutoDelete(False)

    @Slot()
    def run(self) -> None:
        try:
            paths = icons.fetch_icons(self.type_ids)
        except Exception:  # noqa: BLE001 - icons are decoration; never kill the dialog
            paths = {}
        self.signals.done.emit(paths)


def _placeholder(size: int) -> QPixmap:
    """Reserves the icon's space so text does not jump when the real pixmap
    lands, and reads as "loading" rather than as a broken image."""
    pm = QPixmap(size, size)
    pm.fill(QColor(127, 127, 127, 40))
    return pm


class FitDialog(QDialog):
    def __init__(
        self,
        ship_item_id: int,
        ship_name: str,
        ship_type_id: int | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._ship_name = ship_name
        self._rows: list[sqlite3.Row] = []
        # {type_id: [(icon label, display px), ...]} -- filled as the header
        # and rows are built, read when the fetch job reports back.
        self._icon_labels: dict[int, list[tuple[QLabel, int]]] = {}
        self._icon_job: _IconFetchJob | None = None
        self.setWindowTitle(f"Fit — {ship_name}")
        self.resize(460, 600)

        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        self.ship_icon = QLabel()
        self.ship_icon.setFixedSize(_SHIP_ICON_PX, _SHIP_ICON_PX)
        self.ship_icon.setPixmap(_placeholder(_SHIP_ICON_PX))
        header.addWidget(self.ship_icon)
        title = QLabel(f"<b>{ship_name}</b>")
        header.addWidget(title, 1)
        layout.addLayout(header)
        if ship_type_id is not None:
            self._icon_labels[ship_type_id] = [(self.ship_icon, _SHIP_ICON_PX)]

        self.status = QLabel("Loading…")
        self.status.setStyleSheet("color: palette(shadow);")
        layout.addWidget(self.status)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.addStretch(1)
        scroll.setWidget(self.body)
        layout.addWidget(scroll, 1)

        bar = QHBoxLayout()
        self.copy_btn = QPushButton("Copy for Pyfa")
        self.copy_btn.setToolTip(
            "Copies this fit as EFT text -- paste it into Pyfa with "
            "File → Import From Clipboard, or Ctrl+V on the fitting window."
        )
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(self._copy_eft)
        bar.addWidget(self.copy_btn)
        bar.addStretch(1)
        layout.addLayout(bar)

        self._query = AsyncQuery(self)

        def fetch(conn: sqlite3.Connection) -> list[sqlite3.Row]:
            return queries.fetch_fit(conn, ship_item_id)

        self._query.run(fetch, self._on_rows, self._on_failed)

    def _add_row(self, widget: QWidget) -> None:
        self.body_layout.insertWidget(self.body_layout.count() - 1, widget)

    def _make_module_row(self, line) -> QWidget:
        """Icon + text, with the rarity tint as the row's background. Tinting
        the whole line rather than just the icon is deliberate -- the tint is
        what was asked for, and it stays legible because the palette pairs
        were chosen (and are tested) to keep default text at AA contrast."""
        row = QWidget()
        row.setObjectName("fitline")
        # A bare QWidget ignores stylesheet backgrounds unless told otherwise.
        row.setAttribute(Qt.WA_StyledBackground, True)
        tint = rarity_hex(line.meta_group_id)
        if tint:
            row.setStyleSheet(
                f"QWidget#fitline {{ background-color: {tint}; border-radius: 4px; }}"
            )

        lay = QHBoxLayout(row)
        lay.setContentsMargins(12, 2, 8, 2)
        lay.setSpacing(8)
        icon = QLabel()
        icon.setFixedSize(_MODULE_ICON_PX, _MODULE_ICON_PX)
        icon.setPixmap(_placeholder(_MODULE_ICON_PX))
        lay.addWidget(icon)
        text = QLabel(line.text)
        text.setWordWrap(True)
        lay.addWidget(text, 1)

        self._icon_labels.setdefault(line.type_id, []).append((icon, _MODULE_ICON_PX))
        return row

    def _on_rows(self, rows: list[sqlite3.Row]) -> None:
        self._rows = rows
        self.copy_btn.setEnabled(bool(rows))
        self.status.hide()
        groups = group_fit(rows)
        if not groups:
            empty = QLabel("Nothing fit, loaded or stowed on this ship.")
            empty.setStyleSheet("color: palette(shadow);")
            self._add_row(empty)
            self._start_icon_fetch()
            return
        for label, lines in groups:
            header = QLabel(f"<b>{label}</b>")
            self._add_row(header)
            for line in lines:
                if line.type_id is not None:
                    self._add_row(self._make_module_row(line))
                else:
                    item_label = QLabel(line.text)
                    item_label.setContentsMargins(16, 0, 0, 4)
                    item_label.setWordWrap(True)
                    self._add_row(item_label)
        self._start_icon_fetch()

    def _start_icon_fetch(self) -> None:
        wanted = list(self._icon_labels)
        if not wanted:
            return
        job = _IconFetchJob(wanted)
        self._icon_job = job  # strong ref until the signal lands; see _IconFetchJob
        job.signals.done.connect(self._apply_icons)
        QThreadPool.globalInstance().start(job)

    def _apply_icons(self, paths: dict) -> None:
        self._icon_job = None
        for type_id, labels in self._icon_labels.items():
            path = paths.get(type_id)
            if path is None:
                continue  # 404 or offline -- the placeholder stays
            pm = QPixmap(str(path))
            if pm.isNull():
                continue
            for label, px in labels:
                label.setPixmap(
                    pm.scaled(px, px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )

    def _on_failed(self, message: str) -> None:
        self.status.setText(f"Could not load fit: {message}")
        # The ship's own icon does not depend on the fit query -- fetch it
        # anyway rather than leaving a permanent placeholder in the header.
        self._start_icon_fetch()

    def _copy_eft(self) -> None:
        text = to_eft(self._ship_name, self._rows)
        QApplication.clipboard().setText(text)
        self.status.setText("Copied. In Pyfa: File → Import From Clipboard.")
        self.status.show()
