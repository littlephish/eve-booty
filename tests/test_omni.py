"""Omnibox grammar and its SQL, exercised without a window."""

from __future__ import annotations

import random
import time

import fit_corpus as fc
import pytest

from evasset import db, fits, fitting, omni, queries
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


@pytest.fixture()
def aconn(conn):
    """The estate above plus the abyssal shapes: a fetched webifier (item
    11, rolls stored), an unfetched abyssal microwarpdrive (12), and the
    trap -- a stack of mutaplasmids (13) that shares meta group 15 with the
    real abyssals but is not a dynamic type. The web's speedFactor is a
    sign-inverted, as-stored percent; its duration is stored in
    milliseconds and displayed in seconds."""
    conn.executescript("""
      INSERT INTO sde_meta_groups VALUES (15,'Abyssal');
      INSERT INTO sde_groups VALUES (65,7,'Stasis Web',1),(46,7,'Propulsion Module',1),
        (1964,7,'Mutaplasmids',1);
      INSERT INTO sde_types (type_id,name,group_id,meta_group_id,volume,portion_size,published,
                             is_dynamic_type) VALUES
        (47702,'Abyssal Stasis Webifier',65,15,5,1,1,1),
        (526,'Stasis Webifier II',65,2,5,1,1,0),
        (47737,'Gravid Stasis Webifier Mutaplasmid',1964,15,1,1,1,0),
        (47408,'50MN Abyssal Microwarpdrive',46,15,10,1,1,1);
      INSERT INTO sde_dogma_attributes
        (attribute_id,name,display_name,unit_id,high_is_good,default_value,published) VALUES
        (20,'speedFactor','Maximum Velocity Bonus',124,1,0,1),
        (50,'cpu','CPU usage',106,0,0,1),
        (54,'maxRange','Optimal Range',1,1,0,1),
        (73,'duration','Activation time / duration',101,0,0,1);
      INSERT INTO sde_dogma_units VALUES (124,'Modifier Relative Percent','%'),
        (106,'Teraflops','tf'),(1,'Length','m'),(101,'Milliseconds','s');
      INSERT INTO sde_type_dogma VALUES (526,20,-60),(526,50,30),(526,54,10000),(526,73,5000);
      INSERT INTO sde_mutator_ranges VALUES (47737,20,0.9,1.1,0,47702),
        (47737,50,0.8,1.5,NULL,47702),(47737,54,0.8,1.2,NULL,47702);
      INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
                          location_flag,location_type,is_singleton,is_blueprint_copy,
                          custom_name,root_location_id,system_id,region_id) VALUES
        ('character',100,11,47702,1,60003760,'Hangar','station',1,0,NULL,
         60003760,30000142,10000002),
        ('character',100,12,47408,1,60003760,'Hangar','station',1,0,NULL,
         60003760,30000142,10000002),
        ('character',100,13,47737,5,60003760,'Hangar','station',0,0,NULL,
         60003760,30000142,10000002);
      INSERT INTO abyssal_items VALUES (11,47702,526,47737,42,'ok','2026-09-01T00:00:00+00:00');
      INSERT INTO abyssal_attributes VALUES (11,20,-63),(11,50,27),(11,54,11000),(11,73,5000);
    """)
    return conn


def _ids(conn, spec, exclude_level=None):
    where, params = spec.where(exclude_level=exclude_level)
    return {r["item_id"] for r in queries.fetch_assets(conn, where, params)}


ALL_ITEMS = set(range(1, 11))
ABYSSAL_ALL = ALL_ITEMS | {11, 12, 13}


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
        ("stat:cpu<30", omni.STAT_KIND),
        ("stat:cpu=18..22", omni.STAT_KIND),
        ("roll:web>=70", omni.ROLL_KIND),
        ("roll:cpu=60..90", omni.ROLL_KIND),
        ("abyssal", omni.ABYSSAL_KIND),
        ('abyssal:"Abyssal Stasis Webifier"', omni.ABYSSAL_KIND),
        ("is:abyssal", omni.ABYSSAL_KIND),
        ('holds:"Antimatter Charge M<500"', omni.HOLDS_KIND),
        ('holds:"cargo/Antimatter Charge M<500"', omni.HOLDS_KIND),
        ("fit:Ratting", omni.FIT_KIND),
        ("is:fit", "is"),
    ]
    seen = 0
    for token, kind in cases:
        spec = parse(token)
        assert spec.text == "", f"{token} leaked into bare text"
        assert [c.kind for c in spec.chips] == [kind]
        assert not spec.chips[0].negated
        seen += 1
    assert seen == len(cases) == 23


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


def _random_stat_value(rng, alphabet):
    """A stat:/roll: value the grammar accepts: one-sided, or an ordered `..` range."""
    name = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 8)))
    if not name.strip():
        name = "cpu"
    if rng.random() < 0.3:
        lo = rng.randint(-99, 999) + rng.choice([0, 0.5])
        hi = lo + rng.choice([0, 1, 12.5])
        return f"{name}={lo}..{hi}"
    return (name + rng.choice([">", "<", ">=", "<="])
            + rng.choice(["", "-"]) + str(rng.randint(0, 999))
            + rng.choice(["", ".5"]))


def test_round_trip_holds_for_generated_specs():
    """Property test over the whole constructible space: any level-chip,
    abyssal-chip or fit-chip value at all (quotes, backslashes, tabs,
    newlines, colons, commas, unicode, empty), valid
    is:/val:/stat:/roll:/holds: values including `..` ranges and every bay
    prefix, random negation, random bare words. Seeded so a failure
    reproduces exactly; the seed is arbitrary, not a date dependency."""
    rng = random.Random(682_431)
    level_alphabet = 'ab "\\\t\n-:é,'
    # A stat: name may carry spaces, quotes, backslashes, dashes and colons,
    # but not an operator character -- the name group excludes `<`, `>` and
    # `=` so that `a<b<3` has no parse at all -- and not a newline.
    stat_alphabet = 'ab "\\\t-:é'
    is_values = list(omni.IS_FLAGS)
    kinds = list(omni.LEVEL_KINDS) + ["is", "val", omni.STAT_KIND, omni.ROLL_KIND,
                                      omni.ABYSSAL_KIND, omni.HOLDS_KIND, omni.FIT_KIND]
    bays = [""] + [f"{bay}/" for bay in fitting.HOLD_BAYS]
    checked = command_free = 0
    stat_chips = ranges = abyssal_chips = 0
    holds_chips = bayed = fit_chips = 0
    for _ in range(400):
        chips = []
        for _ in range(rng.randint(0, 5)):
            kind = rng.choice(kinds)
            if kind == "is":
                value = rng.choice(is_values)
            elif kind == "val":
                value = (rng.choice([">", "<", ">=", "<="])
                         + str(rng.randint(1, 999))
                         + rng.choice(["", "k", "m", "b", "t"]))
            elif kind in (omni.STAT_KIND, omni.ROLL_KIND):
                value = _random_stat_value(rng, stat_alphabet)
                term = omni.parse_stat(value)
                assert term is not None, value
                stat_chips += 1
                ranges += term.op == ".."
            elif kind == omni.HOLDS_KIND:
                # A holds value is a stat comparison with an optional bay
                # prefix; the stat alphabet carries no "/", so a generated
                # name can never accidentally look like one.
                prefix = rng.choice(bays)
                value = prefix + _random_stat_value(rng, stat_alphabet)
                term = omni.parse_holds(value)
                assert term is not None, value
                holds_chips += 1
                bayed += term.bay is not None
            else:
                value = "".join(rng.choice(level_alphabet)
                                for _ in range(rng.randint(0, 8)))
                abyssal_chips += kind == omni.ABYSSAL_KIND
                fit_chips += kind == omni.FIT_KIND
            chips.append(Chip(kind=kind, value=value, negated=rng.random() < 0.5))
        words = ["".join(rng.choice("abcdefg-:") for _ in range(rng.randint(1, 6)))
                 for _ in range(rng.randint(0, 3))]
        spec = FilterSpec(text=" ".join(words), chips=chips)
        line = spec.to_text()
        assert parse(line) == spec, f"round-trip broke on {spec!r}"
        # A saved view is a to_text() line that the omnibox commits again on
        # recall, so if to_text could ever emit something extract_commands
        # reads as a command, loading a view would silently save or load
        # another one. Nothing generated here may look like a command, and
        # stripping commands from the line must leave the spec unchanged.
        remainder, commands = omni.extract_commands(line)
        assert commands == [], f"to_text emitted a command for {spec!r}"
        assert parse(remainder) == spec, f"stripping commands changed {spec!r}"
        command_free += 1
        checked += 1
    assert checked == 400
    assert command_free == 400, "the no-command proof must have run on every spec"
    assert stat_chips > 50, "the stat:/roll: branch must actually have been generated"
    assert ranges > 15, "and some of them must have been ranges"
    assert abyssal_chips > 30, "and the abyssal chip with arbitrary values"
    assert holds_chips > 30, "and the holds chip"
    assert bayed > 20, "with a bay prefix on some of them"
    assert fit_chips > 30, "and the fit chip with arbitrary names"


def test_save_and_load_are_still_plain_words_to_the_parser():
    """The commands are lifted out of the line before parse() ever sees it,
    so parse itself must stay exactly as unaware of them as it was of any
    other unknown prefix -- a chip kind would be persistable, and a command
    that could be saved into a view would run again on every recall."""
    spec = parse('save:Foo load:Bar save:"Jita ships"')
    assert spec.chips == []
    assert spec.text == 'save:Foo load:Bar save:"Jita ships"'
    assert all(c.kind not in omni.COMMAND_KINDS for c in parse("cat:Ship save:X").chips)


def test_extract_commands_lifts_the_command_and_leaves_the_filter_alone():
    """The one line a pilot types can carry both: `owner:Main save:"Jita
    ships"` means "chip these, then keep them under that name", so the
    remainder has to be exactly the filter half, quotes resolved on the
    command's value and untouched on everything else."""
    remainder, commands = omni.extract_commands('owner:Main save:"Jita ships" tritanium')
    assert remainder == "owner:Main tritanium"
    assert commands == [("save", "Jita ships")]
    assert parse(remainder) == parse("owner:Main tritanium")


def test_a_quoted_or_negated_command_is_a_search_and_not_a_command():
    """`"save:x"` is the escape hatch for searching that literal text, and
    `-save:x` negates a chip kind that does not exist. Neither may write to
    the library, and both must survive into the remainder verbatim so the
    parser can go on treating them as bare text."""
    for line in ('"save:x"', "-save:x", "-load:x", "save", "loading:x"):
        assert omni.extract_commands(line) == (line, []), line
    assert parse(omni.extract_commands('"save:x"')[0]).text == "save:x"


def test_a_command_inside_an_unbalanced_quote_is_not_executed():
    """The omnibox refuses to commit on unbalanced quotes, but the tokeniser
    is what actually decides: a `save:` swallowed into a half-typed quoted
    value is one token with a `holds` prefix, and no amount of the user
    keeping typing may turn it into a write."""
    line = 'holds:"cargo save:x'
    assert omni.extract_commands(line) == (line, [])


def test_an_empty_command_value_is_still_a_command():
    """`save:` alone is the request to open the Save card, so the empty value
    has to reach the caller as a command rather than falling back to text --
    the whole draft-builder empty-request path hangs off it."""
    assert omni.extract_commands("save:") == ("", [("save", "")])
    assert omni.extract_commands("load:") == ("", [("load", "")])
    assert omni.extract_commands('save:""') == ("", [("save", "")])


def test_commands_come_back_in_the_order_they_were_typed_and_case_folded():
    """Only the first is executed, but which one is first is the caller's
    decision to make, so the order is part of the contract; the prefix folds
    case like every other prefix in the grammar."""
    assert omni.extract_commands("LOAD:A cat:Ship Save:B") == (
        "cat:Ship", [("load", "A"), ("save", "B")]
    )
    assert omni.extract_commands("") == ("", [])
    assert omni.extract_commands("   ") == ("", [])


