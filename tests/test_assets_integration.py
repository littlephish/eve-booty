"""The rebuilt Assets tab, wired end to end: omnibox -> table -> rail -> strip.

The pieces are tested on their own (test_omni, test_omnibox, test_rail,
test_grouped_model); what is pinned here is the wiring between them, because
every one of these paths crosses at least two widgets and a broken signal
connection leaves controls that look alive but change nothing. Queries run
inline against the seeded connection (see the view fixture) so the whole
chip -> reload -> repopulate pipeline is real yet deterministic.

The abyssal section at the end seeds the research notes' live Ballistic
Control System sample (docs/research/abyssal-stats.md section 1.4) and
drives the inspector, the badge, the `stat:` and `is:abyssal` chips, the
sync-time switch, the startup re-import and the per-item fetch button through
the same wiring, with every expected number computed by hand in the test.
"""

from __future__ import annotations

import csv
import json
import re

import pytest
from conftest import BCS_BODY, BCS_MUTATOR, BCS_SOURCE, BCS_TYPE, FakeESIClient, match_text

from evasset import abyssal, db, omni, queries, sde
from evasset.config import Settings

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QItemSelectionModel, Qt  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from evasset.ui import assets_view as assets_view_module  # noqa: E402
from evasset.ui import grouped_model as gm  # noqa: E402
from evasset.ui import palette, workers  # noqa: E402
from evasset.ui.abyssal_card import AbyssalCard  # noqa: E402
from evasset.ui.assets_view import AssetsView  # noqa: E402
from evasset.ui.inspector import InspectorWindow, roll_text  # noqa: E402
from evasset.ui.main_window import MainWindow  # noqa: E402

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
    yield from _wired_view(conn)


def _wired_view(conn):
    v = AssetsView(defer_load=True)
    # The view resolves its own GUI-thread connection through db.connect(),
    # which hands back the *default* database. Point it at this test's one
    # instead, so the writes it makes (pinning a price) land where the
    # assertions look.
    v.conn = conn

    def run_now(fn, on_done, on_failed=None):
        on_done(fn(conn))

    for query in (
        v._query,
        v._rail_query,
        v._strip_query,
        v._rolls_query,
        v._window_rolls_query,
        v._card_query,
        v._card_count_query,
    ):
        query.run = run_now
    v.omnibox._complete_query.run = lambda fn, on_done, on_failed=None: None
    v.first_load()
    yield v
    v.deleteLater()


def items(view: AssetsView) -> list[str]:
    return [r["item"] for r in view.model.rows()]


def set_group(view: AssetsView, key: str) -> None:
    view.group_combo.setCurrentIndex(view.group_combo.findData(key))


def panel_open(view: AssetsView) -> bool:
    return view.side.currentWidget() is view.inspector


def window_open(view: AssetsView) -> bool:
    """Whether the inspector window is up. A top-level show() makes
    isVisible() true even offscreen and even though the view itself is never
    shown, so this reads the same in the tests as it does on a desktop."""
    return view.inspector_window.isVisible()


def item_position(view: AssetsView, name: str) -> int:
    return next(i for i, r in enumerate(view.model.rows()) if r["item"] == name)


def select_item(view: AssetsView, name: str) -> None:
    view.tree.selectionModel().setCurrentIndex(
        view.model.index(item_position(view, name), 0), QItemSelectionModel.NoUpdate
    )


def inspect_in_window(view: AssetsView, position: int) -> None:
    """Right-click -> Inspect in window, through the menu the view builds."""
    menu = view._build_context_menu(view.model.index(position, COLUMN["item"]))
    action = menu.actions()[0]
    assert action.text() == "Inspect in window"
    action.trigger()


# ------------------------------------------------------------------ filtering
def test_adding_a_chip_narrows_the_table_and_counts_the_filter(view):
    """The omnibox's changed() drives reload(), and the state row is the only
    place the user can read how much of their estate a filter hides."""
    assert len(view.model.rows()) == 4  # the seed really loaded

    view.omnibox.add_chip("owner", "Alt")

    assert items(view) == ["Damage Control II"]
    # Filter count trails the stacks so it sits beside the Clear all pill.
    assert view.state_label.text() == "1 of 4 stacks · 1 filter"
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
    assert panel_open(view)

    view._escape_from_table()
    assert not panel_open(view)
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


def test_a_click_a_double_click_and_enter_all_open_the_panel(view):
    """Three routes, one panel. A single click follows the row the user is
    looking at; the double-click stays for anyone who learned it that way;
    Enter is the keyboard route. A group header opens nothing."""
    first, second = view.model.index(0, COLUMN["item"]), view.model.index(1, COLUMN["item"])
    assert not panel_open(view)

    view.tree.clicked.emit(first)
    assert panel_open(view)
    assert view._panel_host.row["item_id"] == view.model.row_for_index(first)["item_id"]

    view._close_inspector(view._panel_host)
    assert not panel_open(view)
    view.tree.doubleClicked.emit(second)
    assert panel_open(view)
    assert view._panel_host.row["item_id"] == view.model.row_for_index(second)["item_id"]

    view._close_inspector(view._panel_host)
    view.tree.selectionModel().setCurrentIndex(first, QItemSelectionModel.NoUpdate)
    view._open_inspector_current()
    assert panel_open(view)
    assert view._panel_host.row["item_id"] == view.model.row_for_index(first)["item_id"]
    assert not window_open(view), "none of the three touches the window"

    set_group(view, "location")
    header = view.model.index(0, 0)
    assert view.model.row_for_index(header) is None, "grouped mode puts a header first"
    view._close_inspector(view._panel_host)
    view.tree.clicked.emit(header)
    assert not panel_open(view)
    assert view._build_context_menu(header) is None


def test_inspect_in_window_opens_the_window_and_leaves_the_panel_alone(view):
    """The row menu's route is the other host. Right-clicking a row does not
    move the panel off whatever it shows, so a user comparing two items keeps
    both: the clicked one in the panel, the menu's one in the window."""
    view.tree.clicked.emit(view.model.index(item_position(view, "Damage Control II"), 0))
    assert panel_open(view)

    inspect_in_window(view, item_position(view, "Dominix"))

    assert window_open(view)
    assert view._window_host.row["item"] == "Dominix"
    assert view.inspector_window.inspector.title.text() == "Dominix"
    assert panel_open(view)
    assert view._panel_host.row["item"] == "Damage Control II"
    assert view.inspector.title.text() == "Damage Control II"
    assert view.inspector is not view.inspector_window.inspector


def test_opening_a_second_row_reuses_the_one_inspector_window(view):
    """A window per open would forget the size and place the user dragged
    the last one to, and two rapid opens would leave two windows up. One
    instance, parented to the tab so it dies with it, re-rendered in place."""
    inspect_in_window(view, item_position(view, "Dominix"))
    first = view.inspector_window
    inspect_in_window(view, item_position(view, "Dominix"))  # the rapid double open
    inspect_in_window(view, item_position(view, "Damage Control II"))

    assert view.inspector_window is first
    assert view.findChildren(InspectorWindow) == [first]
    assert first.parent() is view
    assert first.isWindow()
    assert window_open(view)
    assert first.inspector.title.text() == "Damage Control II"


def test_the_inspector_window_is_titled_after_the_item(view):
    """The title bar and the taskbar entry must say which item this is, the
    way the fit dialog's does, so a window left open behind the main one can
    be told apart from a stale one without raising it."""
    inspect_in_window(view, item_position(view, "Dominix"))
    assert view.inspector_window.windowTitle() == "Inspect — Dominix"

    inspect_in_window(view, item_position(view, "Damage Control II"))
    assert view.inspector_window.windowTitle() == "Inspect — Damage Control II"


def test_every_way_of_closing_the_window_runs_the_same_cleanup(view, monkeypatch):
    """Esc inside the window and the title bar's close both land in
    QDialog.reject, the × button in close_clicked; both must clear the
    window's row and cancel its rolls lookup, or a reload after a title-bar
    close would re-render a window nobody can see and a late rolls result
    would paint under a row that is gone."""
    cancels: list[bool] = []
    monkeypatch.setattr(view._window_rolls_query, "cancel", lambda: cancels.append(True))

    # Opening an ordinary row cancels too (the previous row's lookup must
    # not paint under it), so the count is read across the close alone.
    inspect_in_window(view, item_position(view, "Dominix"))
    cancels.clear()
    view.inspector_window.reject()
    assert not window_open(view)
    assert view._window_host.row is None
    assert cancels == [True]

    inspect_in_window(view, item_position(view, "Dominix"))
    assert window_open(view)
    cancels.clear()
    view.inspector_window.inspector.close_btn.click()
    assert not window_open(view)
    assert view._window_host.row is None
    assert cancels == [True]


def test_the_rail_stays_put_while_only_the_window_is_open(view):
    """The window's reason to exist: Where else? flips the rail to
    per-station quantities, which the panel covers up at exactly that
    moment. With the panel closed and the window up, the rail is what the
    splitter shows."""
    inspect_in_window(view, item_position(view, "Dominix"))

    assert window_open(view)
    assert not panel_open(view)
    assert view.side.currentWidget() is view.rail
    assert view.inspector_window.inspector.window() is view.inspector_window


def test_the_escape_ladder_closes_the_panel_but_not_the_window(view):
    """Esc from the omnibox (and the table) walks text, chips, then the
    panel. The window is a pinned item the user opened on purpose from a
    menu, and it closes only on its own Esc, × or title bar -- an escape
    mashed at the table must not take it down as collateral."""
    view.omnibox.add_chip("owner", "Main")
    inspect_in_window(view, item_position(view, "Dominix"))
    view.tree.clicked.emit(view.model.index(item_position(view, "Damage Control II"), 0))
    assert panel_open(view) and window_open(view)

    QTest.keyClick(view.omnibox.edit, Qt.Key_Escape)
    assert view.omnibox.spec().chips == []
    assert panel_open(view), "the chip went first"

    QTest.keyClick(view.omnibox.edit, Qt.Key_Escape)
    assert not panel_open(view)
    assert view._panel_host.row is None
    assert window_open(view)
    assert view._window_host.row["item"] == "Dominix"

    QTest.keyClick(view.omnibox.edit, Qt.Key_Escape)
    view._escape_from_table()
    assert window_open(view), "nothing left on the ladder reaches the window"


def test_a_reload_that_drops_one_hosts_row_leaves_the_other_open(view):
    """The keep-honest check runs per host. Filtering the window's item out
    of the table closes the window and nothing else; the panel's item is
    still on the table and stays rendered."""
    inspect_in_window(view, item_position(view, "Dominix"))
    view.tree.clicked.emit(view.model.index(item_position(view, "Damage Control II"), 0))

    view.omnibox.add_chip("group", "Damage Control")

    assert "Dominix" not in items(view)
    assert not window_open(view)
    assert view._window_host.row is None
    assert panel_open(view)
    assert view._panel_host.row["item"] == "Damage Control II"


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


