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


def _labels(menu):
    return [a.text().replace("&", "") for a in menu.actions() if not a.isSeparator()]


def _every_menu_label(window):
    """Every label reachable from the menu bar, submenus included."""
    found = []

    def walk(menu):
        for act in menu.actions():
            if act.isSeparator():
                continue
            found.append(act.text().replace("&", ""))
            if act.menu() is not None:
                walk(act.menu())

    for top in (window.file_menu, window.update_menu, window.view_menu, window.help_menu):
        walk(top)
    return found


def test_update_menu_lists_widest_scope_first(window):
    items = _labels(window.update_menu)
    assert items[:3] == ["All", "All characters", "Character"]
    assert items[3:] == ["Prices", "Game data", "Net worth snapshot"]


def test_refresh_actions_moved_off_file_and_data(window):
    file_items = _labels(window.file_menu)
    assert "All" not in file_items and "Sync all" not in file_items

    titles = _titles(window)
    assert "Data" not in titles, "Data menu should be folded into Update, not duplicated"
    assert "Update" in titles


def test_character_submenu_says_so_when_there_are_none(window):
    window._populate_character_menu()
    items = [a.text() for a in window.char_menu.actions()]
    assert items == ["No characters linked"]
    assert not window.char_menu.actions()[0].isEnabled()


def test_there_is_no_toolbar(window):
    """A row of buttons repeating the menu below it is the same command in
    two places to keep in step. It already went wrong once: renaming an
    action for the toolbar renamed it in the menu too."""
    assert window.findChildren(QtWidgets.QToolBar) == []


@pytest.mark.parametrize(
    "label",
    ["All", "Characters…", "Prices", "Net worth snapshot", "Game data", "Settings…"],
)
def test_every_command_is_reachable_from_a_menu(window, label):
    assert label in _every_menu_label(window)


def test_menu_items_carry_mnemonics(window):
    """Without a toolbar, the menus are the only route to these, so they need
    to be drivable from the keyboard."""
    for menu in (window.file_menu, window.update_menu):
        for act in menu.actions():
            if act.isSeparator():
                continue
            assert "&" in act.text(), f"{act.text()!r} has no mnemonic"


def test_starting_the_same_kind_twice_only_runs_once(window, monkeypatch):
    started = []
    monkeypatch.setattr(window.tasks, "_start", lambda task: started.append(task.kind))

    assert window._submit("prices", "Update prices", object()) is not None
    assert window._submit("prices", "Update prices", object()) is None
    assert started == ["prices"]
