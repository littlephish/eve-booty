"""Omnibox grammar and its SQL, exercised without a window."""

from __future__ import annotations

import random

import pytest

from evasset import db, omni, queries
from evasset.config import ASSET_SAFETY_LOCATION_ID
from evasset.omni import Chip, FilterSpec, parse

JITA_STATION = "Jita IV - Moon 4 - Caldari Navy Assembly Plant"


@pytest.fixture()
def conn(tmp_path):
    """A small estate built to trap every axis the grammar can filter on.

    Deliberate traps: the Jita station name, and the owner "Jita Trader",
    both contain the word an item ("Jita Snowglobe") and a custom name
    ("Jita run leftovers") legitimately match -- bare text must hit only the
    latter two. One row is fitted to a ship, one sits in asset safety (with
    NULL system/region, to prove negations keep NULL-labelled rows), one is a
    blueprint copy, and two types carry no price row at all.
    """
    c = db.init(tmp_path / "omni.sqlite")
    c.executescript(f"""
      INSERT INTO sde_categories VALUES (6,'Ship',1),(4,'Material',1),(7,'Module',1),
        (9,'Blueprint',1);
      INSERT INTO sde_groups VALUES (513,6,'Freighter',1),(27,6,'Battleship',1),
        (18,4,'Mineral',1),(55,7,'Autocannon',1),(105,9,'Battleship Blueprint',1);
      INSERT INTO sde_meta_groups VALUES (1,'Tech I'),(2,'Tech II');
      INSERT INTO sde_types (type_id,name,group_id,meta_group_id,volume,portion_size,published)
        VALUES
        (20185,'Charon',513,1,16250000,1,1),
        (645,'Dominix',27,1,454500,1,1),
        (34,'Tritanium',18,1,0.01,1,1),
        (2873,'125mm Gatling AutoCannon II',55,2,5,1,1),
        (995,'Jita Snowglobe',18,1,1,1,1),
        (1000,'Dominix Blueprint',105,NULL,0.01,1,1),
        (999,'Paint Widget',18,1,2,1,1);
      INSERT INTO sde_regions VALUES (10000002,'The Forge'),(10000043,'Domain');
      INSERT INTO sde_systems VALUES (30000142,'Jita',20000020,10000002,0.9),
        (30002187,'Amarr',20000322,10000043,1.0);
      INSERT INTO sde_stations VALUES (60003760,'{JITA_STATION}',30000142,10000002),
        (60008494,'Amarr VIII (Oris) - Emperor Family Academy',30002187,10000043);
      INSERT INTO characters (character_id,name,corporation_id,scopes,enabled) VALUES
        (100,'Test Pilot',2000,'s',1),
        (101,'Jita Trader',2000,'s',1);
      INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
                          location_flag,location_type,is_singleton,is_blueprint_copy,
                          custom_name,root_location_id,system_id,region_id) VALUES
        ('character',100, 1,20185,      1,60003760,'Hangar', 'station',1,0,NULL,
         60003760,30000142,10000002),
        ('character',100, 2,  645,      1,60003760,'Hangar', 'station',1,0,NULL,
         60003760,30000142,10000002),
        ('character',100, 3, 2873,      1,       2,'HiSlot0','item',   1,0,NULL,
         60003760,30000142,10000002),
        ('character',100, 4,   34,1000000,60003760,'Hangar', 'station',0,0,NULL,
         60003760,30000142,10000002),
        ('character',101, 5,  995,      2,60008494,'Hangar', 'station',0,0,NULL,
         60008494,30002187,10000043),
        ('character',101, 6,   34,     50,60008494,'Hangar', 'station',0,0,'Jita run leftovers',
         60008494,30002187,10000043),
        ('character',100, 7, 1000,      1,60003760,'Hangar', 'station',1,1,NULL,
         60003760,30000142,10000002),
        ('character',100, 8,  999,      3,60003760,'Hangar', 'station',0,0,NULL,
         60003760,30000142,10000002),
        ('character',100, 9,   34,    500,{ASSET_SAFETY_LOCATION_ID},'AssetSafety','asset_safety',
         0,0,NULL,{ASSET_SAFETY_LOCATION_ID},NULL,NULL),
        ('character',101,10,  645,      1,60008494,'Hangar', 'station',1,0,NULL,
         60008494,30002187,10000043);
      INSERT INTO prices VALUES
        (20185,2000000000,2000000000,'contract_avg',12,'2026-08-04T00:00:00+00:00'),
        (645,   170000000, 180000000,'jita',         1,'2026-08-04T00:00:00+00:00'),
        (34,          5.0,       5.5,'jita',         1,'2026-08-04T00:00:00+00:00'),
        (2873,    1000000,   1200000,'jita',         1,'2026-08-04T00:00:00+00:00'),
        (995,         100,       120,'jita',         1,'2026-08-04T00:00:00+00:00');
    """)
    return c


