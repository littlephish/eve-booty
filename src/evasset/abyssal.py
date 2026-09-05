"""Abyssal (mutated) module rolls: polarity, roll quality, rendering, and the fetch.

An abyssal item is the one thing in the estate whose stats are not a
property of its type. ESI's `/dogma/dynamic/items/{type_id}/{item_id}` is
the only route that knows them (public, no token, one item per call; the
research notes in docs/research/abyssal-stats.md cite the spec and live
responses, 2026-09-01), and the SDE supplies everything needed to read a
roll as a human number: attribute names and units, the source type's base
values, and the mutaplasmid's min..max multiplier range.

Everything here is Qt-free so the arithmetic tests without a window. The
read-side SQL that joins the tables lives in queries.py, as it does for
every other feature; this module owns the rules that SQL cannot express
well -- which way is "good" for an attribute, how far along its possible
range a roll landed -- plus the write side, which is small enough not to
warrant a home in esi/sync.py: the fetch is an explicit user action, not
part of routine sync, because 500 items cost 500 calls.

Polarity is the part that deserves suspicion. A webifier's speedFactor is
a negative number that is better the more negative it is, but the very
same attribute on an afterburner is positive and better the larger it is.
CCP resolved this in the SDE itself: dynamicItemAttributes carries an
optional per-mutaplasmid highIsGood that overrides the attribute's own
flag, so the mutator's word wins here, then the attribute's default. The
POLARITY_OVERRIDES dict outranks both and is empty on purpose -- it exists
so a case the SDE provably gets wrong can be corrected the day a live
check proves it, and no earlier.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

from . import db
from .esi.client import ESIError

# Attribute id -> high_is_good, consulted before the SDE. Empty until a live
# comparison against the in-game roll display proves the SDE wrong for a
# specific attribute; nothing goes in here on suspicion.
POLARITY_OVERRIDES: dict[int, bool] = {}

# Curated short forms for the `stat:` chip, resolved to CCP's internal
# attribute name before the display/internal-name match. Names verified
# against the mutable-attribute list in docs/research/abyssal-stats.md
# section 2.4 (build 3487903). "rof" is speedMultiplier, not `speed`: the
# rate-of-fire attribute a mutaplasmid touches is the weapon upgrade's
# multiplier (204), and turret `speed` (51) is not mutable at all. "speed"
# is the prop mod's velocity bonus because abyssal prop mods vastly
# outnumber abyssal drones; the drone's own m/s figure is "velocity".
#
# An alias wins over a display name of the same spelling: `stat:mass>1`
# resolves to massAddition (796) even though attribute 4 displays "Mass".
# That is acceptable because only attributes a mutaplasmid rolls are worth
# searching, massAddition is the one that is mutable and attribute 4 is
# not (checked against the live SDE, 2026-09-02); anyone who does want the
# other can type its internal name, which no alias shadows.
STAT_ALIASES: dict[str, str] = {
    "cpu": "cpu",
    "pg": "power",
    "power": "power",
    "grid": "power",
    "cap": "capacitorNeed",
    "range": "maxRange",
    "optimal": "maxRange",
    "falloff": "falloff",
    "rof": "speedMultiplier",
    "web": "speedFactor",
    "speed": "speedFactor",
    "velocity": "maxVelocity",
    "damage": "damageMultiplier",
    "dmg": "damageMultiplier",
    "hp": "hp",
    "duration": "duration",
    "cycle": "duration",
    "neut": "energyNeutralizerAmount",
    "nos": "powerTransferAmount",
    "sig": "signatureRadiusBonus",
    "shield": "shieldBonus",
    "armor": "armorDamageAmount",
    "armour": "armorDamageAmount",
    "tracking": "trackingSpeed",
    "mass": "massAddition",
}

# Compact labels for the badge tooltip one-liner ("Web 61% · Range 88%"),
# keyed by internal attribute name; anything not listed falls back to the
# SDE display name. CCP's spelling is kept for game terms (Armor).
SHORT_LABELS: dict[str, str] = {
    "cpu": "CPU",
    "power": "PG",
    "capacitorNeed": "Cap",
    "speedFactor": "Speed",
    "maxRange": "Range",
    "falloff": "Falloff",
    "duration": "Duration",
    "damageMultiplier": "Damage",
    "speedMultiplier": "RoF",
    "missileDamageMultiplierBonus": "Missile dmg",
    "droneDamageBonus": "Drone dmg",
    "hp": "HP",
    "shieldBonus": "Shield boost",
    "armorDamageAmount": "Armor rep",
    "capacitorBonus": "Cap boost",
    "powerTransferAmount": "Nos",
    "energyNeutralizerAmount": "Neut",
    "signatureRadiusBonus": "Sig",
    "signatureRadiusAdd": "Sig add",
    "maxVelocity": "Velocity",
    "shieldCapacity": "Shield HP",
    "armorHP": "Armor HP",
    "armorHPBonusAdd": "Armor HP",
    "capacityBonus": "Capacity",
    "trackingSpeed": "Tracking",
    "massAddition": "Mass",
    "empFieldRange": "Smartbomb range",
    "miningAmount": "Mining",
    "reloadTime": "Reload",
    "emDamage": "EM",
    "explosiveDamage": "Explosive",
    "kineticDamage": "Kinetic",
    "thermalDamage": "Thermal",
    "hullEmDamageResonance": "EM res",
    "hullExplosiveDamageResonance": "Explosive res",
    "hullKineticDamageResonance": "Kinetic res",
    "hullThermalDamageResonance": "Thermal res",
}

# dogmaUnits whose displayed number is a modifier and reads wrongly without
# its sign: 109 Modifier Percent (rendered as (v-1)*100), 124 Modifier
# Relative Percent and 205 modifier realPercent (both shown as-is). A
# webifier's "-60%" and a damage mod's "+10%" both come from here.
SIGNED_UNITS = frozenset({109, 124, 205})

ITEM_ROUTE = "/dogma/dynamic/items/{type_id}/{item_id}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ polarity
def resolve_polarity(
    attribute_id: int,
    attr_high_is_good: int | bool | None,
    mutator_high_is_good: int | bool | None,
) -> bool:
    """Which direction is a better roll for this attribute on this mutator.

    Precedence: POLARITY_OVERRIDES, then the mutaplasmid's own highIsGood
    (CCP's per-mutator sign fix, present on sign-inverted cases such as a
    webifier's speedFactor), then the attribute's default. An attribute
    with no flag anywhere is taken as high-is-good, which is the SDE's own
    prevailing value and the least surprising reading of a bigger number.
    """
    if attribute_id in POLARITY_OVERRIDES:
        return POLARITY_OVERRIDES[attribute_id]
    if mutator_high_is_good is not None:
        return bool(mutator_high_is_good)
    if attr_high_is_good is not None:
        return bool(attr_high_is_good)
    return True


def roll_position(
    value: float | None,
    base: float | None,
    min_mult: float | None,
    max_mult: float | None,
) -> float | None:
    """Where a roll sits in the mutator's possible range, 0..1, or None.

    The range is base*min_mult .. base*max_mult, ordered by value rather
    than by multiplier so a negative base (a webifier's speedFactor of -60)
    still yields lo < hi; the orientation of "good" is applied afterwards
    by quality(). None when any input is unknown or the range is degenerate
    (a zero base, or equal multipliers) -- an unranked roll is shown as a
    value with no bar rather than a made-up percentage. Clamped, because
    float noise and the odd item whose real source differs from the base
    we hold can land a hair outside the range.
    """
    if value is None or base is None or min_mult is None or max_mult is None:
        return None
    lo, hi = sorted((base * min_mult, base * max_mult))
    if hi - lo <= 0:
        return None
    return min(1.0, max(0.0, (value - lo) / (hi - lo)))


def quality(position: float | None, high_is_good: bool) -> float | None:
    """The roll's goodness 0..1: the position, mirrored when low is good."""
    if position is None:
        return None
    return position if high_is_good else 1.0 - position


def verdict(value: float | None, base: float | None, high_is_good: bool) -> bool | None:
    """True for better than the un-mutated base, False for worse, None for equal or unknown."""
    if value is None or base is None or math.isclose(value, base, rel_tol=1e-9, abs_tol=1e-12):
        return None
    return (value > base) if high_is_good else (value < base)


# Dogma units whose displayed number runs the opposite way to the stored
# one: 108 and 111 show (1 - v) * 100 (queries.display_value_sql), so the
# raw low end of a range is the displayed high end.
INVERTED_DISPLAY_UNITS = frozenset({108, 111})


def display_high_is_good(high_is_good: bool | int | None, unit_id: int | None) -> bool:
    """Whether a bigger DISPLAYED number is the better roll.

    The stored polarity (resolve_polarity) is what quality and verdict are
    computed against, and for the inverted units the display transform
    runs the other way: a rate-of-fire multiplier is low-is-good raw and
    reads as a bonus percentage that is high-is-good. Every surface that
    lays display values out on a worst-to-best axis -- the inspector's
    meters, the search card's tracks -- needs this same translation, and
    queries._roll_dict does not perform it (confirmed 2026-09-02). An
    unknown polarity is taken as high-is-good, as resolve_polarity does.
    """
    high = True if high_is_good is None else bool(high_is_good)
    return not high if unit_id in INVERTED_DISPLAY_UNITS else high


def mean_quality(cells: dict[int, tuple[float, float | None]]) -> float | None:
    """The item's overall roll: the plain mean of its rankable qualities, 0..1.

    cells is one item's slice of queries.abyssal_roll_data's cells, attribute_id ->
    (display value, quality). Unrankable rolls (quality None: no base, or a
    degenerate range) are left out rather than counted as zero, and an item
    with nothing rankable has no mean at all -- the Roll column shows a
    blank, not a number that would sort it below every genuinely bad roll.
    An unweighted mean by decision: a typical item rolls its stats around the
    middle of the range, and "half its stats above 50%" is the reading a
    player gives it; weighting by attribute would need a per-type opinion of
    which stat matters, which is the user's, not the app's.
    """
    qualities = [q for _value, q in cells.values() if q is not None]
    if not qualities:
        return None
    return sum(qualities) / len(qualities)


# The word "Abyssal" as CCP places it inside a mutated type's name: leading
# ("Abyssal Stasis Webifier"), or after a size prefix ("50MN Abyssal
# Microwarpdrive", "Large Abyssal Shield Booster"). Word-bounded so a type
# that merely contains the letters is left alone; one removal only, since
# no name carries the word twice (checked against the 89 dynamic types in
# build 3487903, 2026-09-02).
_ABYSSAL_WORD = re.compile(r"\bAbyssal\s+")


def strip_type_prefix(name: str) -> str:
    """The abyssal type's name without the word "Abyssal", for chip labels.

    The chip already reads "Abyssal ·", so "Abyssal · Abyssal Stasis
    Webifier" would say it twice; "Abyssal · Stasis Webifier" reads once.
    Mutated drones ("Light Mutated Drone") have no such word and come back
    unchanged, as does anything whose whole name is the word -- an empty
    label is worse than a redundant one.
    """
    stripped = _ABYSSAL_WORD.sub("", name, count=1).strip()
    return stripped or name


# ---------------------------------------------------------------- rendering
def format_value(value: float, unit_id: int | None, unit_symbol: str | None) -> str:
    """Render a DISPLAY value (unit conversion already applied) for the UI.

    Two decimals under ten, none above -- a 1.11x multiplier needs the
    decimals and a 3,318 HP figure does not -- with a forced sign for the
    modifier units (SIGNED_UNITS), where "10%" would be ambiguous and "-60%"
    is the whole point. Percent and multiplier symbols attach directly, as
    the game shows them; other symbols take a space.
    """
    text = f"{value:.2f}" if abs(value) < 10 else f"{value:,.0f}"
    if float(text.replace(",", "")) == 0:
        text = text.lstrip("-")  # -0.3 rounds to "-0"; a zero has no sign
    if unit_id in SIGNED_UNITS and not text.startswith("-"):
        text = "+" + text
    if not unit_symbol:
        return text
    if unit_symbol in ("%", "x"):
        return text + unit_symbol
    return f"{text} {unit_symbol}"


def short_label(name: str | None, display_name: str | None) -> str:
    return SHORT_LABELS.get(name or "", display_name or name or "?")


# -------------------------------------------------------------------- fetch
# abyssal_items.status, and the third word fetch_abyssal_rolls reports for
# an item with no row yet, so the store, the queries and the inspector
# share one vocabulary.
STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_UNFETCHED = "unfetched"


def pending(
    conn: sqlite3.Connection,
    item_ids: list[int] | None = None,
    retry_missing: bool = False,
) -> list[tuple[int, int]]:
    """(item_id, type_id) for every abyssal asset whose rolls are not stored.

    Gated on sde_types.is_dynamic_type, never on meta group 15: the latter
    also covers 170 mutaplasmid types and a blueprint, and each of those
    would be a guaranteed 404 costing one unit of the 100-per-minute error
    budget. Items already recorded as 'missing' stay out unless the caller
    asks to retry them, for the same reason. Ordered by type then item so a
    progress readout walks through "Abyssal Warp Scramblers" as a block
    rather than jumping between types on every call.
    """
    clauses = ["t.is_dynamic_type = 1"]
    params: list = []
    if retry_missing:
        clauses.append("(i.item_id IS NULL OR i.status = ?)")
        params.append(STATUS_MISSING)
    else:
        clauses.append("i.item_id IS NULL")
    if item_ids is not None:
        if not item_ids:
            return []
        clauses.append("a.item_id IN (" + ",".join("?" * len(item_ids)) + ")")
        params.extend(int(i) for i in item_ids)
    sql = f"""
        SELECT DISTINCT a.item_id, a.type_id
        FROM assets a
        JOIN sde_types t ON t.type_id = a.type_id
        LEFT JOIN abyssal_items i ON i.item_id = a.item_id
        WHERE {" AND ".join(clauses)}
        ORDER BY a.type_id, a.item_id
    """
    return [(int(r[0]), int(r[1])) for r in conn.execute(sql, params)]


def store_rolls(conn: sqlite3.Connection, item_id: int, type_id: int, body: dict) -> None:
    """Record one ESI dynamic-item response: the header row plus every attribute.

    Idempotent by construction -- an upsert on the item row and a delete-
    then-insert of its attributes -- so a re-fetch (a 'missing' item that
    later resolves, or a retry after a crash mid-run) leaves exactly one
    copy. The upsert is written out rather than INSERT OR REPLACE because
    REPLACE deletes the parent row first, and with foreign keys on that
    cascades away the child rows this function is about to rewrite anyway,
    which is at best wasted work and at worst a surprise for the next
    person to reorder these statements.
    """
    attrs = [
        (item_id, int(a["attribute_id"]), float(a["value"]))
        for a in body.get("dogma_attributes") or []
    ]
    with db.transaction(conn):
        conn.execute(
            """INSERT INTO abyssal_items
                   (item_id, type_id, source_type_id, mutator_type_id, created_by,
                    status, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(item_id) DO UPDATE SET
                   type_id = excluded.type_id,
                   source_type_id = excluded.source_type_id,
                   mutator_type_id = excluded.mutator_type_id,
                   created_by = excluded.created_by,
                   status = excluded.status,
                   fetched_at = excluded.fetched_at""",
            (
                item_id, type_id, body.get("source_type_id"), body.get("mutator_type_id"),
                body.get("created_by"), STATUS_OK, _now(),
            ),
        )
        conn.execute("DELETE FROM abyssal_attributes WHERE item_id = ?", (item_id,))
        conn.executemany(
            "INSERT OR REPLACE INTO abyssal_attributes (item_id, attribute_id, value) "
            "VALUES (?, ?, ?)",
            attrs,
        )


def store_missing(conn: sqlite3.Connection, item_id: int, type_id: int) -> None:
    """Record a 404 so the item is not asked about again on every run."""
    with db.transaction(conn):
        conn.execute(
            """INSERT INTO abyssal_items
                   (item_id, type_id, source_type_id, mutator_type_id, created_by,
                    status, fetched_at)
               VALUES (?, ?, NULL, NULL, NULL, ?, ?)
               ON CONFLICT(item_id) DO UPDATE SET
                   type_id = excluded.type_id,
                   source_type_id = NULL,
                   mutator_type_id = NULL,
                   created_by = NULL,
                   status = excluded.status,
                   fetched_at = excluded.fetched_at""",
            (item_id, type_id, STATUS_MISSING, _now()),
        )
        conn.execute("DELETE FROM abyssal_attributes WHERE item_id = ?", (item_id,))


def fetch_rolls(
    conn: sqlite3.Connection,
    client,
    progress: Callable[[int, int, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    item_ids: list[int] | None = None,
    retry_missing: bool = False,
) -> dict:
    """Ask ESI for every pending abyssal item's rolls, one call each.

    Public route, so the request carries no character_id -- this is also
    what lets corp-owned abyssals be fetched through any character. The
    three outcomes are kept apart deliberately: a dict is stored; a 404
    (None from allow_404) is recorded as 'missing' so it is never re-asked
    without an explicit retry, because a repackaged or contracted-away item
    would otherwise burn one error-budget unit per run forever; an ESIError
    (5xx after the client's retries, a 4xx other than 404, a transport
    failure) stores nothing and is counted as failed, so the next run picks
    it up again. should_stop is polled between items only -- a half-written
    item never exists because each store is its own transaction. Returns
    fetched/missing/failed/remaining, the last being what a following run
    would still have to do.
    """
    todo = pending(conn, item_ids, retry_missing)
    total = len(todo)
    names = _type_names(conn, {type_id for _, type_id in todo})
    fetched = missing = failed = 0
    for done, (item_id, type_id) in enumerate(todo):
        if should_stop is not None and should_stop():
            break
        if progress is not None:
            label = names.get(type_id, f"type {type_id}")
            progress(done, total, f"Abyssal stats: {label} ({done + 1}/{total})")
        try:
            body = client.get(ITEM_ROUTE.format(type_id=type_id, item_id=item_id), allow_404=True)
        except ESIError:
            failed += 1
            continue
        if body is None:
            store_missing(conn, item_id, type_id)
            missing += 1
        else:
            store_rolls(conn, item_id, type_id, body)
            fetched += 1
    return {
        "fetched": fetched,
        "missing": missing,
        "failed": failed,
        "remaining": total - fetched - missing,
    }


def _type_names(conn: sqlite3.Connection, type_ids: set[int]) -> dict[int, str]:
    if not type_ids:
        return {}
    ids = sorted(type_ids)
    rows = conn.execute(
        f"SELECT type_id, name FROM sde_types WHERE type_id IN ({','.join('?' * len(ids))})",
        ids,
    )
    return {int(r[0]): r[1] for r in rows}
