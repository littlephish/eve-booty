"""Optional debug logging to a file, off unless a user turns it on.

Off by default because the interesting lines are about somebody's account:
which characters synced, which structures answered, how many assets came back.
That belongs on their disk when they ask for it and nowhere otherwise.

On, it writes DATA_DIR/evebooty.log, rotating so a scheduled --sync cannot
fill a disk over a month. The file sits next to the database rather than in a
temp directory precisely so "send me your log" is a path a user can find.

What is worth logging is what a screenshot cannot show:

  every ESI request and its status   a 403 on one endpoint looks identical to
                                     "no data" once it reaches the table
  each sync step, per character      which of eight pulls was the one that
                                     failed, and how long it took
  the SDE import                     an interrupted import leaves the tables
                                     empty and the app merely looks broken
  every unhandled exception          a worker thread traceback otherwise goes
                                     to a console that a windowed build does
                                     not have

That last one is the reason this exists at all. With
--windows-console-mode=disable there is no stderr, so an exception raised off
the GUI thread is discarded and the user sees a control that did nothing.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .config import DATA_DIR

LOG_PATH = DATA_DIR / "evebooty.log"
LOGGER = logging.getLogger("evasset")

_MAX_BYTES = 2 * 1024 * 1024
_BACKUPS = 3
_configured = False


def configure(enabled: bool) -> Path | None:
    """Turn file logging on or off. Returns the log path when on.

    Safe to call repeatedly; toggling it in Settings should take effect
    without a restart, so the handler is torn down and rebuilt rather than
    stacked.
    """
    global _configured

    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        handler.close()

    if not enabled:
        LOGGER.addHandler(logging.NullHandler())
        LOGGER.setLevel(logging.CRITICAL)
        _configured = False
        return None

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.DEBUG)
    LOGGER.propagate = False
    _configured = True

    from . import __version__

    LOGGER.info("--- logging started, EVE Booty %s, python %s ---", __version__, sys.version)
    return LOG_PATH


def enabled() -> bool:
    return _configured


def install_excepthook() -> None:
    """Route otherwise-lost exceptions into the log.

    A windowed build has no stderr, so sys.excepthook writes into nothing and
    a crash in a worker leaves no trace at all. This does not swallow the
    exception -- the default hook still runs -- it only makes sure a copy
    lands somewhere a user can send on.
    """
    previous = sys.excepthook

    def hook(exc_type, exc, tb):
        LOGGER.critical("unhandled exception", exc_info=(exc_type, exc, tb))
        previous(exc_type, exc, tb)

    sys.excepthook = hook

    # Threads get their own hook; QThreadPool jobs run on threads, and this is
    # where a failure in one would otherwise vanish.
    import threading

    def thread_hook(args):
        LOGGER.critical(
            "unhandled exception in thread %s",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = thread_hook