def test_ctrl_f_opens_the_builder_from_inside_the_omnibox(view, app):
    """Reported defect: the omnibox and the view each bound Ctrl+F with a
    child-covering context, so with the focus in the line edit both matched
    and Qt fired neither -- the builder refused to open from the very field
    people type in. Exactly one binding may exist, and a real keypress from
    the edit must open the card."""
    from PySide6.QtGui import QKeySequence, QShortcut
    from PySide6.QtTest import QTest

    bound = [s for s in view.findChildren(QShortcut) if s.key() == QKeySequence("Ctrl+F")]
    assert len(bound) == 1, f"Ctrl+F is bound {len(bound)} times; two is an ambiguous no-op"

    view.show()
    app.processEvents()
    view.omnibox.edit.setFocus()
    app.processEvents()
    QTest.keyClick(view.omnibox.edit, Qt.Key_F, Qt.ControlModifier)
    app.processEvents()
    assert view.omnibox._draft is not None, "Ctrl+F from the omnibox must open the builder"


# -------------------------------------------------------------------- abyssal
# The research notes' live sample: an Abyssal Ballistic Control System made
# from a Domination BCS with a Gravid mutaplasmid (section 1.4). The webifier
# is a second, synthetic item so the seed carries a sign-inverted attribute
# (speedFactor, negative and better the more negative) and a millisecond
# duration for the display-unit filter. Its source and mutator ids are
# borrowed, not real: in the SDE 526 is "Stasis Webifier I" and 47737 a 5MN
# microwarpdrive mutaplasmid; only the webifier type id 47702 is genuine.
WEB_TYPE, WEB_SOURCE, WEB_MUTATOR = 47702, 526, 47737
BCS_OK, WEB_OK, BCS_UNFETCHED, WEB_MISSING, MUTAPLASMID_STACK = 2001, 2002, 2003, 2004, 2005
ABYSSAL_ROWS = {BCS_OK, WEB_OK, BCS_UNFETCHED, WEB_MISSING}
ORDINARY_ROWS = {1001, 1002, 1003, 1004, MUTAPLASMID_STACK}

# Of BCS_BODY's (conftest) 14 attributes only cpu (50), speedMultiplier (204) and
# missileDamageMultiplierBonus (213) are in the mutator's range table;
# droneDamageBonus (1255) is a synthetic range-table row -- no real BCS
# mutaplasmid lists it -- absent from the body, so the inspector must show
# exactly three rows and the pickers and columns must never offer 1255.

# What a fetch of the webifier would bring home: the same rolls the seed
# stores for WEB_OK, so a fetched WEB_MISSING renders identically.
WEB_BODY = {
    "created_by": 42,
    "dogma_attributes": [
        {"attribute_id": 20, "value": -63.0}, {"attribute_id": 50, "value": 27.0},
        {"attribute_id": 73, "value": 5000.0}, {"attribute_id": 30, "value": 1.0},
    ],
    "dogma_effects": [],
    "mutator_type_id": WEB_MUTATOR,
    "source_type_id": WEB_SOURCE,
}

# Hand computation, all from the seed below (base * min .. base * max, then
# (value - lo) / (hi - lo), mirrored when the attribute is low-is-good). The
# BCS source values and the Gravid mutaplasmid's multipliers are the real
# ones from SDE build 3480926 (read from the cached zip on 2026-09-01); the
# webifier's are synthetic.
#   BCS cpu       25.8 in 20.4..31.2        -> position 0.500, low-is-good  -> 50%
#   BCS RoF       0.8829 in 0.8722..0.9034  -> position 0.343, low-is-good  -> 66%
#   BCS missile   1.1077 in 1.1077..1.1357  -> position 0.001, high-is-good -> 0%
#   web speed     -63 in -66..-54           -> position 0.25, mutator says low-is-good -> 75%
#   web cpu       27 in 24..45              -> position 0.143, low-is-good  -> 86%
# Display units: 106 tf as stored; 111 shows (1 - v) * 100; 109 shows
# (v - 1) * 100 signed; 124 as stored, signed; format_value keeps two
# decimals under ten only.
BCS_ROLL_TEXTS = [
    "CPU usage: 26 tf · 50% of range · ▼ +1.80 tf vs 24 tf",
    "Missile Damage Bonus: +11% · 0% of range · ▼ -1.23% vs +12%",
    "Rate of Fire Bonus: 12% · 66% of range · ▲ +0.71% vs 11%",
]
BCS_QUALITIES = [0.5000, 0.0010, 0.6570]
BCS_SUMMARY = "CPU 50% · Missile dmg 0% · RoF 66%"
BCS_SOURCE_LINE = "Domination Ballistic Control System · Gravid mutaplasmid"
WEB_SOURCE_LINE = "Stasis Webifier II · Gravid mutaplasmid"
WEB_ROLL_TEXTS = [
    "CPU usage: 27 tf · 86% of range · ▲ -3.00 tf vs 30 tf",
    "Maximum Velocity Bonus: -63% · 75% of range · ▲ -3.00% vs -60%",
]
WEB_SUMMARY = "CPU 86% · Speed 75%"


def seed_abyssal(conn) -> None:
    conn.executescript(
        f"""
        INSERT INTO sde_groups VALUES (65,7,'Stasis Web',1),
            (367,7,'Ballistic Control system',1),(1964,7,'Mutaplasmids',1);
        INSERT INTO sde_meta_groups VALUES (2,'Tech II'),(4,'Faction'),(15,'Abyssal');
        INSERT INTO sde_types (type_id,name,group_id,meta_group_id,volume,portion_size,
                               published,is_dynamic_type) VALUES
            ({BCS_TYPE},'Abyssal Ballistic Control System',367,15,5,1,1,1),
            ({BCS_SOURCE},'Domination Ballistic Control System',367,4,5,1,1,0),
            ({BCS_MUTATOR},'Gravid Ballistic Control System Mutaplasmid',1964,15,1,1,1,0),
            ({WEB_TYPE},'Abyssal Stasis Webifier',65,15,5,1,1,1),
            ({WEB_SOURCE},'Stasis Webifier II',65,2,5,1,1,0),
            ({WEB_MUTATOR},'Gravid Stasis Webifier Mutaplasmid',1964,15,1,1,1,0);
        INSERT INTO sde_dogma_attributes
            (attribute_id,name,display_name,unit_id,high_is_good,default_value,published) VALUES
            (20,'speedFactor','Maximum Velocity Bonus',124,1,0,1),
            (30,'power','Powergrid Usage',107,0,0,1),
            (50,'cpu','CPU usage',106,0,0,1),
            (73,'duration','Activation time / duration',101,0,0,1),
            (204,'speedMultiplier','Rate of Fire Bonus',111,0,1,1),
            (213,'missileDamageMultiplierBonus','Missile Damage Bonus',109,1,1,1),
            (1255,'droneDamageBonus','Drone Damage Bonus',105,1,0,1);
        INSERT INTO sde_dogma_units VALUES (101,'Milliseconds','s'),(105,'Percentage','%'),
            (111,'Inverse Absolute Percent','%'),
            (106,'Teraflops','tf'),(107,'MegaWatts','MW'),(109,'Modifier Percent','%'),
            (124,'Modifier Relative Percent','%');
        INSERT INTO sde_type_dogma VALUES
            ({BCS_SOURCE},50,24),({BCS_SOURCE},30,1),({BCS_SOURCE},204,0.89),({BCS_SOURCE},213,1.12),
            ({WEB_SOURCE},20,-60),({WEB_SOURCE},50,30),({WEB_SOURCE},73,5000),({WEB_SOURCE},30,1);
        -- The webifier mutaplasmid carries CCP's per-mutator polarity override
        -- on speedFactor (high_is_good 0); everything else defers to the attribute.
        INSERT INTO sde_mutator_ranges VALUES
            ({BCS_MUTATOR},50,0.85,1.3,NULL,{BCS_TYPE}),
            ({BCS_MUTATOR},204,0.98,1.015,NULL,{BCS_TYPE}),
            ({BCS_MUTATOR},213,0.989,1.014,NULL,{BCS_TYPE}),
            ({BCS_MUTATOR},1255,0.9,1.1,NULL,{BCS_TYPE}),
            ({WEB_MUTATOR},20,0.9,1.1,0,{WEB_TYPE}),
            ({WEB_MUTATOR},50,0.8,1.5,NULL,{WEB_TYPE});
        -- Mutaplasmids trade on the market, so the stack is priced: it shares
        -- meta group 15 with the abyssals yet must count as neither abyssal
        -- nor unpriced.
        INSERT INTO prices(type_id,buy_price,sell_price,source,samples,updated_at) VALUES
            ({BCS_MUTATOR},20000000,25000000,'jita',10,'2026-08-28T00:00:00+00:00');
        INSERT INTO assets(owner_type,owner_id,item_id,type_id,quantity,location_id,
                           location_flag,location_type,is_singleton,is_blueprint_copy,
                           root_location_id,system_id,region_id) VALUES
            ('character',1,{BCS_OK},{BCS_TYPE},1,{JITA_4_4},'Hangar','station',1,0,
             {JITA_4_4},{JITA_SYSTEM},{THE_FORGE}),
            ('character',1,{WEB_OK},{WEB_TYPE},1,{JITA_4_4},'Hangar','station',1,0,
             {JITA_4_4},{JITA_SYSTEM},{THE_FORGE}),
            ('character',2,{BCS_UNFETCHED},{BCS_TYPE},1,{AMARR_STATION},'Hangar','station',1,0,
             {AMARR_STATION},{AMARR_SYSTEM},{DOMAIN}),
            ('character',1,{WEB_MISSING},{WEB_TYPE},1,{AMARR_STATION},'Hangar','station',1,0,
             {AMARR_STATION},{AMARR_SYSTEM},{DOMAIN}),
            ('character',1,{MUTAPLASMID_STACK},{BCS_MUTATOR},3,{JITA_4_4},'Hangar','station',0,0,
             {JITA_4_4},{JITA_SYSTEM},{THE_FORGE});
        INSERT INTO abyssal_items VALUES
            ({WEB_OK},{WEB_TYPE},{WEB_SOURCE},{WEB_MUTATOR},42,'ok','2026-09-01T00:00:00+00:00'),
            ({WEB_MISSING},{WEB_TYPE},NULL,NULL,NULL,'missing','2026-09-01T00:00:00+00:00');
        INSERT INTO abyssal_attributes VALUES
            ({WEB_OK},20,-63),({WEB_OK},50,27),({WEB_OK},73,5000),({WEB_OK},30,1);
        """
    )
    # The BCS goes in through the real store path, body verbatim, so the
    # inspector below reads what a fetch would have written.
    abyssal.store_rolls(conn, BCS_OK, BCS_TYPE, BCS_BODY)


@pytest.fixture
def abyssal_conn(conn):
    seed_abyssal(conn)
    return conn


@pytest.fixture
def abyssal_view(app, abyssal_conn):
    yield from _wired_view(abyssal_conn)


def item_ids(view: AssetsView) -> set[int]:
    return {r["item_id"] for r in view.model.rows()}


def row_position(view: AssetsView, item_id: int) -> int:
    return next(i for i, r in enumerate(view.model.rows()) if r["item_id"] == item_id)


def open_inspector(view: AssetsView, item_id: int) -> None:
    view.tree.selectionModel().setCurrentIndex(
        view.model.index(row_position(view, item_id), 0), QItemSelectionModel.NoUpdate
    )
    view._open_inspector_current()


def sell_cell(view: AssetsView, item_id: int):
    return view.model.index(row_position(view, item_id), COLUMN["sell_value"])


