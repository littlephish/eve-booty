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

The ship-scoped section after it seeds tests/fit_corpus.py -- eleven ships
and three stored fits -- and drives the `holds:` and `fit:` chips, their two
cards, the temporary count column and the inspector's fit diff through the
same wiring, with every count read off the corpus by hand.
"""

from __future__ import annotations

import csv
import re

import fit_corpus
import pytest
from conftest import BCS_BODY, BCS_MUTATOR, BCS_SOURCE, BCS_TYPE, FakeESIClient, match_text

from evasset import abyssal, db, fits, omni, queries, sde, views
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
from evasset.ui.fit_card import FitCard  # noqa: E402
from evasset.ui.holds_card import HoldsCard  # noqa: E402
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
        v._fit_query,
        v._window_fit_query,
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
def library(view: AssetsView) -> list[str]:
    """The names in the view library, in the order the Load card lists them."""
    return [v.name for v in views.list_views(view.conn)]



def save_to_slot(view: AssetsView, slot: int) -> views.View:
    """Put the tab's current posture into the library under the slot's own
    name, the way the retired Ctrl+digit binding used to; tests that need a
    slotted view build it through the store now."""
    with db.transaction(view.conn):
        stored, _created = views.save_to_slot(view.conn, slot, view._current_state())
    return stored

def open_save_card(view: AssetsView, app):
    """Open the Save card through its pill and hand it back.

    The pending events are drained first for card_after_enter's reason: the
    offscreen platform reports the previous test's destroyed window as the
    application deactivating, which closes every popup.
    """
    app.processEvents()
    view.save_btn.click()
    app.processEvents()
    card = view._cards.get(omni.SAVE_COMMAND)
    assert card is not None and card.isVisible(), "the Save pill must open the Save card"
    return card


def open_load_card(view: AssetsView, app):
    """Open the Load card through its pill and hand it back."""
    app.processEvents()
    view.load_btn.click()
    app.processEvents()
    card = view._cards.get(omni.LOAD_COMMAND)
    assert card is not None and card.isVisible(), "the Load pill must open the Load card"
    return card


def load_rows(card) -> list:
    """The row widgets of the Load card's list, top to bottom."""
    return card.rows()


def row_named(card, name: str):
    rows = load_rows(card)
    match = next((r for r in rows if r.name_label.text() == name), None)
    assert match is not None, f"{name!r} is not in {[r.name_label.text() for r in rows]}"
    return match


def select_row(card, name: str) -> None:
    card.view_list.setCurrentRow(load_rows(card).index(row_named(card, name)))


def pill_texts(pills) -> list[str]:
    return [p.prefix_label.text() + p.value_label.text() for p in pills]


def test_saved_views_round_trip_filter_group_by_rail_level_and_rail_sort(view):
    """A saved view is the whole working posture -- filter, grouping, rail
    level, rail sort -- and recalling one must restore all four, not only
    the chips.

    A slotted view lives in the named library now, so the digit is a handle
    on a view called "Slot 2" rather than an anonymous row of its own; the
    four things live in four columns where a JSON blob used to hold them."""
    view.omnibox.add_chip("owner", "Main")
    set_group(view, "group")
    view.rail.level.setCurrentIndex(LEVEL_KEYS.index("owner"))
    view.rail.sort_buttons["volume"].click()
    save_to_slot(view, 2)

    stored = views.view_in_slot(view.conn, 2)
    assert stored is not None and stored.name == "Slot 2"
    assert stored.state == views.ViewState("owner:Main", "group", "owner", "volume")

    view.omnibox.clear()
    set_group(view, "")
    view.rail.level.setCurrentIndex(LEVEL_KEYS.index("location"))
    view.rail.sort_buttons["value"].click()

    view._recall_view(2)

    assert view.omnibox.spec().chips == [omni.Chip("owner", "Main")]
    assert view._current_group_key() == "group"
    assert view.rail.current_level() == "owner"
    assert view.rail.current_sort() == "volume"
    assert len(view.model.rows()) == 3  # the recalled filter really applied


def test_recalling_an_empty_slot_changes_nothing(view):
    view.omnibox.add_chip("owner", "Main")

    view._recall_view(7)

    assert view.omnibox.spec().chips == [omni.Chip("owner", "Main")]
    assert "No saved view" in view.footer.text()


def test_ctrl_digit_loads_the_view_in_that_slot_from_anywhere_on_the_tab(view, app):
    """Ctrl+digit is read-only now: it used to save the current view into the
    slot, so a slip of the hand overwrote a view. It loads instead, from the
    table and from inside the omnibox alike, and an empty digit only reports."""
    with db.transaction(view.conn):
        stored, _ = views.save_view(view.conn, "Main kit", views.ViewState("owner:Main"))
        views.set_slot(view.conn, stored.view_id, 3)
    view.show()
    app.processEvents()
    view.tree.setFocus()
    QTest.keyClick(view.tree, Qt.Key_3, Qt.ControlModifier)
    assert view.omnibox.spec().chips == [omni.Chip("owner", "Main")]
    assert view.footer.text() == "Loaded view 'Main kit' from slot 3."

    view.omnibox.clear()
    view.omnibox.edit.setFocus()
    QTest.keyClick(view.omnibox.edit, Qt.Key_3, Qt.ControlModifier)
    assert view.omnibox.spec().chips == [omni.Chip("owner", "Main")], "works from the omnibox too"

    QTest.keyClick(view.tree, Qt.Key_7, Qt.ControlModifier)
    assert view.omnibox.spec().chips == [omni.Chip("owner", "Main")], "an empty digit loads nothing"
    assert "No saved view in slot 7" in view.footer.text()


def test_ctrl_digit_inside_the_open_load_card_loads_through_the_card_and_closes_it(view, app):
    """Qt's WidgetWithChildrenShortcut check walks up through a Qt.Popup to
    its parent, so with the Load card up the tab's Ctrl+digit binding won
    the key: the view loaded and the card stayed open over it showing a
    diff against the filter that was. The card claims the key instead, and
    the footer tells the two paths apart -- the card's load names no slot."""
    with db.transaction(view.conn):
        stored, _ = views.save_view(view.conn, "Main kit", views.ViewState("owner:Main"))
        views.set_slot(view.conn, stored.view_id, 3)
    view.show()
    app.processEvents()
    card = open_load_card(view, app)
    assert card.hasFocus()

    QTest.keyClick(card, Qt.Key_3, Qt.ControlModifier)

    assert view.omnibox.spec().chips == [omni.Chip("owner", "Main")]
    assert not card.isVisible(), "the card's own handler loaded and closed"
    assert view.footer.text() == "Loaded view 'Main kit'."


def test_saving_over_a_name_replaces_that_view_and_keeps_its_id_and_its_slot(view):
    """Re-saving a refined view is the common act, so a name collision
    replaces rather than prompting -- but only the state: the digit the view
    sits on and its identity in the Load card's list have to survive, or
    every refinement would knock the view off its own shortcut."""
    commit_text(view, "owner:Main save:Jita")
    first = views.find_view(view.conn, "Jita")
    with db.transaction(view.conn):
        views.set_slot(view.conn, first.view_id, 4)

    view.omnibox.clear()
    commit_text(view, "owner:Alt save:jita")

    again = views.find_view(view.conn, "Jita")
    assert again.view_id == first.view_id and again.slot == 4
    assert again.name == "Jita", "the stored spelling outlives a lower-case save"
    assert again.state.filter == "owner:Alt"
    assert library(view) == ["Jita"], "a case-folded name is the same view, not a second"
    assert view.footer.text() == "Saved view 'Jita' (replaced)."


def test_the_load_command_restores_the_filter_the_grouping_and_the_rail(view):
    """The whole posture comes back from a name, and it replaces what was
    there rather than adding to it."""
    view.omnibox.add_chip("owner", "Main")
    set_group(view, "location")
    view.rail.level.setCurrentIndex(LEVEL_KEYS.index("owner"))
    commit_text(view, 'save:"Jita ships"')

    commit_text(view, "clear")
    view.omnibox.clear()
    set_group(view, "")
    view.rail.level.setCurrentIndex(LEVEL_KEYS.index("location"))
    view.omnibox.add_chip("cat", "Module")

    commit_text(view, 'load:"Jita ships"')

    assert view.omnibox.spec().chips == [omni.Chip("owner", "Main")], "a load replaces"
    assert view._current_group_key() == "location"
    assert view.rail.current_level() == "owner"
    assert len(view.model.rows()) == 3
    assert view.footer.text() == "Loaded view 'Jita ships'."


def test_an_unknown_load_name_reports_it_and_still_consumes_the_token(view):
    """A typo must not leave a command in the field: it would fire again on
    the next Enter, and the second firing would look like a bug in the
    filter the user was actually typing."""
    view.omnibox.add_chip("owner", "Main")

    commit_text(view, "load:Nope")

    assert view.footer.text() == "No saved view named 'Nope'."
    assert view.omnibox.edit.text() == ""
    assert view.omnibox.spec().chips == [omni.Chip("owner", "Main")], "nothing was loaded"


def test_a_quoted_command_is_a_search_and_only_the_first_command_of_a_line_runs(view):
    """The quotes are the escape hatch -- somebody looking for the literal
    text "save:x" must not write to the library -- and a line with two
    commands is a typo far more often than a batch API."""
    commit_text(view, "save:One save:Two")

    assert library(view) == ["One"], "the second command was consumed, not run"

    commit_text(view, '"save:Three"')

    assert library(view) == ["One"], "a quoted command is text"
    assert view.omnibox.spec().chips == []


# ---------------------------------------------------------------- the two cards
def test_a_bare_save_command_opens_the_card_with_the_derived_name_selected(view, app):
    """Enter on `save:` with no name is the empty-request branch: it opens
    the card rather than saving a nameless view, and the derived label is
    pre-selected so Enter alone is a whole save and the first keystroke
    replaces it."""
    app.processEvents()
    view.omnibox.add_chip("owner", "Main")
    commit_text(view, "save:")
    card = view._cards.get(omni.SAVE_COMMAND)
    assert card is not None and card.isVisible()
    assert card.name_edit.text() == "owner Main"
    assert card.name_edit.hasSelectedText()
    assert pill_texts(card.pills()) == ["owner:Main"]
    assert card.status_label.text() == "0 saved views"

    saves = record(card.save_requested)
    QTest.keyClick(card.name_edit, Qt.Key_Return)

    assert saves == [("owner Main", "owner:Main", None, "", "location", "value")], (
        "Enter in the name field saves once, with the tab's own posture"
    )
    assert not card.isVisible()
    stored = views.find_view(view.conn, "owner Main")
    assert stored.state == views.ViewState("owner:Main", "", "location", "value")
    assert stored.slot is None
    assert view.footer.text() == "Saved view 'owner Main'."


