"""Background jobs. Anything that touches the network runs here, never on the
Qt main thread, and each worker opens its own SQLite connection."""

from __future__ import annotations

import traceback

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from .. import db, networth, pricing, sde, updater
from ..config import Settings
from ..esi import ESIClient, TokenCache
from ..esi.auth import login as sso_login
from ..esi.sync import Syncer


class WorkerSignals(QObject):
    progress = Signal(str, int)
    message = Signal(str)
    warning = Signal(str)
    failed = Signal(str)
    finished = Signal(object)


class Job(QRunnable):
    def __init__(self):
        super().__init__()
        self.signals = WorkerSignals()
        # TaskManager owns the Python-side lifetime of every job and may still
        # want to call cancel() on one that has just finished. Letting Qt
        # delete the C++ half out from under that wrapper is how you turn a
        # tidy little race into an access violation, so ownership stays in one
        # place: Python's, until the registry lets go.
        self.setAutoDelete(False)
        self._cancelled = False

    def cancel(self) -> None:
        """Ask the job to stop. Cooperative -- run_job has to check."""
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def _progress(self, msg: str, pct: int) -> None:
        self.signals.progress.emit(msg, pct)

    def run_job(self):  # override
        raise NotImplementedError

    @Slot()
    def run(self) -> None:
        try:
            result = self.run_job()
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            self.signals.failed.emit(f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
        else:
            self.signals.finished.emit(result)


class StartupInitJob(Job):
    """Runs db.init() -- the schema script, the migration check, and (the
    first time after an upgrade that adds one) building a brand new index
    over however many rows are already in the table -- off the GUI thread.

    MainWindow.__init__ used to call db.init() itself, directly, before the
    window was ever shown. Since window construction happens before
    QApplication.exec() starts pumping the event loop, that was blocking
    time during which the app was not just slow, it genuinely was not
    processing window messages at all -- which is exactly what makes an OS
    report a process as "not responding" rather than just looking idle.
    Running it here first, behind a splash screen, means MainWindow's own
    db.init() call (on the main thread, once this has already primed the
    schema on disk) finds nothing left to do.
    """

    def run_job(self):
        db.init()
        return None



class UpdateCheckJob(Job):
    """Ask GitHub whether there is a newer release, and fetch it if so.

    Check and download are one job rather than two because the answer to
    "is there an update" is only actionable with the zip in hand, and a second
    round trip through the task queue to get it buys nothing. Returns None when
    already current, so the caller can tell "nothing to do" from "here it is".
    """

    def run_job(self):
        self._progress("Checking for updates", 5)
        release = updater.check()
        if release is None:
            return None
        if self.cancelled:
            return None
        self._progress(f"Downloading {release.version}", 10)
        archive = updater.download(
            release,
            progress=lambda pct: self._progress(
                f"Downloading {release.version}", 10 + int(pct * 0.85)
            ),
        )
        self._progress("Unpacking", 97)
        folder = updater.extract(archive)
        return {"release": release, "folder": str(folder)}

class SdeCheckJob(Job):
    """Ask CCP which SDE build is current. No download.

    Split from SdeUpdateJob because the question and the answer have wildly
    different costs: one 80-byte GET against about 95 MB. Startup can afford
    the first and must not assume the second.

    Bounded by sde.STARTUP_TIMEOUT. Nothing waits on this -- it is a pool
    thread -- but an unbounded call parks that thread and a task bar entry for
    however long CCP takes, to learn something only worth knowing promptly.
    Missing game data needs no network to detect, so the case that matters is
    never the one that can time out.
    """

    def __init__(self, settings):
        super().__init__()
        self.settings = settings

    def run_job(self):
        conn = db.connect()
        if not sde.due_for_check(conn):
            return None
        status = sde.check(conn, self.settings, timeout=sde.STARTUP_TIMEOUT)
        if not status.needed:
            return None
        if status.stale and sde.was_skipped(conn, status.latest):
            # Declined already. A missing SDE is still raised every launch,
            # because that state is broken rather than out of date.
            return None
        return {"status": status}


class SdeUpdateJob(Job):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings

    def run_job(self):
        conn = db.init()
        updated, build = sde.ensure_current(conn, self.settings, self._progress)
        return {
            "updated": updated,
            "build": build,
            "message": f"SDE build {build}" + (" imported" if updated else " already current"),
        }


class LoginJob(Job):
    """Runs the SSO round trip off the UI thread so the window stays alive
    while the browser is open."""

    def __init__(self, settings: Settings, tokens: TokenCache, scopes: list[str]):
        super().__init__()
        self.settings = settings
        self.tokens = tokens
        self.scopes = scopes

    def run_job(self):
        self._progress("Waiting for EVE SSO in your browser", 10)
        ts = sso_login(self.settings, self.scopes)
        self.tokens.put(ts)
        conn = db.init()
        client = ESIClient(self.settings, self.tokens)
        try:
            Syncer(conn, client, self.settings).register_character(
                ts.character_id, ts.character_name, ts.scopes
            )
        finally:
            client.close()
        self._progress(f"Linked {ts.character_name}", 100)
        return {"character_id": ts.character_id, "name": ts.character_name}


class SyncJob(Job):
    """Full refresh: characters -> prices -> snapshot."""

    def __init__(
        self,
        settings: Settings,
        tokens: TokenCache,
        character_ids: list[int] | None = None,
        reprice: bool = True,
        snapshot: bool = True,
    ):
        super().__init__()
        self.settings = settings
        self.tokens = tokens
        self.character_ids = character_ids
        self.reprice = reprice
        self.snapshot = snapshot

    def run_job(self):
        conn = db.init()
        client = ESIClient(self.settings, self.tokens)
        warnings: list[str] = []
        try:
            syncer = Syncer(conn, client, self.settings)
            chars = syncer.enabled_characters()
            if self.character_ids:
                wanted = set(self.character_ids)
                chars = [c for c in chars if c["character_id"] in wanted]
            if not chars:
                return {"characters": 0, "warnings": ["No enabled characters to sync."]}

            done_count = 0
            for i, row in enumerate(chars):
                if self.cancelled:
                    break
                base = int(i * 70 / len(chars))
                span = int(70 / len(chars))

                def relay(msg, pct, _b=base, _s=span):
                    self._progress(msg, _b + int(pct * _s / 100))

                warnings += syncer.sync_character(
                    row, relay, should_stop=lambda: self.cancelled
                )
                done_count += 1

            if self.cancelled:
                self._progress("Cancelled", 100)
                for w in warnings:
                    self.signals.warning.emit(w)
                return {"cancelled": True, "characters": done_count, "warnings": warnings}

            if self.reprice:
                def price_relay(msg, pct):
                    self._progress(msg, 70 + int(pct * 25 / 100))

                stats = pricing.refresh_prices(conn, client, self.settings, price_relay)
            else:
                stats = {}

            if self.snapshot:
                self._progress("Recording net worth snapshot", 97)
                networth.take_snapshot(conn)
                networth.prune_snapshots(conn)

            self._progress("Sync complete", 100)
            for w in warnings:
                self.signals.warning.emit(w)
            return {"characters": len(chars), "prices": stats, "warnings": warnings}
        finally:
            client.close()


class RepriceJob(Job):
    def __init__(self, settings: Settings, tokens: TokenCache, snapshot: bool = False):
        super().__init__()
        self.settings = settings
        self.tokens = tokens
        self.snapshot = snapshot

    def run_job(self):
        conn = db.init()
        client = ESIClient(self.settings, self.tokens)
        try:
            stats = pricing.refresh_prices(conn, client, self.settings, self._progress)
            if self.snapshot:
                networth.take_snapshot(conn)
        finally:
            client.close()
        return {"prices": stats}


class SnapshotJob(Job):
    def run_job(self):
        conn = db.init()
        n = networth.take_snapshot(conn)
        return {"snapshots": n}
