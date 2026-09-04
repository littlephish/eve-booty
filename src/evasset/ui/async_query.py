"""Run a DB read off the GUI thread and deliver the result back onto it.

Filtering and searching used to call straight into sqlite3 from inside the
Qt signal handler -- a combobox's currentIndexChanged, a debounced textChanged.
That runs on the GUI thread, so for as long as the query takes, Qt cannot
repaint or accept input: no progress bar, no spinner, just a window that does
not respond, which reads exactly like a crash even though the query was going
to finish eventually. Optimising the query or the model on the Python side
only moves the threshold at which a big enough table (or the wallet history a
few months in) freezes it again. Running it in the background is the actual
fix.

Rapid input -- typing quickly in search, clicking through filters -- fires off
several queries in succession, and there is no guarantee they finish in the
order they started. AsyncQuery tags each with a generation number and only
delivers the result if it is still the latest one requested, so a slow query
superseded by a newer one is simply dropped instead of racing it and possibly
overwriting fresher results with stale ones.

AsyncQuery.run() also keeps a strong reference to every job it starts, in
self._inflight, until that job reports back. QRunnable is not a QObject, so
nothing about Qt's own object model keeps a job (or the QObject holding its
signals) alive once the Python-side variable that created it goes out of
scope -- and it does, immediately, since run() returns right after handing
the job to the thread pool. Confirmed by reproducing it standalone: without
this, a job that finishes after its creating call has returned can have its
signal object garbage-collected while still running on a worker thread,
which either silently drops the result (the callback never fires) or
segfaults the process (the worker thread emits through a signal whose Python
wrapper no longer exists). Both were reproducible before self._inflight was
added.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from .. import db


class _QuerySignals(QObject):
    done = Signal(int, object)
    failed = Signal(int, str)


class _QueryJob(QRunnable):
    def __init__(self, generation: int, fn: Callable):
        super().__init__()
        self.generation = generation
        self.fn = fn
        self.signals = _QuerySignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            # One connection per pooled thread, reused across jobs (see
            # db.connect). Schema/migration were already brought current by
            # the main thread's db.init() at startup, so there is no need to
            # repeat that here -- just the query.
            conn = db.connect()
            result = self.fn(conn)
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller
            self._emit(self.signals.failed, self.generation, str(exc))
        else:
            self._emit(self.signals.done, self.generation, result)

    @staticmethod
    def _emit(signal, *args) -> None:
        """Deliver a result unless the window went away while we were querying.

        Closing a view with a query still running left the job holding signals
        whose C++ side Qt had already destroyed, and emitting on those raises

            RuntimeError: Signal source has been deleted

        out of a pool thread, where nothing is waiting to catch it. Harmless in
        that the answer was not wanted any more, but it printed a traceback
        over a clean shutdown, and a traceback nobody can act on trains people
        to ignore tracebacks.
        """
        try:
            signal.emit(*args)
        except RuntimeError:
            pass


class AsyncQuery(QObject):
    """One of these per view. Call run() every time the view would previously
    have queried inline; only the most recently started call's result (or
    error) actually reaches your callback."""

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._generation = 0
        self._inflight: set[_QueryJob] = set()

    def run(
        self,
        fn: Callable,
        on_done: Callable,
        on_failed: Callable[[str], None] | None = None,
    ) -> None:
        self._generation += 1
        job = _QueryJob(self._generation, fn)
        self._inflight.add(job)  # see module docstring -- must outlive this call
        job.signals.done.connect(lambda gen, result: self._finish(job, gen, result, on_done))
        job.signals.failed.connect(lambda gen, msg: self._finish(job, gen, msg, on_failed))
        QThreadPool.globalInstance().start(job)

    def cancel(self) -> None:
        """Stop caring about whatever is in flight.

        The job itself still runs to completion -- a sqlite3 call in progress
        on a pool thread cannot be interrupted -- but bumping the generation
        means its result is dropped instead of delivered. That matters for a
        dialog, which unlike a tab can be closed and destroyed while a query
        is still running: without this the callback fires into widgets whose
        C++ side is already gone, which surfaces as a RuntimeError traceback
        on stderr from a thread nobody is watching.
        """
        self._generation += 1

    def _finish(self, job: _QueryJob, generation: int, payload, callback: Callable | None) -> None:
        self._inflight.discard(job)
        if callback is not None and generation == self._generation:
            callback(payload)
