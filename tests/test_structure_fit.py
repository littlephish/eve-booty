"""View fit on a structure.

A ship's fit is "everything whose location_id is the ship" -- that is what
queries.fetch_fit answers, and on a ship it is the right answer. On an Upwell
structure the same question returns the structure's entire contents: every
corp hangar division, every office, everything anybody parked in it. Pointed
at a busy Fortizar that is thousands of rows, and none of them are the fit.

So the structure query is an allowlist of fitting-related location_flags
rather than "everything on this id". Allowlist rather than blocklist on
purpose: a flag CCP adds later is silently absent from the dialog, which is
the safe way to be wrong. A blocklist would leak the whole hangar the day
they rename a division.
"""

from __future__ import annotations

import pytest

from evasset import db, fitting, queries

STRUCTURE = 1035466617946
SYSTEM = 30000142
REGION = 10000002

# Real ids, so nothing here is a made-up type that could not exist.
FORTIZAR = 35833
MARKET_HUB = 35892          # Standup Market Hub I          -- ServiceSlot
MISSILE_LAUNCHER = 35932    # Standup Multirole Missile Launcher I -- HiSlot
ARMOR_RIG = 37260           # Standup L-Set Structure ... -- RigSlot
FUEL_BLOCK = 4051           # Nitrogen Fuel Block -- StructureFuel
CLONE_BAY = 35894           # Standup Clone Bay I           -- ServiceSlot
QUANTUM_CORE = 56201        # Upwell Quantum Core -- QuantumCoreRoom
TRITANIUM = 34              # in a corp hangar division -- not part of the fit


def add(conn, item_id, type_id, flag, quantity=1, location_id=STRUCTURE):
    conn.execute(
        "INSERT INTO assets (owner_type, owner_id, item_id, type_id, quantity,"
        " location_id, location_flag, location_type, is_singleton,"
        " is_blueprint_copy, custom_name, root_location_id, system_id, region_id)"
        " VALUES ('corporation',500,?,?,?,?,?,'item',1,0,NULL,?,?,?)",
        (item_id, type_id, quantity, location_id, flag, STRUCTURE, SYSTEM, REGION),
    )


@pytest.fixture()
def conn(tmp_path):
    c = db.init(tmp_path / "sf.sqlite")
    c.executescript(
        """
        INSERT INTO sde_categories VALUES (65,'Structure',1),(66,'Structure Module',1),
                                          (4,'Material',1),(43,'Celestial',1);
        INSERT INTO sde_groups VALUES (1657,65,'Citadel',1),
                                      (1321,66,'Structure Service Module',1),
                                      (1322,66,'Structure Weapon',1),
                                      (1852,66,'Structure Rig',1),
                                      (1136,4,'Fuel Block',1),
                                      (4030,43,'Quantum Core',1),
                                      (18,4,'Mineral',1);
        INSERT INTO sde_types (type_id,name,group_id,volume,portion_size,base_price,published)
          VALUES (35833,'Fortizar',1657,800000,1,1,1),
                 (35892,'Standup Market Hub I',1321,4000,1,1,1),
                 (35894,'Standup Clone Bay I',1321,4000,1,1,1),
                 (35932,'Standup Multirole Missile Launcher I',1322,4000,1,1,1),
                 (37260,'Standup L-Set Hull Reinforcement I',1852,10,1,1,1),
                 (4051,'Nitrogen Fuel Block',1136,5,1,1,1),
                 (56201,'Upwell Quantum Core',4030,100,1,1,1),
                 (34,'Tritanium',18,0.01,1,5,1);
        INSERT INTO sde_systems VALUES (30000142,'Jita',20000020,10000002,0.9);
        INSERT INTO sde_regions VALUES (10000002,'The Forge');
        """
    )
    add(c, 1, MARKET_HUB, "ServiceSlot0")
    add(c, 2, MISSILE_LAUNCHER, "HiSlot0")
    add(c, 3, ARMOR_RIG, "RigSlot0")
    add(c, 4, FUEL_BLOCK, "StructureFuel", quantity=5000)
    add(c, 5, QUANTUM_CORE, "QuantumCoreRoom")
    add(c, 6, TRITANIUM, "CorpSAG1", quantity=1_000_000)
    c.execute(
        "INSERT INTO structures (structure_id, name, system_id, region_id, type_id,"
        " owner_id, owned, accessible) VALUES (?,?,?,?,?,?,1,1)",
        (STRUCTURE, "Jita Fortizar", SYSTEM, REGION, FORTIZAR, 500),
    )
    return c


