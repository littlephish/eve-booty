"""A background job outliving the window that started it must not explode.

Closing the app during a sync produced this, from a pool thread, over what
was otherwise a clean exit:

    RuntimeError: Signal source has been deleted
    During handling of the above exception, another exception occurred:
    RuntimeError: Signal source has been deleted

Nothing waits for in-flight jobs at shutdown -- app.exec() returns and Qt
destroys its objects while the worker thread is still going -- so a job's
WorkerSignals can have its C++ half deleted mid-run. Every emit after that
raises, including the one in the except branch that exists to report the
first failure, which is why the traceback escaped QRunnable::run() entirely
and Qt printed it.

async_query.py hit exactly this and solved it: _QueryJob._emit swallows
RuntimeError, on the grounds that the answer was not wanted any more and "a
traceback nobody can act on trains people to ignore tracebacks". Job never
got the same treatment. This pins that it has it now.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtWidgets")

from shiboken6 import delete  # noqa: E402

from evasset.ui.workers import Job  # noqa: E402


class Reports(Job):
    """Emits progress, then finishes."""

    def run_job(self):
        self._progress("working", 50)
        return {"ok": True}


class Explodes(Job):
    def run_job(self):
        raise ValueError("the job itself failed")


# --------------------------------------------- the window went away mid-job
def test_progress_after_the_window_is_gone_does_not_raise(qapp_or_skip):
    job = Reports()
    delete(job.signals)  # what Qt's teardown does while the thread runs on
    job.run()


def test_a_failing_job_reports_nothing_rather_than_raising_again(qapp_or_skip):
    """The original crash: the except branch's own emit raised, so the
    RuntimeError escaped run() instead of the ValueError being reported."""
    job = Explodes()
    delete(job.signals)
    job.run()


def test_finishing_after_the_window_is_gone_does_not_raise(qapp_or_skip):
    """No progress calls at all -- the finished emit is its own path."""

    class Quiet(Job):
        def run_job(self):
            return 1

    job = Quiet()
    delete(job.signals)
    job.run()


# ------------------------------------------- and it still works when it can
def test_a_live_job_still_reports_progress_and_result(qapp_or_skip):
    """The guard must not swallow the signals anybody is actually waiting
    for -- a job that silently stops reporting is the worse bug."""
    job = Reports()
    seen: list[tuple[str, int]] = []
    done: list[object] = []
    job.signals.progress.connect(lambda m, p: seen.append((m, p)))
    job.signals.finished.connect(done.append)

    job.run()

    assert seen == [("working", 50)]
    assert done == [{"ok": True}]


def test_a_live_job_still_reports_its_failure(qapp_or_skip):
    """A real error must still reach the UI. Swallowing RuntimeError from a
    deleted signal source is one thing; hiding the job's own exception would
    be another."""
    job = Explodes()
    failures: list[str] = []
    job.signals.failed.connect(failures.append)

    job.run()

    assert failures == ["ValueError: the job itself failed"]