def plain(label) -> str:
    """A rich-text label's words without its colour spans."""
    return re.sub(r"<[^>]+>", "", label.text())


def roll_lines(inspector) -> list[str]:
    """The rendered rows' payloads as roll_text one-liners.

    The fetch-and-rerender tests pin the read path -- which rolls landed,
    with which numbers -- in one string per row; how a row draws those
    numbers is the render tests' business, so this reads the payload each
    row was built from rather than reassembling its labels."""
    return [roll_text(row.roll) for row in inspector.roll_rows]


def meter_geometry(row) -> tuple[float | None, float | None, float | None]:
    return (row.meter.fill_from, row.meter.fill_to, row.meter.base_pos)


def range_texts(row) -> tuple[str, str, str]:
    return tuple(label.text() for label in row.range_labels)


def test_the_inspector_lists_the_live_sample_rolls_as_hand_computed(abyssal_view):
    """The whole read path -- store_rolls, the roll join, the unit CASE, the
    polarity rules, roll_text -- lands on the numbers a person gets with a
    calculator from the research sample. Three rows, not fourteen: the
    mutator's attribute set decides what was rolled, and the drone bonus in
    that set is absent from a BCS so it must not appear as a phantom row."""
    open_inspector(abyssal_view, BCS_OK)
    inspector = abyssal_view.inspector

    assert not inspector.rolls_box.isHidden()
    assert inspector.rolls_header.text() == "ROLLED STATS"
    assert roll_lines(inspector) == BCS_ROLL_TEXTS
    assert [row.label.text() for row in inspector.roll_rows] == [
        "CPU usage", "Missile Damage Bonus", "Rate of Fire Bonus",
    ]
    assert plain(inspector.rolls_note) == BCS_SOURCE_LINE
    assert palette.status_hex(palette.WARN) in inspector.rolls_note.text(), "the tier is flagged"
    assert inspector.fetch_abyssal_btn.isHidden(), "nothing to fetch for a stored item"
    # The verdict colour follows the polarity, not the sign of the delta: the
    # CPU roll went up and that is worse; the rate-of-fire multiplier went
    # down, which the display shows as a bigger bonus, and that is better.
    # The delta itself is unit-less and signed with a true minus.
    cpu, missile, rof = inspector.roll_rows
    assert plain(cpu.value) == "26 tf +1.80"
    assert plain(missile.value) == "+11% −1.23"
    assert plain(rof.value) == "12% +0.71"
    assert palette.delta_hex(False) in cpu.value.text()
    assert palette.delta_hex(False) in missile.value.text()
    assert palette.delta_hex(True) in rof.value.text()


def test_the_roll_meters_run_worst_to_best_whichever_way_the_number_runs(abyssal_view):
    """Hand geometry for the BCS. CPU is low-is-good, so the meter's left end
    is the 31.2 tf worst case and the right the 20.4 tf best: base 24 sits at
    (31.2 - 24) / (31.2 - 20.4) = 0.667 and the 25.8 roll at 0.5, so the fill
    runs leftwards from the tick and is coloured worse. Rate of fire is the
    unit-111 case: the stored multiplier is low-is-good but the displayed
    bonus percentage is high-is-good, so the meter must NOT mirror it --
    the 9.67% worst end goes left, base 11% ticks at 0.429 and the 11.71%
    roll fills rightwards to 0.657, which is the quality figure. Read the
    labels under the meters as the user would."""
    open_inspector(abyssal_view, BCS_OK)
    cpu, missile, rof = abyssal_view.inspector.roll_rows

    assert meter_geometry(cpu) == pytest.approx((0.6667, 0.5, 0.6667), abs=5e-4)
    assert range_texts(cpu) == ("31 tf", "base 24 tf", "20 tf")
    assert cpu.meter.better is False
    # 1.12 * 0.989 .. 1.12 * 1.014 -> +10.77% .. +13.57%, base +12% at 0.44.
    assert meter_geometry(missile) == pytest.approx((0.44, 0.0007, 0.44), abs=5e-4)
    assert range_texts(missile) == ("+11%", "base +12%", "+14%")
    assert meter_geometry(rof) == pytest.approx((0.4286, 0.6565, 0.4286), abs=5e-4)
    assert range_texts(rof) == ("9.67%", "base 11%", "13%")
    assert rof.meter.better is True
    assert [row.meter.fill_to for row in (cpu, missile, rof)] == pytest.approx(
        BCS_QUALITIES, abs=5e-4
    ), "the fill's end is the roll's quality, on every orientation"


def test_a_low_is_good_roll_shows_its_mirrored_quality(abyssal_view):
    """A webifier's speedFactor is -63 against a -60 base: a bigger number
    would be a worse web. The mutator's polarity override puts the -54 worst
    case on the left and -66 on the right, ticks the base at the middle and
    fills to 0.75 in the better colour; the tooltip explains the mirror --
    position 25%, quality 75% -- because a user comparing the two figures
    would otherwise think one of them was wrong. The range is the
    mutaplasmid's 0.9x..1.1x of -60 in display terms, worst end first."""
    open_inspector(abyssal_view, WEB_OK)
    inspector = abyssal_view.inspector

    assert roll_lines(inspector) == WEB_ROLL_TEXTS
    assert plain(inspector.rolls_note) == WEB_SOURCE_LINE
    cpu, speed = inspector.roll_rows
    assert plain(speed.value) == "-63% −3.00"
    assert palette.delta_hex(True) in speed.value.text()
    assert meter_geometry(speed) == pytest.approx((0.5, 0.75, 0.5))
    assert range_texts(speed) == ("-54%", "base -60%", "-66%")
    # 27 tf in 24..45 with a 30 tf base: another low-is-good row, big end left.
    assert meter_geometry(cpu) == pytest.approx((0.7143, 0.8571, 0.7143), abs=5e-4)
    assert range_texts(cpu) == ("45 tf", "base 30 tf", "24 tf")
    assert speed.toolTip().splitlines() == [
        "Rolled 25% of the way from the range's low end to its high end; "
        "lower is better here, so the roll quality is 75%",
        "Source module: -60%",
        "Mutator: Gravid Stasis Webifier Mutaplasmid",
    ]


def test_the_roll_rows_paint_offscreen_at_a_wide_and_a_narrow_width(abyssal_view):
    """The meter is custom-painted, so an exception in paintEvent would show
    up as a blank row and a console trace rather than a failed test unless
    something forces a paint. grab() does; 300 px is the panel at rest and
    120 px is below the 14 px segment pitch times ten, where the last
    segment is clipped short and the tick clamps inside the widget."""
    open_inspector(abyssal_view, BCS_OK)
    inspector = abyssal_view.inspector
    assert inspector.roll_rows, "nothing to paint"

    for width in (300, 120):
        for row in inspector.roll_rows:
            # Pinned on the meter itself: the panel's layout would otherwise
            # hand it back the splitter's width before grab() paints.
            row.meter.setFixedWidth(width)
            QGuiApplication.processEvents()
            image = row.meter.grab()
            assert not image.isNull()
            assert image.width() == width
            assert not row.grab().isNull()


def test_an_unrankable_roll_keeps_its_figures_and_says_the_range_is_unknown(abyssal_view):
    """An attribute the mutator's range table cannot place (a zero base, an
    unknown multiplier) still has a value and a delta worth showing; the
    meter draws its bare track and the labels under it say why there is no
    fill rather than showing an empty pair of numbers. An equal roll reads
    "±0.00" in the muted colour whichever way the verdict would have gone."""
    inspector = abyssal_view.inspector
    inspector.show_rolls({
        "status": "ok", "source": "Stasis Webifier II", "mutator": None,
        "rolls": [
            {"label": "Optimal Range", "unit": "m", "unit_id": None, "value": 12_500.0,
             "base": 10_000.0, "min": None, "max": None, "position": None, "quality": None,
             "high_is_good": True, "better": True},
            {"label": "Activation Cost", "unit": "GJ", "unit_id": None, "value": 5.0,
             "base": 5.0, "min": 4.0, "max": 6.0, "position": 0.5, "quality": 0.5,
             "high_is_good": False, "better": None},
        ],
    })

    unranked, equal = inspector.roll_rows
    assert plain(unranked.value) == "12,500 m +2,500"
    assert palette.delta_hex(True) in unranked.value.text()
    assert meter_geometry(unranked) == (None, None, None)
    assert range_texts(unranked) == ("", "range unknown", "")
    assert unranked.toolTip().splitlines() == ["Source module: 10,000 m", "Mutator: unknown"]
    assert plain(equal.value) == "5.00 GJ ±0.00"
    assert palette.SECONDARY_TEXT in equal.value.text()
    assert meter_geometry(equal) == pytest.approx((0.5, 0.5, 0.5))
    assert range_texts(equal) == ("6.00 GJ", "base 5.00 GJ", "4.00 GJ")
    assert plain(inspector.rolls_note) == "Stasis Webifier II · mutaplasmid unknown"


def test_the_fetch_button_offers_a_first_fetch_or_a_retry_and_hides_for_ordinary_rows(
    abyssal_view,
):
    """The button's caption is the only place the user learns whether they
    are asking ESI for the first time or asking again about an item it has
    already said it does not know; and a Dominix must show no rolls section
    at all, including after an abyssal row left one on screen."""
    inspector = abyssal_view.inspector

    open_inspector(abyssal_view, BCS_UNFETCHED)
    assert not inspector.rolls_box.isHidden()
    assert inspector.roll_rows == []
    assert inspector.rolls_note.text() == "Stats not fetched yet"
    assert not inspector.fetch_abyssal_btn.isHidden()
    assert inspector.fetch_abyssal_btn.text() == "Fetch abyssal stats"

    open_inspector(abyssal_view, WEB_MISSING)
    assert inspector.rolls_note.text() == "ESI has no record of this item"
    assert not inspector.fetch_abyssal_btn.isHidden()
    assert inspector.fetch_abyssal_btn.text() == "Retry"

    open_inspector(abyssal_view, 1001)  # the Dominix
    assert inspector.title.text() == "Dominix"
    assert inspector.rolls_box.isHidden()
    assert inspector.roll_rows == []


def test_an_abyssal_row_badges_abyssal_yet_stays_unpriced_everywhere(abyssal_view):
    """The badge says why there is no quote; it must not turn the item into
    a priced one anywhere -- is:unpriced still finds it and the strip still
    counts it. The mutaplasmid stack shares meta group 15 and is the row
    that would badge wrongly if the gate were the meta group."""
    for item_id in ABYSSAL_ROWS:
        assert sell_cell(abyssal_view, item_id).data(gm.PRICE_BADGE_ROLE) == "abyssal"
    # Priced rows may wear an age badge (the seed's quotes are dated), but
    # never this one.
    assert sell_cell(abyssal_view, MUTAPLASMID_STACK).data(gm.PRICE_BADGE_ROLE) != "abyssal"
    assert sell_cell(abyssal_view, 1001).data(gm.PRICE_BADGE_ROLE) != "abyssal"

    # Tooltips: the batched summary for fetched items, the fetch state for
    # the rest, served under both the summary role and Qt's own tooltip role.
    for role in (gm.ABYSSAL_SUMMARY_ROLE, Qt.ToolTipRole):
        assert sell_cell(abyssal_view, BCS_OK).data(role) == BCS_SUMMARY
        assert sell_cell(abyssal_view, WEB_OK).data(role) == WEB_SUMMARY
        assert sell_cell(abyssal_view, BCS_UNFETCHED).data(role) == "Rolls not fetched"
        assert sell_cell(abyssal_view, WEB_MISSING).data(role) == "ESI has no record of this item"
        assert sell_cell(abyssal_view, MUTAPLASMID_STACK).data(role) is None

    assert abyssal_view.strip._unpriced.value.text() == str(len(ABYSSAL_ROWS))
    abyssal_view.omnibox.add_chip("is", "unpriced")
    assert item_ids(abyssal_view) == ABYSSAL_ROWS


