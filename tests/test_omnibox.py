"""The omnibox widget: chips, the escape ladder, token migration, completion.

The omnibox is the single owner of the Assets tab's filter state, so the
behaviours pinned here are the ones the rest of the tab builds on: a chip
that renders but never announces itself would filter nothing, an escape that
skips a ladder rung would throw away more state than the user meant to, and
a set_spec that emits per chip would fire one table reload per chip while a
saved view restores.
"""

from __future__ import annotations

import pytest

from evasset import db, omni

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QModelIndex, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from evasset.ui.omnibox import Omnibox  # noqa: E402


@pytest.fixture
def app():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def box(app):
    """An Omnibox whose completion lookups never run -- these tests exercise
    chips and keys, and a real AsyncQuery would race a worker thread against
    the assertions and hit the wrong database (the same reasoning as
    test_assets_integration's inline-query fixture)."""
    b = Omnibox()
    b._complete_query.run = lambda fn, on_done, on_failed=None: None
    yield b
    b.deleteLater()


def record(signal) -> list:
    calls: list = []
    signal.connect(lambda *args: calls.append(args))
    return calls


# ------------------------------------------------------------------- chips
def test_add_chip_renders_a_chip_and_announces_it(box):
    """chip_added is what integration listens on to sync outside state, and
    changed is what triggers the reload -- swallowing either leaves a chip
    that looks applied but filters nothing."""
    changed = record(box.changed)
    added = record(box.chip_added)

    box.add_chip("category", "Mineral")

    assert len(changed) == 1
    assert len(added) == 1
    chip = added[0][0]
    assert (chip.kind, chip.value, chip.negated) == ("category", "Mineral", False)
    assert len(box._chips) == 1
    widget = box._chips[0][1]
    assert widget.prefix_label.text() == "cat:"
    assert widget.value_label.text() == "Mineral"


def test_adding_the_same_chip_twice_is_a_no_op(box):
    """Rail rows and context menus re-send the same filter freely, so a
    duplicate must neither stack a second widget nor fire a reload."""
    box.add_chip("owner", "Main")
    changed = record(box.changed)
    added = record(box.chip_added)

    box.add_chip("owner", "Main")

    assert len(box._chips) == 1
    assert not changed and not added

    # A negated twin is a different filter, not a duplicate.
    box.add_chip("owner", "Main", negated=True)
    assert len(box._chips) == 2
    assert len(changed) == 1 and len(added) == 1


def test_the_cross_button_removes_its_chip_and_emits_changed(box):
    box.add_chip("region", "The Forge")
    box.add_chip("owner", "Main")
    changed = record(box.changed)

    box._chips[0][1].close_btn.click()

    assert [c.value for c, _w in box._chips] == ["Main"]
    assert len(changed) == 1


# ----------------------------------------------------------------- escape
def test_escape_ladders_text_then_chips_then_the_escape_signal(box):
    """Each press must undo exactly one visible layer -- an escape that fired
    escape_pressed while chips were still applied would close the panel the
    user was actually trying to prune chips from."""
    box.add_chip("group", "Battleship")
    box.edit.setText("drones")
    changed = record(box.changed)
    escapes = record(box.escape_pressed)

    QTest.keyClick(box.edit, Qt.Key_Escape)
    assert box.edit.text() == ""
    assert len(box._chips) == 1
    assert len(changed) == 1 and not escapes

    QTest.keyClick(box.edit, Qt.Key_Escape)
    assert not box._chips
    assert len(changed) == 2 and not escapes

    QTest.keyClick(box.edit, Qt.Key_Escape)
    assert len(changed) == 2
    assert len(escapes) == 1


# --------------------------------------------------------- token migration
def test_committing_typed_tokens_turns_them_into_chips_and_keeps_bare_text(box):
    changed = record(box.changed)
    added = record(box.chip_added)
    box.edit.setText("cat:Mineral trit")

    QTest.keyClick(box.edit, Qt.Key_Return)

    assert box.edit.text() == "trit"
    assert [c for c, _w in box._chips] == [omni.Chip("category", "Mineral")]
    assert len(added) == 1
    assert len(changed) == 1


def test_a_trailing_space_commits_tokens_mid_typing(box):
    """Space is the natural commit while typing several tokens in a row; a
    chip appearing only on Enter would leave 'loc:Jita cat:Ship' sitting as
    inert bare text until the very end."""
    QTest.keyClicks(box.edit, "group:Battleship ")

    assert box.edit.text() == ""
    assert [c for c, _w in box._chips] == [omni.Chip("group", "Battleship")]


def test_a_space_inside_an_open_quote_does_not_commit(box):
    """Quoted values exist precisely for spaces, so committing on the first
    space inside one would make quoted station names untypeable."""
    QTest.keyClicks(box.edit, 'loc:"Jita ')

    assert not box._chips
    assert box.edit.text() == 'loc:"Jita '


def test_enter_inside_an_open_quote_does_not_commit_either(box):
    """Adversarial-review regression: Enter used to migrate unconditionally,
    minting a chip whose value carried an invisible trailing space that
    exact-matched nothing. Both commit paths must honour an open quote."""
    box.edit.setText('loc:"Jita IV ')

    QTest.keyClick(box.edit, Qt.Key_Return)

    assert not box._chips
    assert box.edit.text() == 'loc:"Jita IV '