def test_the_remainder_is_re_joined_with_single_spaces():
    """Documented consequence of re-tokenising: runs of spaces between tokens
    collapse, exactly as parse() already collapses them, while spaces inside
    a quoted value are glued into the token and survive."""
    remainder, commands = omni.extract_commands('a    b  save:x  loc:"Jita  IV"')
    assert remainder == 'a b loc:"Jita  IV"'
    assert commands == [("save", "x")]
    assert parse(remainder).chips == [Chip(kind="location", value="Jita  IV")]


def test_a_searched_word_shaped_like_a_command_stays_a_word_through_to_text():
    """`"save:x"` quoted is someone searching for that literal text. to_text
    must write it back quoted: parse reads the same bare word either way, but
    a saved line is also what a user may paste or commit again by hand, and an
    unquoted save:x on that line would run the command and overwrite a view
    named x. Pinned so the wrap rule keeps covering commands."""
    spec = parse('"save:x"')
    assert spec.chips == [] and spec.text == "save:x"
    line = spec.to_text()
    assert line == '"save:x"'
    assert parse(line) == spec, "the round trip is intact"
    assert omni.extract_commands(line) == (line, []), (
        "a fresh commit of that line searches; it never saves"
    )


def test_prefix_for_kind_is_the_spelling_to_text_writes():
    """views.suggest_name labels a chip with this, and a label that said
    `location` where the grammar says `loc` would be a second vocabulary to
    keep in step with the first."""
    checked = 0
    for kind in omni.LEVEL_KINDS + (omni.ABYSSAL_KIND, omni.FIT_KIND, omni.HOLDS_KIND):
        line = FilterSpec(chips=[Chip(kind=kind, value="x")]).to_text()
        assert line == f"{omni.prefix_for_kind(kind)}:x", kind
        checked += 1
    assert checked == len(omni.LEVEL_KINDS) + 3
    assert omni.prefix_for_kind("location") == "loc"
    assert omni.prefix_for_kind("nonesuch") == "nonesuch", "an unknown kind is its own prefix"


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


def test_every_is_flag_and_its_negation_pick_the_seeded_rows(aconn):
    """is:abyssal is the type flag, so the unfetched microwarpdrive counts
    and the mutaplasmid stack -- meta group 15, is_dynamic_type 0 -- does
    not; the abyssals have no price row, so they are also unpriced.
    `is:abyssal` is an alias parse() turns into the abyssal chip rather
    than an is: flag, so it is walked here but is not in IS_FLAGS, whose
    length the last line pins.

    Each flag is walked with BOTH sets written out because is:fit is the one
    whose negation is not the complement: it is hull-scoped and ship-scoped,
    so -is:fit lists the deviating Dominix and says nothing at all about the
    Charon (no fit for its hull) or about the module fitted to a ship. Every
    other flag's negation is still pinned as the complement."""
    # Seeded here rather than in the fixture: the tests below assert exact id
    # sets and a new fixture row would move all of them. Item 14 is free, and
    # Tritanium at a station is priced and unfitted, so it lands only in the
    # delivery bucket and leaves every other expectation alone.
    aconn.execute(
        "INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,"
        "location_flag,location_type,is_singleton,is_blueprint_copy,custom_name,"
        "root_location_id,system_id,region_id) VALUES "
        "('character',100,14,34,25,60003760,'Deliveries','station',0,0,NULL,"
        "60003760,30000142,10000002)"
    )
    all_items = ABYSSAL_ALL | {14}
    text = "[Dominix, Gatling]\n125mm Gatling AutoCannon II"
    fits.save_fit(aconn, fits.parse_eft(aconn, text), "Gatling", text)

    expectations = {
        "fitted": ({3}, all_items - {3}),
        "safety": ({9}, all_items - {9}),
        "delivery": ({14}, all_items - {14}),
        "unpriced": ({7, 8, 11, 12, 13}, all_items - {7, 8, 11, 12, 13}),
        "bpc": ({7}, all_items - {7}),
        "abyssal": ({11, 12}, all_items - {11, 12}),
        # Ship 2 is the Dominix carrying that autocannon; ship 10 is the
        # empty one; ship 1 is a Charon, so no fit of its hull exists.
        "fit": ({2}, {10}),
    }
    seen = 0
    for flag, (expected, negated) in expectations.items():
        assert _ids(aconn, parse(f"is:{flag}")) == expected, f"is:{flag}"
        assert _ids(aconn, parse(f"-is:{flag}")) == negated, f"-is:{flag}"
        seen += 1
    # Derived rather than a literal, so adding a flag without an expectation
    # here fails the suite -- which is how is:delivery was caught. The +1 is
    # the abyssal alias, which parse() turns into its own chip kind and so is
    # deliberately not a member of IS_FLAGS.
    assert seen == len(omni.IS_FLAGS) + 1, "every flag and the alias must be exercised"
    # The alias is a chip kind of its own, not an is: flag, which is what the
    # +1 above accounts for.
    assert "abyssal" not in omni.IS_FLAGS
    assert "fit" in omni.IS_FLAGS


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
# ---------------------------------------------------------------- stat: chips
def test_stat_parses_name_operator_and_number_including_negatives_and_spaces():
    T = omni.StatTerm
    cases = [
        ("stat:cpu<30", T("cpu", "<", 30.0)),
        ("stat:cpu>=27.5", T("cpu", ">=", 27.5)),
        ('stat:"CPU usage"<30', T("CPU usage", "<", 30.0)),
        ('stat:"CPU usage <30"', T("CPU usage", "<", 30.0)),
        ("stat:speedFactor<-55", T("speedFactor", "<", -55.0)),
        ("stat:web<=-60", T("web", "<=", -60.0)),
        ('stat:"Optimal Range">10000', T("Optimal Range", ">", 10000.0)),
        ("stat:cpu=18..22", T("cpu", "..", 18.0, 22.0)),
        ("stat:cpu=18.5..22.25", T("cpu", "..", 18.5, 22.25)),
        ("stat:web=-66..-60", T("web", "..", -66.0, -60.0)),
        ('stat:"CPU usage = 18..22"', T("CPU usage", "..", 18.0, 22.0)),
        ("stat:cpu=30..30", T("cpu", "..", 30.0, 30.0)),
    ]
    seen = 0
    for token, expected in cases:
        spec = parse(token)
        assert spec.text == "" and len(spec.chips) == 1, token
        chip = spec.chips[0]
        assert chip.kind == omni.STAT_KIND
        assert omni.parse_stat(chip.value) == expected, token
        seen += 1
    assert seen == len(cases) == 12
    assert omni.parse_stat("cpu<30").high is None, "one-sided terms carry no high end"


def test_malformed_stat_degrades_to_bare_text():
    """No operator, no number, a blank name, two operators, a suffix the
    grammar does not have -- every one is the half-typed state, not a chip.
    `stat:a<b<3` once parsed as name `a<b` (the lazy name group backtracked
    over the first operator), and U+0663 is an Arabic-Indic three that
    `\\d` accepted and float() happily read as 3. The range half-forms are
    malformed too: `=` without `..` (float equality is not a question this
    grammar answers), `..` after an ordinary operator, a range with one end
    missing, a reversed range, and a third dot."""
    cases = ["stat:cpu", "stat:cpu<", "stat:<30", 'stat:" <30"', "stat:cpu<--5",
             "stat:cpu<30k", "stat:cpu=30", "stat:cpu<a", "stat:cpu<3e5", "stat:a<b<3",
             "stat:a<=b>3", "stat:cpu<\u0663",
             "stat:cpu>30..40", "stat:cpu=30..", "stat:cpu=..40", "stat:cpu=40..30",
             "stat:cpu=30...40", "stat:cpu=30..\u0663", "stat:cpu=30.-40", 'stat:"cpu=30 .. 40"',
             "roll:cpu=90..60", "roll:cpu=50", "roll:cpu<=60..90"]
    seen = 0
    for token in cases:
        spec = parse(token)
        assert spec.chips == [], token
        # Kept verbatim, quotes included: the token does not start with a
        # quote, so it is ordinary text the user is still typing.
        assert spec.text == token, token
        seen += 1
    assert seen == len(cases) == 23


def test_stat_round_trips_through_to_text_including_quotes_and_spaces():
    for raw in ['stat:"CPU usage"<30', "stat:cpu<30", "-stat:web<-55",
                'stat:"a\\"b<3"', 'stat:"cpu <30 "', "stat:cpu=18..22", "-roll:cpu=60..90",
                'roll:"Optimal Range"=50..100']:
        spec = parse(raw)
        assert spec.chips, raw
        assert parse(spec.to_text()) == spec, raw
    # A name containing a quote survives with the quote intact, as data.
    spec = parse('stat:"a\\"b<3"')
    assert omni.parse_stat(spec.chips[0].value) == omni.StatTerm('a"b', "<", 3.0)


def test_stat_ranges_are_inclusive_at_both_ends(aconn):
    """`stat:cpu=18..22` is a BETWEEN, and BETWEEN is closed: the web's 27 tf
    is inside 27..30 and inside 20..27, outside 27.01..30. Equal ends are a
    one-value band, not an error."""
    assert _ids(aconn, parse("stat:cpu=27..30")) == {11}
    assert _ids(aconn, parse("stat:cpu=20..27")) == {11}
    assert _ids(aconn, parse("stat:cpu=27.01..30")) == set()
    assert _ids(aconn, parse("stat:cpu=20..26.99")) == set()
    assert _ids(aconn, parse("stat:cpu=27..27")) == {11}
    assert _ids(aconn, parse("stat:web=-66..-60")) == {11}, "negative ends, ordered"
    assert _ids(aconn, parse("stat:duration=4.5..5.5")) == {11}, "display units, so seconds"
    assert _ids(aconn, parse("-stat:cpu=27..30")) == ABYSSAL_ALL - {11}
    where, params = parse("stat:cpu=18..22").where()
    assert "END BETWEEN ? AND ?" in where
    assert params == ("cpu", "cpu", "cpu", 18.0, 22.0)


def test_stat_compares_display_values_of_stored_rolls(aconn):
    """duration is stored as 5000 ms and shown as 5 s, speedFactor is a
    signed as-stored percent, cpu is raw teraflops: the number typed must
    be the number displayed in every case. Display names match too, case-
    insensitively, and the unfetched microwarpdrive never matches."""
    assert _ids(aconn, parse("stat:duration<9")) == {11}
    assert _ids(aconn, parse("stat:duration<4")) == set()
    assert _ids(aconn, parse("stat:speedFactor<-55")) == {11}
    assert _ids(aconn, parse("stat:speedFactor>-55")) == set()
    assert _ids(aconn, parse("stat:cpu<30")) == {11}
    assert _ids(aconn, parse("stat:cpu<=27")) == {11}
    assert _ids(aconn, parse("stat:cpu<27")) == set()
    assert _ids(aconn, parse('stat:"CPU usage"<30')) == {11}
    assert _ids(aconn, parse('stat:"cpu USAGE"<30')) == {11}
    assert _ids(aconn, parse("stat:CPU<30")) == {11}
    assert _ids(aconn, parse('stat:"Optimal Range">=11000')) == {11}
    assert _ids(aconn, parse("stat:maxRange>11000")) == set()
    # Two stat chips AND together, so a pair on one attribute is a band.
    assert _ids(aconn, parse("stat:cpu>20 stat:cpu<30")) == {11}
    assert _ids(aconn, parse("stat:cpu>20 stat:cpu<25")) == set()
    assert _ids(aconn, parse("stat:cpu<30 stat:duration<9")) == {11}


def test_negated_stat_keeps_items_without_stored_stats(aconn):
    """-stat:cpu<30 means "not known to have a cpu under 30": the unfetched
    microwarpdrive, the mutaplasmid stack and every ordinary item survive.
    A negated comparison inside the EXISTS would drop all of them."""
    assert _ids(aconn, parse("-stat:cpu<30")) == ABYSSAL_ALL - {11}
    assert _ids(aconn, parse("-stat:cpu<20")) == ABYSSAL_ALL
    where, _params = parse("-stat:cpu<30").where()
    assert where.startswith("NOT EXISTS")


