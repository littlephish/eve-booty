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
from dataclasses import dataclass

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


@dataclass
class FitLine:
    """One display line of a grouped fit.

    type_id and meta_group_id are only set for slot-rack module lines -- they
    are what the dialog needs to show the module's icon and tint the line by
    its rarity. Hold/bay/cargo lines carry text alone: the "slot racks only"
    scope decision lives here, expressed in data, rather than being re-decided
    by whoever renders the lines. On a module-plus-charge line both ids are
    the module's, never the charge's -- the line is about the module.
    """

    text: str
    type_id: int | None = None
    meta_group_id: int | None = None


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


def _render_flat(items: list[sqlite3.Row]) -> list[FitLine]:
    return [FitLine(text=f"{r['quantity']:,} x {_name(r)}") for r in items]


def _render_slots(items: list[sqlite3.Row]) -> list[FitLine]:
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
        type_id = meta_group_id = None
        if modules:
            line = _name(modules[0])
            type_id = modules[0]["type_id"]
            meta_group_id = modules[0]["meta_group_id"]
            if len(modules) > 1:
                # Should never happen -- one module per numbered slot -- but
                # do not hide it silently if it ever does.
                line += ", " + ", ".join(_name(m) for m in modules[1:])
        else:
            line = "(charge with no module?)"
        if charges:
            loaded = ", ".join(f"{c['quantity']:,} x {_name(c)}" for c in charges)
            line += f"  —  loaded: {loaded}"
        lines.append(FitLine(text=line, type_id=type_id, meta_group_id=meta_group_id))
    return lines


def group_fit(rows: list[sqlite3.Row]) -> list[tuple[str, list[FitLine]]]:
    """Flat fetch_fit() rows -> [(group label, [FitLine, ...]), ...] in a
    sensible on-screen order."""
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


# -------------------------------------------------------- ESI fitting export
# The JSON shape used for a saved fitting: {name, ship_type_id, description,
# items: [{flag, quantity, type_id}, ...]}. This is what Pyfa reads from the
# clipboard -- Port.importAuto in pyfa-org/Pyfa's service/port/port.py treats
# any buffer whose first non-blank character is "{" as an ESI fit and hands it
# to importESI (service/port/esi.py).
#
# The flag numbers are inventory flag ids, taken from Pyfa's own INV_FLAGS in
# service/port/esi.py rather than guessed. They are *integers*: importESI
# compares flag against 5/87/158 numerically and sorts the item list by it.
# Note that ESI's own /characters/{id}/fittings/ endpoints have since moved to
# *string* flags ("HiSlot0"), so this payload is for Pyfa and the fit-sharing
# format, not for POSTing straight at ESI.
_ESI_SLOT_BASES = {
    "LoSlot": 11,       # LoSlot0-7   -> 11-18
    "MedSlot": 19,      # MedSlot0-7  -> 19-26
    "HiSlot": 27,       # HiSlot0-7   -> 27-34
    "RigSlot": 92,      # RigSlot0-7  -> 92-99
    "SubSystemSlot": 125,
    "ServiceSlot": 164,
}
_ESI_FLAG_CARGO = 5
_ESI_FLAG_DRONE_BAY = 87
_ESI_FLAG_FIGHTER_BAY = 158

# ESI's own limits on the fitting object, worth honouring even though we are
# handing this to Pyfa rather than to ESI: name 1-50 chars, description <= 500.
_ESI_NAME_MAX = 50
_ESI_DESCRIPTION_MAX = 500

_SLOT_FLAG_RE = re.compile(r"^([A-Za-z]+?)(\d+)$")


def _esi_slot_flag(location_flag: str) -> int | None:
    """"HiSlot3" -> 30. None for anything that is not a numbered slot, which
    includes FighterTube0-4 -- those are a bay, handled separately below."""
    m = _SLOT_FLAG_RE.match(location_flag or "")
    if not m:
        return None
    base = _ESI_SLOT_BASES.get(m.group(1))
    return None if base is None else base + int(m.group(2))


def to_esi_fitting(
    ship_name: str,
    ship_type_id: int,
    rows: list[sqlite3.Row],
    description: str = "",
) -> dict:
    """Render fetch_fit() rows as an ESI fitting object, ready to be JSON
    encoded and pasted into Pyfa's Import From Clipboard.

    Two things are worth knowing about the shape this produces:

    A charge sitting in a module's slot is exported as *cargo*, not at the
    module's own flag. That is what Pyfa's exporter does, and it is the only
    thing that survives the round trip: importESI builds a Module() for every
    flag it does not recognise as cargo/drone/fighter, Module() raises
    ValueError on a charge, and the except branch drops it silently. A charge
    left on a slot flag would therefore vanish on import; in cargo it arrives.

    Holds with no fitting representation (fleet hangar, ore hold, fuel bay and
    friends) are left out entirely -- same scope decision as to_eft above.
    Their flags would be read as slot numbers by importESI and turn into
    nonsense modules.
    """
    slots: list[tuple[int, int]] = []
    cargo: dict[int, int] = {}
    drones: dict[int, int] = {}
    fighters: dict[int, int] = {}

    def stack(bucket: dict[int, int], row: sqlite3.Row) -> None:
        type_id = int(row["type_id"])
        bucket[type_id] = bucket.get(type_id, 0) + int(row["quantity"] or 0)

    for r in rows:
        flag_name = r["location_flag"] or ""
        slot_flag = _esi_slot_flag(flag_name)
        if slot_flag is not None:
            if (r["category"] or "") == "Charge":
                stack(cargo, r)
            else:
                # One module per numbered slot, hence quantity 1 -- Pyfa's
                # exporter hardcodes the same.
                slots.append((slot_flag, int(r["type_id"])))
        elif flag_name == _EFT_DRONE_FLAG:
            stack(drones, r)
        elif flag_name in _EFT_FIGHTER_FLAGS:
            stack(fighters, r)
        elif flag_name == _EFT_CARGO_FLAG:
            stack(cargo, r)

    items = [{"flag": flag, "quantity": 1, "type_id": tid} for flag, tid in sorted(slots)]
    for flag, bucket in (
        (_ESI_FLAG_CARGO, cargo),
        (_ESI_FLAG_DRONE_BAY, drones),
        (_ESI_FLAG_FIGHTER_BAY, fighters),
    ):
        items += [{"flag": flag, "quantity": q, "type_id": t} for t, q in bucket.items()]

    return {
        "name": (ship_name or "Unnamed fit")[:_ESI_NAME_MAX],
        "ship_type_id": int(ship_type_id),
        "description": (description or "")[:_ESI_DESCRIPTION_MAX],
        "items": items,
    }
