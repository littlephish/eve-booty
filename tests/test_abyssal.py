"""Abyssal roll arithmetic, the read-side queries, the fetch, and the SDE importers.

Everything Qt-free. The seeded estate mixes the shapes that matter: a fetched
webifier with a sign-inverted attribute, the research's live Ballistic
Control System sample, an unfetched item, an item ESI 404'd, one whose
mutator the SDE has never heard of, one whose source has no dogma rows, and
-- the trap -- a stack of mutaplasmids sharing meta group 15 with the real
abyssals but carrying is_dynamic_type 0.
"""

from __future__ import annotations

import json
import zipfile

import pytest
from conftest import BCS_BODY, BCS_MUTATOR, BCS_SOURCE, BCS_TYPE, FakeESIClient

from evasset import abyssal, db, omni, queries, sde

# The webifier is synthetic and its source and mutator ids are borrowed: in
# the SDE 526 is "Stasis Webifier I" and 47737 a 5MN microwarpdrive
# mutaplasmid. The BCS ids and its body (conftest) are the real ones.
WEB_TYPE, WEB_SOURCE, WEB_MUTATOR = 47702, 526, 47737
MWD_TYPE = 47408
UNKNOWN_MUTATOR, UNKNOWN_SOURCE = 99999, 99998

@pytest.fixture()
def conn(tmp_path):
    c = db.init(tmp_path / "abyssal.sqlite")
    c.executescript(f"""
      INSERT INTO sde_categories VALUES (7,'Module',1),(6,'Ship',1);
      INSERT INTO sde_groups VALUES (65,7,'Stasis Web',1),(367,7,'Ballistic Control system',1),
        (46,7,'Propulsion Module',1),(1964,7,'Mutaplasmids',1),(27,6,'Battleship',1);
      INSERT INTO sde_meta_groups VALUES (2,'Tech II'),(4,'Faction'),(15,'Abyssal');
      INSERT INTO sde_types (type_id,name,group_id,meta_group_id,portion_size,published,
                             is_dynamic_type) VALUES
        ({WEB_TYPE},'Abyssal Stasis Webifier',65,15,1,1,1),
        ({WEB_SOURCE},'Stasis Webifier II',65,2,1,1,0),
        ({WEB_MUTATOR},'Gravid Stasis Webifier Mutaplasmid',1964,15,1,1,0),
        ({BCS_TYPE},'Abyssal Ballistic Control System',367,15,1,1,1),
        ({BCS_SOURCE},'Domination Ballistic Control System',367,4,1,1,0),
        ({BCS_MUTATOR},'Gravid Ballistic Control System Mutaplasmid',1964,15,1,1,0),
        ({MWD_TYPE},'50MN Abyssal Microwarpdrive',46,15,1,1,1),
        (645,'Dominix',27,NULL,1,1,0);
      INSERT INTO sde_dogma_attributes
        (attribute_id,name,display_name,unit_id,high_is_good,default_value,published) VALUES
        (20,'speedFactor','Maximum Velocity Bonus',124,1,0,1),
        (50,'cpu','CPU usage',106,0,0,1),
        (30,'power','Powergrid Usage',107,0,0,1),
        (54,'maxRange','Optimal Range',1,1,0,1),
        (73,'duration','Activation time / duration',101,0,0,1),
        (204,'speedMultiplier','Rate of Fire Bonus',111,0,1,1),
        (213,'missileDamageMultiplierBonus','Missile Damage Bonus',109,1,1,1),
        (1255,'droneDamageBonus','Drone Damage Bonus',105,1,0,1),
        (182,'requiredSkill1','Primary Skill required',NULL,NULL,0,1);
      INSERT INTO sde_dogma_units VALUES (124,'Modifier Relative Percent','%'),
        (106,'Teraflops','tf'),(107,'MegaWatts','MW'),(1,'Length','m'),
        (101,'Milliseconds','s'),(105,'Percentage','%'),(109,'Modifier Percent','%'),
        (111,'Inversed Modifier Percent','%');
      INSERT INTO sde_type_dogma VALUES
        ({WEB_SOURCE},20,-60),({WEB_SOURCE},50,30),({WEB_SOURCE},54,10000),
        ({WEB_SOURCE},73,5000),
        ({BCS_SOURCE},50,24),({BCS_SOURCE},204,0.9),({BCS_SOURCE},213,1.1),
        ({BCS_SOURCE},182,3318);
      -- The webifier mutaplasmid carries CCP's polarity override on speedFactor.
      INSERT INTO sde_mutator_ranges VALUES
        ({WEB_MUTATOR},20,0.9,1.1,0,{WEB_TYPE}),
        ({WEB_MUTATOR},50,0.8,1.5,NULL,{WEB_TYPE}),
        ({WEB_MUTATOR},54,0.8,1.2,NULL,{WEB_TYPE}),
        ({BCS_MUTATOR},50,0.8,1.5,NULL,{BCS_TYPE}),
        ({BCS_MUTATOR},204,0.94,1.06,NULL,{BCS_TYPE}),
        ({BCS_MUTATOR},213,0.95,1.05,NULL,{BCS_TYPE}),
        ({BCS_MUTATOR},1255,0.9,1.1,NULL,{BCS_TYPE});
      INSERT INTO characters (character_id,name,corporation_id,scopes,enabled) VALUES
        (100,'Test Pilot',2000,'s',1);
      INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
                          location_flag,location_type,is_singleton) VALUES
        ('character',100,1001,{WEB_TYPE},1,60003760,'Hangar','station',1),
        ('character',100,1002,{BCS_TYPE},1,60003760,'Hangar','station',1),
        ('character',100,1003,{MWD_TYPE},1,60003760,'Hangar','station',1),
        ('character',100,1004,{WEB_MUTATOR},5,60003760,'Hangar','station',0),
        ('character',100,1005,{WEB_TYPE},1,60003760,'Hangar','station',1),
        ('character',100,1006,{WEB_TYPE},1,60003760,'Hangar','station',1),
        ('character',100,1007,{WEB_TYPE},1,60003760,'Hangar','station',1),
        ('character',100,1008,645,1,60003760,'Hangar','station',1);
      INSERT INTO abyssal_items VALUES
        (1001,{WEB_TYPE},{WEB_SOURCE},{WEB_MUTATOR},42,'ok','2026-09-01T00:00:00+00:00'),
        (1005,{WEB_TYPE},NULL,NULL,NULL,'missing','2026-09-01T00:00:00+00:00'),
        (1006,{WEB_TYPE},{WEB_SOURCE},{UNKNOWN_MUTATOR},42,'ok','2026-09-01T00:00:00+00:00'),
        (1007,{WEB_TYPE},{UNKNOWN_SOURCE},{WEB_MUTATOR},42,'ok','2026-09-01T00:00:00+00:00');
      INSERT INTO abyssal_attributes VALUES
        (1001,20,-63),(1001,50,27),(1001,54,11000),(1001,73,5000),(1001,182,3392),
        (1006,20,-58),(1006,50,30),(1006,54,12000),(1006,73,5000),
        (1007,20,-63),(1007,50,27),(1007,54,11000);
    """)
    abyssal.store_rolls(c, 1002, BCS_TYPE, BCS_BODY)
    return c


# -------------------------------------------------------------- unit table
def _display(conn, value, unit_id):
    return conn.execute(
        f"SELECT {queries.display_value_sql(':v', ':u')}", {"v": value, "u": unit_id}
    ).fetchone()[0]


