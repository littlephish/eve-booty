#!/usr/bin/env python3
"""Fill a throwaway database with plausible data, no EVE account needed.

Useful for working on the UI, taking screenshots, and checking a change did
not break a view you were not thinking about.

    EVASSET_DATA_DIR=/tmp/demo uv run python scripts/seed_demo.py
    EVASSET_DATA_DIR=/tmp/demo uv run evebooty

The SDE is downloaded on first run and cached, so this is only slow once.
Real type ids and station ids throughout, so the joins exercise real data.
The abyssal modules come from tests/data/abyssal_corpus.json, an anonymised
corpus of 520 fetched rolls, split between the two pilots' hangars and the
corporation's, so the inspector, the roll columns and the search card have
an estate-sized population to work with.

On top of that estate the seed builds a fleet whose only purpose is to give
the `holds:`, `fit:` and `is:fit` chips something with a right answer. Eight
stored fits spread over five hulls (two each for the Dominix, the Vexor Navy
Issue and the Caracal; one each for the Rorqual and the Thanatos), and every
one of those fits has a ship built for each way a rack can relate to it --
exact, one module missing, one extra, one swapped, the same modules on
permuted slots and an empty hull. The packaged stack and the ship where an
abyssal module out of the corpus has taken a slot are built once per hull
instead of once per fit, because they say nothing a second fit would say
differently and the corpus is finite. Two hundred-odd further ships repeat
the same shapes in bulk, each against one of its hull's fits drawn at random,
so every stored fit is answered by a dozen ships rather than by an anecdote.
A Rorqual and a Thanatos carry the holds the bay forms are about (fuel, ammo,
ore, ship hangar, fighter tubes, fleet hangar) and a Rifter is fitted with no
stored fit for its hull, so both polarities of `is:fit` must leave it out.
A Dominix refitted mostly wrong against the sentry fit -- twelve of twenty
modules -- gives the Compare deviation window a rack worth looking at.

Two saved views go in on top of that, one on digit 1 and one unslotted, so
`Ctrl+L` opens on a library rather than on an empty list.

Every type id and station id here is looked up by name from the imported SDE
at seed time, never hardcoded, and every fit goes in through fits.parse_eft
and fits.save_fit from the EFT blocks below -- so if a name is wrong the seed
says so instead of storing a fit nothing can match. The fleet is deterministic
(one fixed RNG seed) and its invariants are asserted before the seed returns:
a demo whose numbers do not add up is worse than no demo, because the reader
blames the filter.
"""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import count, cycle
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import abyssal_corpus  # noqa: E402

from evasset import db, fits, fitting, networth, omni, queries, sde, views  # noqa: E402
from evasset.config import (  # noqa: E402
    DB_PATH,
    Settings,
)

CHARON, DOMINIX, TRITANIUM, PLEX = 20185, 645, 34, 44992
JITA_4_4, AMARR_VIII = 60003760, 60008494
JITA_SYS, AMARR_SYS = 30000142, 30002187
THE_FORGE, DOMAIN = 10000002, 10000043

PILOT, ALT, CORP = 91000001, 91000002, 98000001

# The fleet's item ids. The hand-written rows above stop at 7 and the abyssal
# corpus owns 1000000000101..1000000000620, so a band of its own keeps the
# three from ever colliding as any of them grows.
FIRST_FLEET_ID = 2_000_000

# One fixed seed, so two runs of this script produce the same estate and a
# screenshot taken today still matches the counts a reader gets tomorrow.
FLEET_SEED = 20260905

# Roughly the fleet size an active pilot's hangar list reaches, which is the
# scale at which a count column and a "N of TOTAL match" footer stop looking
# like a toy. Six fitted-hull fits now share it rather than three, so it is
# sized to leave each of them a population and not merely the fleet a share:
# 150 left the thinnest fit eight ships.
BULK_SHIPS = 210


_TYPE_IDS: dict[str, int] = {}