def test_stat_aliases_resolve_to_the_internal_attribute_name(aconn):
    assert _ids(aconn, parse("stat:web<-55")) == {11}
    assert _ids(aconn, parse("stat:range>=11000")) == {11}
    assert _ids(aconn, parse("stat:CPU<30")) == {11}, "an alias is case-insensitive too"
    where, params = parse("stat:web<-55").where()
    assert params == ("speedFactor", "speedFactor", "speedFactor", -55.0)
    assert "web" not in where
    # The alias table itself maps to names the SDE actually uses.
    from evasset import abyssal
    for alias in ("cpu", "pg", "power", "cap", "range", "falloff", "rof", "web", "damage",
                  "hp", "speed"):
        assert alias in abyssal.STAT_ALIASES
    assert abyssal.STAT_ALIASES["web"] == "speedFactor"
    assert abyssal.STAT_ALIASES["rof"] == "speedMultiplier"
    assert abyssal.STAT_ALIASES["pg"] == "power"


def test_stat_values_are_bound_never_interpolated(aconn):
    """The name and number are user input; only the whitelisted operator
    reaches the SQL text. A hostile name is a parameter that matches
    nothing -- and does not error. The classic `' OR '1'='1` cannot even
    be used here because the name group rejects `=`; this one carries no
    operator character and so does reach the SQL, as a parameter."""
    hostile = "x' OR 'a' IS NOT NULL --"
    spec = parse(f'stat:"{hostile}<30"')
    assert spec.chips and spec.chips[0].kind == omni.STAT_KIND
    where, params = spec.where()
    assert hostile not in where
    assert params == (hostile, hostile, hostile, 30.0)
    assert _ids(aconn, spec) == set()
    assert parse('stat:"x\' OR \'1\'=\'1<30"').chips == [], "an = in the name is not a stat chip"
    hand_built = FilterSpec(text="", chips=[Chip(kind=omni.STAT_KIND, value="cpu<30; DROP TABLE assets")])
    where, params = hand_built.where()
    assert where == "" and params == (), "an untranslatable stat chip is skipped, not guessed"
    assert _ids(aconn, hand_built) == ABYSSAL_ALL
    for op in (">", "<", ">=", "<="):
        where, _ = parse(f"stat:cpu{op}30").where()
        assert f"END {op} ?" in where
        assert f"END {op}= ?" not in where


def test_an_internal_name_is_matched_before_a_shared_display_name(aconn):
    """Real SDE shape (build 3487903): 554 signatureRadiusBonus, a percent,
    and 983 signatureRadiusAdd, metres, both display "Signature Radius
    Modifier". The display name any-matches, as documented, but an internal
    name must reach its own attribute only -- widening
    stat:signatureRadiusAdd>20 to the percent namesake would answer a
    question about metres with a percentage. The decoy attribute, whose
    DISPLAY name is another attribute's internal name, proves the order:
    a display name is consulted only when nothing carries that internal
    name, so the web's real 27 tf cpu wins over the decoy's 999."""
    aconn.executescript("""
      INSERT INTO sde_dogma_attributes
        (attribute_id,name,display_name,unit_id,high_is_good,default_value,published) VALUES
        (554,'signatureRadiusBonus','Signature Radius Modifier',124,0,0,1),
        (983,'signatureRadiusAdd','Signature Radius Modifier',1,0,0,1),
        (9001,'decoy','cpu',106,0,0,1);
      INSERT INTO abyssal_attributes VALUES (11,554,25),(11,983,15),(11,9001,999);
    """)
    assert _ids(aconn, parse('stat:"Signature Radius Modifier">20')) == {11}, "the percent one"
    assert _ids(aconn, parse('stat:"Signature Radius Modifier"<20')) == {11}, "the metres one"
    assert _ids(aconn, parse('stat:"Signature Radius Modifier">30')) == set()
    assert _ids(aconn, parse("stat:signatureRadiusBonus>20")) == {11}
    assert _ids(aconn, parse("stat:signatureRadiusAdd>20")) == set()
    assert _ids(aconn, parse("stat:signatureRadiusAdd>10")) == {11}
    assert _ids(aconn, parse("stat:SIGNATURERADIUSADD>10")) == {11}, "internal names are NOCASE too"
    assert _ids(aconn, parse("stat:sig>20")) == {11}, "the alias names the percent one"
    assert _ids(aconn, parse("stat:cpu>100")) == set(), "the decoy's display name does not match"
    assert _ids(aconn, parse("stat:cpu<30")) == {11}
    assert _ids(aconn, parse("stat:decoy>100")) == {11}


# ------------------------------------------------------------- abyssal chips
def test_bare_abyssal_word_mints_the_chip_in_any_case_and_polarity():
    """`abyssal` is the one bare word that is a chip: the settled gesture is
    "type abyssal, get the Abyssal chip". Case-insensitive like every other
    prefix, and a leading minus negates it like every other chip."""
    for raw in ("abyssal", "Abyssal", "ABYSSAL"):
        assert parse(raw).chips == [Chip(omni.ABYSSAL_KIND, "")], raw
        assert parse(raw).text == ""
    assert parse("-abyssal").chips == [Chip(omni.ABYSSAL_KIND, "", negated=True)]
    # Half-typed shapes stay text: a prefix with nothing after the colon,
    # and the word with more letters.
    assert parse("abyssal:").chips == [] and parse("abyssal:").text == "abyssal:"
    assert parse("abyssals").chips == [] and parse("abyssals").text == "abyssals"


def test_abyssal_value_carries_type_names_that_split_and_join():
    spec = parse('abyssal:"Abyssal Stasis Webifier, Abyssal Warp Disruptor"')
    assert spec.chips == [
        Chip(omni.ABYSSAL_KIND, "Abyssal Stasis Webifier, Abyssal Warp Disruptor")
    ]
    assert omni.split_types(spec.chips[0].value) == [
        "Abyssal Stasis Webifier", "Abyssal Warp Disruptor",
    ]
    assert omni.join_types(["Abyssal Stasis Webifier", "Abyssal Warp Disruptor"]) == (
        "Abyssal Stasis Webifier, Abyssal Warp Disruptor"
    )
    # split forgives what a hand-typed value looks like; join canonicalises.
    assert omni.split_types("A,B") == ["A", "B"]
    assert omni.split_types(" A ,  B , ") == ["A", "B"]
    assert omni.split_types("") == []
    assert omni.split_types(", ,") == [], "a value of only separators is every type"
    assert omni.join_types([]) == ""
    assert omni.join_types([" A ", "", "B"]) == "A, B"
    names = ["Abyssal Stasis Webifier", "50MN Abyssal Microwarpdrive"]
    assert omni.split_types(omni.join_types(names)) == names


def test_is_abyssal_is_an_alias_for_the_abyssal_chip():
    """The flag predates the chip and is in saved views and muscle memory,
    so it keeps parsing -- to the chip, both polarities. It is not an is:
    flag: the draft builder offers the abyssal kind itself."""
    assert parse("is:abyssal").chips == [Chip(omni.ABYSSAL_KIND, "")]
    assert parse("-is:abyssal").chips == [Chip(omni.ABYSSAL_KIND, "", negated=True)]
    assert parse("is:ABYSSAL").chips == [Chip(omni.ABYSSAL_KIND, "")]
    assert "abyssal" not in omni.IS_FLAGS
    assert parse("is:abyssal") == parse("abyssal") == parse('abyssal:""')


def test_abyssal_chip_serialises_as_the_bare_word_or_a_quoted_value():
    assert FilterSpec(chips=[Chip(omni.ABYSSAL_KIND, "")]).to_text() == "abyssal"
    assert FilterSpec(chips=[Chip(omni.ABYSSAL_KIND, "", negated=True)]).to_text() == "-abyssal"
    two = FilterSpec(chips=[Chip(omni.ABYSSAL_KIND, "Abyssal Stasis Webifier, Abyssal Warp Disruptor")])
    assert two.to_text() == 'abyssal:"Abyssal Stasis Webifier, Abyssal Warp Disruptor"'
    for raw in ("abyssal", "-abyssal", 'abyssal:"A, B"', "-abyssal:A", "is:abyssal",
                'abyssal:"say \\"hi\\", B"'):
        spec = parse(raw)
        assert spec.chips, raw
        assert parse(spec.to_text()) == spec, raw
    # Someone searching for the literal word gets it quoted, and it comes
    # back as text rather than as the chip.
    word = FilterSpec(text="abyssal")
    assert word.to_text() == '"abyssal"'
    assert parse(word.to_text()) == word


def test_abyssal_chip_filters_dynamic_types_and_narrows_to_named_ones(aconn):
    """The empty chip is the type flag (unfetched items count, the
    mutaplasmid stack does not); named types narrow it, several OR, and a
    type nobody owns filters to nothing rather than to everything."""
    assert _ids(aconn, parse("abyssal")) == {11, 12}
    assert _ids(aconn, parse('abyssal:"Abyssal Stasis Webifier"')) == {11}
    assert _ids(aconn, parse('abyssal:"50MN Abyssal Microwarpdrive"')) == {12}
    both = 'abyssal:"Abyssal Stasis Webifier, 50MN Abyssal Microwarpdrive"'
    assert _ids(aconn, parse(both)) == {11, 12}
    assert _ids(aconn, parse('abyssal:"Abyssal Warp Disruptor"')) == set()
    assert _ids(aconn, parse('abyssal:"Stasis Webifier II"')) == set(), "a named non-dynamic type"
    assert _ids(aconn, parse('abyssal:"Gravid Stasis Webifier Mutaplasmid"')) == set()
    # Negation: the whole clause, so -abyssal:X keeps the other abyssals.
    assert _ids(aconn, parse("-abyssal")) == ABYSSAL_ALL - {11, 12}
    assert _ids(aconn, parse("-is:abyssal")) == ABYSSAL_ALL - {11, 12}
    assert _ids(aconn, parse('-abyssal:"Abyssal Stasis Webifier"')) == ABYSSAL_ALL - {11}
    # Two positive chips are the union of their lists; the empty chip is
    # already every type and absorbs a narrower one.
    assert _ids(aconn, parse(
        'abyssal:"Abyssal Stasis Webifier" abyssal:"50MN Abyssal Microwarpdrive"'
    )) == {11, 12}
    assert _ids(aconn, parse('abyssal abyssal:"Abyssal Stasis Webifier"')) == {11, 12}
    where, params = parse('abyssal abyssal:"Abyssal Stasis Webifier"').where()
    assert where == "t.is_dynamic_type = 1" and params == ()
    # Composes with the other kinds by AND.
    assert _ids(aconn, parse("abyssal stat:cpu<30")) == {11}
    assert _ids(aconn, parse('abyssal owner:"Jita Trader"')) == set()


def test_abyssal_type_names_are_bound_parameters_one_per_type(aconn):
    hostile = "x') OR 1=1 --"
    spec = parse(f'abyssal:"{hostile}, Abyssal Stasis Webifier"')
    where, params = spec.where()
    assert hostile not in where
    assert params == (hostile, "Abyssal Stasis Webifier")
    assert where == "(t.is_dynamic_type = 1 AND t.name IN (?,?))"
    assert _ids(aconn, spec) == {11}
    # Duplicates across chips collapse to one mark each.
    where, params = parse('abyssal:A abyssal:"A, B" abyssal:B').where()
    assert params == ("A", "B")
    where, params = parse('-abyssal:"A, B"').where()
    assert where == "NOT (t.is_dynamic_type = 1 AND t.name IN (?,?))"
    assert params == ("A", "B")


def test_empty_and_separator_only_abyssal_values_mean_every_type(aconn):
    """`abyssal:""` and `abyssal:", ,"` both parse (the quotes signal intent)
    and both filter as the bare chip; the second keeps its odd value so the
    round-trip holds, and split_types reads it as no types."""
    for raw in ('abyssal:""', 'abyssal:", ,"', 'abyssal:" "'):
        spec = parse(raw)
        assert len(spec.chips) == 1 and spec.chips[0].kind == omni.ABYSSAL_KIND, raw
        assert _ids(aconn, spec) == {11, 12}, raw
        assert spec.where() == ("t.is_dynamic_type = 1", ()), raw
        assert parse(spec.to_text()) == spec, raw


