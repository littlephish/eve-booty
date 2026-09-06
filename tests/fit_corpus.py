"""A synthetic estate built to trap every case the holds and fit chips have.

One station, one pilot, eleven ships and three stored fits, arranged so that
each interesting answer has exactly one ship behind it: a fit matched
exactly, matched with the slots permuted and charges loaded, deviating by an
extra module, by a missing one, by a duplicate count off by one, and by a
module whose SDE category row is missing; an empty hull, a packaged stack, a
hull nobody has written a fit for, a second hull with its own fit, and a
ship matching the second of its hull's two fits. The consumable is spread
over every counting case at once -- cargo, loaded in a launcher, the ammo
hold, the fuel bay, a container in the cargo hold (not counted, one level
only) and a loose hangar stack (not counted, not on a ship). The unfitted
hull carries the cases the holds picker's bay lists need on top: a crystal
loaded in its laser and nowhere else, a drone in its cargo hold, a crate in
its fleet hangar and fuel in its fuel bay -- each of them in exactly one
bay's vocabulary, or in none.

Nothing here comes from anywhere real. The two hulls keep the CCP ids the
existing fixtures already use (Dominix 645, Charon 20185) and the SDE
category ids are CCP's constants; every other id is synthetic, in a 9xxxxx
band, and every other name is invented -- except the two consumables, whose
real names ("Antimatter Charge M", "Nanite Repair Paste") the plan's
examples and the README's grammar block both spell out, and the fuel, whose
real names and real SDE group names ("Fuel Block", "Ice Product") are what
queries.held_type_counts looks up for the fuel bay's list, so the fixture
spells them the same way. The SDE half of that list, and of the drone and
fighter lists, is what the two ice products, the second drone and the second
fighter are for: published types nobody owns, which the picker must still
offer -- and one unpublished drone, which it must not.
"""

from __future__ import annotations

from evasset import fits

# CCP category ids. Ship and Module decide what a `holds:` candidate and a
# fitted module are; Drone, Fighter and Subsystem are what fits.classify
# keys on; Charge is what tells a loaded round from the launcher it sits in.
CAT_MATERIAL, CAT_SHIP, CAT_MODULE, CAT_CHARGE = 4, 6, 7, 8
CAT_DRONE, CAT_SUBSYSTEM, CAT_COMMODITY, CAT_FIGHTER = 18, 32, 43, 87

# Groups. GRP_RIG_ARMOR is the one whose NAME does the classifying: a rig is
# category Module like every other module, and only the "Rig " prefix says
# otherwise. GRP_ORPHAN has no sde_groups row at all, which is how a type
# ends up with a NULL category.
GRP_BATTLESHIP, GRP_FREIGHTER = 27, 513
GRP_FRIGATE = 900101
GRP_ENERGY_TURRET = 900102
GRP_ARMOR_REPAIR = 900103
GRP_RIG_ARMOR = 900104
GRP_LAUNCHER = 900105
GRP_HYBRID_CHARGE = 900106
GRP_COMBAT_DRONE = 900107
GRP_LIGHT_FIGHTER = 900108
GRP_DEFENSIVE_SUBSYSTEM = 900109
GRP_NANITE_COMPOUND = 900110
GRP_ORPHAN = 900111
# The two groups the fuel bay's picker list is drawn from, under their real
# SDE names: the query finds them by name, so the corpus must spell them
# as CCP does even though the ids are synthetic.
GRP_FUEL_BLOCK = 900112
GRP_ICE_PRODUCT = 900113

# Hulls.
DOMINIX, CHARON = 645, 20185
SOLSTICE = 900301

# Modules, rigs, the subsystem, and the two types with no fit behind them.
PULSE_LASER = 900201
ARMOR_REPAIRER = 900202
NANO_PUMP = 900203
FIELD_AMPLIFIER = 900204
LAUNCHER = 900211
FLUX_COIL = 900212
CARGO_BAY = 900213
CORE_MATRIX = 900209
ORPHAN_MODULE = 900214

