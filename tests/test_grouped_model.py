"""The grouped tree model behind the Assets table.

The behaviours worth pinning are the ones a flat QAbstractTableModel never had
to get right: index/parent round-trips (Qt crashes, not errors, on a bad
parent), rollups that match what the rows actually sum to, sorting that stays
inside its group, and the heat/staleness roles the value-cell delegate reads.
Leaves must render exactly like RowTableModel so switching group-by to None
changes structure, never formatting.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from evasset import db, queries

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, Qt  # noqa: E402
from PySide6.QtGui import QBrush  # noqa: E402

from evasset.ui import grouped_model as gm  # noqa: E402
from evasset.ui.models import RowTableModel  # noqa: E402

DOMINIX, DAMAGE_CONTROL, TRITANIUM, PLEX = 645, 2048, 34, 44992
JITA_4_4, AMARR_STATION = 60003760, 60008494
JITA_SYSTEM, AMARR_SYSTEM = 30000142, 30002187
THE_FORGE, DOMAIN = 10000002, 10000043

DOMINIX_SELL = 150_000_000.0
DC_SELL = 500_000.0
PLEX_SELL = 4_000_000.0

# Rollups the seeded fixture must produce, computed by hand from the constants
# above. Jita: the Dominix stack plus one Damage Control stack of 3. Amarr: a
# Damage Control stack of 5, an unpriced Tritanium stack of 1000, and two
# manually priced PLEX.
JITA_SELL = DOMINIX_SELL + 3 * DC_SELL          # 151,500,000
AMARR_SELL = 5 * DC_SELL + 2 * PLEX_SELL        # 10,500,000
JITA_VOLUME = 50_000.0 + 3 * 5.0                # 50,015
AMARR_VOLUME = 5 * 5.0 + 1000 * 0.01 + 2 * 0.01  # 35.02


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def app():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def conn(tmp_path):
    """Five stacks across two stations, covering every price situation the
    model badges: a fresh Jita quote, a stale one, no quote at all, and a
    manual pin that is also old."""
    now = datetime.now(timezone.utc)
    fresh = iso(now - timedelta(hours=1))
    stale = iso(now - timedelta(days=6, hours=3))
    old_manual = iso(now - timedelta(days=10, hours=3))
    c = db.init(tmp_path / "grouped.sqlite")
    c.executescript(
        f"""
        INSERT INTO sde_regions VALUES ({THE_FORGE},'The Forge'),({DOMAIN},'Domain');
        INSERT INTO sde_systems VALUES
            ({JITA_SYSTEM},'Jita',20000020,{THE_FORGE},0.9),
            ({AMARR_SYSTEM},'Amarr',20000322,{DOMAIN},1.0);
        INSERT INTO sde_stations VALUES
            ({JITA_4_4},'Jita IV - Moon 4',{JITA_SYSTEM},{THE_FORGE}),
            ({AMARR_STATION},'Amarr VIII',{AMARR_SYSTEM},{DOMAIN});
        INSERT INTO sde_categories VALUES (6,'Ship',1),(7,'Module',1),(4,'Material',1);
        INSERT INTO sde_groups VALUES
            (27,6,'Battleship',1),(60,7,'Damage Control',1),(18,4,'Mineral',1);
        INSERT INTO sde_types (type_id,name,group_id,volume,portion_size,base_price,published)
            VALUES ({DOMINIX},'Dominix',27,50000,1,153900000,1),
                   ({DAMAGE_CONTROL},'Damage Control II',60,5,1,500000,1),
                   ({TRITANIUM},'Tritanium',18,0.01,100,2,1),
                   ({PLEX},'PLEX',18,0.01,1,0,1);
        INSERT INTO characters(character_id,name,enabled) VALUES (1,'Main',1),(2,'Alt',1);
        INSERT INTO assets(owner_type,owner_id,item_id,type_id,quantity,location_id,
                           location_flag,location_type,is_singleton,is_blueprint_copy,
                           root_location_id,system_id,region_id) VALUES
            ('character',1,1001,{DOMINIX},1,{JITA_4_4},'Hangar','station',1,0,
             {JITA_4_4},{JITA_SYSTEM},{THE_FORGE}),
            ('character',1,1002,{DAMAGE_CONTROL},3,{JITA_4_4},'Hangar','station',0,0,
             {JITA_4_4},{JITA_SYSTEM},{THE_FORGE}),
            ('character',2,1003,{DAMAGE_CONTROL},5,{AMARR_STATION},'Hangar','station',0,0,
             {AMARR_STATION},{AMARR_SYSTEM},{DOMAIN}),
            ('character',2,1004,{TRITANIUM},1000,{AMARR_STATION},'Hangar','station',0,0,
             {AMARR_STATION},{AMARR_SYSTEM},{DOMAIN}),
            ('character',2,1005,{PLEX},2,{AMARR_STATION},'Hangar','station',0,0,
             {AMARR_STATION},{AMARR_SYSTEM},{DOMAIN});
        INSERT INTO prices(type_id,buy_price,sell_price,source,samples,updated_at) VALUES
            ({DOMINIX},140000000,{DOMINIX_SELL},'jita',50,'{fresh}'),
            ({DAMAGE_CONTROL},400000,{DC_SELL},'jita',50,'{stale}'),
            ({PLEX},{PLEX_SELL},{PLEX_SELL},'manual',NULL,'{old_manual}');
        """
    )
    return c


def fetch_rows(conn):
    """Rows as the table fetches them, with price_updated_at guaranteed.

    ASSET_ROWS carries the column itself now, so the fallback join is
    ordinarily dead; it stays so these tests also run against a query shape
    that predates the column.
    """
    rows = queries.fetch_assets(conn)
    assert rows, "fixture seeded no assets"
    if "price_updated_at" in rows[0].keys():
        return rows
    sql = (
        f"SELECT r.*, p.updated_at AS price_updated_at "
        f"FROM ({queries.ASSET_ROWS}) r LEFT JOIN prices p ON p.type_id = r.type_id"
    )
    return list(conn.execute(sql))


@pytest.fixture
def rows(conn):
    return fetch_rows(conn)


@pytest.fixture
def model(app, rows):
    m = gm.GroupedAssetsModel()
    m.set_rows(rows, "location")
    return m


def leaf(model, group_row: int, child_row: int, column: int = 0):
    return model.index(child_row, column, model.index(group_row, 0))


def find_leaf(model, item_id: int):
    """The (group index, leaf index) of the stack with a given item_id."""
    for g in range(model.rowCount()):
        gidx = model.index(g, 0)
        for r in range(model.rowCount(gidx)):
            idx = model.index(r, 0, gidx)
            if model.row_for_index(idx)["item_id"] == item_id:
                return gidx, idx
    raise AssertionError(f"item_id {item_id} not in the model")


# ------------------------------------------------------------------ tree shape
def test_grouping_by_location_builds_value_ordered_headers(model):
    """Two stations, biggest sell rollup first -- Jita's 151.5m outranks
    Amarr's 10.5m however the rows happened to arrive."""
    assert model.rowCount() == 2
    labels = [model.index(g, 0).data(gm.GROUP_LABEL_ROLE) for g in range(2)]
    assert labels == ["Jita IV - Moon 4", "Amarr VIII"]
    assert model.rowCount(model.index(0, 0)) == 2
    assert model.rowCount(model.index(1, 0)) == 3


