"""The omnibox filter grammar and its translation to SQL.

One grammar, parsed in one place, because the filter state used to be spread
over a search field, three combo boxes and two checkboxes -- six widgets that
each owned a fragment of the WHERE clause and had to be kept mutually
consistent by hand. Everything that narrows the asset table is now a token in
a single string: `prefix:value` chips for the grouping levels, `is:` flags,
`val:`, `stat:`, `roll:` and `holds:` comparisons, the `abyssal` and `fit:`
chips, and bare words.
That makes the whole filter state
trivially serialisable (saved views store `to_text()` output), and it gives
every other control a single verb -- rail rows, value-map segments and
context-menu items all "add a chip" instead of each poking a different widget.

Bare text deliberately matches only the item name and the custom name, unlike
the old search field which also swept location, system, region, owner, group
and category. Typing "jita" there matched every single asset docked in Jita,
which made free text useless for finding an item at a busy hub -- the one
thing free text is actually for. Anyone who wants to filter on a place or an
owner has a chip for it, and the completer offers them; the bare words are
reserved for "find my thing called this".

Everything here is Qt-free on purpose: the grammar is exercised by plain
pytest without a window, and the SQL it produces is written against the inner
aliases of `queries.ASSET_ROWS` (t, a, p, mg, ...) so it composes with the
same subquery-injection idiom the rest of `queries.py` uses.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass, field

from . import abyssal, fitting, queries
from .config import ASSET_SAFETY_LOCATION_ID

# The chip kinds that filter one grouping level by exact label. Six of them
# come from queries.ROLLUP_LEVELS; meta is extra -- it is not a rail grouping level,
# but "show me only Tech II" is too useful to leave out of the grammar.
LEVEL_KINDS = ("location", "system", "region", "owner", "category", "group", "meta")

IS_FLAGS = ("fitted", "safety", "delivery", "unpriced", "bpc", "fit")

# stat:<name><op><number> compares one stored attribute of an abyssal item,
# in the units the inspector displays. Stored, not rolled, by decision: ESI
# returns every dogma attribute of the item and all of them are kept, so
# `stat:cpu<26` finds a module by its CPU whether or not its mutaplasmid
# rolls CPU -- which is what someone fitting a ship wants to know. Only the
# rolled subset appears in the inspector. Exported by name so the omnibox
# and palette key their per-kind behaviour on the same string this module
# parses.
STAT_KIND = "stat"

# roll:<name><op><percent> compares the mirrored roll QUALITY of one rolled
# attribute -- how far along the mutaplasmid's range it landed, 0..100 with
# 100 always the good end -- so `roll:web>=70` reads the same for a
# webifier's negative speedFactor as `roll:cpu>=70` does for a positive
# stat. Rolled attributes only, by construction: the quality is defined by
# the mutator's range table, so a stored-but-unrolled attribute has none.
ROLL_KIND = "roll"

# The abyssal chip: `abyssal` alone is every dynamic (mutated) type, and a
# value narrows it to named types, OR'd, joined by ", " -- see split_types.
# Its own kind rather than an is: flag because it carries a value, and
# because the complex-search card hangs off it.
ABYSSAL_KIND = "abyssal"

# holds:<bay/><type name><op><number> counts one consumable inside a ship.
# The candidates are assembled ships in both polarities (see
# queries.SHIP_ROWS_CLAUSE): a ship always has a well-defined count, zero
# when it is empty, so there is no "no data" case for a NOT EXISTS to
# protect the way stat: and roll: have -- and a NOT-EXISTS negation would
# answer "which ships are short of paste" with every asset in the estate.
HOLDS_KIND = "holds"

# fit:<name> compares a ship's fitted rack against a stored fit by exact
# multiset of type ids, and is hull-scoped in both polarities: a fit says
# nothing about a hull it was not written for. Positive fit chips OR
# together (a label family -- "either of these two fits"), unlike holds
# chips, which AND like the other comparisons.
FIT_KIND = "fit"

# save: and load: are the omnibox's COMMANDS, not chip kinds. A command is an
# act performed once when the line is committed -- store this view under a
# name, recall that one -- so it must never be persisted: parse() never mints
# one, to_text() never writes one, where() never sees one, and Chip.kind is
# never one of them. The omnibox strips commands out of the line with
# extract_commands before parse() ever reads it, which is why a saved view
# can never smuggle a command back into a later commit.
SAVE_COMMAND = "save"
LOAD_COMMAND = "load"
COMMAND_KINDS = (SAVE_COMMAND, LOAD_COMMAND)

# The bay forms a holds value may carry, as `bay/` prefixes. Keyed off
# fitting.HOLD_BAYS so the grammar and the SQL can never offer different
# bays. The slash is part of the prefix on purpose: `fleet/` must not claim
# a name that merely starts with the word, or `holds:"Fleeting Compact
# Stasis Webifier"<1` would lose its first five letters to the fleet hangar.
_BAY_PREFIXES = {f"{bay}/": bay for bay in fitting.HOLD_BAYS}

# Short forms are what people type; the long forms are accepted too so that
# to_text() output and hand-written saved views both parse regardless of which
# spelling they used.
_PREFIX_TO_KIND = {
    "loc": "location",
    "location": "location",
    "sys": "system",
    "system": "system",
    "region": "region",
    "owner": "owner",
    "cat": "category",
    "category": "category",
    "group": "group",
    "meta": "meta",
    # item: is the exact-name axis "Where else is this?" filters on. Bare
    # text cannot serve that gesture -- LIKE %Tritanium% also counts every
    # Compressed Tritanium stack, which inflates the answer for any item
    # whose name is a substring of another's.
    "item": "item",
}
_KIND_TO_PREFIX = {"location": "loc", "system": "sys", "category": "cat"}

# val: comparisons -- an operator, a number, and an optional ISK magnitude
# suffix (k/m/b/t), e.g. val:>10m. Anchored on both ends so trailing garbage
# falls through to bare text instead of silently parsing as a comparison.
_VAL_RE = re.compile(r"^(>=|<=|>|<)(\d+(?:\.\d+)?)([kmbt]?)$", re.IGNORECASE)
_VAL_SUFFIX = {"": 1, "k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}

# stat: and roll: comparisons -- an attribute name (display or internal, or
# one of abyssal.STAT_ALIASES; spaces allowed, so the token is usually
# quoted), an operator, and a number that may be negative because a
# webifier's speedFactor is -60; or `name=lo..hi`, an inclusive range. The
# operator group is the only fragment that reaches the SQL text, and it can
# only ever be one of these five literals; the name and numbers travel as
# bound parameters. The name group excludes the operator characters
# outright, so `stat:a<b<3` has no parse at all and degrades to bare text
# rather than guessing -- with a merely lazy `.+?` the regex backtracked
# into name `a<b`, op `<`, 3. No SDE attribute name or display name
# contains `<`, `>` or `=`. Digits are the ASCII class, not `\d`, which
# would also admit Arabic-Indic and other Unicode digits, and float()
# accepts those, so `stat:cpu<٣` would have parsed as 3. The regex admits
# `=` and `..` in any combination; parse_stat then insists they come
# together, since `cpu=30` has no agreed meaning (equality on a float is
# never what anyone wants) and `cpu>30..40` has two.
_STAT_RE = re.compile(
    r"^\s*([^<>=]+?)\s*(>=|<=|>|<|=)\s*(-?[0-9]+(?:\.[0-9]+)?)(?:\.\.(-?[0-9]+(?:\.[0-9]+)?))?\s*$"
)

# The attribute-name match shared by the stat: and roll: clauses. COLLATE
# NOCASE on the equality so "cpu usage" finds "CPU usage".
#
# The internal name is matched first and the display name only when no
# attribute's internal name equals the typed text, because display names
# are not unique: in the real SDE (build 3487903, checked 2026-09-02) 554
# signatureRadiusBonus (unit 124, a percent) and 983 signatureRadiusAdd
# (unit 1, metres) both display "Signature Radius Modifier". Typing the
# display name still matches either -- the completer is what steers a user
# to the internal name in that case -- but typing an internal name must
# never be widened to its namesakes. The typed name is bound three times
# rather than interpolated once; the SQL text stays constant. The inner
# NOT EXISTS is uncorrelated, so SQLite evaluates it once per statement,
# not once per asset row.
_NAME_MATCH = """(sd.name = ? COLLATE NOCASE
           OR (sd.display_name = ? COLLATE NOCASE
               AND NOT EXISTS (SELECT 1 FROM sde_dogma_attributes x
                               WHERE x.name = ? COLLATE NOCASE)))"""

# Correlated on a.item_id: does this asset have a stored attribute of that
# name whose DISPLAY value satisfies the comparison. The unit conversion is
# queries.display_value_sql, the same CASE the inspector renders with, so
# `stat:duration<9` means nine seconds exactly as the panel shows them.
# {cmp} is `<op> ?` or `BETWEEN ? AND ?`, filled by where().
_STAT_EXISTS = f"""EXISTS (
    SELECT 1 FROM abyssal_attributes sa
    JOIN sde_dogma_attributes sd ON sd.attribute_id = sa.attribute_id
    WHERE sa.item_id = a.item_id
      AND {_NAME_MATCH}
      AND {queries.display_value_sql("sa.value", "sd.unit_id")} {{cmp}}
)"""

# Correlated on a.item_id: does this asset have a ROLLED attribute of that
# name whose quality (queries.roll_quality_sql, percent) satisfies the
# comparison. Rolled is enforced by the inner join to the item's own
# mutator's range row, which also supplies the range and the polarity
# override; the source type's base comes from sde_type_dogma with the
# attribute default as fallback, exactly as fetch_abyssal_rolls reads it.
# Every join is a primary-key probe off the abyssal_items row, so the
# clause costs one lookup chain per asset row (pinned by an EXPLAIN QUERY
# PLAN test). {quality} is filled at where() time rather than at import,
# because roll_quality_sql reads abyssal.POLARITY_OVERRIDES when called.
_ROLL_EXISTS = f"""EXISTS (
    SELECT 1 FROM abyssal_items i
    JOIN abyssal_attributes sa ON sa.item_id = i.item_id
    JOIN sde_mutator_ranges mr ON mr.mutator_type_id = i.mutator_type_id
                              AND mr.attribute_id = sa.attribute_id
    JOIN sde_dogma_attributes sd ON sd.attribute_id = sa.attribute_id
    LEFT JOIN sde_type_dogma td ON td.type_id = i.source_type_id
                               AND td.attribute_id = sa.attribute_id
    WHERE i.item_id = a.item_id AND i.status = '{abyssal.STATUS_OK}'
      AND {_NAME_MATCH}
      AND {{quality}} {{cmp}}
)"""


def _roll_quality_expr() -> str:
    return queries.roll_quality_sql(
        value="sa.value",
        base="COALESCE(td.value, sd.default_value)",
        min_mult="mr.min_mult",
        max_mult="mr.max_mult",
        attr_high="sd.high_is_good",
        mutator_high="mr.high_is_good",
        attribute_id="sd.attribute_id",
    )

# is:fitted is the same "does my direct parent belong to the Ship category"
# question the hide-ship-contents checkbox asked, with the polarity flipped:
# the clause in queries.py keeps loose items, this variant keeps the fitted
# and carried ones. Derived from the same string so the two can never drift.
_FITTED_EXISTS = queries.HIDE_SHIP_CONTENTS_CLAUSE.replace("NOT EXISTS", "EXISTS", 1)

# ESI reports a compartment on the row that sits in the location, and the
# things inside it are ordinary child rows carrying their own ordinary flags.
# So "is it in X" is a question about an ancestor, not about the row, and both
# of the filters below need the same recursive walk.
#
# Flags are module constants, never user input, so interpolating them is not
# the injection risk the parameterised chip values would be. Every value a user
# can influence still goes through a ? placeholder -- see the injection tests.
def _inside_flagged(flags: tuple[str, ...]) -> str:
    """Rows carrying one of these flags, plus everything nested inside them."""
    quoted = ", ".join(f"'{flag}'" for flag in flags)
    return f"""a.item_id IN (
        WITH RECURSIVE holding(item_id) AS (
            SELECT item_id FROM assets WHERE location_flag IN ({quoted})
            UNION
            SELECT a2.item_id FROM assets a2 JOIN holding h ON a2.location_id = h.item_id
        )
        SELECT item_id FROM holding
    )"""


# Everything in asset safety: the wraps themselves and everything inside them,
# however deep.
#
# The obvious predicate, root_location_id = 2004, matches nothing in practice.
# ESI does document 2004 as the asset safety location, but that appears to
# describe items while the safety timer is running. Once delivered, they are an
# "Asset Safety Wrap" (type 60) sitting in a real NPC station, carrying
# location_flag='AssetSafety', with the contents as ordinary child rows. On a
# real account of 18,263 assets the old clause matched 0 rows against 213 wraps
# holding 2,483 items.
#
# Recursive because the nesting is two deep, not one: a ship inside a wrap has
# its fitted modules inside the ship. Direct children alone missed 1,539 rows.
#
# 2004 is still checked, since the in-transit case is real and cheap to keep.
_IN_ASSET_SAFETY = (
    _inside_flagged(("AssetSafety",))
    + f" OR a.root_location_id = {ASSET_SAFETY_LOCATION_ID}"
)

# Contract and courier deliveries waiting to be collected, and anything inside
# them -- a delivered ship arrives with its fitting, which is why this walks
# too.
#
# All four names ESI's location_flag enum uses, rather than the two that happen
# to appear in one account's data: a filter that quietly ignores
# CapsuleerDeliveries until somebody has one is worse than no filter.
DELIVERY_FLAGS = (
    "Deliveries",
    "CorpDeliveries",
    "CapsuleerDeliveries",
    "CorporationGoalDeliveries",
)
_IN_DELIVERIES = _inside_flagged(DELIVERY_FLAGS)


# Positive and negated SQL per is: flag. Each pair is written out rather than
# generated by wrapping NOT(...) because the naive negation is wrong for some
# of them: NULL root_location_id rows must survive -is:safety, and the
# fitted pair already exists in both polarities.
# `fit:"name"` and its negation, and the shapes `is:fit` is built from. The
# ship clause leads every one of them so SQLite's left-to-right AND
# short-circuits the correlated fit subquery away on the non-ship rows,
# which is most of an estate.
_FIT_MATCHES = (
    f"EXISTS (SELECT 1 FROM fits f "
    f"WHERE f.name = ? AND {queries.FIT_HULL} AND {queries.FIT_EQUAL})"
)
_FIT_DEVIATES = (
    f"EXISTS (SELECT 1 FROM fits f "
    f"WHERE f.name = ? AND {queries.FIT_HULL} AND NOT {queries.FIT_EQUAL})"
)
_ANY_FIT_MATCHES = (
    f"EXISTS (SELECT 1 FROM fits f WHERE {queries.FIT_HULL} AND {queries.FIT_EQUAL})"
)
_HAS_A_FIT = f"EXISTS (SELECT 1 FROM fits f WHERE {queries.FIT_HULL})"

_IS_SQL = {
    "fitted": (_FITTED_EXISTS, queries.HIDE_SHIP_CONTENTS_CLAUSE),
    # Safe to negate by wrapping, unlike the old root_location_id comparison:
    # item_id is the primary key and never NULL, so NOT IN cannot silently
    # drop rows the way a NULL-valued column would.
    "safety": (f"({_IN_ASSET_SAFETY})", f"NOT ({_IN_ASSET_SAFETY})"),
    # Owner already distinguishes yours from the corp's, so one flag
    # rather than two: is:delivery owner:"Test Corp" is the corp half.
    "delivery": (f"({_IN_DELIVERIES})", f"NOT ({_IN_DELIVERIES})"),
    "unpriced": (
        "COALESCE(p.source, 'none') = 'none'",
        "COALESCE(p.source, 'none') <> 'none'",
    ),
    "bpc": ("a.is_blueprint_copy = 1", "a.is_blueprint_copy = 0"),
    # is:fit is "matches any stored fit for its hull"; its negation is the
    # deviating ships of hulls that HAVE a stored fit, not every other row.
    # A hull nobody has written a fit for is absent from both polarities --
    # there is no verdict to give about it, and answering "deviates" would
    # be a claim the data does not support.
    "fit": (
        f"({queries.SHIP_ROWS_CLAUSE} AND {_ANY_FIT_MATCHES})",
        f"({queries.SHIP_ROWS_CLAUSE} AND {_HAS_A_FIT} AND NOT {_ANY_FIT_MATCHES})",
    ),
}

# The abyssal chip's positive clause: the type flag, narrowed to named types
# when the chip carries any. {names} is a list of `?` marks, one per type,
# so the names are bound, never interpolated.
_ABYSSAL_ALL = "t.is_dynamic_type = 1"
_ABYSSAL_TYPES = "(t.is_dynamic_type = 1 AND t.name IN ({names}))"


@dataclass
class Chip:
    """One parsed token: a level filter, an is: flag, a comparison, or the abyssal chip."""

    # one of LEVEL_KINDS, or "item", "is", "val", STAT_KIND, ROLL_KIND,
    # ABYSSAL_KIND, HOLDS_KIND (a consumable count inside a ship) or
    # FIT_KIND (a ship's rack against a stored fit).
    kind: str
    value: str
    negated: bool = False


@dataclass
class StatTerm:
    """A parsed stat: or roll: value: `name op low`, or `name=low..high` when op is "..".

    high is None for the four one-sided operators. The name is as typed,
    before alias resolution, so a completer can show what the user picked
    and look up the canonical attribute itself via abyssal.STAT_ALIASES;
    where() resolves it at SQL time.
    """

    name: str
    op: str  # one of ">=", "<=", ">", "<", ".."
    low: float
    high: float | None = None


@dataclass
class HoldsTerm:
    """A parsed holds: value: how much of one type a ship must be carrying.

    bay is None for the whole ship or one of fitting.HOLD_BAYS' keys; name
    is one exact SDE type name, matched case-insensitively in the SQL. The
    operator vocabulary is the stat: one, `..` standing for an inclusive
    range, because a count is a comparison like any other and a second
    grammar for it would be one more thing to remember.
    """

    bay: str | None
    name: str
    op: str  # one of ">=", "<=", ">", "<", ".."
    low: float
    high: float | None = None


@dataclass
class FilterSpec:
    """The full filter state: bare search words plus the chips."""

    text: str = ""
    chips: list[Chip] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.chips and not self.text.strip()

    def describe(self) -> int:
        """How many filters are active, for the "N filters" state row.

        All the bare words count as one filter together: they render as one
        editable string, so they clear as one.
        """
        return len(self.chips) + (1 if self.text.strip() else 0)

    def to_text(self) -> str:
        """Serialise back to grammar text that parse() reads to an equal spec.

        This is the saved-view format: one line of the same grammar the user
        types, so a saved view can be inspected, edited or shared as text
        rather than being an opaque blob. The round-trip guarantee
        parse(spec.to_text()) == spec holds for any chips whose values are
        semantically valid (is: values from IS_FLAGS; val:, stat: and roll:
        values matching their comparison grammars; level and abyssal values
        may be any string at all) --
        property-tested in tests/test_omni.py, because the first version of
        this pair quietly corrupted saved views on three shapes of value:
        embedded quotes, embedded non-space whitespace, and empty labels.
        """
        parts = []
        for c in self.chips:
            sign = "-" if c.negated else ""
            if c.kind == ABYSSAL_KIND and not c.value:
                # The bare word is the canonical spelling of "all abyssal
                # types"; parse() reads it back to the same empty chip.
                parts.append(sign + ABYSSAL_KIND)
                continue
            prefix = _KIND_TO_PREFIX.get(c.kind, c.kind)
            parts.append(sign + f"{prefix}:{_quote_value(c.value)}")
        for word in self.text.split():
            # A bare word that would re-tokenize as a chip (someone searched
            # the literal text "cat:mystery"), or as a save:/load: command,
            # or that carries a quote, gets wrapped -- parse() unquotes a
            # quoted bare token back to text, so the word survives recall
            # instead of quietly becoming a different filter than the one
            # that was saved, and a saved line committed again by hand can
            # never run a command the user only ever searched for.
            if '"' in word or parse(word).chips or extract_commands(word)[1]:
                escaped = word.replace("\\", "\\\\").replace('"', '\\"')
                word = f'"{escaped}"'
            parts.append(word)
        return " ".join(parts)

    def where(
        self, exclude_level: str | None = None, exclude_kinds: Collection[str] = ()
    ) -> tuple[str, tuple]:
        """SQL WHERE (no leading keyword) against ASSET_ROWS' inner aliases.

        Composition rules: every bare word must match (AND); positive chips
        of the same kind OR together (picking two locations means "either");
        different kinds, negations, flags and comparisons all AND. Values
        travel as bound parameters -- the only strings interpolated into the
        SQL are expressions this module owns.

        exclude_level drops chips of that kind, both polarities. The rail
        facets its rows by every filter except its own level, so that picking
        one location still shows the other locations to switch to.
        exclude_kinds does the same for several kinds at once: the abyssal
        card's type picker passes every kind the card rewrites on Done
        (abyssal, roll:, stat:), since a picker faceted by a roll: chip the
        user is about to loosen would hide the very types that fail it.

        The two ship-scoped kinds are the exceptions to "negation is the
        complement": both `holds:` and `fit:` restrict to assembled ships in
        either polarity, so -holds: lists the ships that are short and
        -fit: the ships of that hull that deviate, rather than the whole
        estate minus a handful of rows. Positive fit chips OR (two fits of a
        hull are two acceptable answers); holds chips AND (two thresholds
        are two requirements).

        Two positive abyssal chips mean the union of their type lists, the
        same "either" that two location chips mean -- and a chip with no
        types is already every type, so it absorbs any other. A negated
        abyssal chip is NOT of its own clause and ANDs with everything.
        """
        clauses: list[str] = []
        params: list = []

        for word in self.text.split():
            clauses.append("(t.name LIKE ? OR a.custom_name LIKE ?)")
            params.extend([f"%{word}%"] * 2)

        dropped = set(exclude_kinds)
        if exclude_level is not None:
            dropped.add(exclude_level)
        chips = [c for c in self.chips if c.kind not in dropped]

        abyssal_positive = [c for c in chips if c.kind == ABYSSAL_KIND and not c.negated]
        if abyssal_positive:
            names: list[str] = []
            for c in abyssal_positive:
                names.extend(n for n in split_types(c.value) if n not in names)
            if any(not split_types(c.value) for c in abyssal_positive) or not names:
                clauses.append(_ABYSSAL_ALL)
            else:
                clauses.append(_ABYSSAL_TYPES.format(names=",".join("?" * len(names))))
                params.extend(names)
        for c in chips:
            if c.kind == ABYSSAL_KIND and c.negated:
                names = split_types(c.value)
                if names:
                    clauses.append("NOT " + _ABYSSAL_TYPES.format(names=",".join("?" * len(names))))
                    params.extend(names)
                else:
                    clauses.append(f"NOT ({_ABYSSAL_ALL})")

        # COLLATE NOCASE on every chip comparison. Bare words already matched
        # without regard to case, because SQLite's LIKE is case-insensitive for
        # ASCII, so "tritanium" found Tritanium while "cat:ship" found nothing
        # at all -- the same query typed two ways, behaving differently for a
        # reason no user could see.
        #
        # NOCASE folds ASCII only, which matches what LIKE already does, so the
        # two halves of a query now agree. A chip typed with an accented
        # character still has to match its case, as a bare word always has.
        for kind in (*LEVEL_KINDS, "item"):
            # item is not a grouping level, but it filters exactly like one:
            # an exact match on a single expression, OR-able and negatable.
            # The .get default serves meta, the one LEVEL_KINDS entry with no
            # rail level behind it (see the LEVEL_KINDS comment above).
            expr = "t.name" if kind == "item" else queries.OVERVIEW_FILTER_EXPR.get(kind, "mg.name")
            positive = [c for c in chips if c.kind == kind and not c.negated]
            if positive:
                clauses.append(
                    "(" + " OR ".join([f"{expr} = ? COLLATE NOCASE"] * len(positive)) + ")"
                )
                params.extend(c.value for c in positive)
            for c in chips:
                if c.kind == kind and c.negated:
                    # Keep NULL-labelled rows: excluding "Tech II" must not
                    # also hide every item that has no meta group at all.
                    clauses.append(f"({expr} IS NULL OR {expr} <> ? COLLATE NOCASE)")
                    params.append(c.value)

        # Both ship-scoped kinds carry SHIP_ROWS_CLAUSE in both polarities:
        # the complement of "ships carrying 500 rounds" is "ships carrying
        # fewer", never "every other asset". holds chips AND like the other
        # comparisons; positive fit chips OR like a label family, since two
        # fits of one hull are two acceptable answers to "is this ship
        # ready", while two count thresholds are two requirements.
        for c in chips:
            if c.kind != HOLDS_KIND:
                continue
            term = parse_holds(c.value)
            if term is None:
                continue
            if term.op == "..":
                cmp, numbers = "BETWEEN ? AND ?", [term.low, term.high]
            else:
                cmp, numbers = f"{term.op} ?", [term.low]
            count = queries.holds_count_sql(term.bay)
            comparison = f"{count} {cmp}"
            if c.negated:
                comparison = f"NOT ({comparison})"
            clauses.append(f"({queries.SHIP_ROWS_CLAUSE} AND {comparison})")
            params.extend([term.name, *numbers])

        fit_positive = [c for c in chips if c.kind == FIT_KIND and not c.negated]
        if fit_positive:
            either = " OR ".join([_FIT_MATCHES] * len(fit_positive))
            clauses.append(f"({queries.SHIP_ROWS_CLAUSE} AND ({either}))")
            params.extend(c.value for c in fit_positive)
        for c in chips:
            if c.kind == FIT_KIND and c.negated:
                clauses.append(f"({queries.SHIP_ROWS_CLAUSE} AND {_FIT_DEVIATES})")
                params.append(c.value)

        # A chip that cannot be translated is skipped, mirroring parse()'s
        # forgiveness, because chips do not only come from parse: saved views
        # written by a newer build may carry a flag this build has never
        # heard of, and applying the view's other filters beats blowing up
        # the whole assets tab over the one it cannot honour.
        for c in chips:
            if c.kind == "is":
                sql_pair = _IS_SQL.get(c.value)
                if sql_pair is None:
                    continue
                clauses.append(sql_pair[1] if c.negated else sql_pair[0])
            elif c.kind == "val":
                parsed = _parse_val(c.value)
                if parsed is None:
                    continue
                op, amount = parsed
                comparison = f"a.quantity * COALESCE(p.sell_price, 0) {op} ?"
                clauses.append(f"NOT ({comparison})" if c.negated else comparison)
                params.append(amount)
            elif c.kind in (STAT_KIND, ROLL_KIND):
                term = parse_stat(c.value)
                if term is None:
                    continue
                # Aliases resolve to the internal attribute name before the
                # match; anything not an alias is matched as typed, internal
                # name first and display name as the fallback (_NAME_MATCH).
                name = abyssal.STAT_ALIASES.get(term.name.lower(), term.name)
                if term.op == "..":
                    cmp, numbers = "BETWEEN ? AND ?", [term.low, term.high]
                else:
                    cmp, numbers = f"{term.op} ?", [term.low]
                if c.kind == STAT_KIND:
                    exists = _STAT_EXISTS.format(cmp=cmp)
                else:
                    exists = _ROLL_EXISTS.format(quality=_roll_quality_expr(), cmp=cmp)
                # NOT EXISTS, not a negated comparison inside the EXISTS:
                # an item with no stored stats has nothing to compare, and
                # -stat:cpu<30 must keep it rather than hide it. Likewise
                # -roll: keeps every item whose rolls are not fetched.
                clauses.append(f"NOT {exists}" if c.negated else exists)
                params.extend([name, name, name, *numbers])

        return " AND ".join(clauses), tuple(params)


def split_types(value: str) -> list[str]:
    """The type names an abyssal chip value carries, in order, blanks dropped.

    The value is the names joined by ", " (join_types), and this is its
    inverse for the SQL and the card; it also forgives what a hand-typed
    value looks like -- "A,B", "A ,  B", a trailing comma, `", ,"` -- so
    every one of those reads as the same chip. The comma is a hard
    delimiter: a type name containing one cannot be expressed. Acceptable
    because no dynamic type's name does (the 89 in build 3487903 are all
    "<size> Abyssal <module>" or "<size> Mutated <drone>" shapes, checked
    2026-09-02), and the alternative -- a second quoting layer inside the
    already-quoted chip value -- is not something anyone would type. An
    empty result means every dynamic type.
    """
    return [name.strip() for name in value.split(",") if name.strip()]


def join_types(names: list[str]) -> str:
    """The abyssal chip value for these type names: split_types' inverse."""
    return ", ".join(n.strip() for n in names if n.strip())