# What ships carry.
AMMO = 900205
PASTE = 900206
DRONE = 900207
FIGHTER = 900208
CRATE = 900210
CRYSTAL = 900215
FUEL_BLOCK = 900216
# The two ice products are owned by nobody: they are in the fuel bay's
# vocabulary because the SDE lists them, not because a ship holds them. The
# second drone and the second fighter play the same part for the drone and
# fighter bays' lists; the unpublished drone is in the SDE and in no list.
HEAVY_WATER = 900217
STRONTIUM = 900218
SPARE_DRONE = 900219
SPARE_FIGHTER = 900220
UNPUBLISHED_DRONE = 900221

AMMO_NAME = "Antimatter Charge M"
PASTE_NAME = "Nanite Repair Paste"
DRONE_NAME = "Scarab Combat Drone"
FIGHTER_NAME = "Sparrowhawk Light Fighter"
CRYSTAL_NAME = "Ember Crystal M"
FUEL_BLOCK_NAME = "Helium Fuel Block"
HEAVY_WATER_NAME = "Heavy Water"
STRONTIUM_NAME = "Strontium Clathrates"
SPARE_DRONE_NAME = "Locust Combat Drone"
SPARE_FIGHTER_NAME = "Gyrfalcon Light Fighter"
UNPUBLISHED_DRONE_NAME = "Prototype Combat Drone"

JITA_4_4, JITA_SYS, THE_FORGE = 60003760, 30000142, 10000002
PILOT_ID, PILOT_NAME = 100, "Test Pilot"

# Ships, one per case. The band is synthetic and contiguous so a test can
# name them and a reader can tell a ship id from a content id at a glance.
SHIP_EXACT = 5_000_001
SHIP_PERMUTED = 5_000_002
SHIP_EXTRA = 5_000_003
SHIP_MISSING = 5_000_004
SHIP_DUPES = 5_000_005
SHIP_EMPTY = 5_000_006
SHIP_PACKAGED = 5_000_007
SHIP_NO_FIT = 5_000_008
SHIP_CHARON = 5_000_009
SHIP_SOLO = 5_000_010
SHIP_NULL_CATEGORY = 5_000_011

CRATE_ITEM = 6_000_100
LOOSE_STACK = 6_000_900

# Every assembled ship, which is what both chips restrict to in either
# polarity. The packaged stack is deliberately absent.
ASSEMBLED_SHIPS = {
    SHIP_EXACT, SHIP_PERMUTED, SHIP_EXTRA, SHIP_MISSING, SHIP_DUPES, SHIP_EMPTY,
    SHIP_NO_FIT, SHIP_CHARON, SHIP_SOLO, SHIP_NULL_CATEGORY,
}
DOMINIXES = {
    SHIP_EXACT, SHIP_PERMUTED, SHIP_EXTRA, SHIP_MISSING, SHIP_DUPES, SHIP_EMPTY,
    SHIP_SOLO, SHIP_NULL_CATEGORY,
}

# The three stored fits, as EFT text the fixture actually parses -- one
# `Module, Charge` line, one `[Empty ...]` placeholder, stacks and blank
# separators, so the corpus exercises the parser on its way in rather than
# writing fit_items rows by hand.
RATTING_EFT = """[Dominix, Ratting]

Medium Armor Repair Unit
Magnetic Field Amplifier
Magnetic Field Amplifier
Magnetic Field Amplifier

[Empty Med slot]

Focused Pulse Laser
Rocket Launcher Array, Antimatter Charge M

Auxiliary Nano Pump


Scarab Combat Drone x5


Antimatter Charge M x100
"""

SOLO_EFT = """[Dominix, Solo]

Focused Pulse Laser
Focused Pulse Laser

Auxiliary Nano Pump
"""

HAULING_EFT = """[Charon, Hauling]

Expanded Cargo Bay
Expanded Cargo Bay
"""

RATTING, SOLO, HAULING = "Ratting", "Solo", "Hauling"

_CATEGORIES = [
    (CAT_MATERIAL, "Material"),
    (CAT_SHIP, "Ship"),
    (CAT_MODULE, "Module"),
    (CAT_CHARGE, "Charge"),
    (CAT_DRONE, "Drone"),
    (CAT_SUBSYSTEM, "Subsystem"),
    (CAT_COMMODITY, "Commodity"),
    (CAT_FIGHTER, "Fighter"),
]

