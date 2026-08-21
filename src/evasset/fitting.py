"""Group a ship's contents (fetch_fit rows) into something a fit dialog can
render: fitted slots, drone bay, fighter bay, cargo, fleet hangar, and every
specialized hold.

Kept apart from src/evasset/ui/fit_dialog.py the same way queries.py,
pricing.py and networth.py are kept apart from the UI -- this is plain data
transformation with no Qt involved, so it can be unit tested without one.

Slot values (HiSlot0-7, MedSlot0-7, LoSlot0-7, RigSlot0-2, SubSystemSlot0-3)
and the general asset location_flag vocabulary (DroneBay, FighterBay,
FleetHangar, the SpecializedXHold family, etc.) are taken from ESI's own
enum, not guessed -- see eve-glue's location_flag.py
(github.com/esi/eve-glue) for the canonical list. A module and a charge
loaded into it share the very same location_flag (both, say, "HiSlot0"), so
they are told apart by SDE category ("Charge" vs everything else) rather
than by assuming which one a query returns first.
"""

from __future__ import annotations

import re
import sqlite3

_SLOT_GROUPS = [
    ("High slots", [f"HiSlot{i}" for i in range(8)]),
    ("Mid slots", [f"MedSlot{i}" for i in range(8)]),
    ("Low slots", [f"LoSlot{i}" for i in range(8)]),
    ("Rig slots", [f"RigSlot{i}" for i in range(8)]),
    ("Subsystem slots", [f"SubSystemSlot{i}" for i in range(8)]),
]

_HOLD_GROUPS = [
    ("Drone bay", ["DroneBay"]),
    ("Fighter bay", ["FighterBay", *[f"FighterTube{i}" for i in range(5)]]),
    ("Cargo hold", ["Cargo"]),
    ("Fleet hangar", ["FleetHangar"]),
    ("Ship maintenance bay", ["ShipHangar"]),
    ("Ore hold", ["SpecializedOreHold"]),
    ("Ice hold", ["SpecializedIceHold"]),
    ("Gas hold", ["SpecializedGasHold"]),
    ("Mineral hold", ["SpecializedMineralHold"]),
    ("Salvage hold", ["SpecializedSalvageHold"]),
    (
        "Ship hold",
        [
            "SpecializedShipHold",
            "SpecializedSmallShipHold",
            "SpecializedMediumShipHold",
            "SpecializedLargeShipHold",
            "SpecializedIndustrialShipHold",
        ],
    ),
    ("Ammo hold", ["SpecializedAmmoHold"]),
    ("Fuel bay", ["SpecializedFuelBay"]),
    ("Asteroid hold", ["SpecializedAsteroidHold"]),
    ("Command center hold", ["SpecializedCommandCenterHold"]),
    ("Planetary commodities hold", ["SpecializedPlanetaryCommoditiesHold"]),
    ("Material bay", ["SpecializedMaterialBay"]),
    ("Quafe bay", ["QuafeBay"]),
    ("Wardrobe", ["Wardrobe"]),
    ("Booster bay", ["BoosterBay"]),
    ("Corpse bay", ["CorpseBay"]),
]

_ALL_GROUPS = _SLOT_GROUPS + _HOLD_GROUPS
_FLAG_TO_GROUP = {flag: label for label, flags in _ALL_GROUPS for flag in flags}
_GROUP_ORDER = [label for label, _ in _ALL_GROUPS]
_SLOT_LABELS = {label for label, _ in _SLOT_GROUPS}


def _humanize_flag(flag: str) -> str:
    """Fallback label for a location_flag this module has no name for, so an
    unrecognised hold type still shows up under something readable instead of
    silently vanishing from the dialog."""
    spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", flag)
    return spaced.strip() or "Other"


def _slot_number(flag: str) -> int:
    m = re.search(r"(\d+)$", flag)
    return int(m.group(1)) if m else 0


def _name(row: sqlite3.Row) -> str:
    return row["custom_name"] or row["item"]


def _render_flat(items: list[sqlite3.Row]) -> list[str]:
    return [f"{r['quantity']:,} x {_name(r)}" for r in items]


def _render_slots(items: list[sqlite3.Row]) -> list[str]:
    by_flag: dict[str, list[sqlite3.Row]] = {}
    for r in items:
        by_flag.setdefault(r["location_flag"], []).append(r)

    lines = []
    for flag in sorted(by_flag, key=_slot_number):
        occupants = by_flag[flag]
        modules = [r for r in occupants if (r["category"] or "") != "Charge"]
        charges = [r for r in occupants if (r["category"] or "") == "Charge"]
        if not modules and not charges:
            continue
        if modules:
            line = _name(modules[0])
            if len(modules) > 1:
                # Should never happen -- one module per numbered slot -- but
                # do not hide it silently if it ever does.
                line += ", " + ", ".join(_name(m) for m in modules[1:])
        else:
            line = "(charge with no module?)"
        if charges:
            loaded = ", ".join(f"{c['quantity']:,} x {_name(c)}" for c in charges)
            line += f"  —  loaded: {loaded}"
        lines.append(line)
    return lines


