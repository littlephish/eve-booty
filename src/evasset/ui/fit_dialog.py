"""What's on a ship: fitted modules and charges, drones, fighters, cargo,
fleet hangar, and every specialized hold -- everything whose location_id is
that ship's item_id.

The grouping/labelling logic (and the EFT/Pyfa export) lives in
evasset.fitting (Qt-free, unit tested); this file is just the dialog chrome
around it.
"""

from __future__ import annotations

import sqlite3

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

from .. import queries
from ..fitting import group_fit, to_eft
from .async_query import AsyncQuery


class FitDialog(QDialog):
    def __init__(self, ship_item_id: int, ship_name: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._ship_name = ship_name
        self._rows: list[sqlite3.Row] = []
        self.setWindowTitle(f"Fit — {ship_name}")
        self.resize(460, 600)

        layout = QVBoxLayout(self)
        self.status = QLabel("Loading…")
        self.status.setStyleSheet("color: palette(mid);")
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

    def _on_rows(self, rows: list[sqlite3.Row]) -> None:
        self._rows = rows
        self.copy_btn.setEnabled(bool(rows))
        self.status.hide()
        groups = group_fit(rows)
        if not groups:
            empty = QLabel("Nothing fit, loaded or stowed on this ship.")
            empty.setStyleSheet("color: palette(mid);")
            self._add_row(empty)
            return
        for label, lines in groups:
            header = QLabel(f"<b>{label}</b>")
            self._add_row(header)
            for line in lines:
                item_label = QLabel(line)
                item_label.setContentsMargins(16, 0, 0, 4)
                item_label.setWordWrap(True)
                self._add_row(item_label)

    def _on_failed(self, message: str) -> None:
        self.status.setText(f"Could not load fit: {message}")

    def _copy_eft(self) -> None:
        text = to_eft(self._ship_name, self._rows)
        QApplication.clipboard().setText(text)
        self.status.setText("Copied. In Pyfa: File → Import From Clipboard.")
        self.status.show()