_GROUPS = [
    (GRP_BATTLESHIP, CAT_SHIP, "Battleship"),
    (GRP_FREIGHTER, CAT_SHIP, "Freighter"),
    (GRP_FRIGATE, CAT_SHIP, "Skirmish Frigate"),
    (GRP_ENERGY_TURRET, CAT_MODULE, "Energy Turret"),
    (GRP_ARMOR_REPAIR, CAT_MODULE, "Armor Repair Unit"),
    (GRP_RIG_ARMOR, CAT_MODULE, "Rig Armor"),
    (GRP_LAUNCHER, CAT_MODULE, "Missile Launcher Rack"),
    (GRP_HYBRID_CHARGE, CAT_CHARGE, "Hybrid Charge"),
    (GRP_COMBAT_DRONE, CAT_DRONE, "Light Combat Drone"),
    (GRP_LIGHT_FIGHTER, CAT_FIGHTER, "Light Fighter"),
    (GRP_DEFENSIVE_SUBSYSTEM, CAT_SUBSYSTEM, "Defensive Subsystem"),
    (GRP_NANITE_COMPOUND, CAT_MATERIAL, "Nanite Compound"),
    (GRP_FUEL_BLOCK, CAT_COMMODITY, "Fuel Block"),
    (GRP_ICE_PRODUCT, CAT_MATERIAL, "Ice Product"),
]

_TYPES = [
    (DOMINIX, "Dominix", GRP_BATTLESHIP, 454500),
    (CHARON, "Charon", GRP_FREIGHTER, 16250000),
    (SOLSTICE, "Solstice", GRP_FRIGATE, 27000),
    (PULSE_LASER, "Focused Pulse Laser", GRP_ENERGY_TURRET, 5),
    (ARMOR_REPAIRER, "Medium Armor Repair Unit", GRP_ARMOR_REPAIR, 5),
    (NANO_PUMP, "Auxiliary Nano Pump", GRP_RIG_ARMOR, 5),
    (FIELD_AMPLIFIER, "Magnetic Field Amplifier", GRP_ARMOR_REPAIR, 5),
    (LAUNCHER, "Rocket Launcher Array", GRP_LAUNCHER, 5),
    (FLUX_COIL, "Capacitor Flux Coil", GRP_ARMOR_REPAIR, 5),
    (CARGO_BAY, "Expanded Cargo Bay", GRP_ARMOR_REPAIR, 5),
    (CORE_MATRIX, "Defensive Core Matrix", GRP_DEFENSIVE_SUBSYSTEM, 10),
    (AMMO, AMMO_NAME, GRP_HYBRID_CHARGE, 0.0125),
    (PASTE, PASTE_NAME, GRP_NANITE_COMPOUND, 0.01),
    (DRONE, DRONE_NAME, GRP_COMBAT_DRONE, 5),
    (FIGHTER, FIGHTER_NAME, GRP_LIGHT_FIGHTER, 100),
    (CRATE, "Reinforced Cargo Crate", GRP_NANITE_COMPOUND, 100),
    (CRYSTAL, CRYSTAL_NAME, GRP_HYBRID_CHARGE, 1),
    (FUEL_BLOCK, FUEL_BLOCK_NAME, GRP_FUEL_BLOCK, 5),
    (HEAVY_WATER, HEAVY_WATER_NAME, GRP_ICE_PRODUCT, 0.4),
    (STRONTIUM, STRONTIUM_NAME, GRP_ICE_PRODUCT, 3),
    (SPARE_DRONE, SPARE_DRONE_NAME, GRP_COMBAT_DRONE, 5),
    (SPARE_FIGHTER, SPARE_FIGHTER_NAME, GRP_LIGHT_FIGHTER, 100),
    # No sde_groups row for GRP_ORPHAN: this type's category comes out NULL,
    # which both the SQL multiset and fitting.fitted_modules must read as
    # "not a charge, so it is a module".
    (ORPHAN_MODULE, "Unlisted Widget", GRP_ORPHAN, 5),
]

# Published = 0, the SDE's flag for a type the game no longer sells or never
# did. The SDE-wide bay lists must skip it, or a picker over the Drone
# category would offer test and event types nobody can fly.
_UNPUBLISHED_TYPES = [
    (UNPUBLISHED_DRONE, UNPUBLISHED_DRONE_NAME, GRP_COMBAT_DRONE, 5),
]