# ---------------------------------------------------------- spec round-trip
def test_set_spec_rebuilds_and_round_trips_with_exactly_one_changed(box):
    box.add_chip("owner", "Alt")  # must be replaced wholesale, not merged
    spec = omni.FilterSpec(
        text="tritanium",
        chips=[omni.Chip("owner", "Main"), omni.Chip("category", "Ship", negated=True)],
    )
    changed = record(box.changed)
    added = record(box.chip_added)

    box.set_spec(spec)

    assert len(changed) == 1
    assert not added
    assert box.spec() == spec
    assert box.edit.text() == "tritanium"
    # The negated chip is visibly negated, not just internally flagged.
    assert [w.prefix_label.text() for _c, w in box._chips] == ["owner:", "-cat:"]


def test_clear_empties_chips_and_text_with_a_single_changed(box):
    box.add_chip("system", "Jita")
    box.add_chip("meta", "Tech II")
    box.edit.setText("dominix")
    changed = record(box.changed)

    box.clear()

    assert not box._chips
    assert box.edit.text() == ""
    assert box.spec().is_empty
    assert len(changed) == 1


# ------------------------------------------------------------- completion
def settle(box) -> None:
    """Let the completion debounce fire now, the way a pause in typing would."""
    box._complete_debounce.flush()


def test_completion_lookups_skip_is_val_and_bare_text(app):
    """is: has a fixed vocabulary and val: is a comparison the user writes,
    so neither may cost a database round-trip once the typing rests."""
    b = Omnibox()
    lookups = []
    b._complete_query.run = lambda fn, on_done, on_failed=None: lookups.append(fn)

    QTest.keyClicks(b.edit, "is:fitted")
    settle(b)
    b.edit.clear()
    QTest.keyClicks(b.edit, "val:>10m")
    settle(b)
    b.edit.clear()
    QTest.keyClicks(b.edit, "dominix")
    settle(b)
    assert not lookups

    # The spy must be proven live, or the assertions above are vacuous:
    # "cat:M" completes, as one lookup for the whole burst.
    b.edit.clear()
    QTest.keyClicks(b.edit, "cat:M")
    settle(b)
    assert len(lookups) == 1
    b.deleteLater()


def test_a_burst_of_keystrokes_runs_one_completion_lookup_once_the_typing_rests(app):
    """Per keystroke, one typed line launched 58 pool queries and the
    generation guard threw 46 of them away; the guard keeps stale answers
    off the screen, the debounce keeps the work off the pool. A commit or a
    cleared field before the clock runs out cancels the lookup outright."""
    b = Omnibox()
    lookups = []
    b._complete_query.run = lambda fn, on_done, on_failed=None: lookups.append(fn)

    QTest.keyClicks(b.edit, "cat:Mat")
    assert lookups == [], "nothing runs while the keys are still coming"
    assert b._complete_debounce._timer.isActive()

    settle(b)
    assert len(lookups) == 1, "the burst is one query, for the token as it stands"
    settle(b)
    assert len(lookups) == 1, "and firing again with nothing pending runs nothing"

    QTest.keyClicks(b.edit, "e")
    assert b._complete_debounce._timer.isActive()
    QTest.keyClick(b.edit, Qt.Key_Return)
    assert not b._complete_debounce._timer.isActive(), "Enter cancels the pending lookup"
    settle(b)
    assert len(lookups) == 1
    b.deleteLater()


@pytest.fixture
def conn(tmp_path):
    """Two mineral stacks and one ship: 'cat:Mat' must offer exactly one
    value, and its count must be a real aggregate over stacks, not a row
    echo. Ids are the CCP constants (category 4 Material, group 18 Mineral,
    types 34/35 Tritanium/Pyerite, 645 Dominix)."""
    c = db.init(tmp_path / "omnibox.sqlite")
    c.executescript(
        """
        INSERT INTO sde_regions VALUES (10000002,'The Forge');
        INSERT INTO sde_systems VALUES (30000142,'Jita',20000020,10000002,0.9);
        INSERT INTO sde_stations VALUES (60003760,'Jita IV - Moon 4',30000142,10000002);
        INSERT INTO sde_categories VALUES (4,'Material',1),(6,'Ship',1);
        INSERT INTO sde_groups VALUES (18,4,'Mineral',1),(27,6,'Battleship',1);
        INSERT INTO sde_types (type_id,name,group_id,volume,portion_size,base_price,published)
            VALUES (34,'Tritanium',18,0.01,100,2,1),
                   (35,'Pyerite',18,0.01,100,8,1),
                   (645,'Dominix',27,454500,1,153900000,1);
        INSERT INTO characters(character_id,name,enabled) VALUES (1,'Main',1);
        INSERT INTO assets(owner_type,owner_id,item_id,type_id,quantity,location_id,
                           location_flag,location_type,is_singleton,is_blueprint_copy,
                           root_location_id,system_id,region_id) VALUES
            ('character',1,1001,34,1000,60003760,'Hangar','station',0,0,
             60003760,30000142,10000002),
            ('character',1,1002,35,500,60003760,'Hangar','station',0,0,
             60003760,30000142,10000002),
            ('character',1,1003,645,1,60003760,'Hangar','station',1,0,
             60003760,30000142,10000002);
        """
    )
    return c


