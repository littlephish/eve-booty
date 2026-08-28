"""Two-level tree model for the Assets table: group headers over asset rows.

The grouping itself happens in-model rather than in SQL. The rows are already
fetched (and filtered) once for the table; grouping them again server-side
would mean a second query per group-by change and a second copy of the filter
grammar. Instead set_rows() buckets the rows it is handed by one column and
computes each bucket's rollup in Python -- the same numbers a GROUP BY would
have produced, from the same rows the table is showing.

The tree is exactly two levels deep, which keeps the index bookkeeping down to
one integer: internalId() is 0 for any top-level index (a group header, or a
leaf in flat mode) and parent-group-row + 1 for a child. No node objects, no
pointers to keep alive across resets.

Sorting reorders leaves inside their group only; the groups themselves keep
their value-DESC order so the biggest pile stays on top whatever column the
user sorts by. The reorder goes through layoutChanged with the persistent
indexes remapped -- a model reset here would collapse every expanded group on
each header click.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt
from PySide6.QtGui import QBrush, QColor

from ..queries import ASSET_COLUMNS, DATE_COLUMNS, ISK_COLUMNS, NUMERIC_COLUMNS
from . import palette
from .models import fmt_date, fmt_isk, fmt_num, fmt_short_isk

GROUP_LABEL_ROLE = Qt.UserRole + 1
GROUP_ROLLUP_ROLE = Qt.UserRole + 2
HEAT_ROLE = Qt.UserRole + 3
PRICE_AGE_ROLE = Qt.UserRole + 4
PRICE_BADGE_ROLE = Qt.UserRole + 5

# The two columns that carry heat tint, staleness and price badges.
_VALUE_KEYS = {"buy_value", "sell_value"}

# A quote older than this is flagged. Jita moves fast enough that a two-day-old
# price on something volatile can be off by double digits, but flagging at, say,
# 12 hours would badge half the table after every quiet weekend.
_STALE_AFTER = timedelta(hours=48)


def _row_get(row, key: str):
    """Column value, or None when the row does not carry the column at all.

    Not every row source carries every column: ASSET_ROWS serves
    price_updated_at, but a hand-written test SELECT (or any older saved
    query shape) may simply not have it. sqlite3.Row raises IndexError for
    an unknown key, a plain dict raises KeyError; both mean the same thing
    here.
    """
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _parse_utc(value) -> datetime | None:
    """ISO 8601 from the prices table, tolerating both 'Z' and '+00:00'.

    Python 3.10's fromisoformat() does not accept the 'Z' suffix ESI writes
    (3.11 fixed that), so it is rewritten before parsing.
    """
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Timestamps are UTC end to end; a naive one is UTC that lost its tag.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _price_age_days(row) -> int | None:
    """Whole days since the price quote, or None while it is under 48 h old."""
    updated = _parse_utc(_row_get(row, "price_updated_at"))
    if updated is None:
        return None
    age = datetime.now(timezone.utc) - updated
    if age <= _STALE_AFTER:
        return None
    return int(age.total_seconds() // 86400)


class _Group:
    """One group header: its label, its rows, and their precomputed rollup."""

    __slots__ = ("label", "rows", "original", "rollup")

    def __init__(self, label, rows: list):
        self.label = label
        self.rows = list(rows)
        # Kept pristine so reset_sort() can restore insertion order after any
        # number of sort_by() calls.
        self.original = list(rows)
        self.rollup = {
            "stacks": len(rows),
            "units": sum(int(_row_get(r, "quantity") or 0) for r in rows),
            "volume": sum(float(_row_get(r, "volume") or 0) for r in rows),
            "sell_value": sum(float(_row_get(r, "sell_value") or 0) for r in rows),
        }

    def display(self) -> str:
        label = self.label if self.label is not None else "(none)"
        return (
            f"{label} · {self.rollup['stacks']:,} stacks"
            f" · {self.rollup['volume']:,.0f} m³"
            f" · {fmt_short_isk(self.rollup['sell_value'])} ISK"
        )


class GroupedAssetsModel(QAbstractItemModel):
    """Group headers over asset leaves; group_key=None degrades to a flat list."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._columns = ASSET_COLUMNS
        self._keys = [key for key, _ in ASSET_COLUMNS]
        self._group_key: str | None = None
        self._groups: list[_Group] = []
        self._flat: list = []
        self._flat_original: list = []
        self._max_sell = 0.0

    # ---- data plumbing
    def set_rows(self, rows: list, group_key: str | None) -> None:
        self.beginResetModel()
        self._group_key = group_key
        self._groups = []
        self._flat = []
        self._flat_original = []
        if group_key is None:
            self._flat = list(rows)
            self._flat_original = list(rows)
        else:
            buckets: dict = {}
            order = []
            for row in rows:
                label = _row_get(row, group_key)
                if label not in buckets:
                    buckets[label] = []
                    order.append(label)
                buckets[label].append(row)
            self._groups = [_Group(label, buckets[label]) for label in order]
            # Biggest pile first. The sort is stable, so equal-value groups
            # keep their first-appearance order.
            self._groups.sort(key=lambda g: g.rollup["sell_value"], reverse=True)
        self._max_sell = max(
            (float(_row_get(r, "sell_value") or 0) for r in rows), default=0.0
        )
        self.endResetModel()

    def rows(self) -> list:
        """The flat leaf rows in display order, headers excluded."""
        if self._group_key is None:
            return list(self._flat)
        out: list = []
        for group in self._groups:
            out.extend(group.rows)
        return out

    def row_for_index(self, index: QModelIndex):
        """The underlying row behind a leaf index, or None for group headers."""
        if not index.isValid():
            return None
        if self._group_key is None:
            if 0 <= index.row() < len(self._flat):
                return self._flat[index.row()]
            return None
        if index.internalId() == 0:
            return None
        rows = self._groups[index.internalId() - 1].rows
        if 0 <= index.row() < len(rows):
            return rows[index.row()]
        return None

    def _is_header(self, index: QModelIndex) -> bool:
        return self._group_key is not None and index.internalId() == 0

    # ---- QAbstractItemModel structure
    def index(self, row: int, column: int, parent=QModelIndex()) -> QModelIndex:  # noqa: B008
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        if not parent.isValid():
            return self.createIndex(row, column, 0)
        return self.createIndex(row, column, parent.row() + 1)

    def parent(self, index=QModelIndex()) -> QModelIndex:  # noqa: B008 -- Qt signature
        if not index.isValid() or index.internalId() == 0:
            return QModelIndex()
        return self.createIndex(index.internalId() - 1, 0, 0)

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802, B008
        if not parent.isValid():
            return len(self._flat) if self._group_key is None else len(self._groups)
        # Only column 0 of a group header has children -- the Qt convention
        # that keeps a tree view from drawing an expander in every column.
        if self._is_header(parent) and parent.column() == 0:
            return len(self._groups[parent.row()].rows)
        return 0

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802, B008
        return len(self._columns)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return self._columns[section][1]
        return section + 1

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.NoItemFlags
        if self._is_header(index):
            # Headers expand and collapse but never join a selection -- the
            # footer's selection math must only ever sum real rows.
            return Qt.ItemIsEnabled
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemNeverHasChildren

    # ---- data
    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        if self._is_header(index):
            return self._header_data(index, role)
        row = self.row_for_index(index)
        if row is None:
            return None
        key = self._keys[index.column()]

        if role in (HEAT_ROLE, PRICE_AGE_ROLE, PRICE_BADGE_ROLE, Qt.BackgroundRole):
            if key not in _VALUE_KEYS:
                return None
            if role == HEAT_ROLE:
                return self._heat(row, key)
            if role == PRICE_AGE_ROLE:
                return _price_age_days(row)
            if role == PRICE_BADGE_ROLE:
                return self._badge(row)
            fraction = self._heat(row, key)
            if fraction <= 0:
                return None
            tint = palette.heat_tint(fraction)
            return None if tint is None else QBrush(QColor(tint))

        if role in (GROUP_LABEL_ROLE, GROUP_ROLLUP_ROLE):
            return None

        # From here down this mirrors RowTableModel.data exactly, sharing its
        # formatting helpers, so leaves render identically to the flat table.
        raw = _row_get(row, key)
        if role == Qt.DisplayRole:
            if raw is None:
                return ""
            if key in DATE_COLUMNS:
                return fmt_date(raw)
            if key in ISK_COLUMNS:
                return fmt_isk(raw)
            if key in NUMERIC_COLUMNS:
                return fmt_num(raw)
            return str(raw)
        if role == Qt.TextAlignmentRole and key in NUMERIC_COLUMNS:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.UserRole:
            return raw if raw is not None else (0 if key in NUMERIC_COLUMNS else "")
        return None

    def _header_data(self, index: QModelIndex, role):
        group = self._groups[index.row()]
        if role == Qt.DisplayRole:
            return group.display() if index.column() == 0 else ""
        if role == GROUP_LABEL_ROLE:
            return group.label
        if role == GROUP_ROLLUP_ROLE:
            return group.rollup
        return None

    def _badge(self, row) -> str | None:
        """Text for the price badge, or None when the quote needs no caveat.

        'manual' outranks the age badge on purpose: a pinned price does not go
        stale in the market sense -- the repricer refuses to touch it -- so
        showing '10d' on it would nag the user about a number they chose.
        """
        source = _row_get(row, "price_source")
        if source == "none":
            return "unpriced"
        if source == "manual":
            return "manual"
        days = _price_age_days(row)
        if days is not None:
            return f"{days}d"
        return None

    def _heat(self, row, key: str) -> float:
        """Log-scaled share of this cell's value against the biggest current
        leaf's sell value.

        Log rather than linear because asset values span six-plus orders of
        magnitude: on a linear scale one titan makes every other row visually
        identical at zero.
        """
        if _row_get(row, "price_source") == "none":
            return 0.0
        try:
            value = float(_row_get(row, key) or 0)
        except (TypeError, ValueError):
            return 0.0
        if value <= 0 or self._max_sell <= 0:
            return 0.0
        if self._max_sell <= 1.0:
            # Nothing tops one ISK, so log10 has no room to scale in; the
            # only remaining signal is "this is the biggest thing here".
            return 1.0 if value >= self._max_sell else 0.0
        fraction = math.log10(max(value, 1.0)) / math.log10(self._max_sell)
        return min(max(fraction, 0.0), 1.0)

    # ---- sorting
    def sort_by(self, key: str, order: Qt.SortOrder = Qt.AscendingOrder) -> None:
        """Sort leaves by raw value within every group; groups do not move."""
        reverse = order == Qt.DescendingOrder
        if key in NUMERIC_COLUMNS:
            def sort_key(row):
                try:
                    return float(_row_get(row, key) or 0)
                except (TypeError, ValueError):
                    return 0.0
        else:
            def sort_key(row):
                value = _row_get(row, key)
                return "" if value is None else str(value).casefold()
        self._reorder(lambda rows: sorted(rows, key=sort_key, reverse=reverse))

    def reset_sort(self) -> None:
        """Restore the insertion order set_rows() was handed."""
        self._reorder(list)

    def _reorder(self, arrange) -> None:
        """Rearrange every group's leaves via layoutChanged, not a reset.

        A reset would collapse each expanded group on every header click.
        Persistent indexes are remapped by the identity of the row object they
        pointed at: rows never cross groups during a sort, so a leaf's new
        position is always within the same parent. arrange() always works from
        the pristine insertion-order copy -- sorting is a total reordering
        either way, and it makes reset_sort() the trivial case.
        """
        self.layoutAboutToBeChanged.emit()
        persistent = self.persistentIndexList()
        remembered = [(idx, self.row_for_index(idx)) for idx in persistent]

        if self._group_key is None:
            self._flat = arrange(self._flat_original)
        else:
            for group in self._groups:
                group.rows = arrange(group.original)

        position: dict[int, int] = {}
        if self._group_key is None:
            for pos, row in enumerate(self._flat):
                position[id(row)] = pos
        else:
            for group in self._groups:
                for pos, row in enumerate(group.rows):
                    position[id(row)] = pos
        replacements = []
        for idx, row in remembered:
            if row is None:
                # Group headers (and anything already invalid) did not move.
                replacements.append(QModelIndex(idx))
            else:
                replacements.append(
                    self.createIndex(position[id(row)], idx.column(), idx.internalId())
                )
        self.changePersistentIndexList(persistent, replacements)
        self.layoutChanged.emit()
