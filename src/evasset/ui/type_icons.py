"""Type icons on a window's lines, fetched once per open and dropped in late.

The View fit window and the Compare deviation window both draw one line per
module, drone or cargo stack with the type's icon to the left of the text.
The icon comes from CCP's image service through evasset.icons, which is
Qt-free and disk-cached; what both windows need on top of it is the same
small piece of chrome: a fixed-size label holding a placeholder so the text
does not move when the pixmap lands, one batch fetch on a pool thread after
the lines are built, and a hand-out of the fetched pixmaps to every label
that asked for that type. TypeIconLoader is that piece, shared so the two
windows cannot drift apart in size, placeholder or lifetime rules.

The lifetime rules are the ones async_query.py's docstring documents: the
job's signals live on a QObject, and the loader holds a strong reference to
every job until it reports back. On top of that the loader keeps a
generation, since the comparison window rebuilds its grid on every reload
and is deleteLater()'d on close -- a fetch started for the previous grid
must not paint into labels that have since been deleted, so a result whose
job is no longer the current one is dropped unread.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QLabel

from .. import icons

MODULE_ICON_PX = 24
SHIP_ICON_PX = 32


class _IconSignals(QObject):
    # Signal(object), not Signal(dict): dict maps to QVariantMap, whose keys
    # must be strings -- an int-keyed {type_id: Path} fails the C++ conversion
    # at emit time (silently, from a worker thread) and the slot never runs.
    done = Signal(object)  # {type_id: Path}


class IconFetchJob(QRunnable):
    """One batch fetch per window open.

    The lifetime rules are the ones every QRunnable in this app follows
    (async_query.py's docstring has the long version): the signals live on
    a QObject, and the loader holds a strong reference to the job until it
    reports back.
    """

    def __init__(self, type_ids: list[int]):
        super().__init__()
        self.type_ids = type_ids
        self.signals = _IconSignals()
        self.setAutoDelete(False)

    @Slot()
    def run(self) -> None:
        try:
            paths = icons.fetch_icons(self.type_ids)
        except Exception:  # noqa: BLE001 - icons are decoration; never kill the window
            paths = {}
        try:
            self.signals.done.emit(paths)
        except RuntimeError:
            # "Signal source has been deleted": the window, and the loader
            # holding this job, were collected while the fetch ran. The
            # answer is not wanted any more; a traceback out of a pool
            # thread would only teach people to ignore tracebacks (the same
            # reasoning as _QueryJob._emit in async_query.py).
            pass


def placeholder(size: int) -> QPixmap:
    """A translucent grey square the size of an icon.

    It reserves the icon's space so text does not jump when the real pixmap
    lands, and reads as "loading" rather than as a broken image.
    """
    pm = QPixmap(size, size)
    pm.fill(QColor(127, 127, 127, 40))
    return pm


def blank_slot(size: int) -> QLabel:
    """An icon-sized label with nothing in it.

    For a line that has no type behind it -- the fit's own "[Empty Med
    slot]" placeholder -- so its text starts where every other line's text
    does.
    """
    label = QLabel()
    label.setFixedSize(size, size)
    return label


class TypeIconLoader:
    """The icon labels of one window and the fetch that fills them.

    Build the lines first, asking slot() for each icon label, then call
    start() once; the pixmaps land through the Qt event loop when the fetch
    reports. reset() forgets every label and disowns the running fetch, for
    a window that rebuilds its lines; cancel() is reset() for a window that
    is closing. Neither stops the fetch itself -- a download in progress on
    a pool thread cannot be interrupted, and the file it writes is cache
    the next open will use -- but its result is dropped instead of applied.
    """

    def __init__(self) -> None:
        # {type_id: [(icon label, display px), ...]} -- filled as the lines
        # are built, read when the fetch job reports back.
        self._labels: dict[int, list[tuple[QLabel, int]]] = {}
        self._job: IconFetchJob | None = None
        # Every job started and not yet reported, current or disowned; see
        # the module docstring for why a disowned job must still be held.
        self._inflight: set[IconFetchJob] = set()

    @property
    def type_ids(self) -> list[int]:
        return list(self._labels)

    def slot(self, type_id: int, size: int) -> QLabel:
        """A placeholder label the fetch will fill with this type's icon."""
        label = QLabel()
        label.setFixedSize(size, size)
        label.setPixmap(placeholder(size))
        self.register(label, type_id, size)
        return label

    def register(self, label: QLabel, type_id: int, size: int) -> None:
        """Adopt a label built elsewhere, such as a window's header icon."""
        self._labels.setdefault(type_id, []).append((label, size))

    def start(self) -> None:
        wanted = self.type_ids
        if not wanted:
            return
        job = IconFetchJob(wanted)
        self._job = job
        self._inflight.add(job)  # strong ref until the signal lands; see IconFetchJob
        job.signals.done.connect(lambda paths, j=job: self._on_done(j, paths))
        QThreadPool.globalInstance().start(job)

    def reset(self) -> None:
        self._labels = {}
        self._job = None

    def cancel(self) -> None:
        self.reset()

    def _on_done(self, job: IconFetchJob, paths: dict) -> None:
        self._inflight.discard(job)
        if job is not self._job:
            return  # superseded by a reload, or the window has closed
        self._job = None
        self.apply(paths)

    def apply(self, paths: dict) -> None:
        """Paint the fetched files into every label registered for their type.

        An id absent from paths -- a 404, or offline -- keeps its placeholder,
        and the next open retries it.
        """
        for type_id, labels in self._labels.items():
            path = paths.get(type_id)
            if path is None:
                continue
            pm = QPixmap(str(path))
            if pm.isNull():
                continue
            for label, px in labels:
                label.setPixmap(
                    pm.scaled(px, px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
