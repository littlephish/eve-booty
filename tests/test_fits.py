"""The EFT parser, the fit store, the diff and the ship-scoped queries.

All without a window: everything the holds and fit chips rest on is Qt-free
by design, so the awkward cases (a paste out of Notepad, a category row the
SDE never imported, a container in the cargo hold) are cheap to pin here
rather than through a rendered table.
"""

from __future__ import annotations

import sqlite3

import fit_corpus as fc
import pytest

from evasset import db, fits, fitting, queries

# A clipboard BOM, written by name rather than as the character itself:
# an editor that renders it invisibly is one save away from losing it.
BOM = "\N{ZERO WIDTH NO-BREAK SPACE}"


@pytest.fixture()
def conn(tmp_path):
    """The synthetic estate of tests/fit_corpus.py, three stored fits and all."""
    c = db.init(tmp_path / "fits.sqlite")
    fc.install(c)
    return c


def _fit_id(conn, name: str) -> int:
    return int(next(r["fit_id"] for r in fits.list_fits(conn) if r["name"] == name))


# ------------------------------------------------------------------ schema
def test_a_v5_database_gains_the_fit_tables_and_the_new_version(tmp_path):
    """Both fit tables are brand new, so CREATE TABLE IF NOT EXISTS is the
    whole migration -- but only if SCHEMA and migrate() were kept in step and
    the version was actually bumped. A v5 database is the shape every
    existing install is in, and it must come out of init() with the tables
    present, its existing rows untouched, and the current schema_version.

    The version is read from db.SCHEMA_VERSION rather than pinned to a
    literal: this test is about the fit tables arriving, and a later bump
    (v7's views library was the first) should not have to edit it."""
    v5 = db.SCHEMA
    for table in ("fits", "fit_items"):
        start = v5.index(f"CREATE TABLE IF NOT EXISTS {table} (")
        end = v5.index(");", start) + len(");")
        v5 = v5[:start] + v5[end:]
    assert "CREATE TABLE IF NOT EXISTS fits (" not in v5

    path = tmp_path / "v5.sqlite"
    old = sqlite3.connect(path)
    old.executescript(v5)
    old.execute("INSERT INTO meta VALUES ('schema_version', '5')")
    old.execute("INSERT INTO pinned_labels VALUES ('location', 'Jita IV - Moon 4')")
    old.commit()
    old.close()

    conn = db.init(path)
    assert db.get_meta(conn, "schema_version") == str(db.SCHEMA_VERSION)
    for table in ("fits", "fit_items"):
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone(), table
    assert conn.execute("SELECT COUNT(*) FROM pinned_labels").fetchone()[0] == 1


def test_a_fresh_database_can_store_a_fit_and_cascade_its_items_away(tmp_path):
    """The cascade is the whole reason save_fit can replace a fit by deleting
    it first, and it only works because connect() sets PRAGMA
    foreign_keys=ON -- a pragma that is off by default in SQLite."""
    conn = db.init(tmp_path / "fresh.sqlite")
    fc.install(conn)
    fit_id = _fit_id(conn, fc.RATTING)
    assert conn.execute(
        "SELECT COUNT(*) FROM fit_items WHERE fit_id = ?", (fit_id,)
    ).fetchone()[0] > 0
    conn.execute("DELETE FROM fits WHERE fit_id = ?", (fit_id,))
    assert conn.execute(
        "SELECT COUNT(*) FROM fit_items WHERE fit_id = ?", (fit_id,)
    ).fetchone()[0] == 0


# --------------------------------------------------------- flag vocabulary
def test_the_fitted_and_bay_flag_constants_cover_the_slots_and_nothing_else():
    """The SQL and the Python read the same vocabulary; if these drifted, a
    ship's rack would mean one thing to fitting.fitted_modules and another to
    queries.FIT_EQUAL, and the inspector would show an empty difference
    beside a "Deviates" verdict."""
    slot_flags = [flag for _label, flags in fitting._SLOT_GROUPS for flag in flags]
    assert list(fitting.FITTED_FLAGS) == slot_flags
    assert len(fitting.FITTED_FLAGS) == 40, "five racks of eight"
    for prefix in ("HiSlot", "MedSlot", "LoSlot", "RigSlot", "SubSystemSlot"):
        assert f"{prefix}0" in fitting.FITTED_FLAGS and f"{prefix}7" in fitting.FITTED_FLAGS

    assert fitting.CARGO_FLAGS == ("Cargo",)
    assert fitting.FUEL_FLAGS == ("SpecializedFuelBay", "SpecializedAmmoHold")
    assert fitting.DRONE_BAY_FLAGS == ("DroneBay",)
    assert fitting.FIGHTER_FLAGS == (
        "FighterBay", "FighterTube0", "FighterTube1", "FighterTube2", "FighterTube3",
        "FighterTube4",
    )
    assert fitting.FLEET_HANGAR_FLAGS == ("FleetHangar",)
    # The card's selector order after "All" is this dict's order.
    assert list(fitting.HOLD_BAYS) == ["cargo", "fuel", "drones", "fighters", "fleet"]
    assert fitting.HOLD_BAYS["drones"] == fitting.DRONE_BAY_FLAGS
    assert fitting.HOLD_BAYS["fighters"] == fitting.FIGHTER_FLAGS
    assert fitting.HOLD_BAYS["fleet"] == fitting.FLEET_HANGAR_FLAGS
    bay_flags = [f for flags in fitting.HOLD_BAYS.values() for f in flags]
    assert len(bay_flags) == len(set(bay_flags)), "no flag counted under two bays"
    assert not set(bay_flags) & set(fitting.FITTED_FLAGS), (
        "a loaded charge sits on a slot flag and belongs to no bay"
    )
    # The fit comparison's drone bucket stays the wide set -- a fit's drone
    # line is met by anything that launches from anywhere -- and is no bay
    # of the grammar's, which would otherwise count a fleet hangar's drones
    # under drones/ again.
    assert fitting.DRONE_FLAGS == (
        *fitting.DRONE_BAY_FLAGS, *fitting.FIGHTER_FLAGS, *fitting.FLEET_HANGAR_FLAGS
    )
    assert fitting.DRONE_FLAGS not in fitting.HOLD_BAYS.values()


def test_fitted_modules_agrees_with_the_sql_multiset_for_every_corpus_ship(conn):
    """The Python twin and the SQL are two implementations of one rule, and
    the diff would quietly contradict the verdict if they disagreed. Checked
    over every ship in the corpus, including the one carrying a module whose
    category row is missing and the one with a charge loaded in a launcher."""
    checked = 0
    for ship in sorted(fc.ASSEMBLED_SHIPS):
        rows = queries.fetch_fit(conn, ship)
        expected = {
            int(r["type_id"]): int(r["units"])
            for r in conn.execute(
                f"""SELECT fm.type_id, SUM(fm.quantity) AS units
                    FROM assets fm
                    JOIN sde_types fmt ON fmt.type_id = fm.type_id
                    LEFT JOIN sde_groups fmg ON fmg.group_id = fmt.group_id
                    LEFT JOIN sde_categories fmc ON fmc.category_id = fmg.category_id
                    WHERE fm.location_id = ?
                      AND fm.location_flag IN ({queries._sql_flags(fitting.FITTED_FLAGS)})
                      AND COALESCE(fmc.name, '') <> 'Charge'
                    GROUP BY fm.type_id""",
                (ship,),
            )
        }
        assert fitting.fitted_modules(rows) == expected, ship
        checked += 1
    assert checked == 10, "every assembled ship in the corpus must have been compared"
    # The comparison would be vacuous if every ship were empty.
    assert fitting.fitted_modules(queries.fetch_fit(conn, fc.SHIP_EXACT))
    assert fitting.fitted_modules(queries.fetch_fit(conn, fc.SHIP_EMPTY)) == {}