def test_every_leaf_round_trips_through_index_and_parent(model):
    """Qt segfaults rather than erroring on a parent() that disagrees with
    index(), so the round-trip is pinned for every single cell."""
    checked = 0
    for g in range(model.rowCount()):
        gidx = model.index(g, 0)
        assert gidx.parent() == QModelIndex()
        for r in range(model.rowCount(gidx)):
            for c in range(model.columnCount()):
                idx = model.index(r, c, gidx)
                assert idx.isValid()
                assert idx.parent() == gidx
                assert (idx.row(), idx.column()) == (r, c)
                checked += 1
    assert checked == 5 * 15  # the loops really covered every cell


def test_model_sanity_at_the_edges(model):
    """Out-of-range and invalid inputs must come back invalid or empty, never
    raise -- views probe models with garbage constantly."""
    assert not model.index(99, 0).isValid()
    assert not model.index(0, 99).isValid()
    assert model.parent(QModelIndex()) == QModelIndex()
    assert model.rowCount(leaf(model, 0, 0)) == 0
    # 15 columns everywhere: at the root, under a header, under a leaf.
    assert model.columnCount() == 15
    assert model.columnCount(model.index(0, 0)) == 15
    assert model.columnCount(leaf(model, 0, 0)) == 15
    assert model.data(QModelIndex()) is None
    assert model.row_for_index(QModelIndex()) is None


def test_flat_mode_puts_every_row_at_top_level(app, rows):
    model = gm.GroupedAssetsModel()
    model.set_rows(rows, None)
    assert model.rowCount() == len(rows) == 5
    for i, row in enumerate(rows):
        idx = model.index(i, 0)
        assert idx.parent() == QModelIndex()
        assert model.rowCount(idx) == 0
        assert model.row_for_index(idx) is row
        assert idx.data(gm.GROUP_LABEL_ROLE) is None
    assert model.rows() == list(rows)


