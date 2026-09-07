"""The saved-view library: the v6 -> v7 and v7 -> v8 migrations and the store.

All Qt-free. The library is what `Ctrl+S`, `Ctrl+L`, `save:`, `load:` and the
nine digit keys all end up calling, so every rule that decides whether a
keystroke replaces a view or grows a second one is pinned here rather than
through a rendered card.
"""

from __future__ import annotations

import sqlite3

import pytest

from evasset import db, omni, views

DROPPED = "saved_views: dropped (replaced by the views library)"


def _has_table(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


@pytest.fixture()
def conn(tmp_path):
    return db.init(tmp_path / "views.sqlite")


def _state(filter_text="", group_by="", rail_level="", rail_sort="") -> views.ViewState:
    return views.ViewState(
        filter=filter_text, group_by=group_by, rail_level=rail_level, rail_sort=rail_sort
    )


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _v6_database(path) -> None:
    """Write a database in the shape every v6 install is in.

    Built from the current SCHEMA with the views block cut out and the old
    anonymous saved_views table put back, so the fixture cannot drift away
    from the real v6 shape the way a hand-copied CREATE statement would.
    """
    v6 = db.SCHEMA
    start = v6.index("CREATE TABLE IF NOT EXISTS views (")
    end = v6.index(");", start) + len(");")
    v6 = v6[:start] + v6[end:]
    assert "CREATE TABLE IF NOT EXISTS views (" not in v6

    old = sqlite3.connect(path)
    old.executescript(v6)
    old.executescript(
        """
        CREATE TABLE saved_views (
            slot       INTEGER PRIMARY KEY,
            state_json TEXT NOT NULL
        );
        INSERT INTO saved_views VALUES (3, '{"filter": "cat:Ship", "group": "location"}');
        INSERT INTO meta VALUES ('schema_version', '6');
        INSERT INTO pinned_labels VALUES ('location', 'Jita IV - Moon 4');
        INSERT INTO fits (name, hull_type_id, eft_text, created_at)
            VALUES ('Ratting', 645, '[Dominix, Ratting]', '2026-01-01T00:00:00+00:00');
        INSERT INTO fit_items VALUES (1, 34, 'module', 3);
        """
    )
    old.commit()
    old.close()


def _v7_database(path) -> None:
    """Write a database in the shape every v7 install is in: the views table
    without rail_sort, holding named, slotted views.

    Built from the current SCHEMA with the one column line cut out, for the
    v6 fixture's reason -- a hand-copied CREATE statement drifts.
    """
    v7 = db.SCHEMA
    line = "    rail_sort   TEXT    NOT NULL DEFAULT '',\n"
    assert v7.count(line) == 1
    v7 = v7.replace(line, "")

    old = sqlite3.connect(path)
    old.executescript(v7)
    old.executescript(
        """
        INSERT INTO meta VALUES ('schema_version', '7');
        INSERT INTO views (name, slot, filter_text, group_by, rail_level, created_at, updated_at)
            VALUES ('Jita ships', 1, 'cat:Ship loc:Jita', 'location', 'owner',
                    '2026-09-01T00:00:00+00:00', '2026-09-02T00:00:00+00:00'),
                   ('Short of paste', NULL, 'holds:paste<500', '', '',
                    '2026-09-03T00:00:00+00:00', '2026-09-03T00:00:00+00:00');
        INSERT INTO pinned_labels VALUES ('owner', 'Main');
        """
    )
    old.commit()
    old.close()


# --------------------------------------------------------------- migration
def test_a_v7_database_gains_rail_sort_and_keeps_every_view_where_it_was(tmp_path):
    """The rebuild copies the rows across, so the one thing that must not
    happen is a view losing its name, its digit, its id or its timestamps on
    the way; the new column reads '' -- "not recorded" -- rather than a
    guessed default, because _apply_view treats '' as "leave the rail's
    sort alone" and a guessed "value" would move it."""
    path = tmp_path / "v7.sqlite"
    _v7_database(path)

    conn = db.init(path)
    assert db.get_meta(conn, "schema_version") == str(db.SCHEMA_VERSION)
    assert db.get_meta(conn, "migrated_at") == str(db.SCHEMA_VERSION), "migrate() ran"
    assert "rail_sort" in _columns(conn, "views")
    kept = views.list_views(conn)
    assert [(v.view_id, v.name, v.slot) for v in kept] == [
        (1, "Jita ships", 1), (2, "Short of paste", None),
    ]
    assert kept[0].state == views.ViewState("cat:Ship loc:Jita", "location", "owner", "")
    assert kept[0].created_at == "2026-09-01T00:00:00+00:00"
    assert kept[0].updated_at == "2026-09-02T00:00:00+00:00"
    assert conn.execute("SELECT label FROM pinned_labels").fetchone()[0] == "Main"

    assert db.migrate(conn) == [], "a migrated database is not migrated twice"
    # The constraints came back with the rebuild: a second view on digit 1
    # and a case-twin of a name are both still refused.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE views SET slot = 1 WHERE view_id = 2")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO views (name, filter_text, created_at, updated_at)"
            " VALUES ('JITA SHIPS', '', '', '')"
        )
    # And the library writes the new column from here on.
    third, _ = views.save_view(conn, "Third", _state("cat:Frigate", "owner", "region", "name"))
    assert third.view_id == 3, "AUTOINCREMENT carried on from the copied rows"
    assert views.get_view(conn, third.view_id).state.rail_sort == "name"