def test_a_loaded_charge_is_not_a_fitted_module_and_a_null_category_one_is(conn):
    """The two ends of the category rule. A charge shares its launcher's slot
    flag, so only the category tells them apart; a type whose group row never
    made it into the SDE has NO category, and dropping it would shrink the
    rack and turn a deviating ship into a matching one."""
    permuted = fitting.fitted_modules(queries.fetch_fit(conn, fc.SHIP_PERMUTED))
    assert fc.AMMO not in permuted, "the 50 rounds in HiSlot0 are not a module"
    assert permuted[fc.LAUNCHER] == 1
    null_cat = fitting.fitted_modules(queries.fetch_fit(conn, fc.SHIP_NULL_CATEGORY))
    assert null_cat[fc.ORPHAN_MODULE] == 1
    assert conn.execute(
        "SELECT category_id FROM sde_groups WHERE group_id = ?", (fc.GRP_ORPHAN,)
    ).fetchone() is None, "the fixture's trap must really be a missing group"


# ------------------------------------------------------------- EFT parsing
def test_parsing_the_corpus_fit_yields_the_hand_counted_multiset(conn):
    parsed = fits.parse_eft(conn, fc.RATTING_EFT)
    assert parsed.ok
    assert (parsed.hull_type_id, parsed.hull_name, parsed.name) == (
        fc.DOMINIX, "Dominix", "Ratting"
    )
    assert parsed.empty_slots == 1, "the [Empty Med slot] placeholder"
    assert parsed.charges == {fc.AMMO: 1}, "the launcher's loaded round, no quantity"
    verdict = {(i.type_id, i.slot_kind): i.quantity for i in parsed.verdict_items()}
    assert verdict == {
        (fc.ARMOR_REPAIRER, "module"): 1,
        (fc.FIELD_AMPLIFIER, "module"): 3,
        (fc.PULSE_LASER, "module"): 1,
        (fc.LAUNCHER, "module"): 1,
        (fc.NANO_PUMP, "rig"): 1,
    }
    consumables = {(i.type_id, i.slot_kind): i.quantity for i in parsed.consumable_items()}
    assert consumables == {(fc.DRONE, "drone"): 5, (fc.AMMO, "cargo"): 100}


def test_an_exported_fit_parses_back_into_a_fit_its_own_ship_matches(conn):
    """The round trip that matters: export a ship with fitting.to_eft, paste
    the result back in, store it, and the ship it came from must match. Any
    disagreement between the exporter's line shapes and the parser's rules
    shows up here rather than as a user's fit that never matches anything."""
    rows = queries.fetch_fit(conn, fc.SHIP_EXACT)
    text = fitting.to_eft("Dominix", rows)
    parsed = fits.parse_eft(conn, text)
    assert parsed.ok, parsed.unknown
    assert fits.save_fit(conn, parsed, "Exported", text)
    assert fits.diff_for_ship(conn, fc.SHIP_EXACT, fc.DOMINIX, "Exported").matches
    # And a ship one module short of it does not.
    assert not fits.diff_for_ship(conn, fc.SHIP_MISSING, fc.DOMINIX, "Exported").matches


def test_every_awkward_paste_shape_parses_the_way_the_grammar_promises(conn):
    """The adversarial list, one case per line: a clipboard's BOM and CRLF,
    trailing spaces, an offline module, comment lines, a stack whose
    quantity carries thousands separators (read as the number it obviously
    is, where the first build refused it), a malformed separator that still
    falls through, duplicate lines aggregating, and a header with no name at
    all."""
    parsed = fits.parse_eft(conn, BOM + "[Dominix, Ratting]\r\n\r\nFocused Pulse Laser  \r\n")
    assert parsed.ok and parsed.hull_type_id == fc.DOMINIX
    assert [(i.type_id, i.quantity) for i in parsed.items] == [(fc.PULSE_LASER, 1)]

    offline = fits.parse_eft(conn, "[Dominix, X]\nFocused Pulse Laser /OFFLINE")
    assert [(i.type_id, i.slot_kind, i.quantity) for i in offline.items] == [
        (fc.PULSE_LASER, "module", 1)
    ]

    commented = fits.parse_eft(
        conn, "[Dominix, X]\n// a note\n# another\nFocused Pulse Laser"
    )
    assert len(commented.items) == 1 and not commented.unknown

    thousands = fits.parse_eft(
        conn, "[Dominix, X]\nAntimatter Charge M x1,000\nScarab Combat Drone x12,500"
    )
    assert thousands.ok and thousands.unknown == [], (
        "a count written with thousands separators is the number it obviously "
        "is; the first build refused it, and `Scarab Combat Drone x1,000` read "
        "as an unknown module `... x1` plus an unknown charge `000`"
    )
    assert sorted((i.type_id, i.quantity) for i in thousands.items) == sorted(
        [(fc.AMMO, 1000), (fc.DRONE, 12500)]
    )
    malformed = fits.parse_eft(conn, "[Dominix, X]\nAntimatter Charge M x1,00")
    assert malformed.unknown == ["Antimatter Charge M x1", "00"] and not malformed.ok, (
        "only well-formed groups of three are a count; anything else still "
        "falls through to the `Module, Charge` shape, where the unknown names "
        "block the save with the text on screen"
    )
    zero = fits.parse_eft(conn, "[Dominix, X]\nAntimatter Charge M x0\nFocused Pulse Laser")
    assert [(i.type_id, i.quantity) for i in zero.items] == [(fc.PULSE_LASER, 1)], (
        "a stack of nought is a row that says nothing: kept, it would sit in "
        "the fit as an item no ship can ever be short of while still counting "
        "towards the card's 'N cargo types' summary"
    )
    assert zero.unknown == [] and zero.ok, "dropping the row is not an error"

    plain = fits.parse_eft(conn, "[Dominix, X]\nAntimatter Charge M x1000")
    assert [(i.type_id, i.slot_kind, i.quantity) for i in plain.items] == [
        (fc.AMMO, "cargo", 1000)
    ]

    duplicated = fits.parse_eft(
        conn, "[Dominix, X]\nMagnetic Field Amplifier\nMagnetic Field Amplifier"
    )
    assert [(i.type_id, i.quantity) for i in duplicated.items] == [(fc.FIELD_AMPLIFIER, 2)]

    nameless = fits.parse_eft(conn, "[Dominix, ]\nFocused Pulse Laser")
    assert nameless.name == "Dominix fit", "an unnamed fit still needs something to address"
    assert nameless.ok


def test_a_paste_with_no_usable_header_cannot_be_saved(conn):
    """Without a hull there is nothing to compare the fit against, so the
    parse succeeds, the items are kept for the card to show, and ok is
    False -- the card's "No [Hull, Name] header" state."""
    headerless = fits.parse_eft(conn, "Focused Pulse Laser\nAuxiliary Nano Pump")
    assert headerless.hull_type_id is None and headerless.hull_name == ""
    assert len(headerless.items) == 2, "the lines are still parsed, not discarded"
    assert not headerless.ok
    with pytest.raises(ValueError):
        fits.save_fit(conn, headerless, "Nope", "x")

    unknown_hull = fits.parse_eft(conn, "[Frobnicator, X]\nFocused Pulse Laser")
    assert unknown_hull.hull_type_id is None
    assert unknown_hull.hull_name == "Frobnicator", "kept as typed, so the card can say so"
    assert not unknown_hull.ok

    empty = fits.parse_eft(conn, "")
    assert (empty.items, empty.unknown, empty.ok) == ([], [], False)

    only_empties = fits.parse_eft(conn, "[Dominix, X]\n[Empty High slot]\n[Empty Rig slot]")
    assert only_empties.empty_slots == 2 and only_empties.items == [] and only_empties.ok