def test_the_save_card_stores_the_trimmed_filter_on_the_chosen_key_and_leaves_the_omnibox(
    view, app
):
    """The card's crosses and key cell are the two things the typed command
    cannot say. What is stored is the well's line, not the field's; the
    digit is assigned in the same transaction and comes off the view that
    held it; and the omnibox itself keeps every chip, since the card was
    only ever describing the view to save."""
    with db.transaction(view.conn):
        alt, _ = views.save_view(view.conn, "Alt kit", views.ViewState("owner:Alt"))
        views.set_slot(view.conn, alt.view_id, 4)
    view.omnibox.add_chip("owner", "Main")
    view.omnibox.add_chip("category", "Module")
    set_group(view, "group")

    card = open_save_card(view, app)
    assert pill_texts(card.pills()) == ["owner:Main", "cat:Module"]
    assert card.status_label.text() == "1 saved view"
    card.pills()[1].close_btn.click()
    assert pill_texts(card.pills()) == ["owner:Main"]
    card.key_cell.pick(4)
    assert card.conflict_label.text() == "4 is currently “Alt kit” — saving moves it to this view"
    card.name_edit.setText("Main kit")
    card.done_btn.click()

    stored = views.find_view(view.conn, "Main kit")
    assert stored.state == views.ViewState("owner:Main", "group", "location", "value")
    assert stored.slot == 4
    assert views.find_view(view.conn, "Alt kit").slot is None, "the digit moved"
    assert view.footer.text() == "Saved view 'Main kit' to slot 4."
    assert view.omnibox.spec().chips == [
        omni.Chip("owner", "Main"), omni.Chip("category", "Module"),
    ], "the card never edits the live filter"

    card = open_save_card(view, app)
    assert pill_texts(card.pills()) == ["owner:Main", "cat:Module"], "a reopen starts afresh"
    assert card.slot() is None
    card.cancel_btn.click()


def test_the_save_cards_posture_controls_store_a_posture_the_tab_is_not_in(view, app):
    """The controls open on the tab's posture and edit only what the save
    stores, the crosses' rule for the other three columns: the tab keeps
    its flat table, location rail and ISK order while the view is saved
    grouped by owner with the rail on region by volume -- and a load then
    moves all three, which is the proof the columns travelled."""
    view.omnibox.add_chip("owner", "Main")

    card = open_save_card(view, app)
    assert (card.group_by(), card.rail_level(), card.rail_sort()) == ("", "location", "value")
    assert [card.group_combo.itemText(i) for i in range(card.group_combo.count())] == [
        view.group_combo.itemText(i) for i in range(view.group_combo.count())
    ], "the tab's own groupings"
    card.group_combo.setCurrentIndex(card.group_combo.findData("owner"))
    card.rail_bar.level.setCurrentIndex(LEVEL_KEYS.index("region"))
    card.rail_bar.sort_buttons["volume"].click()
    card.name_edit.setText("By owner")
    card.done_btn.click()

    stored = views.find_view(view.conn, "By owner")
    assert stored.state == views.ViewState("owner:Main", "owner", "region", "volume")
    assert view._current_group_key() is None, "the tab's own grouping is untouched"
    assert view.rail.current_level() == "location" and view.rail.current_sort() == "value"

    commit_text(view, "clear")
    view.omnibox.clear()
    assert view.omnibox.spec().chips == []
    commit_text(view, 'load:"By owner"')

    assert view.omnibox.spec().chips == [omni.Chip("owner", "Main")]
    assert view._current_group_key() == "owner"
    assert view.rail.current_level() == "region"
    assert view.rail.current_sort() == "volume"
    assert view.rail.sort_buttons["volume"].isChecked()
    assert len(view.model.rows()) == 3


def test_the_typed_save_command_keeps_saving_the_whole_current_posture(view):
    """`save:Name` has no card and no controls, so what it stores is the tab
    as it stands -- the rail's sort included now that a view has a column
    for it."""
    view.omnibox.add_chip("owner", "Alt")
    set_group(view, "location")
    view.rail.level.setCurrentIndex(LEVEL_KEYS.index("owner"))
    view.rail.sort_buttons["name"].click()

    commit_text(view, "save:Alphabetical")

    stored = views.find_view(view.conn, "Alphabetical")
    assert stored.state == views.ViewState("owner:Alt", "location", "owner", "name")


def test_a_view_with_no_recorded_sort_leaves_the_rails_sort_alone(view):
    """A view saved before v8, or imported from a shared line, carries ''
    for its sort; loading it must not snap the rail back to ISK any more
    than an empty rail level moves the level."""
    with db.transaction(view.conn):
        views.save_view(view.conn, "Old", views.ViewState("owner:Main", "group", "owner", ""))
        views.import_text(view.conn, "owner:Alt", "Shared")
    view.rail.sort_buttons["volume"].click()
    view.rail.level.setCurrentIndex(LEVEL_KEYS.index("region"))

    commit_text(view, "load:Old")
    assert view.rail.current_level() == "owner", "a recorded level moves the rail"
    assert view.rail.current_sort() == "volume", "an unrecorded sort does not"
    assert view.rail.sort_buttons["volume"].isChecked()

    commit_text(view, "load:Shared")
    assert view.rail.current_level() == "owner" and view.rail.current_sort() == "volume"
    assert view.omnibox.spec().chips == [omni.Chip("owner", "Alt")]


def test_escape_from_the_save_card_writes_nothing(view, app):
    """Esc is Cancel everywhere else in the tab, and a card that saved on the
    way out would make Esc the one exception nobody expects."""
    card = open_save_card(view, app)
    card.name_edit.setText("Never stored")

    QTest.keyClick(card, Qt.Key_Escape)

    assert not card.isVisible()
    assert library(view) == []


def test_ctrl_s_and_ctrl_l_are_bound_once_and_open_their_cards_from_the_omnibox(
    view, app
):
    """Both sequences must reach the cards from the field people type in, and
    exactly one binding may exist anywhere: two with a child-covering context
    made Ctrl+F ambiguous and Qt fired neither (the defect above)."""
    from PySide6.QtGui import QKeySequence, QShortcut

    for sequence in ("Ctrl+S", "Ctrl+L"):
        bound = [s for s in view.findChildren(QShortcut) if s.key() == QKeySequence(sequence)]
        assert len(bound) == 1, f"{sequence} is bound {len(bound)} times; two is a no-op"

    view.show()
    app.processEvents()
    view.omnibox.edit.setFocus()
    app.processEvents()
    QTest.keyClick(view.omnibox.edit, Qt.Key_S, Qt.ControlModifier)
    app.processEvents()
    save_card = view._cards.get(omni.SAVE_COMMAND)
    assert save_card is not None and save_card.isVisible()

    QTest.keyClick(save_card, Qt.Key_Escape)
    app.processEvents()
    QTest.keyClick(view.omnibox.edit, Qt.Key_L, Qt.ControlModifier)
    app.processEvents()
    load_card = view._cards.get(omni.LOAD_COMMAND)
    assert load_card is not None and load_card.isVisible()
    assert not save_card.isVisible(), "one command card at a time"


def test_the_load_card_lists_the_library_and_says_what_a_load_would_replace(view, app):
    """A load throws the working filter away, so the pane diffs the picked
    view against it -- and Load view stays disabled until a row is picked,
    because a list that took focus would make row 0 current and that row
    is not a choice."""
    with db.transaction(view.conn):
        jita, _ = views.save_view(
            view.conn, "Jita ships", views.ViewState("owner:Main", "location", "owner")
        )
        views.set_slot(view.conn, jita.view_id, 1)
        views.save_view(view.conn, "Alt kit", views.ViewState("owner:Alt"))
    view.omnibox.add_chip("cat", "Ship")

    card = open_load_card(view, app)

    assert [r.name_label.text() for r in load_rows(card)] == ["Jita ships", "Alt kit"]
    badges = [r.key_cell.label() for r in load_rows(card)]
    assert badges == ["1", "–"], "the slotted view leads the list and wears its digit"
    assert card.selected_view() is None and not card.done_btn.isEnabled()
    assert card.pane.currentIndex() == 0, "nothing to preview yet"
    assert card.first_focus() is card, "the list is never focused"

    select_row(card, "Jita ships")
    assert pill_texts(card.preview_pills()) == ["owner:Main"]
    assert [(p.sign_label.text(), p.prefix_label.text() + p.value_label.text())
            for p in card.diff_pills()] == [("-", "cat:Ship"), ("+", "owner:Main")]
    assert card.hint_label.text() == "Press Ctrl+1 to load"
    assert card.done_btn.isEnabled()


def test_done_loads_the_selected_view_and_cancel_leaves_the_filter_alone(view, app):
    """Done is the card's only writing exit; every other way out changes
    nothing, which is what makes the list safe to browse with a filter up."""
    with db.transaction(view.conn):
        views.save_view(view.conn, "Alt kit", views.ViewState("owner:Alt", "group", ""))
    view.omnibox.add_chip("owner", "Main")

    card = open_load_card(view, app)
    select_row(card, "Alt kit")
    card.cancel_btn.click()

    assert view.omnibox.spec().chips == [omni.Chip("owner", "Main")], "Cancel loaded nothing"

    card = open_load_card(view, app)
    select_row(card, "Alt kit")
    assert card.done_btn.isEnabled()
    card.done_btn.click()

    assert view.omnibox.spec().chips == [omni.Chip("owner", "Alt")]
    assert view._current_group_key() == "group"
    assert items(view) == ["Damage Control II"], "the loaded filter really applied"
    assert view.footer.text() == "Loaded view 'Alt kit'."


def test_the_slot_menu_moves_a_digit_and_the_digit_key_recalls_its_new_holder(view, app):
    """A digit holds one view, so assigning it takes it off whoever had it --
    and the proof that the move is real is the key itself, not the badge."""
    with db.transaction(view.conn):
        main, _ = views.save_view(view.conn, "Main kit", views.ViewState("owner:Main"))
        views.set_slot(view.conn, main.view_id, 2)
        views.save_view(view.conn, "Alt kit", views.ViewState("owner:Alt"))

    card = open_load_card(view, app)
    cell = row_named(card, "Alt kit").key_cell
    assert cell.label() == "–"
    cell.open_menu()
    assert cell.menu.buttons[2].toolTip() == "Take from “Main kit”"
    cell.menu.buttons[2].click()

    assert view.footer.text() == "Slot 2: 'Alt kit'."
    holder = views.view_in_slot(view.conn, 2)
    assert holder is not None and holder.name == "Alt kit"
    assert [(r.name_label.text(), r.key_cell.label()) for r in load_rows(card)] == [
        ("Alt kit", "2"),
        ("Main kit", "–"),
    ]
    select_row(card, "Alt kit")
    assert card.keycap.text() == "2"

    card.cancel_btn.click()
    view.show()
    app.processEvents()
    view.tree.setFocus()
    app.processEvents()
    QTest.keyClick(view.tree, Qt.Key_2, Qt.ControlModifier)

    assert view.omnibox.spec().chips == [omni.Chip("owner", "Alt")]
    assert view.footer.text() == "Loaded view 'Alt kit' from slot 2."


def test_renaming_a_view_to_a_name_already_taken_reverts_the_row_and_says_so(view, app):
    """The library keys on the name, so two views cannot share one -- and the
    row has to go back to the stored name by itself, or the card would show a
    name the database does not have."""
    with db.transaction(view.conn):
        views.save_view(view.conn, "Main kit", views.ViewState("owner:Main"))
        views.save_view(view.conn, "Alt kit", views.ViewState("owner:Alt"))

    card = open_load_card(view, app)
    select_row(card, "Main kit")
    card.rename_btn.click()
    card.name_edit.setText("Alt kit")
    QTest.keyClick(card.name_edit, Qt.Key_Return)

    assert view.footer.text() == "A view named 'Alt kit' already exists."
    assert sorted(library(view)) == ["Alt kit", "Main kit"]
    assert sorted(r.name_label.text() for r in load_rows(card)) == ["Alt kit", "Main kit"]
    assert card.name_label.text() == "Main kit", "the pane shows the stored name again"

    card.begin_rename()
    card.name_edit.setText("Home fleet")
    QTest.keyClick(card.name_edit, Qt.Key_Return)

    assert view.footer.text() == "Renamed to 'Home fleet'."
    assert sorted(library(view)) == ["Alt kit", "Home fleet"]
    assert card.name_label.text() == "Home fleet"
    assert card.selected_view().name == "Home fleet", "the selection survives the re-list"
    assert view.omnibox.spec().chips == [], "a rename is not a load"