def flags(rows) -> set[str]:
    return {r["location_flag"] for r in rows}


def test_the_corp_hangar_is_not_part_of_the_fit(conn):
    """The whole reason this query exists rather than reusing fetch_fit."""
    rows = queries.fetch_structure_fit(conn, STRUCTURE)
    assert "CorpSAG1" not in flags(rows)


def test_service_modules_are_part_of_the_fit(conn):
    rows = queries.fetch_structure_fit(conn, STRUCTURE)
    assert "ServiceSlot0" in flags(rows)


def test_the_fuel_bay_is_part_of_the_fit(conn):
    """The Structures tab says when the fuel runs out; only this says what is
    burning."""
    rows = queries.fetch_structure_fit(conn, STRUCTURE)
    assert "StructureFuel" in flags(rows)


def test_the_quantum_core_is_part_of_the_fit(conn):
    """It lives in the fitting window in game, and a structure without one
    does not work."""
    rows = queries.fetch_structure_fit(conn, STRUCTURE)
    assert "QuantumCoreRoom" in flags(rows)


def test_slots_and_rigs_are_part_of_the_fit(conn):
    rows = queries.fetch_structure_fit(conn, STRUCTURE)
    assert {"HiSlot0", "RigSlot0"} <= flags(rows)


def test_an_unknown_flag_is_left_out_rather_than_guessed(conn):
    """Allowlist, not blocklist: something new shows up absent instead of
    dragging a hangar in behind it."""
    add(conn, 7, TRITANIUM, "SomeFlagCCPAddedLater")
    rows = queries.fetch_structure_fit(conn, STRUCTURE)
    assert "SomeFlagCCPAddedLater" not in flags(rows)


def test_the_rows_carry_what_the_dialog_renders(conn):
    """Same column shape as fetch_fit, so FitDialog and fitting.group_fit do
    not need to know which query produced the rows."""
    rows = queries.fetch_structure_fit(conn, STRUCTURE)
    row = next(r for r in rows if r["location_flag"] == "ServiceSlot0")
    assert row["item"] == "Standup Market Hub I"
    assert row["type_id"] == MARKET_HUB
    assert row["category"] == "Structure Module"


def test_another_structures_fit_is_not_included(conn):
    """Parameterized by this structure's id, the way fetch_fit is."""
    add(conn, 8, MARKET_HUB, "ServiceSlot0", location_id=STRUCTURE + 1)
    rows = queries.fetch_structure_fit(conn, STRUCTURE)
    assert [r["item_id"] for r in rows if r["item_id"] == 8] == []


# ------------------------------------------------------- grouping the fit
# fitting.group_fit already knows the ship slot vocabulary, which structures
# share. What it does not know is ServiceSlot, and the failure mode is quiet
# rather than loud: _humanize_flag turns each occupied service slot into its
# own group, so a Fortizar with five services renders five one-line sections
# headed "Service Slot0" ... "Service Slot4" instead of one rack.
def groups_for(conn):
    return dict(fitting.group_fit(queries.fetch_structure_fit(conn, STRUCTURE)))


def test_service_modules_share_one_rack(conn):
    add(conn, 10, CLONE_BAY, "ServiceSlot1")
    groups = groups_for(conn)
    assert [line.text for line in groups["Service slots"]] == [
        "Standup Market Hub I",
        "Standup Clone Bay I",
    ]


def test_service_slots_are_not_one_group_each(conn):
    add(conn, 10, CLONE_BAY, "ServiceSlot1")
    assert not [label for label in groups_for(conn) if label.startswith("Service Slot")]


def test_the_fuel_bay_is_labelled_as_one(conn):
    groups = groups_for(conn)
    assert [line.text for line in groups["Fuel bay"]] == ["5,000 x Nitrogen Fuel Block"]


def test_the_quantum_core_is_labelled_as_one(conn):
    groups = groups_for(conn)
    assert [line.text for line in groups["Quantum core"]] == ["1 x Upwell Quantum Core"]


def test_slots_come_out_in_fitting_window_order(conn):
    labels = [label for label, _ in fitting.group_fit(queries.fetch_structure_fit(conn, STRUCTURE))]
    assert labels.index("High slots") < labels.index("Rig slots")
    assert labels.index("Rig slots") < labels.index("Service slots")


# ------------------------------------------------------------- EFT export
# to_esi_fitting already handles service slots -- _ESI_SLOT_BASES carries
# "ServiceSlot": 164. to_eft does not, and would drop every service module
# without saying so, which is the exact silent omission the rest of that
# module goes out of its way to avoid.
def test_eft_export_keeps_service_modules(conn):
    text = fitting.to_eft("Fortizar", queries.fetch_structure_fit(conn, STRUCTURE))
    assert "Standup Market Hub I" in text