def type_id(conn, name: str) -> int:
    """The SDE type id of one name; an unknown name stops the seed.

    Nothing in this file hardcodes a module id: the SDE is already imported
    by the time seed() runs, so a name is the honest way to say which item
    is meant, and a rename or a typo stops the seed instead of quietly
    planting a fit that no ship can ever match.

    Answers are memoised because the fleet resolves the same few dozen names
    several thousand times -- once per module of every ship it builds.
    """
    cached = _TYPE_IDS.get(name)
    if cached is not None:
        return cached
    row = conn.execute(
        "SELECT type_id FROM sde_types WHERE name = ? COLLATE NOCASE "
        "ORDER BY published DESC, type_id LIMIT 1",
        (name,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"the imported SDE has no type named {name!r}")
    _TYPE_IDS[name] = int(row["type_id"])
    return _TYPE_IDS[name]


def rack(*groups: tuple[str, tuple[str, ...]]) -> tuple[tuple[str, str], ...]:
    """A slot rack as ESI reports it: ("LoSlot", (a, b)) -> LoSlot0 a, LoSlot1 b.

    The numbering is derived and the module names are not. That is the whole
    point of writing the racks out beside the EFT blocks rather than building
    the ships from the parsed fit: a seed that derived the modules from the
    fit could never disagree with it, which is exactly the disagreement the
    `fit:` chip and the inspector's diff exist to show. Which numbered slot a
    module sits in is not part of that disagreement -- the verdict is a
    multiset -- so the numbers are safe to generate.
    """
    return tuple(
        (f"{prefix}{i}", name) for prefix, names in groups for i, name in enumerate(names)
    )


def permuted(slots: tuple[tuple[str, str], ...]) -> list[tuple[str, str]]:
    """The same modules on the same slots, dealt out in reverse within each rack.

    A ship somebody refitted in a different order. The multiset is untouched,
    so `fit:` must still call it a match -- the case that would fail if the
    verdict ever grew a notion of slot order.
    """
    out: list[tuple[str, str]] = []
    for prefix in ("HiSlot", "MedSlot", "LoSlot", "RigSlot"):
        group = [(flag, name) for flag, name in slots if flag.startswith(prefix)]
        out += [
            (flag, name)
            for (flag, _), (_, name) in zip(group, reversed(group), strict=True)
        ]
    return out


# After EVE Workbench "Dominix, Ratting Dominix - Serpentis"
# (https://eveworkbench.com/fitting/dominix/cf3431f4-a2a2-46c3-6f24-08d9d2f078f1),
# read 2026-09-05; charges and cargo completed here. MedSlot4 carried a second
# Large Cap Battery II there and is left free below, because this is a primary
# fit and the extra case needs a slot the hull really has.
DOMINIX_EFT = """[Dominix, Sentry Ratting]

Large Armor Repairer II
Reactive Armor Hardener
Kinetic Armor Hardener II
Drone Damage Amplifier II
Drone Damage Amplifier II
Drone Damage Amplifier II
Drone Damage Amplifier II

Large Cap Battery II
Omnidirectional Tracking Link II, Tracking Speed Script
Drone Navigation Computer II
Cap Recharger II

Drone Link Augmentor II
Drone Link Augmentor II
Heavy Energy Neutralizer II
Heavy Energy Neutralizer II
Heavy Energy Neutralizer II
425mm Prototype Gauss Gun, Antimatter Charge L

Large Thermal Armor Reinforcer I
Large Thermal Armor Reinforcer I
Large Auxiliary Nano Pump I


Warden II x5
Wasp II x5
Vespa II x5
Hornet II x5
Hornet EC-300 x5


Antimatter Charge L x2000
Thorium Charge L x1000
Omnidirectional Tracking Link II x1
Tracking Speed Script x2
Optimal Range Script x2
Nanite Repair Paste x100
"""

# After the zKillboard fitted loss "Dominix" of 2026-09-05T03:04:42Z
# (https://zkillboard.com/kill/138216685/), read 2026-09-05; charges and cargo
# completed here. The doctrine tanks structure rather than armour, which is
# what a blaster Dominix that has to survive its own web range now looks like.
# The killmail filled all seven lows with a sixth Reinforced Bulkheads II; one
# of the six is left off here so LoSlot6 is the free slot the extra case needs,
# and five bulkheads still carry the doctrine's structure buffer.
DOMINIX_SPARE_EFT = """[Dominix, Blaster Brawl]

Damage Control II
Reinforced Bulkheads II
Reinforced Bulkheads II
Reinforced Bulkheads II
Reinforced Bulkheads II
Reinforced Bulkheads II

Warp Scrambler II
Heavy Stasis Grappler II
500MN Y-T8 Compact Microwarpdrive
Heavy F-RX Compact Capacitor Booster, Navy Cap Booster 800
Large Micro Jump Drive

Ion Blaster Cannon II, Void L
Ion Blaster Cannon II, Void L
Ion Blaster Cannon II, Void L
Ion Blaster Cannon II, Void L
Ion Blaster Cannon II, Void L
Ion Blaster Cannon II, Void L

Large Transverse Bulkhead I
Large Transverse Bulkhead I
Large Transverse Bulkhead I


Praetor II x5
Hammerhead II x5
Hobgoblin II x5
Vespa EC-600 x5


Null L x2000
Void L x1500
Federation Navy Antimatter Charge L x1500
Navy Cap Booster 800 x20
Nanite Repair Paste x150
"""

# After Ashy in Space, "Fit Kitchen: The New VNI", fit "Brawling VNI (Single
# Ancil)" (https://ashy.vargur.dev/fit-kitchen-the-new-vni/), read 2026-09-05;
# charges and cargo completed here. The article's Energized Adaptive Nano
# Membrane II is written under the name CCP has given it since. LoSlot5
# carried a second Magnetic Field Stabilizer II there and is left free below,
# because this is a primary fit and the extra case needs a real slot.
VEXOR_EFT = """[Vexor Navy Issue, Drone Roam]

Damage Control II
Medium Ancillary Armor Repairer, Nanite Repair Paste
Multispectrum Energized Membrane II
Multispectrum Energized Membrane II
Magnetic Field Stabilizer II

Warp Scrambler II
50MN Quad LiF Restrained Microwarpdrive
Stasis Webifier II
Medium F-RX Compact Capacitor Booster, Navy Cap Booster 800

Heavy Ion Blaster II, Antimatter Charge M
Heavy Ion Blaster II, Antimatter Charge M
Heavy Ion Blaster II, Antimatter Charge M
Medium Energy Neutralizer II

Medium Auxiliary Nano Pump I
Medium Polycarbon Engine Housing II
Medium Polycarbon Engine Housing II


Hammerhead II x5
Infiltrator II x5
Hobgoblin II x5
Acolyte II x5
Warrior II x5
Hornet EC-300 x5


Antimatter Charge M x2000
Void M x1500
Null M x1500
Navy Cap Booster 800 x10
Nanite Repair Paste x300
"""

# After EVE Workbench "Vexor Navy Issue, speed tank alpha max skill"
# (https://eveworkbench.com/fitting/vexor-navy-issue/08d7ac9d-bf23-4642-d798-08d6e47469ce),
# read 2026-09-05; the three turret hardpoints it leaves bare, charges and
# cargo completed here. It is the only shield Vexor Navy Issue with a sourced
# rack: since the 2019 rebalance every published PvP fit for the hull is
# armour, so a shield kiter has to be finished by hand from a PvE one. The
# source filled all six lows with a second Capacitor Flux Coil II; one of the
# pair is left off here so LoSlot5 is the free slot the extra case needs, and
# the cap battery and the power diagnostic still hold the recharge up.
VEXOR_SPARE_EFT = """[Vexor Navy Issue, Shield Kite]

Drone Damage Amplifier II
Drone Damage Amplifier II
Drone Damage Amplifier II
Power Diagnostic System II
Capacitor Flux Coil II

Large Clarity Ward Enduring Shield Booster
Multispectrum Shield Hardener II
Large Compact Pb-Acid Cap Battery
100MN Y-S8 Compact Afterburner

250mm Railgun II, Spike M
250mm Railgun II, Spike M
250mm Railgun II, Spike M
Drone Link Augmentor I

Medium Capacitor Control Circuit I
Medium Capacitor Control Circuit I
Medium Capacitor Control Circuit I


Hammerhead II x5
Infiltrator II x5
Hobgoblin II x5
Acolyte II x5
Warrior II x5
Hornet EC-300 x5


Spike M x2000
Antimatter Charge M x2000
Javelin M x1000
Nanite Repair Paste x100
"""

# After the zKillboard fitted loss "Caracal" of 2026-09-05T04:39:43Z
# (https://zkillboard.com/kill/138218253/), read 2026-09-05; charges and cargo
# completed here. LoSlot3 carried a Mark I Compact Reactor Control Unit on the
# killmail and is left free below, because this is a primary fit and the extra
# case needs a slot the hull really has.
CARACAL_EFT = """[Caracal, Missile Kite]

Ballistic Control System II
Ballistic Control System II
Damage Control II

50MN Y-T8 Compact Microwarpdrive
Large F-S9 Regolith Compact Shield Extender
Large F-S9 Regolith Compact Shield Extender
Multispectrum Shield Hardener II
Faint Scoped Warp Disruptor

Heavy Missile Launcher II, Scourge Fury Heavy Missile
Heavy Missile Launcher II, Scourge Fury Heavy Missile
Heavy Missile Launcher II, Scourge Fury Heavy Missile
Heavy Missile Launcher II, Scourge Fury Heavy Missile
Heavy Missile Launcher II, Scourge Fury Heavy Missile

Medium EM Shield Reinforcer I
Medium EM Shield Reinforcer I
Medium Thermal Shield Reinforcer I


Hobgoblin II x2


Scourge Heavy Missile x2000
Caldari Navy Scourge Heavy Missile x500
Caldari Navy Inferno Heavy Missile x500
Inferno Fury Heavy Missile x500
Nanite Repair Paste x100
"""

# After the zKillboard fitted loss "Caracal" of 2026-09-05T18:14:35Z
# (https://zkillboard.com/kill/138232137/), read 2026-09-05; charges and cargo
# completed here. The killmail filled all four lows with a Signal Amplifier II;
# it is left off here so LoSlot3 is the free slot the extra case needs, and of
# the four lows it is the one the doctrine leans on least -- the damage control
# and the pair of ballistic controls are what the fit is for.
CARACAL_SPARE_EFT = """[Caracal, Rapid Light]

Damage Control II
Ballistic Control System II
Ballistic Control System II

50MN Quad LiF Restrained Microwarpdrive
Large Shield Extender II
Large Shield Extender II
Multispectrum Shield Hardener II
Warp Disruptor II

Rapid Light Missile Launcher II, Caldari Navy Inferno Light Missile
Rapid Light Missile Launcher II, Caldari Navy Inferno Light Missile
Rapid Light Missile Launcher II, Caldari Navy Inferno Light Missile
Rapid Light Missile Launcher II, Caldari Navy Inferno Light Missile
Rapid Light Missile Launcher II, Caldari Navy Inferno Light Missile

Medium EM Shield Reinforcer II
Medium Thermal Shield Reinforcer I
Medium Rocket Fuel Cache Partition I


Acolyte II x2


Caldari Navy Scourge Light Missile x1000
Scourge Precision Light Missile x1000
Scourge Fury Light Missile x1000
Caldari Navy Inferno Light Missile x825
Inferno Fury Light Missile x1000
Nanite Repair Paste x100
"""

# After the zKillboard fitted loss "Rorqual" of 2026-09-05T02:59:27Z
# (https://zkillboard.com/kill/138216539/), read 2026-09-05; charges and cargo
# completed here. The killmail's Dark Blood Heavy Energy Nosferatu is written
# as its Tech II equivalent. Heavy Water is what a Capital Industrial Core II
# burns; the Helium Fuel Block stack is the fuel bay the seeded Rorqual below
# actually carries, so the stored fit and the ship agree about it.
RORQUAL_EFT = """[Rorqual, Industrial Core]

Damage Control II
Drone Damage Amplifier II
Drone Damage Amplifier II
Drone Damage Amplifier II

Capital Ancillary Shield Booster, Cap Booster 3200
Capital Shield Extender II
Capital Shield Extender II
Multispectrum Shield Hardener II
Multispectrum Shield Hardener II
Heavy Warp Disruptor I
Heavy Warp Disruptor I

Capital Industrial Core II
Capital Moon Ore Compressor I
Pulse Activated Nexus Invulnerability Core
Heavy Energy Nosferatu II
Mining Foreman Burst II, Mining Laser Optimization Charge
Mining Foreman Burst II, Mining Laser Field Enhancement Charge
Mining Foreman Burst II, Mining Laser Efficiency Charge
Mining Foreman Burst II, Mining Equipment Preservation Charge

Capital Core Defense Field Extender I
Capital Core Defense Field Extender I
Capital Command Processor I


Curator II x25
Infiltrator II x12
Praetor II x10
Wasp II x10
Hornet II x20
Acolyte II x6
Warden II x4
Warrior II x5
Hornet EC-300 x5


Heavy Water x50000
Helium Fuel Block x800
Cap Booster 3200 x40
Mining Laser Optimization Charge x173
Mining Laser Field Enhancement Charge x173
Mining Laser Efficiency Charge x173
Mining Equipment Preservation Charge x173
Nanite Repair Paste x100
"""

# After the EVE Online forums "Carrier Ratting Guide" fit [Thanatos, Ratting]
# (https://forums.eveonline.com/t/carrier-ratting-guide/13913), read
# 2026-09-05; charges and cargo completed here. The guide's Energized Adaptive
# Nano Membrane II is written under the name CCP has given it since, and the
# T1 fighters it names are flown here as their Tech II versions.
THANATOS_EFT = """[Thanatos, Fighter Support]

Drone Damage Amplifier II
Drone Damage Amplifier II
Drone Damage Amplifier II
Drone Damage Amplifier II
Capital I-a Enduring Armor Repairer
Multispectrum Energized Membrane II

Drone Navigation Computer II
Drone Navigation Computer II
Omnidirectional Tracking Link II, Optimal Range Script
Omnidirectional Tracking Link II, Optimal Range Script
Omnidirectional Tracking Link II, Tracking Speed Script

Fighter Support Unit II
Fighter Support Unit II
Fighter Support Unit II
Networked Sensor Array
Cynosural Field Generator I

Capital Hyperspatial Velocity Optimizer I
Capital Hyperspatial Velocity Optimizer I
Capital Hyperspatial Velocity Optimizer I


Templar II x9
Firbolg II x9
Siren II x9
Dromi II x6


Liquid Ozone x400
Helium Isotopes x8000
Nanite Repair Paste x500
"""


@dataclass(frozen=True)
class Fit:
    """One stored fit, the rack that matches it, and the deviations from it.

    drop, extra and swap are the three single-module deviations the designed
    cases build. Each names a slot this fit's rack either occupies (drop,
    swap) or deliberately leaves free (extra), so every designed ship stays a
    rack a real hull could carry.

    The free slot is a uniform rule rather than a coincidence: every fit here
    leaves exactly one mid or low slot empty on its hull, and the extra case
    fills that one slot with the module `extra` names. The primaries were
    written that way from the start. Each second fit arrived from its source
    with every mid and low occupied, so one mid or low module was taken out of
    each and that module is what `extra` puts back.

    The layouts behind that were read on 2026-09-05 from the build the seed
    imports, attributes 14 hiSlots / 13 medSlots / 12 lowSlots / 1137 rigSlots:
    Dominix 6/5/7, Vexor Navy Issue 4/4/6, Caracal 5/5/4, all with three rigs.
    They are quoted here rather than asserted because sde.py imports typeDogma
    only for the types a mutaplasmid can touch -- 1,110 of 26,828 -- so a hull
    has no dogma row in the database to check against.
    """

    name: str
    eft: str
    slots: tuple[tuple[str, str], ...]
    guns: tuple[str, ...]
    ammo: str
    booster: str | None
    drop: str
    extra: tuple[str, str]
    swap: tuple[str, str]


@dataclass(frozen=True)
class Hull:
    """A hull, its stored fits, and the abyssal module one of its ships wears.

    ammo is the hull's cheat-sheet consumable, which every ship of the hull
    stocks whatever it is fitted for. A fit whose guns take something else
    carries both: the sheet's `holds:` lines can only split a fleet if the
    consumable they name stays on enough of the hull's ships, and a pilot who
    flies both of a hull's fits keeps both charges aboard anyway.
    """

    name: str
    fits: tuple[Fit, ...]
    ammo: str
    abyssal: tuple[str, str]


FITTED_HULLS = (
    Hull(
        name="Dominix",
        ammo="Antimatter Charge L",
        abyssal=("LoSlot0", "Large Abyssal Armor Repairer"),
        fits=(
            Fit(
                name="Sentry Ratting",
                eft=DOMINIX_EFT,
                slots=rack(
                    ("LoSlot", (
                        "Large Armor Repairer II", "Reactive Armor Hardener",
                        "Kinetic Armor Hardener II", "Drone Damage Amplifier II",
                        "Drone Damage Amplifier II", "Drone Damage Amplifier II",
                        "Drone Damage Amplifier II",
                    )),
                    ("MedSlot", (
                        "Large Cap Battery II", "Omnidirectional Tracking Link II",
                        "Drone Navigation Computer II", "Cap Recharger II",
                    )),
                    ("HiSlot", (
                        "Drone Link Augmentor II", "Drone Link Augmentor II",
                        "Heavy Energy Neutralizer II", "Heavy Energy Neutralizer II",
                        "Heavy Energy Neutralizer II", "425mm Prototype Gauss Gun",
                    )),
                    ("RigSlot", (
                        "Large Thermal Armor Reinforcer I", "Large Thermal Armor Reinforcer I",
                        "Large Auxiliary Nano Pump I",
                    )),
                ),
                guns=("HiSlot5",),
                ammo="Antimatter Charge L",
                booster=None,
                drop="LoSlot6",
                # A second cap battery in the mid the fit leaves free: the
                # subtler multiset case, where the deviation is a count and
                # not a type.
                extra=("MedSlot4", "Large Cap Battery II"),
                swap=("LoSlot1", "Multispectrum Energized Membrane II"),
            ),
            Fit(
                name="Blaster Brawl",
                eft=DOMINIX_SPARE_EFT,
                slots=rack(
                    ("LoSlot", (
                        "Damage Control II", "Reinforced Bulkheads II",
                        "Reinforced Bulkheads II", "Reinforced Bulkheads II",
                        "Reinforced Bulkheads II", "Reinforced Bulkheads II",
                    )),
                    ("MedSlot", (
                        "Warp Scrambler II", "Heavy Stasis Grappler II",
                        "500MN Y-T8 Compact Microwarpdrive",
                        "Heavy F-RX Compact Capacitor Booster", "Large Micro Jump Drive",
                    )),
                    ("HiSlot", (
                        "Ion Blaster Cannon II", "Ion Blaster Cannon II",
                        "Ion Blaster Cannon II", "Ion Blaster Cannon II",
                        "Ion Blaster Cannon II", "Ion Blaster Cannon II",
                    )),
                    ("RigSlot", (
                        "Large Transverse Bulkhead I", "Large Transverse Bulkhead I",
                        "Large Transverse Bulkhead I",
                    )),
                ),
                guns=("HiSlot0", "HiSlot1", "HiSlot2", "HiSlot3", "HiSlot4", "HiSlot5"),
                ammo="Void L",
                booster="Navy Cap Booster 800",
                drop="LoSlot5",
                extra=("LoSlot6", "Reinforced Bulkheads II"),
                swap=("MedSlot1", "Stasis Webifier II"),
            ),
        ),
    ),
    Hull(
        name="Vexor Navy Issue",
        ammo="Antimatter Charge M",
        abyssal=("MedSlot1", "50MN Abyssal Microwarpdrive"),
        fits=(
            Fit(
                name="Drone Roam",
                eft=VEXOR_EFT,
                slots=rack(
                    ("LoSlot", (
                        "Damage Control II", "Medium Ancillary Armor Repairer",
                        "Multispectrum Energized Membrane II",
                        "Multispectrum Energized Membrane II",
                        "Magnetic Field Stabilizer II",
                    )),
                    ("MedSlot", (
                        "Warp Scrambler II", "50MN Quad LiF Restrained Microwarpdrive",
                        "Stasis Webifier II", "Medium F-RX Compact Capacitor Booster",
                    )),
                    ("HiSlot", (
                        "Heavy Ion Blaster II", "Heavy Ion Blaster II", "Heavy Ion Blaster II",
                        "Medium Energy Neutralizer II",
                    )),
                    ("RigSlot", (
                        "Medium Auxiliary Nano Pump I", "Medium Polycarbon Engine Housing II",
                        "Medium Polycarbon Engine Housing II",
                    )),
                ),
                guns=("HiSlot0", "HiSlot1", "HiSlot2"),
                ammo="Antimatter Charge M",
                booster="Navy Cap Booster 800",
                drop="LoSlot2",
                extra=("LoSlot5", "Magnetic Field Stabilizer II"),
                swap=("LoSlot3", "Medium Armor Repairer II"),
            ),
            Fit(
                name="Shield Kite",
                eft=VEXOR_SPARE_EFT,
                slots=rack(
                    ("LoSlot", (
                        "Drone Damage Amplifier II", "Drone Damage Amplifier II",
                        "Drone Damage Amplifier II", "Power Diagnostic System II",
                        "Capacitor Flux Coil II",
                    )),
                    ("MedSlot", (
                        "Large Clarity Ward Enduring Shield Booster",
                        "Multispectrum Shield Hardener II",
                        "Large Compact Pb-Acid Cap Battery",
                        "100MN Y-S8 Compact Afterburner",
                    )),
                    ("HiSlot", (
                        "250mm Railgun II", "250mm Railgun II", "250mm Railgun II",
                        "Drone Link Augmentor I",
                    )),
                    ("RigSlot", (
                        "Medium Capacitor Control Circuit I", "Medium Capacitor Control Circuit I",
                        "Medium Capacitor Control Circuit I",
                    )),
                ),
                guns=("HiSlot0", "HiSlot1", "HiSlot2"),
                ammo="Spike M",
                booster=None,
                drop="LoSlot2",
                extra=("LoSlot5", "Capacitor Flux Coil II"),
                swap=("MedSlot3", "50MN Quad LiF Restrained Microwarpdrive"),
            ),
        ),
    ),
    Hull(
        name="Caracal",
        ammo="Scourge Heavy Missile",
        abyssal=("LoSlot0", "Abyssal Ballistic Control System"),
        fits=(
            Fit(
                name="Missile Kite",
                eft=CARACAL_EFT,
                slots=rack(
                    ("LoSlot", (
                        "Ballistic Control System II", "Ballistic Control System II",
                        "Damage Control II",
                    )),
                    ("MedSlot", (
                        "50MN Y-T8 Compact Microwarpdrive",
                        "Large F-S9 Regolith Compact Shield Extender",
                        "Large F-S9 Regolith Compact Shield Extender",
                        "Multispectrum Shield Hardener II", "Faint Scoped Warp Disruptor",
                    )),
                    ("HiSlot", (
                        "Heavy Missile Launcher II", "Heavy Missile Launcher II",
                        "Heavy Missile Launcher II", "Heavy Missile Launcher II",
                        "Heavy Missile Launcher II",
                    )),
                    ("RigSlot", (
                        "Medium EM Shield Reinforcer I", "Medium EM Shield Reinforcer I",
                        "Medium Thermal Shield Reinforcer I",
                    )),
                ),
                guns=("HiSlot0", "HiSlot1", "HiSlot2", "HiSlot3", "HiSlot4"),
                ammo="Scourge Heavy Missile",
                booster=None,
                # One of the pair of extenders, so the deviation is a count
                # rather than a type: the ship still carries a Large F-S9
                # Regolith Compact Shield Extender and is still one short of
                # the fit.
                drop="MedSlot2",
                extra=("LoSlot3", "Mark I Compact Reactor Control Unit"),
                swap=("LoSlot2", "Missile Guidance Enhancer II"),
            ),
            Fit(
                name="Rapid Light",
                eft=CARACAL_SPARE_EFT,
                slots=rack(
                    ("LoSlot", (
                        "Damage Control II", "Ballistic Control System II",
                        "Ballistic Control System II",
                    )),
                    ("MedSlot", (
                        "50MN Quad LiF Restrained Microwarpdrive", "Large Shield Extender II",
                        "Large Shield Extender II", "Multispectrum Shield Hardener II",
                        "Warp Disruptor II",
                    )),
                    ("HiSlot", (
                        "Rapid Light Missile Launcher II", "Rapid Light Missile Launcher II",
                        "Rapid Light Missile Launcher II", "Rapid Light Missile Launcher II",
                        "Rapid Light Missile Launcher II",
                    )),
                    ("RigSlot", (
                        "Medium EM Shield Reinforcer II", "Medium Thermal Shield Reinforcer I",
                        "Medium Rocket Fuel Cache Partition I",
                    )),
                ),
                guns=("HiSlot0", "HiSlot1", "HiSlot2", "HiSlot3", "HiSlot4"),
                ammo="Caldari Navy Inferno Light Missile",
                booster=None,
                drop="MedSlot2",
                extra=("LoSlot3", "Signal Amplifier II"),
                swap=("MedSlot4", "Faint Scoped Warp Disruptor"),
            ),
        ),
    ),
)

# One ship per case, per stored fit. The pair is the difference the ship must
# show against the fit it was built from -- (modules missing, modules extra) --
# and it is asserted after the estate is written, so a case that stopped
# demonstrating what it is named after fails the seed. A missing count of None
# means the whole rack, which is what an empty hull is missing.
DESIGNED_CASES = (
    ("exact",    "full",   (0, 0)),
    ("permuted", "full",   (0, 0)),
    ("missing",  "loaded", (1, 0)),
    ("extra",    "full",   (0, 1)),
    ("swapped",  "none",   (1, 1)),
    ("abyssal",  "loaded", (1, 1)),
    ("empty",    "none",   (None, 0)),
)
CASE_DIFFERENCE = {case: difference for case, _stock, difference in DESIGNED_CASES}

# Built against a hull's first fit only. A second abyssal ship would say
# nothing the first does not, and every one of them spends a module out of the
# finite corpus; the packaged stack is a property of the hull rather than of
# any fit, so a second one would only inflate the counts.
FIRST_FIT_ONLY = ("abyssal",)

# The bulk fleet's shape, in ships per hundred. Weighted towards exact matches
# because that is what an estate looks like: most ships are fitted the way
# their owner meant, and the interesting answer is the minority that are not.
BULK_MIX = (("exact", 40), ("missing", 20), ("extra", 20), ("swapped", 10), ("empty", 10))

# The floor every fit of a fitted hull has to clear. Seventy bulk ships per
# hull, two fits drawn between them and forty exact in a hundred put fourteen
# or so on each fit before the designed exact and permuted pair is counted; the
# draw is uneven, and the thinnest population at FLEET_SEED is 11. A fit that
# fell under this would still print a plausible-looking number while having
# stopped being demonstrated.
MIN_FIT_MATCHES = 10

# Cargo rounds by stocking level. "loaded" and "none" carry nothing in the
# hold; the difference between them is whether the guns are loaded at all,
# which is what makes `holds:"<ammo>"<N` split the fleet three ways rather
# than two.
STOCK_ROUNDS = {"full": (2000, 2600, 3200, 4000), "loaded": (0,), "none": (0,)}
LOADED_ROUNDS = (20, 50, 100)

PASTE = "Nanite Repair Paste"
# Deliberately small and straddling 100, so `holds:"Nanite Repair Paste"<100`
# has ships on both sides of it instead of being a way of writing "all".
PASTE_UNITS = (0, 15, 40, 75, 99, 100, 110, 120)
BOOSTER_UNITS = (0, 25, 60, 90, 120)

DRONE_TYPES = ("Hobgoblin II", "Hammerhead II", "Ogre II", "Warrior II", "Acolyte II")
DRONE_UNITS = (2, 3, 5)

# Jita and Amarr are the constants above; these two are looked up by name,
# because a station id typed from memory lands the fleet in a plausible-looking
# region that is not the one the station is in, and nothing in the UI says so.
FLEET_STATION_NAMES = (
    "Dodixie IX - Moon 20 - Federation Navy Assembly Plant",
    "Rens VI - Moon 8 - Brutor Tribe Treasury",
)

BERTH_OWNERS = (
    ("character", PILOT, "Hangar"),
    ("character", ALT, "Hangar"),
    ("corporation", CORP, "CorpSAG1"),
)

# The Rorqual is here for the bay forms and for the flags that belong to no
# bay at all: the ore hold and the ship maintenance bay are in none of
# fitting.HOLD_BAYS, so no bay form's count must ever see them however full
# they are.
RORQUAL_HOLDS = (
    ("MedSlot0", "Cap Booster 3200", 20, 0),
    ("SpecializedFuelBay", "Helium Fuel Block", 5000, 0),
    ("SpecializedAmmoHold", "Antimatter Charge L", 8000, 0),
    ("SpecializedOreHold", "Veldspar", 250000, 0),
    ("ShipHangar", "Rifter", 3, 0),
    ("DroneBay", "Curator II", 25, 1),
    ("DroneBay", "Hammerhead II", 5, 1),
    # What a Capital Industrial Core II burns, so the inspector's consumables
    # line has something to compare the stored fit against.
    ("Cargo", "Heavy Water", 50000, 0),
    ("Cargo", PASTE, 40, 0),
)
# (type, units aboard) for the two rows above that no bay form may count. The
# units are checked too, so the assertion fails on a Rorqual that lost its ore
# rather than passing because there was nothing to count.
RORQUAL_UNCOUNTED = (("Veldspar", 250000), ("Rifter", 3))

# The Thanatos is here for the fighter tubes and the fleet hangar: the
# `fighters/` and `fleet/` forms each count one of them, and its drone bay
# is what keeps `drones/` honest -- the hobgoblins there are the only ones
# the form may count, whatever the fleet hangar carries.
THANATOS_HOLDS = (
    ("FighterTube0", "Templar II", 9, 1),
    ("FighterTube1", "Firbolg II", 9, 1),
    ("FighterTube2", "Siren II", 9, 1),
    ("FighterBay", "Dromi II", 6, 1),
    ("FighterBay", "Templar II", 9, 1),
    ("DroneBay", "Hobgoblin II", 5, 1),
    ("DroneBay", "Warrior II", 5, 1),
    ("FleetHangar", "Antimatter Charge L", 4000, 0),
    ("FleetHangar", PASTE, 80, 0),
    ("Cargo", "Liquid Ozone", 400, 0),
    ("Cargo", "Helium Isotopes", 8000, 0),
)

# The Rifter's hull carries no stored fit, so both polarities of `is:fit` have
# to leave it out: a ship cannot be said to match or deviate from fits that do
# not exist. The rack is nonetheless a real one -- after Ashy in Space, "Fit
# Kitchen: Rifter", fit [Rifter, 2021 Scram Kite Plated]
# (https://ashy.vargur.dev/fit-kitchen-rifter/), read 2026-09-05; charges and
# cargo completed here -- so an empty-looking hull is never the reason a
# reader believes the chip.
RIFTER_SLOTS = rack(
    ("LoSlot", (
        "Small Ancillary Armor Repairer", "IFFA Compact Damage Control",
        "Gyrostabilizer II", "400mm Rolled Tungsten Compact Plates",
    )),
    ("MedSlot", (
        "Warp Scrambler II", "Fleeting Compact Stasis Webifier",
        "1MN Y-S8 Compact Afterburner",
    )),
    ("HiSlot", ("200mm AutoCannon II", "200mm AutoCannon II", "200mm AutoCannon II")),
    ("RigSlot", (
        "Small Projectile Ambit Extension II", "Small Ancillary Current Router II",
        "Small Explosive Armor Reinforcer I",
    )),
)
RIFTER_HOLDS = (
    ("HiSlot0", "Republic Fleet EMP S", 100, 0),
    ("HiSlot1", "Republic Fleet EMP S", 100, 0),
    ("HiSlot2", "Republic Fleet EMP S", 100, 0),
    ("Cargo", "Barrage S", 1000, 0),
    ("Cargo", "Hail S", 1000, 0),
    ("Cargo", "Republic Fleet EMP S", 500, 0),
    ("Cargo", "Republic Fleet Fusion S", 500, 0),
    ("Cargo", "Republic Fleet Phased Plasma S", 500, 0),
    ("Cargo", PASTE, 60, 0),
)

# A Dominix refitted from memory of the Sentry Ratting fit and got mostly
# wrong. Twelve of the fit's twenty modules are absent from the rack -- a meta
# repairer and a meta cap recharger where the fit wants Tech II, a thermal
# hardener for the kinetic one, a damage control and a Tech I amplifier in two
# of the four amplifier slots, a shield extender and an afterburner in the
# mids, one meta link augmentor, a nosferatu and a second gun for two of the
# neutralisers, and two rigs of the wrong kind -- and the holds are short or
# wrong in the same spirit. It exists for the Compare deviation window: the
# designed cases differ from their fit by one module, which is the right test
# and a dull picture, and this ship is the picture, a rack red and green down
# most of its length with a shopping list worth copying. BOTCHED_WRONG pins
# the count so a later edit cannot quietly make it a little wrong instead. It
# berths alone in a station no other seeded asset uses, so one `loc:` chip
# isolates it on screen.
BOTCHED_NAME = "Sentry Domi (bad refit)"
BOTCHED_STATION = "Hek VIII - Moon 12 - Boundless Creation Factory"
BOTCHED_WRONG = 12
BOTCHED_DOMINIX_SLOTS = rack(
    ("LoSlot", (
        "Large Armor Repairer I", "Reactive Armor Hardener",
        "Thermal Armor Hardener II", "Drone Damage Amplifier II",
        "Drone Damage Amplifier II", "Drone Damage Amplifier I", "Damage Control II",
    )),
    ("MedSlot", (
        "Large Shield Extender II", "Omnidirectional Tracking Link II",
        "10MN Afterburner II", "Cap Recharger I",
    )),
    ("HiSlot", (
        "Drone Link Augmentor II", "Drone Link Augmentor I",
        "Heavy Energy Neutralizer II", "Heavy Energy Nosferatu II",
        "425mm Prototype Gauss Gun", "425mm Prototype Gauss Gun",
    )),
    ("RigSlot", (
        "Large Thermal Armor Reinforcer I", "Large Trimark Armor Pump I",
        "Large Explosive Armor Reinforcer I",
    )),
)
BOTCHED_DOMINIX_HOLDS = (
    ("HiSlot4", "Antimatter Charge L", 40, 0),
    ("HiSlot5", "Antimatter Charge L", 40, 0),
    ("DroneBay", "Warden II", 2, 1),
    ("DroneBay", "Garde II", 5, 1),
    ("DroneBay", "Hornet II", 5, 1),
    ("DroneBay", "Hammerhead II", 5, 1),
    ("Cargo", "Antimatter Charge L", 400, 0),
    ("Cargo", "Optimal Range Script", 2, 0),
    ("Cargo", PASTE, 20, 0),
)

# The two stacks of rounds that must never reach a `holds:` count. The
# container's contents hang off the container's own item id, one level below
# the ship, and the hangar stack is not inside anything at all.
CONTAINER_TYPE = "Small Standard Container"
CONTAINER_ROUNDS = 5000
HANGAR_ROUNDS = 20000


@dataclass(frozen=True)
class Berth:
    """Where a ship sits and who owns it: one cell of the owner/station spread."""

    owner_type: str
    owner_id: int
    flag: str
    station: int
    system: int
    region: int


@dataclass
class Fleet:
    """What the fleet builder leaves behind for the checks to work against."""

    ships: list[int] = field(default_factory=list)
    designed: list[tuple[Hull, Fit, str, int]] = field(default_factory=list)
    packaged: set[int] = field(default_factory=set)
    rifter: int = 0
    botched: int = 0
    rorqual: int = 0
    container: int = 0
    container_ship: int = 0
    container_berth: Berth | None = None
    hangar_stack: int = 0


def station_by_name(conn, name: str) -> tuple[int, int, int]:
    """(station, system, region) ids for one SDE station, by its full name."""
    row = conn.execute(
        "SELECT station_id, system_id, region_id FROM sde_stations WHERE name = ? COLLATE NOCASE",
        (name,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"the imported SDE has no station named {name!r}")
    return int(row["station_id"]), int(row["system_id"]), int(row["region_id"])


def fleet_berths(conn) -> list[Berth]:
    """The owner/station cells the fleet is dealt round-robin into."""
    stations = [(JITA_4_4, JITA_SYS, THE_FORGE), (AMARR_VIII, AMARR_SYS, DOMAIN)]
    stations += [station_by_name(conn, name) for name in FLEET_STATION_NAMES]
    return [
        Berth(owner_type, owner_id, flag, station, system, region)
        for station, system, region in stations
        for owner_type, owner_id, flag in BERTH_OWNERS
    ]


def case_slots(hull: Hull, fit: Fit, case: str) -> list[tuple[str, str]]:
    """The rack one designed case fits, as a mutation of one stored fit's own.

    The abyssal case leaves its slot empty here; the corpus module is moved
    into it afterwards, once the ship has an item id to sit inside. It is the
    one case that reads the hull rather than the fit, because the mutated
    module is chosen per hull.
    """
    if case == "exact":
        return list(fit.slots)
    if case == "permuted":
        return permuted(fit.slots)
    if case == "missing":
        return [(f, n) for f, n in fit.slots if f != fit.drop]
    if case == "extra":
        return [*fit.slots, fit.extra]
    if case == "swapped":
        return [(f, fit.swap[1] if f == fit.swap[0] else n) for f, n in fit.slots]
    if case == "abyssal":
        return [(f, n) for f, n in fit.slots if f != hull.abyssal[0]]
    if case == "empty":
        return []
    raise SystemExit(f"the fleet has no case named {case!r}")


def ship_holds(rng: random.Random, hull: Hull, fit: Fit, slots, stock: str) -> list[tuple]:
    """What a ship of this fit is carrying: loaded rounds, cargo and drones.

    Rounds go into every turret or launcher the rack actually has, which is
    what makes the whole-ship `holds:` form and its `cargo/` form disagree --
    a loaded charge sits on the module's slot flag and belongs to no bay.

    A fit whose guns take something other than its hull's cheat-sheet
    consumable stocks both, so a hull's sheet line keeps a population on both
    sides of its threshold however the fleet is split between the hull's fits.
    """
    holds: list[tuple] = []
    if stock != "none":
        for flag, _name in slots:
            if flag in fit.guns:
                holds.append((flag, fit.ammo, rng.choice(LOADED_ROUNDS), 0))
    holds.append(("Cargo", fit.ammo, rng.choice(STOCK_ROUNDS[stock]), 0))
    if fit.ammo != hull.ammo:
        holds.append(("Cargo", hull.ammo, rng.choice(STOCK_ROUNDS[stock]), 0))
    holds.append(("Cargo", PASTE, rng.choice(PASTE_UNITS), 0))
    if fit.booster:
        holds.append(("Cargo", fit.booster, rng.choice(BOOSTER_UNITS), 0))
    for name in rng.sample(DRONE_TYPES, rng.choice((0, 1, 2))):
        holds.append(("DroneBay", name, rng.choice(DRONE_UNITS), 1))
    return holds


def _ship_row(berth: Berth, item_id: int, hull_type: int, quantity: int, singleton: int) -> tuple:
    return (
        berth.owner_type, berth.owner_id, item_id, hull_type, quantity, berth.station,
        berth.flag, "station", singleton, berth.station, berth.system, berth.region,
    )


def _content_row(
    berth: Berth, item_id: int, type_id_: int, quantity: int,
    parent: int, flag: str, singleton: int,
) -> tuple:
    """One row sitting inside a ship or a container.

    is_singleton is what queries.SHIP_ROWS_CLAUSE reads to tell an assembled
    ship from a packaged stack, so it is set with care wherever a Ship-category
    row is involved: assembled and individually fitted things are 1, stacks of
    identical units are 0.
    """
    return (
        berth.owner_type, berth.owner_id, item_id, type_id_, quantity, parent,
        flag, "item", singleton, berth.station, berth.system, berth.region,
    )


def build_ship(conn, rows, ids, berth: Berth, hull_type: int, slots, holds) -> int:
    """Write one assembled ship and everything inside it; return its item id."""
    ship = next(ids)
    rows.append(_ship_row(berth, ship, hull_type, 1, 1))
    for flag, name in slots:
        rows.append(_content_row(berth, next(ids), type_id(conn, name), 1, ship, flag, 1))
    for flag, name, quantity, singleton in holds:
        if quantity <= 0:
            continue
        rows.append(
            _content_row(berth, next(ids), type_id(conn, name), quantity, ship, flag, singleton)
        )
    return ship


def slot_multiset(conn, slots) -> dict[int, int]:
    """{type_id: count} of a rack, the shape fitting.fitted_modules returns."""
    counts: dict[int, int] = {}
    for _flag, name in slots:
        tid = type_id(conn, name)
        counts[tid] = counts.get(tid, 0) + 1
    return counts


def verdict_multiset(parsed) -> dict[int, int]:
    """{type_id: count} of a parsed fit's modules, rigs and subsystems."""
    counts: dict[int, int] = {}
    for item in parsed.verdict_items():
        counts[item.type_id] = counts.get(item.type_id, 0) + item.quantity
    return counts


def claim_abyssal(conn, name: str, ship: int, flag: str, berth: Berth) -> None:
    """Move one corpus module out of a hangar and into a ship's slot.

    Re-homing an existing asset row rather than minting a fresh item keeps the
    module's abyssal_items and abyssal_attributes rows, so the ship both
    deviates under `fit:` and gives the inspector a full roll table for the
    module doing the deviating -- which is the composition the abyssal chip
    and the fit chip are supposed to have.
    """
    wanted = type_id(conn, name)
    row = conn.execute(
        "SELECT item_id FROM assets WHERE type_id = ? AND location_type = 'station' "
        "ORDER BY item_id LIMIT 1",
        (wanted,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"the abyssal corpus has no unfitted {name!r} left to fit")
    conn.execute(
        "UPDATE assets SET owner_type = ?, owner_id = ?, location_id = ?, location_flag = ?,"
        " location_type = 'item', root_location_id = ?, system_id = ?, region_id = ?"
        " WHERE item_id = ?",
        (
            berth.owner_type, berth.owner_id, ship, flag,
            berth.station, berth.system, berth.region, int(row["item_id"]),
        ),
    )


def store_fit(conn, eft: str) -> fits.ParsedFit:
    """Parse one EFT block through the app's own parser and store it."""
    parsed = fits.parse_eft(conn, eft)
    if not parsed.ok:
        raise SystemExit(f"a demo fit does not parse: {parsed.unknown or 'no hull'}")
    fits.save_fit(conn, parsed, parsed.name, eft)
    return parsed


def seed_fleet(conn) -> Fleet:
    """Build the whole fleet and store its fits; return what the checks need."""
    rng = random.Random(FLEET_SEED)
    ids = count(FIRST_FLEET_ID)
    berths = fleet_berths(conn)
    berth = cycle(berths)

    check_deviation_slots()
    stored: dict[str, fits.ParsedFit] = {}
    for hull in FITTED_HULLS:
        for fit in hull.fits:
            stored[fit.name] = store_fit(conn, fit.eft)
    store_fit(conn, RORQUAL_EFT)
    store_fit(conn, THANATOS_EFT)

    rows: list[tuple] = []
    fleet = Fleet()
    abyssal_swaps: list[tuple[Hull, int, Berth]] = []

    for hull in FITTED_HULLS:
        hull_type = type_id(conn, hull.name)
        for fit in hull.fits:
            first = fit is hull.fits[0]
            for case, stock, _difference in DESIGNED_CASES:
                if case in FIRST_FIT_ONLY and not first:
                    continue
                here = next(berth)
                slots = case_slots(hull, fit, case)
                holds = [] if case == "empty" else ship_holds(rng, hull, fit, slots, stock)
                ship = build_ship(conn, rows, ids, here, hull_type, slots, holds)
                fleet.ships.append(ship)
                fleet.designed.append((hull, fit, case, ship))
                if case == "abyssal":
                    abyssal_swaps.append((hull, ship, here))
                if case == "exact" and first and hull.name == "Dominix":
                    fleet.container_ship, fleet.container_berth = ship, here
        # A packaged pair of the hull: not an assembled ship, so it must fall
        # outside every ship-scoped chip in both polarities.
        packaged = next(ids)
        rows.append(_ship_row(next(berth), packaged, hull_type, 2, 0))
        fleet.packaged.add(packaged)

    cases = [case for case, share in BULK_MIX for _ in range(share * BULK_SHIPS // 100)]
    rng.shuffle(cases)
    for n, case in enumerate(cases):
        hull = FITTED_HULLS[n % len(FITTED_HULLS)]
        # Which of the hull's fits a bulk ship answers to is drawn rather than
        # alternated, so the two populations are not a function of the hull
        # rotation above -- and drawn from the seeded RNG, so it is the same
        # draw every run.
        fit = rng.choice(hull.fits)
        slots = case_slots(hull, fit, case)
        stock = rng.choice(("full", "loaded", "none"))
        holds = ship_holds(rng, hull, fit, slots, stock)
        fleet.ships.append(
            build_ship(conn, rows, ids, next(berth), type_id(conn, hull.name), slots, holds)
        )

    # Both capitals wear the rack their stored fit describes, so `fit:` calls
    # each of them a match. A half-empty capital beside a fifteen-module fit
    # would read as the diff being broken rather than as the demo being thin.
    fleet.rorqual = build_ship(
        conn, rows, ids, next(berth), type_id(conn, "Rorqual"),
        rack(
            ("LoSlot", (
                "Damage Control II", "Drone Damage Amplifier II", "Drone Damage Amplifier II",
                "Drone Damage Amplifier II",
            )),
            ("MedSlot", (
                "Capital Ancillary Shield Booster", "Capital Shield Extender II",
                "Capital Shield Extender II", "Multispectrum Shield Hardener II",
                "Multispectrum Shield Hardener II", "Heavy Warp Disruptor I",
                "Heavy Warp Disruptor I",
            )),
            ("HiSlot", (
                "Capital Industrial Core II", "Capital Moon Ore Compressor I",
                "Pulse Activated Nexus Invulnerability Core", "Heavy Energy Nosferatu II",
                "Mining Foreman Burst II", "Mining Foreman Burst II",
                "Mining Foreman Burst II", "Mining Foreman Burst II",
            )),
            ("RigSlot", (
                "Capital Core Defense Field Extender I", "Capital Core Defense Field Extender I",
                "Capital Command Processor I",
            )),
        ),
        RORQUAL_HOLDS,
    )
    thanatos = build_ship(
        conn, rows, ids, next(berth), type_id(conn, "Thanatos"),
        rack(
            ("LoSlot", (
                "Drone Damage Amplifier II", "Drone Damage Amplifier II",
                "Drone Damage Amplifier II", "Drone Damage Amplifier II",
                "Capital I-a Enduring Armor Repairer", "Multispectrum Energized Membrane II",
            )),
            ("MedSlot", (
                "Drone Navigation Computer II", "Drone Navigation Computer II",
                "Omnidirectional Tracking Link II", "Omnidirectional Tracking Link II",
                "Omnidirectional Tracking Link II",
            )),
            ("HiSlot", (
                "Fighter Support Unit II", "Fighter Support Unit II", "Fighter Support Unit II",
                "Networked Sensor Array", "Cynosural Field Generator I",
            )),
            ("RigSlot", (
                "Capital Hyperspatial Velocity Optimizer I",
                "Capital Hyperspatial Velocity Optimizer I",
                "Capital Hyperspatial Velocity Optimizer I",
            )),
        ),
        THANATOS_HOLDS,
    )
    fleet.rifter = build_ship(
        conn, rows, ids, next(berth), type_id(conn, "Rifter"), RIFTER_SLOTS, RIFTER_HOLDS
    )
    fleet.botched = build_ship(
        conn, rows, ids, Berth("character", PILOT, "Hangar", *station_by_name(conn, BOTCHED_STATION)),
        type_id(conn, "Dominix"), BOTCHED_DOMINIX_SLOTS, BOTCHED_DOMINIX_HOLDS,
    )
    fleet.ships += [fleet.rorqual, thanatos, fleet.rifter, fleet.botched]

    # A can of rounds in a ship's cargo, and a pile of them in a hangar. Both
    # are the estate a `holds:` count has to see past: the can's contents hang
    # off the can's own item id, one level below the ship, and the hangar stack
    # is not inside anything at all.
    can_berth = fleet.container_berth
    fleet.container = next(ids)
    rows.append(_content_row(
        can_berth, fleet.container, type_id(conn, CONTAINER_TYPE), 1,
        fleet.container_ship, "Cargo", 1,
    ))
    rows.append(_content_row(
        can_berth, next(ids), type_id(conn, "Antimatter Charge L"), CONTAINER_ROUNDS,
        fleet.container, "Unlocked", 0,
    ))
    fleet.hangar_stack = next(ids)
    rows.append(_ship_row(berths[0], fleet.hangar_stack, type_id(conn, "Antimatter Charge L"),
                          HANGAR_ROUNDS, 0))

    conn.executemany(
        "INSERT INTO assets (owner_type, owner_id, item_id, type_id, quantity, location_id,"
        " location_flag, location_type, is_singleton, root_location_id, system_id, region_id)"
        " VALUES (" + ",".join("?" * 12) + ")",
        rows,
    )
    for hull, ship, here in abyssal_swaps:
        claim_abyssal(conn, hull.abyssal[1], ship, hull.abyssal[0], here)
    # Named so the one ship built to be looked at can be picked out of a table
    # of Dominixes that all deviate from the same fit.
    conn.execute(
        "UPDATE assets SET custom_name = ? WHERE item_id = ?", (BOTCHED_NAME, fleet.botched)
    )
    conn.commit()

    check_designed(conn, fleet, stored)
    check_botched(conn, fleet, stored)
    check_holds(conn, fleet)
    check_is_fit(conn, fleet)
    check_fit_populations(conn)
    return fleet


def check_deviation_slots() -> None:
    """Each fit's deviation slots are the kind of slot the case needs.

    A drop or a swap has to name a slot the rack fills, or it changes nothing;
    an extra has to name one the rack leaves free, or build_ship writes two
    modules onto one flag and the ship stops being a rack the hull could
    carry. Both mistakes survive a careless edit of the tables above and
    neither is visible in the estate afterwards -- the ship stops
    demonstrating what its case is named after.
    """
    for hull in FITTED_HULLS:
        for fit in hull.fits:
            filled = {flag for flag, _name in fit.slots}
            for label, flag in (("drop", fit.drop), ("swap", fit.swap[0])):
                if flag not in filled:
                    raise SystemExit(f"{fit.name}'s {label} names {flag}, which the fit leaves free")
            if fit.extra[0] in filled:
                raise SystemExit(f"{fit.name}'s extra names {fit.extra[0]}, which the fit fills")
        if hull.abyssal[0] not in {flag for flag, _name in hull.fits[0].slots}:
            raise SystemExit(
                f"the {hull.name}'s abyssal module wants {hull.abyssal[0]}, "
                f"which {hull.fits[0].name} leaves free"
            )


def check_designed(conn, fleet: Fleet, stored: dict) -> None:
    """Every designed ship differs from its fit in exactly the intended way.

    Checked rather than trusted because the racks above are hand-written: a
    swap that accidentally named a module already in the fit, or a "missing"
    case whose slot the fit never filled, would demonstrate nothing while
    still looking right in the source.
    """
    for hull, fit, case, ship in fleet.designed:
        where = f"the {case} {hull.name} built from {fit.name}"
        aboard = fitting.fitted_modules(queries.fetch_fit(conn, ship))
        intended = slot_multiset(conn, case_slots(hull, fit, case))
        if case == "abyssal":
            intended[type_id(conn, hull.abyssal[1])] = 1
        if aboard != intended:
            raise SystemExit(f"{where} is not the rack it was built from")
        verdict = verdict_multiset(stored[fit.name])
        # The two matching cases are pinned against the stored fit directly as
        # well as through the difference below, because a rack and a fit that
        # were both wrong in the same way would still show no difference.
        if case in ("exact", "permuted") and aboard != verdict:
            raise SystemExit(f"{where} does not carry the fit's own modules")
        missing = sum(max(0, n - aboard.get(t, 0)) for t, n in verdict.items())
        extra = sum(max(0, n - verdict.get(t, 0)) for t, n in aboard.items())
        want_missing, want_extra = CASE_DIFFERENCE[case]
        if want_missing is None:
            want_missing = sum(verdict.values())
        if (missing, extra) != (want_missing, want_extra):
            raise SystemExit(
                f"{where} shows {missing} missing and {extra} extra, "
                f"not {want_missing} and {want_extra}"
            )


def check_botched(conn, fleet: Fleet, stored: dict) -> None:
    """The bad refit is as wrong as the compare window's demonstration needs.

    BOTCHED_WRONG of the fit's modules must be absent from the rack and as
    many strangers present. Every other check would still pass with a rack
    edited to be a little wrong, and the window's showpiece would be back to
    the one-module picture the designed cases already paint.
    """
    aboard = fitting.fitted_modules(queries.fetch_fit(conn, fleet.botched))
    verdict = verdict_multiset(stored["Sentry Ratting"])
    missing = sum(max(0, n - aboard.get(t, 0)) for t, n in verdict.items())
    extra = sum(max(0, n - verdict.get(t, 0)) for t, n in aboard.items())
    if (missing, extra) != (BOTCHED_WRONG, BOTCHED_WRONG):
        raise SystemExit(
            f"the bad refit shows {missing} missing and {extra} extra against "
            f"Sentry Ratting, not {BOTCHED_WRONG} and {BOTCHED_WRONG}"
        )
    station = station_by_name(conn, BOTCHED_STATION)[0]
    strangers = int(conn.execute(
        "SELECT COUNT(*) FROM assets WHERE root_location_id = ? AND item_id != ? "
        "AND location_id != ?",
        (station, fleet.botched, fleet.botched),
    ).fetchone()[0])
    if strangers:
        raise SystemExit(f"{strangers} other assets share the bad refit's station")


def check_holds(conn, fleet: Fleet) -> None:
    """Nothing one level below a ship, and nothing in a hangar, reaches a count.

    Both exclusions are properties of holds_count_sql's single level of
    nesting rather than of a filter it applies, which is exactly the kind of
    thing that breaks silently: the counts would get bigger.
    """
    ammo = "Antimatter Charge L"
    counted = sum(queries.holds_counts(conn, fleet.ships, ammo).values())
    estate = int(conn.execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM assets WHERE type_id = ?",
        (type_id(conn, ammo),),
    ).fetchone()[0])
    if estate - counted != CONTAINER_ROUNDS + HANGAR_ROUNDS:
        raise SystemExit(
            f"{estate - counted} rounds sit outside every ship's holds count, "
            f"not the {CONTAINER_ROUNDS + HANGAR_ROUNDS} put there deliberately"
        )

    for name, units in RORQUAL_UNCOUNTED:
        whole = queries.holds_counts(conn, [fleet.rorqual], name).get(fleet.rorqual, 0)
        if whole != units:
            raise SystemExit(f"the Rorqual holds {whole} {name}, not the {units} seeded")
        for bay in fitting.HOLD_BAYS:
            if queries.holds_counts(conn, [fleet.rorqual], name, bay).get(fleet.rorqual, 0):
                raise SystemExit(f"{name} in the Rorqual counted under holds:{bay}/")


def check_is_fit(conn, fleet: Fleet) -> None:
    """A hull with no stored fit, and a packaged stack, are in neither polarity."""
    for text in ("is:fit", "-is:fit"):
        where, params = omni.parse(text).where()
        matched = {int(r["item_id"]) for r in queries.fetch_assets(conn, where, params)}
        if not matched:
            raise SystemExit(f"{text} matched nothing, so it proves nothing")
        if fleet.rifter in matched:
            raise SystemExit(f"{text} matched the Rifter, whose hull has no stored fit")
        if matched & fleet.packaged:
            raise SystemExit(f"{text} matched a packaged stack, which is not an assembled ship")


# The Dominix line doubles as the proof that a hull's consumable survives its
# fleet being split between two fits, so it is named rather than positional.
AMMO_SHEET_LINE = 'holds:"Antimatter Charge L"<500'
# The paste line is a saved view as well as a sheet entry (see seed_views), so
# it is named once rather than written out in both places and drifting.
PASTE_SHEET_LINE = f'holds:"cargo/{PASTE}"<100'

# One line per bay form. The drone line counts the drone bay alone -- the
# fleet's hobgoblins are only ever seeded there, so it reads the same as it
# did when `drones/` still summed the fighter bay and the fleet hangar in --
# and the fighter and fleet lines are the Thanatos's two other bays.
CHEAT_SHEET = (
    AMMO_SHEET_LINE,
    PASTE_SHEET_LINE,
    'holds:"fuel/Helium Fuel Block">=1',
    'holds:"drones/Hobgoblin II">=5',
    'holds:"fighters/Templar II">=1',
    f'holds:"fleet/{PASTE}">=1',
    "is:fit",
    "-is:fit",
)


def check_fit_populations(conn) -> None:
    """Every stored fit is matched by ships, and the sheet's counts still split.

    A fit with nothing matching it is the failure this seed exists to prevent:
    the cheat sheet prints a zero, and the reader concludes the chip is broken
    rather than that the demo forgot to build the ships. The floor is the
    designed exact and permuted pair plus the bulk share the RNG deals each
    fit, which came to at least MIN_FIT_MATCHES for all six fitted-hull fits
    at FLEET_SEED; the capitals carry one ship each and only have to be
    non-zero.

    The consumable line is checked from the other side. `holds:` counts are
    only worth printing while some ships fall on each side of the threshold,
    and a hull whose second fit fires something else could quietly take every
    ship over the line with it.
    """
    for row in fits.list_fits(conn):
        name = row["name"]
        where, params = omni.parse(f'fit:"{name}"').where()
        matched = queries.count_assets(conn, where, params)
        floor = MIN_FIT_MATCHES if name in {f.name for h in FITTED_HULLS for f in h.fits} else 1
        if matched < floor:
            raise SystemExit(f'fit:"{name}" matches {matched} ships, fewer than the {floor} meant')

    ships = queries.count_ships(conn)
    for text in CHEAT_SHEET:
        where, params = omni.parse(text).where()
        left = queries.count_assets(conn, where, params)
        if not left:
            raise SystemExit(f"{text} leaves no rows, so the cheat sheet prints a zero")
        if text == AMMO_SHEET_LINE and left >= ships:
            raise SystemExit(f"{text} leaves all {ships} ships, so it splits nothing")


def seed_views(conn) -> None:
    """Two saved views, so the Load card opens on a library rather than a void.

    One sits on a digit and one does not, because those are the two shapes a
    row has and the badge is the only thing that tells a reader `1` recalls a
    view at all. Both filters are the demo's own questions -- what is docked
    in Jita, which ships are short of paste -- and both are counted before
    this returns: a saved view that leaves no rows teaches the reader that
    the library is broken rather than that the seed forgot to build the
    ships. The station is named out of the imported SDE for the reason
    type_id() exists: a name typed from memory lands the view somewhere
    plausible that is not where the fleet is.
    """
    station = conn.execute(
        "SELECT name FROM sde_stations WHERE station_id = ?", (JITA_4_4,)
    ).fetchone()
    if station is None:
        raise SystemExit(f"the imported SDE has no station {JITA_4_4}")
    docked = f'loc:"{station["name"]}" cat:Ship'

    jita, _replaced = views.save_view(conn, "Jita ships", views.ViewState(docked))
    views.set_slot(conn, jita.view_id, 1)
    views.save_view(conn, "Short of paste", views.ViewState(PASTE_SHEET_LINE))

    stored = views.list_views(conn)
    if [(v.name, v.slot) for v in stored] != [("Jita ships", 1), ("Short of paste", None)]:
        raise SystemExit(f"the library came out as {[(v.name, v.slot) for v in stored]}")
    for view in stored:
        where, params = omni.parse(view.state.filter).where()
        if not queries.count_assets(conn, where, params):
            raise SystemExit(f"the saved view {view.name!r} leaves no rows")


def print_cheat_sheet(conn) -> None:
    """The filters this fleet was built for, with the rows each one leaves.

    Counted here rather than written down, so the numbers are the database's
    and not a claim about it that drifts the first time a table above changes.
    """
    texts = list(CHEAT_SHEET)
    for row in fits.list_fits(conn):
        texts.append(f'fit:"{row["name"]}"')
        texts.append(f'-fit:"{row["name"]}"')
    texts.append("abyssal is:fitted")
    width = max(len(t) for t in texts)
    print()
    print("Filters to try in the Assets omnibox, and the rows each one leaves:")
    for text in texts:
        where, params = omni.parse(text).where()
        print(f"  {text:<{width}}  {queries.count_assets(conn, where, params):>7,}")
    print(f"  {'assembled ships in the estate':<{width}}  {queries.count_ships(conn):>7,}")
    print()
    print("Saved views (Ctrl+L for the library, or the digit for a slotted one):")
    for view in views.list_views(conn):
        digit = f"{view.slot}" if view.slot is not None else "-"
        print(f"  [{digit}] {view.name:<16}  {view.state.filter}")


def seed(conn) -> None:
    conn.executescript("""
        DELETE FROM characters;   DELETE FROM corporations; DELETE FROM assets;
        DELETE FROM wallets;      DELETE FROM prices;       DELETE FROM market_orders;
        DELETE FROM wallet_journal; DELETE FROM wallet_transactions; DELETE FROM names;
        DELETE FROM networth_snapshots;
        DELETE FROM abyssal_attributes; DELETE FROM abyssal_items;
        DELETE FROM fits;            DELETE FROM views;
    """)
    conn.executescript(f"""
        INSERT INTO characters (character_id, name, corporation_id, corporation_name,
                                scopes, enabled, include_corp, last_sync_at)
        VALUES ({PILOT}, 'Vex Aldaran', {CORP}, 'Test Holdings', 'esi-assets.read_assets.v1',
                1, 1, '2026-08-06T06:00:00+00:00'),
               ({ALT}, 'Alt Trader', {CORP}, 'Test Holdings', 'esi-assets.read_assets.v1',
                1, 0, '2026-08-06T06:00:00+00:00');

        INSERT INTO corporations (corporation_id, name, ticker, via_character_id)
        VALUES ({CORP}, 'Test Holdings', 'TSTH', {PILOT});

        INSERT INTO names (id, name, category, updated_at) VALUES
            (11, 'Kaari Vex',         'character',   '2026-08-01'),
            (12, 'Red Frog Freight',  'corporation', '2026-08-01'),
            (13, 'Caldari Navy',      'corporation', '2026-08-01');

        -- The stack of three Dominixes is packaged (is_singleton 0): three
        -- hulls cannot be one assembled ship, and left singleton the row would
        -- read to every ship-scoped chip as an empty hull that deviates from
        -- every stored fit.
        INSERT INTO assets (owner_type, owner_id, item_id, type_id, quantity, location_id,
                            location_flag, location_type, is_singleton, root_location_id,
                            system_id, region_id) VALUES
            ('character',   {PILOT}, 1, {CHARON},    1,        {JITA_4_4},  'Hangar',  'station', 1, {JITA_4_4},  {JITA_SYS},  {THE_FORGE}),
            ('character',   {PILOT}, 2, {DOMINIX},   3,        {JITA_4_4},  'Hangar',  'station', 0, {JITA_4_4},  {JITA_SYS},  {THE_FORGE}),
            ('character',   {PILOT}, 3, {TRITANIUM}, 25000000, {JITA_4_4},  'Hangar',  'station', 0, {JITA_4_4},  {JITA_SYS},  {THE_FORGE}),
            ('character',   {PILOT}, 6, {TRITANIUM}, 400000,   1,           'Cargo',   'item',    0, {JITA_4_4},  {JITA_SYS},  {THE_FORGE}),
            ('character',   {ALT},   4, {DOMINIX},   1,        {AMARR_VIII},'Hangar',  'station', 1, {AMARR_VIII},{AMARR_SYS}, {DOMAIN}),
            ('character',   {ALT},   7, {PLEX},      500,      {AMARR_VIII},'Hangar',  'station', 0, {AMARR_VIII},{AMARR_SYS}, {DOMAIN}),
            ('corporation', {CORP},  5, {TRITANIUM}, 900000,   {JITA_4_4},  'CorpSAG1','station', 0, {JITA_4_4},  {JITA_SYS},  {THE_FORGE});

        INSERT INTO wallets (owner_type, owner_id, division, balance) VALUES
            ('character',   {PILOT}, 1, 4200000000),
            ('character',   {ALT},   1, 90000000),
            ('corporation', {CORP},  1, 15000000000);

        INSERT INTO market_orders (owner_type, owner_id, order_id, type_id, location_id,
                                   is_buy_order, price, volume_remain, escrow) VALUES
            ('character', {PILOT}, 9001, {DOMINIX},   {JITA_4_4}, 0, 200000000, 2, 0),
            ('character', {PILOT}, 9002, {TRITANIUM}, {JITA_4_4}, 1, 4.5, 2000000, 9000000);

        -- Real Jita spreads, sampled 2026-08-06. The Charon is contract-priced,
        -- so its bid and ask are the same single number.
        INSERT INTO prices (type_id, buy_price, sell_price, source, samples, updated_at) VALUES
            ({CHARON},    1502000000, 1608000000, 'jita',         1, '2026-08-06T00:00:00+00:00'),
            ({DOMINIX},    148200000,  155700000, 'jita',         1, '2026-08-06T00:00:00+00:00'),
            ({TRITANIUM},       3.76,       3.99, 'jita',         1, '2026-08-06T00:00:00+00:00'),
            ({PLEX},         4528000,    4770000, 'jita',         1, '2026-08-06T00:00:00+00:00');
    """)

    base = datetime(2026, 8, 6, tzinfo=timezone.utc)

    journal = [
        ("market_transaction", -466_500_000.0, "Market: bought 3 Dominix", 11),
        ("brokers_fee",          -3_498_750.0, "Brokers fee", 13),
        ("transaction_tax",     -11_662_500.0, "Transaction tax", 13),
        ("bounty_prizes",        42_500_000.0, "Bounty prizes", 13),
        ("contract_price",   -1_680_000_000.0, "Contract price: Charon", 12),
        ("player_donation",     250_000_000.0, "Thanks for the hauling", 11),
        ("market_escrow",        -9_000_000.0, "Market escrow", 13),
    ]
    balance = 4_200_000_000.0
    rows = []
    for i, (ref, amount, desc, party) in enumerate(journal):
        balance += amount
        rows.append((
            "character", PILOT, 1, 5000 + i,
            (base - timedelta(days=i, hours=i)).isoformat(timespec="seconds"),
            ref, amount, balance, desc, None, party, PILOT, None, None, 0.0, None,
        ))
    conn.executemany(
        "INSERT INTO wallet_journal (owner_type,owner_id,division,entry_id,date,ref_type,"
        "amount,balance,description,reason,first_party_id,second_party_id,context_id,"
        "context_id_type,tax,tax_receiver_id) VALUES (" + ",".join("?" * 16) + ")",
        rows,
    )

    trades = [
        (1, TRITANIUM, 25_000_000,          4.02, 1),
        (2, DOMINIX,            3, 155_500_000.0, 1),
        (3, TRITANIUM, 10_000_000,          5.98, 0),
        (4, CHARON,             1, 1_680_000_000.0, 0),
        (5, PLEX,             500,   4_741_000.0, 1),
        (6, DOMINIX,            1, 182_500_000.0, 0),
    ]
    conn.executemany(
        "INSERT INTO wallet_transactions (owner_type,owner_id,division,transaction_id,date,"
        "type_id,quantity,unit_price,is_buy,is_personal,client_id,location_id,journal_ref_id) "
        "VALUES (" + ",".join("?" * 13) + ")",
        [
            (
                "character", PILOT, 1, tid,
                (base - timedelta(days=tid * 2)).isoformat(timespec="seconds"),
                type_id, qty, price, buy, 1, 11, JITA_4_4, None,
            )
            for tid, type_id, qty, price, buy in trades
        ],
    )
    conn.commit()

    # A net worth curve with some shape to it, rather than a flat line.
    for week, (pilot_mult, corp_mult) in enumerate(
        [(1.0, 1.0), (1.08, 1.03), (1.02, 1.09), (1.21, 1.12), (1.34, 1.10), (1.29, 1.18)]
    ):
        conn.execute(
            "UPDATE wallets SET balance = ? WHERE owner_id = ?",
            (4_200_000_000 * pilot_mult, PILOT),
        )
        conn.execute(
            "UPDATE wallets SET balance = ? WHERE owner_id = ?",
            (15_000_000_000 * corp_mult, CORP),
        )
        taken = (base - timedelta(days=(5 - week) * 7)).isoformat(timespec="seconds")
        networth.take_snapshot(conn, taken)
    conn.commit()

    def place(n: int, item: dict) -> abyssal_corpus.Placement:
        if n % 4 == 2:
            return ("character", ALT, AMARR_VIII, "Hangar", AMARR_SYS, DOMAIN)
        if n % 4 == 3:
            return ("corporation", CORP, JITA_4_4, "CorpSAG1", JITA_SYS, THE_FORGE)
        return ("character", PILOT, JITA_4_4, "Hangar", JITA_SYS, THE_FORGE)

    abyssal_corpus.install_assets(conn, place)
    abyssal_corpus.store_all(conn)

    # Last, so its checks run against the finished estate and so the abyssal
    # modules it fits are already in a hangar to be taken out of.
    seed_fleet(conn)
    # After the fleet: the saved views count their own rows, and the ships
    # they filter down to do not exist until it has been built.
    seed_views(conn)
    conn.commit()


def main() -> int:
    conn = db.init()
    if sde.installed_build(conn) is None:
        print("Importing the SDE (one time, ~95 MB download)…")
        sde.ensure_current(conn, Settings(), lambda m, p: print(f"  [{p:3d}%] {m}"))
    seed(conn)
    print(f"Seeded {DB_PATH}")
    print(f"  {'Owner':<20} {'Jita buy':>20} {'Jita sell':>20}")
    for b in networth.compute_all(conn):
        print(f"  {b.owner_name:<20} {b.total_buy:>20,.2f} {b.total_sell:>20,.2f}")
    print_cheat_sheet(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