# --------------------------------------------------------------------- rollups
def test_header_rollups_match_hand_computed_sums(model):
    """The header numbers are summed in-model from the fetched rows; a drifted
    rollup would silently misreport what the group underneath is worth."""
    jita = model.index(0, 0).data(gm.GROUP_ROLLUP_ROLE)
    amarr = model.index(1, 0).data(gm.GROUP_ROLLUP_ROLE)
    assert jita == {
        "stacks": 2, "units": 4, "volume": pytest.approx(JITA_VOLUME),
        "sell_value": pytest.approx(JITA_SELL),
    }
    assert amarr == {
        "stacks": 3, "units": 1007, "volume": pytest.approx(AMARR_VOLUME),
        "sell_value": pytest.approx(AMARR_SELL),
    }


def test_header_display_fills_column_zero_only(model):
    """Column 0 carries the whole rollup line; the other 14 stay empty so the
    header reads as one line, not a row of stray fragments."""
    gidx = model.index(0, 0)
    assert gidx.data() == "Jita IV - Moon 4 · 2 stacks · 50,015 m³ · 151.50m ISK"
    empties = [model.index(0, c).data() for c in range(1, model.columnCount())]
    assert len(empties) == 14 and set(empties) == {""}


def test_leaves_carry_no_group_roles_and_headers_no_row(model):
    idx = leaf(model, 0, 0)
    assert idx.data(gm.GROUP_LABEL_ROLE) is None
    assert idx.data(gm.GROUP_ROLLUP_ROLE) is None
    assert model.row_for_index(model.index(0, 0)) is None


# ------------------------------------------------------------- leaf formatting
def test_leaves_format_exactly_like_the_flat_table_model(app, rows):
    """RowTableModel is the reference renderer; a leaf that formats an ISK or
    date column differently would make the table change appearance when the
    user merely toggles grouping off."""
    reference = RowTableModel(queries.ASSET_COLUMNS, list(rows))
    model = gm.GroupedAssetsModel()
    model.set_rows(rows, None)
    compared = 0
    for r in range(len(rows)):
        for c in range(len(queries.ASSET_COLUMNS)):
            ours, theirs = model.index(r, c), reference.index(r, c)
            for role in (Qt.DisplayRole, Qt.TextAlignmentRole, Qt.UserRole):
                assert ours.data(role) == theirs.data(role), (
                    f"row {r} col {c} role {role} diverged"
                )
                compared += 1
    assert compared == 5 * 15 * 3


def test_display_order_and_rows_agree(model, rows):
    """rows() feeds CSV export and the selection math -- it must be exactly
    what the tree displays, headers excluded, top to bottom."""
    displayed = []
    for g in range(model.rowCount()):
        gidx = model.index(g, 0)
        for r in range(model.rowCount(gidx)):
            displayed.append(model.row_for_index(model.index(r, 0, gidx)))
    assert len(displayed) == 5
    assert model.rows() == displayed
    # And within each group the leaves kept the insertion order of `rows`.
    assert [r["item_id"] for r in displayed] == [1001, 1002, 1003, 1004, 1005]


# --------------------------------------------------------------------- sorting
def group_item_ids(model) -> list[set]:
    out = []
    for g in range(model.rowCount()):
        gidx = model.index(g, 0)
        out.append({
            model.row_for_index(model.index(r, 0, gidx))["item_id"]
            for r in range(model.rowCount(gidx))
        })
    return out


def test_sorting_reorders_inside_groups_without_leaking_across(model):
    """A row that jumped groups during a sort would be counted under the wrong
    rollup header while the header still showed the old sums."""
    before = group_item_ids(model)
    model.sort_by("quantity", Qt.DescendingOrder)
    assert group_item_ids(model) == before
    assert [r["item_id"] for r in model.rows()] == [1002, 1001, 1004, 1003, 1005]
    # Groups themselves kept the value-DESC order.
    assert model.index(0, 0).data(gm.GROUP_LABEL_ROLE) == "Jita IV - Moon 4"


def test_sorting_a_text_column_is_case_insensitive_alphabetical(model):
    model.sort_by("item", Qt.AscendingOrder)
    assert [r["item"] for r in model.rows()] == [
        "Damage Control II", "Dominix",              # Jita
        "Damage Control II", "PLEX", "Tritanium",    # Amarr
    ]


