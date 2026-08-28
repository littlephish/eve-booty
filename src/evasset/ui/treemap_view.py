"""The Overview tab's numbers as a picture: one rectangle per group, sized by
value, so "where is my ISK" is a glance rather than a read.

Same query as OverviewView (queries.location_totals) and the same grouping
levels, deliberately -- this is a second rendering of one dataset, not a
second dataset. Right-clicking a tile emits the same filter_assets_requested
signal the Overview table does, so click-through to a filtered Assets tab
behaves identically whichever view you came from.

The layout maths lives in evasset.treemap (Qt-free, unit tested); this file
is the chrome, the painting and the hit testing. QtCharts is used elsewhere
in the app for the net worth line chart but has no treemap of its own, hence
the hand-rolled paint event.
"""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import queries, treemap
from .async_query import AsyncQuery
from .models import fmt_isk, fmt_short_isk
from .palette import (
    SECONDARY_TEXT,
    TREEMAP_OTHER_FILL,
    readable_text_on,
    treemap_fill,
)

# Below these a tile gets no label at all -- a truncated one-character string
# is noise, and the tooltip already covers small tiles.
_MIN_LABEL_WIDTH = 54
_MIN_LABEL_HEIGHT = 30
# A second line for the value only when there is comfortably room for it.
_MIN_VALUE_HEIGHT = 46


class _Canvas(QWidget):
    """Paints the tiles and hit-tests the mouse. Split from TreemapView so
    the widget being painted is exactly the drawing area -- the layout is
    computed against its own size, with no toolbar height to subtract."""

    tile_context_menu = Signal(object, object)  # (Tile, QPoint in global coords)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._items: list[tuple[str, float]] = []
        self._tiles: list[treemap.Tile] = []
        self._layout_size = None
        self._hover: treemap.Tile | None = None
        self._total = 0.0
        self._top_n: int | None = None
        self._empty_message = "Nothing to show yet."
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(200)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

    def set_items(self, items: list[tuple[str, float]], top_n: int | None) -> None:
        self._items = items
        self._top_n = top_n
        self._total = sum(v for _, v in items if v > 0)
        self._invalidate()

    def set_empty_message(self, message: str) -> None:
        self._empty_message = message
        self.update()

    def _invalidate(self) -> None:
        # Drop the old tiles rather than leaving them to be overwritten on the
        # next paint. Nothing reads them stale today (both paintEvent and
        # tile_at re-layout first), but "the widget still holds tiles for data
        # it no longer has" is the kind of state that grows a bug later.
        self._tiles = []
        self._layout_size = None
        self._hover = None
        self.update()

    def _ensure_layout(self) -> None:
        """Lay out once per (size, data) rather than once per paint. Resizing
        a window fires paintEvent continuously, and squarifying a few hundred
        tiles on every frame is work for nothing."""
        size = (self.width(), self.height())
        if self._layout_size == size:
            return
        self._tiles = treemap.layout(self._items, self.width(), self.height(), self._top_n)
        self._layout_size = size

    def tile_at(self, x: float, y: float) -> treemap.Tile | None:
        self._ensure_layout()
        for tile in self._tiles:
            if tile.contains(x, y):
                return tile
        return None

    # ------------------------------------------------------------- painting
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt's name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        self._ensure_layout()

        if not self._tiles:
            painter.setPen(self.palette().color(QPalette.Shadow))
            painter.drawText(self.rect(), Qt.AlignCenter, self._empty_message)
            return

        # Tile edges are drawn in the window background so tiles read as
        # separate blocks in either theme without a colour of their own.
        gap = QPen(self.palette().color(QPalette.Window), 1)
        metrics = QFontMetrics(self.font())

        for index, tile in enumerate(self._tiles):
            rect = QRectF(tile.x, tile.y, tile.width, tile.height)
            if rect.width() < 1 or rect.height() < 1:
                continue

            fill = TREEMAP_OTHER_FILL if _is_other(tile) else treemap_fill(index)
            colour = QColor(fill)
            if tile is self._hover:
                colour = colour.lighter(118)
            painter.fillRect(rect, colour)
            painter.setPen(gap)
            painter.drawRect(rect)

            if rect.width() < _MIN_LABEL_WIDTH or rect.height() < _MIN_LABEL_HEIGHT:
                continue

            painter.setPen(QColor(readable_text_on(fill)))
            inner = rect.adjusted(6, 4, -6, -4)
            label = metrics.elidedText(tile.label, Qt.ElideRight, int(inner.width()))
            if rect.height() >= _MIN_VALUE_HEIGHT:
                painter.drawText(inner, Qt.AlignLeft | Qt.AlignTop, label)
                value = metrics.elidedText(
                    fmt_short_isk(tile.value), Qt.ElideRight, int(inner.width())
                )
                painter.drawText(inner, Qt.AlignLeft | Qt.AlignBottom, value)
            else:
                painter.drawText(inner, Qt.AlignLeft | Qt.AlignVCenter, label)

    # ---------------------------------------------------------------- mouse
    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt's name
        position = event.position()
        tile = self.tile_at(position.x(), position.y())
        if tile is not self._hover:
            self._hover = tile
            self.update()
        if tile is None:
            self.setToolTip("")
            return
        share = (tile.value / self._total * 100) if self._total > 0 else 0.0
        self.setToolTip(
            f"{tile.label}\n{fmt_isk(tile.value)} ISK · {share:.1f}% of the total"
        )

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt's name
        if self._hover is not None:
            self._hover = None
            self.update()

    def _on_context_menu(self, pos) -> None:
        tile = self.tile_at(pos.x(), pos.y())
        if tile is not None:
            self.tile_context_menu.emit(tile, self.mapToGlobal(pos))