def test_the_cross_forgets_a_view_and_frees_the_digit_it_held(view, app):
    """A slot is a column of the row, so forgetting the view is the only way
    to free the digit -- and the digit has to report empty afterwards rather
    than recalling a view that is gone."""
    with db.transaction(view.conn):
        kit, _ = views.save_view(view.conn, "Main kit", views.ViewState("owner:Main"))
        views.set_slot(view.conn, kit.view_id, 5)

    card = open_load_card(view, app)
    select_row(card, "Main kit")
    card.delete_btn.click()

    assert view.footer.text() == "Forgot view 'Main kit'."
    assert library(view) == [] and load_rows(card) == []
    assert card.selected_view() is None and card.pane.currentIndex() == 0
    assert views.view_in_slot(view.conn, 5) is None

    view._recall_view(5)
    assert view.footer.text() == "No saved view in slot 5."


def test_the_paste_box_imports_a_shared_line_as_a_view_and_selects_it(view, app):
    """The other end of Copy as text: a fleetmate's one line becomes a view,
    named from the line itself and selected so Done loads what was just
    added. Enter in the paste box adds -- it must never reach the card as
    Done and load whatever row happened to be selected."""
    with db.transaction(view.conn):
        views.save_view(view.conn, "Main kit", views.ViewState("owner:Main"))

    card = open_load_card(view, app)
    select_row(card, "Main kit")
    card.paste_edit.setText("cat:Ship -owner:Alt")
    card.paste_edit.textEdited.emit("cat:Ship -owner:Alt")
    assert card.import_name_edit.placeholderText() == "cat Ship · -owner Alt"
    assert card.import_name_edit.text() == "", "the name is optional"

    QTest.keyClick(card.paste_edit, Qt.Key_Return)

    imported = views.find_view(view.conn, "cat Ship · -owner Alt")
    assert imported is not None and imported.state.filter == "cat:Ship -owner:Alt"
    assert imported.slot is None, "an import is unslotted"
    assert view.footer.text() == "Imported view 'cat Ship · -owner Alt'."
    assert card.selected_view().view_id == imported.view_id
    assert view.omnibox.spec().chips == [], "Enter in the paste box is not Done"
    assert card.isVisible(), "and the card stays up to be used"


def test_both_pills_stay_visible_while_clear_all_follows_the_filter(view, app):
    """An unfiltered table is a view worth loading one into, so neither pill
    hides -- while Clear all keeps its own rule, since there is nothing to
    clear until a chip exists."""
    view.show()
    app.processEvents()

    assert view.save_btn.isVisible() and view.load_btn.isVisible()
    assert not view.clear_all_btn.isVisible()

    view.omnibox.add_chip("owner", "Main")

    assert view.save_btn.isVisible() and view.load_btn.isVisible()
    assert view.clear_all_btn.isVisible()

    view.clear_all_btn.click()

    assert view.save_btn.isVisible() and view.load_btn.isVisible()
    assert not view.clear_all_btn.isVisible()


def test_a_saved_filter_naming_an_unknown_chip_kind_recalls_as_bare_text(view, app):
    """A view can outlive the build that wrote it -- an older release's
    filter, or a line pasted from a newer one. An unknown prefix degrades to
    the search it looks like rather than throwing, and nothing opens a card
    for a kind this build has no card for."""
    app.processEvents()
    with db.transaction(view.conn):
        stale, _ = views.save_view(
            view.conn, "From the future", views.ViewState("wormhole:C5 owner:Main")
        )
        views.set_slot(view.conn, stale.view_id, 6)

    view._recall_view(6)
    app.processEvents()

    assert view.omnibox.spec() == omni.FilterSpec(
        text="wormhole:C5", chips=[omni.Chip("owner", "Main")]
    )
    # Not merely "no card is up": a recall builds no card at all, so an
    # unknown prefix cannot reach _card_for and be answered with the wrong
    # one either.
    assert view._cards == {}
    assert view.footer.text() == "Loaded view 'From the future' from slot 6."
    assert view.state_label.text().endswith("2 filters"), "the text counts as a filter"


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

    The pending events are drained first so the glyph is clicked on a chip
    the layout has placed -- which is also what happens on a desktop between
    a keystroke and a click.
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

    save_to_slot(view, 4)

    stored = views.view_in_slot(view.conn, 4)
    assert stored.name == "Slot 4"
    assert stored.state.filter == f'abyssal:"{BCS_NAME}" roll:cpu=40..60'
    assert omni.parse(stored.state.filter) == spec

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



# --------------------------------------------------- ship holds and stored fits
# The synthetic estate of tests/fit_corpus.py, driven through the same wiring:
# eleven ships (ten assembled, one packaged stack), three stored fits, and one
# consumable spread over every counting case. Every figure below is read off
# that fixture by hand.
#
# Antimatter Charge M, per assembled ship, whole ship then cargo alone:
#   SHIP_EXACT      100 cargo                                    -> 100 / 100
#   SHIP_PERMUTED   300 cargo + 200 ammo hold + 100 fuel bay
#                   + 50 loaded in a launcher; 1,000 more sit
#                     in a crate in the cargo hold, uncounted    -> 650 / 300
#   SHIP_NO_FIT      40 cargo                                    ->  40 /  40
#   every other assembled ship                                   ->   0 /   0
# The 5,000 loose in the hangar belong to no ship and count nowhere.
AMMO_NAME, PASTE_NAME = fit_corpus.AMMO_NAME, fit_corpus.PASTE_NAME
DRONE_NAME, FIGHTER_NAME = fit_corpus.DRONE_NAME, fit_corpus.FIGHTER_NAME
FUEL_NAME = fit_corpus.FUEL_BLOCK_NAME
# 77 asset rows, less everything sitting in a module slot -- the racks and
# the charges loaded into them -- which the everyday view hides
# (omni.hides_fitted).
CORPUS_STACKS = 26
AMMO_ABOARD = {
    fit_corpus.SHIP_EXACT: 100,
    fit_corpus.SHIP_PERMUTED: 650,
    fit_corpus.SHIP_NO_FIT: 40,
}
# Under 500 rounds aboard: every assembled ship but the loaded Dominix.
SHORT_OF_AMMO = fit_corpus.ASSEMBLED_SHIPS - {fit_corpus.SHIP_PERMUTED}

# The Ratting fit is 7 modules; these match it, and these Dominixes deviate.
RATTING_MATCHES = {fit_corpus.SHIP_EXACT, fit_corpus.SHIP_PERMUTED}
RATTING_DEVIATES = fit_corpus.DOMINIXES - RATTING_MATCHES
# is:fit reaches every hull that has a fit: the two Ratting Dominixes, the
# Dominix matching the Solo fit, and the Charon matching Hauling.
MATCHES_ANY_FIT = RATTING_MATCHES | {fit_corpus.SHIP_SOLO, fit_corpus.SHIP_CHARON}
# -is:fit stays inside the hulls that have one, so the Solstice is in neither.
DEVIATES_FROM_ALL = fit_corpus.DOMINIXES - MATCHES_ANY_FIT

# A fit for the hull nobody had written one for: exactly the Solstice's rack,
# so storing it moves that ship into is:fit.
SKIRMISH_EFT = """[Solstice, Skirmish]

Focused Pulse Laser

Defensive Core Matrix
"""
UNKNOWN_EFT = """[Solstice, Broken]

Quantum Whatsit
"""


@pytest.fixture
def fit_conn(tmp_path):
    """A database of its own for the fit corpus: the four-stack estate above
    shares the Dominix type, and its unfitted hull would read as an eleventh
    assembled ship in every count below."""
    conn = db.init(tmp_path / "fits.sqlite")
    fit_corpus.install(conn)
    return conn


@pytest.fixture
def fit_view(app, fit_conn):
    yield from _wired_view(fit_conn)


def holds_key(value: str) -> str:
    """The model key of the count column a holds chip value grows."""
    term = omni.parse_holds(value)
    assert term is not None, f"{value!r} does not parse as a holds term"
    return omni.holds_column_key(term)


def open_kind_card(view: AssetsView, app, kind: str):
    """Click the glyph on the first chip of one kind and hand back its card.

    The pending events are drained first for open_card's reason: the glyph
    belongs to a chip the layout has placed.
    """
    app.processEvents()
    widget = next(w for c, w in view.omnibox._chips if c.kind == kind)
    widget.card_btn.click()
    card = view._cards.get(kind)
    assert card is not None and card.isVisible(), f"the {kind} glyph must open its card"
    return card


def open_new_card(view: AssetsView, app, kind: str):
    """Open a card with no chip behind it, the way the Ctrl+F builder does."""
    app.processEvents()
    view.omnibox.open_draft()
    view.omnibox._draft._enter_value_stage(kind, False)
    card = view._cards.get(kind)
    assert card is not None and card.isVisible(), f"the builder must open the {kind} card"
    return card


def card_after_enter(view: AssetsView, app, text: str, kind: str):
    """Commit omnibox text and let the deferred card request run.

    The leftovers of an earlier test's view are drained first: the offscreen
    platform reports a destroyed window as the application deactivating,
    which closes every popup (the abyssal Enter test's lesson).
    """
    app.processEvents()
    commit_text(view, text)
    app.processEvents()
    card = view._cards.get(kind)
    assert card is not None and card.isVisible(), f"Enter on {text!r} must open the {kind} card"
    return card


def settle_ship_count(view: AssetsView) -> str:
    """Fire the open card's debounced count now and read its footer."""
    view._card_count.flush()
    assert view._active_card is not None
    return match_text(view._active_card)


def holds_picker(card: HoldsCard) -> list[tuple[str, str]]:
    """(name, tooltip) per entry of the consumable dropdown; the rendered
    text is the name itself."""
    combo = card.type_combo
    for i in range(combo.count()):
        assert combo.itemText(i) == combo.itemData(i), "an entry shows its bare name"
    return [(combo.itemData(i), combo.itemData(i, Qt.ToolTipRole)) for i in range(combo.count())]


def fit_rows(card: FitCard) -> list[str]:
    """The rendered text of each stored-fit line."""
    return [
        card.fit_list.itemWidget(card.fit_list.item(i)).findChild(QtWidgets.QLabel).text()
        for i in range(card.fit_list.count())
    ]


def delete_fit_row(card: FitCard, name: str) -> None:
    """Click the × on the stored-fit line with this name."""
    for index in range(card.fit_list.count()):
        widget = card.fit_list.itemWidget(card.fit_list.item(index))
        if widget.findChild(QtWidgets.QLabel).text().startswith(f"{name} ·"):
            widget.findChild(QtWidgets.QToolButton).click()
            return
    raise AssertionError(f"no stored fit named {name!r} in the card")


def select_fit_row(card: FitCard, name: str) -> None:
    for index in range(card.fit_list.count()):
        if card.fit_list.item(index).data(Qt.UserRole + 1)["name"] == name:
            card.fit_list.setCurrentRow(index)
            return
    raise AssertionError(f"no stored fit named {name!r} in the card")


def paste_fit(card: FitCard, text: str) -> None:
    """Type an EFT block into the paste box and let the debounced parse run."""
    card.paste_edit.setPlainText(text)
    card._parse_debounce.flush()