def _parse_val(value: str) -> tuple[str, float] | None:
    """The operator and the scaled amount, or None for a malformed value."""
    m = _VAL_RE.match(value)
    if m is None:
        return None
    op, number, suffix = m.groups()
    return op, float(number) * _VAL_SUFFIX[suffix.lower()]


def parse_stat(value: str) -> StatTerm | None:
    """The StatTerm a stat: or roll: value denotes, or None when malformed.

    A value with no operator, no number, or a blank name is malformed, and
    so are the two half-ranges: `=` without `..` (float equality is never
    the question, and silently reading it as >= or as a band would answer
    one the user did not ask) and `..` after any operator but `=`. A range
    whose low end exceeds its high end is malformed too rather than
    quietly swapped -- the card writes ranges the right way round, so a
    reversed one is a typo mid-edit and the chip should wait. Equal ends
    are a legitimate one-value band.
    """
    m = _STAT_RE.match(value)
    if m is None:
        return None
    name, op, low, high = m.groups()
    name = name.strip()
    if not name:
        return None
    if (op == "=") != (high is not None):
        return None
    if high is None:
        return StatTerm(name, op, float(low))
    lo, hi = float(low), float(high)
    if lo > hi:
        return None
    return StatTerm(name, "..", lo, hi)


def parse_holds(value: str) -> HoldsTerm | None:
    """The HoldsTerm a holds: value denotes, or None when malformed.

    One optional `cargo/`, `fuel/`, `drones/`, `fighters/` or `fleet/`
    prefix (fitting.HOLD_BAYS' keys), case-insensitive, then exactly the
    stat: comparison grammar over what is left -- so the
    operator rules, the range form and every malformed shape are decided in
    one place. An unrecognised prefix is not an error: it stays part of the
    type name, which then matches nothing, because a half-typed `car/Anti…`
    should behave like any other name the estate does not hold rather than
    silently dropping back to the whole ship and reporting counts for the
    wrong scope.
    """
    rest = value
    bay = None
    lowered = value.lstrip().lower()
    for prefix, name in _BAY_PREFIXES.items():
        if lowered.startswith(prefix):
            bay = name
            rest = value.lstrip()[len(prefix):]
            break
    term = parse_stat(rest)
    if term is None:
        return None
    return HoldsTerm(bay=bay, name=term.name, op=term.op, low=term.low, high=term.high)