def test_a_header_naming_a_type_that_is_not_a_ship_is_refused_as_a_hull(conn):
    """Every `fit:` clause is scoped by queries.SHIP_ROWS_CLAUSE, so a fit
    stored against a module -- what a paste that lost its first line looks
    like -- could never match a row. Refusing the hull makes the card say so
    instead of saving a fit that silently answers nothing."""
    parsed = fits.parse_eft(conn, "[Focused Pulse Laser, T]\nAuxiliary Nano Pump")
    assert parsed.hull_type_id is None and not parsed.ok
    assert parsed.hull_name == "Focused Pulse Laser", "kept, so the card can name it back"
    with pytest.raises(ValueError):
        fits.save_fit(conn, parsed, "T", "x")

    ship = fits.parse_eft(conn, "[Dominix, T]\nAuxiliary Nano Pump")
    assert ship.hull_type_id == fc.DOMINIX and ship.ok, "a real hull still passes"


def test_an_unknown_item_or_charge_name_is_recorded_and_blocks_the_save(conn):
    """An unknown name is never dropped: a fit stored a line short would
    report every ship carrying that module as deviating, an answer that
    looks authoritative and is wrong."""
    parsed = fits.parse_eft(conn, "[Dominix, X]\nFocused Pulse Laser\nWidget Of Doom")
    assert parsed.unknown == ["Widget Of Doom"]
    assert not parsed.ok
    with pytest.raises(ValueError):
        fits.save_fit(conn, parsed, "X", "x")
    charged = fits.parse_eft(conn, "[Dominix, X]\nRocket Launcher Array, Mystery Round")
    assert charged.unknown == ["Mystery Round"]
    assert [i.type_id for i in charged.items] == [fc.LAUNCHER], "the module still parsed"


def test_a_name_resolves_to_the_published_type_when_the_sde_holds_a_duplicate(conn):
    """CCP leaves retired duplicates of a name in the SDE. A paste means the
    one that can still be fitted, so published wins and the type id only
    breaks a remaining tie."""
    conn.execute(
        "INSERT INTO sde_types (type_id, name, group_id, volume, portion_size, published) "
        "VALUES (?,?,?,?,1,0)",
        (900_401, "Focused Pulse Laser", fc.GRP_ENERGY_TURRET, 5),
    )
    parsed = fits.parse_eft(conn, "[Dominix, X]\nFocused Pulse Laser")
    assert [i.type_id for i in parsed.items] == [fc.PULSE_LASER]
    assert fc.PULSE_LASER < 900_401, "and it is not simply the lower id winning"


def test_classify_reads_the_category_id_and_the_rig_group_name(conn):
    """A rig is category Module like every other module, so only the group
    name says otherwise; a drone and a fighter are both "drone" because EFT
    writes both as stacks and the consumables line treats them alike."""
    cases = [
        (fc.PULSE_LASER, fc.GRP_ENERGY_TURRET, False, "module"),
        (fc.NANO_PUMP, fc.GRP_RIG_ARMOR, False, "rig"),
        (fc.CORE_MATRIX, fc.GRP_DEFENSIVE_SUBSYSTEM, False, "subsystem"),
        (fc.DRONE, fc.GRP_COMBAT_DRONE, True, "drone"),
        (fc.FIGHTER, fc.GRP_LIGHT_FIGHTER, True, "drone"),
        (fc.AMMO, fc.GRP_HYBRID_CHARGE, True, "cargo"),
        (fc.PASTE, fc.GRP_NANITE_COMPOUND, True, "cargo"),
        # A type the SDE cannot place is kept, as cargo or as a module.
        (fc.ORPHAN_MODULE, fc.GRP_ORPHAN, False, "module"),
        (fc.ORPHAN_MODULE, fc.GRP_ORPHAN, True, "cargo"),
        (fc.ORPHAN_MODULE, None, False, "module"),
        # A drone line that is NOT a stack is a fitted module by the line
        # shape, which is what EFT means by putting it in a slot rack.
        (fc.DRONE, fc.GRP_COMBAT_DRONE, False, "module"),
    ]
    seen = 0
    for type_id, group_id, stacked, expected in cases:
        assert fits.classify(conn, type_id, group_id, stacked) == expected, (type_id, stacked)
        seen += 1
    assert seen == len(cases) == 11
    assert set(fits.SLOT_KINDS) == {"module", "rig", "subsystem", "drone", "cargo"}
    assert fits.VERDICT_KINDS == ("module", "rig", "subsystem")


# --------------------------------------------------------------- the store
def test_saving_over_an_existing_name_on_the_same_hull_replaces_it(conn):
    """Pasting a revised rack under a name that is already taken is an edit,
    not an IntegrityError surfacing in the card. The old items must go with
    the old row -- a fit carrying both racks would match nothing."""
    before = len(fits.list_fits(conn))
    text = "[Dominix, Ratting]\nFocused Pulse Laser"
    new_id = fits.save_fit(conn, fits.parse_eft(conn, text), fc.RATTING, text)
    assert len(fits.list_fits(conn)) == before, "replaced, not added"
    assert [(r["type_id"], r["quantity"]) for r in fits.fit_items(conn, new_id)] == [
        (fc.PULSE_LASER, 1)
    ]
    # The name matches case-insensitively, since the column collates NOCASE.
    fits.save_fit(conn, fits.parse_eft(conn, text), "RATTING", text)
    assert len(fits.list_fits(conn)) == before
    # The same name on a different hull is a different fit.
    hauling = "[Charon, Ratting]\nExpanded Cargo Bay"
    fits.save_fit(conn, fits.parse_eft(conn, hauling), fc.RATTING, hauling)
    assert len(fits.list_fits(conn)) == before + 1


def test_a_blank_name_falls_back_to_the_one_in_the_header(conn):
    """A fit with no name could not be addressed by a chip at all, so the
    header's name (never blank for a fit that parses) stands in rather than
    the save failing in front of the user."""
    text = "[Dominix, Solo Two]\nFocused Pulse Laser"
    fit_id = fits.save_fit(conn, fits.parse_eft(conn, text), "   ", text)
    assert next(r["name"] for r in fits.list_fits(conn) if r["fit_id"] == fit_id) == "Solo Two"


def test_deleting_a_fit_takes_its_items_with_it(conn):
    fit_id = _fit_id(conn, fc.SOLO)
    fits.delete_fit(conn, fit_id)
    assert [r["name"] for r in fits.list_fits(conn)] == [fc.HAULING, fc.RATTING]
    assert conn.execute(
        "SELECT COUNT(*) FROM fit_items WHERE fit_id = ?", (fit_id,)
    ).fetchone()[0] == 0


def test_list_fits_orders_by_hull_then_name_and_counts_only_the_verdict_items(conn):
    """modules is what the card shows beside the name, and it must be the
    size of the multiset the verdict is decided from -- Ratting's 7 modules
    and rigs, not the 112 rounds and drones it also carries."""
    rows = [(r["hull"], r["name"], r["modules"]) for r in fits.list_fits(conn)]
    assert rows == [("Charon", fc.HAULING, 2), ("Dominix", fc.RATTING, 7), ("Dominix", fc.SOLO, 3)]
    hull_rows = [(r["name"], r["modules"]) for r in fits.fits_for_hull(conn, fc.DOMINIX)]
    assert hull_rows == [(fc.RATTING, 7), (fc.SOLO, 3)]
    assert fits.fits_for_hull(conn, fc.SOLSTICE) == []
    items = fits.fit_items(conn, _fit_id(conn, fc.RATTING))
    assert sum(i["quantity"] for i in items if i["slot_kind"] in fits.VERDICT_KINDS) == 7
    assert {i["name"] for i in items} >= {fc.AMMO_NAME, fc.DRONE_NAME}