def test_eft_export_puts_services_last_like_pyfa(conn):
    """Pyfa's own slot export order ends with Slot.SERVICE (service/port/eft.py),
    after low, med, high, rig and subsystem."""
    text = fitting.to_eft("Fortizar", queries.fetch_structure_fit(conn, STRUCTURE))
    assert text.index("Standup Multirole Missile Launcher I") < text.index(
        "Standup Market Hub I"
    )


def test_eft_export_leaves_out_the_fuel_bay(conn):
    """EFT has no syntax for a structure's fuel bay, the same way it has none
    for an ore hold."""
    text = fitting.to_eft("Fortizar", queries.fetch_structure_fit(conn, STRUCTURE))
    assert "Nitrogen Fuel Block" not in text


# ------------------------------------------------------------ the dialog
def test_the_structures_query_carries_the_type_id(conn):
    """FitDialog draws the hull's icon from the type id. STRUCTURES_SQL used
    to select only the type's *name*, which is all the table column needed."""
    row = queries.fetch_structures(conn)[0]
    assert row["type_id"] == FORTIZAR


def test_the_dialog_says_what_is_empty_in_its_own_words(qapp_or_skip, conn):
    """"Nothing fit, loaded or stowed on this ship" is the wrong sentence to
    show somebody who right-clicked a Fortizar."""
    from evasset.ui.fit_dialog import FitDialog

    dialog = FitDialog(
        STRUCTURE,
        "Jita Fortizar",
        ship_type_id=FORTIZAR,
        empty_text="Nothing fitted on this structure.",
    )
    dialog.pane._query.cancel()  # drop whatever the constructor started
    dialog.pane._on_rows([])
    labels = dialog.pane.body.findChildren(__import__("PySide6.QtWidgets", fromlist=["QLabel"]).QLabel)
    assert any("structure" in label.text() for label in labels)


# ------------------------------------------------------- the context menu
# Built as a menu rather than exec'd in one step, the same way
# treemap_view.menu_for_tile is, so the entries can be asserted without a
# modal event loop.
def test_the_structures_table_offers_view_fit(qapp_or_skip, conn):
    from evasset.ui.structures_view import StructuresView

    view = StructuresView(defer_load=True)
    row = queries.fetch_structures(conn)[0]
    menu = view.menu_for_structure(row)
    assert "View fit…" in [a.text() for a in menu.actions()]


def test_view_fit_opens_the_structure_that_was_clicked(qapp_or_skip, conn, monkeypatch):
    from evasset.ui import structures_view

    opened = {}

    class FakeDialog:
        def __init__(self, structure_id, name, parent=None, **kw):
            opened.update(structure_id=structure_id, name=name, tab=kw.get("tab"))

        def exec(self):
            return 0

    monkeypatch.setattr(structures_view, "StructureDialog", FakeDialog)
    view = structures_view.StructuresView(defer_load=True)
    view.open_structure(queries.fetch_structures(conn)[0], tab="Fit")
    assert opened["structure_id"] == STRUCTURE
    assert opened["name"] == "Jita Fortizar"
    assert opened["tab"] == "Fit"


def test_opening_a_structure_lands_on_the_overview(qapp_or_skip, conn, monkeypatch):
    from evasset.ui import structures_view

    opened = {}

    class FakeDialog:
        def __init__(self, structure_id, name, parent=None, **kw):
            opened.update(tab=kw.get("tab"))

        def exec(self):
            return 0

    monkeypatch.setattr(structures_view, "StructureDialog", FakeDialog)
    view = structures_view.StructuresView(defer_load=True)
    view.open_structure(queries.fetch_structures(conn)[0])
    assert opened["tab"] == "Overview"


def test_the_fit_tab_is_given_the_structure_query_not_the_ship_one(qapp_or_skip, conn):
    """The whole point: pointed at a structure with fetch_fit it would show
    the corp hangar. Asserted on the dialog itself rather than on how the view
    calls it, because the dialog is what decides now."""
    from evasset.ui.structure_dialog import StructureDialog

    dialog = StructureDialog(STRUCTURE, "Jita Fortizar", defer_load=True)
    dialog.fit._query.cancel()
    rows = dialog.fit._fetch(conn)
    assert [r["location_flag"] for r in rows] == [
        r["location_flag"] for r in queries.fetch_structure_fit(conn, STRUCTURE)
    ]