def test_the_v7_migration_reports_the_rebuild_exactly_once(tmp_path):
    path = tmp_path / "v7-report.sqlite"
    _v7_database(path)

    conn = db.connect(path)
    conn.executescript(db.SCHEMA)
    assert db.migrate(conn) == ["views: added rail_sort (table rebuilt)"]
    assert db.migrate(conn) == []


def test_a_v6_database_arrives_at_v8_with_rail_sort_in_one_init(tmp_path):
    """The chain: v6 has no views table at all, so SCHEMA creates it in the
    v8 shape and the rebuild must not fire on top of that -- one drop, no
    rebuild, and a library that stores the sort straight away."""
    path = tmp_path / "v6-chain.sqlite"
    _v6_database(path)

    conn = db.init(path)
    assert db.get_meta(conn, "schema_version") == str(db.SCHEMA_VERSION)
    assert "rail_sort" in _columns(conn, "views")
    view, _ = views.save_view(conn, "Jita ships", _state("cat:Ship", "location", "owner", "volume"))
    assert views.get_view(conn, view.view_id).state.rail_sort == "volume"
    assert db.migrate(conn) == []


def test_a_v6_database_loses_its_slots_and_gains_the_views_library(tmp_path):
    """The upgrade deliberately throws the nine anonymous slots away rather
    than inventing names for them, so the one thing that must not go wrong is
    throwing away anything else with them: the rail pins and the stored fits
    live in the same section of the schema and have to survive untouched.
    """
    path = tmp_path / "v6.sqlite"
    _v6_database(path)

    conn = db.init(path)
    assert db.get_meta(conn, "schema_version") == str(db.SCHEMA_VERSION)
    assert db.get_meta(conn, "migrated_at") == str(db.SCHEMA_VERSION), "migrate() ran"
    assert not _has_table(conn, "saved_views")
    assert _has_table(conn, "views")
    assert conn.execute("SELECT COUNT(*) FROM views").fetchone()[0] == 0
    assert conn.execute("SELECT label FROM pinned_labels").fetchone()[0] == "Jita IV - Moon 4"
    assert conn.execute("SELECT name FROM fits").fetchone()[0] == "Ratting"
    assert conn.execute("SELECT quantity FROM fit_items").fetchone()[0] == 3

    assert db.migrate(conn) == [], "a migrated database is not migrated twice"
    assert views.list_views(conn) == [], "and the library is usable straight away"
    views.save_view(conn, "Jita ships", _state("cat:Ship loc:Jita"))
    assert [v.name for v in views.list_views(conn)] == ["Jita ships"]