def test_a_stat_chip_filters_in_display_units_through_the_pipeline(abyssal_view):
    """The number typed is the number the inspector shows: a duration stored
    as 5000 ms matches duration<9 (seconds); the missile bonus stored as
    1.1077 matches "Missile Damage Bonus">10 (percent); the web alias reaches
    speedFactor. Items without the attribute, and items with no stored stats
    at all, fall out of a positive stat: chip -- and back in for -stat:,
    because an item whose rolls are unknown cannot be said to fail a test."""
    omnibox = abyssal_view.omnibox

    omnibox.add_chip("stat", "duration<9")
    assert item_ids(abyssal_view) == {WEB_OK}
    omnibox.clear()
    omnibox.add_chip("stat", "duration<4")
    assert item_ids(abyssal_view) == set()
    omnibox.clear()

    omnibox.add_chip("stat", "Missile Damage Bonus>10")
    assert item_ids(abyssal_view) == {BCS_OK}
    omnibox.clear()
    omnibox.add_chip("stat", "Missile Damage Bonus>11")
    assert item_ids(abyssal_view) == set()
    omnibox.clear()

    omnibox.add_chip("stat", "web<-62")
    assert item_ids(abyssal_view) == {WEB_OK}
    omnibox.clear()

    omnibox.add_chip("stat", "cpu<26", negated=True)
    assert item_ids(abyssal_view) == (ABYSSAL_ROWS | ORDINARY_ROWS) - {BCS_OK}
    assert abyssal_view.state_label.text() == "8 of 9 stacks · 1 filter"
    # The chip's text form quotes the spaced name so a saved view reparses.
    omnibox.clear()
    omnibox.add_chip("stat", "Missile Damage Bonus>10")
    text = omnibox.spec().to_text()
    assert text == 'stat:"Missile Damage Bonus>10"'
    assert omni.parse(text).chips == omnibox.spec().chips


def test_is_abyssal_narrows_to_dynamic_types_and_its_negation_excludes_them(abyssal_view):
    """The chip is the type's isDynamicType, not meta group 15: the
    mutaplasmid stack must sit on the ordinary side of both polarities.
    Typed as `is:abyssal`, the alias, so the view is reached the way a
    saved view from before the chip reaches it."""
    abyssal_view.omnibox.set_spec(omni.parse("is:abyssal"))
    assert item_ids(abyssal_view) == ABYSSAL_ROWS

    abyssal_view.omnibox.set_spec(omni.parse("-is:abyssal"))
    assert item_ids(abyssal_view) == ORDINARY_ROWS


class _FakeSyncer:
    """One enabled character whose pull does nothing, so SyncJob reaches the
    step under test without ESI."""

    def __init__(self, conn, client, settings):
        pass

    def enabled_characters(self):
        return [{"character_id": 1}]

    def sync_character(self, row, progress=None, should_stop=None):
        return []


@pytest.mark.parametrize("enabled", [False, True])
def test_the_settings_switch_gates_the_sync_time_fetch(abyssal_conn, monkeypatch, enabled):
    """Off by default and off means off: a routine sync must not spend one
    request per abyssal item unless the user opted in. On, the sync asks
    about exactly the unfetched item -- never the one already stored, never
    the one ESI 404'd, never the mutaplasmid stack -- and stores the answer."""
    client = FakeESIClient(bodies={BCS_UNFETCHED: BCS_BODY})
    monkeypatch.setattr(db, "init", lambda path=None: abyssal_conn)
    monkeypatch.setattr(workers, "ESIClient", lambda settings, tokens: client)
    monkeypatch.setattr(workers, "Syncer", _FakeSyncer)
    settings = Settings(abyssal_stats_on_sync=enabled)

    job = workers.SyncJob(settings, tokens=None, reprice=False, snapshot=False)
    result = job.run_job()

    assert client.closed
    assert result["characters"] == 1
    if not enabled:
        assert client.calls == []
        assert "abyssal" not in result
        assert queries.fetch_abyssal_rolls(abyssal_conn, BCS_UNFETCHED)["status"] == "unfetched"
    else:
        assert client.calls == [f"/dogma/dynamic/items/{BCS_TYPE}/{BCS_UNFETCHED}"]
        assert result["abyssal"] == {"fetched": 1, "missing": 0, "failed": 0, "remaining": 0}
        rolls = queries.fetch_abyssal_rolls(abyssal_conn, BCS_UNFETCHED)
        assert rolls["status"] == "ok"
        assert [r["label"] for r in rolls["rolls"]] == [
            "CPU usage", "Missile Damage Bonus", "Rate of Fire Bonus",
        ]


@pytest.mark.parametrize("stale", [True, False])
def test_startup_reimports_the_sde_only_when_the_installed_tables_are_stale(
    app, conn, monkeypatch, started_tasks, stale
):
    """An estate that already has an SDE but was imported before the dogma
    tables existed gets them filled at startup from the cached zip; one
    whose tables are current must not pay for an import on every launch.
    The task registry's start is faked so nothing reaches the network."""
    monkeypatch.setattr(db, "init", lambda path=None: conn)
    started = started_tasks
    db.set_meta(conn, "sde_build", "3487903")
    if not stale:
        db.set_meta(conn, sde._TABLES_VERSION_KEY, str(sde.SDE_TABLES_VERSION))
    assert sde.tables_stale(conn) is stale, "the seed must put the database in the case under test"

    window = MainWindow(
        Settings(client_id="test-client-id", check_sde_on_startup=False)
    )
    try:
        kinds = [t.kind for t in started]
        if stale:
            assert kinds == ["sde"]
            assert isinstance(started[0].job, workers.SdeUpdateJob)
            assert started[0].label == "Refresh game data tables"
            assert window.tasks.is_active("sde")
        else:
            assert "sde" not in kinds
            assert not window.tasks.is_active("sde")
    finally:
        window.close()


class _InlinePool:
    """QThreadPool stand-in that runs the job on the calling thread, so the
    job's finished signal fires synchronously inside the click."""

    @staticmethod
    def globalInstance():  # noqa: N802 -- Qt's spelling
        return _InlinePool()

    def start(self, job) -> None:
        job.run()


def _inline_fetch(monkeypatch, conn, client) -> None:
    monkeypatch.setattr(db, "init", lambda path=None: conn)
    monkeypatch.setattr(assets_view_module, "QThreadPool", _InlinePool)
    monkeypatch.setattr(assets_view_module, "ESIClient", lambda settings, tokens: client)
    monkeypatch.setattr(assets_view_module, "TokenCache", lambda settings: None)
    monkeypatch.setattr(assets_view_module.Settings, "load", classmethod(lambda cls: cls()))


@pytest.mark.parametrize(
    ("item_id", "type_id", "body", "texts", "summary"),
    [
        (BCS_UNFETCHED, BCS_TYPE, BCS_BODY, BCS_ROLL_TEXTS, BCS_SUMMARY),
        (WEB_MISSING, WEB_TYPE, WEB_BODY, WEB_ROLL_TEXTS, WEB_SUMMARY),
    ],
    ids=["first fetch", "retry after a 404"],
)
def test_the_inspector_button_fetches_one_item_and_rerenders_with_its_rolls(
    abyssal_view, abyssal_conn, monkeypatch, item_id, type_id, body, texts, summary
):
    """The per-item path end to end: click -> job -> store -> refresh_all ->
    inspector re-rendered with the rolls and the button gone, and the badge
    tooltip beside it updated in the same reload -- a stale "Rolls not
    fetched" next to a panel full of rolls would contradict itself. The
    Retry case proves a 'missing' item is asked again on purpose."""
    client = FakeESIClient(bodies={item_id: body})
    _inline_fetch(monkeypatch, abyssal_conn, client)
    open_inspector(abyssal_view, item_id)
    inspector = abyssal_view.inspector
    assert sell_cell(abyssal_view, item_id).data(Qt.ToolTipRole) != summary

    inspector.fetch_abyssal_btn.click()

    assert client.calls == [f"/dogma/dynamic/items/{type_id}/{item_id}"]
    assert client.closed
    assert abyssal_view._abyssal_jobs == set(), "the strong reference is released on completion"
    assert inspector.title.text() == abyssal_conn.execute(
        "SELECT name FROM sde_types WHERE type_id = ?", (type_id,)
    ).fetchone()["name"]
    assert roll_lines(inspector) == texts
    assert inspector.fetch_abyssal_btn.isHidden()
    assert plain(inspector.rolls_note).endswith(" · Gravid mutaplasmid")
    assert sell_cell(abyssal_view, item_id).data(Qt.ToolTipRole) == summary
    assert panel_open(abyssal_view), "the reload re-rendered into the open panel"


def test_a_failed_per_item_fetch_re_arms_the_button(abyssal_view, abyssal_conn, monkeypatch):
    """set_rolls_fetching holds the button down so a second click cannot
    queue a second request; after an ESI error stores nothing the item is
    still unfetched and the button must come back enabled, or the user is
    stuck with a panel that says nothing and offers nothing."""
    client = FakeESIClient(errors={BCS_UNFETCHED})
    _inline_fetch(monkeypatch, abyssal_conn, client)
    open_inspector(abyssal_view, BCS_UNFETCHED)
    inspector = abyssal_view.inspector
    seen_disabled = []
    client_get = client.get

    def get_and_peek(path, **kw):
        # Observed mid-flight: the button must already be held down while
        # the request is out.
        seen_disabled.append(not inspector.fetch_abyssal_btn.isEnabled())
        return client_get(path, **kw)

    client.get = get_and_peek

    inspector.fetch_abyssal_btn.click()

    assert seen_disabled == [True]
    assert inspector.fetch_abyssal_btn.isEnabled()
    assert not inspector.fetch_abyssal_btn.isHidden()
    assert inspector.rolls_note.text() == "Stats not fetched yet"
    assert queries.fetch_abyssal_rolls(abyssal_conn, BCS_UNFETCHED)["status"] == "unfetched"
    assert abyssal_view._abyssal_jobs == set()


def capture_rolls_lookups(view: AssetsView) -> dict:
    """Hold both hosts' rolls lookups instead of running them inline, so a
    test can deliver each result when (and whether) it chooses."""
    pending: dict[str, tuple] = {}

    def hold(name):
        return lambda fn, on_done, on_failed=None: pending.__setitem__(name, (fn, on_done))

    view._rolls_query.run = hold("panel")
    view._window_rolls_query.run = hold("window")
    return pending