def _ids(conn, spec, exclude_level=None):
    where, params = spec.where(exclude_level=exclude_level)
    return {r["item_id"] for r in queries.fetch_assets(conn, where, params)}


ALL_ITEMS = set(range(1, 11))


# ------------------------------------------------------------------- grammar
def test_every_prefix_parses_to_its_chip_kind():
    cases = [
        ("loc:Jita", "location"),
        ("location:Jita", "location"),
        ("sys:Amarr", "system"),
        ("system:Amarr", "system"),
        ("region:Domain", "region"),
        ("owner:Somebody", "owner"),
        ("cat:Ship", "category"),
        ("category:Ship", "category"),
        ("group:Freighter", "group"),
        ("meta:Tech", "meta"),
        ("is:fitted", "is"),
        ("val:>10m", "val"),
    ]
    seen = 0
    for token, kind in cases:
        spec = parse(token)
        assert spec.text == "", f"{token} leaked into bare text"
        assert [c.kind for c in spec.chips] == [kind]
        assert not spec.chips[0].negated
        seen += 1
    assert seen == len(cases) == 12


def test_quoted_values_keep_their_spaces():
    spec = parse('loc:"Jita IV - Moon 4" tritanium')
    assert spec.chips == [Chip(kind="location", value="Jita IV - Moon 4")]
    assert spec.text == "tritanium"


def test_minus_negates_a_chip_but_not_a_bare_word():
    spec = parse('-is:fitted -cat:Mineral -val:>10m -stray')
    assert [c.negated for c in spec.chips] == [True, True, True]
    assert [(c.kind, c.value) for c in spec.chips] == [
        ("is", "fitted"), ("category", "Mineral"), ("val", ">10m"),
    ]
    # A leading minus on a bare word is not a filter operator; someone
    # searching for "-stray" gets to keep their hyphen.
    assert spec.text == "-stray"


def test_val_suffixes_scale_the_bound_parameter():
    cases = [("val:>10m", ">", 10_000_000.0), ("val:<=2k", "<=", 2_000.0),
             ("val:>=1.5b", ">=", 1_500_000_000.0), ("val:<3t", "<", 3_000_000_000_000.0),
             ("val:>1234", ">", 1234.0)]
    seen = 0
    for token, op, amount in cases:
        where, params = parse(token).where()
        assert f"{op} ?" in where
        # >= must not have matched as > with a stray '=' left behind.
        assert f"{op}= ?" not in where
        assert params == (amount,)
        seen += 1
    assert seen == len(cases) == 5


def test_unknown_is_value_and_malformed_val_degrade_to_bare_text():
    spec = parse("is:stale val:10m loc:")
    assert spec.chips == []
    assert spec.text == "is:stale val:10m loc:"


def test_to_text_round_trips_through_parse():
    raw = f'tritanium -cat:Mineral loc:"{JITA_STATION}" is:bpc val:>10m'
    spec = parse(raw)
    assert spec.chips, "the round-trip means nothing if nothing parsed"
    assert parse(spec.to_text()) == spec