def test_exclude_level_abyssal_drops_the_chip_so_type_counts_can_facet(aconn):
    """The card's type picker lists types faceted by every filter except the
    abyssal chip itself, the way the rail excludes its own level; the view
    drops the card's stat:/roll: chips too, pinned by
    test_exclude_kinds_drops_every_card_kind_both_polarities."""
    spec = parse('abyssal:"Abyssal Stasis Webifier" stat:cpu<30')
    assert _ids(aconn, spec) == {11}
    assert _ids(aconn, spec, exclude_level=omni.ABYSSAL_KIND) == {11}
    spec = parse('abyssal:"50MN Abyssal Microwarpdrive" stat:cpu<30')
    assert _ids(aconn, spec) == set()
    assert _ids(aconn, spec, exclude_level=omni.ABYSSAL_KIND) == {11}
    where, params = parse('-abyssal abyssal:X is:bpc').where(exclude_level=omni.ABYSSAL_KIND)
    assert "is_dynamic_type" not in where and params == ()


def test_exclude_kinds_drops_every_card_kind_both_polarities(aconn):
    """The card rewrites the abyssal, roll: and stat: chips on Done, so its
    picker facets by everything else: with the unfetched microwarpdrive
    picked and a web roll in force nothing matches, yet dropping the card's
    kinds -- the negated stat: chip too -- leaves the whole estate for the
    picker to count. exclude_kinds composes with exclude_level, and an
    empty exclude_kinds is the plain where()."""
    card_kinds = (omni.ABYSSAL_KIND, omni.ROLL_KIND, omni.STAT_KIND)
    spec = parse('abyssal:"50MN Abyssal Microwarpdrive" roll:web>=70 -stat:cpu<30')
    assert _ids(aconn, spec) == set()
    where, params = spec.where(exclude_kinds=card_kinds)
    assert (where, params) == ("", ())
    assert {r["item_id"] for r in queries.fetch_assets(aconn, where, params)} == ABYSSAL_ALL
    # Only the roll: and stat: chips dropped: the abyssal chip still applies.
    where, params = spec.where(exclude_kinds=(omni.ROLL_KIND, omni.STAT_KIND))
    assert {r["item_id"] for r in queries.fetch_assets(aconn, where, params)} == {12}
    # Composed with exclude_level: the negated stat: chip is the one left.
    where, params = spec.where(exclude_level=omni.ABYSSAL_KIND, exclude_kinds=(omni.ROLL_KIND,))
    assert {r["item_id"] for r in queries.fetch_assets(aconn, where, params)} == ABYSSAL_ALL - {11}
    assert spec.where(exclude_kinds=()) == spec.where()
    # A kind outside the card's set is untouched by it.
    spec = parse('abyssal roll:web>=70 owner:"Test Pilot"')
    where, params = spec.where(exclude_kinds=card_kinds)
    assert "owner" in where.lower() and params == ("Test Pilot",)


# ---------------------------------------------------------------- roll: chips
# The seeded web (item 11): speedFactor -63 against a -60 base rolled
# 0.9x..1.1x with CCP's low-is-good override -> 75%; cpu 27 against 30
# rolled 0.8x..1.5x, low-is-good -> 1 - 3/21 = 85.7%; maxRange 11000 against
# 10000 rolled 0.8x..1.2x -> 75%. duration is stored but not rolled.
def test_roll_compares_mirrored_quality_percent_of_rolled_attributes(aconn):
    assert _ids(aconn, parse("roll:web>=70")) == {11}
    assert _ids(aconn, parse("roll:web>=76")) == set()
    assert _ids(aconn, parse("roll:web<75")) == set()
    assert _ids(aconn, parse("roll:web<=75")) == {11}
    assert _ids(aconn, parse("roll:speedFactor>=70")) == {11}, "internal name"
    assert _ids(aconn, parse('roll:"Maximum Velocity Bonus">=70')) == {11}, "display name"
    assert _ids(aconn, parse("roll:cpu>85")) == {11}
    assert _ids(aconn, parse("roll:cpu>86")) == set()
    assert _ids(aconn, parse("roll:cpu<30")) == set(), "quality, not the 27 tf value"
    assert _ids(aconn, parse("roll:range>=75")) == {11}
    # Stored but not in the mutaplasmid's set: no quality, so never matched
    # by roll: while stat: still sees the value.
    assert _ids(aconn, parse("roll:duration<100")) == set()
    assert _ids(aconn, parse("roll:duration>=0")) == set()
    assert _ids(aconn, parse("stat:duration<9")) == {11}
    # Several roll chips AND, and the chip composes with the abyssal chip.
    assert _ids(aconn, parse("roll:web>=70 roll:cpu>=80")) == {11}
    assert _ids(aconn, parse("roll:web>=70 roll:cpu>=90")) == set()
    assert _ids(aconn, parse('abyssal:"Abyssal Stasis Webifier" roll:web>=70')) == {11}
    assert _ids(aconn, parse('abyssal:"50MN Abyssal Microwarpdrive" roll:web>=70')) == set()


def test_roll_ranges_are_inclusive_at_both_ends(aconn):
    """maxRange 11000 in 8000..12000 is exactly 0.75, so 75 is an exact
    percent and both ends of a BETWEEN can be pinned without tolerance."""
    assert _ids(aconn, parse("roll:range=75..80")) == {11}
    assert _ids(aconn, parse("roll:range=70..75")) == {11}
    assert _ids(aconn, parse("roll:range=75..75")) == {11}
    assert _ids(aconn, parse("roll:range=75.01..80")) == set()
    assert _ids(aconn, parse("roll:range=70..74.99")) == set()
    assert _ids(aconn, parse("roll:cpu=60..90")) == {11}
    assert _ids(aconn, parse("roll:cpu=60..85")) == set()
    where, params = parse("roll:cpu=60..90").where()
    assert where.rstrip().endswith("END BETWEEN ? AND ?\n)")
    assert params == ("cpu", "cpu", "cpu", 60.0, 90.0)


def test_a_roll_of_exactly_fifty_sits_on_the_closed_side_of_every_operator(aconn):
    """cpu 34.5 in 24..45 is exactly halfway ((34.5 - 24) / 21 = 0.5, exact in
    binary), low-is-good, so the quality is exactly 50: >= and <= include
    it, > and < do not, and the one-value band finds it."""
    aconn.execute(
        "UPDATE abyssal_attributes SET value = 34.5 WHERE item_id = 11 AND attribute_id = 50"
    )
    assert _ids(aconn, parse("roll:cpu>=50")) == {11}
    assert _ids(aconn, parse("roll:cpu<=50")) == {11}
    assert _ids(aconn, parse("roll:cpu>50")) == set()
    assert _ids(aconn, parse("roll:cpu<50")) == set()
    assert _ids(aconn, parse("roll:cpu=50..50")) == {11}


def test_negated_roll_keeps_items_without_fetched_rolls(aconn):
    """-roll:web>=70 means "not known to roll web at 70 or better": the
    unfetched microwarpdrive, the mutaplasmid stack and every ordinary item
    survive, and so does the web once the bar is above its roll."""
    assert _ids(aconn, parse("-roll:web>=70")) == ABYSSAL_ALL - {11}
    assert _ids(aconn, parse("-roll:web>=90")) == ABYSSAL_ALL
    assert _ids(aconn, parse("-roll:cpu=60..90")) == ABYSSAL_ALL - {11}
    where, _params = parse("-roll:web>=70").where()
    assert where.startswith("NOT EXISTS")


def test_roll_names_and_numbers_are_bound_and_only_the_operator_reaches_the_sql(aconn):
    hostile = "x' OR 'a' IS NOT NULL --"
    spec = parse(f'roll:"{hostile}>=70"')
    assert spec.chips and spec.chips[0].kind == omni.ROLL_KIND
    where, params = spec.where()
    assert hostile not in where
    assert params == (hostile, hostile, hostile, 70.0)
    assert _ids(aconn, spec) == set()
    where, params = parse("roll:web>=70").where()
    assert params == ("speedFactor", "speedFactor", "speedFactor", 70.0), "alias resolved"
    assert "web" not in where
    assert "i.status = 'ok'" in where
    for op in (">", "<", ">=", "<="):
        where, _ = parse(f"roll:cpu{op}30").where()
        assert f"END {op} ?" in where
        assert f"END {op}= ?" not in where
    hand_built = FilterSpec(chips=[Chip(omni.ROLL_KIND, "cpu>=30; DROP TABLE assets")])
    assert hand_built.where() == ("", ()), "an untranslatable roll chip is skipped, not guessed"


def test_roll_clause_probes_every_abyssal_table_by_key_and_scans_none(aconn):
    """The clause is correlated per asset row, so a table scan inside it
    would be a scan per row -- 25k rows times every attribute row in the
    estate. Every abyssal join must resolve by primary key from the item's
    own abyssal_items row. The one SCAN allowed is the uncorrelated
    display-name subselect over sde_dogma_attributes (alias x), which
    SQLite evaluates once per statement."""
    where, params = parse("roll:web>=70").where()
    plan = [r[3] for r in aconn.execute(
        "EXPLAIN QUERY PLAN " + queries.ASSET_ROWS + f" WHERE {where}", params
    )]
    scans = [line for line in plan if line.startswith("SCAN")]
    # The clause being correlated is the design -- what must not happen is a
    # *scan* inside it. Allowed here: the asset table itself (a), the
    # uncorrelated display-name subselect (x), and the container_walk CTE with
    # its window-function subquery (w, container_walk, subquery-N), which
    # SQLite builds once and then searches by key.
    allowed = ("SCAN a", "SCAN x", "SCAN w", "SCAN container_walk", "SCAN (subquery-")
    assert all(line.startswith(allowed) for line in scans), scans
    assert not any("abyssal_attributes" in line for line in scans)
    assert any(line.startswith("SEARCH sa USING") for line in plan), plan
    assert any(line.startswith("SEARCH i USING INTEGER PRIMARY KEY") for line in plan), plan
    assert any(line.startswith("SEARCH mr USING") for line in plan), plan
    assert any(line.startswith("SEARCH td USING") for line in plan), plan
    where, params = parse("stat:cpu<30").where()
    plan = [r[3] for r in aconn.execute(
        "EXPLAIN QUERY PLAN " + queries.ASSET_ROWS + f" WHERE {where}", params
    )]
    assert not any(line.startswith("SCAN") and "sa" in line.split() for line in plan), plan


@pytest.fixture()
def big_conn(tmp_path):
    """25,000 fetched webifiers with four attributes each -- fifty times a
    large estate -- built in one transaction (about a quarter of a second)."""
    c = db.init(tmp_path / "big.sqlite")
    c.executescript("""
      INSERT INTO sde_categories VALUES (7,'Module',1);
      INSERT INTO sde_groups VALUES (65,7,'Stasis Web',1);
      INSERT INTO sde_meta_groups VALUES (15,'Abyssal'),(2,'Tech II');
      INSERT INTO sde_types (type_id,name,group_id,meta_group_id,volume,portion_size,published,
                             is_dynamic_type) VALUES
        (47702,'Abyssal Stasis Webifier',65,15,5,1,1,1),
        (526,'Stasis Webifier II',65,2,5,1,1,0);
      INSERT INTO sde_dogma_attributes
        (attribute_id,name,display_name,unit_id,high_is_good,default_value,published) VALUES
        (20,'speedFactor','Maximum Velocity Bonus',124,1,0,1),
        (50,'cpu','CPU usage',106,0,0,1),
        (54,'maxRange','Optimal Range',1,1,0,1),
        (73,'duration','Activation time / duration',101,0,0,1);
      INSERT INTO sde_type_dogma VALUES (526,20,-60),(526,50,30),(526,54,10000),(526,73,5000);
      INSERT INTO sde_mutator_ranges VALUES (47737,20,0.9,1.1,0,47702),
        (47737,50,0.8,1.5,NULL,47702),(47737,54,0.8,1.2,NULL,47702);
      INSERT INTO characters (character_id,name,corporation_id,scopes,enabled) VALUES
        (100,'Test Pilot',2000,'s',1);
    """)
    n = 25_000
    rng = random.Random(7)
    with db.transaction(c):
        c.executemany(
            "INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,"
            "location_flag,location_type,is_singleton,is_blueprint_copy,custom_name,"
            "root_location_id,system_id,region_id) VALUES ('character',100,?,47702,1,60003760,"
            "'Hangar','station',1,0,NULL,60003760,30000142,10000002)",
            [(i,) for i in range(1, n + 1)],
        )
        c.executemany(
            "INSERT INTO abyssal_items VALUES (?,47702,526,47737,42,'ok','2026-09-01T00:00:00+00:00')",
            [(i,) for i in range(1, n + 1)],
        )
        rows = []
        for i in range(1, n + 1):
            rows.append((i, 20, -60 * rng.uniform(0.9, 1.1)))
            rows.append((i, 50, 30 * rng.uniform(0.8, 1.5)))
            rows.append((i, 54, 10000 * rng.uniform(0.8, 1.2)))
            rows.append((i, 73, 5000))
        c.executemany("INSERT INTO abyssal_attributes VALUES (?,?,?)", rows)
    return c


