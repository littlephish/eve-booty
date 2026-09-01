"""The rebuilt Assets tab, wired end to end: omnibox -> table -> rail -> strip.

The pieces are tested on their own (test_omni, test_omnibox, test_rail,
test_grouped_model); what is pinned here is the wiring between them, because
every one of these paths crosses at least two widgets and a broken signal
connection leaves controls that look alive but change nothing. Queries run
inline against the seeded connection (see the view fixture) so the whole
chip -> reload -> repopulate pipeline is real yet deterministic.
"""

from __future__ import annotations

import json

import pytest

from evasset import db, omni, queries

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QItemSelectionModel, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from evasset.ui.assets_view import AssetsView  # noqa: E402

DOMINIX, DAMAGE_CONTROL = 645, 2048
JITA_4_4, AMARR_STATION = 60003760, 60008494
JITA_SYSTEM, AMARR_SYSTEM = 30000142, 30002187
THE_FORGE, DOMAIN = 10000002, 10000043

LEVEL_KEYS = [key for _label, key in queries.ROLLUP_LEVELS]

# Column positions in queries.ASSET_COLUMNS, resolved by key so a column
# reorder cannot silently retarget the cell-action tests.
COLUMN = {key: i for i, (key, _header) in enumerate(queries.ASSET_COLUMNS)}


@pytest.fixture
def app():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def conn(tmp_path):
    """Four stacks over two owners, two stations, two groups, all priced --
    the smallest spread where the rail facet, the footer sums and the flip
    quantities each have two distinct answers to tell apart."""
    c = db.init(tmp_path / "ai.sqlite")
    c.executescript(
        f"""
        INSERT INTO sde_regions VALUES ({THE_FORGE},'The Forge'),({DOMAIN},'Domain');
        INSERT INTO sde_systems VALUES
            ({JITA_SYSTEM},'Jita',20000020,{THE_FORGE},0.9),
            ({AMARR_SYSTEM},'Amarr',20000322,{DOMAIN},1.0);
        INSERT INTO sde_stations VALUES
            ({JITA_4_4},'Jita IV - Moon 4',{JITA_SYSTEM},{THE_FORGE}),
            ({AMARR_STATION},'Amarr VIII',{AMARR_SYSTEM},{DOMAIN});
        INSERT INTO sde_categories VALUES (6,'Ship',1),(7,'Module',1);
        INSERT INTO sde_groups VALUES (27,6,'Battleship',1),(60,7,'Damage Control',1);
        INSERT INTO sde_types (type_id,name,group_id,volume,portion_size,base_price,published)
            VALUES ({DOMINIX},'Dominix',27,454500,1,153900000,1),
                   ({DAMAGE_CONTROL},'Damage Control II',60,5,1,500000,1);
        INSERT INTO characters(character_id,name,enabled) VALUES (1,'Main',1),(2,'Alt',1);
        INSERT INTO prices(type_id,buy_price,sell_price,source,samples,updated_at) VALUES
            ({DOMINIX},150000000,160000000,'jita',10,'2026-08-28T00:00:00+00:00'),
            ({DAMAGE_CONTROL},400000,500000,'jita',10,'2026-08-28T00:00:00+00:00');
        INSERT INTO assets(owner_type,owner_id,item_id,type_id,quantity,location_id,
                           location_flag,location_type,is_singleton,is_blueprint_copy,
                           root_location_id,system_id,region_id) VALUES
            ('character',1,1001,{DOMINIX},1,{JITA_4_4},'Hangar','station',1,0,
             {JITA_4_4},{JITA_SYSTEM},{THE_FORGE}),
            ('character',1,1002,{DAMAGE_CONTROL},3,{JITA_4_4},'Hangar','station',0,0,
             {JITA_4_4},{JITA_SYSTEM},{THE_FORGE}),
            ('character',2,1003,{DAMAGE_CONTROL},5,{AMARR_STATION},'Hangar','station',0,0,
             {AMARR_STATION},{AMARR_SYSTEM},{DOMAIN}),
            ('character',1,1004,{DAMAGE_CONTROL},2,{AMARR_STATION},'Hangar','station',0,0,
             {AMARR_STATION},{AMARR_SYSTEM},{DOMAIN});
        """
    )
    return c