def test_round_trip_survives_an_embedded_quote_in_a_chip_value():
    """Regression: to_text used to emit an embedded quote unescaped, and on
    re-parse it toggled the tokenizer's quoted state and swallowed every
    later token into one chip -- a saved view corrupting itself. Reachable
    from plain parse output, no hand-built chips required."""
    spec = parse('cat:a"b is:bpc')
    assert parse(spec.to_text()) == spec
    hand_built = FilterSpec(text="", chips=[Chip(kind="owner", value='say "hi"')])
    assert parse(hand_built.to_text()) == hand_built


def test_quoted_empty_value_is_a_chip_and_unquoted_empty_is_not():
    """Regression: to_text serialised an empty-value chip as `loc:""` but
    parse rejected any empty value, so the chip degraded to bare text on the
    way back in. The two halves now agree: quotes signal intent."""
    spec = parse('loc:""')
    assert spec.chips == [Chip(kind="location", value="")]
    assert spec.text == ""
    assert parse(spec.to_text()) == spec
    # A bare `loc:` is the half-typed state, not an empty-label filter.
    assert parse("loc:").chips == []
    assert parse("loc:").text == "loc:"


def test_token_shaped_bare_text_survives_the_round_trip():
    """Adversarial-review regression: a bare word that happens to look like a
    token ("cat:mystery" typed as literal search text) used to re-parse as a
    category chip on saved-view recall -- a different filter than the one
    saved. to_text now quotes such words and parse unquotes them back."""
    spec = FilterSpec(text="cat:mystery")
    assert parse(spec.to_text()) == spec
    quoted = parse('"cat:mystery" trit')
    assert quoted.chips == []
    assert quoted.text == "cat:mystery trit"


def test_round_trip_quotes_whitespace_other_than_plain_spaces():
    """Regression: to_text only quoted on a space, so a tab-bearing value
    serialised bare and re-parsed as two words."""
    spec = FilterSpec(text="", chips=[Chip(kind="owner", value="a\tb")])
    assert parse(spec.to_text()) == spec


def test_round_trip_holds_for_generated_specs():
    """Property test over the whole constructible space: any level-chip
    value at all (quotes, backslashes, tabs, newlines, colons, unicode,
    empty), valid is:/val: values, random negation, random bare words.
    Seeded so a failure reproduces exactly; the seed is arbitrary, not a
    date dependency."""
    rng = random.Random(682_431)
    level_alphabet = 'ab "\\\t\n-:é'
    kinds = list(omni.LEVEL_KINDS) + ["is", "val"]
    checked = 0
    for _ in range(300):
        chips = []
        for _ in range(rng.randint(0, 5)):
            kind = rng.choice(kinds)
            if kind == "is":
                value = rng.choice(omni.IS_FLAGS)
            elif kind == "val":
                value = (rng.choice([">", "<", ">=", "<="])
                         + str(rng.randint(1, 999))
                         + rng.choice(["", "k", "m", "b", "t"]))
            else:
                value = "".join(rng.choice(level_alphabet)
                                for _ in range(rng.randint(0, 8)))
            chips.append(Chip(kind=kind, value=value, negated=rng.random() < 0.5))
        words = ["".join(rng.choice("abcdefg-") for _ in range(rng.randint(1, 6)))
                 for _ in range(rng.randint(0, 3))]
        spec = FilterSpec(text=" ".join(words), chips=chips)
        assert parse(spec.to_text()) == spec, f"round-trip broke on {spec!r}"
        checked += 1
    assert checked == 300


def test_describe_counts_chips_plus_bare_text_as_one():
    assert parse("").describe() == 0
    assert parse("tritanium veldspar").describe() == 1
    assert parse("tritanium is:bpc -cat:Ship").describe() == 3
    assert parse("").is_empty
    assert not parse("x").is_empty


# ----------------------------------------------------------------------- SQL
def test_empty_spec_builds_an_empty_where(conn):
    assert parse("").where() == ("", ())
    assert _ids(conn, parse("")) == ALL_ITEMS


