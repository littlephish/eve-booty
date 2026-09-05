"""Asset browser: an omnibox-filtered, groupable table across every character
and corp, with a rollup rail, a whole-estate strip and a row inspector that
shows in a panel beside the table (click, Enter) or pinned in its own window
(right-click -> Inspect in window).

Every control that narrows the table speaks one verb -- add an omnibox chip.
Rail rows, value-map segments, context-menu items and the f/x keys all end up
in Omnibox.add_chip, so omni.FilterSpec is the single source of filter truth
and there is no second WHERE-building code path to keep in step (the old
combos, checkboxes and GroupPanel each owned a fragment of it).
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import (
    QItemSelectionModel,
    QModelIndex,
    QObject,
    QPoint,
    QSortFilterProxyModel,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QDesktopServices,
    QFont,
    QGuiApplication,
    QKeySequence,
    QPalette,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QStyledItemDelegate,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from .. import abyssal, db, omni, pricing, queries
from ..config import Settings
from ..esi import ESIClient, TokenCache
from . import chest_reveal, palette
from .abyssal_card import CARD_KINDS, AbyssalCard
from .async_query import AsyncQuery
from .debounce import Debounce
from .fit_dialog import FitDialog
from .grouped_model import (
    GROUP_LABEL_ROLE,
    PRICE_BADGE_ROLE,
    ROLL_MEAN_KEY,
    GroupedAssetsModel,
    _row_get,
    is_roll_key,
    roll_key,
)
from .inspector import Inspector, InspectorWindow
from .models import fmt_isk, fmt_short_isk
from .omnibox import Omnibox
from .rail import Rail
from .strip import EstateStrip
from .workers import AppraiseJob, Job

# "View fit" is offered for rows in this SDE category. Scoped to just Ship --
# what a player structure's category is named in the SDE was not checked, so
# it is not being guessed at here.
_FIT_VIEWABLE_CATEGORIES = {"Ship"}

# Corpses. SDE group 14, "Biomass", category "Celestial", and it holds exactly
# two types: Corpse Male (25) and Corpse Female (29148). Matched on the group
# rather than those ids because the group is the thing that means "a dead
# capsuleer", and CCP adding a third would still be one.
#
# Not to be confused with the "Corpse" types under Commodities -- Gallente
# Admiral's Corpse and friends are mission loot, not people.
_CORPSE_GROUP = "Biomass"

# Table column key -> omnibox chip kind, for the context menu and the f/x
# keys. The item column is deliberately absent: filtering to one exact item
# name is what "Where else is this?" does better (it also flips the rail).
_COLUMN_CHIP_KIND = {
    "owner": "owner",
    "grp": "group",
    "category": "category",
    "meta": "meta",
    "location": "location",
    "system": "system",
    "region": "region",
}

# Grouping level key (queries.ROLLUP_LEVELS) -> GroupedAssetsModel row key. Only
# "group" differs, because ASSET_ROWS dodges the SQL keyword with "grp".
_GROUP_ROW_KEY = {
    "location": "location",
    "system": "system",
    "region": "region",
    "owner": "owner",
    "category": "category",
    "group": "grp",
}

# The two value columns the badge delegate paints.
_BADGE_KEYS = {"buy_value", "sell_value"}

_KEY_MAP = [
    ("/", "Focus the omnibox"),
    ("Ctrl+F", "Build a filter chip (pick a type, then a value)"),
    ("j / k", "Next / previous row"),
    ("Space", "Toggle selection on the current row"),
    ("f", "Filter to the focused cell"),
    ("x", "Exclude the focused cell"),
    ("w", "Where else is this item?"),
    ("Enter", "Open the inspector panel"),
    ("g", "Cycle group-by"),
    ("Esc", "Close the inspector panel, else remove the last filter"),
    ("1-9", "Recall saved view"),
    ("Ctrl+1-9", "Save current view"),
    ("?", "This list"),
]


def _is_corpse(row) -> bool:
    """A dead capsuleer, rather than a mission-loot body."""
    return (row["grp"] or "") == _CORPSE_GROUP


class _SortProxy(QSortFilterProxyModel):
    """Sort on the raw value, not the formatted string, so 1,000,000 does not
    sort before 9.

    That used to be a hand-written lessThan() calling back into
    RowTableModel.data() for both sides of every comparison. Qt's own default
    lessThan does exactly the same thing -- compare whatever sortRole()
    returns -- but in C++, and RowTableModel.data(Qt.UserRole) already hands
    back plain, never-None floats/ints/strings (see RowTableModel.data), so
    there is nothing left for a Python override to add. Setting sortRole is
    enough on its own.

    The assets table itself no longer uses this (GroupedAssetsModel sorts
    in-model); it stays here because the wallet, structures and stockpile
    views all import it, and this rebuild does not touch their files.
    """


class _ValueBadgeDelegate(QStyledItemDelegate):
    """Paints the price badge ("unpriced", "manual", "6d") into a value cell.

    The badge rides in the cell's left half because the numbers are
    right-aligned, so the left half is empty in every row wide enough to
    read; a dedicated badge column would spend permanent width on a caveat
    most rows do not carry.
    """

    def paint(self, painter, option, index) -> None:
        super().paint(painter, option, index)
        badge = index.data(PRICE_BADGE_ROLE)
        if not badge:
            return
        painter.save()
        font = QFont(option.font)
        font.setPointSizeF(max(font.pointSizeF() - 1.5, 6.0))
        painter.setFont(font)
        # The same subdued role SECONDARY_TEXT names for stylesheets.
        painter.setPen(option.palette.color(QPalette.Shadow))
        painter.drawText(
            option.rect.adjusted(4, 0, -4, 0), int(Qt.AlignLeft | Qt.AlignVCenter), badge
        )
        painter.restore()


class _TreeSortController(QObject):
    """SortController's click-to-sort discipline, retargeted at the tree model.

    The proxy-based SortController cannot be reused as-is: GroupedAssetsModel
    sorts itself (sort_by keeps leaves inside their groups, which a proxy
    cannot know to do), so the header drives the model directly. That is the
    same conclusion SortController reached for the flat tables, and for the
    same reason -- sorting through a proxy means Qt calls data() on the Python
    model twice per comparison, which is the difference between a sort that
    finishes and one that reads as a hung app. The wait cursor stays because
    the sort is still synchronous (Qt models cannot be touched from a worker
    thread), not because it is slow.

    The sort is remembered by column KEY, not index. The roll columns come
    and go with the abyssal filter, shifting every column after Qty, so an
    index remembered across that change would quietly sort by whatever now
    sits at the old position; the key is re-resolved through model.key_at on
    every reapply, and a key whose column has vanished clears the sort.
    """

    def __init__(self, tree: QTreeView, model: GroupedAssetsModel, parent=None):
        super().__init__(parent)
        self.tree = tree
        self.model = model
        self.key: str | None = None
        # Where the key's column currently is, -1 when unsorted; derived in
        # reapply and kept for the header indicator and for callers that
        # only need to know whether a sort is active.
        self.column = -1
        self.order = Qt.AscendingOrder
        header = tree.header()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self._on_header_clicked)

    def _on_header_clicked(self, column: int) -> None:
        key = self.model.key_at(column)
        if key is None:
            return
        if key == self.key:
            self.order = (
                Qt.DescendingOrder if self.order == Qt.AscendingOrder else Qt.AscendingOrder
            )
        else:
            self.key = key
            self.order = Qt.AscendingOrder
        self.reapply()

    def reapply(self) -> None:
        """Apply the remembered sort; also called after every set_rows so a
        chosen order survives reloads the way the old proxy's did."""
        keys = [key for key, _header in self.model.columns()]
        self.column = keys.index(self.key) if self.key in keys else -1
        if self.column < 0:
            self.key = None
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            if self.key is None:
                self.model.reset_sort()
            else:
                self.model.sort_by(self.key, self.order)
        finally:
            QApplication.restoreOverrideCursor()
        self.tree.header().setSortIndicator(self.column, self.order)

    def reset(self) -> None:
        """Back to insertion order, arrow cleared. Wired to View -> Reset
        sort in MainWindow."""
        self.key = None
        self.order = Qt.AscendingOrder
        self.reapply()