def test_typing_a_token_offers_counted_values_and_picking_adds_the_chip(app, conn):
    """The popup's whole contract: candidates for the token being typed, a
    stack count per value, and a pick that becomes a chip directly rather
    than text the user still has to commit."""
    b = Omnibox()
    b._complete_query.run = lambda fn, on_done, on_failed=None: on_done(fn(conn))

    QTest.keyClicks(b.edit, "cat:Mat")
    settle(b)

    model = b._completion_model
    assert model.rowCount() == 1
    item = model.item(0)
    assert item.data(Qt.UserRole) == "Material"
    assert "Material" in item.text() and "2" in item.text()

    added = record(b.chip_added)
    b._completer.activated[QModelIndex].emit(model.index(0, 0))

    assert [c for c, _w in b._chips] == [omni.Chip("category", "Material")]
    assert len(added) == 1
    assert b.edit.text() == ""

    # A negated token completes too, and picking keeps the negation.
    QTest.keyClicks(b.edit, "-cat:Mat")
    settle(b)
    assert model.rowCount() == 1
    b._completer.activated[QModelIndex].emit(model.index(0, 0))
    assert b._chips[-1][0] == omni.Chip("category", "Material", negated=True)
    b.deleteLater()


def test_stat_completion_offers_internal_names_when_a_display_name_is_shared(app, conn):
    """Real SDE shape: signatureRadiusBonus (%) and signatureRadiusAdd (m)
    both display "Signature Radius Modifier". Writing that display name into
    the field would mint a chip matching either attribute, so the popup must
    offer each internal name, annotated so the two can be told apart, while
    an unshared attribute still completes to its display name. The `sig`
    alias points at the percent one and must land on its internal name too,
    once, not beside a duplicate row for the same name."""
    conn.executescript(
        """
        INSERT INTO sde_dogma_attributes
            (attribute_id,name,display_name,unit_id,high_is_good,default_value,published) VALUES
            (50,'cpu','CPU usage',106,0,0,1),
            (554,'signatureRadiusBonus','Signature Radius Modifier',124,0,0,1),
            (983,'signatureRadiusAdd','Signature Radius Modifier',1,0,0,1);
        INSERT INTO sde_dogma_units VALUES (106,'Teraflops','tf'),
            (124,'Modifier Relative Percent','%'),(1,'Length','m');
        INSERT INTO sde_mutator_ranges VALUES (47297,50,0.8,1.5,NULL,47408),
            (47297,554,0.7,1.3,NULL,47408),(47297,983,0.7,1.3,NULL,47408),
            (47741,554,0.8,1.1,NULL,47408);
        """
    )
    b = Omnibox()
    b._complete_query.run = lambda fn, on_done, on_failed=None: on_done(fn(conn))
    model = b._completion_model

    def offered():
        return [model.item(i).data(Qt.UserRole) for i in range(model.rowCount())]

    QTest.keyClicks(b.edit, "stat:Signature")
    settle(b)
    assert offered() == ["signatureRadiusAdd", "signatureRadiusBonus"]
    assert model.item(0).text() == "signatureRadiusAdd  (Signature Radius Modifier, m)"
    assert model.item(1).text() == "signatureRadiusBonus  (Signature Radius Modifier, %)"

    b._completer.activated[QModelIndex].emit(model.index(1, 0))
    assert b.edit.text() == "stat:signatureRadiusBonus"
    assert b._chips == [], "a name is half a value; no chip until the operator and number"

    b.edit.clear()
    QTest.keyClicks(b.edit, "stat:cp")
    settle(b)
    assert offered() == ["CPU usage"], "an unshared name still completes to its display name"
    assert model.item(0).text() == "CPU usage  (cpu)"

    b.edit.clear()
    QTest.keyClicks(b.edit, "stat:si")
    settle(b)
    assert offered() == ["signatureRadiusBonus", "signatureRadiusAdd"]
    assert model.item(0).text() == "signatureRadiusBonus  (sig · Signature Radius Modifier, %)"
    b.deleteLater()


# ----------------------------------------------------------- draft builder
def open_draft(box):
    box.open_draft()
    return box._draft


def test_the_draft_builder_walks_kind_then_value_into_a_real_chip(box):
    """Ctrl+F's flow: type the first letters of the kind, Enter, type the
    value, Enter -- the committed result must be indistinguishable from a
    typed token, or the builder teaches a second, different grammar."""
    changed = record(box.changed)
    added = record(box.chip_added)
    draft = open_draft(box)

    QTest.keyClicks(draft.edit, "gro")
    QTest.keyClick(draft.edit, Qt.Key_Return)
    assert draft.kind == "group"
    assert draft.prefix_label.text() == "group:"

    QTest.keyClicks(draft.edit, "Battleship")
    QTest.keyClick(draft.edit, Qt.Key_Return)
    assert box._draft is None, "committing must close the card"
    assert [c for c, _w in box._chips] == [omni.Chip("group", "Battleship")]
    assert len(added) == 1 and len(changed) == 1


def test_the_draft_resolves_short_aliases_and_leading_minus_negates(box):
    draft = open_draft(box)
    QTest.keyClicks(draft.edit, "-cat")
    QTest.keyClick(draft.edit, Qt.Key_Return)
    assert (draft.kind, draft.negated) == ("category", True)
    assert draft.prefix_label.text() == "-cat:"

    QTest.keyClicks(draft.edit, "Mineral")
    QTest.keyClick(draft.edit, Qt.Key_Return)
    assert [c for c, _w in box._chips] == [omni.Chip("category", "Mineral", negated=True)]


def test_escape_abandons_the_draft_without_side_effects(box):
    """A cancelled draft must leave no chip, no changed(), no card."""
    changed = record(box.changed)
    draft = open_draft(box)
    QTest.keyClicks(draft.edit, "loc")

    QTest.keyClick(draft.edit, Qt.Key_Escape)

    assert box._draft is None
    assert not box._chips
    assert not changed