def test_a_rolls_result_arriving_after_the_window_closed_neither_paints_nor_reopens_it(
    abyssal_view, abyssal_conn
):
    """The rolls lookup runs on a pool thread and can land after the user
    has closed the window. AsyncQuery's generation guard drops it once
    cancel() has run; this pins the second guard in _load_rolls -- the
    delivery callback itself must refuse to paint when the host has no row
    -- and that nothing on that path calls show()."""
    pending = capture_rolls_lookups(abyssal_view)
    inspect_in_window(abyssal_view, row_position(abyssal_view, BCS_UNFETCHED))
    inspector = abyssal_view.inspector_window.inspector
    assert inspector.rolls_note.text() == "Loading rolled stats…"
    fetch, deliver = pending["window"]

    abyssal_view._close_inspector(abyssal_view._window_host)
    deliver(fetch(abyssal_conn))

    assert not window_open(abyssal_view)
    assert abyssal_view._window_host.row is None
    assert inspector.rolls_note.text() == "Loading rolled stats…", "nothing was painted"
    assert inspector.roll_rows == []


def test_a_rolls_result_for_one_host_never_paints_into_the_other(abyssal_view, abyssal_conn):
    """Two mutated modules open at once -- the stored BCS in the window, the
    404'd web in the panel -- each with its own lookup in flight. Each result
    must land in the host that asked, whichever order they arrive in; one
    shared guard would paint the BCS rolls under the web's name."""
    pending = capture_rolls_lookups(abyssal_view)
    inspect_in_window(abyssal_view, row_position(abyssal_view, BCS_OK))
    open_inspector(abyssal_view, WEB_MISSING)
    panel, window = abyssal_view.inspector, abyssal_view.inspector_window.inspector
    assert panel.rolls_note.text() == window.rolls_note.text() == "Loading rolled stats…"
    assert set(pending) == {"panel", "window"}

    fetch, deliver = pending["window"]
    deliver(fetch(abyssal_conn))
    assert roll_lines(window) == BCS_ROLL_TEXTS
    assert panel.roll_rows == []
    assert panel.rolls_note.text() == "Loading rolled stats…"

    fetch, deliver = pending["panel"]
    deliver(fetch(abyssal_conn))
    assert panel.rolls_note.text() == "ESI has no record of this item"
    assert panel.roll_rows == []
    assert roll_lines(window) == BCS_ROLL_TEXTS
    assert plain(window.rolls_note) == BCS_SOURCE_LINE


def test_the_fetch_button_in_each_host_fetches_that_hosts_item(
    abyssal_view, abyssal_conn, monkeypatch
):
    """The window shows the unfetched BCS, the panel the 404'd web. The
    window's button must fetch the BCS and re-render the window with its
    rolls while the panel keeps saying what it said; then the panel's button
    fetches the web into the panel and the window is untouched."""
    client = FakeESIClient(bodies={BCS_UNFETCHED: BCS_BODY, WEB_MISSING: WEB_BODY})
    _inline_fetch(monkeypatch, abyssal_conn, client)
    inspect_in_window(abyssal_view, row_position(abyssal_view, BCS_UNFETCHED))
    open_inspector(abyssal_view, WEB_MISSING)
    panel, window = abyssal_view.inspector, abyssal_view.inspector_window.inspector

    window.fetch_abyssal_btn.click()

    assert client.calls == [f"/dogma/dynamic/items/{BCS_TYPE}/{BCS_UNFETCHED}"]
    assert roll_lines(window) == BCS_ROLL_TEXTS
    assert window.fetch_abyssal_btn.isHidden()
    assert window_open(abyssal_view) and panel_open(abyssal_view)
    assert panel.roll_rows == []
    assert panel.rolls_note.text() == "ESI has no record of this item"
    assert panel.fetch_abyssal_btn.text() == "Retry"
    assert panel.fetch_abyssal_btn.isEnabled()

    panel.fetch_abyssal_btn.click()

    assert client.calls[-1] == f"/dogma/dynamic/items/{WEB_TYPE}/{WEB_MISSING}"
    assert roll_lines(panel) == WEB_ROLL_TEXTS
    assert panel.fetch_abyssal_btn.isHidden()
    assert roll_lines(window) == BCS_ROLL_TEXTS
    assert abyssal_view._abyssal_jobs == set()


# ------------------------------------------------------ abyssal complex search
# The abyssal chip, its card and the roll columns, driven through the same
# wiring as everything above. Every number here is from the seed: the BCS
# rolls CPU 50%, missile 0% and rate of fire 66% (BCS_QUALITIES, hand-computed
# above), the webifier speed 75% and CPU 86%.
BCS_NAME = "Abyssal Ballistic Control System"
WEB_NAME = "Abyssal Stasis Webifier"
NOBODY_OWNS = "Abyssal Warp Disruptor"

# The seed's BCS range table names four attributes, but the pickers and the
# columns are the ones the estate holds values for: the synthetic drone bonus
# (1255) is in the range table and on no item, so it is offered nowhere -- a
# column blank on every row and a slider with no range would be phantoms.
BCS_ATTRIBUTE_LABELS = ["CPU usage", "Missile Damage Bonus", "Rate of Fire Bonus"]
BCS_ROLL_KEYS = [gm.roll_key(50), gm.roll_key(213), gm.roll_key(204)]
BCS_ROLL_HEADERS = ["CPU", "Missile dmg", "RoF"]
# Display values behind the columns and the export: 25.8 tf as stored (unit
# 106), (1.1077 - 1) * 100 for the modifier percent (109), (1 - 0.8829) * 100
# for the inverse absolute percent (111).
BCS_CPU_DISPLAY = 25.799999713897705
BCS_MISSILE_DISPLAY = (1.1077080251407625 - 1) * 100
BCS_ROF_DISPLAY = (1 - 0.8828844567859173) * 100
# Mean of 0.5000, 0.0010 and 0.6570: the Roll cell shows 39%, the export 38.6.
BCS_MEAN_QUALITY = 0.386


def commit_text(view: AssetsView, text: str) -> None:
    """Type into the omnibox and press Enter, the way a user commits a filter."""
    view.omnibox.edit.setText(text)
    QTest.keyClick(view.omnibox.edit, Qt.Key_Return)


def chip_labels(view: AssetsView) -> list[str]:
    return [widget.value_label.text() for _chip, widget in view.omnibox._chips]


def record(signal) -> list:
    calls: list = []
    signal.connect(lambda *args: calls.append(args))
    return calls


def open_card(view: AssetsView, app) -> AbyssalCard:
    """Click the abyssal chip's glyph and hand back the card it opened.

    Qt closes a popup shown in the same event turn that inserted the chip
    widget it is anchored to, so the pending events are drained first --
    which is also what happens on a desktop between a keystroke and a click.
    """
    app.processEvents()
    widget = next(w for c, w in view.omnibox._chips if c.kind == omni.ABYSSAL_KIND)
    requests = record(view.omnibox.card_requested)
    widget.card_btn.click()
    assert len(requests) == 1 and requests[0][1] is widget
    card = view._card
    assert card is not None and card.isVisible(), "the glyph must open the card"
    return card


def bound_fields(row) -> tuple[str, str]:
    """A stat row's worst-side and best-side field texts."""
    return row.left_field.text(), row.right_field.text()


def settle_count(view: AssetsView) -> str:
    """Fire the card's debounced count now and read the footer."""
    view._card_count.flush()
    return match_text(view._card)


def picker(card: AbyssalCard) -> list[tuple[str, str, bool]]:
    """(type name, rendered text, selected) per entry of the card's type
    dropdown."""
    combo = card.type_combo
    return [
        (combo.itemData(i), combo.itemText(i), i == combo.currentIndex())
        for i in range(combo.count())
    ]


def pick(card: AbyssalCard, name: str) -> None:
    """Select a type in the dropdown by its full name."""
    index = card.type_combo.findData(name)
    assert index >= 0, f"{name!r} not in the picker"
    card.type_combo.setCurrentIndex(index)


def clear_type(card: AbyssalCard) -> None:
    """Empty the type edit and press Enter: the user's way back from a type
    to every abyssal item."""
    card.type_edit.setFocus()
    card.type_edit.selectAll()
    QTest.keyClick(card.type_edit, Qt.Key_Backspace)
    QTest.keyClick(card.type_edit, Qt.Key_Return)


def column_keys(view: AssetsView) -> list[str]:
    return [key for key, _header in view.model.columns()]


def cell(view: AssetsView, item_id: int, key: str):
    return view.model.index(row_position(view, item_id), column_keys(view).index(key))


def background_hex(index) -> str | None:
    brush = index.data(Qt.BackgroundRole)
    return None if brush is None else brush.color().name()


def test_typing_abyssal_in_any_spelling_mints_the_chip_and_narrows_to_the_mutated_modules(
    abyssal_view,
):
    """The bare word, the typed type list and the older is:abyssal spelling
    all land as the one chip kind, rendered through the three label shapes,
    and the filter is the type flag: the mutaplasmid stack shares the
    abyssals' meta group and must stay out."""
    omnibox = abyssal_view.omnibox
    assert len(abyssal_view.model.rows()) == 9

    commit_text(abyssal_view, "abyssal")
    assert omnibox.spec().chips == [omni.Chip("abyssal", "")]
    assert chip_labels(abyssal_view) == ["Abyssal"]
    assert item_ids(abyssal_view) == ABYSSAL_ROWS
    assert abyssal_view.state_label.text() == "4 of 9 stacks · 1 filter"

    omnibox.clear()
    commit_text(abyssal_view, f'abyssal:"{WEB_NAME}"')
    assert omnibox.spec().chips == [omni.Chip("abyssal", WEB_NAME)]
    assert chip_labels(abyssal_view) == ["Abyssal · Stasis Webifier"]
    assert item_ids(abyssal_view) == {WEB_OK, WEB_MISSING}

    omnibox.clear()
    commit_text(abyssal_view, f'abyssal:"{BCS_NAME}, {WEB_NAME}"')
    assert chip_labels(abyssal_view) == ["Abyssal · 2 types"]
    assert item_ids(abyssal_view) == ABYSSAL_ROWS, "types within the chip OR"

    omnibox.clear()
    commit_text(abyssal_view, "is:abyssal")
    assert omnibox.spec().chips == [omni.Chip("abyssal", "")], "the alias is the same chip"
    assert chip_labels(abyssal_view) == ["Abyssal"]
    assert item_ids(abyssal_view) == ABYSSAL_ROWS


def test_roll_chips_filter_on_the_hand_computed_quality_and_negation_keeps_unfetched_items(
    abyssal_view,
):
    """roll: is the mirrored quality in percent, so the webifier's -63
    speedFactor (a better web than base) is a 75% roll and passes >=70 but
    not >=80; a range is inclusive of both ends and matches the BCS's 50%
    CPU. Negation is NOT EXISTS: an item whose rolls are unknown cannot be
    said to fail, so -roll:cpu>=0 keeps the unfetched and the 404'd abyssals
    along with every ordinary row and hides only the two fetched ones."""
    omnibox = abyssal_view.omnibox

    commit_text(abyssal_view, "abyssal roll:web>=70")
    assert omnibox.spec().chips == [omni.Chip("abyssal", ""), omni.Chip("roll", "web>=70")]
    assert item_ids(abyssal_view) == {WEB_OK}
    assert abyssal_view.state_label.text() == "1 of 9 stacks · 2 filters"

    omnibox.clear()
    commit_text(abyssal_view, "abyssal roll:web>=80")
    assert item_ids(abyssal_view) == set()

    omnibox.clear()
    commit_text(abyssal_view, "roll:cpu=40..60")
    assert omnibox.spec().chips == [omni.Chip("roll", "cpu=40..60")]
    assert item_ids(abyssal_view) == {BCS_OK}, "the webifier's 86% CPU roll is out of range"

    omnibox.clear()
    commit_text(abyssal_view, "-roll:cpu>=0")
    assert omnibox.spec().chips == [omni.Chip("roll", "cpu>=0", negated=True)]
    assert item_ids(abyssal_view) == ORDINARY_ROWS | {BCS_UNFETCHED, WEB_MISSING}