def _single_abyssal_type(spec: omni.FilterSpec) -> str | None:
    """The one module type the positive abyssal chips name, or None when
    they name none (every type), several, or there is no abyssal chip. This
    is the gate for the roll columns: each type rolls its own attribute set,
    so only a single-type table has columns that mean the same thing in
    every row."""
    names: list[str] = []
    for chip in spec.chips:
        if chip.kind != omni.ABYSSAL_KIND or chip.negated:
            continue
        types = omni.split_types(chip.value)
        if not types:
            return None
        names.extend(t for t in types if t not in names)
    return names[0] if len(names) == 1 else None


class _PriceRefreshJob(Job):
    """Fetch one type's Jita quote and store it, off the GUI thread.

    A full RepriceJob walks everything owned; the inspector's button is "this
    one number looks wrong, ask again", so it fetches a single type. Manual
    pins survive automatically -- store_prices refuses to overwrite them.
    """

    def __init__(self, type_id: int):
        super().__init__()
        self.type_id = type_id

    def run_job(self):
        quotes = pricing.fetch_jita([self.type_id], Settings.load())
        conn = db.init()
        pricing.store_prices(conn, quotes)
        return {"priced": len(quotes)}


class _AbyssalFetchJob(Job):
    """Fetch one abyssal item's rolled attributes from ESI, off the GUI thread.

    The Update menu's AbyssalStatsJob walks every unfetched item; the
    inspector's button is "this one, now", so it asks for a single item.
    retry_missing is on because the button reads Retry once ESI has 404'd
    the item -- the user is asking again on purpose. No token is needed
    (the dynamic-item route is public), but the client wants a cache to
    exist, so an empty one is built rather than a second client type.
    """

    def __init__(self, item_id: int):
        super().__init__()
        self.item_id = item_id

    def run_job(self):
        settings = Settings.load()
        conn = db.init()
        client = ESIClient(settings, TokenCache(settings))
        try:
            return abyssal.fetch_rolls(
                conn, client, item_ids=[self.item_id], retry_missing=True
            )
        finally:
            client.close()


@dataclass(eq=False)
class _InspectorHost:
    """One place an Inspector lives -- the panel or the window -- with the
    state the view keeps per place.

    The two hosts show different rows at the same time (the window pins an
    item while the panel follows the clicks), so the row on show and the
    rolls lookup are per host. One shared rolls AsyncQuery would let the
    window's lookup bump the generation and silently drop the panel's, and a
    single shared "inspected row" guard would let the window's rolls paint
    into the panel. eq=False keeps identity semantics: hosts are compared by
    which one they are, never by what they currently show.
    """

    inspector: Inspector
    rolls_query: AsyncQuery
    render: Callable[[sqlite3.Row], None]
    show: Callable[[], None]
    hide: Callable[[], None]
    is_open: Callable[[], bool]
    row: sqlite3.Row | None = None