@pytest.fixture
def view(app, conn):
    """An AssetsView whose queries run inline on the seeded connection.

    AsyncQuery hands work to a thread pool whose workers open their own
    connection to the *default* database, not the per-test one -- left
    alone, every reload() would race a worker against the assertions and
    query the wrong file to boot. Running the fetch synchronously keeps the
    whole chip -> reload -> repopulate pipeline real while making its
    results deterministic. The omnibox's completion lookups are silenced
    outright; nothing here types a token.
    """
    v = AssetsView(defer_load=True)
    # The view resolves its own GUI-thread connection through db.connect(),
    # which hands back the *default* database. Point it at this test's one
    # instead, so the writes it makes (pinning a price) land where the
    # assertions look.
    v.conn = conn

    def run_now(fn, on_done, on_failed=None):
        on_done(fn(conn))

    for query in (v._query, v._rail_query, v._strip_query):
        query.run = run_now
    v.omnibox._complete_query.run = lambda fn, on_done, on_failed=None: None
    v.first_load()
    yield v
    v.deleteLater()


def items(view: AssetsView) -> list[str]:
    return [r["item"] for r in view.model.rows()]


def set_group(view: AssetsView, key: str) -> None:
    view.group_combo.setCurrentIndex(view.group_combo.findData(key))


# ------------------------------------------------------------------ filtering
def test_adding_a_chip_narrows_the_table_and_counts_the_filter(view):
    """The omnibox's changed() drives reload(), and the state row is the only
    place the user can read how much of their estate a filter hides."""
    assert len(view.model.rows()) == 4  # the seed really loaded

    view.omnibox.add_chip("owner", "Alt")

    assert items(view) == ["Damage Control II"]
    assert view.state_label.text() == "1 filter · 1 of 4 stacks"
    assert not view.clear_all_btn.isHidden()

    view.clear_all_btn.click()

    assert len(view.model.rows()) == 4
    assert view.state_label.text() == "4 of 4 stacks"
    assert view.clear_all_btn.isHidden()


def test_a_rail_click_lands_as_a_chip_and_filters_the_rows(view):
    """chip_requested -> add_chip is the universal click contract; a broken
    connection would leave a rail that looks clickable but filters nothing."""
    view.rail.chip_requested.emit("owner", "Main")

    chips = view.omnibox.spec().chips
    assert chips == [omni.Chip("owner", "Main")]
    assert len(view.model.rows()) == 3
    assert all(r["owner"] == "Main" for r in view.model.rows())


def test_the_rail_facet_ignores_its_own_level_but_honours_others(view):
    """The rail must keep offering sibling locations while one is picked
    (its own level's chips are excluded) yet still respect every other chip
    -- otherwise it advertises labels that filter to an empty table."""
    view.omnibox.add_chip("owner", "Main")
    view.omnibox.add_chip("location", "Jita IV - Moon 4")

    assert view.rail._flip is None
    labels = {r["label"]: r for r in view.rail._rollups}
    assert set(labels) == {"Jita IV - Moon 4", "Amarr VIII"}
    # Owner chip honoured: Amarr's rollup counts Main's 2 units, not Alt's 5.
    assert labels["Amarr VIII"]["units"] == 2


def test_bare_text_flips_the_rail_to_quantities_and_clearing_restores(view):
    """Free text means "find my thing", so the rail answers where it is and
    how many -- and must fall back to rollups the moment the hunt ends."""
    view.omnibox.set_spec(omni.FilterSpec(text="Damage"))

    assert view.rail._flip is not None
    flip = [(r["label"], r["quantity"]) for r in view.rail._flip]
    assert flip == [("Amarr VIII", 7), ("Jita IV - Moon 4", 3)]

    view.omnibox.set_spec(omni.FilterSpec())

    assert view.rail._flip is None
    assert len(view.rail._rollups) == 2


def test_where_else_drops_location_chips_and_pins_the_exact_item(view):
    """The concept board's gesture: keep every non-location filter, pin the
    exact item as an item: chip -- bare text would LIKE-match substrings and
    inflate the answer, the defect the adversarial review caught -- and let
    the flipped rail answer per location."""
    view.omnibox.add_chip("location", "Jita IV - Moon 4")
    view.omnibox.add_chip("owner", "Main")
    row = next(r for r in view.model.rows() if r["item"] == "Dominix")

    view._where_else(row)

    spec = view.omnibox.spec()
    assert spec.text == ""
    assert spec.chips == [omni.Chip("owner", "Main"), omni.Chip("item", "Dominix")]
    assert view.rail.current_level() == "location"
    assert view.rail._flip is not None