def test_one_roll_clause_costs_no_more_than_the_fetch_it_narrows(big_conn):
    """The clause is measured on a lean base (assets joined to types) so
    that ASSET_ROWS' eleven other joins do not hide the number, and judged
    against the unfiltered fetch timed in the same process rather than a
    fixed wall-clock figure: about 45 ms against a 100 ms ceiling is only
    twice the headroom, which a loaded CI runner eats, while both numbers
    move together under load. Best of five so a scheduler hiccup does not
    fail the build."""
    where, params = parse("abyssal roll:web>=70").where()
    sql = f"SELECT COUNT(*) FROM assets a JOIN sde_types t ON t.type_id = a.type_id WHERE {where}"

    def best_of(fn, n=5):
        best = float("inf")
        for _ in range(n):
            start = time.perf_counter()
            fn()
            best = min(best, time.perf_counter() - start)
        return best

    reference = best_of(lambda: queries.fetch_assets(big_conn))
    best = best_of(lambda: big_conn.execute(sql, params).fetchone()[0])
    matched = big_conn.execute(sql, params).fetchone()[0]
    assert 0 < matched < 25_000, "the filter must have had something to do"
    assert best < max(0.100, 1.5 * reference), (
        f"roll: clause {best * 1000:.1f} ms vs unfiltered fetch {reference * 1000:.1f} ms"
    )


# ---------------------------------------------------------------- holds: chips
def test_holds_parses_a_bay_prefix_a_name_an_operator_and_a_number():
    T = omni.HoldsTerm
    cases = [
        ('holds:"Antimatter Charge M<500"', T(None, "Antimatter Charge M", "<", 500.0)),
        ('holds:"cargo/Antimatter Charge M<500"', T("cargo", "Antimatter Charge M", "<", 500.0)),
        ('holds:"fuel/Antimatter Charge M>=200"', T("fuel", "Antimatter Charge M", ">=", 200.0)),
        ('holds:"drones/Hobgoblin II>0"', T("drones", "Hobgoblin II", ">", 0.0)),
        ('holds:"CARGO/Tritanium<=5"', T("cargo", "Tritanium", "<=", 5.0)),
        ('holds:"Tritanium=100..200"', T(None, "Tritanium", "..", 100.0, 200.0)),
        ('holds:"fuel/Nitrogen Isotopes=0..0"', T("fuel", "Nitrogen Isotopes", "..", 0.0, 0.0)),
        # An unrecognised prefix stays part of the name, forgivingly: a
        # half-typed bay must behave like any name the estate does not hold
        # rather than silently counting the whole ship instead.
        ('holds:"ammo/Tritanium<5"', T(None, "ammo/Tritanium", "<", 5.0)),
        ('holds:"cargo/fuel/Tritanium<5"', T("cargo", "fuel/Tritanium", "<", 5.0)),
        ('holds:"fighters/Templar II>=1"', T("fighters", "Templar II", ">=", 1.0)),
        ('holds:"FLEET/Nanite Repair Paste>=1"', T("fleet", "Nanite Repair Paste", ">=", 1.0)),
        # The slash belongs to the prefix: a name that merely starts with a
        # bay's word keeps every letter, and a bay-like word without the
        # slash is a name too.
        ('holds:"Fleeting Compact Stasis Webifier<1"',
         T(None, "Fleeting Compact Stasis Webifier", "<", 1.0)),
        ('holds:"fleets/Tritanium<5"', T(None, "fleets/Tritanium", "<", 5.0)),
    ]
    seen = 0
    for token, expected in cases:
        spec = parse(token)
        assert spec.text == "" and len(spec.chips) == 1, token
        assert spec.chips[0].kind == omni.HOLDS_KIND
        assert omni.parse_holds(spec.chips[0].value) == expected, token
        seen += 1
    assert seen == len(cases) == 13
    assert list(fitting.HOLD_BAYS) == ["cargo", "fuel", "drones", "fighters", "fleet"], (
        "the prefixes come from here"
    )
    # The spelling the README teaches quotes the NAME and leaves the
    # comparison outside; quotes glue rather than delimit, so it is the same
    # chip, and to_text re-serialises it with the whole value quoted.
    typed = parse('holds:"Antimatter Charge M"<500')
    assert typed == parse('holds:"Antimatter Charge M<500"')
    assert typed.to_text() == 'holds:"Antimatter Charge M<500"'
    assert parse(typed.to_text()) == typed
    assert parse('holds:"cargo/Antimatter Charge M"<500') == parse(
        'holds:"cargo/Antimatter Charge M<500"'
    )


def test_a_malformed_holds_value_degrades_to_bare_text():
    """The stat: rule: a comparison that does not parse is a half-typed
    token, not a chip filtering on nothing. Every shape parse_stat refuses
    is refused here too, prefix or no prefix."""
    cases = ["holds:Tritanium", "holds:Tritanium<", 'holds:"<500"', "holds:cargo/",
             'holds:"cargo/<500"', "holds:Tritanium=500", "holds:Tritanium<500k",
             'holds:"Tritanium=200..100"', "holds:", 'holds:"cargo/Tritanium"']
    seen = 0
    for token in cases:
        spec = parse(token)
        assert spec.chips == [], token
        assert spec.text == token, token
        seen += 1
    assert seen == len(cases) == 10
    assert omni.parse_holds("cargo/") is None


def test_an_unquoted_multi_word_holds_or_fit_value_stays_text_like_stat_does():
    """Which way the unquoted-value rule falls for the two ship chips, pinned
    because it could plausibly fall either way.

    `owner:Test Pilot` works without quotes by matching the run of words
    against the owners that exist, and neither of these kinds has such a
    vocabulary to match against: a holds value carries an operator and a
    number the SDE has never heard of, and a fit name is whatever the user
    typed into the card. So both stay out of _PREFIX_TO_KIND and both degrade
    to bare text exactly as `stat:` does, rather than half-parsing a chip that
    would silently drop the words after the first space."""
    for raw in (f"holds:cargo/{fc.AMMO_NAME}<500", f"holds:{fc.AMMO_NAME}<500"):
        spec = parse(raw)
        assert spec.chips == [], raw
        assert spec.text == raw, raw
    stat_twin = parse("stat:Missile Damage Bonus>10")
    assert stat_twin.chips == [] and stat_twin.text == "stat:Missile Damage Bonus>10"

    # Quoted, the chip mints -- and the vocabulary is never consulted, so a
    # value that names nothing is still a chip that matches nothing.
    assert parse(f'holds:"cargo/{fc.AMMO_NAME}<500"').chips == [
        Chip(kind=omni.HOLDS_KIND, value=f"cargo/{fc.AMMO_NAME}<500")
    ]
    assert parse('fit:"Ratting Dominix"').chips == [
        Chip(kind=omni.FIT_KIND, value="Ratting Dominix")
    ]


def test_the_two_ship_chips_fold_case_the_way_every_other_chip_now_does(fconn):
    """Chip comparisons ignore case throughout the grammar, so these two must
    not be the exception a user trips over. Neither needs COLLATE bolted on:
    the holds lookup already spells its type comparison NOCASE and the fits
    table declares its name column NOCASE, and this pins both against a
    regression that silently makes the same query typed two ways differ."""
    proper = f'holds:"cargo/{fc.AMMO_NAME}">=100'
    assert _ids(fconn, parse(proper)) != set(), "the fixture answers the proper spelling"
    for typed in (proper.lower(), proper.upper()):
        assert _ids(fconn, parse(typed)) == _ids(fconn, parse(proper)), typed
    assert _ids(fconn, parse(f'fit:"{fc.RATTING.upper()}"')) == (
        _ids(fconn, parse(f'fit:"{fc.RATTING}"'))
    )
    assert _ids(fconn, parse("IS:FIT")) == _ids(fconn, parse("is:fit"))


def test_holds_value_is_the_inverse_of_parse_holds_and_the_column_key_folds_case():
    """The card writes chips through holds_value, so a chip it wrote and a
    chip the user typed are the same string -- that is what makes a saved
    view of either recall identically. The column key drops the threshold on
    purpose: two chips comparing one consumable in one bay are two filters
    over ONE column."""
    for raw in ("Antimatter Charge M<500", "cargo/Antimatter Charge M>=100",
                "drones/Hobgoblin II>0", "fuel/Nitrogen Isotopes=0..0",
                "fighters/Templar II>=1", "fleet/Nanite Repair Paste=1..9",
                "Tritanium=100..200.5"):
        term = omni.parse_holds(raw)
        assert term is not None, raw
        assert omni.holds_value(term) == raw, raw
        assert omni.parse_holds(omni.holds_value(term)) == term
    term = omni.parse_holds("cargo/Antimatter Charge M<500")
    assert omni.holds_column_key(term) == "holds:cargo/antimatter charge m"
    assert omni.holds_column_header(term) == "Antimatter Charge M · cargo"
    tubes = omni.parse_holds("fighters/Templar II>=1")
    assert omni.holds_column_key(tubes) == "holds:fighters/templar ii"
    assert omni.holds_column_header(tubes) == "Templar II · fighters"
    assert omni.holds_column_header(omni.parse_holds("fleet/Templar II>=1")) == (
        "Templar II · fleet"
    )
    plain = omni.parse_holds("Antimatter Charge M<500")
    assert omni.holds_column_key(plain) == "holds:all/antimatter charge m"
    assert omni.holds_column_header(plain) == "Antimatter Charge M"
    # Case and threshold do not change the column; the bay does.
    assert omni.holds_column_key(omni.parse_holds("ANTIMATTER charge m>=9")) == \
        omni.holds_column_key(plain)
    assert omni.holds_column_key(omni.parse_holds("fuel/Antimatter Charge M<500")) != \
        omni.holds_column_key(term)


def test_single_holds_terms_lists_the_positive_chips_once_per_column():
    """The view's gate for the count columns. Negated chips grow none: a
    column of numbers all below the threshold answers nothing the rows do
    not already."""
    spec = parse('holds:"Antimatter Charge M<500" holds:"Antimatter Charge M>100" '
                 'holds:"cargo/Antimatter Charge M<500" -holds:"Tritanium>1"')
    terms = omni.single_holds_terms(spec)
    assert [omni.holds_column_key(t) for t in terms] == [
        "holds:all/antimatter charge m", "holds:cargo/antimatter charge m",
    ]
    assert omni.single_holds_terms(parse("cat:Ship")) == []
    assert omni.single_holds_terms(
        FilterSpec(chips=[Chip(omni.HOLDS_KIND, "nonsense")])
    ) == [], "an untranslatable chip grows no column either"


def test_named_fit_is_the_first_positive_fit_chip_else_the_first_negated_one():
    """A positive chip is the user naming a fit outright; failing that, the
    fit a `-fit:` chip filters deviations from is the one they are asking
    about, so the inspector and the comparison measure against it rather
    than the closest fit, which for a deviating ship may be a different one
    that it happens to match."""
    assert omni.named_fit(parse("fit:Ratting fit:Solo")) == "Ratting"
    assert omni.named_fit(parse("-fit:Ratting fit:Solo")) == "Solo"
    assert omni.named_fit(parse("-fit:Ratting")) == "Ratting"
    assert omni.named_fit(parse("-fit:Ratting -fit:Solo")) == "Ratting"
    assert omni.named_fit(parse("cat:Ship")) is None