def test_bare_text_matches_item_and_custom_name_but_never_places_or_owners(conn):
    """The trap this fixture exists for: six rows sit in a station whose name
    contains "Jita" and two belong to an owner called "Jita Trader", yet only
    the item literally named Jita Snowglobe and the stack custom-named "Jita
    run leftovers" may match. The old search field failed exactly this."""
    assert _ids(conn, parse("jita")) == {5, 6}
    assert _ids(conn, parse("dominix")) == {2, 7, 10}  # the hull and its blueprint
    assert _ids(conn, parse("jita leftovers")) == {6}  # words AND together


def test_every_is_flag_and_its_negation_pick_the_seeded_rows(conn):
    # Added here rather than to the shared fixture: several tests below assert
    # exact id sets, and a new seeded row would move all of them for the sake
    # of one flag. Tritanium at a station is priced and unfitted, so it lands
    # only in the delivery bucket and leaves the other expectations alone.
    conn.execute(
        "INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,"
        "location_flag,location_type,is_singleton,is_blueprint_copy,custom_name,"
        "root_location_id,system_id,region_id) VALUES "
        "('character',100,11,34,25,60003760,'Deliveries','station',0,0,NULL,"
        "60003760,30000142,10000002)"
    )
    all_items = ALL_ITEMS | {11}

    expectations = {
        "fitted": {3},
        "safety": {9},
        "delivery": {11},
        "unpriced": {7, 8},
        "bpc": {7},
    }
    seen = 0
    for flag, expected in expectations.items():
        assert _ids(conn, parse(f"is:{flag}")) == expected, f"is:{flag}"
        assert _ids(conn, parse(f"-is:{flag}")) == all_items - expected, f"-is:{flag}"
        seen += 1
    # The count is the point: an advertised flag with no expectation here is a
    # flag nothing checks. This caught is:delivery being added without one.
    assert seen == len(omni.IS_FLAGS), "every advertised flag must be exercised"


def test_level_chips_filter_by_exact_label(conn):
    assert _ids(conn, parse('owner:"Test Pilot"')) == {1, 2, 3, 4, 7, 8, 9}
    assert _ids(conn, parse("region:Domain")) == {5, 6, 10}
    assert _ids(conn, parse("sys:Amarr")) == {5, 6, 10}
    assert _ids(conn, parse("group:Freighter")) == {1}
    assert _ids(conn, parse("cat:Ship")) == {1, 2, 10}
    assert _ids(conn, parse('meta:"Tech II"')) == {3}
    assert _ids(conn, parse(f'loc:"{JITA_STATION}"')) == {1, 2, 3, 4, 7, 8}
    # The asset-safety pseudo-location is addressable like any other label.
    assert _ids(conn, parse('loc:"Asset Safety"')) == {9}


def test_item_chips_match_the_exact_name_while_bare_text_matches_substrings(conn):
    """item: is the where-else axis: exact name equality. Bare text is LIKE,
    so hunting Dominix would otherwise also count every Dominix Blueprint --
    the inflated where-else answer the adversarial review caught."""
    rows = queries.fetch_assets(conn)
    exact = {r["item_id"] for r in rows if r["item"] == "Dominix"}
    superset = {r["item_id"] for r in rows if "Dominix" in r["item"]}
    assert exact and superset - exact, "the fixture must carry the substring trap"
    assert _ids(conn, parse("item:Dominix")) == exact
    assert _ids(conn, parse("Dominix")) == superset
    spec = FilterSpec(text="", chips=[Chip(kind="item", value="Dominix")])
    assert parse(spec.to_text()) == spec


def test_chips_of_one_kind_or_together_and_kinds_and_together(conn):
    both_owners = parse('owner:"Test Pilot" owner:"Jita Trader"')
    assert _ids(conn, both_owners) == ALL_ITEMS
    crossed = parse('owner:"Jita Trader" cat:Ship')
    assert _ids(conn, crossed) == {10}