def test_display_value_applies_every_dogma_unit_rule(conn):
    """The CASE is the single source of truth for both the inspector and the
    stat: filter, so every rule from dogmaUnits (research section 5) is
    pinned here, including the ones that must pass through untouched: the
    already-percent units 124 and 205, a plain unit, and a NULL unit."""
    cases = [
        (5000, 101, 5.0),          # milliseconds shown as seconds
        (0.67, 108, 33.0),         # inverse absolute percent: 0.0 = 100%
        (0.1, 111, 90.0),          # inversed modifier percent: 0.1 = 90%
        (1.1077, 109, 10.77),      # modifier percent: 1.1 = +10%
        (0.9, 109, -10.0),         # ... and the inverse side is negative
        (0.25, 127, 25.0),         # absolute percent: 1.0 = 100%
        (-60, 124, -60.0),         # modifier relative percent: shown as stored
        (3.6, 205, 3.6),           # modifier realPercent: shown as stored
        (24000, 1, 24000.0),       # metres: raw
        (27, None, 27.0),          # no unit at all: raw
    ]
    seen = 0
    for value, unit, expected in cases:
        assert _display(conn, value, unit) == pytest.approx(expected), (value, unit)
        seen += 1
    assert seen == len(cases) == 10


def test_format_value_rounds_by_magnitude_and_signs_modifier_units():
    cases = [
        (1.1077, 104, "x", "1.11x"),
        (25.8, 106, "tf", "26 tf"),
        (3318.0, 113, "HP", "3,318 HP"),
        (5.0, 101, "s", "5.00 s"),
        (-60.0, 124, "%", "-60%"),
        (10.77, 109, "%", "+11%"),     # at or above ten the decimals go
        (9.99, 109, "%", "+9.99%"),
        (3.6, 205, "%", "+3.60%"),
        (33.0, 108, "%", "33%"),      # a resistance is not a modifier: no sign
        (27.0, None, None, "27"),
        (-0.3, 124, "%", "-0.30%"),   # a real negative keeps its sign ...
        (-0.001, 124, "%", "+0.00%"),  # ... but a value that rounds to zero is not "-0"
    ]
    seen = 0
    for value, unit_id, symbol, expected in cases:
        assert abyssal.format_value(value, unit_id, symbol) == expected, (value, unit_id)
        seen += 1
    assert seen == len(cases) == 12


# ------------------------------------------------------- polarity and range
def test_polarity_precedence_is_override_then_mutator_then_attribute(monkeypatch):
    assert abyssal.resolve_polarity(20, 1, None) is True
    assert abyssal.resolve_polarity(20, 1, 0) is False, "the mutator's word beats the attribute"
    assert abyssal.resolve_polarity(20, 0, 1) is True
    assert abyssal.resolve_polarity(20, None, None) is True, "no flag anywhere reads high-is-good"
    monkeypatch.setitem(abyssal.POLARITY_OVERRIDES, 20, True)
    assert abyssal.resolve_polarity(20, 0, 0) is True, "a live-proven override outranks the SDE"


def test_polarity_overrides_ship_empty():
    """The escape hatch exists for a case a live check proves wrong; nothing
    goes in on suspicion, so an entry appearing here needs its evidence."""
    assert abyssal.POLARITY_OVERRIDES == {}


def test_webifier_speed_factor_is_ranked_with_a_negative_base():
    """Hand-computed. A Stasis Webifier II has speedFactor -60; the Gravid
    mutaplasmid rolls 0.9x..1.1x, so the range is -66..-54 -- ordered by
    value, not by multiplier, or lo > hi and the division flips sign. The
    attribute's own highIsGood is true (an afterburner's speedFactor is a
    positive bonus) and CCP's per-mutaplasmid override says false for the
    web, so -63 is 25% along the raw range and a 75% roll."""
    position = abyssal.roll_position(-63, -60, 0.9, 1.1)
    assert position == pytest.approx(0.25)
    high = abyssal.resolve_polarity(20, attr_high_is_good=1, mutator_high_is_good=0)
    assert high is False
    assert abyssal.quality(position, high) == pytest.approx(0.75)
    assert abyssal.verdict(-63, -60, high) is True
    assert abyssal.verdict(-57, -60, high) is False
    # Without the override the same numbers would be read backwards.
    assert abyssal.quality(position, True) == pytest.approx(0.25)


def test_microwarpdrive_signature_bonus_is_ranked_low_is_good():
    """Hand-computed against the live SDE (2026-09-02): the mutaplasmids that
    roll signatureRadiusBonus (554) are the microwarpdrive ones, the
    attribute's own highIsGood is false and no mutator overrides it -- a
    bigger signature bloom is a worse prop mod. Unit 124 shows as stored,
    so a +30 base under a 0.7x..1.3x mutaplasmid (mutator 47297's range for
    the 50MN Abyssal Microwarpdrive) spans 21..39, and a 36 roll sits
    (36-21)/18 = 83.3% along the raw range: a 16.7% quality, worse than
    the base."""
    position = abyssal.roll_position(36, 30, 0.7, 1.3)
    assert position == pytest.approx(15 / 18)
    high = abyssal.resolve_polarity(554, attr_high_is_good=0, mutator_high_is_good=None)
    assert high is False
    assert abyssal.quality(position, high) == pytest.approx(3 / 18)
    assert abyssal.verdict(36, 30, high) is False
    assert abyssal.verdict(24, 30, high) is True


def test_resonance_quality_is_computed_on_raw_values_not_displayed_ones():
    """A hull EM resonance of 0.67 displays as 33% resistance (unit 108),
    and the display flips orientation: lower raw is higher shown. Ranking
    on raw values with the attribute's low-is-good flag gives the right
    answer once; ranking on displayed values and then inverting again
    would give 1 minus it."""
    position = abyssal.roll_position(0.63, 0.67, 0.9, 1.1)
    assert position == pytest.approx((0.63 - 0.603) / (0.737 - 0.603))
    assert abyssal.quality(position, False) == pytest.approx(1 - (0.63 - 0.603) / 0.134)
    assert abyssal.verdict(0.63, 0.67, False) is True


def test_unknown_or_degenerate_range_is_unranked_and_clamped_otherwise():
    assert abyssal.roll_position(5, None, 0.9, 1.1) is None, "no base"
    assert abyssal.roll_position(5, 10, None, 1.1) is None, "no range"
    assert abyssal.roll_position(5, 0, 0.9, 1.1) is None, "zero base collapses the range"
    assert abyssal.roll_position(5, 10, 1.0, 1.0) is None, "equal multipliers"
    assert abyssal.roll_position(20, 10, 0.9, 1.1) == 1.0, "outside the range clamps"
    assert abyssal.roll_position(1, 10, 0.9, 1.1) == 0.0
    assert abyssal.quality(None, True) is None
    assert abyssal.verdict(10, 10, True) is None, "equal to base is neither better nor worse"
    assert abyssal.verdict(10, None, True) is None


# ------------------------------------------------------------ read queries
def test_fetch_abyssal_rolls_reports_fetch_state_for_every_item(conn):
    assert queries.fetch_abyssal_rolls(conn, 1003)["status"] == "unfetched"
    assert queries.fetch_abyssal_rolls(conn, 1008)["status"] == "unfetched"
    missing = queries.fetch_abyssal_rolls(conn, 1005)
    assert missing["status"] == "missing"
    assert missing["rolls"] == [] and missing["source"] is None
    ok = queries.fetch_abyssal_rolls(conn, 1001)
    assert (ok["status"], ok["source"], ok["mutator"], ok["created_by"]) == (
        "ok", "Stasis Webifier II", "Gravid Stasis Webifier Mutaplasmid", 42,
    )