def test_the_strip_badge_and_value_map_add_the_matching_chips(view):
    """set_data drives the badge's visibility, and both strip click targets
    must land in the omnibox like every other filter gesture."""
    view.strip.set_data(
        {
            "total": 5.0,
            "assets_sell": 4.0,
            "wallet_liquid": 1.0,
            "volume": 2.0,
            "unpriced_stacks": 3,
        },
        [],
    )
    assert not view.strip.unpriced_btn.isHidden()

    view.strip.unpriced_btn.click()
    assert omni.Chip("is", "unpriced") in view.omnibox.spec().chips

    view.strip.value_map.segment_clicked.emit("Amarr VIII")
    assert omni.Chip("location", "Amarr VIII") in view.omnibox.spec().chips

    # Zero unpriced stacks hides the badge -- its click would filter to an
    # empty table.
    view.strip.set_data(
        {
            "total": 5.0,
            "assets_sell": 4.0,
            "wallet_liquid": 1.0,
            "volume": 2.0,
            "unpriced_stacks": 0,
        },
        [],
    )
    assert view.strip.unpriced_btn.isHidden()


def test_f_and_x_on_the_current_cell_add_the_matching_chips(view):
    """The keyboard twins of the context menu's Filter/Exclude, keyed off the
    focused cell's column -- the wrong column map would mint chips that
    filter on the wrong level."""
    rows = view.model.rows()
    dc_position = next(i for i, r in enumerate(rows) if r["item"] == "Damage Control II")
    selection = view.tree.selectionModel()

    selection.setCurrentIndex(
        view.model.index(dc_position, COLUMN["grp"]), QItemSelectionModel.NoUpdate
    )
    view._filter_current_cell(negated=False)
    assert view.omnibox.spec().chips == [omni.Chip("group", "Damage Control")]

    # Captured before the exclusion applies -- the chip re-filters the rows,
    # so reading the owner afterwards would check against the wrong row.
    owner = view.model.rows()[0]["owner"]
    selection.setCurrentIndex(
        view.model.index(0, COLUMN["owner"]), QItemSelectionModel.NoUpdate
    )
    view._filter_current_cell(negated=True)
    assert omni.Chip("owner", owner, negated=True) in view.omnibox.spec().chips


def test_the_value_map_culls_thin_segments_into_one_residue_dynamically(view):
    """Performance guard: however many segments the query hands over, the
    map's paint/click/tooltip work is bounded by its width -- anything that
    would paint under the minimum pixel width folds into one muted residue,
    and the fold is recomputed per width so widening the strip reveals more
    segments while narrowing it culls more."""
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest

    from evasset.ui.strip import _MIN_SEGMENT_PX, _ValueMap

    vm = _ValueMap()
    vm.resize(300, 18)
    big = [{"label": f"Station {i}", "sell_value": v} for i, v in enumerate((100.0, 50.0, 30.0))]
    # 500 culled segments summing to a *visible* residue share, so the
    # residue itself is wide enough to hit with an integer-pixel click.
    tiny = [{"label": f"Dust {i}", "sell_value": 0.05} for i in range(500)]
    vm.set_segments(big + tiny)

    spans = vm._spans()
    assert len(spans) <= 300 / _MIN_SEGMENT_PX + 1, "work must stay width-bounded"
    assert [s[0] for s in spans[:3]] == ["Station 0", "Station 1", "Station 2"]
    assert spans[-1][0] is None, "the tail must be the residue"
    assert vm._cache[2] == 500, "every dust segment must be counted as culled"
    assert abs(spans[-1][1] - 25.0) < 1e-9, "residue carries the culled value"
    assert abs(spans[-1][3] - vm.width()) < 1e-6, "spans must fill the bar exactly"

    # The cache is per (segments, width): repeated reads reuse it, a resize
    # recomputes -- fifty 6-px segments all fit at 300 px and all fold at 150.
    assert vm._spans() is spans
    vm.set_segments([{"label": f"S{i}", "sell_value": 1.0} for i in range(50)])
    assert all(label is not None for label, *_ in vm._spans())
    vm.resize(150, 18)
    only = vm._spans()
    assert [label for label, *_ in only] == [None], "at 150 px every 3-px segment folds"

    # The residue is many locations at once, so clicking it must not invent
    # a single-location chip; a real segment still emits.
    vm.resize(300, 18)
    vm.set_segments(big + tiny)
    emitted: list[str] = []
    vm.segment_clicked.connect(emitted.append)
    first = vm._spans()[0]
    QTest.mouseClick(vm, Qt.LeftButton, pos=QPoint(int((first[2] + first[3]) / 2), 9))
    residue = vm._spans()[-1]
    QTest.mouseClick(vm, Qt.LeftButton, pos=QPoint(int((residue[2] + residue[3]) / 2), 9))
    assert emitted == ["Station 0"], "one emit for the segment, none for the residue"