def test_deviation_filter_active_reads_only_the_negated_fit_forms():
    """The row menu's Compare deviation entry follows this: present under
    `-fit:` and `-is:fit`, absent under a positive fit chip, `is:fit` or no
    fit filter at all."""
    assert omni.deviation_filter_active(parse('-fit:"Ratting"'))
    assert omni.deviation_filter_active(parse("cat:Ship -is:fit"))
    assert not omni.deviation_filter_active(parse('fit:"Ratting"'))
    assert not omni.deviation_filter_active(parse("is:fit"))
    assert not omni.deviation_filter_active(parse("cat:Ship"))
    assert not omni.deviation_filter_active(parse(""))


@pytest.fixture()
def fconn(tmp_path):
    """The synthetic fit estate: ten assembled ships, a packaged stack, a
    loose hangar pile and three stored fits (tests/fit_corpus.py)."""
    c = db.init(tmp_path / "fits.sqlite")
    fc.install(c)
    return c


def test_holds_counts_what_sits_inside_a_ship_in_every_bay_form(fconn):
    """The four forms over one corpus consumable. The whole-ship form counts
    the rounds loaded in a launcher; no bay form does, because they sit on a
    slot flag. The ammo hold rides with the fuel bay, a fighter tube with
    the drone bay, and a container's contents belong to the container."""
    ammo = fc.AMMO_NAME
    assert _ids(fconn, parse(f'holds:"{ammo}>=500"')) == {fc.SHIP_PERMUTED}
    assert _ids(fconn, parse(f'holds:"{ammo}>=100"')) == {fc.SHIP_EXACT, fc.SHIP_PERMUTED}
    assert _ids(fconn, parse(f'holds:"{ammo}>=651"')) == set(), "the crate's 1,000 do not count"
    assert _ids(fconn, parse(f'holds:"cargo/{ammo}>=300"')) == {fc.SHIP_PERMUTED}
    assert _ids(fconn, parse(f'holds:"cargo/{ammo}>=100"')) == {fc.SHIP_EXACT, fc.SHIP_PERMUTED}
    assert _ids(fconn, parse(f'holds:"fuel/{ammo}>=300"')) == {fc.SHIP_PERMUTED}
    assert _ids(fconn, parse(f'holds:"fuel/{ammo}>=301"')) == set()
    assert _ids(fconn, parse(f'holds:"drones/{ammo}>=1"')) == set()
    assert _ids(fconn, parse(f'holds:"{fc.DRONE_NAME}>=1"')) == {fc.SHIP_EXACT, fc.SHIP_NO_FIT}
    assert _ids(fconn, parse(f'holds:"drones/{fc.DRONE_NAME}>=1"')) == {fc.SHIP_EXACT}, (
        "a drone in the cargo hold is not in the drone bay"
    )
    assert _ids(fconn, parse(f'holds:"drones/{fc.DRONE_NAME}>=5"')) == {fc.SHIP_EXACT}
    assert _ids(fconn, parse(f'holds:"drones/{fc.DRONE_NAME}>=6"')) == set(), (
        "the three in the fleet hangar are not in the drone bay"
    )
    assert _ids(fconn, parse(f'holds:"fleet/{fc.DRONE_NAME}>=3"')) == {fc.SHIP_EXACT}
    assert _ids(fconn, parse(f'holds:"fleet/{fc.DRONE_NAME}>=4"')) == set()
    assert _ids(fconn, parse(f'holds:"drones/{fc.FIGHTER_NAME}>=1"')) == set(), (
        "a fighter in a tube is not in the drone bay"
    )
    assert _ids(fconn, parse(f'holds:"fighters/{fc.FIGHTER_NAME}>=2"')) == {fc.SHIP_EXACT}, (
        "FighterTube2 rides with the fighter bay"
    )
    assert _ids(fconn, parse(f'holds:"fighters/{fc.DRONE_NAME}>=1"')) == set()
    # Case-insensitive, like every other exact-name axis in the grammar.
    assert _ids(fconn, parse(f'holds:"{ammo.lower()}>=100"')) == {fc.SHIP_EXACT, fc.SHIP_PERMUTED}
    # A range, closed at both ends.
    assert _ids(fconn, parse(f'holds:"{ammo}=100..650"')) == {fc.SHIP_EXACT, fc.SHIP_PERMUTED}
    assert _ids(fconn, parse(f'holds:"{ammo}=101..649"')) == set()
    assert _ids(fconn, parse(f'holds:"{ammo}=0..0"')) == fc.ASSEMBLED_SHIPS - {
        fc.SHIP_EXACT, fc.SHIP_PERMUTED, fc.SHIP_NO_FIT
    }


def test_a_ship_with_none_of_the_type_counts_zero_and_a_non_ship_never_matches(fconn):
    """Zero is a real count, so `<N` lists the empty hull -- which is the
    whole point of "which of my ships are short". The packaged stack is not
    a ship anyone can load, and the 5,000 rounds loose in the hangar are not
    inside anything."""
    ammo = fc.AMMO_NAME
    assert fc.SHIP_EMPTY in _ids(fconn, parse(f'holds:"{ammo}<1"'))
    assert _ids(fconn, parse(f'holds:"{ammo}<500"')) == fc.ASSEMBLED_SHIPS - {fc.SHIP_PERMUTED}
    assert fc.SHIP_PACKAGED not in _ids(fconn, parse(f'holds:"{ammo}<500"'))
    assert fc.LOOSE_STACK not in _ids(fconn, parse(f'holds:"{ammo}<500"'))
    assert _ids(fconn, parse('holds:"Widget Of Doom<1"')) == fc.ASSEMBLED_SHIPS, (
        "a type nobody owns leaves every ship at zero"
    )


def test_negated_holds_is_the_complement_within_the_assembled_ships(fconn):
    """`-holds:"X">=100` answers "which of my ships are short of X" and
    nothing else. A NOT EXISTS negation, the shape stat: and roll: use,
    would list every non-ship asset in the estate alongside them -- the
    opposite of useful -- and there is no "no data" case to protect, because
    an empty ship honestly holds zero."""
    ammo = fc.AMMO_NAME
    short = _ids(fconn, parse(f'-holds:"{ammo}>=100"'))
    assert short == fc.ASSEMBLED_SHIPS - {fc.SHIP_EXACT, fc.SHIP_PERMUTED}
    assert fc.SHIP_EMPTY in short, "an empty hull is short of everything"
    assert not short & {fc.SHIP_PACKAGED, fc.LOOSE_STACK, fc.CRATE_ITEM}
    assert _ids(fconn, parse(f'-holds:"{ammo}>=1"')) | _ids(fconn, parse(f'holds:"{ammo}>=1"')) \
        == fc.ASSEMBLED_SHIPS, "the two polarities partition the ships exactly"
    assert _ids(fconn, parse(f'-holds:"cargo/{ammo}>=300"')) == fc.ASSEMBLED_SHIPS - {
        fc.SHIP_PERMUTED
    }


def test_several_holds_chips_and_together_and_compose_with_other_kinds(fconn):
    """Two thresholds on one consumable are two requirements, so they AND
    into a band -- the same composition the other comparisons have."""
    ammo = fc.AMMO_NAME
    both = parse(f'holds:"{ammo}>=100" holds:"{ammo}<=200"')
    assert _ids(fconn, both) == {fc.SHIP_EXACT}
    assert _ids(fconn, parse(f'holds:"{ammo}>=100" holds:"cargo/{ammo}>=300"')) == {
        fc.SHIP_PERMUTED
    }
    assert _ids(fconn, parse(f'holds:"{ammo}>=1" owner:"{fc.PILOT_NAME}"')) == {
        fc.SHIP_EXACT, fc.SHIP_PERMUTED, fc.SHIP_NO_FIT
    }
    assert _ids(fconn, parse(f'holds:"{ammo}>=1" owner:Nobody')) == set()
    assert _ids(fconn, parse(f'holds:"{ammo}>=1" item:Dominix')) == {
        fc.SHIP_EXACT, fc.SHIP_PERMUTED
    }


def test_holds_binds_the_type_name_and_the_numbers(fconn):
    """The type name is user input; only the whitelisted operator and the
    bay's own flag list reach the SQL text."""
    # The classic `' OR 1=1` cannot even be typed here -- the name group
    # rejects `=`, pinned at the end of this test -- so the hostile name has
    # to carry no operator character to reach the SQL at all.
    hostile = "x' OR 'a' IS NOT NULL --"
    spec = parse(f'holds:"{hostile}<5"')
    assert spec.chips and spec.chips[0].kind == omni.HOLDS_KIND
    where, params = spec.where()
    assert hostile not in where
    assert params == (hostile, 5.0)
    assert _ids(fconn, spec) == fc.ASSEMBLED_SHIPS, "a name nobody owns is zero everywhere"
    where, params = parse('holds:"Tritanium=1..2"').where()
    assert params == ("Tritanium", 1.0, 2.0) and "BETWEEN ? AND ?" in where
    for op in (">", "<", ">=", "<="):
        where, _ = parse(f'holds:"Tritanium{op}5"').where()
        assert f") {op} ?" in where and f") {op}= ?" not in where
    # An `=` inside the name is not a chip at all: the name group rejects it.
    assert parse('holds:"a=b<5"').chips == []
    hand_built = FilterSpec(chips=[Chip(omni.HOLDS_KIND, "Tritanium; DROP TABLE assets")])
    assert hand_built.where() == ("", ()), "an untranslatable holds chip is skipped"


# ------------------------------------------------------------------ fit chips
def test_fit_mints_a_chip_for_any_name_including_a_quoted_empty_one():
    """A fit name is a label, so the level rule applies rather than the
    comparison rule: any string is a legitimate name, and the empty one is
    what the card writes before anything is picked."""
    assert parse("fit:Ratting").chips == [Chip(omni.FIT_KIND, "Ratting")]
    assert parse('-fit:"Deep Safe"').chips == [Chip(omni.FIT_KIND, "Deep Safe", negated=True)]
    assert parse('fit:""').chips == [Chip(omni.FIT_KIND, "")]
    assert parse("FIT:Ratting").chips == [Chip(omni.FIT_KIND, "Ratting")]
    # A bare `fit:` is the half-typed state, like every other prefix.
    assert parse("fit:").chips == [] and parse("fit:").text == "fit:"
    # And the bare word is not a chip: only `abyssal` has that privilege.
    assert parse("fit").chips == [] and parse("fit").text == "fit"
    for raw in ("fit:Ratting", "-fit:Ratting", 'fit:""', 'fit:"a\\"b"', 'fit:"a b"',
                'holds:"cargo/Antimatter Charge M<500"', 'holds:"X=0..0"', "is:fit", "-is:fit"):
        spec = parse(raw)
        assert spec.chips, raw
        assert parse(spec.to_text()) == spec, raw


def test_a_fit_chip_matches_the_ships_of_its_hull_whose_rack_equals_the_fit(fconn):
    """Slot order is ignored and a loaded charge is not a module, so the
    permuted ship matches; an extra module, a missing one and a duplicate
    count one short all deviate, and so does a module whose SDE category row
    is missing -- silently dropping that one would turn a wrong fit right."""
    assert _ids(fconn, parse(f'fit:"{fc.RATTING}"')) == {fc.SHIP_EXACT, fc.SHIP_PERMUTED}
    assert _ids(fconn, parse(f'fit:"{fc.SOLO}"')) == {fc.SHIP_SOLO}
    assert _ids(fconn, parse(f'fit:"{fc.HAULING}"')) == {fc.SHIP_CHARON}
    assert _ids(fconn, parse('fit:"No Such Fit"')) == set()
    assert _ids(fconn, parse('fit:""')) == set()
    # Case-insensitive: the fits.name column collates NOCASE.
    assert _ids(fconn, parse(f'fit:"{fc.RATTING.lower()}"')) == {fc.SHIP_EXACT, fc.SHIP_PERMUTED}