def test_fetch_abyssal_rolls_lists_the_mutators_attributes_in_display_units(conn):
    """Rolled attributes only: duration and the skill requirement are stored
    but not in the mutaplasmid's set, so they stay out. Values are what the
    inspector shows -- the web's raw -63 speedFactor is a signed percent."""
    rolls = queries.fetch_abyssal_rolls(conn, 1001)["rolls"]
    assert [r["label"] for r in rolls] == ["CPU usage", "Maximum Velocity Bonus", "Optimal Range"]
    by_name = {r["name"]: r for r in rolls}
    web = by_name["speedFactor"]
    assert (web["value"], web["base"], web["unit"], web["unit_id"]) == (-63.0, -60.0, "%", 124)
    assert web["position"] == pytest.approx(0.25)
    assert web["quality"] == pytest.approx(0.75)
    assert web["high_is_good"] is False
    assert web["better"] is True
    cpu = by_name["cpu"]
    assert (cpu["value"], cpu["base"], cpu["unit"]) == (27.0, 30.0, "tf")
    assert cpu["position"] == pytest.approx((27 - 24) / (45 - 24))
    assert cpu["quality"] == pytest.approx(1 - (27 - 24) / 21)
    assert cpu["better"] is True
    assert (cpu["min"], cpu["max"]) == (24.0, 45.0)
    # A negative base flips the multiplier order: 0.9x..1.1x of -60 is
    # -54..-66 raw, and the range must still come out low to high.
    assert (web["min"], web["max"]) == (-66.0, -54.0)
    rng = by_name["maxRange"]
    assert (rng["value"], rng["base"], rng["better"]) == (11000.0, 10000.0, True)
    assert set(rolls[0]) >= {
        "attribute_id", "label", "unit", "value", "base", "min", "max", "position", "quality",
        "high_is_good", "better",
    }


def test_fetch_abyssal_rolls_converts_the_live_bcs_sample_as_hand_computed(conn):
    """The research's real item: cpu 25.8 tf against a 24 tf base rolled
    0.8x..1.5x; the RoF multiplier 0.8829 is unit 111 (inversed modifier
    percent, shown as (1 - v) * 100) so it reads 11.71% against a 10% base;
    the missile bonus multiplier 1.1077 shown as +10.77% (unit 109).
    droneDamageBonus is in the mutaplasmid's range set but absent from the
    body (zero on the source), so it does not appear -- exactly as the
    research observed."""
    rolls = {r["name"]: r for r in queries.fetch_abyssal_rolls(conn, 1002)["rolls"]}
    assert set(rolls) == {"cpu", "speedMultiplier", "missileDamageMultiplierBonus"}
    assert rolls["cpu"]["value"] == pytest.approx(25.8)
    assert rolls["cpu"]["position"] == pytest.approx((25.8 - 19.2) / (36 - 19.2))
    assert rolls["cpu"]["quality"] == pytest.approx(1 - (25.8 - 19.2) / 16.8)
    assert (rolls["cpu"]["min"], rolls["cpu"]["max"]) == (pytest.approx(19.2), pytest.approx(36.0))
    rof = rolls["speedMultiplier"]
    assert rof["value"] == pytest.approx(11.71, abs=0.01)
    assert rof["base"] == pytest.approx(10.0)
    assert rof["better"] is True, "a lower RoF multiplier fires faster"
    # The range ends are display values ordered as displayed: the raw
    # 0.846..0.954 (0.9 * 0.94..1.06) inverts under unit 111 to 15.4..4.6,
    # so min must be 4.6 and max 15.4, not the raw order carried across.
    assert (rof["min"], rof["max"]) == (pytest.approx(4.6), pytest.approx(15.4))
    assert rof["min"] < rof["value"] < rof["max"]
    missile = rolls["missileDamageMultiplierBonus"]
    assert missile["value"] == pytest.approx(10.77, abs=0.01)
    assert missile["base"] == pytest.approx(10.0)
    assert missile["position"] == pytest.approx((1.1077 - 1.045) / (1.155 - 1.045), abs=1e-3)
    assert missile["better"] is True


def test_stat_filter_finds_the_live_bcs_sample_by_its_displayed_numbers(conn):
    """The grammar and the inspector share one unit CASE, so the +10.77%
    missile bonus (stored 1.1077, unit 109) and the 11.71% rate-of-fire
    bonus (stored 0.8829, unit 111) are matched by the numbers on screen,
    via an alias, a display name and an internal name alike."""
    from evasset import omni

    def ids(text):
        where, params = omni.parse(text).where()
        return {r["item_id"] for r in queries.fetch_assets(conn, where, params)}

    assert ids('stat:"Missile Damage Bonus">10') == {1002}
    assert ids('stat:"Missile Damage Bonus">11') == set()
    assert ids("stat:rof>11") == {1002}
    assert ids("stat:rof<0.9") == set(), "the stored multiplier is not the number on screen"
    assert ids("stat:speedMultiplier>12") == set()
    assert ids("stat:cpu<26") == {1002}
    assert ids("stat:cpu<26 stat:cpu>25") == {1002}
    assert ids("is:abyssal") == {1001, 1002, 1003, 1005, 1006, 1007}
    assert ids("is:abyssal -stat:cpu<26") == {1001, 1003, 1005, 1006, 1007}


def test_unknown_mutator_falls_back_to_attributes_that_differ_from_base(conn):
    """The SDE cannot rank a mutaplasmid it has never heard of, but the
    item's stats are still real: show whatever moved off the source's base,
    unranked, and keep the unchanged duration and cpu out."""
    rolls = queries.fetch_abyssal_rolls(conn, 1006)["rolls"]
    assert [r["name"] for r in rolls] == ["speedFactor", "maxRange"]
    assert all(r["position"] is None and r["quality"] is None for r in rolls)
    assert all(r["min"] is None and r["max"] is None for r in rolls), "no range to show"
    # With no mutator row there is no polarity override either, so the
    # attribute's own high-is-good rules: -58 reads as "better" than -60
    # here, honestly wrong for a web and exactly what the SDE knows.
    assert rolls[0]["better"] is True
    assert rolls[1]["better"] is True


def test_missing_source_dogma_falls_back_to_the_attribute_default(conn):
    """A source type with no rows in sde_type_dogma (older SDE, or a type
    outside the import restriction) must not crash the join or invent a
    base: the attribute's SDE default stands in, and with a zero default
    the range collapses to unranked."""
    rolls = {r["name"]: r for r in queries.fetch_abyssal_rolls(conn, 1007)["rolls"]}
    assert set(rolls) == {"speedFactor", "cpu", "maxRange"}
    assert rolls["cpu"]["base"] == 0.0
    assert rolls["cpu"]["position"] is None
    assert (rolls["cpu"]["min"], rolls["cpu"]["max"]) == (None, None)
    assert rolls["cpu"]["value"] == 27.0


def test_abyssal_summaries_give_one_line_per_fetched_item_only(conn):
    """Three fixed lines are told apart: a 404, a mutaplasmid the SDE has
    never heard of (1006), and a known mutaplasmid whose source type has no
    dogma rows so nothing ranks (1007). The last used to read "mutator
    unknown to the SDE", which was false -- the mutator was known, the
    seeding just assumed unranked meant unknown."""
    got, _cells = queries.abyssal_roll_data(conn, [1001, 1002, 1003, 1005, 1006, 1007, 1008])
    assert 1003 not in got and 1008 not in got, "unfetched items have no key"
    # cpu 27 in 24..45 low-is-good -> 86%; web -63 in -66..-54 overridden
    # low-is-good -> 75%; range 11000 in 8000..12000 high-is-good -> 75%.
    assert got[1001] == "CPU 86% · Speed 75% · Range 75%"
    assert got[1005] == queries.ABYSSAL_SUMMARY_MISSING
    assert got[1006] == queries.ABYSSAL_SUMMARY_UNRANKED
    assert got[1007] == queries.ABYSSAL_SUMMARY_UNRANKABLE
    assert queries.ABYSSAL_SUMMARY_UNRANKABLE != queries.ABYSSAL_SUMMARY_UNRANKED
    assert "unknown" not in queries.ABYSSAL_SUMMARY_UNRANKABLE
    assert got[1002].startswith("CPU 61%")
    assert "RoF" in got[1002] and "Missile dmg" in got[1002]
    assert queries.abyssal_roll_data(conn, []) == ({}, {})