# ------------------------------------------------------------------- grouping
def test_the_group_combo_regroups_and_none_flattens(view):
    """Group-by must translate the level key to the model's row key ("group"
    is spelled "grp" there); an untranslated key would bucket everything
    under one None header."""
    set_group(view, "group")

    assert view.model.rowCount() == 2  # Battleship, Damage Control
    counts = [view.model.rowCount(view.model.index(i, 0)) for i in range(2)]
    assert sorted(counts) == [1, 3]  # one Battleship, three Damage Control stacks
    header = view.model.index(0, 0)
    assert not (view.model.flags(header) & Qt.ItemIsSelectable)

    set_group(view, "")
    assert view.model.rowCount() == 4


def test_group_headers_span_the_full_row_instead_of_clipping_in_column_one(view):
    """Reported defect: a header's whole "label · stacks · m³ · ISK" line
    lives in column 0, so unspanned it truncated at the first column's edge.
    Spans are view state that every model reset drops, so they must also
    survive a reload."""
    from PySide6.QtCore import QModelIndex

    set_group(view, "location")
    root = QModelIndex()
    assert view.model.rowCount() > 0
    for i in range(view.model.rowCount()):
        assert view.tree.isFirstColumnSpanned(i, root), f"header row {i} not spanned"

    view.reload()  # model reset drops spans; _apply_rows must restore them
    for i in range(view.model.rowCount()):
        assert view.tree.isFirstColumnSpanned(i, root), f"row {i} lost its span on reload"

    set_group(view, "")
    assert not view.tree.isFirstColumnSpanned(0, root), (
        "flat leaves must not inherit spanning from the grouped state"
    )


# ------------------------------------------------------------------- selection
def test_the_footer_sums_the_selection_and_falls_back_to_the_filtered_set(view):
    """The footer's numbers are hand-checked against the seed: getting the
    fallback (whole filtered set) and the selected subset confused would
    misreport by orders of magnitude without looking wrong."""
    # 1 + 3 + 5 + 2 units; 454,500 + 15 + 25 + 10 m³; 160m + 1.5m + 2.5m + 1m.
    assert view.footer.text() == "4 stacks · 11 units · 454,550 m³ · 165.00m ISK (sell)"

    dominix = next(i for i, r in enumerate(view.model.rows()) if r["item"] == "Dominix")
    view.tree.selectionModel().select(
        view.model.index(dominix, 0),
        QItemSelectionModel.Select | QItemSelectionModel.Rows,
    )

    assert view.footer.text() == "1 selected · 1 units · 454,500 m³ · 160.00m ISK (sell)"


def test_copy_list_produces_aggregated_multibuy_lines(view):
    """Multibuy is "name<TAB>qty" per line; quantities of the same item sum
    so pasting never lists one module three times."""
    view.copy_list()

    assert QGuiApplication.clipboard().text() == "Dominix\t1\nDamage Control II\t10"