def test_a_negated_fit_chip_lists_the_deviating_ships_of_that_hull_only(fconn):
    """Hull-scoped by design: `-fit:"Ratting"` says nothing about the Charon
    or about anything that is not a ship. The two polarities partition the
    hull's assembled ships and nothing else."""
    deviating = _ids(fconn, parse(f'-fit:"{fc.RATTING}"'))
    assert deviating == fc.DOMINIXES - {fc.SHIP_EXACT, fc.SHIP_PERMUTED}
    assert fc.SHIP_EMPTY in deviating, "an empty hull deviates from every fit"
    assert fc.SHIP_CHARON not in deviating and fc.SHIP_NO_FIT not in deviating
    assert not deviating & {fc.SHIP_PACKAGED, fc.LOOSE_STACK}
    assert deviating | _ids(fconn, parse(f'fit:"{fc.RATTING}"')) == fc.DOMINIXES
    assert _ids(fconn, parse('-fit:"No Such Fit"')) == set(), (
        "a name no stored fit carries has no hull to scope to"
    )


def test_a_duplicate_count_deviates_in_either_direction(fconn):
    """The multiset comparison is two NOT EXISTS with different shapes -- one
    walks the ship's types, one the fit's -- so too MANY of a type has to be
    checked as well as too few. The corpus covers three amplifiers wanted
    against two fitted; this covers four fitted against three wanted, where
    the fit's own side of the comparison is a non-zero sum rather than the
    COALESCE fallback."""
    fconn.executemany(
        "INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,"
        "location_flag,location_type,is_singleton,is_blueprint_copy,custom_name,"
        "root_location_id,system_id,region_id) "
        "VALUES ('character',100,?,?,1,?,?,'item',1,0,NULL,?,?,?)",
        [
            (5_000_020, fc.DOMINIX, fc.JITA_4_4, "Hangar", fc.JITA_4_4, fc.JITA_SYS, fc.THE_FORGE),
            *[
                (5_000_020 + n + 1, type_id, 5_000_020, flag,
                 fc.JITA_4_4, fc.JITA_SYS, fc.THE_FORGE)
                for n, (type_id, flag) in enumerate([
                    (fc.ARMOR_REPAIRER, "LoSlot0"), (fc.FIELD_AMPLIFIER, "LoSlot1"),
                    (fc.FIELD_AMPLIFIER, "LoSlot2"), (fc.FIELD_AMPLIFIER, "LoSlot3"),
                    (fc.FIELD_AMPLIFIER, "LoSlot4"), (fc.LAUNCHER, "HiSlot0"),
                    (fc.PULSE_LASER, "HiSlot1"), (fc.NANO_PUMP, "RigSlot0"),
                ])
            ],
        ],
    )
    assert _ids(fconn, parse(f'fit:"{fc.RATTING}"')) == {fc.SHIP_EXACT, fc.SHIP_PERMUTED}
    assert 5_000_020 in _ids(fconn, parse(f'-fit:"{fc.RATTING}"'))
    assert 5_000_020 in _ids(fconn, parse("-is:fit"))


def test_a_fit_whose_hull_is_not_a_ship_is_refused_and_would_match_nothing(fconn):
    """Two defences, both pinned. The parser refuses a header naming a charge
    (see test_fits.py), so the card never stores one; and were a row to reach
    the table anyway -- one saved before the rule, or written by hand -- the
    ship clause makes it harmless in both polarities, which is why the SQL
    does not lean on the parser."""
    text = f"[{fc.AMMO_NAME}, Nonsense]\nFocused Pulse Laser"
    parsed = fits.parse_eft(fconn, text)
    assert not parsed.ok and parsed.hull_type_id is None
    with pytest.raises(ValueError):
        fits.save_fit(fconn, parsed, "Nonsense", text)

    fconn.execute(
        "INSERT INTO fits (name, hull_type_id, eft_text, created_at) VALUES (?,?,?,?)",
        ("Nonsense", fc.AMMO, text, "2026-09-05T00:00:00+00:00"),
    )
    assert _ids(fconn, parse('fit:"Nonsense"')) == set()
    assert _ids(fconn, parse('-fit:"Nonsense"')) == set()
    assert fc.LOOSE_STACK not in _ids(fconn, parse("-is:fit"))


def test_positive_fit_chips_or_together_while_a_negated_one_ands(fconn):
    """Two fits of a hull are two acceptable answers to "is this ship
    ready", so they OR the way two location chips do; a negation is a
    requirement and ANDs like every other."""
    either = parse(f'fit:"{fc.RATTING}" fit:"{fc.SOLO}"')
    assert _ids(fconn, either) == {fc.SHIP_EXACT, fc.SHIP_PERMUTED, fc.SHIP_SOLO}
    across_hulls = parse(f'fit:"{fc.RATTING}" fit:"{fc.HAULING}"')
    assert _ids(fconn, across_hulls) == {fc.SHIP_EXACT, fc.SHIP_PERMUTED, fc.SHIP_CHARON}
    assert _ids(fconn, parse(f'-fit:"{fc.RATTING}" -fit:"{fc.SOLO}"')) == (
        fc.DOMINIXES - {fc.SHIP_EXACT, fc.SHIP_PERMUTED, fc.SHIP_SOLO}
    )
    assert _ids(fconn, parse(f'fit:"{fc.RATTING}" -fit:"{fc.SOLO}"')) == {
        fc.SHIP_EXACT, fc.SHIP_PERMUTED
    }
    assert _ids(fconn, parse(f'fit:"{fc.RATTING}" owner:"{fc.PILOT_NAME}"')) == {
        fc.SHIP_EXACT, fc.SHIP_PERMUTED
    }
    assert _ids(fconn, parse(f'fit:"{fc.RATTING}" holds:"{fc.AMMO_NAME}>=500"')) == {
        fc.SHIP_PERMUTED
    }


def test_is_fit_is_any_stored_fit_for_the_hull_and_excludes_hulls_without_one(fconn):
    """A hull nobody has written a fit for is absent from BOTH polarities:
    there is no verdict to give about it, and answering "deviates" would be
    a claim the data does not support. The Solstice and the Charon prove
    each half."""
    assert _ids(fconn, parse("is:fit")) == {
        fc.SHIP_EXACT, fc.SHIP_PERMUTED, fc.SHIP_SOLO, fc.SHIP_CHARON
    }
    assert fc.SHIP_SOLO in _ids(fconn, parse("is:fit")), "matching the SECOND fit of its hull"
    deviating = _ids(fconn, parse("-is:fit"))
    assert deviating == {
        fc.SHIP_EXTRA, fc.SHIP_MISSING, fc.SHIP_DUPES, fc.SHIP_EMPTY, fc.SHIP_NULL_CATEGORY
    }
    assert fc.SHIP_NO_FIT not in deviating and fc.SHIP_NO_FIT not in _ids(fconn, parse("is:fit"))
    assert not (deviating | _ids(fconn, parse("is:fit"))) & {fc.SHIP_PACKAGED, fc.LOOSE_STACK}
    assert _ids(fconn, parse("is:fit -is:fit")) == set(), "no ship is both"


def test_fit_names_are_bound_parameters(fconn):
    hostile = "x' OR 1=1"
    spec = parse(f'fit:"{hostile}"')
    where, params = spec.where()
    assert hostile not in where
    assert params == (hostile,)
    assert _ids(fconn, spec) == set()
    where, params = parse(f'fit:"{fc.RATTING}" -fit:"{fc.SOLO}"').where()
    assert params == (fc.RATTING, fc.SOLO), "positives bind before negations"


def test_exclude_kinds_drops_the_two_ship_chips_in_both_polarities(fconn):
    """The holds and fit cards each rewrite their own kind on Done, so their
    pickers facet by everything else -- and the count that footer shows must
    not be narrowed by the chip the user is editing."""
    spec = parse(f'holds:"{fc.AMMO_NAME}>=5000" -fit:"{fc.RATTING}" cat:Ship')
    assert _ids(fconn, spec) == set()
    where, params = spec.where(exclude_kinds=(omni.HOLDS_KIND, omni.FIT_KIND))
    assert where == "(cat.name = ? COLLATE NOCASE)" and params == ("Ship",)
    assert _ids(fconn, spec, exclude_level=omni.HOLDS_KIND) == (
        fc.DOMINIXES - {fc.SHIP_EXACT, fc.SHIP_PERMUTED}
    )
    where, _ = spec.where(exclude_kinds=(omni.FIT_KIND,))
    assert "fits f" not in where and "assets h" in where


def test_the_ship_chips_probe_every_table_by_key_and_scan_none(fconn):
    """Both clauses are correlated per asset row, so a table scan inside one
    would be a scan per row over the whole estate. The SCANs allowed are the
    outer table, the container_walk CTE and its window-function subquery --
    which ASSET_ROWS builds once, whatever the filter (see the roll clause's
    twin above). The holds: type lookup (HOLDS_TYPE_IDS) used to be a fourth:
    its `name = ? COLLATE NOCASE` cannot use the BINARY idx_types_name, so
    it walked the whole of sde_types per statement until
    idx_types_name_nocase was added; it must now be a probe of that index."""
    walk = ("SCAN a", "SCAN w", "SCAN container_walk", "SCAN (subquery-")
    seen = 0
    for raw in (f'holds:"{fc.AMMO_NAME}<500"', f'holds:"cargo/{fc.AMMO_NAME}<500"'):
        where, params = parse(raw).where()
        plan = [r[3] for r in fconn.execute(
            "EXPLAIN QUERY PLAN " + queries.ASSET_ROWS + f" WHERE {where}", params
        )]
        scans = [line for line in plan if line.startswith("SCAN")]
        assert all(line.startswith(walk) for line in scans), (raw, scans)
        assert any("SEARCH h USING INDEX idx_assets_direct_loc" in line for line in plan), plan
        assert any(
            "SEARCH sde_types USING COVERING INDEX idx_types_name_nocase" in line
            for line in plan
        ), plan
        seen += 1
    for raw in (f'fit:"{fc.RATTING}"', f'-fit:"{fc.RATTING}"', "is:fit", "-is:fit"):
        where, params = parse(raw).where()
        plan = [r[3] for r in fconn.execute(
            "EXPLAIN QUERY PLAN " + queries.ASSET_ROWS + f" WHERE {where}", params
        )]
        scans = [line for line in plan if line.startswith("SCAN")]
        assert all(line.startswith(walk) for line in scans), (raw, scans)
        assert sum("SEARCH fm USING INDEX idx_assets_direct_loc" in line for line in plan) >= 2, (
            "both directions of the multiset comparison key on the ship"
        )
        assert any("SEARCH fi USING INDEX sqlite_autoindex_fit_items_1" in line
                   for line in plan), plan
        assert any(line.startswith("SEARCH f ") and "sqlite_autoindex_fits_1" in line
                   for line in plan), plan
        seen += 1
    assert seen == 6


@pytest.fixture()
def big_fit_conn(tmp_path):
    """25,000 asset rows: 2,500 assembled Dominixes with eight or nine
    modules each and a cargo stack, on top of the corpus's own estate and
    its three stored fits. Built in one transaction, about a second."""
    c = db.init(tmp_path / "bigfit.sqlite")
    fc.install(c)
    rng = random.Random(11)
    rows = []
    item = 7_000_000
    for i in range(2500):
        hull = item
        item += 1
        rows.append((hull, fc.DOMINIX, 1, fc.JITA_4_4, "Hangar", 1))
        mods = [fc.ARMOR_REPAIRER, fc.FIELD_AMPLIFIER, fc.FIELD_AMPLIFIER, fc.FIELD_AMPLIFIER,
                fc.LAUNCHER, fc.PULSE_LASER, fc.NANO_PUMP, fc.FLUX_COIL]
        if i % 3 == 0:
            # A third of them are one module short of the Ratting fit and
            # the rest carry the flux coil too, so the filter has both
            # answers to find rather than one trivial one.
            mods = mods[:-1]
        for n, module in enumerate(mods):
            rows.append((item, module, 1, hull, f"LoSlot{n}", 1))
            item += 1
        rows.append((item, fc.AMMO, rng.randint(0, 2000), hull, "Cargo", 0))
        item += 1
    with db.transaction(c):
        c.executemany(
            "INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,"
            "location_flag,location_type,is_singleton,is_blueprint_copy,custom_name,"
            "root_location_id,system_id,region_id) "
            "VALUES ('character',100,?,?,?,?,?,'item',?,0,NULL,?,?,?)",
            [(i, t, q, loc, flag, s, fc.JITA_4_4, fc.JITA_SYS, fc.THE_FORGE)
             for i, t, q, loc, flag, s in rows],
        )
    return c