def test_backspace_on_an_empty_value_steps_back_to_the_kind_stage(box):
    """Choosing the wrong kind must cost one keypress, not the whole card."""
    draft = open_draft(box)
    QTest.keyClicks(draft.edit, "owner")
    QTest.keyClick(draft.edit, Qt.Key_Return)
    assert draft.kind == "owner"

    QTest.keyClick(draft.edit, Qt.Key_Backspace)

    assert draft.kind is None
    assert not draft.prefix_label.isVisible()


def test_an_ambiguous_or_unknown_kind_does_not_advance(box):
    """"i" could be is or item and "z" is nothing -- Enter must hold the
    card at the kind stage rather than guessing."""
    draft = open_draft(box)
    QTest.keyClicks(draft.edit, "z")
    QTest.keyClick(draft.edit, Qt.Key_Return)
    assert draft.kind is None

    draft.edit.clear()
    QTest.keyClicks(draft.edit, "i")
    QTest.keyClick(draft.edit, Qt.Key_Return)
    assert draft.kind is None


def test_is_kind_offers_the_flag_vocabulary_and_commits_one(box):
    """The is: flags are a fixed vocabulary, so the value stage must list
    them without any database round trip."""
    draft = open_draft(box)
    QTest.keyClicks(draft.edit, "is")
    QTest.keyClick(draft.edit, Qt.Key_Return)

    labels = [
        draft._value_model.item(i).data(Qt.UserRole)
        for i in range(draft._value_model.rowCount())
    ]
    assert labels == list(omni.IS_FLAGS)

    QTest.keyClicks(draft.edit, "unpriced")
    QTest.keyClick(draft.edit, Qt.Key_Return)
    assert [c for c, _w in box._chips] == [omni.Chip("is", "unpriced")]


def test_the_plus_button_opens_the_draft_and_reopening_refocuses(box):
    box.add_btn.click()
    first = box._draft
    assert first is not None

    box.open_draft()
    assert box._draft is first, "a second open must refocus, not stack cards"


def test_the_draft_card_is_laid_out_before_its_kind_popup_opens(box, app):
    """Reported defect: open_draft() opened the popup before the layout pass
    had placed the card, so QCompleter anchored it to empty geometry and the
    list floated halfway down the table. The popup now waits one event-loop
    turn, by which point the card must have a real position and size. (The
    popup's own final x/y is not asserted: the offscreen platform's screen
    geometry degenerates inside QCompleter's placement math, the same reason
    the keyboard tests call handlers directly.) The completer must also list
    every kind at once, the two commands included -- the default of seven
    visible rows scrolled item/is/val out of sight."""
    from evasset.ui.omnibox import _DRAFT_KINDS

    box.resize(900, 44)
    box.show()
    app.processEvents()

    box.open_draft()
    app.processEvents()  # the layout pass, then the deferred popup open
    draft = box._draft
    assert draft.width() > 100 and draft.height() > 0, "card not laid out at popup time"
    assert draft.x() > 0, "card still sitting at the parent origin"
    assert draft.edit.width() > 0

    completer = draft._kind_completer
    assert completer.popup().isVisible(), "the deferred popup must have opened by now"
    assert completer.maxVisibleItems() >= len(_DRAFT_KINDS)
    assert completer.completionCount() == len(_DRAFT_KINDS)


def test_item_tokens_complete_in_the_main_field(app):
    """Style-audit regression: item was missing from the prefix map, so a
    typed "item:Dom" silently offered nothing while the draft builder
    completed the very same values."""
    b = Omnibox()
    lookups = []
    b._complete_query.run = lambda fn, on_done, on_failed=None: lookups.append(fn)

    QTest.keyClicks(b.edit, "item:Dom")
    settle(b)

    assert lookups, "an item: token must trigger a value lookup"
    b.deleteLater()


# ---------------------------------------------------------------- wrapping
_STATIONS = [
    "Amarr VIII (Oris) - Emperor Family Academy",
    "Rens VI - Moon 8 - Brutor Tribe Treasury",
    "Jita IV - Moon 4 - Caldari Navy Assembly Plant",
    "Hek VIII - Moon 12 - Boundless Creation Factory",
    "Dodixie IX - Moon 20 - Federation Navy Assembly Plant",
    "Sobaseki VII - Moon 1 - Caldari Navy Assembly Plant",
]


def test_many_chips_wrap_instead_of_widening_the_field(box):
    """Six station chips once demanded a row wider than a full-screen window
    and shoved the rail and export button off the right edge. The field's
    minimum width must stay independent of how many chips it holds, and at a
    realistic width the chips must occupy several lines."""
    box.add_chip("location", _STATIONS[0])
    one_line = box.heightForWidth(1400)

    for station in _STATIONS[1:]:
        box.add_chip("location", station)

    chip_widths = [widget.minimumSizeHint().width() for _chip, widget in box._chips]
    margins = box._row.contentsMargins()
    # Bounded by the widest single chip plus the field's own margins, never
    # by the chips' sum. The bound is tight: the layout reads exactly these
    # minimum sizes, so equality is the expected outcome.
    assert box.minimumSizeHint().width() <= max(chip_widths) + margins.left() + margins.right()
    assert box.minimumSizeHint().width() < sum(chip_widths)
    assert box.heightForWidth(1400) > one_line
    # 6000 px is wider than six station chips laid end to end even in the
    # offscreen platform's wide fallback glyphs.
    assert box.heightForWidth(1400) > box.heightForWidth(6000) == one_line