def _number(value: float) -> str:
    """A count as someone would type it: 500, not 500.0."""
    return str(int(value)) if float(value).is_integer() else str(value)


def holds_value(term: HoldsTerm) -> str:
    """The chip value for a HoldsTerm: parse_holds' inverse.

    The card writes chips through here so a chip it wrote and a chip the
    user typed are the same string, which is what makes a saved view of
    either recall identically.
    """
    prefix = f"{term.bay}/" if term.bay else ""
    if term.op == "..":
        return f"{prefix}{term.name}={_number(term.low)}..{_number(term.high or 0)}"
    return f"{prefix}{term.name}{term.op}{_number(term.low)}"


def holds_column_key(term: HoldsTerm) -> str:
    """The model key of the count column a holds chip grows.

    Bay and name only, case-folded: two chips comparing the same consumable
    in the same bay against different thresholds are two filters over ONE
    column, and giving them a column each would show the same numbers twice.
    """
    return f"{HOLDS_KIND}:{term.bay or 'all'}/{term.name.casefold()}"


def holds_column_header(term: HoldsTerm) -> str:
    """The count column's header: the type name, and the bay when narrowed."""
    return f"{term.name} · {term.bay}" if term.bay else term.name


def single_holds_terms(spec: FilterSpec) -> list[HoldsTerm]:
    """The positive holds terms of a spec, in order, one per count column.

    Deduplicated by column key for the reason holds_column_key gives.
    Negated chips grow no column: "the ships short of paste" is answered by
    the rows themselves, and a column of numbers all below the threshold
    adds nothing. Lives here rather than in the view so the rule is Qt-free
    and testable, the way the abyssal column gate should have been.
    """
    terms: list[HoldsTerm] = []
    seen: set[str] = set()
    for c in spec.chips:
        if c.kind != HOLDS_KIND or c.negated:
            continue
        term = parse_holds(c.value)
        if term is None:
            continue
        key = holds_column_key(term)
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
    return terms