def group_fit(rows: list[sqlite3.Row]) -> list[tuple[str, list[str]]]:
    """Flat fetch_fit() rows -> [(group label, [display line, ...]), ...] in
    a sensible on-screen order."""
    buckets: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        flag = r["location_flag"] or ""
        label = _FLAG_TO_GROUP.get(flag) or _humanize_flag(flag)
        buckets.setdefault(label, []).append(r)

    order = [g for g in _GROUP_ORDER if g in buckets]
    order += sorted(g for g in buckets if g not in _GROUP_ORDER)

    out = []
    for label in order:
        items = buckets[label]
        lines = _render_slots(items) if label in _SLOT_LABELS else _render_flat(items)
        if lines:
            out.append((label, lines))
    return out


# ---------------------------------------------------------------- EFT export
# EFT is the plain-text fit format Pyfa (and the game's own fit import) reads
# via "Import From Clipboard". The exact shape here -- slot export order,
# the "Module, Charge" syntax, "Name xN" with no thousands separator, and the
# blank-line spacing between sections -- is taken directly from Pyfa's own
# exporter, service/port/eft.py in the pyfa-org/Pyfa repository on GitHub
# (exportEft/exportModules/exportDrones/exportFighters/exportCargo), not
# guessed. In particular:
#   - slot order is LOW, MED, HIGH, RIG, SUBSYSTEM -- note this is not the
#     same order the fit dialog displays them in above, which goes high to
#     low because that reads more naturally top-to-bottom on screen
#   - quantities are "x5", never "x5,000" -- Pyfa's import regex for a
#     drone/cargo line is `x(?P<amount>\d+?)`, which does not allow a comma,
#     so a thousands-separated quantity would simply fail to parse
_EFT_SLOT_FLAGS = [
    [f"LoSlot{i}" for i in range(8)],
    [f"MedSlot{i}" for i in range(8)],
    [f"HiSlot{i}" for i in range(8)],
    [f"RigSlot{i}" for i in range(8)],
    [f"SubSystemSlot{i}" for i in range(8)],
]
_EFT_DRONE_FLAG = "DroneBay"
_EFT_FIGHTER_FLAGS = ["FighterBay", *[f"FighterTube{i}" for i in range(5)]]
_EFT_CARGO_FLAG = "Cargo"


def _eft_stack(row: sqlite3.Row) -> str:
    return f"{_name(row)} x{row['quantity']}"


def to_eft(ship_name: str, rows: list[sqlite3.Row]) -> str:
    """Render fetch_fit() rows as EFT text, ready to paste into Pyfa's
    Import From Clipboard.

    Only modules (with at most one loaded charge each), drones, fighters and
    cargo travel in EFT format -- there is no such thing as an EFT line for a
    fleet hangar or an ore hold, so anything sitting in one of those is left
    out rather than emitted as a line Pyfa cannot parse.
    """
    by_flag: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_flag.setdefault(r["location_flag"] or "", []).append(r)

    module_racks: list[str] = []
    for flags in _EFT_SLOT_FLAGS:
        rack: list[str] = []
        for flag in flags:
            occupants = by_flag.get(flag)
            if not occupants:
                continue
            modules = [r for r in occupants if (r["category"] or "") != "Charge"]
            charges = [r for r in occupants if (r["category"] or "") == "Charge"]
            if not modules:
                continue
            line = _name(modules[0])
            if charges:
                line += f", {_name(charges[0])}"
            rack.append(line)
        if rack:
            module_racks.append("\n".join(rack))

    sections: list[str] = []
    if module_racks:
        sections.append("\n\n".join(module_racks))

    minions: list[str] = []
    drones = by_flag.get(_EFT_DRONE_FLAG, [])
    if drones:
        minions.append("\n".join(_eft_stack(r) for r in drones))
    fighters = [r for flag in _EFT_FIGHTER_FLAGS for r in by_flag.get(flag, [])]
    if fighters:
        minions.append("\n".join(_eft_stack(r) for r in fighters))
    if minions:
        sections.append("\n\n".join(minions))

    cargo = by_flag.get(_EFT_CARGO_FLAG, [])
    if cargo:
        sections.append("\n".join(_eft_stack(r) for r in cargo))

    header = f"[{ship_name}, EVE Assets export]"
    return f"{header}\n\n" + "\n\n\n".join(sections)