def test_the_edit_keeps_a_typable_width_and_the_hint_stays_beside_it(app, box):
    """After wrapping, the line edit must still be wide enough to type in and
    the keyboard hint must sit on the edit's own line, at its right -- an
    edit squeezed to a few pixels at the end of a chip line would be the same
    bug in a smaller frame."""
    for station in _STATIONS:
        box.add_chip("location", station)
    box.resize(1400, box.heightForWidth(1400))
    box.show()
    app.processEvents()

    chip_widgets = [widget for _chip, widget in box._chips]
    assert box.edit.width() >= box._row._fill_min_width
    assert box.edit.geometry().top() > chip_widgets[0].geometry().top()
    # Same line, allowing for the layout centring two widgets of different
    # heights on it.
    assert box.hint.geometry().top() == pytest.approx(box.edit.geometry().top(), abs=6)
    assert box.hint.geometry().left() > box.edit.geometry().right()
    # Every chip stays inside the field's own frame.
    for widget in chip_widgets:
        assert widget.geometry().right() <= box.width()
    box.hide()


# ------------------------------------------------------------ abyssal chip
AMMO = "Antimatter Charge M"
WEBIFIER = "Abyssal Stasis Webifier"
THREE_TYPES = f"{WEBIFIER}, Abyssal Warp Disruptor, 50MN Abyssal Microwarpdrive"


def test_the_abyssal_chip_renders_its_three_label_shapes_with_the_prefix_hidden(box):
    """The chip's word IS the kind, so a muted "abyssal:" in front would say
    it twice; one type shows without its own "Abyssal" word for the same
    reason, and several collapse to a count so three long names cannot eat
    the whole field."""
    box.add_chip("abyssal", "")
    box.add_chip("abyssal", WEBIFIER)
    box.add_chip("abyssal", THREE_TYPES)
    widgets = [w for _c, w in box._chips]
    assert [w.value_label.text() for w in widgets] == [
        "Abyssal", "Abyssal · Stasis Webifier", "Abyssal · 3 types",
    ]
    assert all(w.prefix_label.isHidden() for w in widgets)

    # A negated one keeps only the minus: the wash already went red.
    box.add_chip("abyssal", WEBIFIER, negated=True)
    negated = box._chips[-1][1]
    assert negated.prefix_label.text() == "-" and not negated.prefix_label.isHidden()
    assert negated.value_label.text() == "Abyssal · Stasis Webifier"


def test_only_the_card_kinds_carry_the_card_glyph(box):
    """The glyph says "there is more to this chip than a value you type".
    Three kinds have a card; stat: is edited inside the abyssal one and has
    no card of its own, so it must not grow a glyph that opens nothing."""
    from evasset.ui.omnibox import CARD_CHIP_KINDS

    box.add_chip("category", "Mineral")
    box.add_chip("stat", "cpu<30")
    box.add_chip("abyssal", "")
    box.add_chip(CARD_CHIP_KINDS[1], f'{AMMO}<500')     # holds
    box.add_chip(CARD_CHIP_KINDS[2], "Ratting")         # fit
    plain, stat, abyssal_chip, holds, fit = (w for _c, w in box._chips)
    assert plain.card_btn is None and stat.card_btn is None
    assert [w.card_btn.text() for w in (abyssal_chip, holds, fit)] == ["▾"] * 3
    # One tooltip per kind: three cards that all said "Refine" would teach
    # nothing about which one opens.
    tips = [w.card_btn.toolTip() for w in (abyssal_chip, holds, fit)]
    assert len(set(tips)) == 3 and all(tips)
    assert not plain.prefix_label.isHidden(), "ordinary chips keep their prefix"
    # The holds and fit chips keep their prefix too -- only abyssal's label
    # already is its kind.
    assert holds.prefix_label.text() == "holds:" and fit.prefix_label.text() == "fit:"


def test_the_glyph_asks_for_the_card_with_the_chip_and_its_anchor(box):
    """The omnibox knows nothing about the card's contents; it hands the
    view the chip to seed from and the widget to anchor the popover under."""
    box.add_chip("abyssal", WEBIFIER)
    requests = record(box.card_requested)
    chip, widget = box._chips[0]

    widget.card_btn.click()

    assert len(requests) == 1
    assert requests[0][0] == chip
    assert requests[0][1] is widget


def test_typed_abyssal_tokens_mint_the_chip_in_both_spellings(box):
    """The bare word and the prefixed form must render through the same
    label logic -- a typed abyssal: with a quoted type is what a saved view
    replays."""
    QTest.keyClicks(box.edit, "abyssal ")
    assert [c for c, _w in box._chips] == [omni.Chip("abyssal", "")]
    assert box._chips[0][1].value_label.text() == "Abyssal"

    QTest.keyClicks(box.edit, f'abyssal:"{WEBIFIER}" ')
    assert box._chips[-1][0] == omni.Chip("abyssal", WEBIFIER)
    assert box._chips[-1][1].value_label.text() == "Abyssal · Stasis Webifier"


def abyssal_widget(box):
    return next(w for c, w in box._chips if c.kind == omni.ABYSSAL_KIND)


def test_a_typed_abyssal_chip_asks_for_the_card_one_event_turn_after_enter(box, app):
    """Typing the word and pressing Enter is the natural way in, so the card
    request goes out without a glyph click -- but only after the event
    turn that inserted the chip widget, so the card anchors to a chip the
    layout has placed rather than one still at its default geometry."""
    requests = record(box.card_requested)
    QTest.keyClicks(box.edit, "abyssal")
    QTest.keyClick(box.edit, Qt.Key_Return)
    assert [c for c, _w in box._chips] == [omni.Chip("abyssal", "")]
    assert requests == [], "deferred, not in the turn that laid the chip out"

    app.processEvents()

    assert len(requests) == 1
    assert requests[0][0] == omni.Chip("abyssal", "")
    assert requests[0][1] is abyssal_widget(box)

    # The alias and the typed type list are the same chip kind and open too.
    box.clear()
    QTest.keyClicks(box.edit, "is:abyssal")
    QTest.keyClick(box.edit, Qt.Key_Return)
    app.processEvents()
    assert len(requests) == 2 and requests[1][0] == omni.Chip("abyssal", "")

    box.clear()
    QTest.keyClicks(box.edit, f'abyssal:"{WEBIFIER}"')
    QTest.keyClick(box.edit, Qt.Key_Return)
    app.processEvents()
    assert len(requests) == 3 and requests[2][0] == omni.Chip("abyssal", WEBIFIER)


