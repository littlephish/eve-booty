"""The Treemap tab, built against a real database and a real widget.

The layout maths is covered in test_treemap.py; what is checked here is the
wiring the maths cannot see -- that the view groups by what the combo says,
re-tiles without re-querying when only the presentation changes, paints
without raising, and refuses to offer a filter for the rolled-up "Other"
tile (which is not a group and would filter Assets to nothing).
"""

from __future__ import annotations

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QThreadPool  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402

from evasset import db, treemap  # noqa: E402
from evasset.ui.treemap_view import TreemapView, _Canvas  # noqa: E402

SEED = """
INSERT OR REPLACE INTO sde_categories VALUES (6,'Ship',1),(20,'Implant',1),(8,'Charge',1);
INSERT OR REPLACE INTO sde_groups VALUES (513,6,'Freighter',1),(300,20,'Cyber Learning',1),
  (83,8,'Projectile Ammo',1);
INSERT OR REPLACE INTO sde_types (type_id,name,group_id,volume,portion_size,published) VALUES
  (20185,'Charon',513,16250000,1,1),
  (9899,'Ocular Filter',300,1,1,1),
  (206,'EMP S',83,0.0125,1,1);
INSERT OR REPLACE INTO sde_regions VALUES (10000002,'The Forge'),(10000043,'Domain');
INSERT OR REPLACE INTO sde_systems VALUES (30000142,'Jita',20000020,10000002,0.9),
  (30002187,'Amarr',20000322,10000043,1.0);
INSERT OR REPLACE INTO sde_stations VALUES (60003760,'Jita IV - Moon 4',30000142,10000002),
  (60008494,'Amarr VIII',30002187,10000043);
INSERT OR REPLACE INTO characters (character_id,name,corporation_id,corporation_name,scopes,enabled)
  VALUES (100,'Test Pilot',2000,'Test Corp','x',1);
INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,location_flag,
                    location_type,is_singleton,root_location_id,system_id,region_id) VALUES
  ('character',100,1,20185,1,60003760,'Hangar','station',1,60003760,30000142,10000002),
  ('character',100,2,9899,4,60008494,'Hangar','station',0,60008494,30002187,10000043),
  ('character',100,3,206,1000,60003760,'Hangar','station',0,60003760,30000142,10000002);
-- Ammo is deliberately left unpriced: it must not become a tile.
INSERT OR REPLACE INTO prices VALUES (20185,1800000000,2000000000,'jita',1,'2026-08-04T00:00:00+00:00'),
                          (9899,   90000000, 100000000,'jita',1,'2026-08-04T00:00:00+00:00');
"""


@pytest.fixture
def app():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def view(app):
    """A view over the default database, seeded.

    It has to be the default database, not a tmp_path file: the view takes no
    connection, and AsyncQuery runs its query on a pool thread against
    db.connect(), which always opens the configured database. The seam for
    tests is EVASSET_DATA_DIR, which conftest points at a scratch directory.
    """
    conn = db.init()
    # The default database lives for the whole test session, so the seed is
    # written to be re-runnable rather than assuming an empty file.
    conn.executescript("DELETE FROM assets; DELETE FROM prices;")
    conn.executescript(SEED)
    conn.commit()
    widget = TreemapView(defer_load=True)
    widget.resize(900, 500)
    widget.show()  # so the layout gives the canvas a real size to tile into
    QtWidgets.QApplication.processEvents()
    yield widget
    widget.close()


def _settle(view):
    """Wait for the AsyncQuery pool job to land, then force a paint.

    The canvas lays out lazily inside paintEvent -- deliberately, so that
    resizing a hidden tab costs nothing -- which means _tiles stays empty
    until something actually draws. grab() is that something.
    """
    for _ in range(80):
        QThreadPool.globalInstance().waitForDone(25)
        QtWidgets.QApplication.processEvents()
        if view._rows:
            view.canvas.grab()
            return
    raise AssertionError("query never delivered")


def _labels(view):
    return [t.label for t in view.canvas._tiles]


