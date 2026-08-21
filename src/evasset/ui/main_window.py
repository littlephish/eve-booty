"""Main window: toolbar, tabs, status bar."""

from __future__ import annotations

from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import db, queries
from ..config import DB_PATH, Settings
from ..esi import TokenCache
from .assets_view import AssetsView, OverviewView
from .async_query import AsyncQuery
from .characters_dialog import CharactersDialog
from .networth_view import NetWorthView
from .settings_dialog import SettingsDialog
from .wallet_view import WalletView
from .workers import RepriceJob, SdeUpdateJob, SnapshotJob, SyncJob


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.tokens = TokenCache(settings)
        self.conn = db.init()
        self.pool = QThreadPool.globalInstance()
        self._warnings: list[str] = []
        self._inflight: set = set()  # jobs still running -- see _run()

        self.setWindowTitle("EVE Assets")
        self.resize(1440, 880)

        self.tabs = QTabWidget()
        # One connection, shared down to every tab, and every tab's first
        # real query deferred until it is actually shown. Each view used to
        # open its own connection via db.init(), which re-runs the full
        # CREATE TABLE script and migration check every time -- harmless once,
        # wasteful five times over on the main thread before the window even
        # paints. And all five tabs used to reload() (query + format + resize)
        # unconditionally in their own __init__, meaning a cold start paid for
        # Assets, Overview, both Wallet tables and Net worth before you could
        # look at any of them. Now only the tab on screen loads.
        self.assets = AssetsView(self.conn, defer_load=True)
        self.overview = OverviewView(self.conn, defer_load=True)
        self.networth = NetWorthView(self.conn, defer_load=True)
        self.wallet = WalletView(self.conn, defer_load=True)
        self.log = _LogPane()

        self.tabs.addTab(self.assets, "Assets")
        self.tabs.addTab(self.overview, "Overview")
        self.tabs.addTab(self.wallet, "Wallet")
        self.tabs.addTab(self.networth, "Net worth")
        self.tabs.addTab(self.log, "Log")
        self.setCentralWidget(self.tabs)

        self._tab_first_load = {
            self.assets: self.assets.first_load,
            self.overview: self.overview.first_load,
            self.wallet: self.wallet.first_load,
            self.networth: self.networth.first_load,
        }
        self._tab_reload = {
            self.assets: lambda: (self.assets.refresh_filters(), self.assets.reload()),
            self.overview: self.overview.reload,
            self.wallet: self.wallet.reload,
            self.networth: self.networth.refresh,
        }
        self._loaded: set = set()
        self._dirty: set = set()
        self.tabs.currentChanged.connect(self._ensure_tab_loaded)
        self.overview.filter_assets_requested.connect(self._filter_assets_from_overview)
        self._status_query = AsyncQuery(self)

        self._build_actions()
        self._build_statusbar()
        self._refresh_status()
        self._ensure_tab_loaded(self.tabs.currentIndex())  # load just the visible tab

        if not settings.client_id:
            QTimer.singleShot(400, self._first_run_hint)

    # ---------------------------------------------------------------- chrome
    def _build_actions(self) -> None:
        bar = self.addToolBar("Main")
        bar.setMovable(False)

        self.act_sync = QAction("Sync all", self)
        self.act_sync.setShortcut(QKeySequence("F5"))
        self.act_sync.triggered.connect(self.sync_all)

        self.act_chars = QAction("Characters…", self)
        self.act_chars.triggered.connect(self.open_characters)

        self.act_prices = QAction("Update prices", self)
        self.act_prices.triggered.connect(self.reprice)

        self.act_snapshot = QAction("Snapshot now", self)
        self.act_snapshot.triggered.connect(self.snapshot)

        self.act_sde = QAction("Update game data", self)
        self.act_sde.triggered.connect(self.update_sde)

        self.act_settings = QAction("Settings…", self)
        self.act_settings.triggered.connect(self.open_settings)

        for act in (
            self.act_sync, self.act_chars, self.act_prices,
            self.act_snapshot, self.act_sde, self.act_settings,
        ):
            bar.addAction(act)

        menu = self.menuBar()
        file_menu = menu.addMenu("&File")
        file_menu.addAction(self.act_sync)
        file_menu.addAction(self.act_chars)
        file_menu.addAction(self.act_settings)
        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        data_menu = menu.addMenu("&Data")
        data_menu.addAction(self.act_prices)
        data_menu.addAction(self.act_snapshot)
        data_menu.addAction(self.act_sde)

        self.act_reset_sort = QAction("Reset sort", self)
        self.act_reset_sort.triggered.connect(self._reset_sort)
        view_menu = menu.addMenu("&View")
        view_menu.addAction(self.act_reset_sort)

        help_menu = menu.addMenu("&Help")
        about = QAction("About", self)
        about.triggered.connect(self.about)
        help_menu.addAction(about)

    def _build_statusbar(self) -> None:
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setVisible(False)
        self.status_label = QLabel("")
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.progress)

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
        self.status_label.setText(
            f"{chars} character(s) · {assets:,} asset stacks · SDE build {build}"
        )

    # --------------------------------------------------------------- actions
    def _run(self, job, done=None) -> None:
        self._set_busy(True)
        # The job must outlive this call. QRunnable is not a QObject, so once
        # the local goes out of scope -- immediately, since _run returns as
        # soon as the pool has the job -- nothing keeps the job or the
        # WorkerSignals QObject hanging off it alive. Collect that object
        # while the worker thread is still running and the queued finished /
        # failed emitted at the end of the job is discarded before it ever
        # reaches the GUI thread, so _set_busy(False) never runs and the
        # window stays busy forever with no error to show for it -- the sync
        # itself having completed normally. Same reason, same fix, as
        # AsyncQuery._inflight; see that module's docstring.
        self._inflight.add(job)
        job.signals.progress.connect(self._on_progress)
        job.signals.warning.connect(self.log.add)
        job.signals.failed.connect(lambda m, j=job: self._on_failed(m, j))
        job.signals.finished.connect(lambda r, j=job: self._on_finished(r, done, j))
        self.pool.start(job)

    def sync_all(self) -> None:
        if not self._have_characters():
            return
        self.log.add("Sync started.")
        self._run(
            SyncJob(self.settings, self.tokens, snapshot=self.settings.snapshot_on_sync),
            self._after_data_change,
        )

    def reprice(self) -> None:
        self.log.add("Repricing.")
        self._run(RepriceJob(self.settings, self.tokens), self._after_data_change)

    def snapshot(self) -> None:
        self._run(SnapshotJob(), self._after_data_change)

    def update_sde(self) -> None:
        self.log.add("Checking for a newer Static Data Export.")
        self._run(SdeUpdateJob(self.settings), self._after_data_change)

    def _filter_assets_from_overview(self, level: str, value: str) -> None:
        """Right-click "Filter Assets to ..." in Overview -- switch to the
        Assets tab (loading it first if it has never been shown) and apply
        the filter. AssetsView.reload() is safe to call again immediately
        after a lazy-load's own reload() -- AsyncQuery only ever delivers the
        most recent request, so calling it twice in a row just means the
        first result is quietly dropped, not that it races."""
        self.tabs.setCurrentWidget(self.assets)
        self.assets.apply_external_filter(level, value)

    def _reset_sort(self) -> None:
        # The Log tab has no table/sorter at all, hence the getattr guard.
        widget = self.tabs.currentWidget()
        reset = getattr(widget, "reset_sort", None)
        if reset is not None:
            reset()

    def open_characters(self) -> None:
        dialog = CharactersDialog(self.settings, self.tokens, self)
        dialog.changed.connect(self._after_data_change)
        dialog.exec()
        self._after_data_change(None)

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
    def _on_progress(self, message: str, pct: int) -> None:
        self.status_label.setText(message)
        self.progress.setValue(max(0, min(100, pct)))

    def _on_failed(self, message: str, job=None) -> None:
        self._inflight.discard(job)
        self._set_busy(False)
        self.log.add(f"FAILED: {message}")
        self._refresh_status()
        QMessageBox.critical(self, "Something went wrong", message)

    def _on_finished(self, result, done, job=None) -> None:
        self._inflight.discard(job)
        self._set_busy(False)
        if isinstance(result, dict):
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
        if done:
            done(result)
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

    def _set_busy(self, busy: bool) -> None:
        self.progress.setVisible(busy)
        for act in (
            self.act_sync, self.act_prices, self.act_snapshot,
            self.act_sde, self.act_chars,
        ):
            act.setEnabled(not busy)

    # ------------------------------------------------------------------ misc
    def _have_characters(self) -> bool:
        n = self.conn.execute("SELECT COUNT(*) c FROM characters WHERE enabled=1").fetchone()["c"]
        if n:
            return True
        QMessageBox.information(
            self, "No characters",
            "Add a character first — toolbar → Characters… → Add character.",
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