def test_the_draft_builder_committing_an_abyssal_chip_asks_for_the_card_too(box, app):
    requests = record(box.card_requested)
    draft = open_draft(box)
    QTest.keyClicks(draft.edit, "a")
    # One Return: the kind pick mints the chip, there is no value stage.
    QTest.keyClick(draft.edit, Qt.Key_Return)
    assert [c for c, _w in box._chips] == [omni.Chip("abyssal", "")]
    assert requests == []
    app.processEvents()
    assert len(requests) == 1 and requests[0][1] is abyssal_widget(box)


def test_an_abyssal_chip_that_is_restored_placed_negated_or_already_present_opens_no_card(
    box, app
):
    """The card is asked for only by a chip the user has just typed: a
    saved view or the card's own Done restores one through set_spec, a rail
    row places one through add_chip, a trailing space is mid-sentence, a
    negated chip is one the card cannot express, and a chip already in the
    row (the card may be open under it) is a duplicate the field drops."""
    requests = record(box.card_requested)

    box.set_spec(omni.parse(f'abyssal:"{WEBIFIER}" roll:web>=70'))
    app.processEvents()
    box.add_chip("abyssal", "")
    app.processEvents()
    assert requests == []

    box.clear()
    QTest.keyClicks(box.edit, "abyssal ")
    assert [c for c, _w in box._chips] == [omni.Chip("abyssal", "")]
    app.processEvents()
    assert requests == [], "a space commits the token but the user is still typing"

    QTest.keyClicks(box.edit, "abyssal")
    QTest.keyClick(box.edit, Qt.Key_Return)
    app.processEvents()
    assert [c for c, _w in box._chips] == [omni.Chip("abyssal", "")]
    assert requests == [], "the chip was already there"

    box.clear()
    QTest.keyClicks(box.edit, "-abyssal")
    QTest.keyClick(box.edit, Qt.Key_Return)
    app.processEvents()
    assert [c for c, _w in box._chips] == [omni.Chip("abyssal", "", negated=True)]
    assert requests == []


def test_a_chip_removed_before_the_deferred_request_fires_opens_no_card(box, app):
    """The timer outlives the chip when a set_spec or the cross lands in the
    same turn; a card anchored to a deleted widget would be a crash."""
    requests = record(box.card_requested)
    QTest.keyClicks(box.edit, "abyssal")
    QTest.keyClick(box.edit, Qt.Key_Return)
    box.set_spec(omni.parse("cat:Module"))
    app.processEvents()
    assert requests == []
    assert [c for c, _w in box._chips] == [omni.Chip("category", "Module")]


def test_the_draft_builder_offers_abyssal_and_roll_and_mints_abyssal_without_a_value_stage(box):
    """"a" resolves to abyssal alone, and picking it mints the whole-kind
    chip at once: the module type is chosen in the card that opens on the
    chip, so a value stage here would have asked the same question twice."""
    from evasset.ui.omnibox import _ALL_KINDS

    assert "abyssal" in _ALL_KINDS and "roll" in _ALL_KINDS
    draft = open_draft(box)
    QTest.keyClicks(draft.edit, "a")
    QTest.keyClick(draft.edit, Qt.Key_Return)

    assert box._draft is None
    assert [c for c, _w in box._chips] == [omni.Chip("abyssal", "")]


def test_one_commit_opens_exactly_one_card_however_many_kinds_were_minted(box, app):
    """Three cards fighting over one Enter would each close the last as an
    outside click and only the survivor would be seen, so a commit that
    mints several card chips asks for one card: the first one typed."""
    from evasset.ui.omnibox import CARD_CHIP_KINDS

    holds_kind, fit_kind = CARD_CHIP_KINDS[1], CARD_CHIP_KINDS[2]
    minted = [
        omni.Chip(omni.ABYSSAL_KIND, ""),
        omni.Chip(holds_kind, f"{AMMO}<500"),
        omni.Chip(fit_kind, "Ratting"),
    ]
    for chip in minted:
        box.add_chip(chip.kind, chip.value, chip.negated)
    requests = record(box.card_requested)

    box._request_card_later(minted)
    app.processEvents()

    assert len(requests) == 1
    assert requests[0][0] == minted[0]
    # And each of the three qualifies on its own -- the rule is one per
    # commit, not "abyssal wins".
    for chip in minted[1:]:
        box._request_card_later([chip])
        app.processEvents()
    assert [r[0] for r in requests] == minted


def test_a_negated_card_chip_still_opens_no_card(box, app):
    from evasset.ui.omnibox import CARD_CHIP_KINDS

    requests = record(box.card_requested)
    chip = omni.Chip(CARD_CHIP_KINDS[2], "Ratting", negated=True)
    box.add_chip(chip.kind, chip.value, chip.negated)
    box._request_card_later([chip])
    app.processEvents()
    assert requests == []