def fit_lines(inspector) -> list[str]:
    """The visible lines of the inspector's fit block, verdict first.

    The block itself must be showing: a hidden box whose labels still carry
    the last ship's verdict would otherwise read as a pass.
    """
    assert not inspector.fit_box.isHidden(), "the fit block is hidden"
    return [inspector.fit_verdict.text()] + [
        label.text()
        for label in (inspector.fit_missing, inspector.fit_extra, inspector.fit_short)
        if not label.isHidden()
    ]


def capture_fit_lookups(view: AssetsView) -> dict:
    """Hold both hosts' fit-diff lookups so a test delivers each when it
    chooses -- capture_rolls_lookups for the other pair of per-host streams."""
    pending: dict[str, tuple] = {}

    def hold(name):
        return lambda fn, on_done, on_failed=None: pending.__setitem__(name, (fn, on_done))

    view._fit_query.run = hold("panel")
    view._window_fit_query.run = hold("window")
    return pending


def test_a_holds_chip_narrows_to_ships_and_grows_a_count_column_after_qty(fit_view):
    """The whole point of the chip: the ships short of ammunition, and the
    number saying how short. The column lands after Qty with every other
    column keeping its order, and its cells are the hand-computed totals --
    the loaded rounds and the specialised holds included, the crate's
    thousand and the hangar's five thousand not. Nothing that is not an
    assembled ship survives either polarity of the chip, so the packaged
    Dominix stack and every module on every rack are gone."""
    view = fit_view
    commit_text(view, f'holds:"{AMMO_NAME}"<500')

    assert item_ids(view) == SHORT_OF_AMMO
    assert fit_corpus.SHIP_PACKAGED not in item_ids(view), "a packaged stack has no holds"
    assert view.state_label.text() == f"9 of {CORPUS_STACKS} stacks · 1 filter"

    key = holds_key(f"{AMMO_NAME}<500")
    keys = column_keys(view)
    base_keys = [k for k, _header in queries.ASSET_COLUMNS]
    qty = base_keys.index("quantity")
    assert keys[: qty + 1] == base_keys[: qty + 1]
    assert keys[qty + 1] == key
    assert keys[qty + 2 :] == base_keys[qty + 1 :]
    assert view.model.columns()[qty + 1] == (key, AMMO_NAME)
    assert view.model.columnCount() == len(queries.ASSET_COLUMNS) + 1

    seen = 0
    for ship in SHORT_OF_AMMO:
        index = cell(view, ship, key)
        expected = AMMO_ABOARD.get(ship, 0)
        assert index.data(Qt.UserRole) == expected, ship
        assert index.data(Qt.DisplayRole) == f"{expected:,}"
        assert index.data(Qt.TextAlignmentRole) == int(Qt.AlignRight | Qt.AlignVCenter)
        seen += 1
    assert seen == 9, "every short ship was checked"
    assert cell(view, fit_corpus.SHIP_EXACT, key).data(Qt.DisplayRole) == "100"
    assert cell(view, fit_corpus.SHIP_EMPTY, key).data(Qt.DisplayRole) == "0", (
        "a ship carrying none of it reads 0, not a blank"
    )

    # A count is nobody's chip kind, so the cell actions are a no-op on it
    # rather than minting a filter on a key that is not a row column.
    before = view.omnibox.spec()
    view.tree.selectionModel().setCurrentIndex(
        cell(view, fit_corpus.SHIP_EXACT, key), QItemSelectionModel.NoUpdate
    )
    view._filter_current_cell(negated=False)
    view._filter_current_cell(negated=True)
    assert view.omnibox.spec() == before

    # The column belongs to the chip: taking the chip away takes it too.
    view.omnibox.clear()
    assert view.model.columns() == queries.ASSET_COLUMNS


def test_the_bay_forms_count_only_their_own_holds(fit_view):
    """Each bay form is a different question about the same rounds. Cargo
    sees 300 of the loaded Dominix's 650; fuel sees the ammo hold and the
    fuel bay together (200 + 100); the 50 loaded in its launcher answer to
    the whole ship and to no bay at all. The drone bay, the fighter tubes
    and the fleet hangar are three forms, so the five drones in the bay,
    the two fighters in a tube and the three drones in the fleet hangar
    each count once, under their own bay and no other."""
    view = fit_view

    commit_text(view, f'holds:"cargo/{AMMO_NAME}">=100')
    cargo_key = holds_key(f"cargo/{AMMO_NAME}>=100")
    assert item_ids(view) == {fit_corpus.SHIP_EXACT, fit_corpus.SHIP_PERMUTED}
    assert view.model.columns()[4] == (cargo_key, f"{AMMO_NAME} · cargo")
    assert cell(view, fit_corpus.SHIP_PERMUTED, cargo_key).data(Qt.UserRole) == 300

    view.omnibox.set_spec(omni.parse(f'holds:"fuel/{AMMO_NAME}">=1'))
    fuel_key = holds_key(f"fuel/{AMMO_NAME}>=1")
    assert item_ids(view) == {fit_corpus.SHIP_PERMUTED}
    assert cell(view, fit_corpus.SHIP_PERMUTED, fuel_key).data(Qt.UserRole) == 300

    view.omnibox.set_spec(omni.parse(f'holds:"drones/{DRONE_NAME}">=1'))
    drone_key = holds_key(f"drones/{DRONE_NAME}>=1")
    assert item_ids(view) == {fit_corpus.SHIP_EXACT}
    assert cell(view, fit_corpus.SHIP_EXACT, drone_key).data(Qt.UserRole) == 5

    view.omnibox.set_spec(omni.parse(f'holds:"drones/{FIGHTER_NAME}">=1'))
    assert item_ids(view) == set(), "a fighter in a tube is not in the drone bay"

    view.omnibox.set_spec(omni.parse(f'holds:"fighters/{FIGHTER_NAME}">=1'))
    fighter_key = holds_key(f"fighters/{FIGHTER_NAME}>=1")
    assert item_ids(view) == {fit_corpus.SHIP_EXACT}, "FighterTube2 rides with the fighter bay"
    assert view.model.columns()[4] == (fighter_key, f"{FIGHTER_NAME} · fighters")
    assert cell(view, fit_corpus.SHIP_EXACT, fighter_key).data(Qt.UserRole) == 2

    view.omnibox.set_spec(omni.parse(f'holds:"fleet/{DRONE_NAME}">=1'))
    fleet_key = holds_key(f"fleet/{DRONE_NAME}>=1")
    assert item_ids(view) == {fit_corpus.SHIP_EXACT}
    assert view.model.columns()[4] == (fleet_key, f"{DRONE_NAME} · fleet")
    assert cell(view, fit_corpus.SHIP_EXACT, fleet_key).data(Qt.UserRole) == 3

    # Several positive holds chips AND, and each keeps a column of its own.
    view.omnibox.set_spec(
        omni.parse(f'holds:"{AMMO_NAME}"<500 holds:"cargo/{AMMO_NAME}">=100')
    )
    assert item_ids(view) == {fit_corpus.SHIP_EXACT}
    assert column_keys(view)[4:6] == [holds_key(f"{AMMO_NAME}<500"), cargo_key]


def test_a_negated_holds_chip_lists_the_ships_short_and_nothing_else(fit_view):
    """The complement is taken within the assembled ships, not within the
    estate: `-holds:"X">=500` answers "which of my ships are short" -- the
    empty hull among them, since a ship with none of the type counts 0 --
    and never lets a module, a crate or the loose hangar stack through the
    way a NOT EXISTS negation would."""
    view = fit_view
    commit_text(view, f'-holds:"{AMMO_NAME}">=500')

    assert item_ids(view) == SHORT_OF_AMMO
    assert fit_corpus.SHIP_EMPTY in item_ids(view), "an empty hull is short of everything"
    assert fit_corpus.LOOSE_STACK not in item_ids(view)
    assert fit_corpus.CRATE_ITEM not in item_ids(view)
    # A negated chip grows no column: the rows are the whole answer.
    assert view.model.columns() == queries.ASSET_COLUMNS


def test_sorting_the_holds_column_orders_by_count_survives_a_reload_and_clears_with_it(
    fit_view,
):
    """The counts sort numerically, not as the text they paint ("40" sorts
    after "100" alphabetically), the sort is remembered by key so it outlives
    a reload, and it clears when the chip that grew the column goes rather
    than landing on whatever column now sits at that index."""
    view = fit_view
    commit_text(view, f'holds:"{AMMO_NAME}"<1000')
    key = holds_key(f"{AMMO_NAME}<1000")
    column = column_keys(view).index(key)
    assert item_ids(view) == fit_corpus.ASSEMBLED_SHIPS

    view.tree.header().sectionClicked.emit(column)
    counts = [view.model.holds_count(r, key) for r in view.model.rows()]
    assert len(counts) == 10 and counts == sorted(counts), f"ascending by count: {counts}"
    assert counts[-3:] == [40, 100, 650]
    assert (view.sorter.key, view.sorter.column) == (key, column)

    view.tree.header().sectionClicked.emit(column)
    assert [view.model.holds_count(r, key) for r in view.model.rows()][:3] == [650, 100, 40]

    view.reload()
    assert view.sorter.key == key
    assert [view.model.holds_count(r, key) for r in view.model.rows()][:3] == [650, 100, 40]

    view.omnibox.clear()
    assert view.model.columns() == queries.ASSET_COLUMNS
    assert view.sorter.key is None and view.sorter.column == -1
    assert view.tree.header().sortIndicatorSection() == -1
    assert len(view.model.rows()) == CORPUS_STACKS, "the table carries on"


def test_export_csv_carries_the_holds_column_and_its_counts(fit_view, tmp_path, monkeypatch):
    """The export reads the model's live columns, so the count goes out under
    the consumable's name with the number the cell shows -- the zeroes
    included, since a ship carrying none of it is an answer, not a blank."""
    view = fit_view
    commit_text(view, f'holds:"{AMMO_NAME}"<500')
    key = holds_key(f"{AMMO_NAME}<500")
    target = tmp_path / "holds.csv"
    monkeypatch.setattr(
        assets_view_module.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *args, **kwargs: (str(target), "CSV files (*.csv)")),
    )

    view.export_csv()

    with open(target, newline="", encoding="utf-8") as fh:
        header, *body = list(csv.reader(fh))
    assert header == [h for _k, h in view.model.columns()]
    assert header[4] == AMMO_NAME
    assert len(body) == len(SHORT_OF_AMMO)
    column = column_keys(view).index(key)
    exported = {
        int(body[row_position(view, ship)][column]): ship for ship in SHORT_OF_AMMO
    }
    assert sorted(exported) == [0, 40, 100], "the zeroes, the Solstice and the exact match"
    assert body[row_position(view, fit_corpus.SHIP_EXACT)][column] == "100"


