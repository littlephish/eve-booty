"""The Update menu, built against a real MainWindow."""

from __future__ import annotations

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from evasset.config import Settings  # noqa: E402
from evasset.ui.main_window import MainWindow  # noqa: E402


@pytest.fixture
def app():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(app):
    settings = Settings.load()
    # Non-empty so __init__ does not arm the first-run hint, which is a modal
    # box on a 400ms timer and would sit there waiting for a click.
    settings.client_id = "test-client-id"
    win = MainWindow(settings)
    yield win
    win.close()


def _titles(window):
    return [a.text().replace("&", "") for a in window.menuBar().actions()]


def test_update_menu_lists_widest_scope_first(window):
    items = [a.text() for a in window.update_menu.actions() if not a.isSeparator()]
    assert items[:3] == ["All", "All characters", "Character"]
    assert items[3:] == ["Prices", "Game data", "Net worth snapshot"]


def test_refresh_actions_moved_off_file_and_data(window):
    file_items = [a.text() for a in window.file_menu.actions()]
    assert "All" not in file_items and "Sync all" not in file_items

    titles = _titles(window)
    assert "Data" not in titles, "Data menu should be folded into Update, not duplicated"
    assert "Update" in titles


def test_character_submenu_says_so_when_there_are_none(window):
    window._populate_character_menu()
    items = [a.text() for a in window.char_menu.actions()]
    assert items == ["No characters linked"]
    assert not window.char_menu.actions()[0].isEnabled()


def test_toolbar_keeps_self_describing_labels(window):
    """"All" reads fine under a menu titled Update and means nothing on a
    bare toolbar."""
    bar = window.findChild(QtWidgets.QToolBar, "")
    labels = [a.text() for a in bar.actions()]
    assert "Update all" in labels
    assert "All" not in labels


def test_starting_the_same_kind_twice_only_runs_once(window, monkeypatch):
    started = []
    monkeypatch.setattr(window.tasks, "_start", lambda task: started.append(task.kind))

    assert window._submit("prices", "Update prices", object()) is not None
    assert window._submit("prices", "Update prices", object()) is None
    assert started == ["prices"]
