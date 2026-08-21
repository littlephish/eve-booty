"""A pooled job must outlive the call that started it.

QRunnable is not a QObject, so once the local holding a job goes out of scope
-- which happens the instant the starting method returns -- nothing keeps the
job, or the WorkerSignals QObject hanging off it, alive. Collect that while
the worker thread is still running and Qt drops the queued finished/failed
signal on the floor: the work completes, but the GUI thread never hears about
it, so whatever the callback was going to do (clear the busy flag, re-enable
the toolbar) never happens.

This is the bug that left the window stuck busy after a completed Sync All.
AsyncQuery already guards against it with self._inflight; MainWindow._run and
CharactersDialog did not.

There is deliberately no test that reproduces the unguarded case. Doing so
means emitting through a signal whose Python wrapper has been collected, and
that does not merely drop the signal -- it takes the interpreter down with an
access violation partway through the run, killing every test after it. The
reproduction lives in the commit message instead.
"""

from __future__ import annotations

import gc
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtCore = pytest.importorskip("PySide6.QtCore")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from evasset.ui.workers import Job  # noqa: E402

JOBS = 25


class _Sleeper(Job):
    """Long enough that the starting frame is long gone before it finishes."""

    def run_job(self):
        time.sleep(0.05)
        return {"ok": True}


@pytest.fixture
def app():
    existing = QtWidgets.QApplication.instance()
    yield existing or QtWidgets.QApplication([])


def _drain(app, delivered: list, expected: int, timeout_ms: int = 8000):
    """Spin the event loop, forcing GC, until every job reports or we give up."""
    loop = QtCore.QEventLoop()
    churn = QtCore.QTimer()
    churn.timeout.connect(gc.collect)
    churn.start(5)
    deadline = QtCore.QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    deadline.start(timeout_ms)

    poll = QtCore.QTimer()
    poll.timeout.connect(lambda: loop.quit() if len(delivered) >= expected else None)
    poll.start(10)

    loop.exec()
    churn.stop()
    poll.stop()
    QtCore.QThreadPool.globalInstance().waitForDone(3000)


def test_job_kept_in_an_inflight_set_always_reports_back(app):
    """The fix: hold the job until it reports, exactly as AsyncQuery does."""
    delivered: list = []
    inflight: set = set()

    def start():  # the new MainWindow._run() shape
        job = _Sleeper()
        inflight.add(job)
        job.signals.finished.connect(
            lambda r, j=job: (inflight.discard(j), delivered.append(r))
        )
        QtCore.QThreadPool.globalInstance().start(job)

    for _ in range(JOBS):
        start()
    _drain(app, delivered, JOBS)

    assert len(delivered) == JOBS
    assert not inflight, "every job should have been released once it reported"
