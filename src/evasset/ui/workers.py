"""Background jobs. Anything that touches the network runs here, never on the
Qt main thread, and each worker opens its own SQLite connection."""

from __future__ import annotations

import traceback

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from .. import abyssal, db, janice, networth, pricing, sde, updater
from ..config import Settings
from ..esi import ESIClient, TokenCache
from ..esi.auth import login as sso_login
from ..esi.sync import Syncer
from ..logsetup import LOGGER


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


class AppraiseJob(Job):
    """Create a Janice appraisal from a multibuy list.

    A network call, so it belongs off the GUI thread like every other one. It
    is also allowed to fail without that being an error the user has to
    acknowledge: the caller falls back to the clipboard, which is what the
    button did before there was an API to call.
    """

    def __init__(self, text: str, settings: Settings):
        super().__init__()
        self.text = text
        self.settings = settings

    def run_job(self):
        self._progress("Appraising", 30)
        try:
            appraisal = janice.create(self.text, self.settings)
        except janice.JaniceError as exc:
            LOGGER.warning("appraisal failed: %s", exc)
            return {"error": str(exc)}
        LOGGER.info(
            "appraisal %s: %d priced, %d unrecognised", appraisal.code,
            appraisal.priced, appraisal.failed,
        )
        return {"appraisal": appraisal}


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
                    self._progress(msg, 70 + int(pct * 20 / 100))

                stats = pricing.refresh_prices(conn, client, self.settings, price_relay)
            else:
                stats = {}

            # After the assets are in and before the snapshot: the rolls are
            # keyed on item_ids the sync just wrote, and the snapshot does
            # not depend on them. One public request per new abyssal item,
            # so this is opt-in (Settings -> Behaviour) and only ever asks
            # for items with no stored answer yet.
            rolls = None
            if self.settings.abyssal_stats_on_sync and not self.cancelled:
                rolls = abyssal.fetch_rolls(
                    conn, client,
                    progress=_abyssal_relay(self._progress, 90, 7),
                    should_stop=lambda: self.cancelled,
                )

            if self.snapshot:
                self._progress("Recording net worth snapshot", 97)
                networth.take_snapshot(conn)
                networth.prune_snapshots(conn)

            self._progress("Sync complete", 100)
            for w in warnings:
                self.signals.warning.emit(w)
            result = {"characters": len(chars), "prices": stats, "warnings": warnings}
            if rolls is not None:
                result["abyssal"] = rolls
            return result
        finally:
            client.close()


def _abyssal_relay(progress, base: int, span: int):
    """Adapt abyssal.fetch_rolls' (done, total, message) progress to the
    job's (message, percent) signal, squeezed into base..base+span."""
    def relay(done: int, total: int, message: str) -> None:
        fraction = done / total if total else 1.0
        progress(message, base + int(fraction * span))
    return relay


class AbyssalStatsJob(Job):
    """Fetch the rolled attributes of every abyssal item ESI has not yet been
    asked about, one public request per item.

    Its own job rather than a SyncJob flag because the estate can hold
    hundreds of mutated modules and ESI has no batch route (research notes,
    2026-09-01): a first fetch is minutes of sequential requests, which is
    something to start on purpose from the Update menu, not a cost every
    routine sync silently pays. Items answered once are never asked again;
    retry_missing re-asks the ones ESI 404'd.
    """

    def __init__(
        self,
        settings: Settings,
        tokens: TokenCache,
        item_ids: list[int] | None = None,
        retry_missing: bool = False,
    ):
        super().__init__()
        self.settings = settings
        self.tokens = tokens
        self.item_ids = item_ids
        self.retry_missing = retry_missing

    def run_job(self):
        conn = db.init()
        client = ESIClient(self.settings, self.tokens)
        try:
            self._progress("Looking for abyssal items to fetch", 0)
            result = abyssal.fetch_rolls(
                conn, client,
                progress=_abyssal_relay(self._progress, 0, 100),
                should_stop=lambda: self.cancelled,
                item_ids=self.item_ids,
                retry_missing=self.retry_missing,
            )
        finally:
            client.close()
        if self.cancelled:
            self._progress("Cancelled", 100)
            return {"cancelled": True, **result}
        self._progress("Abyssal stats complete", 100)
        return dict(result)


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