# (item_id, type_id, quantity, location_id, location_flag, is_singleton).
# A ship's contents carry the ship's item_id as their location_id, which is
# what makes them "inside" it; the crate's contents carry the crate's, which
# is what keeps them out of every count.
_ASSETS = [
    (SHIP_EXACT, DOMINIX, 1, JITA_4_4, "Hangar", 1),
    (SHIP_PERMUTED, DOMINIX, 1, JITA_4_4, "Hangar", 1),
    (SHIP_EXTRA, DOMINIX, 1, JITA_4_4, "Hangar", 1),
    (SHIP_MISSING, DOMINIX, 1, JITA_4_4, "Hangar", 1),
    (SHIP_DUPES, DOMINIX, 1, JITA_4_4, "Hangar", 1),
    (SHIP_EMPTY, DOMINIX, 1, JITA_4_4, "Hangar", 1),
    (SHIP_PACKAGED, DOMINIX, 3, JITA_4_4, "Hangar", 0),
    (SHIP_NO_FIT, SOLSTICE, 1, JITA_4_4, "Hangar", 1),
    (SHIP_CHARON, CHARON, 1, JITA_4_4, "Hangar", 1),
    (SHIP_SOLO, DOMINIX, 1, JITA_4_4, "Hangar", 1),
    (SHIP_NULL_CATEGORY, DOMINIX, 1, JITA_4_4, "Hangar", 1),

    # The exact match, with drones and fighters aboard and its full 100
    # rounds, so nothing about it is short.
    (5_100_001, ARMOR_REPAIRER, 1, SHIP_EXACT, "LoSlot0", 1),
    (5_100_002, FIELD_AMPLIFIER, 1, SHIP_EXACT, "LoSlot1", 1),
    (5_100_003, FIELD_AMPLIFIER, 1, SHIP_EXACT, "LoSlot2", 1),
    (5_100_004, FIELD_AMPLIFIER, 1, SHIP_EXACT, "LoSlot3", 1),
    (5_100_005, LAUNCHER, 1, SHIP_EXACT, "HiSlot0", 1),
    (5_100_006, PULSE_LASER, 1, SHIP_EXACT, "HiSlot1", 1),
    (5_100_007, NANO_PUMP, 1, SHIP_EXACT, "RigSlot0", 1),
    (5_100_008, DRONE, 5, SHIP_EXACT, "DroneBay", 0),
    (5_100_009, DRONE, 3, SHIP_EXACT, "FleetHangar", 0),
    (5_100_010, FIGHTER, 2, SHIP_EXACT, "FighterTube2", 0),
    (5_100_011, AMMO, 100, SHIP_EXACT, "Cargo", 0),

    # The same rack in different slots, with a charge loaded into the
    # launcher: the loaded rounds count towards the whole-ship total and
    # towards no bay, and must not make the fit deviate.
    (5_200_001, ARMOR_REPAIRER, 1, SHIP_PERMUTED, "LoSlot3", 1),
    (5_200_002, FIELD_AMPLIFIER, 1, SHIP_PERMUTED, "LoSlot0", 1),
    (5_200_003, FIELD_AMPLIFIER, 1, SHIP_PERMUTED, "LoSlot2", 1),
    (5_200_004, FIELD_AMPLIFIER, 1, SHIP_PERMUTED, "LoSlot5", 1),
    (5_200_005, LAUNCHER, 1, SHIP_PERMUTED, "HiSlot0", 1),
    (5_200_006, AMMO, 50, SHIP_PERMUTED, "HiSlot0", 0),
    (5_200_007, PULSE_LASER, 1, SHIP_PERMUTED, "HiSlot2", 1),
    (5_200_008, NANO_PUMP, 1, SHIP_PERMUTED, "RigSlot2", 1),
    (5_200_009, AMMO, 300, SHIP_PERMUTED, "Cargo", 0),
    (5_200_010, AMMO, 200, SHIP_PERMUTED, "SpecializedAmmoHold", 0),
    (5_200_011, AMMO, 100, SHIP_PERMUTED, "SpecializedFuelBay", 0),
    (CRATE_ITEM, CRATE, 1, SHIP_PERMUTED, "Cargo", 1),
    (6_000_101, AMMO, 1000, CRATE_ITEM, "Cargo", 0),

    # One module too many.
    (5_300_001, ARMOR_REPAIRER, 1, SHIP_EXTRA, "LoSlot0", 1),
    (5_300_002, FIELD_AMPLIFIER, 1, SHIP_EXTRA, "LoSlot1", 1),
    (5_300_003, FIELD_AMPLIFIER, 1, SHIP_EXTRA, "LoSlot2", 1),
    (5_300_004, FIELD_AMPLIFIER, 1, SHIP_EXTRA, "LoSlot3", 1),
    (5_300_005, FLUX_COIL, 1, SHIP_EXTRA, "LoSlot4", 1),
    (5_300_006, LAUNCHER, 1, SHIP_EXTRA, "HiSlot0", 1),
    (5_300_007, PULSE_LASER, 1, SHIP_EXTRA, "HiSlot1", 1),
    (5_300_008, NANO_PUMP, 1, SHIP_EXTRA, "RigSlot0", 1),

    # The rig never fitted.
    (5_400_001, ARMOR_REPAIRER, 1, SHIP_MISSING, "LoSlot0", 1),
    (5_400_002, FIELD_AMPLIFIER, 1, SHIP_MISSING, "LoSlot1", 1),
    (5_400_003, FIELD_AMPLIFIER, 1, SHIP_MISSING, "LoSlot2", 1),
    (5_400_004, FIELD_AMPLIFIER, 1, SHIP_MISSING, "LoSlot3", 1),
    (5_400_005, LAUNCHER, 1, SHIP_MISSING, "HiSlot0", 1),
    (5_400_006, PULSE_LASER, 1, SHIP_MISSING, "HiSlot1", 1),

    # Two amplifiers where the fit wants three: the count, not the type.
    (5_500_001, ARMOR_REPAIRER, 1, SHIP_DUPES, "LoSlot0", 1),
    (5_500_002, FIELD_AMPLIFIER, 1, SHIP_DUPES, "LoSlot1", 1),
    (5_500_003, FIELD_AMPLIFIER, 1, SHIP_DUPES, "LoSlot2", 1),
    (5_500_004, LAUNCHER, 1, SHIP_DUPES, "HiSlot0", 1),
    (5_500_005, PULSE_LASER, 1, SHIP_DUPES, "HiSlot1", 1),
    (5_500_006, NANO_PUMP, 1, SHIP_DUPES, "RigSlot0", 1),

    # A hull nobody has written a fit for, carrying a subsystem and some
    # rounds so it shows up in the holds counts but in neither is:fit
    # polarity. It also carries the picker's bay-list cases, which no
    # stored fit ever compares against: a crystal loaded in the laser and
    # held nowhere else (the whole ship's list, no bay's), a drone in the
    # cargo hold (cargo's list, not drones'), a crate in the fleet hangar
    # (a drone flag, but not a drone) and fuel in the fuel bay.
    (5_800_001, PULSE_LASER, 1, SHIP_NO_FIT, "HiSlot0", 1),
    (5_800_002, CORE_MATRIX, 1, SHIP_NO_FIT, "SubSystemSlot0", 1),
    (5_800_003, AMMO, 40, SHIP_NO_FIT, "Cargo", 0),
    (5_800_004, CRYSTAL, 1, SHIP_NO_FIT, "HiSlot0", 0),
    (5_800_005, DRONE, 2, SHIP_NO_FIT, "Cargo", 0),
    (5_800_006, CRATE, 1, SHIP_NO_FIT, "FleetHangar", 0),
    (5_800_007, FUEL_BLOCK, 40, SHIP_NO_FIT, "SpecializedFuelBay", 0),

    # The second hull and its own fit.
    (5_900_001, CARGO_BAY, 1, SHIP_CHARON, "LoSlot0", 1),
    (5_900_002, CARGO_BAY, 1, SHIP_CHARON, "LoSlot1", 1),
    (5_900_003, PASTE, 5, SHIP_CHARON, "Cargo", 0),

    # Matches the SECOND of the Dominix's two fits and neither the first.
    (5_910_001, PULSE_LASER, 1, SHIP_SOLO, "HiSlot0", 1),
    (5_910_002, PULSE_LASER, 1, SHIP_SOLO, "HiSlot1", 1),
    (5_910_003, NANO_PUMP, 1, SHIP_SOLO, "RigSlot0", 1),

    # The exact rack plus a module whose category row is missing. It must
    # count as a module and make the ship deviate, not vanish.
    (5_920_001, ARMOR_REPAIRER, 1, SHIP_NULL_CATEGORY, "LoSlot0", 1),
    (5_920_002, FIELD_AMPLIFIER, 1, SHIP_NULL_CATEGORY, "LoSlot1", 1),
    (5_920_003, FIELD_AMPLIFIER, 1, SHIP_NULL_CATEGORY, "LoSlot2", 1),
    (5_920_004, FIELD_AMPLIFIER, 1, SHIP_NULL_CATEGORY, "LoSlot3", 1),
    (5_920_005, LAUNCHER, 1, SHIP_NULL_CATEGORY, "HiSlot0", 1),
    (5_920_006, PULSE_LASER, 1, SHIP_NULL_CATEGORY, "HiSlot1", 1),
    (5_920_007, NANO_PUMP, 1, SHIP_NULL_CATEGORY, "RigSlot0", 1),
    (5_920_008, ORPHAN_MODULE, 1, SHIP_NULL_CATEGORY, "HiSlot3", 1),

    # Loose in the hangar: never a ship's contents, however large the pile.
    (LOOSE_STACK, AMMO, 5000, JITA_4_4, "Hangar", 0),
]