def test_asset_rows_expose_is_dynamic_type(conn):
    flags = {r["item_id"]: r["is_dynamic_type"] for r in queries.fetch_assets(conn)}
    assert flags[1001] == 1 and flags[1003] == 1
    assert flags[1004] == 0, "a mutaplasmid stack is meta group 15 but not a dynamic type"
    assert flags[1008] == 0


# ------------------------------------------------------------------- fetch
def test_pending_is_gated_on_is_dynamic_type_and_skips_stored_items(conn):
    """The mutaplasmid stack (item 1004) shares meta group 15 with the real
    abyssals; asking ESI about it would be a guaranteed 404 and one unit of
    error budget per sync. Only the unfetched abyssal is pending."""
    assert abyssal.pending(conn) == [(1003, MWD_TYPE)]
    assert abyssal.pending(conn, retry_missing=True) == [(1003, MWD_TYPE), (1005, WEB_TYPE)]
    assert abyssal.pending(conn, item_ids=[1005]) == []
    assert abyssal.pending(conn, item_ids=[1005], retry_missing=True) == [(1005, WEB_TYPE)]
    assert abyssal.pending(conn, item_ids=[1004, 1008]) == [], "never a non-dynamic type"
    assert abyssal.pending(conn, item_ids=[]) == []


def test_pending_orders_by_type_then_item(conn):
    conn.execute("DELETE FROM abyssal_items")
    got = abyssal.pending(conn)
    assert got == sorted(got, key=lambda p: (p[1], p[0]))
    assert len(got) == 6, "every dynamic-type asset, and nothing else"
    assert {i for i, _ in got} == {1001, 1002, 1003, 1005, 1006, 1007}
    assert got[0] == (1003, MWD_TYPE), "type 47408 sorts before the 477xx webs"


def test_fetch_requests_only_dynamic_types_on_the_public_route(conn):
    conn.execute("DELETE FROM abyssal_items")
    client = FakeESIClient(bodies={1002: BCS_BODY})
    result = abyssal.fetch_rolls(conn, client)
    assert 1004 not in client.requested_items, "the mutaplasmid stack must never be asked about"
    assert 1008 not in client.requested_items
    assert set(client.requested_items) == {1001, 1002, 1003, 1005, 1006, 1007}
    seen = 0
    for path, kw in zip(client.calls, client.kwargs, strict=True):
        assert path.startswith("/dogma/dynamic/items/")
        assert kw.get("allow_404") is True
        assert kw.get("character_id") is None, "public route: no token, so corp items work too"
        seen += 1
    assert seen == 6
    # The path pairs the asset's own (abyssal) type id with the item id.
    assert f"/dogma/dynamic/items/{BCS_TYPE}/1002" in client.calls
    assert result == {"fetched": 1, "missing": 5, "failed": 0, "remaining": 0}


def test_a_404_is_recorded_as_missing_and_never_asked_again(conn):
    client = FakeESIClient()
    first = abyssal.fetch_rolls(conn, client)
    assert first == {"fetched": 0, "missing": 1, "failed": 0, "remaining": 0}
    assert client.requested_items == [1003]
    row = conn.execute("SELECT status FROM abyssal_items WHERE item_id=1003").fetchone()
    assert row["status"] == "missing"
    second = abyssal.fetch_rolls(conn, client)
    assert client.requested_items == [1003], "a second run must not spend error budget on it"
    assert second == {"fetched": 0, "missing": 0, "failed": 0, "remaining": 0}
    # An explicit retry asks once more, and a body this time upgrades the row.
    client.bodies[1003] = {**BCS_BODY, "source_type_id": 5975, "mutator_type_id": 47297}
    third = abyssal.fetch_rolls(conn, client, item_ids=[1003], retry_missing=True)
    assert third["fetched"] == 1
    row = conn.execute("SELECT status, source_type_id FROM abyssal_items WHERE item_id=1003").fetchone()
    assert (row["status"], row["source_type_id"]) == ("ok", 5975)


def test_an_esi_error_leaves_the_item_pending_and_stores_nothing(conn):
    client = FakeESIClient(errors={1003})
    result = abyssal.fetch_rolls(conn, client)
    assert result == {"fetched": 0, "missing": 0, "failed": 1, "remaining": 1}
    assert conn.execute("SELECT COUNT(*) FROM abyssal_items WHERE item_id=1003").fetchone()[0] == 0
    assert abyssal.pending(conn) == [(1003, MWD_TYPE)], "the next run picks it up again"


def test_cancel_is_honoured_between_items(conn):
    conn.execute("DELETE FROM abyssal_items")
    client = FakeESIClient(bodies={1002: BCS_BODY})
    calls = 0

    def stop():
        nonlocal calls
        calls += 1
        return calls > 2  # let two items through, then ask to stop

    result = abyssal.fetch_rolls(conn, client, should_stop=stop)
    assert len(client.calls) == 2
    assert result["fetched"] + result["missing"] == 2
    assert result["remaining"] == 4, "the unreached items are still owed"
    assert len(abyssal.pending(conn)) == 4


def test_store_is_idempotent_and_replaces_stale_attributes(conn):
    def count():
        return conn.execute("SELECT COUNT(*) FROM abyssal_attributes WHERE item_id=1002").fetchone()[0]

    assert count() == len(BCS_BODY["dogma_attributes"]) == 14
    abyssal.store_rolls(conn, 1002, BCS_TYPE, BCS_BODY)
    assert count() == 14, "a second store of the same body must not duplicate rows"
    assert conn.execute("SELECT COUNT(*) FROM abyssal_items WHERE item_id=1002").fetchone()[0] == 1
    smaller = {**BCS_BODY, "dogma_attributes": [{"attribute_id": 50, "value": 20.0}]}
    abyssal.store_rolls(conn, 1002, BCS_TYPE, smaller)
    assert count() == 1, "attributes no longer in the body are gone, not merged"
    assert conn.execute(
        "SELECT value FROM abyssal_attributes WHERE item_id=1002 AND attribute_id=50"
    ).fetchone()[0] == 20.0
    abyssal.store_missing(conn, 1002, BCS_TYPE)
    assert count() == 0
    assert conn.execute("SELECT status FROM abyssal_items WHERE item_id=1002").fetchone()[0] == "missing"


def test_progress_reports_position_and_type_name(conn):
    conn.execute("DELETE FROM abyssal_items")
    seen: list[tuple[int, int, str]] = []
    abyssal.fetch_rolls(conn, FakeESIClient(), progress=lambda d, t, m: seen.append((d, t, m)))
    assert [(d, t) for d, t, _ in seen] == [(i, 6) for i in range(6)]
    assert "Abyssal Ballistic Control System" in " ".join(m for *_, m in seen)


# ------------------------------------------------------------ SDE importers
def _jsonl(records):
    return "".join(json.dumps(r) + "\n" for r in records)


