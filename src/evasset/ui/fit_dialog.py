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
come from CCP's image service via evasset.icons through type_icons'
TypeIconLoader (shared with the comparison window), fetched off the GUI
thread and cached on disk, so the dialog opens instantly with placeholders
and the icons drop in when the fetch lands (immediately, once cached).
"""

from __future__ import annotations

import json
import sqlite3

from PySide6.QtCore import Qt
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
from ..fitting import group_fit, to_eft, to_esi_fitting
from .async_query import AsyncQuery
from .palette import SECONDARY_TEXT, rarity_hex
from .type_icons import MODULE_ICON_PX, SHIP_ICON_PX, TypeIconLoader, placeholder


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
        self._ship_type_id = ship_type_id
        self._rows: list[sqlite3.Row] = []
        self._icons = TypeIconLoader()
        self.setWindowTitle(f"Fit - {ship_name}")
        self.resize(460, 600)

        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        self.ship_icon = QLabel()
        self.ship_icon.setFixedSize(SHIP_ICON_PX, SHIP_ICON_PX)
        self.ship_icon.setPixmap(placeholder(SHIP_ICON_PX))
        header.addWidget(self.ship_icon)
        if ship_type_id is not None:
            self._icons.register(self.ship_icon, ship_type_id, SHIP_ICON_PX)
        title = QLabel(f"<b>{ship_name}</b>")
        header.addWidget(title, 1)
        layout.addLayout(header)

        self.status = QLabel("Loading…")
        self.status.setStyleSheet(f"color: {SECONDARY_TEXT};")
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
            "Copies this fit as ESI fitting JSON -- paste it into Pyfa with "
            "File → Import From Clipboard, or Ctrl+V on the fitting window."
        )
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(self._copy_esi)
        bar.addWidget(self.copy_btn)

        self.copy_eft_btn = QPushButton("Copy as EFT")
        self.copy_eft_btn.setToolTip(
            "Copies this fit as EFT text -- the plain-text format for forums, "
            "chat and the in-game fitting window."
        )
        self.copy_eft_btn.setEnabled(False)
        self.copy_eft_btn.clicked.connect(self._copy_eft)
        bar.addWidget(self.copy_eft_btn)
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
        lay.addWidget(self._icons.slot(line.type_id, MODULE_ICON_PX))
        text = QLabel(line.text)
        text.setWordWrap(True)
        lay.addWidget(text, 1)
        return row

    def _on_rows(self, rows: list[sqlite3.Row]) -> None:
        self._rows = rows
        self.copy_btn.setEnabled(bool(rows))
        self.copy_eft_btn.setEnabled(bool(rows))
        self.status.hide()
        groups = group_fit(rows)
        if not groups:
            empty = QLabel("Nothing fit, loaded or stowed on this ship.")
            empty.setStyleSheet(f"color: {SECONDARY_TEXT};")
            self._add_row(empty)
            self._icons.start()
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
        self._icons.start()

    def _on_failed(self, message: str) -> None:
        self.status.setText(f"Could not load fit: {message}")
        # The ship's own icon does not depend on the fit query -- fetch it
        # anyway rather than leaving a permanent placeholder in the header.
        self._icons.start()

    def _copy_esi(self) -> None:
        """ESI fitting JSON, which is what Pyfa's clipboard import actually
        wants: Port.importAuto reads any buffer starting with "{" as an ESI
        fit. json.dumps puts the brace first, so no pretty-printing here."""
        if self._ship_type_id is None:
            self._say("No type id for this hull, so no fit can be built.")
            return
        fit = to_esi_fitting(self._ship_name, self._ship_type_id, self._rows)
        if not fit["items"]:
            self._say("Nothing on this ship can be expressed as a fit.")
            return
        QApplication.clipboard().setText(json.dumps(fit))
        self._say("Copied. In Pyfa: File → Import From Clipboard.")

    def _copy_eft(self) -> None:
        QApplication.clipboard().setText(to_eft(self._ship_name, self._rows))
        self._say("Copied as EFT.")

    def _say(self, message: str) -> None:
        self.status.setText(message)
        self.status.show()
