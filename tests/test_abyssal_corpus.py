"""Whole-estate properties of the abyssal feature, over the anonymised corpus.

conftest's single BCS and the webifier pin each shape once; these tests run
the same code over 520 items of 36 types so a property that only breaks on
the odd unit, polarity or mutator combination has somewhere to break.
"""

from __future__ import annotations

import abyssal_corpus as corpus
import pytest
from conftest import FakeESIClient

from evasset import abyssal, db, queries
from evasset.omni import parse


@pytest.fixture()
def sde() -> dict:
    return corpus.load()["sde"]


@pytest.fixture()
def cconn(tmp_path):
    """The corpus's SDE subset and one asset row per item, rolls not yet stored."""
    c = db.init(tmp_path / "corpus.sqlite")
    corpus.install_sde(c)
    c.execute(
        "INSERT INTO characters (character_id,name,corporation_id,scopes,enabled) "
        "VALUES (100,'Test Pilot',2000,'s',1)"
    )
    corpus.install_assets(c)
    return c


@pytest.fixture()
def stored(cconn):
    corpus.store_all(cconn)
    return cconn


def _ranges(sde) -> dict[int, dict[int, dict]]:
    out: dict[int, dict[int, dict]] = {}
    for r in sde["mutator_ranges"]:
        out.setdefault(r["mutator_type_id"], {})[r["attribute_id"]] = r
    return out


def _python_quality(item: dict, attribute_id: int, sde) -> float | None:
    """abyssal.quality from the corpus's own rows, the way the inspector's
    SQL is supposed to arrive at it."""
    rg = _ranges(sde)[item["mutator_type_id"]][attribute_id]
    attr = next(a for a in sde["dogma_attributes"] if a["attribute_id"] == attribute_id)
    base = next(
        (r["value"] for r in sde["type_dogma"]
         if r["type_id"] == item["source_type_id"] and r["attribute_id"] == attribute_id),
        None,
    )
    value = dict(item["attributes"]).get(attribute_id)
    high = abyssal.resolve_polarity(attribute_id, attr["high_is_good"], rg["high_is_good"])
    return abyssal.quality(
        abyssal.roll_position(value, base, rg["min_mult"], rg["max_mult"]), high
    )


def test_the_corpus_is_synthetic_in_every_id_and_complete_in_every_reference(sde):
    """The file is the one place a real item or roller id could leak into
    the repository, so the bands are asserted rather than trusted; and a
    mutator without ranges or a source without dogma would make the roll
    tests below pass vacuously on rolls they never ranked."""
    items = corpus.items()
    assert len(items) == 520
    assert len({it["type_id"] for it in items}) == 36
    ids = [it["item_id"] for it in items]
    assert ids == list(range(corpus.FIRST_ITEM_ID, corpus.FIRST_ITEM_ID + len(items)))
    rollers = {it["created_by"] for it in items}
    assert rollers <= set(range(corpus.FIRST_ROLLER_ID, corpus.FIRST_ROLLER_ID + 100))
    assert len(rollers) > 1
    ranged = {r["mutator_type_id"] for r in sde["mutator_ranges"]}
    based = {r["type_id"] for r in sde["type_dogma"]}
    known = {a["attribute_id"] for a in sde["dogma_attributes"]}
    for it in items:
        assert it["mutator_type_id"] in ranged
        assert it["source_type_id"] in based
        assert {a for a, _ in it["attributes"]} <= known
        assert it["attributes"], it["item_id"]


