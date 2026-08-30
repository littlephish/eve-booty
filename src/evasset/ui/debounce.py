"""Wait for someone to stop typing before doing the expensive thing.

Every search box in the app is wired to a reload that runs a database query.
Connected to textChanged directly, that is one query per keystroke: typing
"tritanium" fires nine, of which eight are already obsolete before they
finish. AsyncQuery makes sure only the last one's *result* is used (it tags
each with a generation and drops the stale ones), but the queries still run,
which is wasted work on the pool and, for anything querying on the GUI
thread, wasted responsiveness.

A single-shot timer restarted on every keystroke collapses that to one query
once the typing pauses. The two concerns are separate and both needed:
debouncing stops the work being started, generations stop a slow early
result overwriting a fast later one.

220 ms is the interval used throughout. It is below the ~250 ms at which a
pause starts to feel like lag, and above a fast typist's inter-key gap, so a
word typed at speed produces exactly one query.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer

DEFAULT_INTERVAL_MS = 220


class Debounce(QObject):
    """Collapses a burst of signals into one call, once they stop.

    Parented to the widget that owns it, so it dies with the view rather than
    firing a reload into a half-torn-down window.
    """

    def __init__(self, parent: QObject, slot: Callable[[], None],
                 interval: int = DEFAULT_INTERVAL_MS):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(interval)
        self._timer.timeout.connect(slot)

    def trigger(self, *_ignored) -> None:
        """Restart the clock. Takes and discards whatever the signal sends --
        textChanged passes the new text, which the slot re-reads from the
        widget anyway."""
        self._timer.start()

    def flush(self) -> None:
        """Fire now if something is pending. For the cases where waiting is
        wrong: Enter pressed, focus lost, a dialog about to be accepted."""
        if self._timer.isActive():
            self._timer.stop()
            self._timer.timeout.emit()