def named_fit(spec: FilterSpec) -> str | None:
    """The fit the inspector and the comparison window diff against.

    The first positive fit chip when there is one; otherwise the first
    negated one, because a user filtering `-fit:"Ratting"` is looking at the
    ships that deviate from Ratting and wants each one measured against
    Ratting, not against whichever stored fit happens to sit closest. None
    when nothing names a fit, which is the cue to fall back to the closest
    stored fit for the hull (fits.choose_fit).
    """
    for c in spec.chips:
        if c.kind == FIT_KIND and not c.negated:
            return c.value
    for c in spec.chips:
        if c.kind == FIT_KIND and c.negated:
            return c.value
    return None


# The chip kinds that mean "I am looking for a particular thing": a category,
# a group or an item named outright, or an abyssal module by type, stat or
# roll. With one of these in the filter the fitted rows come back, because
# someone filtering cat:Module or naming the launcher wants to see where
# every one of them is, racks included. The abyssal kinds belong here
# because a mutated module spends most of its life in a slot: hiding fitted
# rows under `abyssal` emptied the table, the "N of M" count, the card's type
# picker and its roll columns for exactly the modules the search is for.
_SPECIFIC_KINDS = ("category", "group", "item", ABYSSAL_KIND, STAT_KIND, ROLL_KIND)


