"""Collapsing a burst of keystrokes into one query.

Every search box reloads from the database. Wired straight to textChanged
that is one query per character typed; the Debounce helper waits for the
typing to stop first. AsyncQuery's generation counter already stopped a stale
*result* landing after a fresh one, but it could not stop the queries being
run in the first place, and one of these searches (the stockpile add-item
picker) runs on the GUI thread where that is felt directly.
"""

from __future__ import annotations

import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QObject  # noqa: E402

from evasset.ui.debounce import DEFAULT_INTERVAL_MS, Debounce  # noqa: E402


@pytest.fixture
def app():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def owner(app):
    """A parent that outlives the test. Debounce parents itself to the widget
    that owns it, so a throwaway QObject() with no reference is collected
    immediately and takes the timer down with it -- which is the intended
    lifetime behaviour, and would otherwise just look like a flaky test."""
    holder = QObject()
    yield holder
    holder.deleteLater()


def pump(until, timeout=3.0):
    """Spin the event loop until a condition holds or we give up. Timers need
    the loop to be pumped; a bare sleep would never fire them."""
    deadline = time.time() + timeout
    while not until() and time.time() < deadline:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.005)
    return until()


def test_a_burst_of_triggers_becomes_a_single_call(owner):
    calls = []
    d = Debounce(owner, lambda: calls.append(1), interval=30)

    for _ in range(10):          # ten keystrokes, faster than the interval
        d.trigger()
    assert calls == [], "fired while the user was still typing"

    assert pump(lambda: calls), "never fired after the typing stopped"
    assert calls == [1], "a burst produced more than one call"


def test_the_clock_restarts_on_every_trigger(owner):
    """Typing steadily must not fire mid-word just because the total time
    typing exceeded the interval."""
    calls = []
    d = Debounce(owner, lambda: calls.append(1), interval=60)

    for _ in range(6):           # 6 * ~20ms = 120ms of typing, interval 60ms
        d.trigger()
        end = time.time() + 0.02
        while time.time() < end:
            QtWidgets.QApplication.processEvents()
            time.sleep(0.002)
    assert calls == [], "fired mid-burst; the timer is not being restarted"

    assert pump(lambda: calls)
    assert calls == [1]


def test_the_signals_argument_is_accepted_and_ignored(owner):
    """textChanged passes the new text; the slot re-reads it from the widget,
    so trigger has to swallow whatever arrives."""
    calls = []
    d = Debounce(owner, lambda: calls.append(1), interval=10)
    d.trigger("some typed text")
    assert pump(lambda: calls)
    assert calls == [1]


def test_flush_fires_immediately_when_something_is_pending(owner):
    calls = []
    d = Debounce(owner, lambda: calls.append(1), interval=10_000)
    d.trigger()
    assert calls == []
    d.flush()
    assert calls == [1], "flush did not fire the pending call"


def test_flush_does_nothing_when_nothing_is_pending(owner):
    calls = []
    d = Debounce(owner, lambda: calls.append(1), interval=10_000)
    d.flush()
    assert calls == []


def test_nothing_fires_without_a_trigger(owner):
    calls = []
    Debounce(owner, lambda: calls.append(1), interval=10)
    pump(lambda: False, timeout=0.1)
    assert calls == []


def test_the_shared_interval_is_in_a_sane_range():
    """Below the point a pause reads as lag, above a fast typist's key gap."""
    assert 100 <= DEFAULT_INTERVAL_MS <= 300
