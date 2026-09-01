"""Character management.

Add as many characters as you like, one SSO round trip each. Per character you
can toggle whether it is included in syncs and whether its corporation's
hangars and wallets are pulled too (that one needs the in-game roles, and the
dialog tells you when they are missing).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import db
from ..config import SCOPES, Settings
from ..esi import TokenCache
from ..esi.auth import delete_refresh_token, load_refresh_token, using_fallback_store
from .palette import SECONDARY_TEXT
from .tasks import TaskManager
from .workers import LoginJob, SyncJob

COL_NAME, COL_CORP, COL_ENABLED, COL_CORP_DATA, COL_TOKEN, COL_SCOPES, COL_LAST, COL_NOTE = range(8)
HEADERS = [
    "Character", "Corporation", "Sync", "Corp data", "Token",
    "Scopes", "Last sync", "Last result",
]


class CharactersDialog(QDialog):
    changed = Signal()

    def __init__(
        self,
        settings: Settings,
        tokens: TokenCache,
        tasks: TaskManager,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.settings = settings
        self.tokens = tokens
        # db.connect(), not db.init(): this dialog is only ever opened from
        # MainWindow, which has already run db.init(), so re-running the whole
        # schema script and migration check every time someone opens Characters
        # buys nothing. connect() caches one connection per thread, so this is
        # the same object the main window and the Stockpile tab are using.
        self.conn = db.connect()
        # Shared with the main window rather than a pool of its own: this
        # dialog is not modal any more, so its work and the window's work are
        # the same queue, shown in the same status bar, deduplicated against
        # each other. Two registries would happily sync one character twice.
        self.tasks = tasks
        self.tasks.changed.connect(self._render_tasks)
        self._pending_remove: list[int] = []

        self.setWindowTitle("Characters")
        self.resize(1000, 460)

        layout = QVBoxLayout(self)

        text = (
            "Each character is authorised separately through EVE SSO. "
            "Corp data needs Director or the matching role in-game; without it "
            "those pulls are skipped and noted in Last result."
        )
        if using_fallback_store():
            text += (
                "  No OS credential store was found on this machine, so refresh "
                "tokens are kept in a local file — treat it as a password."
            )
        blurb = QLabel(text)
        blurb.setWordWrap(True)
        blurb.setStyleSheet(f"color: {SECONDARY_TEXT};")
        layout.addWidget(blurb)

        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(COL_NOTE, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {SECONDARY_TEXT};")
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        self.btn_add = QPushButton("Add character…")
        self.btn_reauth = QPushButton("Re-authorise")
        self.btn_sync = QPushButton("Sync selected")
        self.btn_remove = QPushButton("Remove")
        for b in (self.btn_add, self.btn_reauth, self.btn_sync, self.btn_remove):
            buttons.addWidget(b)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        close_box = QDialogButtonBox(QDialogButtonBox.Close)
        close_box.rejected.connect(self.accept)
        layout.addWidget(close_box)

        self.btn_add.clicked.connect(self.add_character)
        self.btn_reauth.clicked.connect(self.add_character)  # same flow, overwrites the token
        self.btn_sync.clicked.connect(self.sync_selected)
        self.btn_remove.clicked.connect(self.remove_selected)

        self.reload()

    # ------------------------------------------------------------------ data
    def reload(self) -> None:
        rows = list(
            self.conn.execute(
                "SELECT * FROM characters ORDER BY name COLLATE NOCASE"
            )
        )
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            cid = row["character_id"]
            granted = (row["scopes"] or "").split()

            self._set(r, COL_NAME, row["name"] or str(cid), cid)
            self._set(r, COL_CORP, row["corporation_name"] or "")

            enabled = QCheckBox()
            enabled.setChecked(bool(row["enabled"]))
            enabled.stateChanged.connect(
                lambda state, c=cid: self._set_flag(c, "enabled", state == Qt.Checked.value)
            )
            self.table.setCellWidget(r, COL_ENABLED, self._center(enabled))

            corp = QCheckBox()
            corp.setChecked(bool(row["include_corp"]))
            corp.setToolTip("Pull this character's corporation hangars, wallets and orders")
            corp.stateChanged.connect(
                lambda state, c=cid: self._set_flag(c, "include_corp", state == Qt.Checked.value)
            )
            self.table.setCellWidget(r, COL_CORP_DATA, self._center(corp))

            has_token = load_refresh_token(cid) is not None
            self._set(r, COL_TOKEN, "stored" if has_token else "missing")
            self._set(r, COL_SCOPES, f"{len(granted)}/{len(SCOPES)}")
            self._set(r, COL_LAST, (row["last_sync_at"] or "never")[:19].replace("T", " "))

            note = row["last_error"] or ""
            first = note.splitlines()[0] if note else "ok"
            item = QTableWidgetItem(first)
            if note:
                item.setToolTip(note)
            self.table.setItem(r, COL_NOTE, item)

        missing_scopes = [
            r["name"] for r in rows if len((r["scopes"] or "").split()) < len(SCOPES)
        ]
        if missing_scopes:
            self.status.setText(
                "Re-authorise to pick up newly requested scopes: " + ", ".join(missing_scopes)
            )
        else:
            self.status.setText(f"{len(rows)} character(s) linked.")

    def _set(self, row: int, col: int, text: str, data=None) -> None:
        item = QTableWidgetItem(text)
        if data is not None:
            item.setData(Qt.UserRole, data)
        self.table.setItem(row, col, item)

    @staticmethod
    def _center(widget: QWidget) -> QWidget:
        holder = QWidget()
        lay = QHBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addStretch(1)
        lay.addWidget(widget)
        lay.addStretch(1)
        return holder

    def _set_flag(self, character_id: int, column: str, value: bool) -> None:
        self.conn.execute(
            f"UPDATE characters SET {column}=? WHERE character_id=?",  # column is a literal above
            (int(value), character_id),
        )
        self.changed.emit()

    def selected_ids(self) -> list[int]:
        ids = []
        for index in self.table.selectionModel().selectedRows():
            item = self.table.item(index.row(), COL_NAME)
            if item:
                ids.append(int(item.data(Qt.UserRole)))
        return ids

    # ---------------------------------------------------------------- actions
    def add_character(self) -> None:
        if not self.settings.client_id:
            QMessageBox.warning(
                self, "No client ID",
                "Set your ESI application's client ID in Settings first, and make sure "
                f"{self.settings.redirect_uri} is registered as a callback URL.",
            )
            return
        job = LoginJob(self.settings, self.tokens, SCOPES)
        task = self.tasks.submit("login", "Link character", job, done=self._after_login)
        if task is None:
            self.status.setText("Already waiting on EVE SSO in your browser.")
            return
        self.status.setText("Opening EVE SSO in your browser…")

    def _after_login(self, result: dict) -> None:
        """A freshly linked character has a token and no data. Pull it now
        rather than leaving the user to notice the empty rows and work out
        that they were supposed to press something."""
        cid = result["character_id"]
        name = result["name"]
        self.reload()
        self.changed.emit()
        started = self.tasks.submit(
            f"sync:{cid}", f"Update {name}",
            SyncJob(
                self.settings, self.tokens,
                character_ids=[cid], reprice=False, snapshot=False,
            ),
            done=self._after_sync,
        )
        self.status.setText(
            f"Linked {name}. Pulling their data now."
            if started else
            f"Linked {name}. An update is already running for them."
        )

    def _after_sync(self, result: dict) -> None:
        self.reload()
        self.changed.emit()

    def sync_selected(self) -> None:
        ids = self.selected_ids()
        if not ids:
            QMessageBox.information(self, "Nothing selected", "Select one or more characters.")
            return
        # One task per character rather than one covering all of them, so each
        # can be cancelled, retried or removed independently.
        started = 0
        for cid in ids:
            row = self.conn.execute(
                "SELECT name FROM characters WHERE character_id=?", (cid,)
            ).fetchone()
            name = (row["name"] if row else None) or str(cid)
            if self.tasks.submit(
                f"sync:{cid}", f"Update {name}",
                SyncJob(
                    self.settings, self.tokens,
                    character_ids=[cid], reprice=False, snapshot=False,
                ),
                done=self._after_sync,
            ):
                started += 1
        skipped = len(ids) - started
        self.status.setText(
            f"Updating {started} character(s)."
            + (f" {skipped} already running." if skipped else "")
        )

    def remove_selected(self) -> None:
        ids = self.selected_ids()
        if not ids:
            return
        names = []
        for cid in ids:
            row = self.conn.execute(
                "SELECT name FROM characters WHERE character_id=?", (cid,)
            ).fetchone()
            names.append(row["name"] if row else str(cid))
        confirm = QMessageBox.question(
            self, "Remove characters",
            "Remove " + ", ".join(names) + "?\n\n"
            "Their stored token, assets and current balances go too. "
            "Net worth history is kept so the chart stays intact.",
        )
        if confirm != QMessageBox.Yes:
            return

        # An update still running for one of these would write its rows back
        # after the delete, leaving assets owned by a character that no longer
        # exists and an UPDATE against a row that is gone. Stop it first. A
        # whole-estate sync counts too -- it covers these characters as well.
        kinds = tuple(f"sync:{cid}" for cid in ids) + ("sync:all", "sync:characters")
        if self.tasks.cancel_kinds(kinds):
            # Cancellation is cooperative, so the job stops at its next
            # checkpoint rather than now. Hold the delete until it has.
            self._pending_remove = ids
            self.status.setText("Stopping updates before removing…")
            self._render_tasks()
            return
        self._remove_now(ids)

    def _remove_now(self, ids: list[int]) -> None:
        for cid in ids:
            for table in (
                "assets", "wallets", "market_orders", "contracts", "industry_jobs", "blueprints"
            ):
                self.conn.execute(
                    f"DELETE FROM {table} WHERE owner_type='character' AND owner_id=?", (cid,)
                )
            self.conn.execute("DELETE FROM characters WHERE character_id=?", (cid,))
            delete_refresh_token(cid)
            self.tokens.forget(cid)
        self.status.setText(f"Removed {len(ids)} character(s).")
        self.reload()
        self.changed.emit()

    # ------------------------------------------------------------------- misc
    def _render_tasks(self) -> None:
        """The dialog no longer owns any progress of its own -- the status bar
        does. All that is left here is the SSO round trip, which is worth
        calling out because it is waiting on a browser window rather than on
        us, and the deferred removal."""
        login_running = self.tasks.is_active("login")
        self.progress.setVisible(login_running)
        if login_running:
            self.progress.setRange(0, 0)     # waiting on a human, not on bytes
        self.btn_add.setEnabled(not login_running)
        self.btn_reauth.setEnabled(not login_running)

        if self._pending_remove:
            kinds = tuple(f"sync:{cid}" for cid in self._pending_remove)
            still_going = self.tasks.any_active(kinds + ("sync:all", "sync:characters"))
            if not still_going:
                ids, self._pending_remove = self._pending_remove, []
                self._remove_now(ids)