def hides_fitted(spec: FilterSpec) -> bool:
    """Whether the Assets tab should hide what sits in fitted modules' slots.

    True for the everyday filters -- nothing, a station, an owner, a bare
    word -- where a rack of launchers and the rounds inside them are noise
    between the hangar rows. False when the filter is about ships or fits (a
    positive cat:Ship, is:fitted, is:fit, any fit: chip) or names the kind of
    thing outright (a positive category, group, item, abyssal, stat: or
    roll: chip; `is:abyssal` parses to the abyssal kind), since then the
    fitted rows are part of the answer. The rule lives here so the view, its
    counts, the rail and the strip all ask the same question.
    """
    for c in spec.chips:
        if c.kind == FIT_KIND:
            return False
        if c.negated:
            continue
        # is:unpriced joins the ship and fit flags: the strip's unpriced badge
        # counts the whole estate, and its SHOW button must list the same
        # rows, fitted modules included, or the badge says 39 and the table
        # shows 10.
        if c.kind == "is" and c.value in ("fitted", "fit", "unpriced"):
            return False
        if c.kind in _SPECIFIC_KINDS:
            return False
    return True


def deviation_filter_active(spec: FilterSpec) -> bool:
    """Whether the table is filtered to ships that deviate from a fit.

    True for a negated fit chip (`-fit:"Ratting"`) or `-is:fit`; the row
    menu offers Compare deviation only then, so the entry appears exactly
    when the rows on screen are the deviating ones and the question "from
    what?" already has an answer.
    """
    for c in spec.chips:
        if c.negated and (c.kind == FIT_KIND or (c.kind == "is" and c.value == "fit")):
            return True
    return False


