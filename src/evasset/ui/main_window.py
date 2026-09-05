"""Main window: menus, tabs, status bar."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMainWindow,
    QMessageBox,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import db, diagnostics, queries, sde, updater
from ..config import DB_PATH, PROJECT_URL, Settings
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
from .workers import (
    AbyssalStatsJob,
    RepriceJob,
    SdeCheckJob,
    SdeUpdateJob,
    SnapshotJob,
    SyncJob,
    UpdateCheckJob,
)

# meta key remembering that the one-time "fetch rolls during sync?" offer
# has been made, so it is never asked twice however many manual runs follow.
ABYSSAL_OFFER_META_KEY = "abyssal_sync_offer_made"


def abyssal_summary_line(stats: dict) -> str:
    """The Log line for one abyssal.fetch_rolls result."""
    return (
        f"Abyssal stats: fetched {stats.get('fetched', 0):,}, "
        f"{stats.get('missing', 0):,} not known to ESI, "
        f"{stats.get('failed', 0):,} failed (will retry)"
    )

# Who to credit in About, linked to their public character page. These are
# in-game character names given deliberately as attribution -- the one place
# AGENTS.md's "never commit a real character name" rule does not apply, because
# nothing here is anybody's account data. Verified against ESI
# /characters/{id}/ rather than typed from memory: a misspelled name next to a
# working link is worse than no credit.
AUTHORS = (
    ("LittlePhish", 972237621),
    ("Ulatlar Brimfire", 94872713),
)
EVEWHO = "https://evewho.com/character/{id}"


def _author_links() -> str:
    """The credit line, each name linking to its public character page.

    QMessageBox's label already has openExternalLinks set and
    LinksAccessibleByMouse in its interaction flags, so these open in a real
    browser without any extra wiring -- checked rather than assumed, because a
    credit that renders as blue text and does nothing when clicked is worse
    than plain text.
    """
    return ", ".join(
        f'<a href="{EVEWHO.format(id=cid)}">{name}</a>' for name, cid in AUTHORS
    )


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.tokens = TokenCache(settings)
        self.conn = db.init()
        self._warnings: list[str] = []
        self.tasks = TaskManager(self)

        self.setWindowTitle("EVE Booty")
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
        self.assets.abyssal_fetch_requested.connect(self._fetch_abyssal_items)
        self._status_query = AsyncQuery(self)

        # Connected here rather than next to the TaskManager itself: warned
        # lands in the log pane, which does not exist until the tabs are up.
        self.tasks.warned.connect(self.log.add)
        self.tasks.failed.connect(self._on_task_failed)
        self.tasks.finished.connect(self._on_task_finished)

        self._build_actions()
        self._build_statusbar()
        self._refresh_status()
        self._auto_refresh_sde_tables()
        self._ensure_tab_loaded(self.tabs.currentIndex())  # load just the visible tab

        # Deferred so the window is up first: this is a network call, and a
        # dialog that appears before the app has drawn reads as a crash.
        if settings.check_sde_on_startup:
            QTimer.singleShot(1200, self._check_game_data)

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
        self.act_abyssal = action(
            "A&byssal stats", self.update_abyssal,
            "Fetch the rolled attributes of abyssal modules not yet asked of ESI",
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
        update_menu.addAction(self.act_abyssal)
        update_menu.addAction(self.act_snapshot)

        self.act_reset_sort = QAction("Reset sort", self)
        self.act_reset_sort.triggered.connect(self._reset_sort)
        self.view_menu = view_menu = menu.addMenu("&View")
        view_menu.addAction(self.act_reset_sort)

        self.help_menu = help_menu = menu.addMenu("&Help")
        about = QAction("About", self)
        about.triggered.connect(self.about)
        self.act_update = QAction("Check for &updates…", self)
        self.act_update.triggered.connect(self.check_for_updates)
        # Only the packaged build can replace itself; from source this would
        # be an offer to overwrite somebody's git checkout.
        self.act_update.setEnabled(updater.can_update())
        if not updater.can_update():
            self.act_update.setToolTip("Updates apply to the installed build only")
        help_menu.addAction(self.act_update)
        self.act_diagnostics = QAction("&Diagnostics…", self)
        self.act_diagnostics.setToolTip(
            "A summary to paste into a bug report: versions, row counts and "
            "what the last sync said"
        )
        self.act_diagnostics.triggered.connect(self.show_diagnostics)
        help_menu.addAction(self.act_diagnostics)
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

    def _auto_refresh_sde_tables(self) -> None:
        """Re-import the SDE at startup when this build reads tables the
        installed import never filled (the dogma tables behind abyssal
        rolls, for one). Only for an estate that already has an SDE: the
        importer reuses the cached zip when the build is current, so this
        is a few seconds of local work, whereas a fresh install with no SDE
        at all would be a surprise download before the user has been shown
        the Update menu that normally starts one."""
        if db.get_meta(self.conn, "sde_build") is None or not sde.tables_stale(self.conn):
            return
        self._submit(
            "sde", "Refresh game data tables",
            SdeUpdateJob(self.settings), self._after_data_change,
        )

    def update_abyssal(self) -> None:
        # Follows any sync for the same reason the snapshot does: the fetch
        # is keyed on the item_ids in the assets table, and a sync replaces
        # that table one owner at a time.
        self._submit(
            "abyssal", "Abyssal stats",
            AbyssalStatsJob(self.settings, self.tokens), self._on_abyssal_done,
            after=("sync",),
        )

    def _fetch_abyssal_items(self, item_ids: list) -> None:
        """The Assets tab's abyssal card asked for specific items' rolls: the
        same job and the same after-sync ordering as the Update menu's full
        run, scoped to the ids the card resolved."""
        self._submit(
            "abyssal", "Abyssal stats",
            AbyssalStatsJob(self.settings, self.tokens, item_ids=list(item_ids)),
            self._on_abyssal_done,
            after=("sync",),
        )

    def _on_abyssal_done(self, result) -> None:
        self.log.add(abyssal_summary_line(result))
        self._after_data_change()
        if result.get("cancelled") or not result.get("fetched"):
            return
        # The first run that actually brought rolls home is the one moment
        # the sync-time switch is worth explaining; the meta flag makes it
        # the only moment. Set before the question so a crash or a second
        # run mid-dialog still counts as asked.
        if (
            self.settings.abyssal_stats_on_sync
            or db.get_meta(self.conn, ABYSSAL_OFFER_META_KEY) is not None
        ):
            return
        db.set_meta(self.conn, ABYSSAL_OFFER_META_KEY, "1")
        answer = QMessageBox.question(
            self, "Abyssal stats",
            "Fetch rolls for new abyssal items automatically during every sync?\n\n"
            "Items already fetched are never asked for again, so this only costs "
            "one ESI request per new abyssal module. You can change it later under "
            "Settings → Behaviour.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.settings.abyssal_stats_on_sync = True
            self.settings.save()
            self.log.add("Abyssal rolls will be fetched during sync.")

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

    def check_for_updates(self) -> None:
        """Check, download and hand over to the swap helper.

        The download runs through the same TaskManager as every other long
        job, so it shows in the status bar and cannot be started twice.
        """
        if not updater.can_update():
            QMessageBox.information(
                self, "Updates",
                "Automatic updates apply to the installed Windows build. "
                "This copy is running from source, so update it with git.",
            )
            return
        self.tasks.submit("update", "Check for updates", UpdateCheckJob(),
                          done=self._on_update_ready)

    def _on_update_ready(self, result) -> None:
        if not result:
            QMessageBox.information(self, "Updates", "EVE Booty is up to date.")
            return
        release = result["release"]
        if _UpdateDialog(release, self).exec() != QDialog.Accepted:
            return
        from pathlib import Path
        if updater.apply(Path(result["folder"])):
            # The helper waits for this exe to unlock before it mirrors, so
            # quitting is the last step of applying the update, not a
            # side effect of it.
            QApplication.quit()
        else:
            QMessageBox.warning(
                self, "Update failed",
                "The update helper could not be started. "
                "Download the latest release manually instead.",
            )

    def _check_game_data(self) -> None:
        """Ask whether newer game data exists, and offer to fetch it.

        Deliberately a prompt rather than an automatic download. The payload is
        around 95 MB, which is not a thing to start on somebody's connection
        without asking, and a metered or slow link makes that decision for
        them.
        """
        self.tasks.submit(
            "sde-check", "Checking game data", SdeCheckJob(self.settings),
            done=self._on_game_data_checked,
        )

    def _on_game_data_checked(self, result) -> None:
        if not result:
            return  # current, recently checked, or already declined
        status = result["status"]

        if status.missing:
            # Not an update, a prerequisite. Nothing can be shown without it:
            # the assets query inner joins sde_types, so the table renders
            # empty however much has been synced.
            title = "Game data needed"
            text = (
                "<b>EVE Booty needs CCP's game data before it can show anything.</b>"
                "<br><br>Without it the Assets tab is empty even after a sync, "
                "because every item's name, group and volume comes from it."
                "<br><br>Download it now? It is about 95 MB and imports in a "
                "few seconds."
            )
        else:
            title = "New game data available"
            text = (
                f"<b>Game data build {status.latest} is available.</b>"
                f"<br>You have build {status.installed}."
                "<br><br>CCP publishes a new build most patch days; updating "
                "keeps new items, structures and stations resolving by name."
                "<br><br>Download it now? It is about 95 MB."
            )

        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setTextFormat(Qt.RichText)
        box.setText(text)
        download = box.addButton("Download now", QMessageBox.AcceptRole)
        box.addButton("Not now", QMessageBox.RejectRole)
        box.exec()

        if box.clickedButton() is download:
            self.update_sde()
        elif status.stale:
            # Remember the refusal so the same build is not offered on every
            # launch. A missing SDE is deliberately not remembered: that state
            # is broken, and should be raised again next time.
            sde.skip_build(self.conn, status.latest)
            self.log.add(
                f"Game data build {status.latest} skipped. "
                "Update -> Game data when you want it."
            )

    def show_diagnostics(self) -> None:
        """The report, with a button that puts it on the clipboard.

        Read-only and copyable rather than written to a file the user then has
        to find: the whole point is that "paste this into the issue" is one
        click from the menu.
        """
        text = diagnostics.report(self.conn, self.settings)
        dialog = QDialog(self)
        dialog.setWindowTitle("Diagnostics")
        dialog.resize(720, 560)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Paste this into a bug report."))

        view = QTextEdit()
        view.setReadOnly(True)
        view.setLineWrapMode(QTextEdit.NoWrap)
        view.setFontFamily("Consolas")
        view.setPlainText(text)
        layout.addWidget(view, 1)

        buttons = QDialogButtonBox()
        copy = buttons.addButton("Copy to clipboard", QDialogButtonBox.ActionRole)
        buttons.addButton("Close", QDialogButtonBox.RejectRole)
        copy.clicked.connect(lambda: QApplication.clipboard().setText(text))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def about(self) -> None:
        QMessageBox.about(
            self, "EVE Booty",
            "<h3>EVE Booty</h3>"
            "<p>Everything you own in New Eden, counted and valued: "
            "cross-character asset inventory and net worth tracking.</p>"
            f"<p>Version: {updater.version_string()}<br>"
            f"Database: <code>{DB_PATH}</code><br>"
            f"SDE build: {queries.sde_build(self.conn)}</p>"
            f"<p>Built by {_author_links()}<br>"
            f'<a href="{PROJECT_URL}">{PROJECT_URL}</a></p>'
            "<p>Item and market data from CCP's ESI and Static Data Export. "
            "Jita aggregates from Fuzzwork.<br>"
            "© 2014 CCP hf. All rights reserved. &quot;EVE&quot;, &quot;EVE Online&quot;, "
            "&quot;CCP&quot;, and all related logos and images are trademarks or "
            "registered trademarks of CCP hf.</p>",
        )

    # ------------------------------------------------------------- callbacks
    def _on_task_failed(self, label: str, message: str) -> None:
        self.log.add(f"FAILED: {label} - {message}")
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
            if isinstance(result.get("abyssal"), dict):
                self.log.add(abyssal_summary_line(result["abyssal"]))
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
            "Add a character first - File → Characters… → Add character.",
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


class _UpdateDialog(QDialog):
    """What is in the update, before being asked to install it.

    A plain yes/no box naming two version numbers asks somebody to approve a
    change they have been told nothing about -- and by the time it appears the
    build has already been downloaded, so the only thing left to decide is
    whether to trust it. The release notes are the evidence for that decision
    and were being fetched and discarded.

    Rendered as Markdown because that is what GitHub returns, and shown in a
    QTextBrowser rather than a QMessageBox so a long list of changes scrolls
    instead of growing the dialog off the screen. The browser stays read-only
    and opens links externally; it renders no scripts, so notes are display
    data and nothing more.
    """

    def __init__(self, release, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Update available")
        self.resize(560, 460)
        layout = QVBoxLayout(self)

        heading = QLabel(
            f"<b>{release.version}</b> is available. You have {release.current}."
        )
        heading.setTextFormat(Qt.RichText)
        layout.addWidget(heading)

        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        if release.notes:
            notes.setMarkdown(release.notes)
        else:
            # A release can be published with an empty body. Say so rather
            # than showing a blank panel that looks like a failure to load.
            notes.setPlainText("No release notes were published for this version.")
        layout.addWidget(notes, 1)

        if release.page_url:
            link = QLabel(f'<a href="{release.page_url}">View this release on GitHub</a>')
            link.setOpenExternalLinks(True)
            layout.addWidget(link)

        layout.addWidget(QLabel("EVE Booty will close, swap itself for the new build, and reopen."))

        buttons = QDialogButtonBox()
        install = buttons.addButton("Update and restart", QDialogButtonBox.AcceptRole)
        buttons.addButton("Not now", QDialogButtonBox.RejectRole)
        install.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


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