def test_negated_level_chip_keeps_null_labelled_rows(conn):
    """Excluding a region must not also hide asset safety, whose region is
    NULL -- plain `<> ?` would silently drop it."""
    assert _ids(conn, parse("-region:Domain")) == ALL_ITEMS - {5, 6, 10}
    assert _ids(conn, parse('-meta:"Tech II"')) == ALL_ITEMS - {3}  # item 7 has NULL meta


def test_val_comparisons_run_against_quantity_times_sell(conn):
    assert _ids(conn, parse("val:>10m")) == {1, 2, 10}
    assert _ids(conn, parse("val:>1b")) == {1}
    assert _ids(conn, parse("val:>=180m")) == {1, 2, 10}
    # Unpriced rows value as zero, so they land on the cheap side.
    assert _ids(conn, parse("val:<1k")) == {5, 6, 7, 8}
    assert _ids(conn, parse("-val:>10m")) == ALL_ITEMS - {1, 2, 10}


def test_exclude_level_drops_exactly_that_kind_both_polarities(conn):
    spec = parse(f'-loc:"{JITA_STATION}" loc:"Asset Safety" cat:Ship')
    # Faceting for a location-level rail: the location chips vanish, the
    # category chip stays.
    assert _ids(conn, spec, exclude_level="location") == {1, 2, 10}
    # And untouched, the same spec still applies everything.
    assert _ids(conn, spec) == set()
    # A different level being excluded leaves location chips in force.
    assert _ids(conn, spec, exclude_level="owner") == set()
    where, _params = spec.where(exclude_level="category")
    assert "cat.name" not in where


def test_fitted_flag_matches_the_hide_ship_contents_clause_polarity(conn):
    """-is:fitted must be the exact clause the old checkbox applied, so the
    two features can never disagree about what "on a ship" means."""
    where, params = parse("-is:fitted").where()
    assert where == queries.HIDE_SHIP_CONTENTS_CLAUSE
    assert params == ()


def test_where_composes_text_chips_and_flags_together(conn):
    spec = parse('tritanium owner:"Test Pilot" -is:safety')
    assert _ids(conn, spec) == {4}


def test_where_skips_chips_it_cannot_translate(conn):
    """A saved view written by a newer build may carry a flag or comparison
    this build does not know. where() must apply the filters it can honour
    and drop the rest, the same forgiveness parse shows -- not raise a bare
    KeyError over the one chip it cannot translate."""
    spec = FilterSpec(text="", chips=[
        Chip(kind="is", value="stale"),
        Chip(kind="val", value="banana"),
        Chip(kind="category", value="Ship"),
    ])
    where, params = spec.where()
    assert where == "(cat.name = ? COLLATE NOCASE)"
    assert params == ("Ship",)
    assert _ids(conn, spec) == {1, 2, 10}


def test_filterspec_where_only_ever_binds_values_as_parameters():
    """A chip value is user input; it must never be interpolated into the
    SQL string where a crafted label could smuggle in its own clauses."""
    hostile = "x' OR '1'='1"
    spec = FilterSpec(text="", chips=[Chip(kind="owner", value=hostile)])
    where, params = spec.where()
    assert hostile not in where
    assert params == (hostile,)


def _names(conn, query: str) -> set[str]:
    """Item names a query returns, for comparing two spellings of one filter."""
    from evasset import queries

    spec = parse(query)
    where, params = spec.where()
    sql = queries.ASSET_ROWS + (f" WHERE {where}" if where else "")
    return {r["item"] for r in conn.execute(sql, params)}


# ------------------------------------------------------------ case folding
def test_chip_values_match_regardless_of_case(conn):
    """A bare word already ignored case, because SQLite's LIKE does for ASCII.
    Chips used a plain =, so "tritanium" found Tritanium while "cat:ship"
    found nothing -- the same query typed two ways, behaving differently for a
    reason nobody could see from the outside.
    """
    for query in ("cat:Ship", "cat:ship", "cat:SHIP", "cat:sHiP"):
        assert _names(conn, query) == _names(conn, "cat:Ship"), query