# ------------------------------------------------------------------ saved views
def test_saved_views_round_trip_filter_group_by_and_rail_level(view):
    """A saved view is the whole working posture -- filter, grouping, rail
    level -- and recalling one must restore all three, not just the chips."""
    view.omnibox.add_chip("owner", "Main")
    set_group(view, "group")
    view.rail.level.setCurrentIndex(LEVEL_KEYS.index("owner"))
    view._save_view(2)

    stored = view.conn.execute(
        "SELECT state_json FROM saved_views WHERE slot=2"
    ).fetchone()
    assert json.loads(stored["state_json"]) == {
        "filter": "owner:Main",
        "group_by": "group",
        "rail_level": "owner",
    }

    view.omnibox.clear()
    set_group(view, "")
    view.rail.level.setCurrentIndex(LEVEL_KEYS.index("location"))

    view._recall_view(2)

    assert view.omnibox.spec().chips == [omni.Chip("owner", "Main")]
    assert view._current_group_key() == "group"
    assert view.rail.current_level() == "owner"
    assert len(view.model.rows()) == 3  # the recalled filter really applied


def test_recalling_an_empty_slot_changes_nothing(view):
    view.omnibox.add_chip("owner", "Main")

    view._recall_view(7)

    assert view.omnibox.spec().chips == [omni.Chip("owner", "Main")]
    assert "No saved view" in view.footer.text()


# ------------------------------------------------------------------- keyboard
def test_escape_from_the_table_closes_the_inspector_then_pops_chips(view):
    """The escape ladder: the inspector is the cheapest state to shed, then
    chips newest-first -- skipping a rung would throw away more than the
    user meant to."""
    view.omnibox.add_chip("owner", "Main")
    view.tree.selectionModel().setCurrentIndex(
        view.model.index(0, 0), QItemSelectionModel.NoUpdate
    )
    view._open_inspector_current()
    assert view.side.currentWidget() is view.inspector

    view._escape_from_table()
    assert view.side.currentWidget() is view.rail
    assert len(view.omnibox.spec().chips) == 1  # the chip survived that press

    view._escape_from_table()
    assert view.omnibox.spec().chips == []


def test_the_inspector_renders_the_current_row(view):
    row = next(r for r in view.model.rows() if r["item"] == "Dominix")
    position = view.model.rows().index(row)
    view.tree.selectionModel().setCurrentIndex(
        view.model.index(position, 0), QItemSelectionModel.NoUpdate
    )

    view._open_inspector_current()

    assert view.inspector.title.text() == "Dominix"
    assert view.inspector.owner.text() == "Main"
    assert "Jita IV - Moon 4" in view.inspector.location.text()
    assert "jita" in view.inspector.price_line.text()


# ----------------------------------------------------------------------- pins
def test_toggling_a_pin_persists_and_reaches_the_rail(view):
    """pin_toggled must write pinned_labels and re-rank the rail; a pin that
    only flips the star would evaporate on the next refresh."""
    view.rail.pin_toggled.emit("location", "Amarr VIII")

    stored = view.conn.execute("SELECT level, label FROM pinned_labels").fetchall()
    assert [(r["level"], r["label"]) for r in stored] == [("location", "Amarr VIII")]
    assert view.rail._pinned == {"Amarr VIII"}

    view.rail.pin_toggled.emit("location", "Amarr VIII")

    assert view.conn.execute("SELECT COUNT(*) c FROM pinned_labels").fetchone()["c"] == 0
    assert view.rail._pinned == set()


def test_a_reload_preserves_which_groups_the_user_collapsed(view):
    """Performance-audit regression: expandAll re-ran on every debounced
    keystroke (110-280 ms a press at a few thousand rows) and re-opened
    every group the user had deliberately collapsed. A reload under an
    unchanged grouping must restore the arrangement instead."""
    from PySide6.QtCore import QModelIndex

    set_group(view, "location")
    root = QModelIndex()
    first = view.model.index(0, 0, root)
    kept_open = view.model.index(1, 0, root)
    assert view.tree.isExpanded(first) and view.tree.isExpanded(kept_open)

    view.tree.collapse(first)
    collapsed_label = first.data(Qt.UserRole + 1)  # GROUP_LABEL_ROLE
    view.reload()

    labels = {
        view.model.index(i, 0, root).data(Qt.UserRole + 1): view.tree.isExpanded(
            view.model.index(i, 0, root)
        )
        for i in range(view.model.rowCount())
    }
    assert labels[collapsed_label] is False, "the collapsed group must stay collapsed"
    assert any(state for state in labels.values()), "the open group must stay open"