def test_the_migration_reports_the_drop_exactly_once(tmp_path):
    """migrate()'s return value is the upgrade log a reader consults to find
    out what a version bump did, so a silent drop of the user's slots would
    be the worst possible shape for this particular change."""
    path = tmp_path / "v6-report.sqlite"
    _v6_database(path)

    conn = db.connect(path)
    conn.executescript(db.SCHEMA)
    assert db.migrate(conn) == [DROPPED]
    assert db.migrate(conn) == []


def test_a_fresh_database_has_the_library_and_never_had_the_slots(tmp_path):
    """The trigger is the legacy table being present, not `views` being
    absent -- init() runs SCHEMA before migrate(), so a check the other way
    round would never fire at all and this test would pass regardless."""
    conn = db.init(tmp_path / "fresh.sqlite")
    assert _has_table(conn, "views")
    assert not _has_table(conn, "saved_views")
    assert "rail_sort" in _columns(conn, "views")
    assert db.get_meta(conn, "schema_version") == "8"


def test_the_filter_column_answers_to_its_written_name(conn):
    """`filter_text` rather than `filter` because FILTER is an SQLite keyword
    (the window-function clause). Hand-written SQL against the column, in a
    SELECT and an ORDER BY, is the thing that would break if it were ever
    renamed back."""
    views.save_view(conn, "Two", _state("b"))
    views.save_view(conn, "One", _state("a"))
    rows = conn.execute("SELECT filter_text FROM views ORDER BY filter_text").fetchall()
    assert [r[0] for r in rows] == ["a", "b"]


# ------------------------------------------------------------------- store
def test_saving_a_second_time_under_one_name_replaces_and_keeps_the_row(conn):
    """Saving over a name is an edit, not a second view: the row keeps its
    id (so an open card's selection survives), its slot (so re-saving the
    view on digit 3 does not knock it off), its created_at and the spelling
    of the name it was first given."""
    first, replaced = views.save_view(conn, "Jita ships", _state("cat:Ship", "location", "owner"))
    assert not replaced
    views.set_slot(conn, first.view_id, 3)
    conn.execute(
        "UPDATE views SET updated_at = '2000-01-01T00:00:00+00:00' WHERE view_id = ?",
        (first.view_id,),
    )

    second, replaced = views.save_view(conn, "JITA SHIPS", _state("cat:Frigate", "owner", ""))
    assert replaced
    assert second.view_id == first.view_id
    assert second.name == "Jita ships", "the stored spelling wins over the typed one"
    assert second.slot == 3
    assert second.created_at == first.created_at
    assert second.updated_at != "2000-01-01T00:00:00+00:00"
    assert second.state == views.ViewState("cat:Frigate", "owner", "")
    assert len(views.list_views(conn)) == 1


def test_a_view_cannot_be_saved_or_renamed_without_a_name(conn):
    """A nameless view is unaddressable: nothing could load it, and the Save
    card's Done button exists to prevent exactly this."""
    with pytest.raises(ValueError):
        views.save_view(conn, "   ", _state("cat:Ship"))
    view, _ = views.save_view(conn, "Keeper", _state())
    with pytest.raises(ValueError):
        views.rename_view(conn, view.view_id, "")
    assert views.find_view(conn, "Keeper") is not None


def test_renaming_onto_another_views_name_raises_rather_than_an_integrity_error(conn):
    """The card has to name the clash in a sentence a pilot can act on, and
    an IntegrityError's message is a sentence about an index."""
    views.save_view(conn, "Jita ships", _state("cat:Ship"))
    other, _ = views.save_view(conn, "Short of paste", _state("holds:paste<500"))
    with pytest.raises(views.NameTaken) as caught:
        views.rename_view(conn, other.view_id, "jita ships")
    assert caught.value.name == "jita ships"
    assert views.get_view(conn, other.view_id).name == "Short of paste"