def test_holds_columns_are_sized_from_the_text_they_paint_not_the_count_repr(
    fit_view, monkeypatch
):
    """The twin of the roll columns' float-repr test above. A holds cell
    paints "12,400" and exports 12400, so the sizing pass has to measure
    holds_cell_text; sized from cell_value the column is a comma short of
    the number it shows. Unlike the roll case the width alone cannot prove
    which was measured -- every SDE consumable name is wider than any count
    that fits a cargo bay, so the header sets the width either way -- so the
    assertion is on what the pass asked the model for, with the divergence
    it would otherwise miss pinned alongside it."""
    view = fit_view
    commit_text(view, f'holds:"{AMMO_NAME}"<500')
    key = holds_key(f"{AMMO_NAME}<500")
    rows = view.model.rows()

    # A four-figure hold is where the two disagree; the corpus' own counts
    # are all under a thousand, so the divergence is arranged here.
    view.model.set_holds_counts({key: {fit_corpus.SHIP_EXACT: 12400}})
    loaded = next(r for r in rows if r["item_id"] == fit_corpus.SHIP_EXACT)
    assert view.model.holds_cell_text(loaded, key) == "12,400"
    assert str(view.model.cell_value(loaded, key)) == "12400", "the export's narrower repr"

    asked: list[tuple[str, str]] = []
    painted, raw = view.model.holds_cell_text, view.model.cell_value

    def record_painted(row, k):
        asked.append(("painted", k))
        return painted(row, k)

    def record_raw(row, k):
        asked.append(("raw", k))
        return raw(row, k)

    monkeypatch.setattr(view.model, "holds_cell_text", record_painted)
    monkeypatch.setattr(view.model, "cell_value", record_raw)

    view._size_columns(rows, {key})

    assert asked, "the sizing pass measured something"
    assert {kind for kind, k in asked if k == key} == {"painted"}
    assert [k for _kind, k in asked if k != key] == [], "only the fresh column was measured"

    metrics = view.tree.fontMetrics()
    column = column_keys(view).index(key)
    widest = max(metrics.horizontalAdvance(painted(row, key)) for row in rows)
    assert view.tree.header().sectionSize(column) == (
        max(metrics.horizontalAdvance(AMMO_NAME), widest) + 24
    )


def test_the_holds_card_opens_from_enter_and_from_the_glyph_with_the_estates_consumables(
    fit_view, app
):
    """Typing the chip and pressing Enter is the natural way in, so the card
    opens on it one turn later, seeded from the chip -- and the glyph reopens
    it afterwards. The picker is the seeded bay's list: the chip names the
    cargo hold, so it offers what the cargo holds carry, by name, with the
    count on the tooltip, the fitted modules absent (a launcher is not a
    consumable) and the crystal held only in a laser absent too."""
    view = fit_view
    card = card_after_enter(view, app, f'holds:"cargo/{AMMO_NAME}"<500', omni.HOLDS_KIND)

    assert isinstance(card, HoldsCard)
    assert card.type_name() == AMMO_NAME
    assert card.bay() == "cargo"
    assert card.op_combo.currentData() == "<"
    assert card.low_spin.value() == 500
    assert card.high_spin.isHidden(), "the high field belongs to the range form alone"
    assert holds_picker(card) == [
        (AMMO_NAME, "440 in 3 ships"),
        (PASTE_NAME, "5 in 1 ships"),
        ("Reinforced Cargo Crate", "1 in 1 ships"),
        (DRONE_NAME, "2 in 1 ships"),
    ]
    listed = [name for name, _tip in holds_picker(card)]
    assert "Focused Pulse Laser" not in listed and fit_corpus.CRYSTAL_NAME not in listed
    # The denominator is ships, not assets: every one of the ten assembled
    # ships carries under 500 rounds in its cargo hold.
    assert settle_ship_count(view) == "10 of 10 match"

    card.cancel_btn.click()
    reopened = open_kind_card(view, app, omni.HOLDS_KIND)
    assert reopened is card and reopened.type_name() == AMMO_NAME
    card.cancel_btn.click()


def test_the_holds_picker_follows_the_bay_and_the_fuel_list_offers_every_fuel(fit_view, app):
    """A blank card opens on the whole ship, busiest first; each bay click
    refetches the list for that bay. Fuel, drones and fighters offer what
    the estate does not hold -- the SDE's fuel blocks and ice products, its
    drones, its fighters -- since each question is usually "which ship has
    none", with the held ones leading and the rest by name; picking one of
    those writes the very chip the grammar would parse from typed text. A
    name typed before the click stays in the edit across the refill."""
    view = fit_view
    card = open_new_card(view, app, omni.HOLDS_KIND)
    assert card.bay() is None
    assert [name for name, _tip in holds_picker(card)] == [
        AMMO_NAME, FUEL_NAME, DRONE_NAME, PASTE_NAME, "Reinforced Cargo Crate",
        FIGHTER_NAME, fit_corpus.CRYSTAL_NAME,
    ]

    card.bay_buttons["fuel"].click()
    assert holds_picker(card) == [
        (AMMO_NAME, "300 in 1 ships"),
        (FUEL_NAME, "40 in 1 ships"),
        (fit_corpus.HEAVY_WATER_NAME, "None aboard the ships in scope"),
        (fit_corpus.STRONTIUM_NAME, "None aboard the ships in scope"),
    ]
    card.bay_buttons["drones"].click()
    assert holds_picker(card) == [
        (DRONE_NAME, "5 in 1 ships"),
        (fit_corpus.SPARE_DRONE_NAME, "None aboard the ships in scope"),
    ], "the bay's drones, then the SDE's; the fighter in its tube is in neither half"
    card.bay_buttons["cargo"].click()
    assert [name for name, _tip in holds_picker(card)] == [
        AMMO_NAME, PASTE_NAME, "Reinforced Cargo Crate", DRONE_NAME,
    ]

    card.type_edit.setText(FUEL_NAME)
    card.bay_buttons["fuel"].click()
    assert card.type_name() == FUEL_NAME
    assert card.type_combo.currentIndex() == 1, "re-selected from the fuel list"
    # The count follows the bay too: the one Solstice burns fuel.
    assert settle_ship_count(view) == "1 of 10 match"

    card.done_btn.click()
    assert view.omnibox.spec().chips == [omni.Chip(omni.HOLDS_KIND, f"fuel/{FUEL_NAME}>=1")]
    assert item_ids(view) == {fit_corpus.SHIP_NO_FIT}


def test_the_fighters_and_fleet_bays_list_their_own_holds_and_done_writes_the_bay(
    fit_view, app
):
    """The two bays the drone form used to swallow. Fighters offers the
    fighter in its tube first and the SDE's other fighter after it, and a
    pick plus Done writes the fighters/ chip that lists the one carrier;
    Fleet offers whatever the fleet hangars hold -- the crate and the drones
    -- because a fleet hangar is a general hold, not a drone bay."""
    view = fit_view
    card = open_new_card(view, app, omni.HOLDS_KIND)
    card.bay_buttons["fighters"].click()
    assert holds_picker(card) == [
        (FIGHTER_NAME, "2 in 1 ships"),
        (fit_corpus.SPARE_FIGHTER_NAME, "None aboard the ships in scope"),
    ]
    card.type_combo.setCurrentIndex(0)
    assert card.type_name() == FIGHTER_NAME
    assert settle_ship_count(view) == "1 of 10 match"
    card.done_btn.click()
    assert view.omnibox.spec().chips == [
        omni.Chip(omni.HOLDS_KIND, f"fighters/{FIGHTER_NAME}>=1")
    ]
    assert item_ids(view) == {fit_corpus.SHIP_EXACT}
    key = holds_key(f"fighters/{FIGHTER_NAME}>=1")
    assert view.model.columns()[4] == (key, f"{FIGHTER_NAME} · fighters")

    card = open_kind_card(view, app, omni.HOLDS_KIND)
    assert card.bay() == "fighters", "seeded from the chip it wrote"
    card.bay_buttons["fleet"].click()
    assert holds_picker(card) == [
        ("Reinforced Cargo Crate", "1 in 1 ships"),
        (DRONE_NAME, "3 in 1 ships"),
    ]
    card.type_combo.setCurrentIndex(1)
    card.done_btn.click()
    assert view.omnibox.spec().chips == [omni.Chip(omni.HOLDS_KIND, f"fleet/{DRONE_NAME}>=1")]
    assert item_ids(view) == {fit_corpus.SHIP_EXACT}


def test_holds_card_done_writes_the_exact_chip_and_replaces_only_the_holds_one(fit_view, app):
    """Done is the one path that applies. The chip it writes is the one the
    grammar would have parsed from typed text, so a saved view of either
    recalls the same; a chip of another kind stays exactly where it was, and
    a second Done replaces the card's own chip rather than ANDing another on.
    Free text the picker never offered is a valid consumable name, unlike the
    abyssal card's list."""
    view = fit_view
    view.omnibox.set_spec(omni.parse(f'sys:Jita holds:"{AMMO_NAME}"<500'))
    assert item_ids(view) == SHORT_OF_AMMO

    card = open_kind_card(view, app, omni.HOLDS_KIND)
    done = record(card.done)
    card.bay_buttons["cargo"].click()
    card.op_combo.setCurrentIndex(
        next(i for i in range(card.op_combo.count()) if card.op_combo.itemData(i) == ">=")
    )
    card.low_spin.setValue(100)
    card.done_btn.click()

    assert done == [([omni.Chip(omni.HOLDS_KIND, f"cargo/{AMMO_NAME}>=100")],)]
    assert view.omnibox.spec() == omni.FilterSpec(
        chips=[
            omni.Chip("system", "Jita"),
            omni.Chip(omni.HOLDS_KIND, f"cargo/{AMMO_NAME}>=100"),
        ]
    )
    assert item_ids(view) == {fit_corpus.SHIP_EXACT, fit_corpus.SHIP_PERMUTED}
    assert not card.isVisible()

    card = open_kind_card(view, app, omni.HOLDS_KIND)
    card.bay_buttons[None].click()
    card.type_edit.setText(PASTE_NAME)
    QTest.keyClick(card.type_edit, Qt.Key_Return)
    assert card.isVisible(), "Enter in the type edit commits the text; it is not Done"
    card.op_combo.setCurrentIndex(
        next(i for i in range(card.op_combo.count()) if card.op_combo.itemData(i) == "..")
    )
    card.low_spin.setValue(1)
    card.high_spin.setValue(9)
    card.done_btn.click()

    assert view.omnibox.spec().chips == [
        omni.Chip("system", "Jita"),
        omni.Chip(omni.HOLDS_KIND, f"{PASTE_NAME}=1..9"),
    ]
    assert item_ids(view) == {fit_corpus.SHIP_CHARON}, "the only ship carrying paste"


def test_a_negated_holds_chip_and_other_kinds_ride_through_the_cards_done(fit_view, app):
    """The card has no polarity control, so a negated holds chip is not its to
    rewrite: it seeds from the positive one and Done puts the negated chip
    back untouched, beside every chip of another kind -- the way a typed
    `roll:` chip rides through the abyssal card's Done."""
    view = fit_view
    view.omnibox.set_spec(
        omni.parse(f'sys:Jita -holds:"{PASTE_NAME}">=1 holds:"{AMMO_NAME}"<500')
    )
    assert item_ids(view) == SHORT_OF_AMMO - {fit_corpus.SHIP_CHARON}

    card = open_kind_card(view, app, omni.HOLDS_KIND)
    assert card.type_name() == AMMO_NAME, "seeded from the positive chip, not the negated one"
    card.low_spin.setValue(50)
    card.done_btn.click()

    assert view.omnibox.spec().chips == [
        omni.Chip("system", "Jita"),
        omni.Chip(omni.HOLDS_KIND, f"{PASTE_NAME}>=1", negated=True),
        omni.Chip(omni.HOLDS_KIND, f"{AMMO_NAME}<50"),
    ]
    assert item_ids(view) == SHORT_OF_AMMO - {
        fit_corpus.SHIP_CHARON, fit_corpus.SHIP_EXACT
    }