def test_it_opens_on_item_group(view):
    """The tab exists to answer "implants, ammo or ships"; opening on the
    table's location-first default would bury that."""
    assert view.current_level() == "group"


def test_tiles_are_the_item_groups(view):
    view.first_load()
    _settle(view)
    assert set(_labels(view)) == {"Freighter", "Cyber Learning"}


def test_unpriced_groups_do_not_become_tiles(view):
    """Projectile Ammo is in the assets table but has no price row, so it has
    no area to occupy."""
    view.first_load()
    _settle(view)
    assert "Projectile Ammo" not in _labels(view)


def test_grouping_by_system_regroups_the_same_isk(view):
    view.first_load()
    _settle(view)
    by_group = sum(t.value for t in view.canvas._tiles)

    view.level.setCurrentIndex([k for _, k in view.LEVELS].index("system"))
    _settle(view)
    assert set(_labels(view)) == {"Jita", "Amarr"}
    assert sum(t.value for t in view.canvas._tiles) == pytest.approx(by_group)


def test_switching_basis_changes_the_values(view):
    view.first_load()
    _settle(view)
    sell = {t.label: t.value for t in view.canvas._tiles}

    view.basis.setCurrentIndex(1)  # Buy value
    _settle(view)
    buy = {t.label: t.value for t in view.canvas._tiles}
    assert buy["Freighter"] == pytest.approx(1_800_000_000)
    assert sell["Freighter"] == pytest.approx(2_000_000_000)


def test_changing_top_n_does_not_requery(view):
    """"Show top N" is presentation. Re-running the query for it would put a
    database round trip behind a combo box."""
    view.first_load()
    _settle(view)
    rows = view._rows
    view.top_n.setCurrentIndex(3)  # Everything
    assert view._rows is rows


def test_painting_does_not_raise(view):
    """A paint event that throws is invisible in normal use -- Qt prints to
    stderr and carries on with a half-drawn widget."""
    view.first_load()
    _settle(view)
    assert not view.canvas.grab().isNull()


def test_painting_an_empty_treemap_does_not_raise(app):
    """The empty state has its own paint path, reached on a fresh database."""
    canvas = _Canvas()
    canvas.resize(400, 300)
    canvas.set_items([], None)
    assert isinstance(canvas.grab(), QPixmap)


def test_right_click_offers_a_filter_that_matches_the_grouping(view):
    """The emitted (level, label) is what MainWindow hands to
    AssetsView.apply_external_filter, so it has to name the level currently
    grouped by -- not the view's default."""
    view.first_load()
    _settle(view)
    seen = []
    view.filter_assets_requested.connect(lambda level, value: seen.append((level, value)))

    tile = view.canvas._tiles[0]
    menu = view.menu_for_tile(tile)
    assert menu is not None
    (action,) = menu.actions()
    assert action.text() == f'Show "{tile.label}" in Assets'
    assert seen == [], "building the menu must not emit anything on its own"

    action.trigger()
    assert seen == [("group", tile.label)]


def test_the_other_tile_is_not_offered_as_a_filter(view):
    """"Other (12 more)" is a rollup, not a group -- filtering Assets to that
    string would return nothing at all."""
    view.first_load()
    _settle(view)
    other = treemap.Tile(label="Other (12 more)", value=1.0, x=0, y=0, width=10, height=10)
    assert view.menu_for_tile(other) is None


def test_hit_testing_finds_the_tile_under_a_point(view):
    view.first_load()
    _settle(view)
    assert view.canvas.tile_at(1, 1) is view.canvas._tiles[0]
    assert view.canvas.tile_at(-5, -5) is None


def test_a_failed_query_says_so_instead_of_painting_stale_tiles(view):
    view.first_load()
    _settle(view)
    assert view.canvas._tiles

    view._on_query_failed("no such table: assets")
    assert view.canvas._tiles == []
    assert "no such table" in view.canvas._empty_message
    assert not view.canvas.grab().isNull()