def install(conn) -> None:
    """Seed the whole corpus: SDE rows, the estate, and the three stored fits.

    The fits go in through fits.parse_eft and fits.save_fit rather than as
    hand-written fit_items rows, so a change that breaks the parser cannot
    leave the SQL tests passing against rows the app could never have
    produced.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO sde_categories (category_id, name) VALUES (?,?)", _CATEGORIES
    )
    conn.executemany(
        "INSERT OR IGNORE INTO sde_groups (group_id, category_id, name) VALUES (?,?,?)",
        [(g, c, n) for g, c, n in _GROUPS],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO sde_types (type_id, name, group_id, volume, portion_size, "
        "published) VALUES (?,?,?,?,1,1)",
        [(t, n, g, v) for t, n, g, v in _TYPES],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO sde_types (type_id, name, group_id, volume, portion_size, "
        "published) VALUES (?,?,?,?,1,0)",
        [(t, n, g, v) for t, n, g, v in _UNPUBLISHED_TYPES],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO sde_regions (region_id, name) VALUES (?,?)",
        [(THE_FORGE, "The Forge")],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO sde_systems (system_id, name, constellation_id, region_id, "
        "security) VALUES (?,?,?,?,?)",
        [(JITA_SYS, "Jita", 20000020, THE_FORGE, 0.9)],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO sde_stations (station_id, name, system_id, region_id) "
        "VALUES (?,?,?,?)",
        [(JITA_4_4, "Jita IV - Moon 4 - Caldari Navy Assembly Plant", JITA_SYS, THE_FORGE)],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO characters (character_id, name, corporation_id, scopes, enabled) "
        "VALUES (?,?,?,?,1)",
        [(PILOT_ID, PILOT_NAME, 2000, "s")],
    )
    conn.executemany(
        "INSERT INTO assets (owner_type, owner_id, item_id, type_id, quantity, location_id, "
        "location_flag, location_type, is_singleton, is_blueprint_copy, custom_name, "
        "root_location_id, system_id, region_id) "
        "VALUES ('character',?,?,?,?,?,?,?,?,0,NULL,?,?,?)",
        [
            (
                PILOT_ID, item, type_id, qty, loc, flag,
                "station" if loc == JITA_4_4 else "item",
                singleton, JITA_4_4, JITA_SYS, THE_FORGE,
            )
            for item, type_id, qty, loc, flag, singleton in _ASSETS
        ],
    )
    for text in (RATTING_EFT, SOLO_EFT, HAULING_EFT):
        parsed = fits.parse_eft(conn, text)
        assert parsed.ok, f"the corpus's own fit must parse: {parsed.unknown or 'no hull'}"
        fits.save_fit(conn, parsed, parsed.name, text)
    conn.commit()