def test_cancel_escape_and_an_outside_open_leave_the_holds_filter_as_it_was(fit_view, app):
    """Every way out but Done reads as Cancel, and opening a second card is
    one of them: a Qt.Popup closes on the click that opens the next, so the
    registry hands the count and the writes to the new card while the first
    changes nothing."""
    view = fit_view
    view.omnibox.set_spec(omni.parse(f'holds:"{AMMO_NAME}"<500 fit:"Ratting"'))
    before = view.omnibox.spec()
    changes = record(view.omnibox.changed)

    card = open_kind_card(view, app, omni.HOLDS_KIND)
    cancelled = record(card.cancelled)
    card.low_spin.setValue(42)
    card.cancel_btn.click()
    assert cancelled == [()] and view.omnibox.spec() == before

    card = open_kind_card(view, app, omni.HOLDS_KIND)
    card.low_spin.setValue(7)
    QTest.keyClick(card, Qt.Key_Escape)
    assert cancelled == [(), ()] and view.omnibox.spec() == before

    card = open_kind_card(view, app, omni.HOLDS_KIND)
    card.low_spin.setValue(3)
    fit_card = open_kind_card(view, app, omni.FIT_KIND)
    assert not card.isVisible(), "opening the fit card cancels the holds card"
    assert cancelled == [(), (), ()]
    assert view._active_card is fit_card
    fit_card.cancel_btn.click()

    assert view.omnibox.spec() == before
    assert changes == [], "nothing on the Cancel paths touched the omnibox"


def test_the_draft_builders_holds_and_fit_kinds_open_a_card_without_minting_a_chip(
    fit_view, app
):
    """Neither chip has a bare form worth inserting -- an empty holds chip
    compares nothing and an empty fit chip names no fit -- so the builder's
    kind stage hands the request straight to the card and puts nothing in the
    omnibox until Done."""
    view = fit_view

    seen = []
    for kind, klass in ((omni.HOLDS_KIND, HoldsCard), (omni.FIT_KIND, FitCard)):
        card = open_new_card(view, app, kind)
        assert isinstance(card, klass)
        assert view.omnibox.spec().chips == [], f"the {kind} draft minted a chip"
        assert view.omnibox._draft is None, "the builder closed behind the card"
        card.cancel_btn.click()
        seen.append(kind)
    assert seen == [omni.HOLDS_KIND, omni.FIT_KIND]

    # Seeded blank, the holds card knows no consumable, so Done is unavailable
    # -- applying an empty chip list would silently delete the user's chip.
    card = view._cards[omni.HOLDS_KIND]
    assert card.term() is None and not card.done_btn.isEnabled()


def test_a_fit_card_opened_with_no_chip_behind_it_selects_no_fit(fit_view, app):
    """Pins the contract: `chips()` is empty and Done disabled until a fit is
    picked. A card that pre-selects on the user's behalf turns "let me look at
    my stored fits" into a filter on whichever one sorts first."""
    view = fit_view
    card = open_new_card(view, app, omni.FIT_KIND)

    assert card.selected_fit() is None
    assert card.chips() == []
    assert not card.done_btn.isEnabled()


def test_typing_three_card_chips_and_pressing_enter_opens_exactly_one_card(fit_view, app):
    """Three cards fighting over one Enter would each close the last as an
    outside click and only the survivor would be seen, so one commit asks for
    one card: the first card-kind chip in the text. Every chip is still
    minted."""
    view = fit_view
    requests = record(view.omnibox.card_requested)
    app.processEvents()

    commit_text(view, f'abyssal holds:"{AMMO_NAME}"<5 fit:"Ratting"')
    app.processEvents()

    assert len(requests) == 1
    assert requests[0][0] == omni.Chip(omni.ABYSSAL_KIND, "")
    assert [k for k, c in view._cards.items() if c.isVisible()] == [omni.ABYSSAL_KIND]
    assert [c.kind for c in view.omnibox.spec().chips] == [
        omni.ABYSSAL_KIND,
        omni.HOLDS_KIND,
        omni.FIT_KIND,
    ]
    view._cards[omni.ABYSSAL_KIND].cancel_btn.click()


def test_the_fit_card_lists_the_stored_fits_parses_a_paste_refuses_unknowns_and_saves(
    fit_view, app
):
    """The card is the whole store's editor. It lists what is stored, hull
    first; a paste naming an item the SDE does not know reports it and cannot
    be saved, because a fit missing a line would call every ship carrying
    that module deviating; a clean paste saves, appears in the list, and the
    fit it stores really does decide the chip -- the Solstice nobody had a
    fit for joins is:fit the moment its rack is stored."""
    view = fit_view
    view.omnibox.set_spec(omni.parse("is:fit"))
    assert item_ids(view) == MATCHES_ANY_FIT
    assert fit_corpus.SHIP_NO_FIT not in item_ids(view)

    card = open_new_card(view, app, omni.FIT_KIND)
    assert isinstance(card, FitCard)
    assert fit_rows(card) == [
        "Hauling · Charon · 2 modules",
        "Ratting · Dominix · 7 modules",
        "Solo · Dominix · 3 modules",
    ]

    paste_fit(card, UNKNOWN_EFT)
    assert card.status_label.text() == "Unknown: Quantum Whatsit"
    assert not card.save_btn.isEnabled(), "an unknown name cannot be stored"

    paste_fit(card, "not a fit at all")
    assert card.status_label.text() == "No [Hull, Name] header"
    assert not card.save_btn.isEnabled()

    paste_fit(card, SKIRMISH_EFT)
    assert card.status_label.text() == "Solstice · 1 module · 1 subsystem"
    assert card.name_edit.text() == "Skirmish", "the header names the fit"
    assert card.save_btn.isEnabled()

    card.save_btn.click()

    assert view.footer.text() == "Stored the fit Skirmish."
    assert fit_rows(card)[-1] == "Skirmish · Solstice · 2 modules"
    assert card.selected_fit()["name"] == "Skirmish", "the fit just saved is the selection"
    assert [r["name"] for r in fits.list_fits(view.conn)] == [
        "Hauling", "Ratting", "Solo", "Skirmish",
    ]

    card.cancel_btn.click()
    view.reload()
    assert item_ids(view) == MATCHES_ANY_FIT | {fit_corpus.SHIP_NO_FIT}


def test_the_fit_card_deletes_a_fit_and_the_chip_naming_it_then_matches_nothing(fit_view, app):
    """Forgetting a fit is one click on its line. The chip that named it is
    left standing on purpose -- it now matches nothing, which is the honest
    answer, and removing the user's chip behind their back would be worse."""
    view = fit_view
    commit_text(view, 'fit:"Ratting"')
    assert item_ids(view) == RATTING_MATCHES

    card = open_kind_card(view, app, omni.FIT_KIND)
    assert card.selected_fit()["name"] == "Ratting", "the chip seeded the selection"
    delete_fit_row(card, "Ratting")

    assert [r["name"] for r in fits.list_fits(view.conn)] == ["Hauling", "Solo"]
    assert fit_rows(card) == ["Hauling · Charon · 2 modules", "Solo · Dominix · 3 modules"]
    assert card.selected_fit() is None and not card.done_btn.isEnabled()

    card.cancel_btn.click()
    view.reload()
    assert view.omnibox.spec().chips == [omni.Chip(omni.FIT_KIND, "Ratting")]
    assert item_ids(view) == set()


def test_a_fit_saved_or_deleted_in_the_card_moves_the_rows_without_a_manual_reload(fit_view, app):
    """The rows answer to the store as much as to the filter. Under
    fit:"Ratting", pasting a different rack under that name changes which
    ships match, and forgetting the fit leaves the chip matching nothing;
    both used to re-list the card and leave the table showing the answer
    the store no longer gave until something else happened to reload it."""
    view = fit_view
    commit_text(view, 'fit:"Ratting"')
    assert item_ids(view) == RATTING_MATCHES

    card = open_kind_card(view, app, omni.FIT_KIND)
    paste_fit(card, fit_corpus.SOLO_EFT)
    card.name_edit.setText("Ratting")
    card.name_edit.textEdited.emit("Ratting")
    card.save_btn.click()
    assert view.footer.text() == "Stored the fit Ratting."
    assert "Ratting · Dominix · 3 modules" in fit_rows(card), "an edit, not a second Ratting"
    card.cancel_btn.click()

    assert item_ids(view) == {fit_corpus.SHIP_SOLO}, "the rows followed the replacement rack"

    card = open_kind_card(view, app, omni.FIT_KIND)
    delete_fit_row(card, "Ratting")
    card.cancel_btn.click()

    assert view.omnibox.spec().chips == [omni.Chip(omni.FIT_KIND, "Ratting")]
    assert item_ids(view) == set(), "the chip stands and the rows say it matches nothing"


def test_the_inspectors_fit_block_follows_a_fit_deleted_in_the_card(fit_view, app):
    """The block is diffed against a stored fit, so forgetting that fit has
    to re-render it at once: the reload the delete now runs re-inspects the
    open row (_on_rows), and the verdict moves to the closest fit left."""
    view = fit_view
    open_inspector(view, fit_corpus.SHIP_EXACT)
    assert fit_lines(view.inspector) == ["Matches Ratting"]

    card = open_new_card(view, app, omni.FIT_KIND)
    delete_fit_row(card, "Ratting")
    card.cancel_btn.click()

    assert panel_open(view) and view._panel_host.row["item_id"] == fit_corpus.SHIP_EXACT
    assert fit_lines(view.inspector)[0] == "Closest stored fit: Solo"
    assert view.inspector.fit_note.text() == "Solo · Dominix"


def test_fit_card_done_writes_the_polarity_the_radios_show(fit_view, app):
    """One radio pair rather than two chips: both questions are asked of the
    same fit and the hull scope is the same either way, so the card owns both
    polarities and Done rewrites whichever was there."""
    view = fit_view
    card = card_after_enter(view, app, 'fit:"Ratting"', omni.FIT_KIND)
    assert card.match_radio.isChecked() and item_ids(view) == RATTING_MATCHES

    card.deviate_radio.setChecked(True)
    card.done_btn.click()

    assert view.omnibox.spec().chips == [omni.Chip(omni.FIT_KIND, "Ratting", negated=True)]
    assert item_ids(view) == RATTING_DEVIATES
    assert fit_corpus.SHIP_CHARON not in item_ids(view), "a fit says nothing of other hulls"
    assert fit_corpus.SHIP_PACKAGED not in item_ids(view)

    card = open_kind_card(view, app, omni.FIT_KIND)
    assert card.deviate_radio.isChecked(), "the negation seeded the radios"
    assert card.selected_fit()["name"] == "Ratting"
    card.match_radio.setChecked(True)
    card.done_btn.click()

    assert view.omnibox.spec().chips == [omni.Chip(omni.FIT_KIND, "Ratting")]
    assert item_ids(view) == RATTING_MATCHES


def test_is_fit_and_its_negation_only_speak_about_hulls_with_a_stored_fit(fit_view):
    """`is:fit` is "fitted the way some stored fit of this hull says", and the
    negation is its complement *within the hulls that have one*: the Solstice,
    which nobody has written a fit for, is in neither answer, and the packaged
    stack is in neither either."""
    view = fit_view

    commit_text(view, "is:fit")
    assert item_ids(view) == MATCHES_ANY_FIT
    assert fit_corpus.SHIP_SOLO in item_ids(view), "matching a hull's second fit counts"

    view.omnibox.set_spec(omni.parse("-is:fit"))
    assert item_ids(view) == DEVIATES_FROM_ALL
    assert fit_corpus.SHIP_NO_FIT not in item_ids(view)
    assert fit_corpus.SHIP_PACKAGED not in item_ids(view)
    assert item_ids(view) & MATCHES_ANY_FIT == set(), "the polarities never overlap"

    # Two positive fit chips OR, unlike the ANDing holds ones.
    view.omnibox.set_spec(omni.parse('fit:"Ratting" fit:"Solo"'))
    assert item_ids(view) == RATTING_MATCHES | {fit_corpus.SHIP_SOLO}