def test_case_folding_applies_to_every_chip_kind(conn):
    """Not just category: owner, location, system, region, group and meta all
    build the same comparison, so all of them were affected."""
    for lower, proper in (
        ('owner:"test pilot"', 'owner:"Test Pilot"'),
        ("sys:jita", "sys:Jita"),
        ('region:"the forge"', 'region:"The Forge"'),
    ):
        assert _names(conn, lower) == _names(conn, proper), lower


def test_case_folding_applies_to_negation(conn):
    """A negated chip that matched nothing would exclude nothing, which reads
    as the filter silently doing nothing at all."""
    assert _names(conn, "-cat:ship") == _names(conn, "-cat:Ship")


def test_a_row_lands_on_exactly_one_side_whatever_the_case(conn):
    everything = _names(conn, "")
    included = _names(conn, "cat:ship")
    excluded = _names(conn, "-cat:ship")

    assert not (included & excluded)
    assert included | excluded == everything


def test_quoted_multi_word_values_fold_too(conn):
    """The values most worth typing by hand are the long ones."""
    assert _names(conn, 'loc:"jita iv - moon 4"') == _names(conn, 'loc:"Jita IV - Moon 4"')


# --------------------------------------------------------------- injection
# The WHERE clause is assembled by string concatenation, which is worth being
# suspicious of. What gets concatenated is structure from closed sets:
#
#   expr  comes from queries.OVERVIEW_FILTER_EXPR, six fixed column
#         expressions, keyed by a loop over the fixed (*LEVEL_KINDS, "item")
#         rather than by anything a user typed
#   op    comes from _VAL_RE, an anchored ^(>=|<=|>|<) alternation, so it can
#         only ever be one of four literal strings
#   the rest is "?" placeholders and SQL keywords
#
# Every value a user can influence goes into params. These tests exist so that
# stays true: someone adding a filter kind could reasonably reach for an
# f-string, and this is what should stop them.
INJECTIONS = [
    """loc:"x'; DROP TABLE assets; --" """,
    """owner:"' OR 1=1 --" """,
    """cat:"' UNION SELECT name FROM sqlite_master --" """,
    "val:>1); DROP TABLE assets; --",
    "'; DELETE FROM assets; --",
    """group:"'||(SELECT value FROM meta)||'" """,
    """item:"'; UPDATE assets SET quantity=0; --" """,
]


@pytest.mark.parametrize("attack", INJECTIONS)
def test_a_hostile_filter_value_cannot_reach_the_sql(conn, attack):
    before = {r["item"] for r in conn.execute(queries.ASSET_ROWS)}

    spec = parse(attack)
    where, params = spec.where()
    sql = queries.ASSET_ROWS + (f" WHERE {where}" if where else "")
    rows = {r["item"] for r in conn.execute(sql, params)}

    # It must match nothing, rather than everything or something extra.
    assert not rows - before
    # And it must not have changed anything.
    assert {r["item"] for r in conn.execute(queries.ASSET_ROWS)} == before
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0] > 20, "tables are still there"


def test_the_value_is_a_parameter_and_never_part_of_the_clause():
    """The clause carries a placeholder; the value travels beside it."""
    spec = parse("""owner:"' OR 1=1 --" """)
    where, params = spec.where()

    assert "?" in where
    assert "OR 1=1" not in where
    assert params == ("' OR 1=1 --",)


def test_a_chip_kind_from_nowhere_cannot_choose_a_column():
    """Chips do not only come from parse -- a saved view can carry anything.
    The column expression is chosen by iterating fixed kinds, so an unknown
    kind selects no column rather than supplying one."""
    spec = FilterSpec(text="", chips=[Chip(kind="a.x FROM sqlite_master --", value="z")])
    where, params = spec.where()

    assert "sqlite_master" not in where
    assert params == ()


