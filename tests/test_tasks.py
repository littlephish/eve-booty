"""TaskManager scheduling: dedup, ordering and cancellation."""

from __future__ import annotations

import os
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtCore = pytest.importorskip("PySide6.QtCore")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from evasset.ui.tasks import QUEUED, RUNNING, TaskManager  # noqa: E402
from evasset.ui.workers import Job  # noqa: E402


class _Gated(Job):
    """Blocks until released, so a test can hold a task in RUNNING."""

    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def run_job(self):
        self.started.set()
        self.release.wait(5)
        return {"ok": True, "cancelled": self.cancelled}


@pytest.fixture
def app():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _spin(ms: int = 60) -> None:
    """Let queued signals land."""
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _wait_until(predicate, timeout_ms: int = 4000) -> bool:
    elapsed = 0
    while elapsed < timeout_ms:
        if predicate():
            return True
        _spin(20)
        elapsed += 20
    return predicate()


def test_same_kind_is_refused_while_one_is_running(app):
    tasks = TaskManager()
    first_job, second_job = _Gated(), _Gated()

    first = tasks.submit("prices", "Update prices", first_job)
    assert first is not None
    assert first_job.started.wait(3)

    second = tasks.submit("prices", "Update prices", second_job)
    assert second is None, "a second reprice should be refused, not queued"
    assert not second_job.started.is_set()

    first_job.release.set()
    assert _wait_until(lambda: not tasks.active())


def test_different_characters_run_side_by_side(app):
    tasks = TaskManager()
    a, b = _Gated(), _Gated()

    assert tasks.submit("sync:1", "Update A", a) is not None
    assert tasks.submit("sync:2", "Update B", b) is not None
    assert a.started.wait(3) and b.started.wait(3)
    assert len(tasks.active()) == 2

    a.release.set()
    b.release.set()
    assert _wait_until(lambda: not tasks.active())


def test_snapshot_waits_for_a_running_sync(app):
    """A snapshot over half-synced assets writes a wrong total into history,
    and history is never rewritten, so the bad point would be permanent."""
    tasks = TaskManager()
    sync_job, snap_job = _Gated(), _Gated()

    tasks.submit("sync:all", "Update all", sync_job)
    assert sync_job.started.wait(3)

    snapshot = tasks.submit("snapshot", "Snapshot", snap_job, after=("sync",))
    assert snapshot is not None
    _spin()
    assert snapshot.state == QUEUED
    assert not snap_job.started.is_set(), "snapshot must not start under a sync"

    sync_job.release.set()
    assert _wait_until(lambda: snap_job.started.is_set()), "snapshot should start once sync ends"
    assert snapshot.state == RUNNING

    snap_job.release.set()
    assert _wait_until(lambda: not tasks.active())


def test_cancelling_a_queued_task_drops_it_without_running_it(app):
    tasks = TaskManager()
    sync_job, snap_job = _Gated(), _Gated()

    tasks.submit("sync:all", "Update all", sync_job)
    assert sync_job.started.wait(3)
    snapshot = tasks.submit("snapshot", "Snapshot", snap_job, after=("sync",))

    tasks.cancel(snapshot.id)
    _spin()
    assert snapshot.id not in [t.id for t in tasks.active()]

    sync_job.release.set()
    assert _wait_until(lambda: not tasks.active())
    assert not snap_job.started.is_set(), "a cancelled queued task must never run"


def test_cancelling_a_running_task_asks_the_job_to_stop(app):
    tasks = TaskManager()
    job = _Gated()
    task = tasks.submit("sync:all", "Update all", job)
    assert job.started.wait(3)

    tasks.cancel(task.id)
    assert job.cancelled, "cancel() should reach the job"

    job.release.set()
    assert _wait_until(lambda: not tasks.active())


def test_cancel_kinds_stops_matching_syncs_only(app):
    tasks = TaskManager()
    doomed, spared = _Gated(), _Gated()
    tasks.submit("sync:42", "Update doomed", doomed)
    tasks.submit("prices", "Update prices", spared)
    assert doomed.started.wait(3) and spared.started.wait(3)

    stopped = tasks.cancel_kinds(("sync:42", "sync:all"))

    assert len(stopped) == 1
    assert doomed.cancelled
    assert not spared.cancelled

    doomed.release.set()
    spared.release.set()
    assert _wait_until(lambda: not tasks.active())
