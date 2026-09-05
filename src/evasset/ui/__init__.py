"""Qt widgets.

MainWindow is exported lazily, via PEP 562, and that is load-bearing rather
than tidy. Importing it pulls in every view, the chart and the treemap, which
costs around 450ms. Python initialises a parent package before any submodule,
so `from evasset.ui.workers import StartupInitJob` used to pay that 450ms in
full -- and that import happens in main() before QApplication exists, so it
was 450ms with nothing on screen at all.

The splash appeared afterwards, for a few frames, and was gone. It never
looked like it animated because it barely existed; the wait was in front of
it, not behind it.

Deferred, `evasset.ui.workers` costs only what workers itself needs, and the
expensive import happens on the pool thread inside StartupInitJob, behind a
splash that is already up and pumping events.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = ["MainWindow"]

if TYPE_CHECKING:  # pragma: no cover - for type checkers and editors only
    from .main_window import MainWindow


def __getattr__(name: str):
    if name == "MainWindow":
        from .main_window import MainWindow

        return MainWindow
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
