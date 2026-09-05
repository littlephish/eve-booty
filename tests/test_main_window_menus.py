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
    settings.client_id = "test-client-id"
    # Off, or constructing a window asks CCP for the current SDE build and
    # then opens a modal dialog on a timer, which in a test is a network call
    # the suite must not make and a box with nobody to click it.
    settings.check_sde_on_startup = False
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
    assert items[3:] == ["Prices", "Game data", "Abyssal stats", "Net worth snapshot"]


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
    [
        "All", "Characters…", "Prices", "Net worth snapshot", "Game data",
        "Abyssal stats", "Settings…",
    ],
)
def test_every_command_is_reachable_from_a_menu(window, label):
    assert label in _every_menu_label(window)


def test_abyssal_stats_waits_for_any_sync_and_dedups_like_the_snapshot(window, started_tasks):
    """The fetch is keyed on item_ids the sync rewrites one owner at a time;
    started alongside a sync it would read a half-replaced assets table."""
    window._submit("sync:all", "Update all", object())
    window.act_abyssal.trigger()
    assert [t.kind for t in started_tasks] == ["sync:all"], "abyssal queues behind the sync"
    task = next(t for t in window.tasks.active() if t.kind == "abyssal")
    assert task.after == ("sync",)
    window.act_abyssal.trigger()
    assert [t.kind for t in window.tasks.active()].count("abyssal") == 1


def test_startup_skips_the_sde_refresh_on_a_database_with_no_sde(window):
    """A fresh install has no SDE and no dogma tables; refreshing tables it
    never had would be a surprise download before the user has seen the
    Update menu. Only an installed-but-older import triggers it."""
    assert not window.tasks.is_active("sde")


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


def test_about_reports_the_version(window, monkeypatch):
    """A user filing a bug has to be able to say which build they are on.
    About used to show the database path and the SDE build and no app version
    at all, while __version__ sat in __init__.py referenced by nothing.
    """
    from PySide6.QtWidgets import QMessageBox

    import evasset

    shown: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "about", staticmethod(lambda parent, title, text: shown.append(text))
    )
    window.about()

    assert len(shown) == 1
    body = shown[0]
    assert evasset.__version__ in body
    assert "github.com/littlephish/eve-booty" in body


def test_the_update_dialog_shows_what_changed(app):
    """The user is asked to approve an update *after* the build has already
    been downloaded, so the notes are the only evidence they have for the
    decision. A yes/no box naming two version numbers gave them none."""
    from PySide6.QtWidgets import QTextBrowser

    from evasset.ui.main_window import _UpdateDialog
    from evasset.updater import Release

    release = Release(
        version="v2.0.0", url="http://x/EVEBooty-2.0.0-win64.zip", current="1.0.0.0",
        notes="## What's Changed\n* A change the user should see first",
        page_url="https://github.com/littlephish/eve-booty/releases/tag/v2.0.0",
    )
    dialog = _UpdateDialog(release)
    browser = dialog.findChild(QTextBrowser)

    assert "A change the user should see first" in browser.toPlainText()
    # Rendered as Markdown, not dumped as source.
    assert "##" not in browser.toPlainText()
    # Display only: read-only, and links leave for a real browser.
    assert browser.isReadOnly()
    assert browser.openExternalLinks()
    dialog.deleteLater()


def test_the_update_dialog_handles_a_release_with_no_notes(app):
    """An empty panel reads as a failure to load; say so instead."""
    from PySide6.QtWidgets import QTextBrowser

    from evasset.ui.main_window import _UpdateDialog
    from evasset.updater import Release

    dialog = _UpdateDialog(Release(version="v2", url="u", current="1"))
    assert "No release notes" in dialog.findChild(QTextBrowser).toPlainText()
    dialog.deleteLater()


def test_about_credits_both_authors_with_working_links(window, monkeypatch):
    """Names verified against ESI /characters/{id}/, not typed from memory --
    a misspelled name beside a working link is worse than no credit."""
    from PySide6.QtWidgets import QMessageBox

    from evasset.ui.main_window import AUTHORS

    shown: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "about", staticmethod(lambda parent, title, text: shown.append(text))
    )
    window.about()

    body = shown[0]
    assert "Built by" in body
    for name, character_id in AUTHORS:
        assert name in body
        assert f"https://evewho.com/character/{character_id}" in body


def test_the_author_ids_are_plausible_character_ids():
    """A typo'd id links to somebody else's character, which is the failure
    nobody would notice."""
    from evasset.ui.main_window import AUTHORS

    assert len(AUTHORS) == 2
    for name, character_id in AUTHORS:
        assert name.strip() == name and name
        # EVE character ids are >= 90000000; anything smaller is a corp,
        # an alliance or a mistake.
        assert isinstance(character_id, int)
        assert character_id > 90_000_000
