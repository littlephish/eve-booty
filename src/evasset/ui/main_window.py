"""Main window: menus, tabs, status bar."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QMainWindow,
    QMessageBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import db, queries
from ..config import DB_PATH, Settings
from ..esi import TokenCache
from .assets_view import AssetsView
from .async_query import AsyncQuery
from .characters_dialog import CharactersDialog
from .networth_view import NetWorthView
from .settings_dialog import SettingsDialog
from .stockpile_view import StockpileView
from .structures_view import StructuresView
from .task_bar import TaskBar
from .tasks import TaskManager
from .treemap_view import TreemapView
from .wallet_view import WalletView
from .workers import RepriceJob, SdeUpdateJob, SnapshotJob, SyncJob


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.tokens = TokenCache(settings)
        self.conn = db.init()
        self._warnings: list[str] = []
        self.tasks = TaskManager(self)

        self.setWindowTitle("EVE Assets")
        self.resize(1440, 880)

        self.tabs = QTabWidget()
        # Every tab's first real query is deferred until it is actually shown.
        # All of them used to reload() (query + format + resize) in their own
        # __init__, so a cold start paid for Assets, both Wallet tables and
        # Net worth before you could look at any of them. Now only the tab on
        # screen loads.
        #
        # The views take no connection. Their reads all go through AsyncQuery,
        # which uses db.connect() on the pool thread it runs on, and the few
        # GUI-thread queries (this class, the Assets and Stockpile writes)
        # call db.connect() for this thread's. db.connect() caches one
        # connection per thread, so the db.init() above is what actually opens
        # the main thread's -- and the schema script and migration check run
        # once here rather than once per view.
        self.assets = AssetsView(defer_load=True)
        self.networth = NetWorthView(defer_load=True)
        self.wallet = WalletView(defer_load=True)
        self.structures = StructuresView(defer_load=True)
        self.stockpile = StockpileView(defer_load=True)
        self.treemap = TreemapView(defer_load=True)
        self.log = _LogPane()

        self.tabs.addTab(self.assets, "Assets")
        self.tabs.addTab(self.treemap, "Treemap")
        self.tabs.addTab(self.wallet, "Wallet")
        self.tabs.addTab(self.networth, "Net worth")
        self.tabs.addTab(self.structures, "Structures")
        self.tabs.addTab(self.stockpile, "Stockpile")
        self.tabs.addTab(self.log, "Log")
        self.setCentralWidget(self.tabs)

        self._tab_first_load = {
            self.assets: self.assets.first_load,
            self.treemap: self.treemap.first_load,
            self.wallet: self.wallet.first_load,
            self.networth: self.networth.first_load,
            self.structures: self.structures.first_load,
            self.stockpile: self.stockpile.first_load,
        }
        self._tab_reload = {
            self.assets: self.assets.refresh_all,
            self.treemap: self.treemap.reload,
            self.wallet: self.wallet.reload,
            self.networth: self.networth.refresh,
            self.structures: lambda: (
                self.structures.refresh_filters(), self.structures.reload()
            ),
            self.stockpile: self.stockpile.refresh_filters,
        }
        self._loaded: set = set()
        self._dirty: set = set()
        self.tabs.currentChanged.connect(self._ensure_tab_loaded)
        self.treemap.filter_assets_requested.connect(self._filter_assets_from_treemap)
        self._status_query = AsyncQuery(self)

        # Connected here rather than next to the TaskManager itself: warned
        # lands in the log pane, which does not exist until the tabs are up.
        self.tasks.warned.connect(self.log.add)
        self.tasks.failed.connect(self._on_task_failed)
        self.tasks.finished.connect(self._on_task_finished)

        self._build_actions()
        self._build_statusbar()
        self._refresh_status()
        self._ensure_tab_loaded(self.tabs.currentIndex())  # load just the visible tab

        if not settings.client_id:
            QTimer.singleShot(400, self._first_run_hint)

    # ---------------------------------------------------------------- chrome
    def _build_actions(self) -> None:
        # No toolbar. Every one of these lives in a menu, and a row of buttons
        # repeating the menu it sits under is the same command in two places
        # to keep in step -- which already bit once, when renaming an action
        # for the toolbar renamed it in the menu too. The tooltips the toolbar
        # carried were worth keeping, so they moved onto the actions.
        def action(text, slot, tip, shortcut=None):
            act = QAction(text, self)
            act.setToolTip(tip)
            act.setStatusTip(tip)
            if shortcut is not None:
                act.setShortcut(shortcut)
            act.triggered.connect(slot)
            return act

        self.act_sync = action(
            "&All", self.sync_all,
            "Sync every character, then reprice and snapshot",
            QKeySequence("F5"),
        )
        self.act_chars_all = action(
            "All c&haracters", self.sync_characters,
            "Pull every character's data, without repricing or snapshotting",
        )
        self.act_prices = action(
            "&Prices", self.reprice, "Refresh market prices only",
        )
        self.act_snapshot = action(
            "&Net worth snapshot", self.snapshot, "Record net worth now",
        )
        self.act_sde = action(
            "&Game data", self.update_sde,
            "Check for a newer Static Data Export",
        )
        self.act_chars = action(
            "&Characters…", self.open_characters,
            "Add, remove and authorise characters",
        )
        self.act_settings = action(
            "&Settings…", self.open_settings, "Application settings",
        )

        menu = self.menuBar()
        self.file_menu = file_menu = menu.addMenu("&File")
        file_menu.addAction(self.act_chars)
        file_menu.addAction(self.act_settings)
        file_menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # Everything that refreshes data lives here, widest scope first, so
        # the common case is the first thing you land on and the specific
        # pulls are below it rather than scattered across File and Data.
        self.update_menu = update_menu = menu.addMenu("&Update")
        update_menu.addAction(self.act_sync)
        update_menu.addAction(self.act_chars_all)
        self.char_menu = update_menu.addMenu("&Character")
        self.char_menu.aboutToShow.connect(self._populate_character_menu)
        update_menu.addSeparator()
        update_menu.addAction(self.act_prices)
        update_menu.addAction(self.act_sde)
        update_menu.addAction(self.act_snapshot)

        self.act_reset_sort = QAction("Reset sort", self)
        self.act_reset_sort.triggered.connect(self._reset_sort)
        self.view_menu = view_menu = menu.addMenu("&View")
        view_menu.addAction(self.act_reset_sort)

        self.help_menu = help_menu = menu.addMenu("&Help")
        about = QAction("About", self)
        about.triggered.connect(self.about)
        help_menu.addAction(about)

    def _build_statusbar(self) -> None:
        self.task_bar = TaskBar(self.tasks, self)
        self.task_bar.cancel_requested.connect(self.tasks.cancel)
        self.statusBar().addWidget(self.task_bar, 1)

    def _refresh_status(self) -> None:
        # A bare COUNT(*) with no WHERE still has to walk the whole assets
        # table -- much cheaper per row than the DISTINCT/JOIN filter queries
        # elsewhere, but this one runs after every single job (sync, reprice,
        # snapshot, character add/remove) and at startup, on the GUI thread,
        # so it adds up the same way those did. Same fix.
        def fetch(conn):
            chars = conn.execute("SELECT COUNT(*) c FROM characters WHERE enabled=1").fetchone()["c"]
            assets = conn.execute("SELECT COUNT(*) c FROM assets").fetchone()["c"]
            build = queries.sde_build(conn)
            return chars, assets, build

        self._status_query.run(fetch, self._on_status)

    def _on_status(self, payload) -> None:
        chars, assets, build = payload
        self.task_bar.set_idle_text(
            f"{chars} character(s) · {assets:,} asset stacks · SDE build {build}"
        )

    # --------------------------------------------------------------- actions
    def _submit(self, kind, label, job, done=None, after=()):
        """Hand a job to the registry. Returns None if that kind is already
        in flight -- clicking Prices twice should not reprice twice."""
        task = self.tasks.submit(kind, label, job, done, after)
        if task is None:
            self.log.add(f"{label} is already running.")
            return None
        self.log.add(f"{label} started.")
        return task

    def sync_all(self) -> None:
        if not self._have_characters():
            return
        self._submit(
            "sync:all", "Update all",
            SyncJob(self.settings, self.tokens, snapshot=self.settings.snapshot_on_sync),
            self._after_data_change,
        )

    def sync_characters(self) -> None:
        """Every character, but no repricing and no snapshot -- the ESI pull
        on its own, for when you just want fresh assets."""
        if not self._have_characters():
            return
        self._submit(
            "sync:characters", "Update all characters",
            SyncJob(self.settings, self.tokens, reprice=False, snapshot=False),
            self._after_data_change,
        )

    def sync_character(self, character_id: int, name: str) -> None:
        self._submit(
            f"sync:{character_id}", f"Update {name}",
            SyncJob(
                self.settings, self.tokens,
                character_ids=[character_id], reprice=False, snapshot=False,
            ),
            self._after_data_change,
        )

    def reprice(self) -> None:
        self._submit(
            "prices", "Update prices",
            RepriceJob(self.settings, self.tokens), self._after_data_change,
        )

    def snapshot(self) -> None:
        # Follows any sync. A snapshot taken over half-synced assets records a
        # wrong total into history, and history is the one table we never
        # rewrite, so a bad point is permanent.
        self._submit(
            "snapshot", "Net worth snapshot",
            SnapshotJob(), self._after_data_change, after=("sync",),
        )

    def update_sde(self) -> None:
        self._submit(
            "sde", "Update game data",
            SdeUpdateJob(self.settings), self._after_data_change,
        )

    def _populate_character_menu(self) -> None:
        """Rebuilt every time the submenu opens: characters get added and
        removed while the window is up, and a stale list here would offer to
        sync someone who is gone."""
        self.char_menu.clear()
        rows = list(self.conn.execute(
            "SELECT character_id, name FROM characters WHERE enabled=1 "
            "ORDER BY name COLLATE NOCASE"
        ))
        if not rows:
            empty = self.char_menu.addAction("No characters linked")
            empty.setEnabled(False)
            return
        for row in rows:
            cid = row["character_id"]
            name = row["name"] or str(cid)
            act = self.char_menu.addAction(name)
            if self.tasks.is_active(f"sync:{cid}"):
                act.setEnabled(False)
                act.setText(f"{name}  (updating…)")
            act.triggered.connect(
                lambda _checked=False, c=cid, n=name: self.sync_character(c, n)
            )

    def _filter_assets_from_treemap(self, level: str, value: str) -> None:
        """Right-click a treemap tile -> show me those assets.

        The Assets tab filters through the omnibox now, so this adds a chip
        rather than calling an apply_external_filter() that no longer exists.
        The level keys in ROLLUP_LEVELS are deliberately the same strings as
        omni.LEVEL_KINDS, so a level is already a chip kind and needs no
        translation table between the two.
        """
        self.tabs.setCurrentWidget(self.assets)
        self._ensure_tab_loaded(self.tabs.currentIndex())
        self.assets.omnibox.add_chip(level, value)

    def _reset_sort(self) -> None:
        # The Log tab has no table/sorter at all, hence the getattr guard.
        widget = self.tabs.currentWidget()
        reset = getattr(widget, "reset_sort", None)
        if reset is not None:
            reset()

    def open_characters(self) -> None:
        """Non-modal on purpose. Adding a character now kicks off an update
        for them straight away, and a modal dialog would sit on top of the
        status bar that reports it -- hiding the progress it just caused, and
        stopping you queueing up the next character while the first one runs."""
        existing = getattr(self, "_characters_dialog", None)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        dialog = CharactersDialog(self.settings, self.tokens, self.tasks, self)
        dialog.changed.connect(self._after_data_change)
        dialog.setAttribute(Qt.WA_DeleteOnClose, False)
        self._characters_dialog = dialog   # outlives this call; it is modeless
        dialog.show()

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec():
            self.log.add("Settings saved.")

    def about(self) -> None:
        QMessageBox.about(
            self, "EVE Assets",
            "<h3>EVE Assets</h3>"
            "<p>Cross-character asset inventory and net worth tracking for EVE Online.</p>"
            f"<p>Database: <code>{DB_PATH}</code><br>"
            f"SDE build: {queries.sde_build(self.conn)}</p>"
            "<p>Item and market data from CCP's ESI and Static Data Export. "
            "Jita aggregates from Fuzzwork.<br>"
            "EVE Online and all related material are property of CCP hf.</p>",
        )

    # ------------------------------------------------------------- callbacks
    def _on_task_failed(self, label: str, message: str) -> None:
        self.log.add(f"FAILED: {label} — {message}")
        self._refresh_status()
        QMessageBox.critical(self, "Something went wrong", f"{label}\n\n{message}")

    def _on_task_finished(self, kind: str, result) -> None:
        if isinstance(result, dict):
            if result.get("cancelled"):
                self.log.add("Cancelled.")
                self._refresh_status()
                return
            if "message" in result:
                self.log.add(result["message"])
            if "prices" in result and result["prices"]:
                p = result["prices"]
                self.log.add(
                    f"Priced {p.get('total', 0):,} types "
                    f"({p.get('jita', 0):,} from the Jita order book, "
                    f"{p.get('contract_avg', 0):,} from contract averages, "
                    f"{p.get('base_price', 0):,} base price fallback)."
                )
            if result.get("characters"):
                self.log.add(f"Synced {result['characters']} character(s).")
        self._refresh_status()

    def _ensure_tab_loaded(self, index: int) -> None:
        widget = self.tabs.widget(index)
        loader = self._tab_first_load.get(widget)
        if loader is None:  # the Log tab, or nothing shown yet
            return
        if widget not in self._loaded:
            loader()
            self._loaded.add(widget)
            self._dirty.discard(widget)
        elif widget in self._dirty:
            self._tab_reload[widget]()
            self._dirty.discard(widget)

    def _after_data_change(self, _result=None) -> None:
        # Only the tab actually on screen gets reloaded right away. A tab
        # that has never been opened this session does not need refreshing at
        # all -- its eventual first_load() will pull current data anyway. A
        # tab that has been opened but is not the current one is marked dirty
        # and picks up the change lazily next time it is switched to, rather
        # than every tab re-running its query and reformatting after every
        # sync, reprice or snapshot regardless of whether anyone is looking
        # at it.
        self._dirty |= self._loaded
        self._ensure_tab_loaded(self.tabs.currentIndex())
        self._refresh_status()

    # ------------------------------------------------------------------ misc
    def _have_characters(self) -> bool:
        n = self.conn.execute("SELECT COUNT(*) c FROM characters WHERE enabled=1").fetchone()["c"]
        if n:
            return True
        QMessageBox.information(
            self, "No characters",
            "Add a character first — File → Characters… → Add character.",
        )
        return False

    def _first_run_hint(self) -> None:
        QMessageBox.information(
            self, "Set up your ESI application",
            "Before adding characters, open Settings and paste in the client ID of "
            "your application from developers.eveonline.com.\n\n"
            f"Register this callback URL on that application:\n{self.settings.redirect_uri}",
        )
        self.open_settings()


class _LogPane(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QTextEdit.NoWrap)
        layout.addWidget(self.text)

    def add(self, message: str) -> None:
        from datetime import datetime

        self.text.append(f"{datetime.now():%H:%M:%S}  {message}")
