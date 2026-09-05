"""Background task registry.

Every long-running thing the user can start -- a sync, a reprice, an SDE
update, a net worth snapshot -- goes through here rather than being handed
straight to the thread pool. The registry exists for four reasons:

1. Lifetime. A QRunnable held only by the local that started it can be
   collected while the worker thread is still running, taking its signals
   object with it, which either drops the completion signal or crashes the
   process. The registry holds every task until it reports back. See
   async_query.py's docstring for the long version.

2. Concurrency. The old model was a single busy flag that disabled the whole
   toolbar, so one sync locked out everything else. Tasks now run alongside
   each other; only a task of a kind already in flight is refused.

3. Ordering. A few things genuinely must not overlap. A net worth snapshot
   taken while a sync is halfway through the characters records a total over
   half-fresh, half-stale assets and writes that number into history, where
   it stays. Those tasks declare what they have to follow and wait for it.

4. Cancellation. Removing a character while its sync is in flight used to
   race: the sync would write assets back after the delete, leaving rows
   owned by a character that no longer exists. Tasks can now be asked to
   stop, and callers can wait for that to happen.

Kinds are strings, and dedup/ordering both work on them. A whole-estate sync
is "sync:all", one character is "sync:<id>", so two different characters can
sync at once but the same one cannot be started twice, and "snapshot" can
declare that it follows anything starting with "sync".
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QThreadPool, Signal

from ..logsetup import LOGGER

QUEUED = "queued"
RUNNING = "running"


@dataclass
class Task:
    id: int
    kind: str
    label: str
    job: object
    state: str = QUEUED
    percent: int = 0
    message: str = ""
    done: Callable | None = None
    after: tuple[str, ...] = ()
    cancelling: bool = False
    _connected: bool = field(default=False, repr=False)

    @property
    def active(self) -> bool:
        return self.state in (QUEUED, RUNNING)


class TaskManager(QObject):
    """Owns every in-flight job. One of these per main window."""

    changed = Signal()                     # anything added, updated or removed
    finished = Signal(str, object)         # kind, result
    failed = Signal(str, str)              # label, message
    warned = Signal(str)                   # a soft warning from a job

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._tasks: dict[int, Task] = {}
        self._ids = itertools.count(1)
        self._pool = QThreadPool.globalInstance()

    # -------------------------------------------------------------- querying
    def active(self) -> list[Task]:
        return [t for t in self._tasks.values() if t.active]

    def is_active(self, kind: str) -> bool:
        return any(t.kind == kind and t.active for t in self._tasks.values())

    def any_active(self, prefixes: Iterable[str]) -> bool:
        return any(
            t.active and t.kind.startswith(tuple(prefixes)) for t in self._tasks.values()
        )

    def overall_percent(self) -> int:
        running = [t for t in self.active() if t.state == RUNNING]
        if not running:
            return 0
        return int(sum(t.percent for t in running) / len(running))

    # ------------------------------------------------------------ submitting
    def submit(
        self,
        kind: str,
        label: str,
        job,
        done: Callable | None = None,
        after: tuple[str, ...] = (),
    ) -> Task | None:
        """Register and (if nothing blocks it) start a job.

        Returns None if a task of this kind is already active -- clicking
        "Update prices" twice should not start two repricings.
        """
        if self.is_active(kind):
            LOGGER.info("task %r refused: already running", kind)
            return None

        task = Task(
            id=next(self._ids), kind=kind, label=label, job=job, done=done, after=tuple(after)
        )
        self._tasks[task.id] = task
        # The log recorded what happened (every ESI call) but never what asked
        # for it, so a sync and a game data update were indistinguishable
        # after the fact -- and an operation that made no ESI calls at all,
        # like the SDE, left no trace whatsoever.
        LOGGER.info("task %r started: %s", kind, label)
        self._pump()
        self.changed.emit()
        return task

    def _blocked(self, task: Task) -> bool:
        if not task.after:
            return False
        return any(
            other.active and other.id != task.id and other.kind.startswith(task.after)
            for other in self._tasks.values()
        )

    def _pump(self) -> None:
        """Start every queued task whose blockers have cleared."""
        for task in list(self._tasks.values()):
            if task.state != QUEUED or self._blocked(task):
                continue
            self._start(task)

    def _start(self, task: Task) -> None:
        task.state = RUNNING
        if not task._connected:
            job = task.job
            job.signals.progress.connect(
                lambda msg, pct, t=task: self._on_progress(t, msg, pct)
            )
            job.signals.warning.connect(self.warned.emit)
            job.signals.failed.connect(lambda msg, t=task: self._on_failed(t, msg))
            job.signals.finished.connect(lambda res, t=task: self._on_finished(t, res))
            task._connected = True
        self._pool.start(task.job)

    # ------------------------------------------------------------- callbacks
    def _on_progress(self, task: Task, message: str, percent: int) -> None:
        task.message = message
        task.percent = max(0, min(100, percent))
        self.changed.emit()

    def _on_failed(self, task: Task, message: str) -> None:
        LOGGER.error("task %r failed: %s", task.kind, message)
        self._retire(task)
        if not task.cancelling:
            self.failed.emit(task.label, message)

    def _on_finished(self, task: Task, result) -> None:
        LOGGER.info(
            "task %r finished%s", task.kind, " (cancelled)" if task.cancelling else ""
        )
        self._retire(task)
        if task.cancelling:
            return
        if task.done is not None:
            task.done(result)
        self.finished.emit(task.kind, result)

    def _retire(self, task: Task) -> None:
        self._tasks.pop(task.id, None)
        self._pump()          # a blocked task may now be free to run
        self.changed.emit()

    # ---------------------------------------------------------- cancellation
    def cancel(self, task_id: int) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.cancelling = True
        if task.state == QUEUED:      # never started, just drop it
            self._retire(task)
            return
        task.message = "Cancelling…"
        cancel = getattr(task.job, "cancel", None)
        if cancel is not None:
            cancel()
        self.changed.emit()

    def cancel_kinds(self, prefixes: Iterable[str]) -> list[Task]:
        """Cancel every active task whose kind starts with one of these.
        Returns the tasks that were asked to stop."""
        stopped = []
        for task in list(self._tasks.values()):
            if task.active and task.kind.startswith(tuple(prefixes)):
                self.cancel(task.id)
                stopped.append(task)
        return stopped
