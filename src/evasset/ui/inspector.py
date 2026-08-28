"""Row inspector: full detail for one asset row, shown in place of the rail.

The panel slides over the rail rather than opening a dialog because the rail
and the inspector answer the same "tell me more about what I selected" need
-- only one of them is useful at a time, and sharing the splitter slot means
the inspector inherits the rail's width, its resize behaviour and its place
in the layout for free (the host swaps them in a QStackedWidget).

Deliberately dumb, like EstateStrip: show_row() renders whatever row it is
handed and every button only emits a signal. The host owns the database
writes, the network job and the dialogs those buttons imply, so this widget
tests as pure rendering and the threading rules (async_query.py) stay the
host's single responsibility.

The location line shows the resolved root location plus the slot flag. A
recursive location_id walk reconstructing the full container nesting ("in a
can, in a ship, in a hangar") was considered and left out: ASSET_ROWS already
flattens every row to its root location, the flag names the immediate
context, and the walk would need its own query per opened row for a path the
table's Location column cannot show anyway.
"""

from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import palette

# One tolerant ISO parser for price timestamps; importing grouped_model's
# beats a second copy of the 'Z'-suffix workaround drifting out of step.
from .grouped_model import _parse_utc
from .models import fmt_isk, fmt_num, fmt_short_isk

# ASSET_ROWS carries the meta group's name, not its id, so the pill maps the
# four rarity names back to palette.RARITY_TINTS' keys -- "Faction" reads
# green here exactly as it does on a fitted module in the fit dialog; every
# other meta group keeps the neutral accent.
_RARITY_META_IDS = {
    "Faction": palette.META_FACTION,
    "Officer": palette.META_OFFICER,
    "Deadspace": palette.META_DEADSPACE,
    "Abyssal": palette.META_ABYSSAL,
}


class Inspector(QWidget):
    """Detail panel for one row; every action is a signal the host handles."""

    close_clicked = Signal()
    where_else_clicked = Signal()
    refresh_price_clicked = Signal()
    pin_price_clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        # Same splitter-floor reasoning as the rail: without an explicit
        # minimum the form layout's widest row becomes the panel's floor.
        self.setMinimumWidth(150)

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 4)

        head = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(0)
        self.title = QLabel("")
        self.title.setStyleSheet("font-weight: 600;")
        self.title.setWordWrap(True)
        title_box.addWidget(self.title)
        self.subtitle = QLabel("")
        self.subtitle.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        self.subtitle.setWordWrap(True)
        title_box.addWidget(self.subtitle)
        head.addLayout(title_box, 1)
        self.close_btn = QToolButton()
        self.close_btn.setText("×")
        self.close_btn.setAutoRaise(True)
        self.close_btn.setToolTip("Close (Esc)")
        self.close_btn.clicked.connect(self.close_clicked)
        head.addWidget(self.close_btn, 0, Qt.AlignTop)
        root.addLayout(head)

        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(2)
        self.owner = QLabel("")
        self.meta = QLabel("")
        # Restyled per row in show_row: the pill borrows the fit dialog's
        # rarity wash where the meta group has one, so "Faction" reads green
        # here exactly as it does on a fitted module.
        self.location = QLabel("")
        self.location.setWordWrap(True)
        self.place = QLabel("")
        self.place.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        self.quantity = QLabel("")
        self.volume = QLabel("")
        self.buy = QLabel("")
        self.sell = QLabel("")
        self.price_line = QLabel("")
        self.badges = QLabel("")
        self.badges.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        form.addRow("Owner", self.owner)
        form.addRow("Meta", self.meta)
        form.addRow("Location", self.location)
        form.addRow("", self.place)
        form.addRow("Quantity", self.quantity)
        form.addRow("Volume", self.volume)
        form.addRow("Buy", self.buy)
        form.addRow("Sell", self.sell)
        form.addRow("Price", self.price_line)
        form.addRow("", self.badges)
        root.addLayout(form)

        buttons = QVBoxLayout()
        buttons.setSpacing(4)
        self.where_else_btn = QPushButton("Where else?")
        self.where_else_btn.clicked.connect(self.where_else_clicked)
        buttons.addWidget(self.where_else_btn)
        self.refresh_btn = QPushButton("Refresh price")
        self.refresh_btn.clicked.connect(self.refresh_price_clicked)
        buttons.addWidget(self.refresh_btn)
        self.pin_btn = QPushButton("Pin price…")
        self.pin_btn.clicked.connect(self.pin_price_clicked)
        buttons.addWidget(self.pin_btn)
        root.addLayout(buttons)
        root.addStretch(1)

        shortcut = QShortcut(QKeySequence(Qt.Key_Escape), self)
        shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        shortcut.activated.connect(self.close_clicked)

    def show_row(self, row) -> None:
        """Render one ASSET_ROWS row. Called again with a fresh row whenever
        the host reloads, so the panel never shows numbers the table has
        already moved past."""
        self.title.setText(row["item"] or "")
        custom = row["custom_name"] or ""
        self.subtitle.setText(custom)
        self.subtitle.setVisible(bool(custom))
        self.owner.setText(row["owner"] or "")
        meta = row["meta"] or ""
        self.meta.setText(meta)
        self.meta.setVisible(bool(meta))
        wash = palette.rarity_hex(_RARITY_META_IDS.get(meta))
        if wash is None:
            pair = palette.CHIP_ACCENT
            wash = pair[1] if palette.is_dark() else pair[0]
        self.meta.setStyleSheet(f"background: {wash}; border-radius: 8px; padding: 0 6px;")
        flag = row["location_flag"] or ""
        location = row["location"] or ""
        self.location.setText(f"{location} · {flag}" if flag else location)
        self.place.setText(" · ".join(p for p in (row["system"], row["region"]) if p))
        self.quantity.setText(fmt_num(row["quantity"]))
        self.volume.setText(
            f"{row['unit_volume']:,.2f} m³ each · {row['volume']:,.0f} m³ total"
        )
        self.buy.setText(f"{fmt_isk(row['buy_price'])} · lot {fmt_short_isk(row['buy_value'])}")
        self.sell.setText(
            f"{fmt_isk(row['sell_price'])} · lot {fmt_short_isk(row['sell_value'])}"
        )
        source = row["price_source"] or "none"
        self.price_line.setText(_price_text(source, row["price_updated_at"]))
        self.badges.setText(" · ".join(_badges(row, source)))
        # The host decides what the pin dialog does; the caption only has to
        # be honest about which of the two conversations the click starts.
        self.pin_btn.setText("Unpin / change…" if source == "manual" else "Pin price…")


def _price_text(source: str, updated_at) -> str:
    """Source plus quote age, or the plain truth when there is no quote."""
    if source == "none":
        return "unpriced"
    updated = _parse_utc(updated_at)
    if updated is None:
        return source
    hours = (datetime.now(timezone.utc) - updated).total_seconds() / 3600
    when = f"{max(int(hours), 0)} h ago" if hours < 48 else f"{int(hours // 24)} d ago"
    return f"{source} · {when}"


def _badges(row, source: str) -> list[str]:
    parts: list[str] = []
    if source == "none":
        parts.append("unpriced")
    elif source == "manual":
        parts.append("manual price")
    if row["is_blueprint_copy"]:
        parts.append("BPC")
    return parts