def _mini_sde(path):
    """A zip with every file import_zip opens, small enough to read here.

    Three types carry dogma: the web source, the abyssal web, and a Dominix
    that no mutaplasmid touches -- its dogma must be left out of the import.
    """
    files = {
        "categories.jsonl": [{"_key": 7, "name": {"en": "Module"}, "published": True}],
        "groups.jsonl": [
            {"_key": 65, "categoryID": 7, "name": {"en": "Stasis Web"}, "published": True},
            {"_key": 1964, "categoryID": 7, "name": {"en": "Mutaplasmids"}, "published": True},
        ],
        "marketGroups.jsonl": [{"_key": 1, "name": {"en": "Ships"}}],
        "metaGroups.jsonl": [{"_key": 15, "name": {"en": "Abyssal"}}],
        "types.jsonl": [
            {"_key": WEB_TYPE, "groupID": 65, "metaGroupID": 15, "isDynamicType": True,
             "name": {"en": "Abyssal Stasis Webifier"}, "portionSize": 1, "published": True},
            {"_key": WEB_SOURCE, "groupID": 65, "metaGroupID": 2,
             "name": {"en": "Stasis Webifier II"}, "portionSize": 1, "published": True},
            {"_key": WEB_MUTATOR, "groupID": 1964, "metaGroupID": 15,
             "name": {"en": "Gravid Stasis Webifier Mutaplasmid"}, "portionSize": 1,
             "published": True},
            {"_key": 645, "groupID": 27, "name": {"en": "Dominix"}, "portionSize": 1,
             "published": True},
        ],
        "dogmaAttributes.jsonl": [
            {"_key": 20, "name": "speedFactor", "displayName": {"en": "Maximum Velocity Bonus"},
             "unitID": 124, "highIsGood": True, "defaultValue": 0.0, "published": True},
            {"_key": 50, "name": "cpu", "displayName": {"en": "CPU usage"}, "unitID": 106,
             "highIsGood": False, "defaultValue": 0.0, "published": True},
            {"_key": 160, "name": "trackingSpeed", "displayName": {"en": "Tracking Speed"},
             "highIsGood": True, "defaultValue": 0.0, "published": True},
            {"_key": 9999, "name": "noDisplayName", "published": False},
        ],
        "dogmaUnits.jsonl": [
            {"_key": 124, "name": "Modifier Relative Percent", "displayName": {"en": "%"}},
            {"_key": 106, "name": "Teraflops", "displayName": {"en": "tf"}},
        ],
        "dynamicItemAttributes.jsonl": [
            {"_key": WEB_MUTATOR,
             "attributeIDs": [{"_key": 20, "min": 0.9, "max": 1.1, "highIsGood": False},
                              {"_key": 50, "min": 0.8, "max": 1.5}],
             "inputOutputMapping": [{"applicableTypes": [WEB_SOURCE], "resultingType": WEB_TYPE}]},
        ],
        "typeDogma.jsonl": [
            {"_key": WEB_SOURCE, "dogmaAttributes": [{"attributeID": 20, "value": -60.0},
                                                     {"attributeID": 50, "value": 30.0}]},
            {"_key": WEB_TYPE, "dogmaAttributes": [{"attributeID": 182, "value": 3392.0}]},
            {"_key": 645, "dogmaAttributes": [{"attributeID": 50, "value": 700.0}]},
        ],
        "mapRegions.jsonl": [{"_key": 10000002, "name": {"en": "The Forge"}}],
        "mapSolarSystems.jsonl": [
            {"_key": 30000142, "name": {"en": "Jita"}, "constellationID": 20000020,
             "regionID": 10000002, "securityStatus": 0.9},
        ],
        "npcCorporations.jsonl": [{"_key": 1000035, "name": {"en": "Caldari Navy"}}],
        "stationOperations.jsonl": [{"_key": 26, "operationName": {"en": "Assembly Plant"}}],
        "npcStations.jsonl": [
            {"_key": 60003760, "solarSystemID": 30000142, "celestialIndex": 4, "orbitIndex": 4,
             "ownerID": 1000035, "operationID": 26, "useOperationName": True},
        ],
    }
    with zipfile.ZipFile(path, "w") as zf:
        for name, records in files.items():
            zf.writestr(name, _jsonl(records))
    return path


def test_import_zip_fills_the_dogma_tables_and_the_dynamic_type_flag(tmp_path):
    conn = db.init(tmp_path / "sde.sqlite")
    sde.import_zip(conn, _mini_sde(tmp_path / "mini.zip"), build=3487903)

    flags = {r[0]: r[1] for r in conn.execute("SELECT type_id, is_dynamic_type FROM sde_types")}
    assert flags == {WEB_TYPE: 1, WEB_SOURCE: 0, WEB_MUTATOR: 0, 645: 0}

    attrs = {r["attribute_id"]: dict(r) for r in conn.execute("SELECT * FROM sde_dogma_attributes")}
    assert attrs[20]["display_name"] == "Maximum Velocity Bonus"
    assert (attrs[20]["unit_id"], attrs[20]["high_is_good"]) == (124, 1)
    assert attrs[50]["high_is_good"] == 0
    assert attrs[160]["unit_id"] is None, "trackingSpeed has no unit in the SDE"
    assert attrs[9999]["display_name"] is None and attrs[9999]["high_is_good"] is None

    units = {r[0]: r[1] for r in conn.execute("SELECT unit_id, display_name FROM sde_dogma_units")}
    assert units == {124: "%", 106: "tf"}

    ranges = {
        r["attribute_id"]: dict(r)
        for r in conn.execute("SELECT * FROM sde_mutator_ranges WHERE mutator_type_id=?", (WEB_MUTATOR,))
    }
    assert ranges[20]["high_is_good"] == 0, "the per-mutaplasmid override is kept as stored"
    assert ranges[50]["high_is_good"] is None, "absent override stays NULL, not a default"
    assert (ranges[50]["min_mult"], ranges[50]["max_mult"]) == (0.8, 1.5)
    assert {r["resulting_type_id"] for r in ranges.values()} == {WEB_TYPE}

    dogma_types = {r[0] for r in conn.execute("SELECT DISTINCT type_id FROM sde_type_dogma")}
    assert dogma_types == {WEB_SOURCE, WEB_TYPE}, "applicableTypes plus resultingType, nothing else"
    assert conn.execute(
        "SELECT value FROM sde_type_dogma WHERE type_id=? AND attribute_id=20", (WEB_SOURCE,)
    ).fetchone()[0] == -60.0

    assert db.get_meta(conn, "sde_tables_version") == str(sde.SDE_TABLES_VERSION)
    assert sde.installed_build(conn) == 3487903
    assert not sde.tables_stale(conn)


def test_a_current_build_with_stale_tables_reimports_from_the_cached_zip(tmp_path, monkeypatch):
    """An install that imported build N before these tables existed already
    has the latest build number recorded, so the build comparison alone
    would never import again. installed_build reports None until the
    tables version matches, which sends ensure_current through the cached
    zip once -- and only once."""
    conn = db.init(tmp_path / "stale.sqlite")
    db.set_meta(conn, "sde_build", "3487903")
    assert sde.tables_stale(conn)
    assert sde.installed_build(conn) is None
    assert queries.sde_build(conn) == "3487903", "the display string is untouched by staleness"

    zip_path = _mini_sde(tmp_path / "sde-3487903.zip")
    downloads: list[int] = []

    def fake_download(build, settings=None, progress=None):
        downloads.append(build)
        return zip_path

    monkeypatch.setattr(sde, "latest_build", lambda settings=None: 3487903)
    monkeypatch.setattr(sde, "download", fake_download)

    updated, build = sde.ensure_current(conn)
    assert (updated, build) == (True, 3487903)
    assert downloads == [3487903]
    assert not sde.tables_stale(conn)
    assert conn.execute("SELECT COUNT(*) FROM sde_mutator_ranges").fetchone()[0] == 2

    updated, _ = sde.ensure_current(conn)
    assert updated is False, "once current, the same build is not imported again"
    assert downloads == [3487903]


def test_tables_stale_is_false_for_a_database_with_no_sde_at_all(tmp_path):
    """A fresh install has nothing cached to re-import from; the first-run
    download flow owns that case, so the startup auto-import must not fire."""
    conn = db.init(tmp_path / "fresh.sqlite")
    assert not sde.tables_stale(conn)
    assert sde.installed_build(conn) is None