def test_the_chip_glyph_opens_the_card_listing_owned_types_and_gating_the_stat_rows(
    abyssal_view, app
):
    """The picker is the estate's types with their counts, busiest first and
    the mutaplasmid stack absent, none of them picked for the bare chip --
    there is no "All" entry, the empty edit is that state; the stat rows
    are usable only with one type picked, and then offer that type's rolled
    attributes with slider bounds taken from the items actually fetched."""
    abyssal_view.omnibox.add_chip("abyssal", "")

    card = open_card(abyssal_view, app)

    assert picker(card) == [
        (BCS_NAME, "Ballistic Control System · 2", False),
        (WEB_NAME, "Stasis Webifier · 2", False),
    ]
    # Open ready to type: the edit has the keyboard, empty under its placeholder.
    assert card.type_edit.hasFocus()
    assert card.type_combo.currentIndex() == -1 and card.type_edit.text() == ""
    assert card.type_edit.placeholderText() == "Type to search, or pick…"
    assert not card.rows_box.isEnabled()
    assert card.add_row_btn.isHidden()
    assert card.add_row() is None, "no type: no attribute list, no row"

    pick(card, BCS_NAME)
    assert card.rows_box.isEnabled()
    assert card.add_row_btn.isEnabled()
    row = card.add_row()
    assert [row.attr_combo.itemText(i) for i in range(row.attr_combo.count())] == (
        BCS_ATTRIBUTE_LABELS
    )
    assert [row.attr_combo.itemData(i) for i in range(row.attr_combo.count())] == [50, 213, 204]
    # Bounds come from the one fetched BCS, after unit conversion, and the
    # picker offers exactly the attributes that have them: the drone bonus
    # is in the range table but on no BCS, so it is not there to pick.
    assert set(card._bounds) == {50, 204, 213}
    assert card._bounds[50] == pytest.approx((BCS_CPU_DISPLAY, BCS_CPU_DISPLAY))
    assert card._bounds[204] == pytest.approx((BCS_ROF_DISPLAY, BCS_ROF_DISPLAY))
    assert card._bounds[213] == pytest.approx((BCS_MISSILE_DISPLAY, BCS_MISSILE_DISPLAY))
    assert row.select_attribute(50)
    assert not row.select_attribute(1255), "an attribute with no values is not offered"

    # The webifier's list is its own: speed and CPU, nothing of the BCS's --
    # so a row about the missile bonus does not survive the switch. The
    # switch is typed this time: a search committed with Enter must run the
    # same reload as a pick in the list.
    assert row.select_attribute(213)
    card.type_edit.setFocus()
    card.type_edit.selectAll()
    QTest.keyClicks(card.type_edit, "web")
    QTest.keyClick(card._type_completer.popup(), Qt.Key_Return)
    assert card.selected_types() == [WEB_NAME] and card.isVisible()
    assert card.rows() == [], "a row about a stat the new type does not roll is dropped"
    row = card.add_row()
    assert [row.attr_combo.itemText(i) for i in range(row.attr_combo.count())] == [
        "CPU usage", "Maximum Velocity Bonus",
    ]
    assert card._bounds[20] == pytest.approx((-63.0, -63.0))

    # Back to every item, by clearing the edit: the rows go dark but stay,
    # for a return to the type.
    clear_type(card)
    assert card.selected_types() == [] and card.type_edit.text() == ""
    assert not card.rows_box.isEnabled()
    assert card.add_row_btn.isHidden()
    assert card.add_row() is None
    assert card.rows() == [row]


def test_done_writes_the_type_and_a_stat_chip_per_row_and_replaces_the_cards_old_chips(
    abyssal_view, app
):
    """Done is the one path that applies: the abyssal chip re-written with
    the picked type and one stat: chip per row, in place of every positive
    abyssal/stat chip that was there -- the CPU chip is replaced by the row
    it seeded, and the stat chip about powergrid, which no BCS mutaplasmid
    rolls, vanishes rather than AND the new filter down to nothing -- while
    a chip of any other kind stays exactly where it was. The picker facets
    by every chip but the abyssal one, so both Jita items count once and
    the Amarr ones not at all. The estate's one fetched BCS sets the row's
    bounds to its own 25.799999713897705 tf, so the chip Done writes back
    must still admit that item, and a reopen must land on the same row."""
    omnibox = abyssal_view.omnibox
    omnibox.set_spec(
        omni.parse('loc:"Jita IV - Moon 4" stat:"CPU usage">=25 stat:power<2 abyssal')
    )
    assert item_ids(abyssal_view) == {BCS_OK, WEB_OK}

    card = open_card(abyssal_view, app)
    assert picker(card) == [
        (BCS_NAME, "Ballistic Control System · 1", False),
        (WEB_NAME, "Stasis Webifier · 1", False),
    ]
    pick(card, BCS_NAME)
    (row,) = card.rows()
    assert row.attribute_id() == 50, "the CPU chip seeded its row"
    assert bound_fields(row) == ("25.8 tf", "25.8 tf"), "one fetched BCS"
    done = record(card.done)

    card.done_btn.click()

    # Over one item the range is a point: the slider reads both handles as
    # resting on the low bound, so the chip is one-sided -- and still admits
    # the item, which is what matters.
    assert done == [([omni.Chip("abyssal", BCS_NAME), omni.Chip("stat", "CPU usage<=25.8")],)]
    assert omnibox.spec() == omni.FilterSpec(
        text="",
        chips=[
            omni.Chip("location", "Jita IV - Moon 4"),
            omni.Chip("abyssal", BCS_NAME),
            omni.Chip("stat", "CPU usage<=25.8"),
        ],
    )
    assert not card.isVisible()
    assert chip_labels(abyssal_view) == [
        "Jita IV - Moon 4", "Abyssal · Ballistic Control System", "CPU usage<=25.8",
    ]
    assert item_ids(abyssal_view) == {BCS_OK}, "Jita's BCS at 25.8 tf; Amarr's is unfetched"
    assert abyssal_view.state_label.text() == "1 of 9 stacks · 3 filters"

    # Reopening seeds the card from what Done wrote: the type picked, the
    # row back on the CPU at the one value the estate holds.
    card = open_card(abyssal_view, app)
    assert [name for name, _text, selected in picker(card) if selected] == [BCS_NAME]
    (row,) = card.rows()
    assert row.attribute_id() == 50
    assert bound_fields(row) == ("25.8 tf", "25.8 tf")
    card.cancel_btn.click()


def test_cancel_and_escape_leave_the_filter_exactly_as_it_was(abyssal_view, app):
    """Every way out but Done reads as Cancel: picking a type and building a
    row, then Cancel or Esc, must not move a chip or reload a row -- an
    exploratory look at the card that rewrote the filter would be a filter
    that drifts each time it is opened."""
    omnibox = abyssal_view.omnibox
    omnibox.set_spec(omni.parse("abyssal roll:cpu>=40"))
    before = omnibox.spec()
    assert item_ids(abyssal_view) == {BCS_OK, WEB_OK}, "50% and 86% CPU rolls both clear 40"
    changes = record(omnibox.changed)

    card = open_card(abyssal_view, app)
    cancelled = record(card.cancelled)
    pick(card, BCS_NAME)
    assert card.rows() == [], "a roll: chip is not the card's to edit and seeds no row"
    row = card.add_row(50)
    row.set_range(0, 100)
    card.add_row(213)
    card.cancel_btn.click()

    assert cancelled == [()]
    assert not card.isVisible()
    assert omnibox.spec() == before
    assert item_ids(abyssal_view) == {BCS_OK, WEB_OK}

    card = open_card(abyssal_view, app)
    pick(card, WEB_NAME)
    pick(card, BCS_NAME)
    QTest.keyClick(card, Qt.Key_Escape)

    assert cancelled == [(), ()]
    assert not card.isVisible()
    assert omnibox.spec() == before
    assert changes == [], "nothing on the Cancel paths touched the omnibox"


def test_the_banner_counts_unfetched_items_and_fetch_hands_their_ids_to_the_job_path(
    abyssal_view, app
):
    """The banner counts what the fetch job would ask ESI about: the
    unfetched BCS, not the webifier ESI already 404'd. Fetch resolves the
    picked type (none picked means every type) to item ids and emits them for the
    main window's job -- with the webifier picked there is nothing pending
    and the banner is gone."""
    abyssal_view.omnibox.add_chip("abyssal", "")
    requests = record(abyssal_view.abyssal_fetch_requested)

    card = open_card(abyssal_view, app)

    assert not card.banner.isHidden()
    assert card.banner_label.text() == "1 abyssal item not fetched —"
    card.fetch_btn.click()
    assert requests == [([BCS_UNFETCHED],)]
    assert not card.fetch_btn.isEnabled(), "held down while the request is out"

    pick(card, WEB_NAME)
    assert card.banner.isHidden(), "the 404'd webifier is not pending"

    pick(card, BCS_NAME)
    assert not card.banner.isHidden()
    assert card.fetch_btn.isEnabled(), "re-armed with the fresh count"
    card.fetch_btn.click()
    assert requests == [([BCS_UNFETCHED],), ([BCS_UNFETCHED],)]


def test_the_cards_fetch_request_reaches_the_main_window_as_an_abyssal_job(
    app, abyssal_conn, monkeypatch, started_tasks
):
    """The view only emits; the main window must submit the job scoped to
    those ids and queued behind any sync, or the banner's button would be
    the one abyssal fetch path that does nothing. The registry's start is
    faked so nothing reaches ESI."""
    monkeypatch.setattr(db, "init", lambda path=None: abyssal_conn)
    db.set_meta(abyssal_conn, "sde_build", "3487903")
    db.set_meta(abyssal_conn, sde._TABLES_VERSION_KEY, str(sde.SDE_TABLES_VERSION))
    started = started_tasks
    window = MainWindow(
        Settings(client_id="test-client-id", check_sde_on_startup=False)
    )
    try:
        assert [t.kind for t in started] == [], "a current SDE starts nothing at launch"

        window.assets.abyssal_fetch_requested.emit([BCS_UNFETCHED])

        assert [t.kind for t in started] == ["abyssal"]
        (task,) = started
        assert isinstance(task.job, workers.AbyssalStatsJob)
        assert task.job.item_ids == [BCS_UNFETCHED]
        assert task.job.retry_missing is False
        assert task.after == ("sync",)
    finally:
        window.close()