def test_renaming_a_view_to_its_own_name_in_another_case_fixes_the_spelling(conn):
    """Case is the one rename that looks like a clash and is not: the row
    found by the NOCASE lookup is the row being renamed."""
    view, _ = views.save_view(conn, "jita ships", _state())
    views.rename_view(conn, view.view_id, "Jita Ships")
    assert views.get_view(conn, view.view_id).name == "Jita Ships"
    assert len(views.list_views(conn)) == 1


def test_a_hostile_name_is_stored_as_text_and_not_executed(conn):
    """Names come from a text field, so the injection attempt has to travel
    as a bound parameter everywhere: the save, the lookup and the rename."""
    hostile = "'; DROP TABLE views; --"
    view, _ = views.save_view(conn, hostile, _state("cat:Ship"))
    assert views.find_view(conn, hostile).view_id == view.view_id
    views.rename_view(conn, view.view_id, hostile + " 2")
    assert _has_table(conn, "views")
    assert views.get_view(conn, view.view_id).name == hostile + " 2"

    imported, _ = views.import_text(conn, "loc:Jita", hostile)
    assert imported.name == hostile and _has_table(conn, "views")


def test_an_awkward_but_honest_name_is_stored_whole(conn):
    """Names come from a free-text field and the Save card exists for the
    long ones, so neither a name that runs to 200 characters nor one carrying
    the quote character the grammar uses may be truncated or mangled."""
    long_name = "N" * 200
    quoted = 'He said "hi"'
    for name in (long_name, quoted):
        view, _ = views.save_view(conn, name, _state("cat:Ship"))
        assert views.get_view(conn, view.view_id).name == name
        assert views.find_view(conn, name.upper()).view_id == view.view_id


def test_find_view_ignores_case_and_the_spaces_around_a_typed_name(conn):
    """`save: Jita ` and `load:jita` mean the stored "Jita": a stray space is
    a typo, not a second view."""
    views.save_view(conn, "Jita", _state("loc:Jita"))
    assert views.find_view(conn, "  jita  ") is not None
    assert views.find_view(conn, "Jit") is None
    assert views.find_view(conn, "") is None


def test_deleting_a_view_frees_the_digit_it_was_holding(conn):
    """The digit is a property of the row, so forgetting the view has to
    leave the key recallable-as-empty rather than pointing at nothing."""
    view, _ = views.save_view(conn, "Jita ships", _state("cat:Ship"))
    views.set_slot(conn, view.view_id, 4)
    views.delete_view(conn, view.view_id)
    assert views.view_in_slot(conn, 4) is None
    assert views.get_view(conn, view.view_id) is None


def test_assigning_a_digit_takes_it_off_whoever_held_it(conn):
    """One digit, one view. The previous holder keeps its name and filter and
    loses the key -- silently, because the row menu that does this is
    an assignment, not a question."""
    first, _ = views.save_view(conn, "First", _state("cat:Ship"))
    second, _ = views.save_view(conn, "Second", _state("cat:Frigate"))
    views.set_slot(conn, first.view_id, 3)
    with db.transaction(conn):
        views.set_slot(conn, second.view_id, 3)
    assert views.view_in_slot(conn, 3).view_id == second.view_id
    assert views.get_view(conn, first.view_id).slot is None
    assert views.get_view(conn, first.view_id).state.filter == "cat:Ship"

    views.set_slot(conn, second.view_id, None)
    assert views.view_in_slot(conn, 3) is None
    assert views.get_view(conn, second.view_id).slot is None