# ------------------------------------------------- unquoted multi-word values
# Most filter values have spaces in them: on a real account 469 of 472
# locations and 308 of 376 group names do. Requiring quotes for those made the
# common case the awkward one.
#
# The naive rule -- swallow words until the next prefix -- cannot be used: it
# would turn `owner:Main tritanium` into a search for an owner of that name
# and silently drop the text search. Resolving against the values that
# actually exist is what makes both readings possible.
VOCAB = {
    "owner": ["Test Pilot", "Main", "Main Fleet", "Jita Trader"],
    "system": ["Jita", "Amarr"],
    "region": ["The Forge"],
    "location": ["Jita IV - Moon 4 - Caldari Navy Assembly Plant"],
    "category": ["Ship", "Module"],
}


def _chips(query: str):
    return [(c.kind, c.value, c.negated) for c in parse(query, VOCAB).chips]


def test_an_unquoted_multi_word_value_is_matched():
    assert _chips("owner:Test Pilot") == [("owner", "Test Pilot", False)]
    assert parse("owner:Test Pilot", VOCAB).text == ""


def test_a_space_after_the_colon_is_allowed():
    spec = parse("owner: Test Pilot sys:Jita", VOCAB)
    assert [(c.kind, c.value) for c in spec.chips] == [
        ("owner", "Test Pilot"), ("system", "Jita")
    ]
    assert spec.text == ""


def test_a_following_chip_ends_the_value():
    """Otherwise one long value would swallow the rest of the query."""
    spec = parse("loc:Jita IV - Moon 4 - Caldari Navy Assembly Plant cat:Ship", VOCAB)
    assert [c.kind for c in spec.chips] == ["location", "category"]


def test_words_that_are_not_part_of_a_value_stay_a_search():
    """The case that rules out swallowing words until the next prefix."""
    spec = parse("owner:Main tritanium", VOCAB)
    assert [(c.kind, c.value) for c in spec.chips] == [("owner", "Main")]
    assert spec.text == "tritanium"


def test_the_longest_real_value_wins():
    """"Main" is a value and so is "Main Fleet"; typing the longer one must
    not be read as the shorter one plus a stray word."""
    assert _chips("owner:Main Fleet") == [("owner", "Main Fleet", False)]
    assert _chips("owner:Main") == [("owner", "Main", False)]


def test_matching_ignores_case_and_returns_the_stored_spelling():
    """The chip has to carry the value as stored: it is displayed, saved into
    views, and round-tripped through to_text()."""
    assert _chips("owner:test pilot") == [("owner", "Test Pilot", False)]
    assert _chips("REGION:the forge") == [("region", "The Forge", False)]


def test_negation_still_works_unquoted():
    assert _chips("-owner:Test Pilot") == [("owner", "Test Pilot", True)]


def test_an_unknown_value_behaves_as_it_always_did():
    """No vocabulary entry means no run-on. The first word becomes the value,
    matching nothing, and the rest stays a search -- which is what an
    unrecognised value did before any of this."""
    spec = parse("owner:Nobody Here tritanium", VOCAB)
    assert [(c.kind, c.value) for c in spec.chips] == [("owner", "Nobody")]
    assert spec.text == "Here tritanium"


def test_quoting_still_works_and_still_delimits():
    spec = parse('owner:"Test Pilot" tritanium', VOCAB)
    assert [(c.kind, c.value) for c in spec.chips] == [("owner", "Test Pilot")]
    assert spec.text == "tritanium"


def test_a_bare_prefix_is_still_the_half_typed_state():
    """`owner:` on its own is what the box holds mid-keystroke. It must stay
    bare text rather than becoming a chip matching everything or nothing."""
    spec = parse("owner:", VOCAB)
    assert spec.chips == []
    assert spec.text == "owner:"


def test_without_a_vocabulary_nothing_changes():
    """Every existing caller, and every saved view, parses without one."""
    spec = parse("owner:Test Pilot")
    assert [(c.kind, c.value) for c in spec.chips] == [("owner", "Test")]
    assert spec.text == "Pilot"


def test_a_quoted_word_is_never_absorbed_into_a_value():
    """Quotes are a deliberate delimiter in both directions."""
    spec = parse('owner:Main "Fleet"', VOCAB)
    assert [(c.kind, c.value) for c in spec.chips] == [("owner", "Main")]
    assert spec.text == "Fleet"