def test_one_type_grows_roll_columns_after_qty_with_values_washes_and_a_mean(abyssal_view):
    """With the chip on one type the table gains that type's rolled
    attributes after Qty and a Roll column, every other column keeping its
    order. A cell is the display value the inspector shows, washed by its
    quality -- nothing at the BCS's 50% CPU, which sits in the plain band;
    the CRITICAL side for the 0% missile roll and the POSITIVE side for the
    66% rate of fire -- and Roll is the mean over the rankable rolls. An
    unfetched item's cells are blank and unwashed."""
    abyssal_view.omnibox.add_chip("abyssal", BCS_NAME)
    assert item_ids(abyssal_view) == {BCS_OK, BCS_UNFETCHED}

    keys = column_keys(abyssal_view)
    base_keys = [key for key, _header in queries.ASSET_COLUMNS]
    qty = base_keys.index("quantity")
    assert keys[: qty + 1] == base_keys[: qty + 1]
    assert keys[qty + 1 : qty + 5] == BCS_ROLL_KEYS + [gm.ROLL_MEAN_KEY]
    assert keys[qty + 5 :] == base_keys[qty + 1 :]
    headers = [header for _key, header in abyssal_view.model.columns()]
    assert headers[qty + 1 : qty + 5] == BCS_ROLL_HEADERS + ["Roll"]
    assert abyssal_view.model.columnCount() == len(queries.ASSET_COLUMNS) + 4
    assert gm.roll_key(1255) not in keys, "no BCS has a drone bonus value: no column"

    cpu = cell(abyssal_view, BCS_OK, gm.roll_key(50))
    assert cpu.data(Qt.DisplayRole) == "26 tf"
    assert cpu.data(Qt.UserRole) == pytest.approx(BCS_CPU_DISPLAY)
    assert cpu.data(gm.ROLL_QUALITY_ROLE) == pytest.approx(0.5, abs=1e-6)
    assert background_hex(cpu) is None, "50% is the plain band"
    assert cpu.data(Qt.ToolTipRole) == "50% of the possible roll"

    missile = cell(abyssal_view, BCS_OK, gm.roll_key(213))
    assert missile.data(Qt.DisplayRole) == "+11%"
    assert missile.data(gm.ROLL_QUALITY_ROLE) == pytest.approx(0.001, abs=1e-3)
    rof = cell(abyssal_view, BCS_OK, gm.roll_key(204))
    assert rof.data(Qt.DisplayRole) == "12%"
    assert rof.data(gm.ROLL_QUALITY_ROLE) == pytest.approx(0.657, abs=1e-3)
    for index in (missile, rof):
        expected = palette.quality_tint(index.data(gm.ROLL_QUALITY_ROLE))
        assert expected is not None
        assert background_hex(index) == QColor(expected).name()
    assert background_hex(missile) != background_hex(rof), "a bad and a good roll differ"
    dark = palette.is_dark()
    assert background_hex(missile) == QColor(palette._quality_hex(0.001, dark)).name()
    assert background_hex(rof) == QColor(palette._quality_hex(0.657, dark)).name()

    roll = cell(abyssal_view, BCS_OK, gm.ROLL_MEAN_KEY)
    assert roll.data(Qt.DisplayRole) == "39%"
    assert roll.data(Qt.UserRole) == pytest.approx(BCS_MEAN_QUALITY, abs=1e-3)
    assert background_hex(roll) == QColor(
        palette._quality_hex(roll.data(Qt.UserRole), dark)
    ).name()

    for key in BCS_ROLL_KEYS + [gm.ROLL_MEAN_KEY]:
        blank = cell(abyssal_view, BCS_UNFETCHED, key)
        assert blank.data(Qt.DisplayRole) == ""
        assert background_hex(blank) is None
        assert blank.data(gm.ROLL_QUALITY_ROLE) is None

    # The positional consumers still hold: the value badge sits on the
    # shifted sell column, and a filter from the Group cell still mints a
    # group chip rather than whatever now occupies Group's old index.
    sell = cell(abyssal_view, BCS_OK, "sell_value")
    assert sell.data(gm.PRICE_BADGE_ROLE) == "abyssal"
    abyssal_view.tree.selectionModel().setCurrentIndex(
        cell(abyssal_view, BCS_OK, "grp"), QItemSelectionModel.NoUpdate
    )
    abyssal_view._filter_current_cell(negated=False)
    assert omni.Chip("group", "Ballistic Control system") in abyssal_view.omnibox.spec().chips


def test_sorting_by_a_roll_column_orders_by_value_survives_a_reload_and_clears_with_it(
    abyssal_view, abyssal_conn
):
    """A second fetched BCS with a 9.5 tf CPU: numerically below the 25.8 tf
    one but alphabetically after it ("9.50 tf" > "26 tf"), so a sort on the
    rendered text would put it last. The sort is remembered by key, so it
    outlives a reload; and when the chip widens to two types the columns
    go, the sort clears rather than landing on whatever column now sits at
    that index, and the table carries on."""
    body = dict(
        BCS_BODY,
        dogma_attributes=[
            {**a, "value": 9.5} if a["attribute_id"] == 50 else a
            for a in BCS_BODY["dogma_attributes"]
        ],
    )
    abyssal.store_rolls(abyssal_conn, BCS_UNFETCHED, BCS_TYPE, body)
    view = abyssal_view
    view.omnibox.add_chip("abyssal", BCS_NAME)
    cpu_column = column_keys(view).index(gm.roll_key(50))
    assert cell(view, BCS_UNFETCHED, gm.roll_key(50)).data(Qt.DisplayRole) == "9.50 tf"
    assert [r["item_id"] for r in view.model.rows()] == [BCS_OK, BCS_UNFETCHED], "insertion order"

    view.tree.header().sectionClicked.emit(cpu_column)
    assert [r["item_id"] for r in view.model.rows()] == [BCS_UNFETCHED, BCS_OK]
    assert (view.sorter.key, view.sorter.column) == (gm.roll_key(50), cpu_column)
    assert view.tree.header().sortIndicatorSection() == cpu_column

    view.tree.header().sectionClicked.emit(cpu_column)
    assert [r["item_id"] for r in view.model.rows()] == [BCS_OK, BCS_UNFETCHED], "descending"

    view.reload()
    assert [r["item_id"] for r in view.model.rows()] == [BCS_OK, BCS_UNFETCHED]
    assert view.sorter.key == gm.roll_key(50)

    view.omnibox.set_spec(
        omni.FilterSpec(chips=[omni.Chip("abyssal", omni.join_types([BCS_NAME, WEB_NAME]))])
    )
    assert view.model.columns() == queries.ASSET_COLUMNS
    assert view.sorter.key is None and view.sorter.column == -1
    assert view.tree.header().sortIndicatorSection() == -1
    assert item_ids(view) == ABYSSAL_ROWS
    assert view.footer.text().startswith("4 stacks"), "no query failure landed in the footer"


def test_a_saved_view_with_the_abyssal_chip_and_a_roll_range_recalls_identically(abyssal_view):
    """Saved views are grammar text, so the new chips are covered only if
    to_text writes what parse reads: the quoted type name, the `..` range
    and the bare-word spelling of the plain chip all round-trip, and the
    recalled view filters and grows its columns like the original."""
    view = abyssal_view
    spec = omni.FilterSpec(
        chips=[omni.Chip("abyssal", BCS_NAME), omni.Chip("roll", "cpu=40..60")]
    )
    view.omnibox.set_spec(spec)
    assert item_ids(view) == {BCS_OK}
    assert len(view.model.columns()) == len(queries.ASSET_COLUMNS) + 4

    view._save_view(4)

    stored = json.loads(
        view.conn.execute("SELECT state_json FROM saved_views WHERE slot=4").fetchone()[
            "state_json"
        ]
    )
    assert stored["filter"] == f'abyssal:"{BCS_NAME}" roll:cpu=40..60'
    assert omni.parse(stored["filter"]) == spec

    view.omnibox.clear()
    assert len(view.model.rows()) == 9
    assert view.model.columns() == queries.ASSET_COLUMNS

    view._recall_view(4)

    assert view.omnibox.spec() == spec
    assert item_ids(view) == {BCS_OK}
    assert column_keys(view)[4:8] == BCS_ROLL_KEYS + [gm.ROLL_MEAN_KEY]

    plain = omni.FilterSpec(
        chips=[omni.Chip("abyssal", ""), omni.Chip("roll", "web>=70", negated=True)]
    )
    assert plain.to_text() == "abyssal -roll:web>=70"
    assert omni.parse(plain.to_text()) == plain


def test_a_type_nobody_owns_filters_to_nothing_and_the_picker_shows_it_with_a_zero_count(
    abyssal_view, app
):
    """A saved view can outlive its item. The table goes honestly empty,
    and the card lists the vanished type picked with a count of 0 so the
    user can see why and pick another -- and Done with a real type in its
    place recovers the table."""
    view = abyssal_view
    view.omnibox.set_spec(omni.parse(f'abyssal:"{NOBODY_OWNS}"'))
    assert item_ids(view) == set()
    assert view.state_label.text() == "0 of 9 stacks · 1 filter"
    assert chip_labels(view) == ["Abyssal · Warp Disruptor"]
    # One type, but nothing fetched of it: no roll columns, not even Roll.
    assert view.model.columns() == queries.ASSET_COLUMNS

    card = open_card(view, app)

    assert picker(card) == [
        (BCS_NAME, "Ballistic Control System · 2", False),
        (WEB_NAME, "Stasis Webifier · 2", False),
        (NOBODY_OWNS, "Warp Disruptor · 0", True),
    ]
    pick(card, WEB_NAME)
    card.done_btn.click()

    assert view.omnibox.spec().chips == [omni.Chip("abyssal", WEB_NAME)]
    assert item_ids(view) == {WEB_OK, WEB_MISSING}