def test_reset_sort_restores_insertion_order(model):
    model.sort_by("quantity", Qt.DescendingOrder)
    model.reset_sort()
    assert [r["item_id"] for r in model.rows()] == [1001, 1002, 1003, 1004, 1005]


def test_a_persistent_index_follows_its_row_through_a_sort(model):
    """QTreeView remembers selection and the current cell as persistent
    indexes; a sort that resets instead of remapping would silently move the
    selection onto whatever row landed in the old position."""
    _gidx, idx = find_leaf(model, 1001)
    persistent = QPersistentModelIndex(idx)
    model.sort_by("quantity", Qt.DescendingOrder)
    assert persistent.isValid()
    moved = model.index(persistent.row(), persistent.column(), persistent.parent())
    assert model.row_for_index(moved)["item_id"] == 1001
    assert persistent.row() == 1  # the Dominix dropped below the DC stack of 3


# ------------------------------------------------------- heat and price badges
def sell_cell(model, item_id: int):
    gidx, idx = find_leaf(model, item_id)
    sell_col = [k for k, _ in queries.ASSET_COLUMNS].index("sell_value")
    return model.index(idx.row(), sell_col, gidx)


def test_heat_is_one_for_the_biggest_row_and_zero_for_unpriced(model):
    assert sell_cell(model, 1001).data(gm.HEAT_ROLE) == pytest.approx(1.0)
    assert sell_cell(model, 1004).data(gm.HEAT_ROLE) == 0.0


def test_heat_scales_logarithmically_between_the_extremes(model):
    """log10, not linear: the 1.5m Damage Control stack against the 150m
    Dominix must land around 0.76, not at the 0.01 a linear share would give,
    or every mid-value row renders stone cold."""
    import math

    expected = math.log10(3 * DC_SELL) / math.log10(DOMINIX_SELL)
    assert sell_cell(model, 1002).data(gm.HEAT_ROLE) == pytest.approx(expected)
    assert 0.0 < expected < 1.0


def test_heat_lives_only_on_the_value_columns(model):
    gidx, idx = find_leaf(model, 1001)
    qty_col = [k for k, _ in queries.ASSET_COLUMNS].index("quantity")
    assert model.index(idx.row(), qty_col, gidx).data(gm.HEAT_ROLE) is None
    assert model.index(idx.row(), qty_col, gidx).data(Qt.BackgroundRole) is None


def test_hot_value_cells_get_a_brush_and_cold_ones_none(model):
    hot = sell_cell(model, 1001).data(Qt.BackgroundRole)
    assert isinstance(hot, QBrush)
    assert sell_cell(model, 1004).data(Qt.BackgroundRole) is None


def test_price_age_is_none_when_fresh_and_whole_days_when_stale(model):
    """The 48 h grace period is the point: without it every row would carry an
    age badge the morning after any sync gap."""
    assert sell_cell(model, 1001).data(gm.PRICE_AGE_ROLE) is None
    assert sell_cell(model, 1002).data(gm.PRICE_AGE_ROLE) == 6
    assert sell_cell(model, 1004).data(gm.PRICE_AGE_ROLE) is None  # no quote at all


def test_price_badges_flag_unpriced_manual_and_stale(model):
    assert sell_cell(model, 1004).data(gm.PRICE_BADGE_ROLE) == "unpriced"
    assert sell_cell(model, 1002).data(gm.PRICE_BADGE_ROLE) == "6d"
    assert sell_cell(model, 1001).data(gm.PRICE_BADGE_ROLE) is None


def test_a_manual_price_badges_manual_even_when_old(model):
    """The repricer never overwrites a manual pin, so nagging about its age
    would be telling the user off for a number they chose on purpose."""
    assert sell_cell(model, 1005).data(gm.PRICE_BADGE_ROLE) == "manual"


def test_rows_without_the_price_age_column_degrade_to_no_badge(app, conn):
    """Rows fetched before price_updated_at joined ASSET_ROWS (or from any
    hand-written SELECT) may lack the column entirely; the model must treat
    that as fresh rather than raise on every paint."""
    plain = [
        {key: row[key] for key in row.keys() if key != "price_updated_at"}
        for row in fetch_rows(conn)
    ]
    assert plain and "price_updated_at" not in plain[0]
    model = gm.GroupedAssetsModel()
    model.set_rows(plain, "location")
    cell = sell_cell(model, 1002)
    assert cell.data(gm.PRICE_AGE_ROLE) is None
    assert cell.data(gm.PRICE_BADGE_ROLE) is None