def test_a_hostile_fit_name_travels_as_a_parameter(conn):
    """Fit names are user input and reach the store and the chip's SQL; a
    name carrying a quote must be stored and found, not executed."""
    hostile = "x' OR 1=1 --"
    text = f"[Dominix, {hostile}]\nFocused Pulse Laser"
    fit_id = fits.save_fit(conn, fits.parse_eft(conn, text), hostile, text)
    assert next(r["name"] for r in fits.list_fits(conn) if r["fit_id"] == fit_id) == hostile
    assert fits.diff_for_ship(conn, fc.SHIP_SOLO, fc.DOMINIX, hostile).fit_id == fit_id


# ---------------------------------------------------------------- the diff
def test_the_diff_reports_hand_counted_missing_extra_and_short_lines(conn):
    """Each corpus ship exists for one of these answers, so the expectations
    are written out rather than derived -- a derived expectation would agree
    with a wrong diff."""
    ratting = _fit_id(conn, fc.RATTING)
    items = fits.fit_items(conn, ratting)

    exact = fits.diff(queries.fetch_fit(conn, fc.SHIP_EXACT), items)
    assert exact == ([], [], []), "the ship the fit was written for"

    permuted = fits.diff(queries.fetch_fit(conn, fc.SHIP_PERMUTED), items)
    assert permuted[:2] == ([], []), "slot order and a loaded charge change nothing"
    assert permuted[2] == [(fc.DRONE_NAME, 0, 5)], "but it carries no drones"

    missing, extra, _short = fits.diff(queries.fetch_fit(conn, fc.SHIP_MISSING), items)
    assert (missing, extra) == ([("Auxiliary Nano Pump", 1)], [])

    missing, extra, _short = fits.diff(queries.fetch_fit(conn, fc.SHIP_EXTRA), items)
    assert (missing, extra) == ([], [("Capacitor Flux Coil", 1)])

    missing, extra, _short = fits.diff(queries.fetch_fit(conn, fc.SHIP_DUPES), items)
    assert (missing, extra) == ([("Magnetic Field Amplifier", 1)], []), "count, not type"

    missing, extra, _short = fits.diff(queries.fetch_fit(conn, fc.SHIP_NULL_CATEGORY), items)
    assert (missing, extra) == ([], [("Unlisted Widget", 1)])

    missing, extra, short = fits.diff(queries.fetch_fit(conn, fc.SHIP_EMPTY), items)
    assert extra == [] and len(missing) == 5
    assert sum(n for _name, n in missing) == 7
    assert short == [(fc.AMMO_NAME, 0, 100), (fc.DRONE_NAME, 0, 5)]


def test_short_counts_a_consumable_anywhere_inside_the_ship(conn):
    """A pilot asking whether they have 2,000 rounds does not care which
    hold the rounds are in -- cargo, the ammo hold, or already loaded in a
    launcher all count. The same count feeds the window's Cargo section, so
    the two can never say different figures."""
    text = f"[Dominix, Deep]\nFocused Pulse Laser\n{fc.AMMO_NAME} x700"
    fits.save_fit(conn, fits.parse_eft(conn, text), "Deep", text)
    items = fits.fit_items(conn, _fit_id(conn, "Deep"))
    # The permuted ship holds 300 in cargo, 200 in the ammo hold, 100 in the
    # fuel bay and 50 loaded: 650 of the 700 wanted, and the 1,000 inside
    # the crate in its cargo hold are not its own.
    assert fits.diff(queries.fetch_fit(conn, fc.SHIP_PERMUTED), items)[2] == [
        (fc.AMMO_NAME, 650, 700)
    ]
    aboard = fitting.carried(queries.fetch_fit(conn, fc.SHIP_PERMUTED))
    assert aboard.cargo[fc.AMMO] == 650 and fc.AMMO not in aboard.drones
    assert aboard.cargo[fc.CRATE] == 1, "the crate itself is cargo; its contents are not"


def test_diff_for_ship_prefers_the_named_fit_and_otherwise_the_closest(conn):
    """A `fit:` chip is the user saying which fit they are asking about, so
    it wins even when another fit is nearer. Without one the closest fit is
    the most useful description of a deviating ship, and ties go to the
    lowest fit_id so the panel does not shuffle between reloads."""
    named = fits.diff_for_ship(conn, fc.SHIP_SOLO, fc.DOMINIX, fc.RATTING)
    assert named.named and named.fit_name == fc.RATTING and not named.matches

    closest = fits.diff_for_ship(conn, fc.SHIP_SOLO, fc.DOMINIX, None)
    assert not closest.named and closest.fit_name == fc.SOLO and closest.matches

    # A named fit that does not exist for this hull falls back to closest.
    absent = fits.diff_for_ship(conn, fc.SHIP_SOLO, fc.DOMINIX, fc.HAULING)
    assert not absent.named and absent.fit_name == fc.SOLO

    # A hull with no stored fit has no diff at all: the block stays hidden.
    assert fits.diff_for_ship(conn, fc.SHIP_NO_FIT, fc.SOLSTICE, None) is None
    assert fits.diff_for_ship(conn, fc.SHIP_NO_FIT, fc.SOLSTICE, fc.RATTING) is None

    # The name matches case-insensitively, like the chip's SQL.
    assert fits.diff_for_ship(conn, fc.SHIP_EXACT, fc.DOMINIX, "ratting").named


def test_a_tie_between_two_equally_distant_fits_goes_to_the_lower_fit_id(conn):
    """Deterministic, because a diff block that swapped fits between
    reloads would read as the data changing under the user."""
    text = "[Dominix, Aardvark]\nCapacitor Flux Coil"
    late = fits.save_fit(conn, fits.parse_eft(conn, text), "Aardvark", text)
    ratting = _fit_id(conn, fc.RATTING)
    assert late > ratting, "the tie-break must not be the alphabet"
    empty_diff = fits.diff_for_ship(conn, fc.SHIP_EMPTY, fc.DOMINIX, None)
    # Aardvark is one line short, Solo two and Ratting five, so Aardvark is
    # the closest outright and its later fit_id does not count against it.
    assert empty_diff.fit_id == late and empty_diff.fit_name == "Aardvark"
    # Now give it a genuine tie: another one-line fit, stored later.
    text2 = "[Dominix, Bandicoot]\nExpanded Cargo Bay"
    later = fits.save_fit(conn, fits.parse_eft(conn, text2), "Bandicoot", text2)
    assert later > late
    assert fits.diff_for_ship(conn, fc.SHIP_EMPTY, fc.DOMINIX, None).fit_id == late


# ---------------------------------------------------------- the comparison
def _statuses(racks) -> list[tuple[str, str, int, str]]:
    """Every line of a rack listing flattened to (rack, name, quantity, status)."""
    return [(rack, ln.name, ln.quantity, ln.status) for rack, lines in racks for ln in lines]


def _compare(conn, ship: int, fit_name: str | None = None, hull: int = fc.DOMINIX):
    comparison = fits.compare_for_ship(conn, ship, hull, fit_name)
    assert comparison is not None
    return comparison


def test_the_exact_match_compares_as_match_lines_on_both_sides(conn):
    """The window's baseline: a ship fitted exactly as the fit says shows no
    coloured line anywhere, and its racks sit opposite the fit's -- the ship
    grouped by slot flag, the fit read out of the text's sections, both in
    the dialog's high-to-low order. The text's `[Empty Med slot]` survives as
    a muted placeholder, because the fit was written with that rack empty
    and closing it up would hide that."""
    comparison = _compare(conn, fc.SHIP_EXACT)
    assert comparison.summary == "Matches" and comparison.matches
    assert comparison.headline == f"{fc.RATTING} · Dominix"
    assert [rack for rack, _lines in comparison.ship] == ["High slots", "Low slots", "Rig slots"]
    assert [rack for rack, _lines in comparison.fit] == [
        "High slots", "Mid slots", "Low slots", "Rig slots"
    ]
    ship_lines = _statuses(comparison.ship)
    fit_lines = _statuses(comparison.fit)
    assert len(ship_lines) == 5 and len(fit_lines) == 6, "seven modules, one placeholder"
    assert {status for _r, _n, _q, status in ship_lines} == {fits.MATCH}
    assert {status for _r, _n, _q, status in fit_lines} == {fits.MATCH, fits.EMPTY}
    assert ("Mid slots", "[Empty Med slot]", 0, fits.EMPTY) in fit_lines
    assert sum(q for _r, _n, q, _s in ship_lines) == sum(q for _r, _n, q, _s in fit_lines) == 7
    assert comparison.short == []