def test_every_rolled_value_lies_inside_its_mutaplasmid_range(sde):
    """roll_position clamps, so a value outside base*min..base*max would be
    silently reported as a 0% or 100% roll; over real rolls that never
    happens, which is what makes the clamp a safety net and not a lie."""
    base = {(r["type_id"], r["attribute_id"]): r["value"] for r in sde["type_dogma"]}
    ranges = _ranges(sde)
    checked = 0
    for it in corpus.items():
        values = dict(it["attributes"])
        for attribute_id, rg in ranges[it["mutator_type_id"]].items():
            b = base.get((it["source_type_id"], attribute_id))
            if b is None or b == 0 or attribute_id not in values:
                continue
            lo, hi = sorted((b * rg["min_mult"], b * rg["max_mult"]))
            v = values[attribute_id]
            assert lo - 1e-6 * abs(lo) <= v <= hi + 1e-6 * abs(hi), (it["item_id"], attribute_id)
            checked += 1
    assert checked > 2000


def test_the_fetch_path_stores_the_whole_corpus_and_leaves_nothing_pending(cconn):
    """One call per item and every attribute row kept, so the estate's
    counts below rest on the real write path rather than on a shortcut."""
    assert len(abyssal.pending(cconn)) == 520
    client = FakeESIClient(bodies=corpus.bodies())
    result = abyssal.fetch_rolls(cconn, client)
    assert result["fetched"] == 520
    assert result["missing"] == result["failed"] == result["remaining"] == 0
    assert len(client.calls) == 520
    assert abyssal.pending(cconn) == []
    stored = cconn.execute("SELECT COUNT(*) FROM abyssal_attributes").fetchone()[0]
    assert stored == sum(len(it["attributes"]) for it in corpus.items())


def test_sql_quality_agrees_with_the_python_form_on_every_roll(stored, sde):
    """roll_quality_sql is pinned to abyssal.quality on three hand-picked
    cases in test_abyssal; here it is pinned on every unit, polarity and
    mutator override the corpus contains, in both the inspector's per-item
    query and the table's batched one."""
    _, cells = queries.abyssal_roll_data(stored, [it["item_id"] for it in corpus.items()])
    compared = 0
    for it in corpus.items():
        rolls = queries.fetch_abyssal_rolls(stored, it["item_id"])
        assert rolls["status"] == abyssal.STATUS_OK
        assert rolls["rolls"], it["item_id"]
        for roll in rolls["rolls"]:
            expected = _python_quality(it, roll["attribute_id"], sde)
            assert roll["quality"] == pytest.approx(expected, abs=1e-9)
            _, cell_quality = cells[it["item_id"]][roll["attribute_id"]]
            assert cell_quality == pytest.approx(expected, abs=1e-9)
            compared += 1
    assert compared > 2000


def test_type_counts_bounds_and_grammar_agree_with_the_corpus(stored, sde):
    """The card's type list, its estate-scoped slider bounds and the typed
    grammar all read the same tables; over an estate of real size they
    must agree with a plain walk of the corpus."""
    rows = queries.abyssal_type_counts(stored)
    by_name = {r["name"]: r for r in rows}
    assert len(rows) == 36
    assert sum(r["items"] for r in rows) == sum(r["fetched"] for r in rows) == 520

    def count(text: str) -> int:
        where, params = parse(text).where()
        return queries.count_assets(stored, where, params)

    assert count("is:abyssal") == 520
    assert count("abyssal") == 520
    assert count("-is:abyssal") == 0
    busiest = rows[0]["name"]
    assert count(f'abyssal:"{busiest}"') == by_name[busiest]["items"]

    bounds = queries.abyssal_attribute_bounds(stored, busiest)
    assert bounds
    type_id = by_name[busiest]["type_id"]
    for attribute_id, (lo, hi) in bounds.items():
        assert lo <= hi
        # cpu (attribute 50) displays as stored, so the bounds are the raw extremes.
        if attribute_id == 50:
            values = [
                dict(it["attributes"])[50] for it in corpus.items() if it["type_id"] == type_id
            ]
            assert (lo, hi) == pytest.approx((min(values), max(values)))

    expected = sum(
        1 for it in corpus.items()
        if 50 in _ranges(sde)[it["mutator_type_id"]]
        and (q := _python_quality(it, 50, sde)) is not None and q * 100 >= 50
    )
    assert expected > 0
    assert count("roll:cpu>=50") == expected