def _tokenize(raw: str) -> list[str]:
    """Split on whitespace, except inside double quotes.

    Quotes glue rather than delimit: `loc:"Jita IV - Moon 4"` is one token
    with the quotes still attached, so the prefix split below sees them and
    _unquote can resolve just the value part. Inside a quoted region a
    backslash escapes a following quote or backslash -- to_text() emits those
    escapes for values that themselves contain a quote, and if the tokenizer
    let that escaped quote toggle the quoted state it would swallow every
    later token into one (the saved-view corruption this pair of functions is
    property-tested against). The escape pair is kept verbatim here so a
    token that falls back to bare text keeps exactly what the user typed.
    """
    tokens: list[str] = []
    buf: list[str] = []
    quoted = False
    i = 0
    while i < len(raw):
        ch = raw[i]
        if quoted and ch == "\\" and i + 1 < len(raw) and raw[i + 1] in '"\\':
            buf.append(ch)
            buf.append(raw[i + 1])
            i += 2
            continue
        if ch == '"':
            quoted = not quoted
            buf.append(ch)
        elif ch.isspace() and not quoted:
            if buf:
                tokens.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
        i += 1
    if buf:
        tokens.append("".join(buf))
    return tokens


def _unquote(value: str) -> str:
    """Resolve quoting and escapes in a chip value: the inverse of _quote_value.

    Unescaped quotes delimit and are dropped; inside them, backslash-escaped
    quotes and backslashes become literal. Outside a quoted region a
    backslash is an ordinary character -- station names do not contain them,
    but a half-typed Windows-path-looking search must not eat its own
    separators.
    """
    out: list[str] = []
    quoted = False
    i = 0
    while i < len(value):
        ch = value[i]
        if quoted and ch == "\\" and i + 1 < len(value) and value[i + 1] in '"\\':
            out.append(value[i + 1])
            i += 2
            continue
        if ch == '"':
            quoted = not quoted
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _quote_value(value: str) -> str:
    """Serialise one chip value so parse() reads it back verbatim.

    Quoting triggers on any whitespace (the tokenizer splits on isspace, not
    just on spaces), on an embedded quote (which must also be escaped, or it
    would toggle the tokenizer's quoted state mid-value), and on the empty
    string (an unquoted `loc:` is the half-typed-token shape parse treats as
    bare text). Backslashes are escaped first so the quote escapes survive.
    """
    if value and '"' not in value and not any(ch.isspace() for ch in value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def prefix_for_kind(kind: str) -> str:
    """The spelling to_text() writes for a chip kind.

    Public so anything labelling a chip outside this module -- views.py's
    suggested view names, for one -- says `loc` where the grammar says `loc`
    rather than inventing a second vocabulary that drifts from this one.
    """
    return _KIND_TO_PREFIX.get(kind, kind)


def extract_commands(raw: str) -> tuple[str, list[tuple[str, str]]]:
    """Split a committed line into its filter remainder and its commands.

    Returns the line with every command token removed (the surviving tokens
    verbatim, in order, joined by single spaces) and the commands as
    (kind, value) pairs. A token counts as a command when the prefix before
    its first colon lower-cases to a COMMAND_KINDS entry, it does not start
    with a quote and it carries no leading `-`: `"save:x"` is someone
    searching for that literal text and `-save:x` is a negation of a chip
    kind that does not exist, and neither should silently write to the
    library. The value may be empty -- a bare `save:` is the request to open
    the Save card.

    It lives here rather than in the omnibox because the quote-aware
    tokenizer must stay single-sourced (a second splitter is how quoted
    values start disagreeing with each other) and because a rule this
    load-bearing has to be testable without a window. parse() deliberately
    does not call it: the omnibox strips the commands first and hands parse()
    the remainder, so a command can never reach a FilterSpec.
    """
    kept: list[str] = []
    commands: list[tuple[str, str]] = []
    for token in _tokenize(raw or ""):
        prefix, sep, value = token.partition(":")
        if sep and not token.startswith('"') and prefix.lower() in COMMAND_KINDS:
            commands.append((prefix.lower(), _unquote(value)))
        else:
            kept.append(token)
    return " ".join(kept), commands


def _vocabulary(known) -> dict[str, dict[str, str]]:
    """kind -> {lowered value: as stored}, for resolving unquoted values.

    Lowered because chip comparisons fold case (COLLATE NOCASE), so the parser
    has to agree with the SQL about what counts as the same value.
    """
    if not known:
        return {}
    return {
        kind: {str(v).lower(): str(v) for v in values if v}
        for kind, values in known.items()
    }


def _extend_value(vocab: dict[str, str], tokens: list[str], start: int, first: str):
    """Longest run of following words that is a real value, or None.

    This is what lets `owner:Test Pilot` work without quotes. The naive rule --
    swallow words until the next prefix -- cannot be used, because it would
    turn `owner:Main tritanium` into a search for an owner of that name and
    silently drop the text search. Matching against the values that actually
    exist resolves it: "Main tritanium" is not an owner, "Main" is, so the
    remainder stays a search word.

    Longest first, so `owner:Main` still wins outright when there is no longer
    match, and a genuine "Main Fleet" beats the "Main" prefix of it.
    """
    limit = len(tokens)
    # Only bare words may be absorbed. A quoted token was deliberately
    # delimited, and one carrying its own prefix starts the next chip.
    end = start + 1
    while end < limit:
        nxt = tokens[end]
        if nxt.startswith('"'):
            break
        body = nxt[1:] if nxt.startswith("-") and len(nxt) > 1 else nxt
        prefix, sep, _ = body.partition(":")
        if sep and (prefix.lower() in _PREFIX_TO_KIND or prefix.lower() in ("is", "val")):
            break
        end += 1

    for stop in range(end, start, -1):
        parts = ([first] if first else []) + tokens[start + 1:stop]
        candidate = " ".join(parts).strip()
        if candidate and candidate.lower() in vocab:
            return vocab[candidate.lower()], stop
    return None


def parse(raw: str, known=None) -> FilterSpec:
    """Parse omnibox text into a FilterSpec.

    Forgiving by design: anything that does not parse as a chip -- an unknown
    prefix, an is: flag this app has never heard of, a malformed val:
    comparison -- degrades to a bare search word rather than raising. The
    omnibox re-parses on every keystroke, so half-typed tokens are the normal
    case, not an error.

    `known` maps a chip kind to the values that actually exist, and is what
    lets an unquoted `owner:Test Pilot` work. Optional: without it, values with
    spaces must be quoted, which is the behaviour every existing caller and
    saved view already relies on. Passing it can only make more things parse,
    never fewer.
    """
    vocab = _vocabulary(known)
    words: list[str] = []
    chips: list[Chip] = []
    tokens = _tokenize(raw or "")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        body = token
        negated = False
        if body.startswith("-") and len(body) > 1:
            negated = True
            body = body[1:]

        prefix, sep, raw_value = body.partition(":")
        value = _unquote(raw_value)
        chip = None
        # A quoted empty value (`loc:""`) is a deliberate empty-label filter
        # and to_text() serialises empty-value chips exactly that way, so the
        # two halves agree; an unquoted empty value (`loc:`) is the normal
        # half-typed state and stays bare text.
        quoted = '"' in raw_value
        kind = _PREFIX_TO_KIND.get(prefix.lower()) if sep else None

        # An unquoted value may run on into the following words. Tried before
        # the checks below so that `owner:` alone, which is otherwise the
        # half-typed state and stays bare text, can still start a real value.
        #
        # Only ever fires for a prefixed token, so it cannot swallow the bare
        # `abyssal` chip handled just below.
        if kind is not None and not quoted and vocab.get(kind):
            extended = _extend_value(vocab[kind], tokens, index - 1, value)
            if extended is not None:
                resolved_value, stop = extended
                chips.append(Chip(kind=kind, value=resolved_value, negated=negated))
                index = stop
                continue

        if not sep and body.lower() == ABYSSAL_KIND:
            # The one bare word that is a chip. Anyone hunting an item whose
            # name contains the word has the quoted form, which to_text()
            # emits for exactly this collision.
            chip = Chip(kind=ABYSSAL_KIND, value="", negated=negated)
        elif sep and (value or quoted):
            if kind is not None:
                chip = Chip(kind=kind, value=value, negated=negated)
            elif prefix.lower() == ABYSSAL_KIND:
                # Kept as typed, not normalised through split/join_types, so
                # to_text() round-trips any value; split_types reads it.
                chip = Chip(kind=ABYSSAL_KIND, value=value, negated=negated)
            elif prefix.lower() == "is" and value.lower() == ABYSSAL_KIND:
                # `is:abyssal` is an alias for the bare chip, kept because
                # saved views and habit carry it; the negation carries over,
                # since -is:abyssal and -abyssal ask the same question of a
                # NOT NULL flag.
                chip = Chip(kind=ABYSSAL_KIND, value="", negated=negated)
            elif prefix.lower() == "is" and value.lower() in IS_FLAGS:
                chip = Chip(kind="is", value=value.lower(), negated=negated)
            elif prefix.lower() == "val" and _VAL_RE.match(value):
                chip = Chip(kind="val", value=value, negated=negated)
            elif prefix.lower() in (STAT_KIND, ROLL_KIND) and parse_stat(value) is not None:
                chip = Chip(kind=prefix.lower(), value=value, negated=negated)
            elif prefix.lower() == HOLDS_KIND and parse_holds(value) is not None:
                # The stat: rule: a comparison that does not parse is a
                # half-typed token, not a chip that filters on nothing.
                chip = Chip(kind=HOLDS_KIND, value=value, negated=negated)
            elif prefix.lower() == FIT_KIND:
                # The level rule instead: a fit name is a label, and any
                # string at all is a legitimate one -- including the empty
                # name a card writes before anything is picked. A name no
                # stored fit carries matches nothing, which is the same
                # answer loc: gives for a station that has been unanchored.
                chip = Chip(kind=FIT_KIND, value=value, negated=negated)
        if chip is not None:
            chips.append(chip)
        elif token.startswith('"'):
            # A quoted bare token is the escape hatch for text that would
            # otherwise read as a token; to_text() wraps such words, so the
            # quotes come off here to complete the round trip.
            words.append(_unquote(token))
        else:
            words.append(token)
    return FilterSpec(text=" ".join(words), chips=chips)