def test_the_ship_clauses_cost_a_bounded_multiple_of_the_lean_count_they_narrow(big_fit_conn):
    """Judged against the lean COUNT base (assets joined to types, groups
    and categories) timed in the same process rather than a wall-clock
    figure, so a loaded runner moves both numbers together, and with a
    budget per clause: on 2026-09-05 the base took 1.7 ms over 24k rows,
    `is:fit` 38 ms (22x) and the holds clause 9 ms (5x), so 45x and 12x
    leave each about twice its headroom while a 4x regression of either
    fails. The first version judged them against the full table fetch (186
    ms here, 330 ms at 46k rows), which a 4x regression sailed under. This
    is the test that caught the real cost: without the `+` that stops SQLite
    indexing fm.type_id, the fit clause probed every asset of a module's
    type once per (ship, fit item) pair and measured 1,583 ms against a 64
    ms fetch -- 30 ms with it."""
    def best_of(fn, n=5):
        best = float("inf")
        for _ in range(n):
            start = time.perf_counter()
            fn()
            best = min(best, time.perf_counter() - start)
        return best

    total = big_fit_conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    assert total > 24_000, "the estate must actually be large"
    base = ("SELECT COUNT(*) FROM assets a JOIN sde_types t ON t.type_id = a.type_id "
            "LEFT JOIN sde_groups g ON g.group_id = t.group_id "
            "LEFT JOIN sde_categories cat ON cat.category_id = g.category_id")
    reference = best_of(lambda: big_fit_conn.execute(base).fetchone()[0])
    budgets = {"is:fit": 45, f'holds:"{fc.AMMO_NAME}<500"': 12}
    checked = 0
    for raw, budget in budgets.items():
        where, params = parse(raw).where()
        sql = base + " WHERE " + where
        best = best_of(lambda s=sql, p=params: big_fit_conn.execute(s, p).fetchone()[0])
        matched = big_fit_conn.execute(sql, params).fetchone()[0]
        assert 0 < matched < 2500, f"{raw} must have had something to do"
        assert best < max(0.015, budget * reference), (
            f"{raw} {best * 1000:.1f} ms vs lean count base {reference * 1000:.2f} ms "
            f"(budget {budget}x)"
        )
        checked += 1
    assert checked == 2


def test_fitted_rows_hide_for_everyday_filters_and_show_for_ships_fits_and_named_kinds():
    """The Assets tab's default: racks and the rounds inside them are noise
    between hangar rows unless the question is about ships, fits or that kind
    of item, in which case every fitted row is part of the answer. Negated
    kind chips do not count as naming a thing -- "-cat:Charge" is asking for
    everything else. The abyssal kinds reveal too, and that pins a bug that
    shipped: a mutated module spends most of its life in a slot, and hiding
    fitted rows under `abyssal` emptied the table, the "N of M" count and the
    card's type picker for exactly the modules being searched for."""
    hides = omni.hides_fitted
    hidden = ("", "loc:Jita", "owner:Main", "antimatter", "-cat:Charge", "-is:fitted", "val:>1m",
              "-abyssal", "-is:abyssal", '-abyssal:"Abyssal Stasis Webifier"', "-stat:cpu<30",
              "-roll:web>=70")
    shown = (
        "cat:Ship", "cat:Charge", "group:Hybrid", "item:Tritanium", "is:fitted", "is:fit",
        'fit:"Ratting"', '-fit:"Ratting"', "loc:Jita cat:Charge",
        "abyssal", "is:abyssal", 'abyssal:"Abyssal Stasis Webifier"', "stat:cpu<30",
        "roll:web>=70", "loc:Jita abyssal", "abyssal -stat:cpu<30",
    )
    checked = 0
    for text in hidden:
        spec = parse(text)
        assert spec.chips or spec.text == text, text
        assert hides(spec), text
        checked += 1
    for text in shown:
        assert parse(text).chips, text
        assert not hides(parse(text)), text
        checked += 1
    assert checked == len(hidden) + len(shown) == 28


def test_a_decimal_threshold_survives_the_holds_round_trip():
    """The holds card seeds its spinners from the chip and rounds to whole
    units, which is right for a hold; the grammar underneath must not, or a
    typed `<1.5` would come back as `<1` from a saved view as well as from
    the card, and the two would disagree about which ships are short."""
    spec = parse('holds:"Nanite Repair Paste"<1.5')
    assert len(spec.chips) == 1 and spec.chips[0].kind == omni.HOLDS_KIND
    term = omni.parse_holds(spec.chips[0].value)
    assert (term.name, term.op, term.low, term.high) == ("Nanite Repair Paste", "<", 1.5, None)
    assert omni.holds_value(term) == "Nanite Repair Paste<1.5"
    assert spec.to_text() == 'holds:"Nanite Repair Paste<1.5"'
    assert parse(spec.to_text()) == spec
    ranged = omni.parse_holds("cargo/Tritanium=0.5..2.25")
    assert (ranged.low, ranged.high) == (0.5, 2.25)
    assert omni.holds_value(ranged) == "cargo/Tritanium=0.5..2.25"


def test_a_fitted_abyssal_module_stays_in_the_count_and_the_type_picker_under_the_abyssal_chips(
    aconn,
):
    """Pins the shipped bug end to end: hides_fitted answered True for the
    abyssal kinds, so the view ANDed HIDE_FITTED_CLAUSE onto the filter and
    a mutated module sitting in a slot -- where most of them live -- fell
    out of the table, the "N of M" count and the card's type picker, which
    listed nothing for a type owned only in a rack."""
    aconn.executescript("""
      INSERT INTO sde_types (type_id,name,group_id,meta_group_id,volume,portion_size,published,
                             is_dynamic_type) VALUES
        (47740,'Abyssal Warp Scrambler',46,15,5,1,1,1);
      INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
                          location_flag,location_type,is_singleton,is_blueprint_copy,
                          custom_name,root_location_id,system_id,region_id) VALUES
        ('character',100,14,47740,1,2,'MedSlot0','item',1,0,NULL,60003760,30000142,10000002);
    """)
    checked = 0
    for text in ("abyssal", 'abyssal:"Abyssal Warp Scrambler"', "is:abyssal",
                 'abyssal owner:"Test Pilot"'):
        spec = parse(text)
        where, params = spec.where()
        if omni.hides_fitted(spec):
            where = f"({where}) AND {queries.HIDE_FITTED_CLAUSE}"
        ids = {r["item_id"] for r in queries.fetch_assets(aconn, where, params)}
        assert 14 in ids, text
        assert queries.count_assets(aconn, where, params) == len(ids), text
        picker = {r["name"]: r["items"] for r in queries.abyssal_type_counts(aconn, where, params)}
        assert picker.get("Abyssal Warp Scrambler") == 1, (text, picker)
        checked += 1
    assert checked == 4
    # The everyday filter still hides it: the rule widened, it did not vanish.
    everyday = parse('owner:"Test Pilot"')
    assert omni.hides_fitted(everyday)
    where, params = everyday.where()
    where = f"({where}) AND {queries.HIDE_FITTED_CLAUSE}"
    ids = {r["item_id"] for r in queries.fetch_assets(aconn, where, params)}
    assert 14 not in ids and 3 not in ids, "both fitted rows stay hidden"
    assert {2, 11, 12} <= ids, "the pilot's hangar rows are still the answer"


def test_the_lean_row_source_counts_what_the_full_one_does_without_the_container_walk(request):
    """count_assets, count_ships, the holds picker's ship-id subquery,
    abyssal_type_counts and the rail's label family run on ASSET_ROWS_LEAN.
    Its whole justification is that the recursive container CTE -- which
    SQLite materialises over every asset row before the WHERE runs -- is
    not in the plan, and its whole risk is a count disagreeing with the
    table it describes. So: for every kind of WHERE the grammar writes, the
    lean COUNT equals the full one, and the count's plan never touches the
    walk."""
    cases = {
        "conn": ["", "jita", "cat:Ship", "loc:Jita", "owner:Test Pilot", "-region:Domain",
                 "is:bpc", "is:safety", "val:>10m", "group:Freighter", "item:Dominix",
                 "-is:fitted", "is:fitted", "owner:Main tritanium"],
        "aconn": ["abyssal", "-abyssal", "is:abyssal", "stat:cpu<30", "-stat:cpu<30",
                  "roll:web>=70", "abyssal roll:cpu=60..90",
                  'abyssal:"Abyssal Stasis Webifier"'],
        "fconn": ["is:fit", "-is:fit", f'fit:"{fc.RATTING}"', f'-fit:"{fc.RATTING}"',
                  f'holds:"{fc.AMMO_NAME}<500"', f'holds:"cargo/{fc.AMMO_NAME}>=100"',
                  "cat:Ship", f'-holds:"{fc.PASTE_NAME}">=100'],
    }
    checked = 0
    for fixture, texts in cases.items():
        c = request.getfixturevalue(fixture)
        for text in texts:
            where, params = parse(text).where()
            full_sql = (f"SELECT COUNT(*) FROM ({queries.ASSET_ROWS}"
                        f"{' WHERE ' + where if where else ''})")
            full = c.execute(full_sql, params).fetchone()[0]
            assert queries.count_assets(c, where, params) == full, text
            ships_sql = (f"SELECT COUNT(*) FROM ({queries.ASSET_ROWS} WHERE "
                         f"{queries.SHIP_ROWS_CLAUSE}{' AND (' + where + ')' if where else ''})")
            assert queries.count_ships(c, where, params) == (
                c.execute(ships_sql, params).fetchone()[0]
            ), text
            plan = [r[3] for r in c.execute(
                "EXPLAIN QUERY PLAN " + queries.count_assets_sql(where), params
            )]
            # The is:safety clause materialises a CTE of its own, so the pin
            # is on the container walk by name, not on MATERIALIZE at large.
            assert not any("container" in line for line in plan), (text, plan)
            checked += 1
    assert checked == sum(len(v) for v in cases.values()) == 30
    assert "container_path" not in queries.ASSET_ROWS_LEAN
    assert "cp." not in queries.ASSET_ROWS_LEAN
    assert "container_path" in queries.ASSET_ROWS and "AS container" in queries.ASSET_ROWS


def test_the_count_column_query_probes_ships_by_location_whatever_the_statistics_say(fconn):
    """holds_counts refreshes the count column on every filter change. With
    no sqlite_stat1 rows -- every database between SDE imports, since that
    is the only time the app runs ANALYZE -- the planner preferred
    idx_assets_type and walked every stack of the consumable in the estate
    to filter it by the visible ships afterwards; the `+` on h.type_id
    forces the ship-list probe (1.30 ms to 0.39 ms for 300 visible ships
    over 16k stacks of the type, measured 2026-09-05). The type lookup must
    be a probe of idx_types_name_nocase for the same reason the chip's own
    plan test demands it."""
    ids = (fc.SHIP_EXACT, fc.SHIP_PERMUTED, fc.SHIP_MISSING)
    checked = 0
    for bay in (None, "cargo", "fuel"):
        sql = queries.holds_counts_sql(len(ids), bay)
        plan = [r[3] for r in fconn.execute("EXPLAIN QUERY PLAN " + sql, (*ids, fc.AMMO_NAME))]
        assert any(
            line.startswith("SEARCH h USING INDEX idx_assets_direct_loc") for line in plan
        ), (bay, plan)
        assert not any("idx_assets_type" in line for line in plan), (bay, plan)
        assert any(
            "SEARCH sde_types USING COVERING INDEX idx_types_name_nocase" in line
            for line in plan
        ), (bay, plan)
        assert not any(line.startswith("SCAN") for line in plan), (bay, plan)
        checked += 1
    assert checked == 3
    # The steer changes the plan, not the answer.
    counted = queries.holds_counts(fconn, ids, fc.AMMO_NAME)
    assert counted and all(units > 0 for units in counted.values())
    assert counted == queries.holds_counts(fconn, list(ids) * 2, fc.AMMO_NAME)
