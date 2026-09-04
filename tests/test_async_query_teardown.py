"""A query that outlives the view that asked for it.

AsyncQuery runs on a pool thread and emits the answer back. Close the window
first and Qt has already destroyed the C++ side of those signals, so the emit
raises

    RuntimeError: Signal source has been deleted

on a thread with nothing to catch it. The answer was not wanted any more, so
losing it is correct -- but it printed a traceback over an otherwise clean
shutdown, and tracebacks nobody can act on are how people learn to ignore
tracebacks.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtWidgets")

from evasset.ui.async_query import _QueryJob  # noqa: E402


class _DeadSignal:
    """What Qt gives you once the owning QObject has been destroyed."""

    def emit(self, *args):
        raise RuntimeError("Signal source has been deleted")


class _LiveSignal:
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)


def test_a_result_for_a_closed_view_is_dropped_quietly():
    _QueryJob._emit(_DeadSignal(), 1, "result")  # must not raise


def test_a_live_receiver_still_gets_the_result():
    """The guard must not swallow delivery in the normal case."""
    signal = _LiveSignal()
    _QueryJob._emit(signal, 7, {"rows": 3})
    assert signal.emitted == [(7, {"rows": 3})]


def test_only_a_destroyed_receiver_is_tolerated():
    """A genuine bug in a callback should still surface, not be hidden by a
    guard meant for shutdown."""

    class Exploding:
        def emit(self, *args):
            raise ValueError("a real bug in the callback")

    with pytest.raises(ValueError):
        _QueryJob._emit(Exploding(), 1, None)