@pytest.mark.parametrize("typed", ["h", "fi"])
def test_the_draft_builder_asks_for_the_holds_and_fit_cards_without_minting(box, typed):
    """Neither kind has a bare form worth minting: an empty holds chip has
    nothing to compare and an empty fit chip names no fit, so the draft
    hands the view an empty chip as a request and inserts nothing. The card
    anchors on the omnibox itself, there being no chip widget yet."""
    from evasset.ui.omnibox import _ALL_KINDS

    assert "holds" in _ALL_KINDS and "fit" in _ALL_KINDS
    requests = record(box.card_requested)
    draft = open_draft(box)
    QTest.keyClicks(draft.edit, typed)
    QTest.keyClick(draft.edit, Qt.Key_Return)

    assert box._draft is None, "the draft closes; the card takes over"
    assert box._chips == [], "nothing is inserted until the card's Done"
    assert len(requests) == 1
    chip, anchor = requests[0]
    assert chip.value == "" and not chip.negated
    assert chip.kind == ("holds" if typed == "h" else "fit")
    assert anchor is box


def test_a_negated_draft_of_a_card_kind_carries_the_minus_into_the_request(box):
    requests = record(box.card_requested)
    draft = open_draft(box)
    QTest.keyClicks(draft.edit, "-h")
    QTest.keyClick(draft.edit, Qt.Key_Return)
    assert len(requests) == 1 and requests[0][0] == omni.Chip("holds", "", negated=True)


def test_roll_tokens_share_the_stat_completion_and_write_back_their_own_prefix(app, conn):
    conn.executescript(
        """
        INSERT INTO sde_dogma_attributes
            (attribute_id,name,display_name,unit_id,high_is_good,default_value,published)
            VALUES (50,'cpu','CPU usage',106,0,0,1);
        INSERT INTO sde_dogma_units VALUES (106,'Teraflops','tf');
        INSERT INTO sde_mutator_ranges VALUES (47297,50,0.8,1.5,NULL,47408);
        """
    )
    b = Omnibox()
    b._complete_query.run = lambda fn, on_done, on_failed=None: on_done(fn(conn))
    model = b._completion_model

    QTest.keyClicks(b.edit, "roll:cp")
    settle(b)
    assert [model.item(i).data(Qt.UserRole) for i in range(model.rowCount())] == ["CPU usage"]

    b._completer.activated[QModelIndex].emit(model.index(0, 0))
    assert b.edit.text() == 'roll:"CPU usage"', "the write-back must keep the roll prefix"
    assert b._chips == []
    b.deleteLater()


# ------------------------------------------------------- save/load commands
def commands(box) -> tuple[list, list]:
    """Both command signals, recorded: every test here cares which of the two
    fired as much as that one did."""
    return record(box.save_requested), record(box.load_requested)


def test_a_save_command_runs_after_the_chips_on_its_line_are_minted(box):
    """`owner:Main save:Jita` must store the view the user is looking at,
    not the one they had before the line -- so the command comes out of the
    field first and the remaining tokens migrate before it runs. It leaves
    nothing behind either: a command is performed once, and a `save` chip or
    a leftover word would fire again on the next Enter."""
    saved, loaded = commands(box)

    QTest.keyClicks(box.edit, 'owner:Main save:"Jita ships"')
    QTest.keyClick(box.edit, Qt.Key_Return)

    assert saved == [("Jita ships",)]
    assert loaded == []
    assert [c for c, _w in box._chips] == [omni.Chip("owner", "Main")]
    assert box.edit.text() == ""


def test_an_empty_command_asks_for_its_card(box):
    """`load:` with no name is the request for the list -- the holds and fit
    empty-request precedent, at the command level."""
    saved, loaded = commands(box)
    QTest.keyClicks(box.edit, "load:")
    QTest.keyClick(box.edit, Qt.Key_Return)
    assert loaded == [("",)] and saved == []
    assert box.edit.text() == "" and box._chips == []


def test_bare_text_survives_a_command_on_the_same_line(box):
    """The remainder is put back verbatim: a search word beside a command is
    still a search."""
    saved, _loaded = commands(box)
    QTest.keyClicks(box.edit, "save:Jita tritanium")
    QTest.keyClick(box.edit, Qt.Key_Return)
    assert saved == [("Jita",)]
    assert box.edit.text() == "tritanium"


def test_only_the_first_command_on_a_line_runs_and_the_rest_are_consumed(box):
    """A line is one intent. A second `save:` on it is a typo far more often
    than a batch API -- but leaving it in the field would fire it on the
    next Enter, so it goes too."""
    saved, loaded = commands(box)
    QTest.keyClicks(box.edit, "save:One load:Two save:Three")
    QTest.keyClick(box.edit, Qt.Key_Return)
    assert saved == [("One",)] and loaded == []
    assert box.edit.text() == "" and box._chips == []


def test_a_trailing_space_does_not_run_a_command(box):
    """`save:Jita ` is a name in progress: the space-commit path mints chips
    but must never perform an act the user has not pressed Enter on."""
    saved, loaded = commands(box)
    QTest.keyClicks(box.edit, "cat:Ship save:Jita ")
    assert saved == [] and loaded == []
    assert box.edit.text() == "save:Jita ", "the command is still being typed"
    assert [c for c, _w in box._chips] == [omni.Chip("category", "Ship")]


def test_a_command_inside_an_open_quote_is_not_run(box):
    """The open-quote guard is what stops `loc:"Jita save:x` from saving
    while the location value is still being typed."""
    saved, _loaded = commands(box)
    QTest.keyClicks(box.edit, 'loc:"Jita save:Jita')
    QTest.keyClick(box.edit, Qt.Key_Return)
    assert saved == []
    assert box.edit.text() == 'loc:"Jita save:Jita'