def test_missing_extra_and_a_duplicate_count_mark_exactly_the_right_lines(conn):
    """Each deviating corpus ship exists for one wrong line, and the window
    must colour that line and no other. A count that is off splits the type
    into a matched part and an unmatched part rather than colouring all
    three amplifiers, because two of them are right."""
    missing = _compare(conn, fc.SHIP_MISSING)
    assert missing.summary == "1 missing · 2 short"
    assert [ln for ln in _statuses(missing.fit) if ln[3] == fits.MISSING] == [
        ("Rig slots", "Auxiliary Nano Pump", 1, fits.MISSING)
    ]
    assert not [ln for ln in _statuses(missing.ship) if ln[3] != fits.MATCH]

    extra = _compare(conn, fc.SHIP_EXTRA)
    assert extra.summary == "1 extra · 2 short"
    assert [ln for ln in _statuses(extra.ship) if ln[3] == fits.EXTRA] == [
        ("Low slots", "Capacitor Flux Coil", 1, fits.EXTRA)
    ]
    assert not [ln for ln in _statuses(extra.fit) if ln[3] == fits.MISSING]

    dupes = _compare(conn, fc.SHIP_DUPES)
    assert dupes.summary == "1 missing · 2 short"
    low = dict(dupes.fit)["Low slots"]
    assert [(ln.name, ln.quantity, ln.status) for ln in low] == [
        ("Medium Armor Repair Unit", 1, fits.MATCH),
        ("Magnetic Field Amplifier", 2, fits.MATCH),
        ("Magnetic Field Amplifier", 1, fits.MISSING),
    ]
    assert not [ln for ln in _statuses(dupes.ship) if ln[3] != fits.MATCH]

    orphan = _compare(conn, fc.SHIP_NULL_CATEGORY)
    assert [ln for ln in _statuses(orphan.ship) if ln[3] == fits.EXTRA] == [
        ("High slots", "Unlisted Widget", 1, fits.EXTRA)
    ], "a module with no category row is a module here as everywhere else"


def test_a_swapped_fit_splits_the_shared_type_and_counts_the_rest(conn):
    """The Solo Dominix held against Ratting by name: one of its two lasers
    is the laser Ratting wants and the other is extra, the rig matches, and
    everything else Ratting asks for is missing. The headline carries both
    figures in the inspector's order, missing first, and then the two
    consumable types the bare hull has none of."""
    comparison = _compare(conn, fc.SHIP_SOLO, fc.RATTING)
    assert comparison.named and comparison.summary == "5 missing · 1 extra · 2 short"
    assert dict(comparison.ship)["High slots"] == [
        fits.CompareLine("Focused Pulse Laser", 1, fits.MATCH, fc.PULSE_LASER),
        fits.CompareLine("Focused Pulse Laser", 1, fits.EXTRA, fc.PULSE_LASER),
    ]
    assert [ln for ln in _statuses(comparison.fit) if ln[3] == fits.MISSING] == [
        ("High slots", "Rocket Launcher Array", 1, fits.MISSING),
        ("Low slots", "Medium Armor Repair Unit", 1, fits.MISSING),
        ("Low slots", "Magnetic Field Amplifier", 3, fits.MISSING),
    ]
    assert dict(comparison.fit)["Rig slots"] == [
        fits.CompareLine("Auxiliary Nano Pump", 1, fits.MATCH, fc.NANO_PUMP)
    ]
    # missing/extra/short are diff()'s own lists, so the two views agree.
    diff = fits.diff_for_ship(conn, fc.SHIP_SOLO, fc.DOMINIX, fc.RATTING)
    assert (comparison.missing, comparison.extra, comparison.short) == (
        diff.missing, diff.extra, diff.short
    )


def test_a_permuted_rack_matches_and_a_loaded_charge_is_a_note_not_a_line(conn):
    """Slot numbers are not part of the fit, so the same modules in other
    slots colour nothing. The rounds loaded in the launcher share its slot
    flag; they are shown greyed on the launcher's line and never as an extra
    module, which is the mistake the verdict would make if it read flags
    alone."""
    comparison = _compare(conn, fc.SHIP_PERMUTED)
    assert comparison.summary == "Matches · 1 short", "fitted right, and low on drones"
    lines = _statuses(comparison.ship) + _statuses(comparison.fit)
    assert len(lines) == 11
    assert not [ln for ln in lines if ln[3] in (fits.EXTRA, fits.MISSING)]
    assert not [ln for ln in lines if ln[1] == fc.AMMO_NAME], "a charge is not a module line"
    launcher = next(
        ln for _rack, rack_lines in comparison.ship for ln in rack_lines
        if ln.type_id == fc.LAUNCHER
    )
    assert launcher.loaded == f"loaded: 50 × {fc.AMMO_NAME}"
    assert all(ln.loaded == "" for _r, rack_lines in comparison.fit for ln in rack_lines)
    assert comparison.short == [(fc.DRONE_NAME, 0, 5)], "the consumables line is diff()'s"


def test_the_eft_sections_map_to_the_racks_in_pyfas_order(conn):
    """fit_items keeps only module/rig/subsystem, so the text is the one
    record of which module rack a line was written in. Pyfa writes low, mid,
    high, rig, subsystem; a placeholder names its own rack; a section of
    rigs or of subsystems is known by its kind, so a paste with no mid or
    high section does not shove the rigs into the mid rack."""
    ratting = _compare(conn, fc.SHIP_EMPTY, fc.RATTING)
    assert [(rack, [ln.name for ln in lines]) for rack, lines in ratting.fit] == [
        ("High slots", ["Focused Pulse Laser", "Rocket Launcher Array"]),
        ("Mid slots", ["[Empty Med slot]"]),
        ("Low slots", ["Medium Armor Repair Unit", "Magnetic Field Amplifier"]),
        ("Rig slots", ["Auxiliary Nano Pump"]),
    ]

    text = (
        "[Dominix, Sparse]\n\nCapacitor Flux Coil\n\n[Empty High slot]\n"
        "Focused Pulse Laser, Antimatter Charge M\nRocket Launcher Array /OFFLINE\n\n"
        "Auxiliary Nano Pump\n\nDefensive Core Matrix\n\n\nScarab Combat Drone x5\n"
    )
    fits.save_fit(conn, fits.parse_eft(conn, text), "Sparse", text)
    sparse = _compare(conn, fc.SHIP_EMPTY, "Sparse")
    assert [(rack, [ln.name for ln in lines]) for rack, lines in sparse.fit] == [
        ("High slots", ["[Empty High slot]", "Focused Pulse Laser", "Rocket Launcher Array"]),
        ("Low slots", ["Capacitor Flux Coil"]),
        ("Rig slots", ["Auxiliary Nano Pump"]),
        ("Subsystem slots", ["Defensive Core Matrix"]),
    ], "the charge and the /OFFLINE suffix are stripped, the drone stack is not a rack"
    assert sum(ln.quantity for _r, lines in sparse.fit for ln in lines) == 5