def test_a_digit_outside_one_to_nine_is_refused_before_any_write(conn):
    """The CHECK constraint would catch it, but a ValueError names the
    argument; there are only nine digit keys on the tab."""
    view, _ = views.save_view(conn, "Jita ships", _state())
    for bad in (0, 10, -1):
        with pytest.raises(ValueError):
            views.set_slot(conn, view.view_id, bad)
        with pytest.raises(ValueError):
            views.save_to_slot(conn, bad, _state())
    assert views.get_view(conn, view.view_id).slot is None


def test_the_unique_digit_still_fires_when_a_slot_is_written_directly(conn):
    """set_slot clears the previous holder itself; the UNIQUE constraint is
    the backstop for anything that writes the column another way, and it must
    actually fire rather than leaving two views on one key."""
    first, _ = views.save_view(conn, "First", _state())
    second, _ = views.save_view(conn, "Second", _state())
    views.set_slot(conn, first.view_id, 7)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE views SET slot = 7 WHERE view_id = ?", (second.view_id,))


def test_ctrl_digit_creates_a_named_view_and_then_updates_it_in_place(conn):
    """`Ctrl+3` means "make digit 3 this", not "rename digit 3": the first
    press has to invent a name because a keystroke cannot ask for one, and
    every press after that must leave the name the user gave it alone."""
    created, is_new = views.save_to_slot(conn, 3, _state("cat:Ship", "location", "owner"))
    assert is_new and created.name == "Slot 3" and created.slot == 3

    views.rename_view(conn, created.view_id, "Jita ships")
    updated, is_new = views.save_to_slot(conn, 3, _state("cat:Frigate", "owner", ""))
    assert not is_new
    assert updated.view_id == created.view_id
    assert updated.name == "Jita ships" and updated.slot == 3
    assert updated.state == views.ViewState("cat:Frigate", "owner", "")
    assert len(views.list_views(conn)) == 1


def test_ctrl_digit_adopts_an_unslotted_view_already_called_slot_n(conn):
    """A view named "Slot 3" whose digit was taken away by another view is
    the one case where the invented name is already taken. Adopting it beats
    raising at a keystroke that has nowhere to show the question."""
    stray, _ = views.save_view(conn, "Slot 3", _state("cat:Ship"))
    view, is_new = views.save_to_slot(conn, 3, _state("cat:Frigate"))
    assert not is_new, "the row already existed, so nothing was created"
    assert view.view_id == stray.view_id and view.slot == 3
    assert view.state.filter == "cat:Frigate"
    assert len(views.list_views(conn)) == 1


def test_list_views_puts_the_digits_first_and_sorts_the_rest_by_name(conn):
    """The digit keys are the fast path, so the list a pilot reads answers
    "which digit holds what" before it answers anything else; NULL slots sort
    last despite SQLite's own NULLs-first ordering."""
    for name in ("zeta", "Alpha", "one", "two", "nine"):
        views.save_view(conn, name, _state())
    for name, slot in (("one", 1), ("two", 2), ("nine", 9)):
        views.set_slot(conn, views.find_view(conn, name).view_id, slot)
    assert [v.name for v in views.list_views(conn)] == ["one", "two", "nine", "Alpha", "zeta"]


def test_update_state_moves_the_posture_and_leaves_the_name_and_digit(conn):
    view, _ = views.save_view(conn, "Jita ships", _state("cat:Ship"))
    views.set_slot(conn, view.view_id, 2)
    views.update_state(conn, view.view_id, _state("cat:Frigate", "owner", "region", "name"))
    after = views.get_view(conn, view.view_id)
    assert after.name == "Jita ships" and after.slot == 2
    assert after.state == views.ViewState("cat:Frigate", "owner", "region", "name")


