"""A corpus of anonymised abyssal rolls, shared by the tests and the demo seed.

tests/data/abyssal_corpus.json holds 520 fetched abyssal modules across 36
module types, each as ESI's dynamic-item route describes it (source type,
mutaplasmid, roller, the full attribute list), together with the public SDE
rows they reference: the types, groups and categories, the dogma attributes
and units, the source types' base dogma and the mutaplasmids' roll ranges.
The single hand-built webifier and BCS in conftest cover the shapes one at a
time; this corpus is for the properties that only hold, or only fail, over a
whole estate -- the SQL quality form against the Python form on every roll,
the card's estate-scoped bounds, the type facet counts, the fetch path over
hundreds of items.

Every item id and every roller id is synthetic, assigned after the items
were shuffled, so the file carries no ordering or identity from anywhere;
the type, source and mutator ids and the SDE rows are CCP's constants.
Values are rounded to nine significant figures, far inside anything a
quality computation can notice.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from evasset import abyssal

CORPUS_PATH = Path(__file__).with_name("data") / "abyssal_corpus.json"

# The synthetic bands, so a test can prove nothing real slipped in.
FIRST_ITEM_ID = 1_000_000_000_101
FIRST_ROLLER_ID = 91_000_101

JITA_4_4, JITA_SYS, THE_FORGE = 60003760, 30000142, 10000002

Placement = tuple[str, int, int, str, int, int]
"""(owner_type, owner_id, station_id, location_flag, system_id, region_id)."""


@lru_cache(maxsize=1)
def load() -> dict:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


def items() -> list[dict]:
    return load()["items"]


def bodies() -> dict[int, dict]:
    """Each item as the ESI dynamic-item body abyssal.store_rolls consumes,
    keyed by item id -- also exactly what conftest.FakeESIClient serves."""
    return {
        it["item_id"]: {
            "created_by": it["created_by"],
            "dogma_attributes": [
                {"attribute_id": a, "value": v} for a, v in it["attributes"]
            ],
            "dogma_effects": [],
            "mutator_type_id": it["mutator_type_id"],
            "source_type_id": it["source_type_id"],
        }
        for it in items()
    }


_SDE_TABLES = {
    "categories": "sde_categories",
    "groups": "sde_groups",
    "meta_groups": "sde_meta_groups",
    "types": "sde_types",
    "dogma_attributes": "sde_dogma_attributes",
    "dogma_units": "sde_dogma_units",
    "type_dogma": "sde_type_dogma",
    "mutator_ranges": "sde_mutator_ranges",
}


def install_sde(conn) -> None:
    """Insert the corpus's SDE subset, leaving any row a test seeded first alone."""
    for key, table in _SDE_TABLES.items():
        rows = load()["sde"][key]
        if not rows:
            continue
        cols = list(rows[0])
        conn.executemany(
            f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})",
            [tuple(r[c] for c in cols) for r in rows],
        )
    conn.commit()


def _jita_hangar(n: int, item: dict) -> Placement:
    return ("character", 100, JITA_4_4, "Hangar", JITA_SYS, THE_FORGE)


def install_assets(conn, place: Callable[[int, dict], Placement] = _jita_hangar) -> None:
    """One singleton asset row per item, placed by the callback (index, item).

    The default parks everything in character 100's Jita 4-4 hangar, the
    owner the abyssal tests already seed; the demo seed spreads them over
    its own pilots and stations."""
    rows = []
    for n, it in enumerate(items()):
        owner_type, owner_id, station, flag, system, region = place(n, it)
        rows.append((
            owner_type, owner_id, it["item_id"], it["type_id"], 1, station, flag, "station",
            1, station, system, region,
        ))
    conn.executemany(
        "INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,"
        "location_flag,location_type,is_singleton,root_location_id,system_id,region_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()


def store_all(conn) -> None:
    """Store every item's rolls through the real write path."""
    for it in items():
        abyssal.store_rolls(conn, it["item_id"], it["type_id"], bodies()[it["item_id"]])