def test_a_fit_whose_text_has_no_sections_falls_back_to_the_item_kinds(conn):
    """A fit stored with no text to read racks from still has to draw every
    module it asks for: rigs and subsystems under the racks the ship side
    uses, plain modules under a rack of their own -- and the count must be
    the verdict's, so nothing the diff counted is off the screen."""
    parsed = fits.parse_eft(conn, fc.SOLO_EFT)
    fits.save_fit(conn, parsed, "Bare", "")
    bare = _compare(conn, fc.SHIP_SOLO, "Bare")
    assert bare.matches
    assert [(rack, [(ln.name, ln.quantity, ln.status) for ln in lines]) for rack, lines in bare.fit] == [
        ("Rig slots", [("Auxiliary Nano Pump", 1, fits.MATCH)]),
        ("Modules", [("Focused Pulse Laser", 2, fits.MATCH)]),
    ]
    # A text that places only part of what the items count falls back for
    # the rest, so a hand-trimmed paste cannot make a module vanish.
    partial = "[Dominix, Partial]\n\nFocused Pulse Laser\n\nAuxiliary Nano Pump\n"
    fits.save_fit(conn, parsed, "Partial", partial)
    comparison = _compare(conn, fc.SHIP_SOLO, "Partial")
    assert sum(ln.quantity for _r, lines in comparison.fit for ln in lines) == 3
    assert ("Modules", "Focused Pulse Laser", 1, fits.MATCH) in _statuses(comparison.fit)


# ------------------------------------------------------ the hold sections
def _holds(comparison) -> dict[str, list[tuple[str, int, str, int]]]:
    """Both sides' hold sections as {side/section: [(name, qty, status, wanted)]}."""
    out = {}
    for side, sections in (("ship", comparison.ship_holds), ("fit", comparison.fit_holds)):
        for section, lines in sections:
            out[f"{side}/{section}"] = [
                (ln.name, ln.quantity, ln.status, ln.wanted) for ln in lines
            ]
    return out


def test_the_exact_match_shows_its_drones_and_cargo_as_match_lines_under_the_racks(conn):
    """The screenshot that prompted the sections: a ship fitted exactly as
    the fit says, with its drones and rounds aboard, ended at the rigs with
    a "Short:" paragraph under a blank half-window. Now the Drones and Cargo
    sections follow the racks on both sides, every line a match. The ship's
    side carries its own count (eight drones where five are asked, three of
    them in the fleet hangar) rather than a match plus a split-off surplus,
    and the fighters in a tube -- a type the fit never lists -- are a plain
    extra line after the fit's own."""
    comparison = _compare(conn, fc.SHIP_EXACT)
    assert comparison.summary == "Matches" and comparison.short == []
    assert _holds(comparison) == {
        "ship/Drones": [
            (fc.DRONE_NAME, 8, fits.MATCH, 0),
            (fc.FIGHTER_NAME, 2, fits.EXTRA, 0),
        ],
        "ship/Cargo": [(fc.AMMO_NAME, 100, fits.MATCH, 0)],
        "fit/Drones": [(fc.DRONE_NAME, 5, fits.MATCH, 0)],
        "fit/Cargo": [(fc.AMMO_NAME, 100, fits.MATCH, 0)],
    }
    captions = [caption for caption, _left, _right in comparison.columns()]
    assert captions == ["High slots", "Mid slots", "Low slots", "Rig slots", "Drones", "Cargo"]


def test_a_ship_short_of_a_stack_gets_a_red_line_carrying_the_fits_figure(conn):
    """The Charon holds five paste against a fit asking for a hundred: the
    ship's line is SHORT with the fit's figure on it, so it renders as
    "5 × Nanite Repair Paste (of 100)" without the reader looking across,
    and the fit's line is MISSING opposite it. A bare hull reads the same
    way with a zero: the Ratting Dominix with nothing aboard shows
    "0 × ... (of 100)" and "0 × ... (of 5)". The headline counts the types
    short after the verdict, which for the Charon is still Matches."""
    text = f"[Charon, Stocked]\nExpanded Cargo Bay\nExpanded Cargo Bay\n{fc.PASTE_NAME} x100"
    fits.save_fit(conn, fits.parse_eft(conn, text), "Stocked", text)
    charon = _compare(conn, fc.SHIP_CHARON, "Stocked", hull=fc.CHARON)
    assert charon.matches and charon.summary == "Matches · 1 short"
    assert _holds(charon) == {
        "ship/Cargo": [(fc.PASTE_NAME, 5, fits.SHORT, 100)],
        "fit/Cargo": [(fc.PASTE_NAME, 100, fits.MISSING, 0)],
    }
    assert charon.short == [(fc.PASTE_NAME, 5, 100)], "the inspector's line says the same"

    bare = _compare(conn, fc.SHIP_MISSING)
    assert bare.summary == "1 missing · 2 short"
    assert _holds(bare) == {
        "ship/Drones": [(fc.DRONE_NAME, 0, fits.SHORT, 5)],
        "ship/Cargo": [(fc.AMMO_NAME, 0, fits.SHORT, 100)],
        "fit/Drones": [(fc.DRONE_NAME, 5, fits.MISSING, 0)],
        "fit/Cargo": [(fc.AMMO_NAME, 100, fits.MISSING, 0)],
    }


def test_a_stack_the_fit_never_lists_is_a_plain_extra_line_after_the_fits_own(conn):
    """Surplus cargo is not a deviation, so the crate in the permuted
    Dominix's hold is listed -- the pilot should see what is aboard -- as
    EXTRA, which the window draws uncoloured, and it sorts after the fit's
    own rounds however the rows came out of the database. The Charon
    against Hauling, a fit with no stacks at all, keeps a Cargo caption on
    the ship's side with nothing opposite, so the columns stay aligned
    exactly as a one-sided rack does."""
    permuted = _compare(conn, fc.SHIP_PERMUTED)
    assert _holds(permuted)["ship/Cargo"] == [
        (fc.AMMO_NAME, 650, fits.MATCH, 0),
        ("Reinforced Cargo Crate", 1, fits.EXTRA, 0),
    ]
    assert _holds(permuted)["fit/Cargo"] == [(fc.AMMO_NAME, 100, fits.MATCH, 0)]

    charon = _compare(conn, fc.SHIP_CHARON, hull=fc.CHARON)
    assert charon.fit_holds == []
    assert _holds(charon) == {"ship/Cargo": [(fc.PASTE_NAME, 5, fits.EXTRA, 0)]}
    assert charon.columns()[-1] == ("Cargo", charon.ship_holds[0][1], [])
    assert charon.summary == "Matches", "surplus is not short"


def test_loaded_charges_count_towards_cargo_and_fighter_tubes_towards_drones(conn):
    """The permuted Dominix holds 300 rounds in cargo, 200 in the ammo hold,
    100 in the fuel bay and 50 loaded in the launcher: against a fit asking
    for 700 the Cargo line says 650, so the rounds already in the launcher
    are not reported as missing from the hold. The fighters in the exact
    match's tube answer a fit's fighter line under Drones, since the tubes
    are where a carrier keeps them and DRONE_FLAGS already says so."""
    deep = f"[Dominix, Deep]\nFocused Pulse Laser\n{fc.AMMO_NAME} x700"
    fits.save_fit(conn, fits.parse_eft(conn, deep), "Deep", deep)
    comparison = _compare(conn, fc.SHIP_PERMUTED, "Deep")
    assert _holds(comparison)["ship/Cargo"][0] == (fc.AMMO_NAME, 650, fits.SHORT, 700)
    assert comparison.summary == "6 extra · 1 short", "Deep asks for one module of the seven"

    carrier = f"[Dominix, Carrier]\nFocused Pulse Laser\n{fc.FIGHTER_NAME} x2\n{fc.DRONE_NAME} x9"
    fits.save_fit(conn, fits.parse_eft(conn, carrier), "Carrier", carrier)
    comparison = _compare(conn, fc.SHIP_EXACT, "Carrier")
    assert _holds(comparison)["ship/Drones"] == [
        (fc.DRONE_NAME, 8, fits.SHORT, 9),
        (fc.FIGHTER_NAME, 2, fits.MATCH, 0),
    ]
    assert _holds(comparison)["fit/Drones"] == [
        (fc.DRONE_NAME, 9, fits.MISSING, 0),
        (fc.FIGHTER_NAME, 2, fits.MATCH, 0),
    ]
    assert "ship/Cargo" in _holds(comparison), "the rounds are still listed, as surplus"
    assert "fit/Cargo" not in _holds(comparison)