def test_export_csv_carries_the_roll_columns_and_their_values(abyssal_view, tmp_path, monkeypatch):
    """The export reads the model's live column set, so the roll columns go
    out under their headers with the raw display values -- and Roll as the
    percent the table shows, not the 0..1 fraction it sorts by. An
    unfetched item's roll cells export empty, as they render."""
    view = abyssal_view
    view.omnibox.add_chip("abyssal", BCS_NAME)
    target = tmp_path / "rolls.csv"
    monkeypatch.setattr(
        assets_view_module.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *args, **kwargs: (str(target), "CSV files (*.csv)")),
    )

    view.export_csv()

    with open(target, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    header, *body = rows
    assert header == [h for _k, h in view.model.columns()]
    qty = header.index("Qty")
    assert header[qty + 1 : qty + 5] == BCS_ROLL_HEADERS + ["Roll"]
    assert len(body) == 2
    columns = column_keys(view)
    fetched = body[row_position(view, BCS_OK)]
    assert fetched[header.index("Item")] == BCS_NAME
    assert float(fetched[columns.index(gm.roll_key(50))]) == pytest.approx(BCS_CPU_DISPLAY)
    assert float(fetched[columns.index(gm.roll_key(213))]) == pytest.approx(BCS_MISSILE_DISPLAY)
    assert float(fetched[columns.index(gm.roll_key(204))]) == pytest.approx(BCS_ROF_DISPLAY)
    assert fetched[columns.index(gm.ROLL_MEAN_KEY)] == "38.6"
    unfetched = body[row_position(view, BCS_UNFETCHED)]
    assert [unfetched[columns.index(k)] for k in BCS_ROLL_KEYS + [gm.ROLL_MEAN_KEY]] == [""] * 4
    assert view.footer.text() == f"Exported 2 rows to {target}"


def test_a_full_range_value_row_over_one_item_keeps_that_item_through_done(abyssal_view, app):
    """Pins a bug that shipped: the bound fields show the BCS's
    25.799999713897705 tf CPU as 25.8, and a chip written from the fields
    -- ``CPU usage>=25.8`` -- excluded the one item whose bounds the
    slider ran between, because stat: compares the exact display value. The
    chip must round outward from the slider's exact position, so Done over
    an untouched full-range row keeps the item."""
    view = abyssal_view
    view.omnibox.add_chip("abyssal", BCS_NAME)
    assert item_ids(view) == {BCS_OK, BCS_UNFETCHED}

    card = open_card(view, app)
    row = card.add_row(50)
    assert bound_fields(row) == ("25.8 tf", "25.8 tf"), "what the user reads"
    card.done_btn.click()

    (stat_chip,) = [c for c in view.omnibox.spec().chips if c.kind == omni.STAT_KIND]
    term = omni.parse_stat(stat_chip.value)
    assert term is not None and term.name == "CPU usage"
    assert term.low <= BCS_CPU_DISPLAY, f"{stat_chip.value} excludes the bound item"
    assert term.high is None or term.high >= BCS_CPU_DISPLAY
    assert item_ids(view) == {BCS_OK}, "the row was scoped to this item; it must survive"


def test_the_footer_counts_live_what_done_would_leave_out_of_the_types_items(abyssal_view, app):
    """The card's "N of TOTAL match" is asked of the database after every
    change, debounced: it reads "…" until the count lands, TOTAL is the
    picked type's items under the rest of the filter (every abyssal item
    with no type picked), and N is what the chips as they stand would leave -- a
    full-range stat row already drops the unfetched BCS and the 404'd
    webifier, which have no rolls to compare. Cancel takes the pending
    count off the clock."""
    view = abyssal_view
    view.omnibox.add_chip("abyssal", "")
    card = open_card(view, app)
    assert match_text(card) == "…"
    assert settle_count(view) == "4 of 4 match"

    pick(card, BCS_NAME)
    assert card.match_count_label.text() == "…", "a type pick is a change"
    assert settle_count(view) == "2 of 2 match"
    row = card.add_row(50)
    assert settle_count(view) == "1 of 2 match", "the unfetched BCS has no CPU roll"

    pick(card, WEB_NAME)
    assert card.rows() == [row], "the webifier rolls CPU too, so the row survives the switch"
    assert bound_fields(row) == ("27 tf", "27 tf")
    assert settle_count(view) == "1 of 2 match"
    row = card.add_row(20)
    assert bound_fields(row) == ("-63%", "-63%")
    assert row.attribute()["high_is_good"] is False, "the mutaplasmid's override reached the card"
    assert row.attribute()["base"] == -60.0, "the Webifier II's base reached the card"
    assert [label.text() for label in row.range_labels] == ["-63%", "base -60%", "-63%"]
    assert row.track.base_fraction is None, "a zero-length track has no position for it"
    assert settle_count(view) == "1 of 2 match", "ESI has no rolls for the 404'd webifier"

    card.cancel_btn.click()
    assert not view._card_count._timer.isActive()


def test_the_footer_total_keeps_a_typed_roll_chip_and_the_dropdown_facets_it_away(
    abyssal_view, app
):
    """Done keeps a typed roll: chip, so the footer counts under it too --
    both figures -- while the type dropdown, which lists types to switch
    to, facets it away. The two readings of "how many webifiers" differ
    by design and the test pins both."""
    view = abyssal_view
    view.omnibox.set_spec(omni.parse(f'abyssal:"{WEB_NAME}" roll:web>=70'))
    card = open_card(view, app)
    assert picker(card)[1] == (WEB_NAME, "Stasis Webifier · 2", True)
    assert settle_count(view) == "1 of 1 match"
    card.cancel_btn.click()


def test_roll_columns_are_sized_from_the_text_they_paint_not_the_float_repr(abyssal_view):
    """The roll columns are sized when they appear, and the first pass sized
    them from cell_value -- "25.799999713897705", the export's raw number --
    so the CPU column opened at some 240 px for a cell reading "26 tf". The
    width must be the painted text's, header included, and nowhere near the
    repr's."""
    view = abyssal_view
    view.omnibox.add_chip("abyssal", BCS_NAME)
    metrics = view.tree.fontMetrics()
    header = view.tree.header()

    cpu = column_keys(view).index(gm.roll_key(50))
    assert cell(view, BCS_OK, gm.roll_key(50)).data(Qt.DisplayRole) == "26 tf"
    painted = max(metrics.horizontalAdvance("CPU"), metrics.horizontalAdvance("26 tf")) + 24
    assert header.sectionSize(cpu) == painted
    assert header.sectionSize(cpu) < metrics.horizontalAdvance(str(BCS_CPU_DISPLAY)) + 24
    roll = column_keys(view).index(gm.ROLL_MEAN_KEY)
    assert header.sectionSize(roll) == (
        max(metrics.horizontalAdvance("Roll"), metrics.horizontalAdvance("39%")) + 24
    )


def test_the_picker_facets_by_every_chip_but_the_cards_own_kinds(abyssal_view, app):
    """The card rewrites the abyssal and stat: chips on Done, and a roll:
    chip -- which Done leaves alone -- narrows by an attribute only some
    types roll, so the picker must not be narrowed by any of the three: with
    the webifier and a web-strength roll in force the table holds one item,
    yet the BCS -- which rolls no web strength and could never pass that
    chip -- is still listed with its full count, ready to be picked in the
    webifier's place. A negated card chip is
    dropped from the facet too (Done keeps it as it is), which is why the
    counts can overstate: -stat:cpu>0 empties the table of fetched items and
    the picker still says two of each."""
    view = abyssal_view
    view.omnibox.set_spec(omni.parse(f'abyssal:"{WEB_NAME}" roll:web>=70'))
    assert item_ids(view) == {WEB_OK}

    card = open_card(view, app)
    assert picker(card) == [
        (BCS_NAME, "Ballistic Control System · 2", False),
        (WEB_NAME, "Stasis Webifier · 2", True),
    ]
    card.cancel_btn.click()

    view.omnibox.set_spec(omni.parse(f'abyssal:"{WEB_NAME}" roll:web>=70 -stat:cpu>0'))
    assert item_ids(view) == set()
    card = open_card(view, app)
    assert [(name, text) for name, text, _selected in picker(card)] == [
        (BCS_NAME, "Ballistic Control System · 2"), (WEB_NAME, "Stasis Webifier · 2"),
    ]
    card.cancel_btn.click()

    # A chip of any other kind still facets: Amarr's items are gone.
    view.omnibox.set_spec(omni.parse(f'abyssal:"{WEB_NAME}" roll:web>=70 loc:"Jita IV - Moon 4"'))
    card = open_card(view, app)
    assert [(name, text) for name, text, _selected in picker(card)] == [
        (BCS_NAME, "Ballistic Control System · 1"), (WEB_NAME, "Stasis Webifier · 1"),
    ]
    card.cancel_btn.click()


def test_a_typed_roll_chip_rides_through_done_untouched(abyssal_view, app):
    """The card speaks display units only, so a roll: chip is nothing it
    can show or rebuild; Done must leave one where it was rather than
    swallow it with the chips it replaces. With the webifier's web roll in
    force and a CPU row added, Done writes the abyssal chip and the stat:
    chip after the untouched roll: chip, and the table is the AND of all
    three."""
    view = abyssal_view
    view.omnibox.set_spec(omni.parse(f'abyssal:"{WEB_NAME}" roll:web>=70'))
    assert item_ids(view) == {WEB_OK}

    card = open_card(view, app)
    assert card.rows() == [], "the roll: chip seeds no row"
    row = card.add_row(50)
    assert row.left_field.text().endswith(" tf")
    card.done_btn.click()

    chips = view.omnibox.spec().chips
    assert len(chips) == 3
    assert chips[0] == omni.Chip("roll", "web>=70")
    assert chips[1] == omni.Chip("abyssal", WEB_NAME)
    assert chips[2].kind == omni.STAT_KIND and chips[2].value.startswith("CPU usage=")
    assert item_ids(view) == {WEB_OK}


def test_typing_abyssal_and_pressing_enter_opens_the_card_under_the_new_chip(abyssal_view, app):
    """Enter on a typed ``abyssal`` is the natural way in, so the card
    opens without a glyph click -- one event turn later, seeded and faceted
    like a glyph open, with the filter already applied. Typing it again
    while the card is up mints no second chip and asks for no second card;
    Done restores the chips through set_spec, which must not reopen the
    card it just closed."""
    view = abyssal_view
    requests = record(view.omnibox.card_requested)
    # The previous test's view is still awaiting its deferred delete, and
    # the offscreen platform reports a destroyed window as the application
    # deactivating, which closes every popup -- so the leftovers are drained
    # first, as open_card does. On a desktop nothing is torn down between
    # the keystroke and the card.
    app.processEvents()

    commit_text(view, "abyssal")

    assert item_ids(view) == ABYSSAL_ROWS, "the filter applies in the commit's own turn"
    assert view._card is None, "the card waits for the turn that laid the chip out"
    app.processEvents()
    assert len(requests) == 1
    (chip, widget) = requests[0]
    assert chip == omni.Chip("abyssal", "") and widget is view.omnibox._chips[0][1]
    card = view._card
    assert card is not None and card.isVisible()
    assert card.type_combo.currentIndex() == -1, "the bare chip picks no type"
    assert [name for name, _text, _selected in picker(card)] == [BCS_NAME, WEB_NAME]

    commit_text(view, "abyssal")
    app.processEvents()
    assert len(requests) == 1 and view._card is card and card.isVisible()
    assert view.omnibox.spec().chips == [omni.Chip("abyssal", "")]

    pick(card, WEB_NAME)
    card.done_btn.click()
    app.processEvents()
    assert view.omnibox.spec().chips == [omni.Chip("abyssal", WEB_NAME)]
    assert item_ids(view) == {WEB_OK, WEB_MISSING}
    assert not card.isVisible() and len(requests) == 1

    # The alias spelling is the same chip and opens the same way.
    view.omnibox.clear()
    commit_text(view, "is:abyssal")
    app.processEvents()
    assert len(requests) == 2 and view._card.isVisible()
    view._card.cancel_btn.click()


def test_an_abyssal_chip_that_arrives_any_way_but_typing_opens_no_card(abyssal_view, app):
    """A saved view's set_spec, a rail row's add_chip and a negated chip --
    which the card cannot express -- place the chip and nothing more; the
    glyph is the way to open the card for those."""
    view = abyssal_view
    requests = record(view.omnibox.card_requested)
    app.processEvents()

    view.omnibox.add_chip("abyssal", "")
    app.processEvents()
    view.omnibox.set_spec(omni.parse(f'abyssal:"{WEB_NAME}" roll:web>=70'))
    app.processEvents()
    view.omnibox.clear()
    commit_text(view, "-abyssal")
    app.processEvents()

    assert view.omnibox.spec().chips == [omni.Chip("abyssal", "", negated=True)]
    assert item_ids(view) == ORDINARY_ROWS
    assert requests == [] and view._card is None