# --------------------------------------------------------- roll quality SQL
_ROLL_ROWS_SQL = """
    SELECT i.item_id, aa.attribute_id, aa.value AS raw_value,
           COALESCE(td.value, da.default_value) AS raw_base,
           mr.min_mult, mr.max_mult,
           da.high_is_good AS attr_high, mr.high_is_good AS mutator_high,
           {quality} AS q
    FROM abyssal_items i
    JOIN abyssal_attributes aa ON aa.item_id = i.item_id
    JOIN sde_mutator_ranges mr ON mr.mutator_type_id = i.mutator_type_id
                              AND mr.attribute_id = aa.attribute_id
    JOIN sde_dogma_attributes da ON da.attribute_id = aa.attribute_id
    LEFT JOIN sde_type_dogma td ON td.type_id = i.source_type_id
                               AND td.attribute_id = aa.attribute_id
    WHERE i.status = 'ok'
    ORDER BY i.item_id, aa.attribute_id
"""


def _quality_sql():
    return queries.roll_quality_sql(
        "aa.value", "COALESCE(td.value, da.default_value)", "mr.min_mult", "mr.max_mult",
        "da.high_is_good", "mr.high_is_good", attribute_id="da.attribute_id",
    )


def _roll_rows(conn):
    return conn.execute(_ROLL_ROWS_SQL.format(quality=_quality_sql())).fetchall()


def test_roll_quality_sql_agrees_with_the_python_quality_on_every_seeded_roll(conn):
    """The `roll:` filter ranks in SQL what the inspector ranks in Python;
    the two must never disagree about a percentage. Every rolled row of
    every fetched item is compared: the web's sign-inverted speedFactor
    with CCP's override and its negative base, the BCS sample's unit-111
    rate of fire (ranked on the raw multiplier, not the displayed percent),
    and the item whose source has no dogma rows, where both sides must say
    NULL rather than divide by zero."""
    rows = _roll_rows(conn)
    compared = nulls = 0
    for r in rows:
        high = abyssal.resolve_polarity(r["attribute_id"], r["attr_high"], r["mutator_high"])
        position = abyssal.roll_position(
            r["raw_value"], r["raw_base"], r["min_mult"], r["max_mult"]
        )
        expected = abyssal.quality(position, high)
        if expected is None:
            assert r["q"] is None, (r["item_id"], r["attribute_id"])
            nulls += 1
        else:
            assert r["q"] == pytest.approx(expected * 100, abs=1e-9), (
                r["item_id"], r["attribute_id"],
            )
        compared += 1
    assert compared >= 9, "the web's three rolls, the BCS's three, the unrankable item's three"
    assert nulls >= 3, "item 1007's zero-default bases must have produced NULLs"
    by_key = {(r["item_id"], r["attribute_id"]): r["q"] for r in rows}
    assert by_key[(1001, 20)] == pytest.approx(75.0), "web: -63 in -66..-54, low is good"
    assert by_key[(1001, 50)] == pytest.approx(100 * (1 - 3 / 21))
    assert by_key[(1001, 54)] == pytest.approx(75.0)
    assert by_key[(1002, 204)] == pytest.approx(
        100 * (1 - (0.8828844567859173 - 0.846) / 0.108)
    )
    assert by_key[(1007, 50)] is None


def test_roll_quality_sql_honours_a_polarity_override_when_one_exists(conn, monkeypatch):
    """POLARITY_OVERRIDES is empty today; the day a live check fills it the
    SQL must flip with the Python or `roll:` would contradict the inspector.
    Read at call time, so the override lands without a restart of anything
    but the query."""
    assert "CASE da.attribute_id" not in _quality_sql(), "no override, no CASE"
    monkeypatch.setitem(abyssal.POLARITY_OVERRIDES, 20, True)
    sql = _quality_sql()
    assert "CASE da.attribute_id WHEN 20 THEN 1 ELSE" in sql
    by_key = {(r["item_id"], r["attribute_id"]): r["q"] for r in _roll_rows(conn)}
    assert by_key[(1001, 20)] == pytest.approx(25.0), "the web's roll now reads high-is-good"
    assert by_key[(1001, 50)] == pytest.approx(100 * (1 - 3 / 21)), "other attributes untouched"
    assert abyssal.quality(abyssal.roll_position(-63, -60, 0.9, 1.1),
                           abyssal.resolve_polarity(20, 1, 0)) == pytest.approx(0.25)


def test_roll_quality_sql_divides_as_real_even_with_integer_operands(conn):
    """Bound parameters have no affinity, so 5 / 10 would be 0 in SQLite
    where roll_position gives 0.5; the expression multiplies by 1.0 first.
    Also the negative-base and degenerate cases straight through the SQL."""
    expr = queries.roll_quality_sql(":v", ":b", ":lo", ":hi", ":ah", ":mh")

    def q(v, b, lo, hi, ah, mh):
        return conn.execute(
            f"SELECT {expr}", {"v": v, "b": b, "lo": lo, "hi": hi, "ah": ah, "mh": mh}
        ).fetchone()[0]

    assert q(5, 10, 0, 1, 1, None) == pytest.approx(50.0)
    assert q(-63, -60, 0.9, 1.1, 1, 0) == pytest.approx(75.0)
    assert q(-63, -60, 0.9, 1.1, 1, None) == pytest.approx(25.0), "without the override"
    assert q(5, None, 0.9, 1.1, 1, None) is None, "no base"
    assert q(5, 0, 0.9, 1.1, 1, None) is None, "zero base collapses the range"
    assert q(5, 10, 1.0, 1.0, 1, None) is None, "equal multipliers"
    assert q(5, 10, None, 1.1, 1, None) is None, "no range row"
    assert q(20, 10, 0.9, 1.1, 1, None) == pytest.approx(100.0), "clamped above"
    assert q(1, 10, 0.9, 1.1, 1, None) == pytest.approx(0.0), "clamped below"
    assert q(1, 10, 0.9, 1.1, 0, None) == pytest.approx(100.0), "low is good mirrors"
    assert q(12, 10, 0.9, 1.1, None, None) == pytest.approx(100.0), "no flag reads high-is-good"


# ------------------------------------------------------- card read queries
def test_abyssal_type_counts_list_owned_dynamic_types_busiest_first(conn):
    """Four webs (three with rolls stored, one ESI 404'd), one BCS, one
    unfetched microwarpdrive; the mutaplasmid stack and the Dominix are not
    dynamic types and stay out. Ties on items break by name, and "50MN"
    sorts before "Abyssal" as the type list in the card will show them."""
    rows = queries.abyssal_type_counts(conn)
    assert [(r["type_id"], r["name"], r["items"], r["fetched"]) for r in rows] == [
        (WEB_TYPE, "Abyssal Stasis Webifier", 4, 3),
        (MWD_TYPE, "50MN Abyssal Microwarpdrive", 1, 0),
        (BCS_TYPE, "Abyssal Ballistic Control System", 1, 1),
    ]


def test_abyssal_type_counts_facet_by_every_filter_but_the_abyssal_chip(conn):
    """The card's picker facets its type counts by every filter but the
    card's own kinds (assets_view passes exclude_kinds); exclude_level alone
    is exercised here because it is the narrower drop -- picking one type
    must not hide the others."""
    from evasset import omni

    spec = omni.parse('abyssal:"Abyssal Stasis Webifier" stat:cpu<26')
    where, params = spec.where(exclude_level=omni.ABYSSAL_KIND)
    rows = queries.abyssal_type_counts(conn, where, params)
    assert [(r["name"], r["items"], r["fetched"]) for r in rows] == [
        ("Abyssal Ballistic Control System", 1, 1),
    ]
    where, params = spec.where()
    assert queries.abyssal_type_counts(conn, where, params) == [], "unfaceted, the chip wins"
    where, params = omni.parse("-stat:cpu<26").where(exclude_level=omni.ABYSSAL_KIND)
    rows = queries.abyssal_type_counts(conn, where, params)
    assert [(r["name"], r["items"]) for r in rows] == [
        ("Abyssal Stasis Webifier", 4), ("50MN Abyssal Microwarpdrive", 1),
    ]