def test_a_quoted_or_negated_command_word_is_an_ordinary_search(box):
    """Quoting is how a user searches for the literal text, and the grammar
    has no negative form of an act -- both must stay bare text."""
    saved, loaded = commands(box)
    QTest.keyClicks(box.edit, '"save:x" -load:y')
    QTest.keyClick(box.edit, Qt.Key_Return)
    assert saved == [] and loaded == []
    assert box._chips == []
    assert box.edit.text() == '"save:x" -load:y'


def test_set_spec_never_runs_a_command(box):
    """A saved view can carry the words `save:x` as bare text (a filter
    typed before this build knew the command). Restoring it must not save:
    only a fresh Enter is a command."""
    saved, loaded = commands(box)
    box.set_spec(omni.parse("save:x load:y"))
    assert saved == [] and loaded == []
    # Written back quoted: bare, the next Enter would run the commands the
    # user only ever searched for; quoted, a fresh Enter searches again.
    assert box.edit.text() == '"save:x" "load:y"', "still text, and stays text on Enter"


def test_a_command_wins_over_the_card_a_chip_on_the_same_line_would_open(box, app):
    """Two popups cannot share one Enter: the card requested a turn later
    would land on the command's own card and close it as an outside click,
    so the thing the user asked for explicitly is the one that happens."""
    saved, _loaded = commands(box)
    requests = record(box.card_requested)

    QTest.keyClicks(box.edit, 'holds:"Nanite Repair Paste"<5 save:')
    QTest.keyClick(box.edit, Qt.Key_Return)
    app.processEvents()

    assert saved == [("",)]
    assert requests == [], "exactly one card opens, and it is the Save card"
    assert [c.kind for c, _w in box._chips] == ["holds"]


def test_the_draft_builder_resolves_the_commands_without_shadowing_a_kind(box):
    """The commands are offered last and looked at last: the letters that
    already mean a filter keep meaning it, or `l` would stop being location
    the day load: shipped."""
    from evasset.ui.omnibox import _DRAFT_KINDS

    assert _DRAFT_KINDS[-2:] == (omni.SAVE_COMMAND, omni.LOAD_COMMAND)
    draft = open_draft(box)
    assert draft._resolve_kind("l") == ("location", False)
    assert draft._resolve_kind("lo") == ("location", False)
    assert draft._resolve_kind("loa") == (omni.LOAD_COMMAND, False)
    assert draft._resolve_kind("sa") == (omni.SAVE_COMMAND, False)
    assert draft._resolve_kind("s") is None, "system or stat, as before"


def test_the_draft_command_stage_stays_a_draft_and_commits_on_an_empty_enter(box):
    """A command never becomes a chip, so the card must not take a kind's
    wash and pretend it is about to: the dashed border is the truth. Enter
    on an empty value is the request for the card, and nothing is inserted
    either way."""
    saved, loaded = commands(box)
    draft = open_draft(box)
    QTest.keyClicks(draft.edit, "sa")
    QTest.keyClick(draft.edit, Qt.Key_Return)

    assert (draft.kind, draft.negated) == (omni.SAVE_COMMAND, False)
    assert draft.prefix_label.text() == "save:"
    assert "dashed" in draft.styleSheet()

    QTest.keyClick(draft.edit, Qt.Key_Return)
    assert saved == [("",)] and loaded == []
    assert box._draft is None and box._chips == []
    assert box.edit.text() == ""


def test_the_draft_command_stage_carries_a_typed_name(box):
    saved, _loaded = commands(box)
    draft = open_draft(box)
    QTest.keyClicks(draft.edit, "save")
    QTest.keyClick(draft.edit, Qt.Key_Return)
    QTest.keyClicks(draft.edit, '"Jita ships"')
    QTest.keyClick(draft.edit, Qt.Key_Return)
    assert saved == [("Jita ships",)]
    assert box._chips == []


def test_a_minus_before_a_command_carries_no_polarity_into_it(box):
    """`-save` is not "unsave": the draft drops the minus rather than
    building a negated command nothing downstream could read."""
    saved, _loaded = commands(box)
    draft = open_draft(box)
    QTest.keyClicks(draft.edit, "-sa")
    QTest.keyClick(draft.edit, Qt.Key_Return)
    assert (draft.kind, draft.negated) == (omni.SAVE_COMMAND, False)
    assert draft.prefix_label.text() == "save:"
    QTest.keyClick(draft.edit, Qt.Key_Return)
    assert saved == [("",)]


def test_a_chip_prefix_label_is_parented_before_it_is_ever_shown(box, monkeypatch):
    """A prefixed chip once created its prefix label unparented and showed it
    before the layout adopted it: a top-level window for a few milliseconds
    that took and returned focus, which Qt reports as ApplicationDeactivate
    and answers by closing every popup -- the card opened on the same line
    as `owner:Main` died before it was seen. The offscreen platform does not
    surface that deactivation to a test, so the pin is structural: no label
    may be shown while it has no parent."""
    from PySide6.QtWidgets import QLabel

    shown_orphans = []
    real_set_visible = QLabel.setVisible

    def guarded(self, visible):
        if visible and self.parent() is None:
            shown_orphans.append(self.text())
        return real_set_visible(self, visible)

    monkeypatch.setattr(QLabel, "setVisible", guarded)
    box.add_chip("owner", "Main")
    box.add_chip("category", "Ship")
    assert [c for c, _w in box._chips] == [omni.Chip("owner", "Main"), omni.Chip("category", "Ship")]
    assert shown_orphans == []