def _is_other(tile: treemap.Tile) -> bool:
    """The rolled-up tail is not a real group, so it gets no colour of its own
    and cannot be filtered on."""
    return tile.label.startswith("Other (")


class TreemapView(QWidget):
    """Assets by value, as area. Group by the same levels the Overview table
    offers."""

    LEVELS = queries.ROLLUP_LEVELS

    # (level_key, value), same contract as OverviewView.filter_assets_requested
    # so MainWindow can hand both to AssetsView.apply_external_filter().
    filter_assets_requested = Signal(str, str)

    # (label, column key) for the value the tiles are sized by.
    BASES = [("Sell value", "sell_value"), ("Buy value", "buy_value")]

    # Past roughly two dozen tiles the small ones stop being labellable, so
    # the default rolls the tail up rather than drawing a band of slivers.
    TOP_N_CHOICES = [("Top 20", 20), ("Top 40", 40), ("Top 80", 80), ("Everything", None)]

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        defer_load: bool = False,
    ):
        super().__init__(parent)

        root = QVBoxLayout(self)
        bar = QHBoxLayout()

        bar.addWidget(QLabel("Group by"))
        self.level = QComboBox()
        for label, _ in self.LEVELS:
            self.level.addItem(label)
        # Item group is what the tab is for -- "implants, ammo, ships" -- so it
        # opens there rather than on the table's location-first default.
        self.level.setCurrentIndex(_index_of(self.LEVELS, "group"))
        bar.addWidget(self.level)

        bar.addWidget(QLabel("Sized by"))
        self.basis = QComboBox()
        for label, _ in self.BASES:
            self.basis.addItem(label)
        bar.addWidget(self.basis)

        bar.addWidget(QLabel("Show"))
        self.top_n = QComboBox()
        for label, _ in self.TOP_N_CHOICES:
            self.top_n.addItem(label)
        bar.addWidget(self.top_n)

        bar.addStretch(1)
        self.summary = QLabel("")
        bar.addWidget(self.summary)
        root.addLayout(bar)

        self.canvas = _Canvas()
        self.canvas.tile_context_menu.connect(self._show_tile_menu)
        root.addWidget(self.canvas, 1)

        hint = QLabel("Right-click a tile to filter the Assets tab to it.")
        hint.setStyleSheet(f"color: {SECONDARY_TEXT};")
        root.addWidget(hint)

        self.level.currentIndexChanged.connect(self.reload)
        self.basis.currentIndexChanged.connect(self.reload)
        self.top_n.currentIndexChanged.connect(self._relayout)

        self._rows: list[sqlite3.Row] = []
        self._query = AsyncQuery(self)
        if not defer_load:
            self.first_load()

    # ----------------------------------------------------------------- data
    def current_level(self) -> str:
        return self.LEVELS[self.level.currentIndex()][1]

    def current_basis(self) -> str:
        return self.BASES[self.basis.currentIndex()][1]

    def current_top_n(self) -> int | None:
        return self.TOP_N_CHOICES[self.top_n.currentIndex()][1]

    def first_load(self) -> None:
        self.reload()

    def reload(self) -> None:
        level = self.current_level()

        def fetch(conn: sqlite3.Connection) -> list[sqlite3.Row]:
            return queries.location_totals(conn, level)

        self._query.run(fetch, self._on_rows, self._on_query_failed)

    def _on_rows(self, rows: list[sqlite3.Row]) -> None:
        self._rows = rows
        self.canvas.set_empty_message(
            "Nothing priced to show. Sync, then price, and this fills in."
        )
        self._relayout()

    def _on_query_failed(self, message: str) -> None:
        self._rows = []
        self.canvas.set_items([], None)
        self.canvas.set_empty_message(f"Query failed: {message}")
        self.summary.setText("")

    def _relayout(self) -> None:
        """Re-tile from rows already in hand. Changing "show top N" is a
        presentation choice, so it must not re-run the query."""
        basis = self.current_basis()
        items = [(row["label"], float(row[basis] or 0)) for row in self._rows]
        drawn = treemap.roll_up(items, self.current_top_n())
        self.canvas.set_items(items, self.current_top_n())

        total = sum(value for _, value in items if value > 0)
        priced = sum(1 for _, value in items if value > 0)
        basis_label = self.BASES[self.basis.currentIndex()][0].lower()
        self.summary.setText(
            f"{priced:,} groups · {len(drawn):,} tiles · "
            f"{basis_label} <b>{fmt_short_isk(total)}</b> ({fmt_isk(total)} ISK)"
        )

    # ------------------------------------------------------------------ ui
    def reset_sort(self) -> None:
        """No table here, but MainWindow's View -> Reset sort walks every tab
        and calls this if it exists. Resetting to the default grouping is the
        closest thing this view has to a default sort."""
        self.level.setCurrentIndex(_index_of(self.LEVELS, "group"))
        self.top_n.setCurrentIndex(0)

    def menu_for_tile(self, tile: treemap.Tile) -> QMenu | None:
        """The context menu for a tile, or None when there is nothing to
        offer. Separate from showing it so the decision (and the action it
        wires up) can be tested without a modal exec() that would hang."""
        if _is_other(tile):
            return None
        menu = QMenu(self)
        level = self.current_level()
        action = menu.addAction(f'Filter Assets to "{tile.label}"')
        action.triggered.connect(
            lambda: self.filter_assets_requested.emit(level, tile.label)
        )
        return menu

    def _show_tile_menu(self, tile: treemap.Tile, global_pos) -> None:
        menu = self.menu_for_tile(tile)
        if menu is not None:
            menu.exec(global_pos)


def _index_of(levels: list[tuple[str, str]], key: str) -> int:
    for index, (_label, level_key) in enumerate(levels):
        if level_key == key:
            return index
    return 0