def test_the_rail_sort_round_trips_through_every_reader_and_both_writers(conn):
    """The sort is the fourth column and the one added by a migration, so
    the easy mistake is a reader or a writer that still names three: each
    path the tab uses is walked here with a sort that is not the default."""
    view, _ = views.save_view(conn, "Jita ships", _state("cat:Ship", "location", "owner", "volume"))
    views.set_slot(conn, view.view_id, 3)
    assert views.get_view(conn, view.view_id).state.rail_sort == "volume"
    assert views.find_view(conn, "jita ships").state.rail_sort == "volume"
    assert views.view_in_slot(conn, 3).state.rail_sort == "volume"
    assert views.list_views(conn)[0].state.rail_sort == "volume"

    again, replaced = views.save_view(
        conn, "Jita ships", _state("cat:Ship", "location", "owner", "name")
    )
    assert replaced and again.state.rail_sort == "name", "the upsert writes the sort too"

    slotted, _ = views.save_to_slot(conn, 3, _state("cat:Frigate", "", "", "value"))
    assert slotted.view_id == view.view_id and slotted.state.rail_sort == "value"
    created, _ = views.save_to_slot(conn, 4, _state("cat:Drone", "", "", "volume"))
    assert created.name == "Slot 4" and created.state.rail_sort == "volume"


# ------------------------------------------------------------------ import
def test_import_stores_the_line_the_way_the_omnibox_would_have_written_it(conn):
    """A shared line is hand-typed, so it arrives in whatever spelling its
    author used. Canonicalising through parse/to_text means two people
    pasting the same filter get the same stored view, and the Load card's
    preview reads like the omnibox rather than like someone's typing."""
    view, replaced = views.import_text(conn, 'category:Ship   -owner:Alt', "")
    assert not replaced
    assert view.state.filter == "cat:Ship -owner:Alt"
    assert view.name == "cat Ship · -owner Alt"
    assert view.state == views.ViewState("cat:Ship -owner:Alt", "", "", ""), (
        "only the filter half travels; the posture, sort included, stays unrecorded"
    )


def test_import_under_an_existing_name_replaces_it_and_says_so(conn):
    views.save_view(conn, "Shared", _state("cat:Ship"))
    view, replaced = views.import_text(conn, "loc:Jita", "shared")
    assert replaced and view.state.filter == "loc:Jita"
    assert view.name == "Shared" and len(views.list_views(conn)) == 1


def test_a_line_with_no_filter_in_it_is_nothing_to_import(conn):
    """Blank and whitespace are the obvious cases; a lone quote is the one
    that used to slip through, parsing to an empty spec and storing a view
    that silently meant "everything"."""
    for text in ("", "   ", '"'):
        with pytest.raises(ValueError):
            views.import_text(conn, text, "")
    assert views.list_views(conn) == []


def test_a_very_long_pasted_line_imports_under_a_name_that_still_fits(conn):
    """A 2,000-character paste is a real thing (someone's whole search
    history on one line); the view has to store all of it while the suggested
    name stays a handle rather than a paragraph."""
    long_line = " ".join(f"word{i:04d}" for i in range(250))
    assert len(long_line) > 2000
    view, _ = views.import_text(conn, long_line, "")
    assert view.state.filter == long_line
    assert len(view.name) == 40 and view.name.endswith("…")


# ------------------------------------------------------------------ naming
def test_a_suggested_name_reads_like_the_grammar_it_came_from():
    """The person naming a view has just typed those chips, so the label uses
    the spelling to_text writes -- `loc`, not `location` -- rather than a
    second vocabulary that drifts from the grammar."""
    assert views.suggest_name(omni.parse("")) == views.UNFILTERED
    assert views.suggest_name(omni.parse("cat:Ship")) == "cat Ship"
    assert views.suggest_name(omni.parse("category:Ship loc:Jita")) == "cat Ship · loc Jita"
    assert views.suggest_name(omni.parse("-owner:Alt")) == "-owner Alt"
    assert views.suggest_name(omni.parse("tritanium veldspar")) == "tritanium veldspar"
    assert views.suggest_name(omni.parse("cat:Ship tritanium")) == "cat Ship · tritanium"
    assert views.suggest_name(omni.parse("holds:paste<500")) == "holds paste<500"