def test_abyssal_type_attributes_come_from_the_mutator_ranges_not_the_estate(conn):
    """Everything the type's mutaplasmids can roll, whether or not any owned
    item carries it: the seed's BCS range table lists a synthetic
    droneDamageBonus row (no real BCS mutaplasmid does) that the sample
    item's body never carried, and it is listed all the same. Resolved via
    resulting_type_id, ordered by label, with the unit symbol along."""
    web = queries.abyssal_type_attributes(conn, "Abyssal Stasis Webifier")
    assert [(a["attribute_id"], a["label"], a["unit"]) for a in web] == [
        (50, "CPU usage", "tf"), (20, "Maximum Velocity Bonus", "%"), (54, "Optimal Range", "m"),
    ]
    assert set(web[0]) == {"attribute_id", "name", "label", "unit_id", "unit", "high_is_good"}
    assert web[0]["name"] == "cpu" and web[0]["unit_id"] == 106
    # The stored polarity, resolved the way a roll's is: the mutaplasmid's
    # override on speedFactor (low-is-good), the attribute's own flag on
    # cpu (low) and on the range (high).
    assert {a["attribute_id"]: a["high_is_good"] for a in web} == {50: False, 20: False, 54: True}
    bcs = queries.abyssal_type_attributes(conn, "Abyssal Ballistic Control System")
    assert {a["attribute_id"] for a in bcs} == {50, 204, 213, 1255}
    assert queries.abyssal_type_attributes(conn, "Stasis Webifier II") == []
    assert queries.abyssal_type_attributes(conn, "No Such Type") == []


def test_abyssal_type_columns_are_the_rolled_attributes_the_estate_has_values_for(conn):
    """The card's pickers and the table's columns are built from this pair,
    not from the range table alone: the BCS's synthetic droneDamageBonus is
    rolled on paper and on no item, so it would be a picker entry with no
    range to slide along and a column blank on every row. The attributes
    are the range table's, in its label order, cut to the keys of the
    bounds, and returned with those bounds so the two cannot disagree; a
    type with nothing fetched has neither."""
    attrs, bounds = queries.abyssal_type_columns(conn, "Abyssal Ballistic Control System")
    assert [a["attribute_id"] for a in attrs] == [50, 213, 204]
    assert set(bounds) == {50, 213, 204}
    assert bounds == queries.abyssal_attribute_bounds(conn, "Abyssal Ballistic Control System")
    full = queries.abyssal_type_attributes(conn, "Abyssal Ballistic Control System")
    without_base = [{k: v for k, v in a.items() if k != "base"} for a in attrs]
    assert without_base == [a for a in full if a["attribute_id"] != 1255], "same dicts, same order"
    # Each carries the type's base display value: the Domination BCS's own
    # 24 tf CPU, and its 0.9 rate-of-fire multiplier shown as 10%.
    assert {a["attribute_id"]: a["base"] for a in attrs} == pytest.approx(
        {50: 24.0, 213: 10.0, 204: 10.0}
    )

    attrs, bounds = queries.abyssal_type_columns(conn, "Abyssal Stasis Webifier")
    assert [a["attribute_id"] for a in attrs] == [50, 20, 54]
    assert set(bounds) == {50, 20, 54}

    assert queries.abyssal_type_columns(conn, "50MN Abyssal Microwarpdrive") == ([], {})
    assert queries.abyssal_type_columns(conn, "No Such Type") == ([], {})


def test_abyssal_attribute_bases_are_the_dominant_sources_display_values(conn):
    """A type is made from several source modules, so the base ticked on the
    card's track is the base of the source most fetched items came from:
    two of the three fetched webifiers are Webifier IIs (the third names an
    unknown source), so the II's dogma is the base -- -60% speed, 30 tf
    CPU, 10 km range -- with the attribute default standing in where the
    source has no row (the drone bonus the range table lists for the BCS).
    A type with nothing fetched has no dominant source and no bases."""
    web = queries.abyssal_attribute_bases(conn, "Abyssal Stasis Webifier")
    assert web == pytest.approx({20: -60.0, 50: 30.0, 54: 10000.0})
    bcs = queries.abyssal_attribute_bases(conn, "Abyssal Ballistic Control System")
    assert bcs == pytest.approx({50: 24.0, 204: 10.0, 213: 10.0, 1255: 0.0})
    assert queries.abyssal_attribute_bases(conn, "50MN Abyssal Microwarpdrive") == {}
    assert queries.abyssal_attribute_bases(conn, "No Such Type") == {}

    # Two more webifiers from the unknown source outvote the IIs, and the
    # unknown source has no dogma at all: every base falls to the default.
    conn.executescript(f"""
      INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
                          location_flag,location_type,is_singleton) VALUES
        ('character',100,1011,{WEB_TYPE},1,60003760,'Hangar','station',1),
        ('character',100,1012,{WEB_TYPE},1,60003760,'Hangar','station',1);
      INSERT INTO abyssal_items VALUES
        (1011,{WEB_TYPE},{UNKNOWN_SOURCE},{WEB_MUTATOR},42,'ok','2026-09-01T00:00:00+00:00'),
        (1012,{WEB_TYPE},{UNKNOWN_SOURCE},{WEB_MUTATOR},42,'ok','2026-09-01T00:00:00+00:00');
    """)
    assert queries.abyssal_attribute_bases(conn, "Abyssal Stasis Webifier") == pytest.approx(
        {20: 0.0, 50: 0.0, 54: 0.0}
    )


def test_display_high_is_good_flips_the_stored_polarity_for_the_inverted_units():
    """A rate-of-fire multiplier (unit 111) is low-is-good as stored and
    reads as a bonus percent that is high-is-good; every other unit keeps
    the stored sense, and an unknown polarity reads as high-is-good."""
    assert abyssal.display_high_is_good(False, 111) is True
    assert abyssal.display_high_is_good(True, 108) is False
    assert abyssal.display_high_is_good(False, 106) is False
    assert abyssal.display_high_is_good(1, 124) is True
    assert abyssal.display_high_is_good(0, 124) is False
    assert abyssal.display_high_is_good(None, None) is True
    assert abyssal.display_high_is_good(None, 111) is False


def test_count_assets_counts_exactly_the_rows_fetch_assets_returns(conn):
    """The card's live "N of TOTAL match" comes from this, so it must
    agree with the table for the same WHERE -- the bare one and a stat:
    clause that correlates into the rolls."""
    assert queries.count_assets(conn) == len(queries.fetch_assets(conn))
    where, params = omni.parse('abyssal:"Abyssal Stasis Webifier" stat:cpu<=27').where()
    assert queries.count_assets(conn, where, params) == len(
        queries.fetch_assets(conn, where, params)
    )
    assert queries.count_assets(conn, where, params) == 2, "1001 and 1007 roll 27 tf"
    where, params = omni.parse('abyssal:"No Such Type"').where()
    assert queries.count_assets(conn, where, params) == 0


