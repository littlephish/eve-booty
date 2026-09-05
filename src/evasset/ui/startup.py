"""The one job that runs before there is a window, kept importable cheaply.

Separate from workers.py purely for import cost, and the difference is not
small. workers.py imports pricing, which imports esi.client, which imports
auth, which imports python-jose and keyring: 187ms, before QApplication
exists and therefore before anything can be on screen. Add httpx at 99ms and
the splash was appearing roughly half a second after launch, for a handful of
frames, then closing. It never looked like it animated because it barely
existed -- the wait was in front of it, not behind it.

Nothing here imports anything expensive. db is small, and the costly work
(the whole UI package, and by extension the ESI stack) is done inside run_job
on the pool thread, behind a splash that is already up and pumping events.

Duplicating the QRunnable plumbing rather than importing Job from workers is
the entire point: importing Job means importing workers means importing all
of it, which is the thing being avoided.
"""

from __future__ import annotations

import traceback

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from .. import db


class StartupSignals(QObject):
    failed = Signal(str)
    finished = Signal(object)


class StartupInitJob(QRunnable):
    """Prime everything slow, off the GUI thread, while the splash animates.

    db.init() -- the schema script, the migration check, and (just once, the
    first launch after an upgrade that adds one) building a brand new index
    over however many rows are already in the table -- used to run directly in
    main(), before the window was shown and before app.exec() had started
    pumping the event loop. That is not "slow", it is the process genuinely
    not processing window messages yet, which is what makes an OS report it as
    not responding rather than just idle.

    The UI import is here for the same reason. Importing a module that defines
    QWidget subclasses off the GUI thread is fine; constructing one is not,
    and construction stays in main()'s start_main_window.
    """

    def __init__(self):
        super().__init__()
        self.signals = StartupSignals()
        self.setAutoDelete(False)

    @Slot()
    def run(self) -> None:
        try:
            # The attribute access is the point. evasset.ui defers MainWindow
            # behind a module __getattr__, so a bare `import evasset.ui` would
            # load nothing and leave the cost on the GUI thread after all.
            from evasset.ui import MainWindow  # noqa: F401

            db.init()
        except Exception as exc:  # noqa: BLE001 - surfaced by the caller
            self.signals.failed.emit(f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
        else:
            self.signals.finished.emit(None)