def test_a_bare_abyssal_chip_is_named_by_its_bare_word():
    """`abyssal` alone is the one chip whose canonical spelling carries no
    prefix at all, so a name of `abyssal ` with an empty value would be a
    label with a hole in it."""
    assert views.suggest_name(omni.parse("abyssal")) == "abyssal"
    assert views.suggest_name(omni.parse("-abyssal")) == "-abyssal"
    assert views.suggest_name(omni.parse('abyssal:"Damage Control"')) == "abyssal Damage Control"


def test_a_suggested_name_stops_at_three_parts_and_forty_characters():
    """A name is a handle; the row underneath shows the filter in full. Both
    limits are visible in one label so neither can quietly stop applying."""
    many = views.suggest_name(omni.parse("cat:Ship loc:Jita owner:Main is:bpc"))
    assert many == "cat Ship · loc Jita · owner Main", "the fourth chip is dropped"
    long_one = views.suggest_name(omni.parse('loc:"Jita IV - Moon 4 - Caldari Navy Assembly"'))
    assert len(long_one) == 40 and long_one.endswith("…")
    assert long_one.startswith("loc Jita IV")


# -------------------------------------------------------------------- diff
def test_the_diff_lists_drops_then_shared_then_additions_in_source_order():
    """The Load card shows the diff so a load is never a surprise, and the
    drops lead because they are the cost: the chips the user is about to
    lose. Within each group the filter's own order is kept, so the chips
    read as they sit in the omnibox rather than in some sorted order."""
    diff = views.diff_tokens("cat:Ship owner:Main loc:Jita", "loc:Jita item:Caracal cat:Ship")
    assert diff == [
        ("-", omni.Chip("owner", "Main")),
        ("=", omni.Chip("category", "Ship")),
        ("=", omni.Chip("location", "Jita")),
        ("+", omni.Chip("item", "Caracal")),
    ]


def test_a_negated_chip_is_a_different_token_from_its_positive_twin():
    """`-owner:Main` and `owner:Main` are opposite filters; treating them as
    one changed token would hide the very flip a load performs."""
    diff = views.diff_tokens("owner:Main", "-owner:Main")
    assert diff == [("-", omni.Chip("owner", "Main")), ("+", omni.Chip("owner", "Main", True))]


def test_bare_words_diff_as_words_and_never_equal_a_chip():
    """A search word is a token of its own kind: the omnibox carries it in
    the line edit rather than as a chip, and it still counts as a filter."""
    diff = views.diff_tokens("paste owner:Main", "paste cat:Ship")
    assert diff == [
        ("-", omni.Chip("owner", "Main")),
        ("=", "paste"),
        ("+", omni.Chip("category", "Ship")),
    ]


def test_identical_lines_diff_to_shared_tokens_only_and_blank_lines_to_nothing():
    """The card turns an all-"=" diff into "Identical to the current filter",
    which only works if identical lines really yield no "-" or "+"; and two
    empty filters must not invent a token to compare."""
    same = views.diff_tokens('cat:Ship loc:"Jita IV"', 'loc:"Jita IV" cat:Ship')
    assert [sign for sign, _token in same] == ["=", "="]
    assert views.diff_tokens("", "") == []
    assert views.diff_tokens("", "owner:Main") == [("+", omni.Chip("owner", "Main"))]
    assert views.diff_tokens("owner:Main", "") == [("-", omni.Chip("owner", "Main"))]


def test_a_repeated_token_diffs_once():
    """The filter treats `cat:Ship cat:Ship` as one chip, so the diff must
    too, or the load would appear to drop a chip that was never there twice."""
    assert views.diff_tokens("cat:Ship cat:Ship", "cat:Ship") == [
        ("=", omni.Chip("category", "Ship"))
    ]