def test_the_inspector_shows_the_fit_diff_for_a_ship_and_hides_it_for_everything_else(
    fit_view,
):
    """The block is for assembled ships whose hull has a stored fit, and for
    nothing else. A deviating Dominix reports the closest fit by name with the
    modules missing and the consumables it is short of; a matching one says so
    with no lines under it; a hull nobody has a fit for, a packaged stack and
    an ordinary module get no block at all."""
    view = fit_view
    inspector = view.inspector

    open_inspector(view, fit_corpus.SHIP_MISSING)
    assert not inspector.fit_box.isHidden()
    assert fit_lines(inspector) == [
        "Closest stored fit: Ratting",
        "Missing: 1 × Auxiliary Nano Pump",
        f"Short: {AMMO_NAME} 0 of 100, {DRONE_NAME} 0 of 5",
    ]
    assert inspector.fit_extra.isHidden(), "nothing extra on this rack"
    assert inspector.fit_note.text() == "Ratting · Dominix"

    open_inspector(view, fit_corpus.SHIP_NULL_CATEGORY)
    assert "Extra: 1 × Unlisted Widget" in fit_lines(inspector), (
        "a module whose SDE category row is missing is a module, not a vanishing act"
    )

    open_inspector(view, fit_corpus.SHIP_EXACT)
    assert fit_lines(inspector) == ["Matches Ratting"], "loaded charges are not modules"
    assert inspector.fit_missing.isHidden() and inspector.fit_short.isHidden()

    open_inspector(view, fit_corpus.SHIP_NO_FIT)
    assert inspector.fit_box.isHidden(), "no stored fit for this hull, nothing to say"

    open_inspector(view, fit_corpus.SHIP_PACKAGED)
    assert inspector.fit_box.isHidden(), "a packaged stack has no rack"

    # The launcher fitted to the exact match is hidden from the everyday
    # view; is:fitted lists it, and even then a module gets no fit block.
    view.omnibox.set_spec(omni.parse("is:fitted"))
    open_inspector(view, 5_100_005)
    assert inspector.fit_box.isHidden()


def test_a_fit_chip_makes_the_inspector_diff_against_the_fit_it_names(fit_view):
    """Without a chip the block reports the closest fit, which for the Solo
    Dominix is the Solo fit it matches. A positive `fit:` chip is the user
    saying which fit they are asking about, so the verdict switches to that
    one -- and says "Deviates from", a claim the closest-fit wording is not
    allowed to make. A negated chip names the same fit: the user is looking
    at the ships that deviate from Ratting, so each is measured against
    Ratting rather than against whichever stored fit sits closest."""
    view = fit_view
    inspector = view.inspector

    open_inspector(view, fit_corpus.SHIP_SOLO)
    assert fit_lines(inspector) == ["Matches Solo"]

    view.omnibox.set_spec(omni.parse('-fit:"Ratting"'))
    assert fit_corpus.SHIP_SOLO in item_ids(view)
    open_inspector(view, fit_corpus.SHIP_SOLO)
    assert fit_lines(inspector)[0] == "Deviates from Ratting", "a negated chip names its fit"

    # Two positive fit chips OR, so the Solo Dominix is on the table while
    # the first of them, Ratting, is what the inspector is asked about.
    view.omnibox.set_spec(omni.parse('fit:"Ratting" fit:"Solo"'))
    open_inspector(view, fit_corpus.SHIP_SOLO)
    assert fit_lines(inspector) == [
        "Deviates from Ratting",
        "Missing: 3 × Magnetic Field Amplifier, 1 × Medium Armor Repair Unit, "
        "1 × Rocket Launcher Array",
        "Extra: 1 × Focused Pulse Laser",
        f"Short: {AMMO_NAME} 0 of 100, {DRONE_NAME} 0 of 5",
    ]
    assert inspector.fit_note.text() == "Ratting · Dominix"


def test_a_fit_diff_for_one_host_never_paints_into_the_other(fit_view, fit_conn):
    """Two ships open at once, each with a lookup in flight. Each result must
    land in the host that asked, whichever order they arrive in; one shared
    guard would paint the exact match's verdict under the deviating ship. The
    same guard drops a result whose host has closed in the meantime."""
    view = fit_view
    pending = capture_fit_lookups(view)
    inspect_in_window(view, row_position(view, fit_corpus.SHIP_EXACT))
    open_inspector(view, fit_corpus.SHIP_MISSING)
    panel, window = view.inspector, view.inspector_window.inspector
    assert set(pending) == {"panel", "window"}
    loading = "Comparing with the stored fits…"
    assert panel.fit_verdict.text() == window.fit_verdict.text() == loading

    fetch, deliver = pending["window"]
    deliver(fetch(fit_conn))
    assert fit_lines(window) == ["Matches Ratting"]
    assert panel.fit_verdict.text() == loading, "the panel is still waiting"

    fetch, deliver = pending["panel"]
    deliver(fetch(fit_conn))
    assert panel.fit_verdict.text() == "Closest stored fit: Ratting"
    assert fit_lines(window) == ["Matches Ratting"], "the window kept its own answer"

    open_inspector(view, fit_corpus.SHIP_DUPES)
    assert panel.fit_verdict.text() == loading
    fetch, deliver = pending["panel"]
    view._close_inspector(view._panel_host)
    deliver(fetch(fit_conn))

    assert not panel_open(view) and view._panel_host.row is None
    assert panel.fit_verdict.text() == loading, "nothing was painted"
    assert window_open(view), "closing the panel left the window alone"


def compare_action(view: AssetsView, item_id: int):
    """The row menu's "Compare deviation…" action, or None when the row has
    no such entry. Read after "View fit…" so the test also pins where the
    entry sits: right under the action a user already knows."""
    menu = view._build_context_menu(view.model.index(row_position(view, item_id), COLUMN["item"]))
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    if "Compare deviation…" not in texts:
        return None
    assert texts.index("Compare deviation…") == texts.index("View fit…") + 1
    return next(a for a in menu.actions() if a.text() == "Compare deviation…")


@pytest.fixture
def inline_compare_queries(fit_conn, monkeypatch):
    """Run every AsyncQuery created from now on inline on the corpus
    connection. The comparison window builds its own query after
    _wired_view has patched the view's, so the class is patched for the
    windows the tests open; the view's instance-level patches still win."""
    from evasset.ui.async_query import AsyncQuery

    monkeypatch.setattr(
        AsyncQuery, "run", lambda self, fn, on_done, on_failed=None: on_done(fn(fit_conn))
    )


def test_compare_deviation_opens_the_window_for_a_deviating_ship(fit_view, inline_compare_queries):
    """Right-click a Dominix listed under `-fit:"Ratting"` and the action is
    there, enabled, and opens the window against Ratting -- the closest fit,
    since a negated chip names nothing -- with the one missing rig and the
    two stacks the empty hull has none of counted in the headline. Those
    stacks are drawn under the racks as the Drones and Cargo sections, the
    ship's zero in red opposite the fit's figure in green, in place of the
    "Short:" paragraph the window used to end with."""
    view = fit_view
    view.omnibox.set_spec(omni.parse('-fit:"Ratting"'))
    assert fit_corpus.SHIP_MISSING in item_ids(view)
    action = compare_action(view, fit_corpus.SHIP_MISSING)
    assert action is not None and action.isEnabled()
    action.trigger()
    dialog = view._compare_dialogs[fit_corpus.SHIP_MISSING]
    assert dialog.isVisible()
    assert dialog.windowTitle() == "Compare with Ratting"
    assert dialog.comparison.fit_name == fit_corpus.RATTING and dialog.comparison.named
    assert dialog.verdict.text() == "1 missing · 2 short"
    assert dialog.note.text() == "compared with Ratting · Dominix"
    assert dialog.captions[-2:] == ["Drones", "Cargo"]
    ship_cargo = [(line, label) for c, line, label in dialog.ship_labels if c == "Cargo"]
    fit_cargo = [(line, label) for c, line, label in dialog.fit_labels if c == "Cargo"]
    assert [(line.status, label.text()) for line, label in ship_cargo] == [
        (fits.SHORT, f"0 × {AMMO_NAME} (of 100)")
    ]
    assert [(line.status, label.text()) for line, label in fit_cargo] == [
        (fits.MISSING, f"100 × {AMMO_NAME}")
    ]
    assert palette.delta_hex(False, dialog.palette()) in ship_cargo[0][1].styleSheet()
    assert palette.delta_hex(True, dialog.palette()) in fit_cargo[0][1].styleSheet()
    assert not hasattr(dialog, "short_label")
    dialog.reject()


def test_compare_deviation_is_offered_only_while_a_deviation_filter_is_active(fit_view):
    """The entry answers "how does this ship deviate from the fit I am
    filtering on", so it belongs to that filter: absent with no fit filter
    and under a positive `fit:` chip (those rows match, there is nothing to
    compare), present on the assembled ships listed under `-fit:"…"` and
    under `-is:fit`, and never on a packaged stack."""
    view = fit_view
    assert compare_action(view, fit_corpus.SHIP_MISSING) is None, "no fit filter"

    view.omnibox.set_spec(omni.parse('fit:"Ratting"'))
    assert fit_corpus.SHIP_EXACT in item_ids(view)
    assert compare_action(view, fit_corpus.SHIP_EXACT) is None, "a positive chip lists matches"

    view.omnibox.set_spec(omni.parse('-fit:"Ratting"'))
    assert fit_corpus.SHIP_MISSING in item_ids(view)
    action = compare_action(view, fit_corpus.SHIP_MISSING)
    assert action is not None and action.isEnabled()
    assert fit_corpus.SHIP_PACKAGED not in item_ids(view), "a packaged stack is under neither polarity"

    view.omnibox.set_spec(omni.parse("-is:fit"))
    assert fit_corpus.SHIP_EXTRA in item_ids(view)
    action = compare_action(view, fit_corpus.SHIP_EXTRA)
    assert action is not None and action.isEnabled()


def test_the_named_fit_wins_over_the_closest_when_a_fit_chip_is_active(
    fit_view, inline_compare_queries
):
    """Under `-fit:"Ratting"` the Solo Dominix -- which matches Solo, the
    fit closest to it -- is measured against Ratting, the fit the user is
    filtering deviations from; the inspector's own Compare… button, which
    needs no filter, falls back to the closest fit and reports the match.
    Both go through one window per ship, re-run rather than re-opened."""
    view = fit_view
    view.omnibox.set_spec(omni.parse('-fit:"Ratting"'))
    assert fit_corpus.SHIP_SOLO in item_ids(view)
    compare_action(view, fit_corpus.SHIP_SOLO).trigger()
    dialog = view._compare_dialogs[fit_corpus.SHIP_SOLO]
    assert dialog.windowTitle() == "Compare with Ratting"
    assert dialog.comparison.named and dialog.verdict.text() == "5 missing · 1 extra · 2 short"

    view.omnibox.set_spec(omni.parse("cat:Ship"))
    view._open_compare_dialog(view.model.rows()[row_position(view, fit_corpus.SHIP_SOLO)])
    assert view._compare_dialogs[fit_corpus.SHIP_SOLO] is dialog, "one window per ship"
    assert dialog.windowTitle() == "Compare with Solo" and dialog.verdict.text() == "Matches"
    dialog.reject()