def test_abyssal_type_attributes_disambiguate_a_shared_display_name(conn):
    """Real SDE shape: 554 signatureRadiusBonus (unit 124, %) and 983
    signatureRadiusAdd (unit 1, m) both display "Signature Radius
    Modifier". Two attributes with the same label AND no distinct unit fall
    back to the internal name, so no two rows in a picker ever read alike."""
    conn.executescript(f"""
      INSERT INTO sde_dogma_attributes
        (attribute_id,name,display_name,unit_id,high_is_good,default_value,published) VALUES
        (554,'signatureRadiusBonus','Signature Radius Modifier',124,0,0,1),
        (983,'signatureRadiusAdd','Signature Radius Modifier',1,0,0,1),
        (9001,'twinA','Twin',NULL,1,0,1),
        (9002,'twinB','Twin',NULL,1,0,1);
      INSERT INTO sde_mutator_ranges VALUES
        ({WEB_MUTATOR},554,0.9,1.1,NULL,{WEB_TYPE}),
        ({WEB_MUTATOR},983,0.9,1.1,NULL,{WEB_TYPE}),
        ({WEB_MUTATOR},9001,0.9,1.1,NULL,{WEB_TYPE}),
        ({WEB_MUTATOR},9002,0.9,1.1,NULL,{WEB_TYPE});
    """)
    attrs = queries.abyssal_type_attributes(conn, "Abyssal Stasis Webifier")
    labels = {a["attribute_id"]: a["label"] for a in attrs}
    assert labels[554] == "Signature Radius Modifier (%)"
    assert labels[983] == "Signature Radius Modifier (m)"
    assert labels[9001] == "Twin (twinA)" and labels[9002] == "Twin (twinB)"
    assert labels[50] == "CPU usage", "an unshared label is left alone"
    assert len(set(labels.values())) == len(labels)
    assert [a["label"] for a in attrs] == sorted((a["label"] for a in attrs), key=str.casefold)


def test_abyssal_attribute_bounds_are_estate_extremes_after_unit_conversion(conn):
    """Sliders are scoped per type: the web's cpu bounds must be the web's,
    not the BCS's. A second BCS whose rate-of-fire multiplier is 0.95 (unit
    111, displayed as 5%) proves the extremes are taken after conversion:
    the raw minimum 0.8829 is the displayed MAXIMUM, 11.71%."""
    conn.executescript(f"""
      INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
                          location_flag,location_type,is_singleton) VALUES
        ('character',100,1009,{BCS_TYPE},1,60003760,'Hangar','station',1),
        ('character',100,1010,{WEB_TYPE},1,60003760,'Hangar','station',1);
      INSERT INTO abyssal_items VALUES
        (1009,{BCS_TYPE},{BCS_SOURCE},{BCS_MUTATOR},42,'ok','2026-09-01T00:00:00+00:00'),
        (1010,{WEB_TYPE},{WEB_SOURCE},{WEB_MUTATOR},42,'ok','2026-09-01T00:00:00+00:00');
      INSERT INTO abyssal_attributes VALUES
        (1009,204,0.95),(1009,50,20),
        (1010,20,-57),(1010,50,40),(1010,54,9000),(1010,73,5000);
    """)
    web = queries.abyssal_attribute_bounds(conn, "Abyssal Stasis Webifier")
    assert set(web) == {20, 50, 54}, "rolled attributes only: duration is stored, not rolled"
    assert web[50] == (27.0, 40.0)
    assert web[20] == (-63.0, -57.0)
    assert web[54] == (9000.0, 11000.0)
    bcs = queries.abyssal_attribute_bounds(conn, "Abyssal Ballistic Control System")
    assert bcs[50] == (20.0, pytest.approx(25.8))
    lo, hi = bcs[204]
    assert lo == pytest.approx(5.0) and hi == pytest.approx(11.71, abs=0.01)
    assert lo < hi
    assert queries.abyssal_attribute_bounds(conn, "50MN Abyssal Microwarpdrive") == {}
    assert queries.abyssal_attribute_bounds(conn, "No Such Type") == {}


def test_abyssal_cells_and_summaries_come_from_one_stream_and_agree(conn):
    """The roll columns and the badge tooltip must show the same percentages
    for the same item, so the cells' qualities rounded are exactly the
    numbers in the summary line; unfetched, 404'd and unknown-mutator items
    have no cells, and an unrankable fetched item has cells with no quality
    so its values still show."""
    ids = [1001, 1002, 1003, 1005, 1006, 1007, 1008]
    summaries, cells = queries.abyssal_roll_data(conn, ids)
    assert set(cells) == {1001, 1002, 1007}
    web = cells[1001]
    assert set(web) == {20, 50, 54}
    assert web[20] == (-63.0, pytest.approx(0.75))
    assert web[54] == (11000.0, pytest.approx(0.75))
    assert web[50][0] == 27.0 and web[50][1] == pytest.approx(1 - 3 / 21)
    percents = sorted(round(q * 100) for _v, q in web.values())
    in_summary = sorted(
        int(bit.rsplit(" ", 1)[1].rstrip("%")) for bit in summaries[1001].split(" · ")
    )
    assert percents == in_summary
    assert summaries[1001] == "CPU 86% · Speed 75% · Range 75%"
    bcs = cells[1002]
    assert set(bcs) == {50, 204, 213}
    assert bcs[204][0] == pytest.approx(11.71, abs=0.01), "display value, unit 111"
    assert cells[1007] == {20: (-63.0, None), 50: (27.0, None), 54: (11000.0, None)}
    assert abyssal.mean_quality(web) == pytest.approx((0.75 + 0.75 + (1 - 3 / 21)) / 3)
    assert abyssal.mean_quality(cells[1007]) is None


def test_abyssal_pending_count_wraps_pending_and_narrows_by_type_name(conn):
    assert queries.abyssal_pending_count(conn) == 1, "the unfetched microwarpdrive"
    assert queries.abyssal_pending_count(conn, None) == 1
    assert queries.abyssal_pending_count(conn, []) == 1, "an empty list is the empty chip: all"
    assert queries.abyssal_pending_count(conn, ["50MN Abyssal Microwarpdrive"]) == 1
    assert queries.abyssal_pending_count(conn, ["Abyssal Stasis Webifier"]) == 0
    assert queries.abyssal_pending_count(conn, ["No Such Type"]) == 0
    conn.execute("DELETE FROM abyssal_items")
    assert queries.abyssal_pending_count(conn) == 6
    assert queries.abyssal_pending_count(conn, ["Abyssal Stasis Webifier"]) == 4
    assert queries.abyssal_pending_count(
        conn, ["Abyssal Stasis Webifier", "Abyssal Ballistic Control System"]
    ) == 5
    assert queries.abyssal_pending_count(conn, ["Gravid Stasis Webifier Mutaplasmid"]) == 0


# ------------------------------------------------------------ chip helpers
def test_mean_quality_averages_the_rankable_rolls_only():
    assert abyssal.mean_quality({}) is None
    assert abyssal.mean_quality({50: (27.0, None)}) is None
    assert abyssal.mean_quality({50: (27.0, 0.5)}) == 0.5
    assert abyssal.mean_quality({50: (27.0, 0.2), 20: (-63.0, 0.8), 54: (11000.0, None)}) == (
        pytest.approx(0.5)
    )
    assert abyssal.mean_quality({1: (0.0, 0.0), 2: (0.0, 1.0), 3: (0.0, 1.0)}) == (
        pytest.approx(2 / 3)
    )


def test_strip_type_prefix_removes_the_word_abyssal_wherever_ccp_put_it():
    cases = [
        ("Abyssal Stasis Webifier", "Stasis Webifier"),
        ("50MN Abyssal Microwarpdrive", "50MN Microwarpdrive"),
        ("Large Abyssal Shield Booster", "Large Shield Booster"),
        ("Light Mutated Drone", "Light Mutated Drone"),
        ("Abyssal", "Abyssal"),
        ("Abyssalish Thing", "Abyssalish Thing"),
        ("", ""),
    ]
    seen = 0
    for name, expected in cases:
        assert abyssal.strip_type_prefix(name) == expected, name
        seen += 1
    assert seen == len(cases) == 7