class AssetsView(QWidget):
    # item_ids whose abyssal rolls the card's Fetch button asked for; the
    # main window owns the job registry, so it submits the AbyssalStatsJob.
    abyssal_fetch_requested = Signal(list)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        defer_load: bool = False,
    ):
        super().__init__(parent)
        # Reads go through AsyncQuery, which opens its own connection on the
        # pool thread it runs on. This one is for the writes and lookups that
        # happen on the GUI thread (pinning a price, the inspector). No
        # caller passes it in: db.connect() caches one connection per thread,
        # so this is the very object MainWindow's db.init() already created.
        self.conn = db.connect()

        root = QVBoxLayout(self)

        # The estate strip leads the tab: its headline figures describe the
        # whole estate and never change with the filter, so they belong above
        # the controls that narrow the table, not between those and the table.
        self.strip = EstateStrip()
        root.addWidget(self.strip)

        # The omnibox has the whole first line to itself: it wraps chips onto
        # further lines as filters pile up, and a neighbour on that line would
        # either float mid-field or steal width the chips need.
        self.omnibox = Omnibox()
        root.addWidget(self.omnibox)

        state_row = QHBoxLayout()
        self.state_label = QLabel("")
        self.state_label.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        state_row.addWidget(self.state_label)

        # Hidden unless the table is empty for a reason the user cannot see.
        # It sits on the state row, beside the stack count, because that is
        # where the eye already is when the number reads zero.
        self.empty_hint = QLabel("")
        self.empty_hint.setTextFormat(Qt.RichText)
        self.empty_hint.setVisible(False)
        state_row.addWidget(self.empty_hint)
        self.clear_all_btn = QPushButton("Clear all")
        # A neutral pill rather than a flat button: flat blended into the
        # window and only looked clickable under the pointer, and this is the
        # one control that resets the whole filter, so it has to be findable.
        self.clear_all_btn.setStyleSheet(
            palette.pill_stylesheet("QPushButton", self.palette())
        )
        self.clear_all_btn.setCursor(Qt.PointingHandCursor)
        self.clear_all_btn.setVisible(False)
        state_row.addWidget(self.clear_all_btn)
        state_row.addStretch(1)
        state_row.addWidget(QLabel("Group by"))
        self.group_combo = QComboBox()
        # "" rather than None as the flat entry's data: QComboBox.findData
        # against None is unreliable across bindings, and saved views store
        # the key as a plain string anyway.
        self.group_combo.addItem("None", "")
        for label, key in queries.ROLLUP_LEVELS:
            self.group_combo.addItem(label, key)
        state_row.addWidget(self.group_combo)
        # Export lives on the state row, after Group by: it acts on the table
        # as currently filtered and grouped, so it sits with the controls
        # that describe that state.
        self.export_btn = QPushButton("Export CSV…")
        state_row.addWidget(self.export_btn)
        root.addLayout(state_row)

        self.model = GroupedAssetsModel(self)
        self.tree = QTreeView()
        self.tree.setModel(self.model)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.setUniformRowHeights(True)
        self.tree.setRootIsDecorated(False)  # flat until a group-by is chosen
        self.tree.header().setSectionResizeMode(QHeaderView.Interactive)
        self.tree.header().setStretchLastSection(True)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        # Mouse routes into the panel. Enter was the only way in, which left
        # it undiscoverable for anyone not reading the footer hint; a single
        # click follows the row the user is looking at, the double-click
        # stays for anyone who learned it that way.
        self.tree.clicked.connect(self._open_panel_at)
        self.tree.doubleClicked.connect(self._open_panel_at)
        self._badge_delegate = _ValueBadgeDelegate(self.tree)
        # An explicit default for every other column, so a value column that
        # shifts right when the roll columns appear leaves a plain delegate
        # at its old index rather than the badge painter.
        self._plain_delegate = QStyledItemDelegate(self.tree)
        self._apply_delegates()

        # The rail and the panel inspector share the splitter slot: for a
        # quick look at the clicked row only one of them is useful at a
        # time, and the stack means the panel inherits the rail's width and
        # resize behaviour for free. The window is the inspector's other
        # host (see _InspectorHost): one for the tab's lifetime, hidden
        # rather than destroyed between rows so it keeps the size and place
        # the user gave it, and parented here so it dies with the tab.
        self.rail = Rail()
        self.inspector = Inspector()
        self.side = QStackedWidget()
        self.side.addWidget(self.rail)
        self.side.addWidget(self.inspector)
        self.inspector_window = InspectorWindow(self)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self.tree)
        self.splitter.addWidget(self.side)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([1180, 260])  # roughly window width minus the rail
        root.addWidget(self.splitter, 1)
        self.sorter = _TreeSortController(self.tree, self.model, self)

        foot = QHBoxLayout()
        self.footer = QLabel("")
        foot.addWidget(self.footer, 1)
        self.copy_btn = QPushButton("Copy list")
        self.copy_btn.setToolTip("Copy as EVE multibuy text: item name, tab, quantity")
        foot.addWidget(self.copy_btn)
        self.appraise_btn = QPushButton("Appraise")
        self.appraise_btn.setToolTip("Copy the multibuy text and open Janice")
        foot.addWidget(self.appraise_btn)
        self.key_hints = QLabel("j k rows · space select · ↵ inspect · ? all keys")
        self.key_hints.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        foot.addWidget(self.key_hints)
        root.addLayout(foot)

        self._total_stacks = 0
        self._sized_once = False
        self._last_group_key: str | None = None
        # The roll columns and cells of the current single-type filter, kept
        # on the view so a regroup (which re-buckets the same rows without a
        # reload) hands the model the same extras again.
        self._extra_columns: list[tuple[str, str]] = []
        self._cells: dict = {}
        self._cell_attrs: list[dict] = []
        # The complex-search card, built on first use and reused: one popup
        # per view, re-seeded on every open. _card_types maps the type names
        # it shows to type ids for the Fetch button's item lookup.
        self._card: AbyssalCard | None = None
        self._card_types: dict[str, int] = {}
        # Strong references per the QRunnable lifetime rules in
        # async_query.py: a price job must outlive its starting call.
        self._price_jobs: set[_PriceRefreshJob] = set()
        self._appraise_jobs: set = set()
        self._vocab_query = AsyncQuery(self)
        self._abyssal_jobs: set[_AbyssalFetchJob] = set()

        # Distinct AsyncQuery instances per query stream, same reason the old
        # view held three: reload() starts the row fetch and the rail fetch
        # back to back, and a shared instance would bump the generation so
        # only whichever started last ever delivered.
        self._query = AsyncQuery(self)
        self._rail_query = AsyncQuery(self)
        self._strip_query = AsyncQuery(self)
        # The inspectors' rolls lookups get their own streams too, one per
        # host: each starts from show_row while a row reload may be in
        # flight, must be cancellable on its own when that host closes, and
        # must not drop the other host's lookup by bumping its generation.
        self._rolls_query = AsyncQuery(self)
        self._window_rolls_query = AsyncQuery(self)
        # The card's own stream: type counts, attributes and bounds arrive
        # while a row reload may be in flight, and a re-tick in the card
        # must supersede the previous type's attribute fetch, not the table.
        self._card_query = AsyncQuery(self)
        # The card's live match count on a stream of its own: it is asked
        # after every handle move, and on the attribute stream a count
        # started during a type switch would supersede the attribute fetch
        # and leave the rows on the previous type's bounds. Debounced below
        # the omnibox's 220 ms because a drag emits per pixel and the footer
        # should settle within the same pause a keystroke does.
        self._card_count_query = AsyncQuery(self)
        self._card_count = Debounce(self, self._fetch_card_count, interval=120)
        self._panel_host = _InspectorHost(
            self.inspector,
            self._rolls_query,
            render=self.inspector.show_row,
            show=lambda: self.side.setCurrentWidget(self.inspector),
            hide=lambda: self.side.setCurrentWidget(self.rail),
            is_open=lambda: self.side.currentWidget() is self.inspector,
        )
        self._window_host = _InspectorHost(
            self.inspector_window.inspector,
            self._window_rolls_query,
            render=self.inspector_window.show_row,
            show=self._raise_window,
            hide=self.inspector_window.hide,
            is_open=self.inspector_window.isVisible,
        )

        self.omnibox.changed.connect(self.reload)
        self.omnibox.card_requested.connect(self._open_card)
        # The omnibox rung of the escape ladder sheds the panel; the window
        # is closed on its own terms (its Esc, its title bar, its × button).
        self.omnibox.escape_pressed.connect(lambda: self._close_inspector(self._panel_host))
        self.clear_all_btn.clicked.connect(self.omnibox.clear)
        self.group_combo.currentIndexChanged.connect(self._on_group_changed)
        self.export_btn.clicked.connect(self.export_csv)
        self.copy_btn.clicked.connect(self.copy_list)
        self.appraise_btn.clicked.connect(self.appraise)
        self.tree.selectionModel().selectionChanged.connect(lambda *_: self._update_footer())

        self.rail.chip_requested.connect(self.omnibox.add_chip)
        self.rail.level_changed.connect(lambda _level: self._refresh_rail())
        self.rail.refresh_needed.connect(self._refresh_rail)
        self.rail.pin_toggled.connect(self._on_pin_toggled)

        self.strip.unpriced_clicked.connect(lambda: self.omnibox.add_chip("is", "unpriced"))
        self.strip.location_clicked.connect(
            lambda label: self.omnibox.add_chip("location", label)
        )

        for host in (self._panel_host, self._window_host):
            self._wire_inspector(host)
        # Every way out of the window runs the same cleanup: the × button
        # (wired above), Esc inside it and the title bar's close all end in
        # _close_inspector, the last two by way of QDialog.reject -> finished.
        self.inspector_window.finished.connect(
            lambda _code: self._close_inspector(self._window_host)
        )

        self._build_shortcuts()

        if not defer_load:
            self.first_load()

    # ------------------------------------------------------------- lifecycle
    def reset_sort(self) -> None:
        self.sorter.reset()

    def first_load(self) -> None:
        """Kick off the first real queries.

        Split out from __init__ so MainWindow can skip this for every tab
        that is not on screen yet -- each tab only pays its query cost the
        first time it is actually looked at, and even then off the GUI
        thread (see reload()).
        """
        self.refresh_all()

    def refresh_all(self) -> None:
        """Everything current after a data change: estate strip, rows, rail.

        MainWindow's reload path for this tab. The strip is not refreshed by
        plain filter changes -- it is filter-independent by design -- so it
        only re-queries here and on first load.
        """
        self.refresh_strip()
        self.refresh_vocabulary()
        self.reload()

    def refresh_vocabulary(self) -> None:
        """Tell the omnibox which filter values actually exist.

        Only used to resolve unquoted multi-word values as they are typed, so
        it runs off the GUI thread and nothing waits on it: until it arrives,
        those values need quoting, exactly as they always did.

        Unfaceted on purpose. The value pickers list what the current filter
        still leaves, because picking from them should never lead to an empty
        table; this is the opposite job. Somebody typing owner:Test Pilot
        should get a chip whether or not the filters already on screen happen
        to leave that owner any rows -- otherwise a filter would refuse to
        parse because of a filter.
        """
        def fetch(conn):
            # ROLLUP_LEVELS is (label, key) pairs; the key is what
            # omni.parse indexes its vocabulary by.
            return {
                kind: queries.group_names(conn, kind)
                for _label, kind in queries.ROLLUP_LEVELS
            }

        self._vocab_query.run(fetch, self.omnibox.set_vocabulary)

    # ----------------------------------------------------------------- reload
    def reload(self) -> None:
        spec = self.omnibox.spec()
        where, params = spec.where()
        single_type = _single_abyssal_type(spec)

        def fetch(conn: sqlite3.Connection):
            rows = queries.fetch_assets(conn, where, params)
            total = conn.execute("SELECT COUNT(*) AS c FROM assets").fetchone()["c"]
            # The badge tooltips and the roll cells for every abyssal row on
            # the table, from one batched query alongside the rows they
            # describe -- so a fetch that just completed shows up on the
            # very reload it triggers, and hovering never queries. Column
            # probed once: a row source without it (older SELECT shapes)
            # simply has no abyssal rows.
            summaries: dict[int, str] = {}
            cells: dict = {}
            if rows and "is_dynamic_type" in rows[0].keys():
                abyssal_ids = [r["item_id"] for r in rows if r["is_dynamic_type"]]
                if abyssal_ids:
                    summaries, cells = queries.abyssal_roll_data(conn, abyssal_ids)
            # With the filter down to one module type the table grows a
            # column per rolled attribute the estate has values for, plus the
            # mean-quality Roll column. A type with nothing fetched yet grows
            # none at all -- not even Roll, which would be a column of blanks
            # -- until a fetch gives it something to show. The cells are
            # fetched regardless (they are the same rows the summaries come
            # from) and simply go unread without the columns.
            attrs: list[dict] = []
            extra: list[tuple[str, str]] = []
            if single_type is not None:
                attrs, _bounds = queries.abyssal_type_columns(conn, single_type)
                if attrs:
                    extra = [
                        (roll_key(a["attribute_id"]), abyssal.short_label(a["name"], a["label"]))
                        for a in attrs
                    ]
                    extra.append((ROLL_MEAN_KEY, "Roll"))
            return rows, total, summaries, extra, cells, attrs

        self._query.run(fetch, self._on_rows, self._on_query_failed)
        self._refresh_rail()

    def _on_rows(self, payload) -> None:
        rows, total, summaries, extra, cells, attrs = payload
        self._total_stacks = total
        self.model.set_abyssal_summaries(summaries)
        self._extra_columns = list(extra)
        self._cells = cells
        self._cell_attrs = attrs
        self.model.set_abyssal_cells(cells, attrs)
        self._apply_rows(rows)
        self._update_state_row(len(rows))
        if not self._sized_once:
            self._size_columns(rows)
            self._sized_once = True
        for host in (self._panel_host, self._window_host):
            if host.row is None:
                continue
            # Keep each open inspector honest against the fresh rows: same
            # item re-rendered, a vanished item closes that host rather than
            # showing numbers the table no longer contains. Per host, so a
            # filter that drops the window's item leaves the panel alone.
            item_id = host.row["item_id"]
            match = next((r for r in rows if r["item_id"] == item_id), None)
            if match is None:
                self._close_inspector(host)
            else:
                self._show_inspected(host, match)

    _SIZING_SAMPLE = 200

    def _size_columns(self, rows: list, only_keys: set[str] | None = None) -> None:
        """Initial column widths from the headers plus a bounded row sample.

        resizeColumnToContents measures every cell -- 1.6 seconds at 25k
        rows, all of it spent on the tab's first paint, the one moment the
        deferred-load design exists to keep fast. Two hundred rows pin the
        typical width just as well, and the columns stay Interactive so any
        outlier is one drag from readable. only_keys restricts the pass to
        columns that have just appeared (the roll columns), leaving the
        widths the user has since dragged alone."""
        metrics = self.tree.fontMetrics()
        header = self.tree.header()
        for column, (key, title) in enumerate(self.model.columns()):
            if only_keys is not None and key not in only_keys:
                continue
            width = metrics.horizontalAdvance(title) + 24
            roll = is_roll_key(key)
            for row in rows[: self._SIZING_SAMPLE]:
                # Roll cells are measured as painted: cell_value is the raw
                # float for the export, whose repr is several times wider
                # than the "26 tf" the cell shows.
                value = self.model.roll_cell_text(row, key) if roll else self.model.cell_value(row, key)
                if value is not None:
                    width = max(width, metrics.horizontalAdvance(str(value)) + 24)
            header.resizeSection(column, min(width, 420))

    def _apply_delegates(self) -> None:
        """Re-point the badge delegate at the value columns wherever they now
        sit. Delegates are per column index, and the roll columns shift the
        value columns right when they appear and back when they vanish."""
        for column, (key, _header) in enumerate(self.model.columns()):
            delegate = self._badge_delegate if key in _BADGE_KEYS else self._plain_delegate
            self.tree.setItemDelegateForColumn(column, delegate)

    def _apply_rows(self, rows: list) -> None:
        level = self._current_group_key()
        # Section widths are per index too, so a column set change would
        # hand Qty's width to the first roll column and so on down the row.
        # Remember widths by key, put them back by key, and size only the
        # columns that are genuinely new.
        header = self.tree.header()
        widths: dict[str, int] = {}
        if self._sized_once:
            widths = {
                key: header.sectionSize(i) for i, (key, _h) in enumerate(self.model.columns())
            }
        # Which groups were open before the model reset wipes the view state
        # -- only meaningful while the grouping key is unchanged, so a
        # debounced keystroke refines the table without re-expanding (or
        # collapsing) anything the user arranged.
        expanded: set | None = None
        if level is not None and level == self._last_group_key and self.model.rowCount():
            root = QModelIndex()
            expanded = {
                self.model.index(i, 0, root).data(GROUP_LABEL_ROLE)
                for i in range(self.model.rowCount())
                if self.tree.isExpanded(self.model.index(i, 0, root))
            }
        self._last_group_key = level
        self.model.set_rows(rows, _GROUP_ROW_KEY[level] if level else None, self._extra_columns)
        self._apply_delegates()
        if widths:
            fresh: set[str] = set()
            for i, (key, _h) in enumerate(self.model.columns()):
                if key in widths:
                    header.resizeSection(i, widths[key])
                else:
                    fresh.add(key)
            if fresh:
                self._size_columns(rows, fresh)
        self.tree.setRootIsDecorated(level is not None)
        if level is not None:
            # A header's whole "label · stacks · m³ · ISK" line lives in
            # column 0, so without spanning it clips at the first column's
            # edge. Spanned, it reads as the design's full-width band. Spans
            # are view state carried on persistent indexes -- every model
            # reset drops them, so they are re-applied after each set_rows.
            # Grouped mode only: every top-level row is a header there, and
            # the reset has already cleared any spans before a flat reload.
            root = QModelIndex()
            for i in range(self.model.rowCount()):
                self.tree.setFirstColumnSpanned(i, root, True)
        if level is not None:
            if expanded is not None:
                # Same grouping as before the reload: restore what was open
                # instead of re-expanding wholesale. expandAll re-ran on
                # every debounced keystroke and measured 110-280 ms per
                # press at 3-8k rows -- and forgot every group the user had
                # deliberately collapsed.
                self.tree.setUpdatesEnabled(False)
                try:
                    root = QModelIndex()
                    for i in range(self.model.rowCount()):
                        index = self.model.index(i, 0, root)
                        if index.data(GROUP_LABEL_ROLE) in expanded:
                            self.tree.expand(index)
                finally:
                    self.tree.setUpdatesEnabled(True)
            elif len(rows) <= 10_000:
                # Entering grouped mode (or switching level): open everything
                # once. expandAll is pure GUI-thread time and measured ~0.7s
                # at 25k rows; past that size the groups start collapsed --
                # the rollup headers were designed to be read that way
                # ("scanning 40 locations becomes 40 summary lines"), and
                # expanding the one you care about is a click.
                self.tree.expandAll()
        if self.sorter.key is not None:
            self.sorter.reapply()
        self._update_footer()

    def _update_state_row(self, shown: int) -> None:
        count = self.omnibox.spec().describe()
        # Stacks first, filter count last: the count sits immediately left of
        # the Clear all pill, so "8 filters  [Clear all]" reads as one unit --
        # the thing being cleared and the control that clears it.
        suffix = f" · {count} filter{'s' if count != 1 else ''}" if count else ""
        self.state_label.setText(f"{shown:,} of {self._total_stacks:,} stacks{suffix}")
        self.clear_all_btn.setVisible(count > 0)
        self._explain_if_empty(shown)

    def _explain_if_empty(self, shown: int) -> None:
        """Say why the table is empty when the reason is not the filter.

        A first-time user reported 32,297 synced stacks showing as "0 of
        32,297" with every column blank. The cause was that the game data had
        never been imported: ASSET_ROWS inner joins sde_types, so an empty SDE
        removes every row before the table sees one. The status bar did say
        "SDE build not imported", but that is the wrong place -- it is not
        where somebody looks when a table they expected to be full is empty,
        and it does not connect the two facts.

        Only speaks up when rows exist but none survive the query, so a filter
        that genuinely matches nothing still reads as an ordinary empty result.
        """
        if shown or not self._total_stacks:
            self.empty_hint.setVisible(False)
            return
        try:
            has_sde = bool(
                db.connect().execute("SELECT 1 FROM sde_types LIMIT 1").fetchone()
            )
        except Exception:  # noqa: BLE001 - a hint must never break the view
            has_sde = True
        if has_sde:
            self.empty_hint.setVisible(False)
            return
        # status_hex rather than a literal: it is already measured against
        # WCAG AA in both themes by tests/test_contrast.py, and a warning
        # nobody can read is not a warning.
        colour = palette.status_hex(palette.WARN, self.palette())
        self.empty_hint.setStyleSheet(f"color: {colour};" if colour else "")
        self.empty_hint.setText(
            "Game data has not been imported, so none of your "
            f"{self._total_stacks:,} stacks can be shown. "
            "Run <b>Update -&gt; Game data</b> to download it."
        )
        self.empty_hint.setVisible(True)

    def _on_query_failed(self, message: str) -> None:
        self.footer.setText(f"Query failed: {message}")

    # ------------------------------------------------------------------- rail
    def _refresh_rail(self) -> None:
        spec = self.omnibox.spec()
        level = self.rail.current_level()
        hunting = spec.text or any(c.kind == "item" and not c.negated for c in spec.chips)
        if hunting:
            # Bare item text -- or an exact item: chip, the where-else
            # gesture -- flips the rail to "where is it": per-label
            # quantities of the matched items under the FULL filter,
            # text included; the user is hunting a thing, not browsing
            # values.
            where, params = spec.where()

            def fetch_flip(conn: sqlite3.Connection):
                return queries.where_is_item(conn, level, where, params)

            self._rail_query.run(fetch_flip, self.rail.set_flip, self._on_query_failed)
            return

        # Rollup mode facets by every filter except chips of the rail's own
        # level, so picking one location still shows the others to switch to.
        where, params = spec.where(exclude_level=level)
        sort = self.rail.current_sort()

        def fetch_rollups(conn: sqlite3.Connection):
            rows = queries.rail_rollups(conn, level, where, params, sort)
            pinned = {
                r["label"]
                for r in conn.execute(
                    "SELECT label FROM pinned_labels WHERE level = ?", (level,)
                )
            }
            return rows, pinned

        def deliver(payload) -> None:
            rows, pinned = payload
            self.rail.clear_flip()
            self.rail.set_rollups(rows, pinned)

        self._rail_query.run(fetch_rollups, deliver, self._on_query_failed)

    def _on_pin_toggled(self, level: str, label: str) -> None:
        # Toggle by DELETE-first: rowcount says whether the pin existed. A
        # single-row write is cheap enough to stay on the GUI thread.
        with db.transaction(self.conn):
            cursor = self.conn.execute(
                "DELETE FROM pinned_labels WHERE level = ? AND label = ?", (level, label)
            )
            if cursor.rowcount == 0:
                self.conn.execute(
                    "INSERT INTO pinned_labels(level, label) VALUES(?, ?)", (level, label)
                )
        self._refresh_rail()

    # ------------------------------------------------------------------ strip
    def refresh_strip(self) -> None:
        def fetch(conn: sqlite3.Connection):
            # More segments than the widget can usually show: the map culls
            # to what fits its current width and folds the rest into one
            # "N more" residue, so a generous limit costs nothing and lets a
            # wide window show more detail.
            return queries.estate_summary(conn), list(queries.value_map(conn, limit=24))

        self._strip_query.run(fetch, self._on_strip)

    def _on_strip(self, payload) -> None:
        summary, segments = payload
        self.strip.set_data(summary, segments)

    # --------------------------------------------------------------- grouping
    def _current_group_key(self) -> str | None:
        """The group-by level key, or None for the flat table."""
        return self.group_combo.currentData() or None

    def _on_group_changed(self, _index: int) -> None:
        # Regroup in place from the rows already fetched -- grouping is
        # in-model (see grouped_model.py), so no re-query is needed.
        self._apply_rows(self.model.rows())

    def _cycle_group_by(self) -> None:
        self.group_combo.setCurrentIndex(
            (self.group_combo.currentIndex() + 1) % self.group_combo.count()
        )

    # ------------------------------------------------------------- navigation
    def _build_shortcuts(self) -> None:
        def key(sequence, slot, parent) -> None:
            shortcut = QShortcut(QKeySequence(sequence), parent)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)

        # Tab-wide: / focuses the omnibox, Ctrl+digit saves a view. Everything
        # on a bare letter or digit is parented on the tree instead, so typing
        # in the omnibox (or the rail's filter box) never triggers it.
        key("/", self._focus_omnibox, self)
        # Tab-wide as well, and the ONLY Ctrl+F binding: the builder must
        # open from the table, the rail, anywhere, and a second binding on the
        # omnibox made the pair ambiguous (see Omnibox.add_btn).
        key("Ctrl+F", self.omnibox.open_draft, self)
        for digit in range(1, 10):
            key(f"Ctrl+{digit}", lambda d=digit: self._save_view(d), self)
            key(str(digit), lambda d=digit: self._recall_view(d), self.tree)
        key("g", self._cycle_group_by, self.tree)
        key("j", lambda: self._move_current(1), self.tree)
        key("k", lambda: self._move_current(-1), self.tree)
        key("Space", self._toggle_selection, self.tree)
        key("f", lambda: self._filter_current_cell(negated=False), self.tree)
        key("x", lambda: self._filter_current_cell(negated=True), self.tree)
        key("w", self._where_else_current, self.tree)
        key(Qt.Key_Return, self._open_inspector_current, self.tree)
        key(Qt.Key_Enter, self._open_inspector_current, self.tree)
        key(Qt.Key_Escape, self._escape_from_table, self.tree)
        key("?", self._show_key_map, self.tree)

    def _focus_omnibox(self) -> None:
        # The shortcut is tab-wide, which swallows the keystroke even while
        # the omnibox itself has focus -- and a few item names genuinely
        # contain a slash, so put the character back in that one case.
        if self.omnibox.edit.hasFocus():
            self.omnibox.edit.insert("/")
        else:
            self.omnibox.focus_search()

    def _move_current(self, delta: int) -> None:
        current = self.tree.currentIndex()
        if current.isValid() and current.column() != 0:
            current = current.siblingAtColumn(0)
        step = self.tree.indexBelow if delta > 0 else self.tree.indexAbove
        candidate = step(current) if current.isValid() else self.model.index(0, 0)
        # Group headers are enabled (they must expand) but are not rows, so
        # j/k walk straight over them.
        while candidate.isValid() and self.model.row_for_index(candidate) is None:
            candidate = step(candidate)
        if candidate.isValid():
            self.tree.selectionModel().setCurrentIndex(
                candidate, QItemSelectionModel.NoUpdate
            )

    def _toggle_selection(self) -> None:
        index = self.tree.currentIndex()
        if self.model.row_for_index(index) is None:
            return
        self.tree.selectionModel().select(
            index, QItemSelectionModel.Toggle | QItemSelectionModel.Rows
        )

    def _escape_from_table(self) -> None:
        if self._panel_host.is_open():
            self._close_inspector(self._panel_host)
            return
        spec = self.omnibox.spec()
        if spec.chips:
            self.omnibox.remove_chip(spec.chips[-1])
        elif spec.text:
            self.omnibox.clear()

    def _show_key_map(self) -> None:
        rows = "".join(
            f"<tr><td><b>{keys}</b>&nbsp;&nbsp;</td><td>{what}</td></tr>"
            for keys, what in _KEY_MAP
        )
        QMessageBox.information(self, "Keyboard", f"<table>{rows}</table>")

    # ------------------------------------------------------------ saved views
    def _save_view(self, slot: int) -> None:
        state = json.dumps(
            {
                "filter": self.omnibox.spec().to_text(),
                "group_by": self._current_group_key() or "",
                "rail_level": self.rail.current_level(),
            }
        )
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO saved_views(slot, state_json) VALUES(?, ?) "
                "ON CONFLICT(slot) DO UPDATE SET state_json = excluded.state_json",
                (slot, state),
            )
        self.footer.setText(f"Saved view {slot}.")

    def _recall_view(self, slot: int) -> None:
        row = self.conn.execute(
            "SELECT state_json FROM saved_views WHERE slot = ?", (slot,)
        ).fetchone()
        if row is None:
            self.footer.setText(f"No saved view in slot {slot}.")
            return
        try:
            state = json.loads(row["state_json"])
        except ValueError:
            self.footer.setText(f"Saved view {slot} is unreadable.")
            return
        # Combo and rail level move silently; the omnibox's set_spec fires
        # the single changed() that reloads rows, groups and rail together.
        index = self.group_combo.findData(state.get("group_by") or "")
        if index >= 0:
            self.group_combo.blockSignals(True)
            self.group_combo.setCurrentIndex(index)
            self.group_combo.blockSignals(False)
        level_keys = [key for _label, key in queries.ROLLUP_LEVELS]
        if state.get("rail_level") in level_keys:
            self.rail.level.blockSignals(True)
            self.rail.level.setCurrentIndex(level_keys.index(state["rail_level"]))
            self.rail.level.blockSignals(False)
        self.omnibox.set_spec(omni.parse(state.get("filter", "")))

    # ---------------------------------------------------------- cell actions
    def _filter_current_cell(self, *, negated: bool) -> None:
        index = self.tree.currentIndex()
        row = self.model.row_for_index(index)
        key = self.model.key_at(index.column())
        if row is None or key is None:
            return
        kind = _COLUMN_CHIP_KIND.get(key)
        value = self.model.cell_value(row, key)
        if kind is None or value in (None, ""):
            return
        self.omnibox.add_chip(kind, str(value), negated=negated)

    def _where_else_current(self) -> None:
        row = self.model.row_for_index(self.tree.currentIndex())
        if row is not None:
            self._where_else(row)

    def _where_else(self, row: sqlite3.Row) -> None:
        """Ask "where else is this item": drop every location chip, pin the
        exact item as an item: chip, and point the rail at locations so its
        flip mode answers in per-station quantities.

        An item: chip, not bare text -- bare words filter by LIKE substring,
        so asking about Tritanium would also count every Compressed Tritanium
        stack and quietly inflate the answer."""
        chips = [c for c in self.omnibox.spec().chips if c.kind not in ("location", "item")]
        chips.append(omni.Chip("item", row["item"]))
        self.rail.level.blockSignals(True)
        self.rail.level.setCurrentIndex(0)  # ROLLUP_LEVELS[0] is Location
        self.rail.level.blockSignals(False)
        self.omnibox.set_spec(omni.FilterSpec(text="", chips=chips))

    # ---------------------------------------------------------- context menu
    def _show_context_menu(self, pos) -> None:
        menu = self._build_context_menu(self.tree.indexAt(pos))
        if menu is not None:
            menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _build_context_menu(self, index) -> QMenu | None:
        """The row menu for one cell, or None off the rows. Built apart from
        exec so tests can read the actions without a modal loop."""
        row = self.model.row_for_index(index)
        if row is None:  # empty space, or a group header
            return None
        key, header = self.model.columns()[index.column()]
        # A roll cell's value is the rendered text ("-63%", "27 tf"), which
        # is what "Copy cell" should put on the clipboard; a row column's is
        # the raw value, as before.
        value = index.data(Qt.DisplayRole) if is_roll_key(key) else row[key]
        kind = _COLUMN_CHIP_KIND.get(key)

        menu = QMenu(self)
        # Named for where it opens: a click already put this row in the
        # panel, so a bare "Inspect" here would read as a no-op.
        menu.addAction(
            "Inspect in window", lambda: self._open_inspector(self._window_host, row)
        )
        menu.addSeparator()
        if kind is not None and value not in (None, ""):
            menu.addAction(
                f"Filter: {header} = {value}",
                lambda: self.omnibox.add_chip(kind, str(value)),
            )
            menu.addAction(
                f"Exclude: {header} = {value}",
                lambda: self.omnibox.add_chip(kind, str(value), negated=True),
            )
            menu.addSeparator()
        menu.addAction("Where else is this?", lambda: self._where_else(row))
        menu.addSeparator()
        menu.addAction(
            "Copy cell",
            lambda: QGuiApplication.clipboard().setText(
                "" if value is None else str(value)
            ),
        )
        menu.addAction(
            "Copy item name",
            lambda: QGuiApplication.clipboard().setText(row["item"] or ""),
        )
        if (row["category"] or "") in _FIT_VIEWABLE_CATEGORIES or _is_corpse(row):
            menu.addSeparator()
            menu.addAction("View fit…", lambda: self._open_fit_dialog(row))
        return menu

    def _open_fit_dialog(self, row: sqlite3.Row) -> None:
        if _is_corpse(row):
            # A corpse has no fitting slots, so there is nothing for FitDialog
            # to read. Show what it is carrying instead.
            chest_reveal.play(self)
            return
        name = row["custom_name"] or row["item"]
        dialog = FitDialog(row["item_id"], name, ship_type_id=row["type_id"], parent=self)
        dialog.exec()

    # ----------------------------------------------------------- abyssal card
    def _open_card(self, chip: omni.Chip, anchor: QWidget) -> None:
        """The abyssal chip's glyph, or the omnibox's deferred request after
        the chip was typed: seed the card from the current chips, start its
        data fetch, and show it under the chip."""
        card = self._card
        if card is None:
            card = self._card = AbyssalCard(self)
            card.selection_changed.connect(self._on_card_selection)
            card.filter_changed.connect(self._card_count.trigger)
            card.fetch_requested.connect(self._on_card_fetch)
            card.done.connect(self._on_card_done)
            # A count still on the clock when the card goes away would run
            # for nobody.
            card.cancelled.connect(self._card_count.stop)
        if card.isVisible():
            card.hide()  # reads as Cancel; the seed below starts afresh
        card.seed(self.omnibox.spec().chips)
        self._fetch_card_data(card.seeded_types(), with_types=True, anchor=anchor)
        card.adjustSize()
        self._place_card(anchor)
        card.show()
        # Once more after the event loop has laid the card out: the size it
        # has before its first show is a guess, and the anchor itself may
        # have moved if the omnibox re-flowed (the draft chip's lesson).
        QTimer.singleShot(0, lambda: self._place_card(anchor))

    def _place_card(self, anchor: QWidget) -> None:
        card = self._card
        if card is None:
            return
        try:
            anchored = anchor.isVisible()
            below = anchor.mapToGlobal(QPoint(0, anchor.height()))
            above = anchor.mapToGlobal(QPoint(0, 0))
        except RuntimeError:
            # The chip widget was removed (the cross, a set_spec) between
            # the click and this deferred placement; fall back to the field.
            anchored = False
        if not anchored:
            below = self.omnibox.mapToGlobal(QPoint(0, self.omnibox.height()))
            above = self.omnibox.mapToGlobal(QPoint(0, 0))
        screen = QGuiApplication.screenAt(below) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else None
        x, y = below.x(), below.y() + 2
        if area is not None:
            x = max(area.left(), min(x, area.right() - card.width() + 1))
            if y + card.height() > area.bottom() and above.y() - card.height() >= area.top():
                y = above.y() - card.height() - 2
        card.move(x, y)

    def _fetch_card_data(
        self, selected: list[str], *, with_types: bool, anchor: QWidget | None = None
    ) -> None:
        """One pool-thread trip for everything the card shows: the type
        counts faceted by every filter except the card's own kinds, the
        not-fetched count for the picked type, and -- with one picked rather
        than none -- its rolled attributes and value bounds.

        The facet drops every abyssal, stat: and roll: chip: the card
        rewrites the first two on Done, and roll: names an attribute only
        some types roll, so faceting by any of them would hide exactly the
        types the user might switch to (with ``abyssal:"Abyssal Stasis
        Webifier" stat:web<=-60`` in force no BCS has a web strength).
        Negated card chips go too, which the card cannot represent and Done
        keeps; their counts are therefore slightly overstated -- the price
        of one rule for both polarities."""
        card = self._card
        if card is None:
            return
        spec = self.omnibox.spec()
        where, params = spec.where(exclude_kinds=(*CARD_KINDS, omni.ROLL_KIND))
        single = selected[0] if len(selected) == 1 else None
        names = list(selected)

        def fetch(conn: sqlite3.Connection) -> dict:
            payload: dict = {
                "types": queries.abyssal_type_counts(conn, where, params) if with_types else None,
                "pending": queries.abyssal_pending_count(conn, names or None),
                "attrs": [],
                "bounds": {},
            }
            if single is not None:
                payload["attrs"], payload["bounds"] = queries.abyssal_type_columns(conn, single)
            return payload

        def deliver(payload: dict) -> None:
            if payload["types"] is not None:
                self._card_types = {
                    str(r["name"]): int(r["type_id"]) for r in payload["types"]
                }
                card.set_types(payload["types"], names)
            card.set_attributes(payload["attrs"], payload["bounds"])
            card.set_pending(payload["pending"])
            card.adjustSize()
            if anchor is not None and card.isVisible():
                self._place_card(anchor)

        self._card_query.run(fetch, deliver, self._on_query_failed)

    def _on_card_selection(self, names: list) -> None:
        self._fetch_card_data(list(names), with_types=False)

    def _spec_with_card(self, chips: list) -> omni.FilterSpec:
        """The omnibox's filter with the card's chips in place of every
        positive abyssal/stat chip and everything else -- other kinds, a
        typed roll: chip, bare text, and negated chips of the card's kinds,
        which it cannot represent and must not silently discard -- exactly
        as it was. What Done writes, and what the live count counts, so the
        two can never disagree."""
        spec = self.omnibox.spec()
        kept = [c for c in spec.chips if c.kind not in CARD_KINDS or c.negated]
        return omni.FilterSpec(text=spec.text, chips=kept + list(chips))

    def _fetch_card_count(self) -> None:
        """The card's footer: how many items its chips would leave, out of
        the picked type's items under the same filter -- the abyssal chip
        alone, so the denominator is the dropdown's own count for the type
        (plus whatever a kept roll: or negated chip takes off both). Both
        counts in one trip; a stale answer is dropped by the generation
        guard, and the card shows "…" until the fresh one lands."""
        card = self._card
        if card is None or not card.isVisible():
            return
        chips = card.chips()
        matched_where, matched_params = self._spec_with_card(chips).where()
        total_where, total_params = self._spec_with_card(chips[:1]).where()

        def fetch(conn: sqlite3.Connection) -> tuple[int, int]:
            return (
                queries.count_assets(conn, matched_where, matched_params),
                queries.count_assets(conn, total_where, total_params),
            )

        def deliver(counts: tuple[int, int]) -> None:
            card.set_match_count(*counts)

        self._card_count_query.run(fetch, deliver, self._on_query_failed)

    def _on_card_done(self, chips: list) -> None:
        """Done: one set_spec, one reload (see _spec_with_card)."""
        self._card_count.stop()
        self.omnibox.set_spec(self._spec_with_card(chips))

    def _on_card_fetch(self, names: list) -> None:
        """The banner's Fetch: resolve the picked type (none = every type)
        to the item_ids still pending and hand them to the main window's job
        path. abyssal.pending is one indexed pass over the assets table,
        cheap enough for the GUI thread that owns self.conn."""
        wanted = {self._card_types[n] for n in names if n in self._card_types}
        pending = abyssal.pending(self.conn)
        item_ids = [item for item, type_id in pending if not names or type_id in wanted]
        if not item_ids:
            self.footer.setText("Nothing left to fetch for those types.")
            if self._card is not None:
                self._card.set_pending(0)
            return
        self.abyssal_fetch_requested.emit(item_ids)

    # -------------------------------------------------------------- inspector
    def _wire_inspector(self, host: _InspectorHost) -> None:
        """Every action an Inspector emits, bound to the host it lives in, so
        one set of slots serves the panel and the window alike."""
        inspector = host.inspector
        inspector.close_clicked.connect(lambda: self._close_inspector(host))
        inspector.where_else_clicked.connect(lambda: self._where_else_inspected(host))
        inspector.refresh_price_clicked.connect(lambda: self._refresh_inspected_price(host))
        inspector.pin_price_clicked.connect(lambda: self._pin_inspected_price(host))
        inspector.fetch_abyssal_clicked.connect(lambda: self._fetch_inspected_rolls(host))

    def _open_inspector_current(self) -> None:
        self._open_panel_at(self.tree.currentIndex())

    def _open_panel_at(self, index) -> None:
        row = self.model.row_for_index(index)
        if row is not None:  # a group header has nothing to inspect
            self._open_inspector(self._panel_host, row)

    def _open_inspector(self, host: _InspectorHost, row: sqlite3.Row) -> None:
        self._show_inspected(host, row)
        host.show()

    def _raise_window(self) -> None:
        # show() rather than exec(): Where else? and the filter actions
        # change the table behind the window, which a modal loop would
        # block. raise_/activateWindow cover the re-open case, where the
        # window is already up but behind the main one.
        self.inspector_window.show()
        self.inspector_window.raise_()
        self.inspector_window.activateWindow()

    def _show_inspected(self, host: _InspectorHost, row: sqlite3.Row) -> None:
        """Render a row in one host and, for a mutated module, start the
        rolls lookup that fills its stats section."""
        host.row = row
        host.render(row)
        if _row_get(row, "is_dynamic_type"):
            self._load_rolls(host, row["item_id"])
        else:
            # An in-flight lookup for the previous (abyssal) row must not
            # paint its rolls under this one; the item guard in _load_rolls
            # would drop it anyway, but there is no point letting it land.
            host.rolls_query.cancel()

    def _load_rolls(self, host: _InspectorHost, item_id: int) -> None:
        def fetch(conn: sqlite3.Connection) -> dict:
            return queries.fetch_abyssal_rolls(conn, item_id)

        def deliver(payload: dict) -> None:
            # AsyncQuery's generation guard drops results a newer lookup
            # superseded; this guard covers the other race -- the host
            # closed, or moved to a row that started no lookup, between the
            # query being latest and its result arriving. It reads this
            # host's own row, so the window's rolls cannot land in the panel.
            current = host.row
            if current is not None and current["item_id"] == item_id:
                host.inspector.show_rolls(payload)

        host.rolls_query.run(fetch, deliver, self._on_query_failed)

    def _close_inspector(self, host: _InspectorHost) -> None:
        # The row goes before anything is hidden, so a rolls result landing
        # mid-close finds no row to paint under. The window's hide is
        # hide(), not reject(): reject emits finished, which leads back here.
        host.row = None
        host.rolls_query.cancel()
        host.hide()

    def _where_else_inspected(self, host: _InspectorHost) -> None:
        if host.row is not None:
            self._where_else(host.row)

    def _refresh_inspected_price(self, host: _InspectorHost) -> None:
        if host.row is None:
            return
        job = _PriceRefreshJob(host.row["type_id"])
        self._price_jobs.add(job)  # must outlive this call -- see async_query.py
        job.signals.finished.connect(lambda _result, j=job: self._on_price_job_done(j))
        job.signals.failed.connect(
            lambda message, j=job: self._on_price_job_failed(j, message)
        )
        QThreadPool.globalInstance().start(job)

    def _on_price_job_done(self, job: _PriceRefreshJob) -> None:
        self._price_jobs.discard(job)
        self.refresh_all()

    def _on_price_job_failed(self, job: _PriceRefreshJob, message: str) -> None:
        self._price_jobs.discard(job)
        self.footer.setText(f"Price refresh failed: {message}")

    def _fetch_inspected_rolls(self, host: _InspectorHost) -> None:
        if host.row is None:
            return
        job = _AbyssalFetchJob(host.row["item_id"])
        self._abyssal_jobs.add(job)  # must outlive this call -- see async_query.py
        job.signals.finished.connect(lambda _result, j=job: self._on_abyssal_job_done(j))
        job.signals.failed.connect(
            lambda message, j=job: self._on_abyssal_job_failed(j, host, message)
        )
        host.inspector.set_rolls_fetching()
        QThreadPool.globalInstance().start(job)

    def _on_abyssal_job_done(self, job: _AbyssalFetchJob) -> None:
        self._abyssal_jobs.discard(job)
        # A full refresh rather than just re-running the rolls lookup: the
        # badge tooltip summaries ride the row reload, and a stale "Rolls
        # not fetched" over a freshly fetched item would contradict the
        # inspector showing its rolls. The reload re-renders both hosts, so
        # the same item open in the other one picks the rolls up as well.
        self.refresh_all()

    def _on_abyssal_job_failed(
        self, job: _AbyssalFetchJob, host: _InspectorHost, message: str
    ) -> None:
        self._abyssal_jobs.discard(job)
        self.footer.setText(f"Abyssal stats fetch failed: {message}")
        if host.row is not None and _row_get(host.row, "is_dynamic_type"):
            # Re-arm the button from the stored state, whatever it is now.
            self._load_rolls(host, host.row["item_id"])

    def _pin_inspected_price(self, host: _InspectorHost) -> None:
        row = host.row
        if row is None:
            return
        if (row["price_source"] or "") == pricing.MANUAL:
            box = QMessageBox(self)
            box.setWindowTitle("Manual price")
            box.setText(f"{row['item']} is pinned at {fmt_isk(row['sell_price'])} ISK.")
            change = box.addButton("Change…", QMessageBox.AcceptRole)
            unpin = box.addButton("Unpin", QMessageBox.DestructiveRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            if box.clickedButton() is unpin:
                pricing.clear_manual_price(self.conn, row["type_id"])
                self.refresh_all()
                return
            if box.clickedButton() is not change:
                return
        price, ok = QInputDialog.getDouble(
            self,
            "Pin price",
            f"Price per unit for {row['item']} (ISK):",
            float(row["sell_price"] or 0),
            0.0,
            1e15,
            2,
        )
        if not ok:
            return
        pricing.set_manual_price(self.conn, row["type_id"], price)
        self.refresh_all()

    # ------------------------------------------------------ selection footer
    def _selected_rows(self) -> list[sqlite3.Row]:
        selection = self.tree.selectionModel()
        if selection is None:
            return []
        rows = []
        # selection() ranges rather than selectedRows(): Qt materialises the
        # flat index list at ~0.17 ms per selected row (a 25k select-all
        # measured 4.1 seconds, re-paid on every selectionChanged), while the
        # handful of contiguous ranges costs nothing to walk.
        for srange in selection.selection():
            parent = srange.parent()
            for position in range(srange.top(), srange.bottom() + 1):
                row = self.model.row_for_index(self.model.index(position, 0, parent))
                if row is not None:  # headers are unselectable, but stay honest
                    rows.append(row)
        return rows

    def _update_footer(self) -> None:
        selected = self._selected_rows()
        rows = selected or self.model.rows()
        units = sum(int(r["quantity"] or 0) for r in rows)
        volume = sum(float(r["volume"] or 0) for r in rows)
        sell = sum(float(r["sell_value"] or 0) for r in rows)
        head = f"{len(selected):,} selected" if selected else f"{len(rows):,} stacks"
        self.footer.setText(
            f"{head} · {units:,} units · {volume:,.0f} m³ · {fmt_short_isk(sell)} ISK (sell)"
        )

    def _multibuy_text(self) -> str:
        """Selection (or the whole filtered set) as EVE multibuy lines,
        quantities summed per item name -- the same "name<TAB>qty" shape
        stockpile.shopping_list produces."""
        totals: dict[str, float] = {}
        order: list[str] = []
        for row in self._selected_rows() or self.model.rows():
            name = row["item"]
            if not name:
                continue
            if name not in totals:
                totals[name] = 0
                order.append(name)
            totals[name] += row["quantity"] or 0
        return "\n".join(f"{name}\t{int(totals[name])}" for name in order)

    def copy_list(self) -> None:
        text = self._multibuy_text()
        if not text:
            self.footer.setText("Nothing to copy.")
            return
        QGuiApplication.clipboard().setText(text)
        lines = len(text.splitlines())
        self.footer.setText(
            f"Copied {lines} line(s). In EVE: open the market, right-click → Multibuy, paste."
        )

    def appraise(self) -> None:
        """Send the list to Janice and open the finished appraisal.

        This used to copy the text and open janice.e-351.com, leaving the user
        to paste it. Janice will take the list over its API and hand back a
        saved appraisal, so the paste step can go.

        The clipboard is still filled, every time, before anything is sent.
        That is the fallback if the call fails, and it costs nothing to do
        unconditionally -- somebody who wanted the text still has it.
        """
        text = self._multibuy_text()
        if not text:
            self.footer.setText("Nothing to appraise.")
            return

        QGuiApplication.clipboard().setText(text)
        self.footer.setText("Appraising…")

        # Settings.load() here rather than a constructor argument: no
        # caller passes settings to AssetsView, and _PriceRefreshJob
        # already loads its own for the same reason.
        job = AppraiseJob(text, Settings.load())
        # Held for the same reason _price_jobs exists: a QRunnable with no
        # Python reference can be collected mid-flight, and its signals go
        # with it. See async_query.py.
        self._appraise_jobs.add(job)
        job.signals.finished.connect(lambda r, j=job: self._on_appraised(r, j))
        job.signals.failed.connect(lambda m, j=job: self._on_appraise_failed(m, j))
        QThreadPool.globalInstance().start(job)

    def _on_appraised(self, result, job=None) -> None:
        self._appraise_jobs.discard(job)
        if not result or "error" in (result or {}):
            self._on_appraise_failed((result or {}).get("error", "unknown error"))
            return
        appraisal = result["appraisal"]
        QDesktopServices.openUrl(QUrl(appraisal.url))

        note = f"Appraised {appraisal.priced} item(s): {fmt_short_isk(appraisal.total_sell)} sell"
        if appraisal.failed:
            # Named rather than counted. "3 lines were not recognised" sends
            # somebody hunting; the lines themselves are usually enough to see
            # why at a glance.
            sample = ", ".join(
                line.split("	")[0] for line in appraisal.failed_lines[:3]
            )
            more = "…" if appraisal.failed > 3 else ""
            note += f" · {appraisal.failed} not recognised ({sample}{more})"
        self.footer.setText(note)

    def _on_appraise_failed(self, message: str, job=None) -> None:
        self._appraise_jobs.discard(job)
        """Fall back to what the button did before.

        An appraisal is a convenience. Losing it should not lose the list, and
        the text is already on the clipboard, so this degrades to exactly the
        old behaviour rather than to nothing.
        """
        QDesktopServices.openUrl(QUrl("https://janice.e-351.com/"))
        self.footer.setText(f"Janice unavailable ({message}). Copied - paste it in.")

    # ----------------------------------------------------------------- export
    def export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export assets", "assets.csv", "CSV files (*.csv)"
        )
        if not path:
            return
        columns = self.model.columns()
        keys = [k for k, _ in columns]
        headers = [h for _, h in columns]
        rows = self.model.rows()
        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(headers)
                for r in rows:
                    writer.writerow([self.model.cell_value(r, k) for k in keys])
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self.footer.setText(f"Exported {len(rows):,} rows to {path}")