def test_sections_empty_on_both_sides_are_left_out(conn):
    """The Solo Dominix against Solo: a fit with no stacks and a ship with
    nothing in any hold. Drawing empty Drones and Cargo captions under
    every such pair would put two blank sections on the screen that
    prompted the sections in the first place."""
    comparison = _compare(conn, fc.SHIP_SOLO)
    assert comparison.matches and comparison.summary == "Matches"
    assert comparison.ship_holds == [] and comparison.fit_holds == []
    # The ship's lasers sit in high slots; the fit's text, read in Pyfa's
    # low-first order, places its two under Low slots -- the racks are the
    # EFT reader's business, pinned above; what matters here is what follows.
    assert [caption for caption, _l, _r in comparison.columns()] == [
        "High slots", "Low slots", "Rig slots"
    ]


def test_compare_for_ship_and_diff_for_ship_choose_the_same_fit(conn):
    """The window and the inspector block must talk about one fit. Both go
    through choose_fit, and this holds them to it over every Dominix, with
    and without a chip naming a fit, so a later change to either path
    cannot leave the panel saying Solo while the window shows Ratting."""
    checked = 0
    for ship in sorted(fc.DOMINIXES):
        for wanted in (None, fc.RATTING, fc.SOLO, "ratting", fc.HAULING):
            diff = fits.diff_for_ship(conn, ship, fc.DOMINIX, wanted)
            comparison = fits.compare_for_ship(conn, ship, fc.DOMINIX, wanted)
            assert (comparison.fit_id, comparison.fit_name, comparison.named) == (
                diff.fit_id, diff.fit_name, diff.named
            )
            assert comparison.matches == diff.matches
            checked += 1
    assert checked == len(fc.DOMINIXES) * 5
    assert fits.compare_for_ship(conn, fc.SHIP_NO_FIT, fc.SOLSTICE, None) is None
    assert fits.compare_for_ship(conn, fc.SHIP_NO_FIT, fc.SOLSTICE, fc.RATTING) is None


def test_hull_has_fits_is_the_menus_cheap_gate(conn):
    """The context menu asks this on every right-click on a ship, so it is
    one probe; the answer is per hull, not per ship, which is why the
    Solstice with a rack full of modules still says no."""
    assert fits.hull_has_fits(conn, fc.DOMINIX)
    assert fits.hull_has_fits(conn, fc.CHARON)
    assert not fits.hull_has_fits(conn, fc.SOLSTICE)
    fits.delete_fit(conn, _fit_id(conn, fc.HAULING))
    assert not fits.hull_has_fits(conn, fc.CHARON)


# ------------------------------------------------------- ship-scoped reads
def test_count_ships_counts_assembled_hulls_and_honours_a_filter(conn):
    """The cards' denominator. A packaged stack is not a ship anyone can
    load, so it is outside the count in every polarity."""
    assert queries.count_ships(conn) == len(fc.ASSEMBLED_SHIPS) == 10
    assert queries.count_ships(conn, hull_type_id=fc.DOMINIX) == len(fc.DOMINIXES) == 8
    assert queries.count_ships(conn, hull_type_id=fc.CHARON) == 1
    assert queries.count_ships(conn, "a.item_id = ?", (fc.SHIP_EXACT,)) == 1
    assert queries.count_ships(conn, "a.item_id = ?", (fc.LOOSE_STACK,)) == 0
    # The hull's parameter is bound before the caller's, not after.
    assert queries.count_ships(
        conn, "a.item_id = ?", (fc.SHIP_CHARON,), hull_type_id=fc.CHARON
    ) == 1
    assert queries.count_ships(
        conn, "a.item_id = ?", (fc.SHIP_CHARON,), hull_type_id=fc.DOMINIX
    ) == 0
    assert queries.count_assets(conn) > queries.count_ships(conn), "ships are not every row"


def test_holds_counts_answers_per_ship_for_the_whole_hull_and_each_bay(conn):
    """The count column's data. A ship with none of the type is absent from
    the result and reads 0 in the model, which is what makes `<N` list the
    empty hull."""
    ships = sorted(fc.ASSEMBLED_SHIPS)
    assert queries.holds_counts(conn, ships, fc.AMMO_NAME) == {
        fc.SHIP_EXACT: 100, fc.SHIP_PERMUTED: 650, fc.SHIP_NO_FIT: 40
    }
    assert queries.holds_counts(conn, ships, fc.AMMO_NAME, "cargo") == {
        fc.SHIP_EXACT: 100, fc.SHIP_PERMUTED: 300, fc.SHIP_NO_FIT: 40
    }
    assert queries.holds_counts(conn, ships, fc.AMMO_NAME, "fuel") == {fc.SHIP_PERMUTED: 300}
    assert queries.holds_counts(conn, ships, fc.AMMO_NAME, "drones") == {}
    # Three bays, three questions: the five drones in the bay, the three in
    # the fleet hangar and the two fighters in a tube each answer to one
    # form and to no other -- the drone form used to sum all three as eight.
    assert queries.holds_counts(conn, ships, fc.DRONE_NAME, "drones") == {fc.SHIP_EXACT: 5}
    assert queries.holds_counts(conn, ships, fc.DRONE_NAME, "fleet") == {fc.SHIP_EXACT: 3}
    assert queries.holds_counts(conn, ships, fc.DRONE_NAME, "fighters") == {}
    assert queries.holds_counts(conn, ships, fc.FIGHTER_NAME, "fighters") == {fc.SHIP_EXACT: 2}
    assert queries.holds_counts(conn, ships, fc.FIGHTER_NAME, "drones") == {}
    assert queries.holds_counts(conn, ships, fc.FIGHTER_NAME, "fleet") == {}
    assert queries.holds_counts(conn, ships, "Reinforced Cargo Crate", "fleet") == {
        fc.SHIP_NO_FIT: 1
    }
    # Case-insensitive, like the chip's SQL, and an unknown name is silence.
    assert queries.holds_counts(conn, ships, fc.AMMO_NAME.lower())[fc.SHIP_EXACT] == 100
    assert queries.holds_counts(conn, ships, "Widget Of Doom") == {}
    # The crate's 1,000 rounds belong to the crate, and the hangar stack to
    # nobody; neither ever reaches a ship's count.
    assert fc.SHIP_PERMUTED in queries.holds_counts(conn, ships, fc.AMMO_NAME)
    assert queries.holds_counts(conn, [fc.CRATE_ITEM], fc.AMMO_NAME) == {fc.CRATE_ITEM: 1000}


def test_holds_counts_chunks_a_long_id_list_without_exceeding_the_variable_limit(conn):
    """SQLite's bound-parameter limit is 999 on older builds, and a visible
    page of a big estate can name more ships than that."""
    padding = list(range(8_000_000, 8_002_000))
    counts = queries.holds_counts(conn, [*padding, fc.SHIP_EXACT], fc.AMMO_NAME)
    assert counts == {fc.SHIP_EXACT: 100}


def _vocabulary(rows) -> list[tuple[int, str, int, int, int]]:
    return [(r["type_id"], r["name"], r["units"], r["ships"], r["owned"]) for r in rows]


