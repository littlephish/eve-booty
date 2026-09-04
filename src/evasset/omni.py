"""The omnibox filter grammar and its translation to SQL.

One grammar, parsed in one place, because the filter state used to be spread
over a search field, three combo boxes and two checkboxes -- six widgets that
each owned a fragment of the WHERE clause and had to be kept mutually
consistent by hand. Everything that narrows the asset table is now a token in
a single string: `prefix:value` chips for the grouping levels, `is:` flags,
`val:`, `stat:` and `roll:` comparisons, the `abyssal` chip, and bare words.
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

from . import abyssal, queries
from .config import ASSET_SAFETY_LOCATION_ID

# The chip kinds that filter one grouping level by exact label. Six of them
# come from queries.ROLLUP_LEVELS; meta is extra -- it is not a rail grouping level,
# but "show me only Tech II" is too useful to leave out of the grammar.
LEVEL_KINDS = ("location", "system", "region", "owner", "category", "group", "meta")

IS_FLAGS = ("fitted", "safety", "unpriced", "bpc")

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

# Positive and negated SQL per is: flag. Each pair is written out rather than
# generated by wrapping NOT(...) because the naive negation is wrong for some
# of them: NULL root_location_id rows must survive -is:safety, and the
# fitted pair already exists in both polarities.
_IS_SQL = {
    "fitted": (_FITTED_EXISTS, queries.HIDE_SHIP_CONTENTS_CLAUSE),
    "safety": (
        f"a.root_location_id = {ASSET_SAFETY_LOCATION_ID}",
        f"a.root_location_id IS NOT {ASSET_SAFETY_LOCATION_ID}",
    ),
    "unpriced": (
        "COALESCE(p.source, 'none') = 'none'",
        "COALESCE(p.source, 'none') <> 'none'",
    ),
    "bpc": ("a.is_blueprint_copy = 1", "a.is_blueprint_copy = 0"),
}

# The abyssal chip's positive clause: the type flag, narrowed to named types
# when the chip carries any. {names} is a list of `?` marks, one per type,
# so the names are bound, never interpolated.
_ABYSSAL_ALL = "t.is_dynamic_type = 1"
_ABYSSAL_TYPES = "(t.is_dynamic_type = 1 AND t.name IN ({names}))"


@dataclass
class Chip:
    """One parsed token: a level filter, an is: flag, a comparison, or the abyssal chip."""

    kind: str  # one of LEVEL_KINDS, or "item", "is", "val", STAT_KIND, ROLL_KIND, ABYSSAL_KIND
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
            # A bare word that would re-tokenise as a chip (someone searched
            # the literal text "cat:mystery"), or that carries a quote, gets
            # wrapped -- parse() unquotes a quoted bare token back to text,
            # so the word survives recall instead of quietly becoming a
            # different filter than the one that was saved.
            if '"' in word or parse(word).chips:
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

        for kind in (*LEVEL_KINDS, "item"):
            # item is not a grouping level, but it filters exactly like one:
            # an exact match on a single expression, OR-able and negatable.
            # The .get default serves meta, the one LEVEL_KINDS entry with no
            # rail level behind it (see the LEVEL_KINDS comment above).
            expr = "t.name" if kind == "item" else queries.OVERVIEW_FILTER_EXPR.get(kind, "mg.name")
            positive = [c for c in chips if c.kind == kind and not c.negated]
            if positive:
                clauses.append("(" + " OR ".join([f"{expr} = ?"] * len(positive)) + ")")
                params.extend(c.value for c in positive)
            for c in chips:
                if c.kind == kind and c.negated:
                    # Keep NULL-labelled rows: excluding "Tech II" must not
                    # also hide every item that has no meta group at all.
                    clauses.append(f"({expr} IS NULL OR {expr} <> ?)")
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


def parse(raw: str) -> FilterSpec:
    """Parse omnibox text into a FilterSpec.

    Forgiving by design: anything that does not parse as a chip -- an unknown
    prefix, an is: flag this app has never heard of, a malformed val:
    comparison -- degrades to a bare search word rather than raising. The
    omnibox re-parses on every keystroke, so half-typed tokens are the normal
    case, not an error.
    """
    words: list[str] = []
    chips: list[Chip] = []
    for token in _tokenize(raw or ""):
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
        if not sep and body.lower() == ABYSSAL_KIND:
            # The one bare word that is a chip. Anyone hunting an item whose
            # name contains the word has the quoted form, which to_text()
            # emits for exactly this collision.
            chip = Chip(kind=ABYSSAL_KIND, value="", negated=negated)
        elif sep and (value or '"' in raw_value):
            kind = _PREFIX_TO_KIND.get(prefix.lower())
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