def test_a_second_compare_reuses_the_window_and_closing_it_forgets_it(
    fit_view, inline_compare_queries
):
    """Two right-clicks on one ship are one window; a right-click on another
    ship is another. Closing a window takes it out of the registry, so the
    next compare on that ship builds a fresh one rather than showing a
    dialog Qt has already torn down."""
    view = fit_view
    view.omnibox.set_spec(omni.parse('-fit:"Ratting"'))
    compare_action(view, fit_corpus.SHIP_MISSING).trigger()
    compare_action(view, fit_corpus.SHIP_MISSING).trigger()
    compare_action(view, fit_corpus.SHIP_EXTRA).trigger()
    assert set(view._compare_dialogs) == {fit_corpus.SHIP_MISSING, fit_corpus.SHIP_EXTRA}
    first = view._compare_dialogs[fit_corpus.SHIP_MISSING]
    assert first.isVisible()

    first.reject()
    assert set(view._compare_dialogs) == {fit_corpus.SHIP_EXTRA}
    compare_action(view, fit_corpus.SHIP_MISSING).trigger()
    assert view._compare_dialogs[fit_corpus.SHIP_MISSING] is not first
    for dialog in list(view._compare_dialogs.values()):
        dialog.reject()
    assert view._compare_dialogs == {}


def test_the_inspectors_compare_button_opens_the_same_window(fit_view, fit_conn, inline_compare_queries):
    """The Fit block's button is the second door to the one window: hidden
    while the block is still comparing and for rows with no block, shown
    with the verdict, and opening through it registers the same dialog the
    menu would."""
    view = fit_view
    pending = capture_fit_lookups(view)
    open_inspector(view, fit_corpus.SHIP_MISSING)
    inspector = view.inspector
    assert inspector.fit_compare_btn.isHidden(), "nothing to lay side by side yet"
    fetch, deliver = pending["panel"]
    deliver(fetch(fit_conn))
    assert not inspector.fit_compare_btn.isHidden()

    inspector.fit_compare_btn.click()
    dialog = view._compare_dialogs[fit_corpus.SHIP_MISSING]
    assert dialog.isVisible() and dialog.windowTitle() == "Compare with Ratting"
    dialog.reject()

    open_inspector(view, fit_corpus.SHIP_NO_FIT)
    fetch, deliver = pending["panel"]
    deliver(fetch(fit_conn))
    assert inspector.fit_box.isHidden() and inspector.fit_compare_btn.isHidden()


def test_a_saved_view_with_a_holds_and_a_negated_fit_chip_recalls_identically(fit_view):
    """Saved views are grammar text, so the new chips are covered only if
    to_text writes what parse reads: the bay prefix inside the quotes, the
    comparison, the negated fit name and `is:fit` all round-trip, and the
    recalled view filters and grows its column like the original."""
    view = fit_view
    spec = omni.parse(f'holds:"cargo/{AMMO_NAME}<500" -fit:"Ratting"')
    view.omnibox.set_spec(spec)
    key = holds_key(f"cargo/{AMMO_NAME}<500")
    assert item_ids(view) == RATTING_DEVIATES
    assert column_keys(view)[4] == key

    save_to_slot(view, 3)

    stored = views.view_in_slot(view.conn, 3)
    assert stored.name == "Slot 3"
    assert stored.state.filter == f'holds:"cargo/{AMMO_NAME}<500" -fit:Ratting'
    assert omni.parse(stored.state.filter) == spec

    view.omnibox.clear()
    assert len(view.model.rows()) == CORPUS_STACKS
    assert view.model.columns() == queries.ASSET_COLUMNS

    view._recall_view(3)

    assert view.omnibox.spec() == spec
    assert item_ids(view) == RATTING_DEVIATES
    assert column_keys(view)[4] == key

    flag = omni.FilterSpec(chips=[omni.Chip("is", "fit")])
    assert flag.to_text() == "is:fit"
    assert omni.parse(flag.to_text()) == flag


def test_the_fit_cards_footer_counts_the_ships_of_the_selected_fits_hull(fit_view, app):
    """A fit says nothing about any other hull, so "N of TOTAL" counts the
    assembled ships of the selection's hull: two of the eight Dominixes match
    Ratting, six deviate, and switching to the Charon's fit switches the
    denominator to that hull's one ship."""
    view = fit_view
    card = card_after_enter(view, app, 'fit:"Ratting"', omni.FIT_KIND)
    assert settle_ship_count(view) == "2 of 8 match"

    card.deviate_radio.setChecked(True)
    assert settle_ship_count(view) == "6 of 8 match"

    select_fit_row(card, "Hauling")
    card.match_radio.setChecked(True)
    assert settle_ship_count(view) == "1 of 1 match"
    card.cancel_btn.click()


def test_a_card_chip_typed_after_another_chip_on_one_line_still_opens_its_card(fit_view, app):
    """`cat:Ship abyssal` and `owner:Main save:` once opened nothing: the
    prefixed chip's label flashed a stray top-level window whose focus round
    trip Qt read as the application deactivating, and closeAllPopups took
    the card with it one turn later. Pinned through the real commit path,
    with the chip and the card request in the same turn, for a filter chip
    and for a command."""
    view = fit_view
    card = card_after_enter(view, app, "cat:Ship abyssal", omni.ABYSSAL_KIND)
    for _ in range(5):
        app.processEvents()
    assert card.isVisible(), "the abyssal card must outlive the chip insertion beside it"
    card.hide()
    app.processEvents()
    save = card_after_enter(view, app, "owner:Main save:", omni.SAVE_COMMAND)
    for _ in range(5):
        app.processEvents()
    assert save.isVisible(), "the Save card must outlive the chip insertion beside it"
    assert [c.kind for c in view.omnibox.spec().chips] == ["category", "abyssal", "owner"]
    save.hide()


def test_fitted_rows_are_hidden_until_the_filter_is_about_ships_fits_or_that_kind(fit_view):
    """The corpus's permuted Dominix carries a launcher in a high slot with
    50 rounds loaded and 300 more in its cargo hold. The everyday view lists
    the cargo stack and neither the launcher nor the loaded rounds -- fifty
    fitted ships would otherwise put fifty racks between the hangar rows --
    and the count agrees with the rows. A filter naming the kind of thing,
    or is:fitted, brings the fitted rows back, since then they are the
    answer."""
    view = fit_view
    launcher, loaded, cargo = 5_200_005, 5_200_006, 5_200_009
    shown = item_ids(view)
    assert cargo in shown and launcher not in shown and loaded not in shown
    assert view._total_stacks == len(view.model.rows()), "the count is of the rows shown"

    view.omnibox.set_spec(omni.parse(f'owner:"{fit_corpus.PILOT_NAME}"'))
    shown = item_ids(view)
    assert cargo in shown and launcher not in shown, "an owner is an everyday filter"

    # A fit: or cat:Ship filter lists ship rows only, so the rule's answer
    # for those never reaches a fitted row; the filters below can show one.
    for text, wanted in (
        ("cat:Charge", loaded), (f'item:"{fit_corpus.AMMO_NAME}"', loaded),
        ("cat:Module", launcher), ("is:fitted", launcher), ("is:fitted", loaded),
    ):
        view.omnibox.set_spec(omni.parse(text))
        assert wanted in item_ids(view), text


def test_the_rail_and_the_empty_hint_follow_the_hide_rule_for_a_bare_word(fit_view):
    """Typing a word that names only fitted modules once left "0 of N stacks"
    with no explanation while the rail still counted six hits it had run
    without the hide rule. Both surfaces read the same query now, and the
    empty state says what was hidden and how to see it."""
    view = fit_view
    view.omnibox.set_spec(omni.parse("Launcher"))
    assert view.model.rows() == []
    assert not view.empty_hint.isHidden()
    assert "fitted row" in view.empty_hint.text() and "is:fitted" in view.empty_hint.text()
    kinds = [r.get("kind") for r in rail_rows_as_dicts(view)]
    assert "flip" not in kinds and "row" not in kinds, (
        "the rail must not list hits the table hides: " + repr(kinds)
    )

    view.omnibox.set_spec(omni.parse("Launcher is:fitted"))
    assert view.model.rows(), "is:fitted reveals the launchers the word names"
    assert view.empty_hint.isHidden()
    kinds = [r.get("kind") for r in rail_rows_as_dicts(view)]
    assert "flip" in kinds or "row" in kinds, "and the rail lists where they are"


def rail_rows_as_dicts(view) -> list[dict]:
    """The rail's rows as plain dicts, tolerant of the row shape."""
    rows = []
    for index in range(view.rail.rows_list.count()):
        item = view.rail.rows_list.item(index)
        data = item.data(Qt.UserRole)
        if isinstance(data, dict):
            rows.append(data)
        else:
            try:
                rows.append(dict(data))
            except Exception:  # noqa: BLE001 - the row payload shape is the rail's own
                rows.append({})
    return rows


def test_is_unpriced_from_the_strip_lists_the_same_rows_the_badge_counts(fit_view):
    """The strip's unpriced badge counts the whole estate, fitted modules
    included; its SHOW button adds is:unpriced, so that chip must not hide
    the fitted rows or the badge says one number and the table another."""
    view = fit_view
    view.omnibox.set_spec(omni.parse("is:unpriced"))
    shown = item_ids(view)
    where, params = omni.parse("is:unpriced").where()
    plain = {r["item_id"] for r in queries.fetch_assets(view.conn, where, params)}
    assert shown == plain, "no unpriced row is hidden under is:unpriced"
    assert 5_200_005 in shown, "the unpriced launcher in a slot is listed"


def test_done_from_the_second_holds_chips_glyph_keeps_the_first(fit_view, app):
    """Two holds chips AND. Opening the card from the second chip's glyph
    once seeded it from the first and Done then dropped the second, so the
    table widened without anyone asking. The clicked chip seeds its card
    and Done replaces only that chip."""
    view = fit_view
    first = f'holds:"cargo/{fit_corpus.AMMO_NAME}<500"'
    second = f'holds:"{fit_corpus.AMMO_NAME}<500"'
    view.omnibox.set_spec(omni.parse(f"{first} {second}"))
    before = item_ids(view)
    app.processEvents()
    chip, widget = [(c, w) for c, w in view.omnibox._chips if c.kind == omni.HOLDS_KIND][1]
    widget.card_btn.click()
    card = view._cards[omni.HOLDS_KIND]
    assert card.isVisible() and card.term().bay is None, "seeded from the clicked chip"
    card.apply()
    assert view.omnibox.spec().to_text() == f"{first} {second}"
    assert item_ids(view) == before


def test_a_quoted_command_word_stays_quoted_when_the_field_is_rewritten(fit_view):
    """`"save:x"` typed in quotes is a search for that text. The commit once
    wrote the remaining text back bare, so a second Enter saved a view
    named x. The write-back goes through to_text, which keeps the quotes."""
    view = fit_view
    commit_text(view, f'owner:"{fit_corpus.PILOT_NAME}" "save:x"')
    assert view.omnibox.edit.text() == '"save:x"'
    commit_text(view, view.omnibox.edit.text())
    assert views.find_view(view.conn, "x") is None, "the second Enter searched; it did not save"


def test_a_footer_notice_outlives_the_reload_it_triggered(fit_view):
    """A confirmation written before reload() was replaced by the selection
    sum when the rows landed, so "Loaded view 'X'." was gone before it was
    read. The notice now holds through exactly one footer refresh."""
    view = fit_view
    view._notice("Loaded view 'Probe'.")
    view._update_footer()
    assert view.footer.text() == "Loaded view 'Probe'."
    view._update_footer()
    assert view.footer.text() != "Loaded view 'Probe'."