def test_held_type_counts_lists_what_the_filtered_ships_carry_busiest_first(conn):
    """The holds picker's whole-ship vocabulary: everything inside a ship
    that is not a fitted module, so a crystal loaded in a laser counts and
    the laser does not, and a drone counts wherever it sits. Every row is
    owned by definition -- the whole-ship list has no SDE half."""
    rows = _vocabulary(queries.held_type_counts(conn))
    assert rows == [
        (fc.AMMO, fc.AMMO_NAME, 790, 3, 1),
        (fc.FUEL_BLOCK, fc.FUEL_BLOCK_NAME, 40, 1, 1),
        (fc.DRONE, fc.DRONE_NAME, 10, 2, 1),
        (fc.PASTE, fc.PASTE_NAME, 5, 1, 1),
        (fc.CRATE, "Reinforced Cargo Crate", 2, 2, 1),
        (fc.FIGHTER, fc.FIGHTER_NAME, 2, 1, 1),
        (fc.CRYSTAL, fc.CRYSTAL_NAME, 1, 1, 1),
    ]
    listed = {r[0] for r in rows}
    assert fc.PULSE_LASER not in listed, "a fitted module is not a consumable"
    assert fc.CORE_MATRIX not in listed, "nor is a fitted subsystem"
    assert fc.STRONTIUM not in listed, "a type nobody holds is not aboard"
    assert fc.SPARE_DRONE not in listed and fc.SPARE_FIGHTER not in listed
    # Faceted by the caller's WHERE, written against ASSET_ROWS' aliases.
    charon_only = queries.held_type_counts(conn, "a.item_id = ?", (fc.SHIP_CHARON,))
    assert [(r["name"], r["units"]) for r in charon_only] == [(fc.PASTE_NAME, 5)]
    assert queries.held_type_counts(conn, "a.item_id = ?", (fc.SHIP_EMPTY,)) == []


def test_each_bay_list_is_that_bays_own_vocabulary_held_first_then_the_sde(conn):
    """A bay's list must offer exactly what a chip on that bay could count,
    or the picker would suggest names whose count is always zero. Cargo is
    the cargo flag alone: the crystal held only in a laser and the fuel in
    the fuel bay are not in it. Fuel, drones and fighters are the SDE's
    whole vocabulary -- the fuel groups, the Drone category, the Fighter
    category -- whether or not anyone owns a type, because each question is
    usually about the ship that has none; what the ships in scope actually
    hold in that bay leads, busiest first, and the rest follows by name, so
    the estate's own drones are never buried in the SDE's hundreds. A drone
    parked in a cargo hold is in no drone list, a fighter in a tube is in
    the fighter list and not the drone one, an unpublished drone is in
    neither, and the fleet hangar lists whatever sits there, crate and
    drones alike, by name."""
    cargo = _vocabulary(queries.held_type_counts(conn, bay="cargo"))
    assert cargo == [
        (fc.AMMO, fc.AMMO_NAME, 440, 3, 1),
        (fc.PASTE, fc.PASTE_NAME, 5, 1, 1),
        (fc.CRATE, "Reinforced Cargo Crate", 1, 1, 1),
        (fc.DRONE, fc.DRONE_NAME, 2, 1, 1),
    ]
    cargo_ids = {r[0] for r in cargo}
    assert fc.CRYSTAL not in cargo_ids and fc.FUEL_BLOCK not in cargo_ids

    fuel = _vocabulary(queries.held_type_counts(conn, bay="fuel"))
    assert fuel == [
        (fc.AMMO, fc.AMMO_NAME, 300, 1, 1),
        (fc.FUEL_BLOCK, fc.FUEL_BLOCK_NAME, 40, 1, 1),
        (fc.HEAVY_WATER, fc.HEAVY_WATER_NAME, 0, 0, 0),
        (fc.STRONTIUM, fc.STRONTIUM_NAME, 0, 0, 0),
    ]

    drones = _vocabulary(queries.held_type_counts(conn, bay="drones"))
    assert drones == [
        (fc.DRONE, fc.DRONE_NAME, 5, 1, 1),
        (fc.SPARE_DRONE, fc.SPARE_DRONE_NAME, 0, 0, 0),
    ], "the bay's five lead; the three in the fleet hangar and the two in cargo do not count"
    assert fc.UNPUBLISHED_DRONE not in {r[0] for r in drones}

    fighters = _vocabulary(queries.held_type_counts(conn, bay="fighters"))
    assert fighters == [
        (fc.FIGHTER, fc.FIGHTER_NAME, 2, 1, 1),
        (fc.SPARE_FIGHTER, fc.SPARE_FIGHTER_NAME, 0, 0, 0),
    ]

    fleet = _vocabulary(queries.held_type_counts(conn, bay="fleet"))
    assert fleet == [
        (fc.CRATE, "Reinforced Cargo Crate", 1, 1, 1),
        (fc.DRONE, fc.DRONE_NAME, 3, 1, 1),
    ]

    # The facet still applies to the held half of every list; the SDE half
    # is unfaceted by nature, so a ship flying nothing still gets every
    # drone offered -- in name order, since none of them is held -- and no
    # facet can drop a published type from it.
    solstice = (fc.SHIP_NO_FIT,)
    assert _vocabulary(queries.held_type_counts(conn, "a.item_id = ?", solstice, "cargo")) == [
        (fc.AMMO, fc.AMMO_NAME, 40, 1, 1),
        (fc.DRONE, fc.DRONE_NAME, 2, 1, 1),
    ]
    assert _vocabulary(queries.held_type_counts(conn, "a.item_id = ?", solstice, "drones")) == [
        (fc.SPARE_DRONE, fc.SPARE_DRONE_NAME, 0, 0, 0),
        (fc.DRONE, fc.DRONE_NAME, 0, 0, 0),
    ]
    assert _vocabulary(queries.held_type_counts(conn, "a.item_id = ?", solstice, "fleet")) == [
        (fc.CRATE, "Reinforced Cargo Crate", 1, 1, 1),
    ]
    charon = (fc.SHIP_CHARON,)
    assert [(r["name"], r["ships"], r["owned"]) for r in
            queries.held_type_counts(conn, "a.item_id = ?", charon, "fuel")] == [
        (fc.HEAVY_WATER_NAME, 0, 0), (fc.FUEL_BLOCK_NAME, 0, 0), (fc.STRONTIUM_NAME, 0, 0),
    ]
    assert [(r["name"], r["owned"]) for r in
            queries.held_type_counts(conn, "a.item_id = ?", charon, "fighters")] == [
        (fc.SPARE_FIGHTER_NAME, 0), (fc.FIGHTER_NAME, 0),
    ]


def test_the_shopping_list_is_multibuy_text_for_the_missing_modules_and_the_shortfalls(conn):
    """The window's last job is the market: one Name<tab>quantity line per
    type, missing modules first, then each consumable's gap rather than its
    whole want (the ship already carries the rest), and nothing at all for a
    ship that matches and lacks nothing -- an empty paste into multibuy is
    an error dialog in the game."""
    short = fits.compare_for_ship(conn, fc.SHIP_SOLO, fc.DOMINIX, fc.RATTING)
    lines = short.shopping_list().split("\n")
    assert all(line.count("\t") == 1 for line in lines)
    names = [line.split("\t")[0] for line in lines]
    quantities = {line.split("\t")[0]: int(line.split("\t")[1]) for line in lines}
    for name, quantity in short.missing:
        assert quantities[name] >= quantity
    for name, have, want in short.short:
        assert quantities[name] == want - have
    assert names[: len(short.missing)] == [name for name, _q in short.missing]
    assert len(names) == len(set(names)), "one line per type"

    exact = fits.compare_for_ship(conn, fc.SHIP_EXACT, fc.DOMINIX, fc.RATTING)
    assert exact.missing == [] and exact.short == []
    assert exact.shopping_list() == ""

