"""The Assets tab's right-hand rail: per-level rollups with pins, value bars
and a type-ahead box. Successor to the old GroupPanel.

Two departures from the panel this replaces, both consequences of the omnibox.
A single click on a row now applies it as a filter chip immediately -- the old
panel demanded an explicit Apply because its filter was invisible once
applied, so a stray click silently rewrote the table with no sign of why. A
chip is visible and deletable, which turns a misclick into a one-click undo
and leaves the apply ceremony with nothing left to pay for. And the type-ahead
box filters client-side over the rows already delivered rather than
re-querying: the rail never holds more than the distinct labels of one
grouping level, narrowing by name needs no aggregate recomputed, and a
per-keystroke round trip through AsyncQuery would only add latency and a
generation race to a list that is already in memory. Sorting, by contrast, is
server-side (refresh_needed) because the rollup query orders on aggregates it
computes itself.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import queries
from . import palette
from .models import fmt_num, fmt_short_isk

# ESI hands back asset locations the app has not resolved to a station or
# structure name yet; queries.py labels them "Unknown location <id>". At location
# level those ids are noise -- dozens of rows a click on which would filter to
# nothing recognisable -- so the rail folds them into one synthetic row.
_UNKNOWN_PREFIX = "Unknown location "


def _is_unknown_label(label: str) -> bool:
    """True only for the machine-generated fallback labels.

    Those are always the prefix plus a bare location id, so the suffix must
    be all digits -- a player structure someone actually named
    "Unknown location HQ" is a real place and must stay clickable rather
    than being folded into the synthetic row."""
    return label.startswith(_UNKNOWN_PREFIX) and label[len(_UNKNOWN_PREFIX):].isdigit()


# Sort keys as rail_rollups() spells them, with the segment-button captions.
_SORT_LABELS = [("value", "ISK"), ("name", "A–Z"), ("volume", "m³")]
_SORT_DISPLAY = dict(_SORT_LABELS)

_UNKNOWN_TOOLTIP = (
    "Stations or structures whose names have not been resolved yet. "
    "They resolve on the next sync."
)


class _ElideLabel(QLabel):
    """QLabel that elides in the middle instead of clipping.

    Middle elision because station names share long prefixes ("Jita IV -
    Moon 4 - Caldari Navy ...") -- the distinguishing part sits at both ends,
    so end elision would render half the rail identical. text() keeps the full
    string; only painting elides.
    """

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(text, parent)
        # QLabel's minimumSizeHint demands the full text width, which would
        # let one long station name force the whole rail wider than the
        # splitter gave it. A small floor keeps the rail squeezable.
        self.setMinimumWidth(40)

    def minimumSizeHint(self):  # noqa: N802
        hint = super().minimumSizeHint()
        hint.setWidth(40)
        return hint

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        elided = self.fontMetrics().elidedText(self.text(), Qt.ElideMiddle, self.width())
        painter.drawText(self.rect(), int(Qt.AlignLeft | Qt.AlignVCenter), elided)


class _ValueBar(QWidget):
    """Thin bar whose filled width is a fraction of the row's width.

    The fraction is stored as data rather than derived from pixels so the
    proportionality is testable offscreen, where nothing is ever painted.
    """

    def __init__(self, fraction: float, parent: QWidget | None = None):
        super().__init__(parent)
        self.fraction = max(0.0, min(1.0, fraction))
        self.setFixedHeight(3)

    def paintEvent(self, event) -> None:  # noqa: N802
        if self.fraction <= 0:
            return
        pair = palette.RAIL_BAR
        colour = pair[1] if palette.is_dark(self.palette()) else pair[0]
        painter = QPainter(self)
        painter.fillRect(0, 0, round(self.width() * self.fraction), self.height(), QColor(colour))


class _RollupRow(QWidget):
    """One rail row: optional pin star, elided label on its own line, value
    bar, then the muted stacks-and-volume line with the compact ISK at its
    end. The value rides the meta line rather than the name line so a long
    station name gets the full rail width instead of fighting the number for
    it. pinned=None means no star at all -- the synthetic unknown-locations
    row has no stable label to pin."""

    def __init__(
        self,
        label: str,
        isk: str,
        sub: str,
        fraction: float,
        pinned: bool | None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(4, 3, 6, 3)
        outer.setSpacing(4)

        self.star: QToolButton | None = None
        if pinned is not None:
            self.star = QToolButton()
            self.star.setCheckable(True)
            self.star.setChecked(pinned)
            self.star.setAutoRaise(True)
            self.star.setText("★" if pinned else "☆")
            self.star.setToolTip("Pin to the top of the rail")
            # The glyph flips immediately on click; the authoritative pinned
            # order still comes from the host's next set_rollups.
            self.star.toggled.connect(lambda on: self.star.setText("★" if on else "☆"))
            outer.addWidget(self.star, 0, Qt.AlignTop)

        body = QVBoxLayout()
        body.setSpacing(1)
        self.name = _ElideLabel(label)
        body.addWidget(self.name)
        self.bar = _ValueBar(fraction)
        body.addWidget(self.bar)
        foot = QHBoxLayout()
        foot.setSpacing(6)
        self.sub = QLabel(sub)
        self.sub.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        # The same squeezability floor _ElideLabel gives the name: without
        # it, one wide meta line would force the whole rail past the width
        # the splitter granted.
        self.sub.setMinimumWidth(40)
        foot.addWidget(self.sub, 1)
        self.isk = QLabel(isk)
        foot.addWidget(self.isk, 0)
        body.addLayout(foot)
        outer.addLayout(body, 1)


class _FlipRow(QWidget):
    """A "where is it" row: label plus right-aligned quantity, nothing else."""

    def __init__(self, label: str, quantity: str, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 3, 6, 3)
        layout.setSpacing(6)
        self.name = _ElideLabel(label)
        layout.addWidget(self.name, 1)
        self.quantity = QLabel(quantity)
        self.quantity.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self.quantity, 0)


class Rail(QWidget):
    """Rollup rail: pick a level, see every label's stacks, volume and value,
    click one to add it as an omnibox chip.

    The host owns all data flow: it calls set_rollups()/set_flip() with rows
    from queries.rail_rollups()/where_is_item(), re-queries on refresh_needed
    (sort changed) and level_changed, and persists pins on pin_toggled. The
    rail itself only re-arranges what it was last given -- the type-ahead is
    a client-side name filter, never a query.
    """

    chip_requested = Signal(str, str)  # (level key, label)
    level_changed = Signal(str)  # level key
    pin_toggled = Signal(str, str)  # (level key, label)
    refresh_needed = Signal()  # sort changed; the host re-queries

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        bar = QHBoxLayout()
        self.level = QComboBox()
        for label, _key in queries.ROLLUP_LEVELS:
            self.level.addItem(label)
        bar.addWidget(self.level, 1)

        self._sort = "value"
        self.sort_buttons: dict[str, QToolButton] = {}
        self._sort_group = QButtonGroup(self)
        self._sort_group.setExclusive(True)
        for key, text in _SORT_LABELS:
            button = QToolButton()
            button.setText(text)
            button.setCheckable(True)
            button.setAutoRaise(True)
            button.setToolTip(f"Sort by {text}")
            self._sort_group.addButton(button)
            self.sort_buttons[key] = button
            bar.addWidget(button)
        self.sort_buttons[self._sort].setChecked(True)
        root.addLayout(bar)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter…")
        self.search.setClearButtonEnabled(True)
        root.addWidget(self.search)

        # Same reasoning as the old GroupPanel: without an explicit minimum
        # the splitter takes the header row's layout minimum as this panel's
        # floor, which tracks font metrics and silently widens on large
        # system fonts. An explicit floor keeps the rail squeezable.
        self.setMinimumWidth(150)

        self.rows_list = QListWidget()
        # Rows act on click, so a lingering selection highlight would imply a
        # state the rail does not have.
        self.rows_list.setSelectionMode(QAbstractItemView.NoSelection)
        # Rows are sized to the viewport (see _fit_item), so a horizontal
        # scrollbar could only ever appear during a transient relayout --
        # never draw one.
        self.rows_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Splitter drags and the vertical scrollbar appearing both change the
        # viewport width; the filter re-fits every row so the value bars
        # always end at the rail's edge.
        self.rows_list.viewport().installEventFilter(self)
        root.addWidget(self.rows_list, 1)

        self._rollups: list = []
        self._pinned: set[str] = set()
        self._flip: list | None = None

        self.level.currentIndexChanged.connect(
            lambda _index: self.level_changed.emit(self.current_level())
        )
        self.search.textChanged.connect(lambda _text: self._apply_type_ahead())
        self._sort_group.buttonClicked.connect(self._on_sort_clicked)
        self.rows_list.itemClicked.connect(self._on_item_clicked)

    # ------------------------------------------------------------- public API
    def current_level(self) -> str:
        return queries.ROLLUP_LEVELS[self.level.currentIndex()][1]

    def current_sort(self) -> str:
        return self._sort

    def filter_text(self) -> str:
        return self.search.text()

    def set_rollups(self, rows, pinned: set[str]) -> None:
        """Replace the rollup rows (queries.rail_rollups shape: label, stacks,
        units, volume, sell_value) and the pinned labels for the current
        level. While flip mode is active the data is stored but the flip list
        stays on screen -- leaving flip mode is clear_flip()'s job alone."""
        self._rollups = list(rows)
        self._pinned = set(pinned)
        self._render()

    def set_flip(self, rows) -> None:
        """Swap to "where is it" mode: rows of (label, quantity), no pins or
        bars. The type-ahead keeps filtering."""
        self._flip = list(rows)
        self._render()

    def clear_flip(self) -> None:
        if self._flip is None:
            return
        self._flip = None
        self._render()

    # ------------------------------------------------------------- rendering
    def _render(self) -> None:
        lst = self.rows_list
        scroll = lst.verticalScrollBar().value()
        # Blocked for the whole rebuild so clearing and re-adding items can
        # never surface as a spurious chip_requested mid-refresh.
        lst.blockSignals(True)
        lst.clear()
        if self._flip is not None:
            rows = list(self._flip)
            if rows:
                self._add_caption("Where is it · by quantity")
            for row in rows:
                self._add_flip_row(row)
        else:
            rows = self._collapsed_rollups()
            # Bars scale to the largest rollup in the full set. The
            # type-ahead hides rows rather than re-rendering (see
            # _apply_type_ahead), so the survivors keep their absolute
            # proportions instead of rescaling under the cursor.
            top = max((float(r["sell_value"] or 0) for r in rows), default=0.0)
            pinned = [r for r in rows if r["label"] in self._pinned]
            rest = [r for r in rows if r["label"] not in self._pinned]
            if pinned:
                self._add_caption("Pinned")
                for row in pinned:
                    self._add_rollup_row(row, top, pinned=True)
            if rest:
                self._add_caption(f"All · by {_SORT_DISPLAY[self._sort]}")
                for row in rest:
                    self._add_rollup_row(row, top, pinned=False)
        lst.blockSignals(False)
        # clear() zeroed the scrollbar range; force the layout so the range
        # is current again before the old position is put back. The refit
        # catches the viewport width change if that layout just made the
        # vertical scrollbar appear or vanish.
        lst.doItemsLayout()
        self._refit_items()
        self._apply_type_ahead()
        lst.verticalScrollBar().setValue(scroll)

    def _apply_type_ahead(self) -> None:
        """Hide the rows the filter box rules out; captions stay put.

        Hiding instead of re-rendering keeps a keystroke near-free --
        rebuilding ~300 widget rows measured around 200 ms per press, paid
        on every character typed. The trade is that the value bars keep
        their full-set scale while filtered, which also stops them jumping
        under the cursor as the list narrows."""
        needle = self.search.text().strip().lower()
        for i in range(self.rows_list.count()):
            item = self.rows_list.item(i)
            data = item.data(Qt.UserRole)
            if data["kind"] == "caption":
                continue
            item.setHidden(bool(needle) and needle not in str(data["label"]).lower())

    def _collapsed_rollups(self) -> list:
        """Rollup rows with the unknown-location fold applied.

        Only at location level: at every other level "Unknown location" is
        not a label the query produces, and folding by prefix elsewhere could
        swallow a legitimately named owner or container. The synthetic row is
        a plain dict standing in the position of the first unknown row, so
        the server-side sort order is disturbed as little as possible."""
        if self.current_level() != "location":
            return list(self._rollups)
        out: list = []
        unknown: list = []
        slot: int | None = None
        for row in self._rollups:
            if _is_unknown_label(str(row["label"])):
                if slot is None:
                    slot = len(out)
                    out.append(None)
                unknown.append(row)
            else:
                out.append(row)
        if unknown:
            out[slot] = {
                "label": f"Unknown locations ({len(unknown)})",
                "stacks": sum(r["stacks"] or 0 for r in unknown),
                "units": sum(r["units"] or 0 for r in unknown),
                "volume": sum(r["volume"] or 0 for r in unknown),
                "sell_value": sum(r["sell_value"] or 0 for r in unknown),
            }
        return out

    def _add_caption(self, text: str) -> None:
        item = QListWidgetItem()
        item.setFlags(Qt.NoItemFlags)
        item.setData(Qt.UserRole, {"kind": "caption", "label": text})
        label = QLabel(text)
        label.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        label.setContentsMargins(4, 6, 4, 1)
        self._fit_item(item, label)
        self.rows_list.addItem(item)
        self.rows_list.setItemWidget(item, label)

    def _add_rollup_row(self, row, top: float, *, pinned: bool) -> None:
        label = str(row["label"])
        # The synthetic unknown-locations row is the one dict among
        # sqlite3.Rows -- see _collapsed_rollups().
        synthetic = isinstance(row, dict)
        value = float(row["sell_value"] or 0)
        # Compact volume, as the concept board draws it ("24.5K m³") -- the
        # full figure was most of what used to push rows past the rail edge.
        sub = f"{fmt_num(row['stacks'])} stacks · {fmt_short_isk(row['volume'] or 0)} m³"
        widget = _RollupRow(
            label, fmt_short_isk(value), sub, value / top if top > 0 else 0.0,
            None if synthetic else pinned,
        )
        if synthetic:
            widget.setToolTip(_UNKNOWN_TOOLTIP)
        else:
            # The name label elides, so the full label rides on the tooltip.
            widget.setToolTip(f"{label}\n{fmt_num(row['units'])} units")
            widget.star.clicked.connect(
                lambda _checked=False, lbl=label: self.pin_toggled.emit(
                    self.current_level(), lbl
                )
            )
        item = QListWidgetItem()
        item.setData(Qt.UserRole, {"kind": "unknown" if synthetic else "row", "label": label})
        self._fit_item(item, widget)
        self.rows_list.addItem(item)
        self.rows_list.setItemWidget(item, widget)

    def _add_flip_row(self, row) -> None:
        label = str(row["label"])
        widget = _FlipRow(label, fmt_num(row["quantity"]))
        widget.setToolTip(label)
        item = QListWidgetItem()
        item.setData(Qt.UserRole, {"kind": "flip", "label": label})
        self._fit_item(item, widget)
        self.rows_list.addItem(item)
        self.rows_list.setItemWidget(item, widget)

    def _fit_item(self, item: QListWidgetItem, widget: QWidget) -> None:
        """Item width tracks the viewport, never the widget's natural width.

        setSizeHint(widget.sizeHint()) froze each row at whatever width its
        content happened to want, so one wide meta line pushed every
        100%-fraction bar past the rail's visible edge. Height still comes
        from the widget; width belongs to the rail."""
        width = max(self.rows_list.viewport().width(), 1)
        item.setSizeHint(QSize(width, widget.sizeHint().height()))

    def _refit_items(self) -> None:
        width = max(self.rows_list.viewport().width(), 1)
        for i in range(self.rows_list.count()):
            item = self.rows_list.item(i)
            hint = item.sizeHint()
            if hint.width() != width:
                item.setSizeHint(QSize(width, hint.height()))

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.rows_list.viewport() and event.type() == QEvent.Resize:
            self._refit_items()
        return super().eventFilter(obj, event)

    # --------------------------------------------------------------- handlers
    def _on_sort_clicked(self, button: QToolButton) -> None:
        key = next(k for k, b in self.sort_buttons.items() if b is button)
        # buttonClicked fires on a click of the already-checked button too;
        # re-querying for an unchanged order would be pure waste.
        if key != self._sort:
            self._sort = key
            self.refresh_needed.emit()

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.UserRole) or {}
        # Captions never get here (Qt.NoItemFlags disables them); the
        # synthetic unknown row does, and deliberately emits nothing -- there
        # is no single chip that selects "every unresolved location".
        if data.get("kind") in ("row", "flip"):
            self.chip_requested.emit(self.current_level(), data["label"])
