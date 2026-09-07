"""Stored fits: the EFT parser, the fit store, and the ship-against-fit diff.

A fit is a shopping list of modules with a name and a hull, pasted as EFT
text and kept in the database so the omnibox can ask "which of my ships are
fitted the way I meant them to be". The verdict is deliberately a multiset
comparison by type_id and nothing more: slot order is not part of what a
pilot means by "the Ratting Dominix", EFT itself does not record which
numbered slot a module sits in, and a fit that failed because the repairer
moved from LoSlot2 to LoSlot3 would be useless. Drones and cargo are parsed
but do not decide the verdict -- they feed the inspector's "short" line and
the comparison window's Drones and Cargo sections, since a ship being 200
rounds down is a supply problem, not a wrong fit.

Qt-free like fitting.py and pricing.py. The writes live here beside their
reads rather than in queries.py, following stockpile.py: this is the one
module that owns the fits tables, so the parser, the store and the diff can
be read together and a caller never has to know which half lives where.

EFT is Pyfa's plain-text format; the line shapes parsed below (a
`[Hull, Name]` header, `Module, Charge`, `Name xN` with no thousands
separator, `[Empty High slot]` placeholders, blank lines between racks) are
the ones fitting.to_eft emits, taken from Pyfa's own
service/port/eft.py -- see the comment above to_eft for the reference.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import fitting, queries

# The kinds a parsed line can carry. The first three decide a `fit:` verdict;
# the last two are consumables, reported as "short" and never as a deviation.
SLOT_KINDS = ("module", "rig", "subsystem", "drone", "cargo")
VERDICT_KINDS = ("module", "rig", "subsystem")

# SDE category ids, CCP constants: 6 Ship, 7 Module, 18 Drone, 32 Subsystem,
# 87 Fighter. Keyed on the id rather than the name because the names are the
# part CCP renames (see the "meta group" precedent), and the ids never move.
_CATEGORY_SHIP = 6
_CATEGORY_MODULE = 7
_CATEGORY_DRONE = 18
_CATEGORY_SUBSYSTEM = 32
_CATEGORY_FIGHTER = 87

# Pyfa's own import regexes, narrowed to what a fit line can be:
#   `Name xN`      a stack of drones, fighters or cargo. Pyfa's pattern is
#                  `x(?P<amount>\d+)` with no comma; this one also takes a
#                  count written with thousands separators (`x1,000`,
#                  `x12,500`), because `Scarab Combat Drone x1,000` can only
#                  mean a thousand and the refusal used to send it down the
#                  `Module, Charge` path as an unknown module "... x1" plus
#                  an unknown charge "000". Only well-formed groups of three
#                  qualify; `x1,00` still falls through to that path, where
#                  the unknown names block the save with the text on screen.
#   `Name /OFFLINE` a module the fit leaves offline. It is still fitted, so
#                  the suffix is dropped and the module counts.
#   `[Empty ...]`  a placeholder Pyfa writes for an unused slot.
_STACK_RE = re.compile(r"^(?P<name>.+?)\s+x(?P<amount>[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)$")
_OFFLINE_RE = re.compile(r"\s*/OFFLINE\s*$", re.IGNORECASE)
_EMPTY_SLOT_RE = re.compile(r"^\[\s*empty\b.*\]$", re.IGNORECASE)
_HEADER_RE = re.compile(r"^\[(?P<body>[^\]]*)\]$")

# A group whose name starts with this is a rig, the one distinction the
# category id cannot make: rigs are category 7 (Module) like every other
# module, and only the group ("Rig Armor", "Rig Energy Grid", ...) says
# otherwise. Checked case-insensitively with the trailing space, so a group
# called "Rigging Tools" would never be mistaken for one.
_RIG_GROUP_PREFIX = "rig "

# U+FEFF, written as an escape because an editor that sees it as a literal
# character tends to eat it. Notepad and several browsers put one at the
# front of a copied fit, and a hull name carrying it resolves against nothing.
_BOM = "\ufeff"


@dataclass
class FitItem:
    """One line of a parsed fit, already aggregated: N of a type, in one kind."""

    type_id: int
    name: str
    slot_kind: str
    quantity: int


@dataclass
class ParsedFit:
    """Everything parse_eft could make of a paste, including what it could not.

    hull_type_id is None when the paste carries no usable `[Hull, Name]`
    header, names a hull the SDE has never heard of, or names a type that is
    not a ship (hull_name still carries the text either way, so the card can
    say which of the three it was); unknown lists every
    item name that did not resolve, verbatim and in order, so the card can
    say which line to fix rather than silently storing a smaller fit than
    the one that was pasted. charges counts the loaded charges named on
    `Module, Charge` lines -- EFT carries no quantity for them, so they are
    reported and not stored.
    """

    hull_type_id: int | None = None
    hull_name: str = ""
    name: str = ""
    items: list[FitItem] = field(default_factory=list)
    charges: dict[int, int] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)
    empty_slots: int = 0

    @property
    def ok(self) -> bool:
        """Whether this fit can be saved: a known hull and no unknown names.

        An unknown name is fatal rather than skippable because a fit stored
        with a line missing would report every ship carrying that module as
        deviating -- an answer that looks authoritative and is wrong.
        """
        return self.hull_type_id is not None and not self.unknown

    def verdict_items(self) -> list[FitItem]:
        return [i for i in self.items if i.slot_kind in VERDICT_KINDS]

    def consumable_items(self) -> list[FitItem]:
        return [i for i in self.items if i.slot_kind not in VERDICT_KINDS]


def classify(conn: sqlite3.Connection, type_id: int, group_id: int | None, stacked: bool) -> str:
    """Which SLOT_KIND a fit line belongs to.

    The EFT line shape decides first: a `Name xN` stack is never a fitted
    module, so it is a drone (category Drone or Fighter) or cargo. An
    unstacked line is fitted, and is a subsystem, a rig or a plain module.
    A type the SDE cannot place -- no group row, no category row -- falls
    through to "cargo" when stacked and "module" when not, which keeps it in
    the fit rather than discarding it.
    """
    category_id, group_name = _group_facts(conn, group_id)
    if stacked:
        return "drone" if category_id in (_CATEGORY_DRONE, _CATEGORY_FIGHTER) else "cargo"
    if category_id == _CATEGORY_SUBSYSTEM:
        return "subsystem"
    if category_id == _CATEGORY_MODULE and (group_name or "").lower().startswith(_RIG_GROUP_PREFIX):
        return "rig"
    return "module"


def _group_facts(conn: sqlite3.Connection, group_id: int | None) -> tuple[int | None, str | None]:
    if group_id is None:
        return None, None
    row = conn.execute(
        "SELECT category_id, name FROM sde_groups WHERE group_id = ?", (int(group_id),)
    ).fetchone()
    if row is None:
        return None, None
    return row["category_id"], row["name"]


def _resolve(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """The type a fit line names, matched case-insensitively.

    Published types win over unpublished ones: CCP leaves retired duplicates
    of a name in the SDE, and a paste naming "Warp Scrambler II" means the
    one that can still be fitted. The type id breaks a remaining tie so the
    answer is stable between calls.
    """
    return conn.execute(
        "SELECT type_id, group_id FROM sde_types WHERE name = ? COLLATE NOCASE "
        "ORDER BY published DESC, type_id LIMIT 1",
        (name,),
    ).fetchone()


def _clean_lines(text: str) -> list[str]:
    """The paste as lines, with the noise a clipboard adds already gone.

    A BOM survives a copy out of Notepad, CRLF survives every Windows
    clipboard, and trailing spaces survive Pyfa's own export -- each of them
    would otherwise make an exact type name fail to resolve.
    """
    text = (text or "").lstrip(_BOM).replace("\r\n", "\n").replace("\r", "\n")
    return [line.strip() for line in text.split("\n")]


def parse_eft(conn: sqlite3.Connection, text: str) -> ParsedFit:
    """Read EFT text into a ParsedFit, resolving every name against the SDE.

    Forgiving in the same way the omnibox grammar is: a line this parser has
    no rule for is a module, because that is what an EFT body is mostly made
    of, and a name that does not resolve is recorded rather than dropped.
    The one thing it will not do is guess a hull -- without the header there
    is no way to know which ships the fit should be compared against, so
    hull_type_id stays None and the card refuses to save.
    """
    parsed = ParsedFit()
    lines = _clean_lines(text)
    body: list[str] = []
    seen_header = False
    for line in lines:
        if not line:
            continue
        if line.startswith("//") or line.startswith("#"):
            continue
        if _EMPTY_SLOT_RE.match(line):
            parsed.empty_slots += 1
            continue
        if not seen_header:
            seen_header = True
            header = _HEADER_RE.match(line)
            if header is not None:
                _read_header(conn, parsed, header.group("body"))
                continue
            # No header at all: the first line is already a module, and the
            # fit is unsaveable but still worth showing the user parsed.
        body.append(line)

    counts: dict[tuple[int, str], FitItem] = {}
    for line in body:
        _read_body_line(conn, parsed, line, counts)
    parsed.items = list(counts.values())
    return parsed


def _read_header(conn: sqlite3.Connection, parsed: ParsedFit, body: str) -> None:
    """Fill hull and name from the `[Hull, Name]` header's contents.

    A header with no comma is treated as hull-only rather than as no header:
    someone pruning the name out of a paste still means the hull they typed.
    An empty name becomes "<Hull> fit" so the stored fit always has something
    a `fit:` chip can address. The hull has to resolve to an SDE Ship for
    hull_type_id to be filled; see the category check below.
    """
    hull, _sep, name = body.partition(",")
    hull = hull.strip()
    parsed.hull_name = hull
    parsed.name = name.strip() or (f"{hull} fit" if hull else "")
    if not hull:
        return
    row = _resolve(conn, hull)
    if row is None:
        # The hull name is kept as typed so the card can show what failed;
        # hull_type_id stays None, which is what ok reads.
        return
    category_id, _group_name = _group_facts(conn, row["group_id"])
    if category_id != _CATEGORY_SHIP:
        # A header naming something that is not a ship -- `[Focused Pulse
        # Laser, T]`, most often a paste that lost its first line -- resolves
        # to a perfectly real type, but every `fit:` clause is scoped by
        # queries.SHIP_ROWS_CLAUSE, so a fit stored against it could never
        # match a row. Refusing it here means the card says so instead of
        # saving a fit that silently answers nothing.
        return
    parsed.hull_type_id = int(row["type_id"])


def _read_body_line(
    conn: sqlite3.Connection,
    parsed: ParsedFit,
    line: str,
    counts: dict[tuple[int, str], FitItem],
) -> None:
    line = _OFFLINE_RE.sub("", line).strip()
    if not line:
        return
    charge_name = ""
    stacked = False
    quantity = 1
    stack = _STACK_RE.match(line)
    if stack is not None:
        stacked = True
        line = stack.group("name").strip()
        quantity = int(stack.group("amount").replace(",", ""))
        if quantity == 0:
            # `Name x0` is a row that says nothing: stored, it would put a
            # zero-quantity item in the fit that no ship can ever be short
            # of while still inflating the card's counts and the "N modules"
            # summary. Dropping it is what the line means.
            return
    else:
        # `Module, Charge`: the module is the line, the charge is tallied
        # separately. Only the first comma splits -- no module name contains
        # one, and a charge name that did would otherwise lose its tail.
        name, sep, charge = line.partition(",")
        if sep:
            line, charge_name = name.strip(), charge.strip()
    if not line:
        return
    row = _resolve(conn, line)
    if row is None:
        parsed.unknown.append(line)
    else:
        type_id = int(row["type_id"])
        kind = classify(conn, type_id, row["group_id"], stacked)
        key = (type_id, kind)
        item = counts.get(key)
        if item is None:
            counts[key] = FitItem(type_id=type_id, name=line, slot_kind=kind, quantity=quantity)
        else:
            item.quantity += quantity
    if charge_name:
        charge_row = _resolve(conn, charge_name)
        if charge_row is None:
            parsed.unknown.append(charge_name)
        else:
            charge_id = int(charge_row["type_id"])
            parsed.charges[charge_id] = parsed.charges.get(charge_id, 0) + 1


# ------------------------------------------------------------------- store
def save_fit(conn: sqlite3.Connection, parsed: ParsedFit, name: str, eft_text: str) -> int:
    """Store a parsed fit under a name, replacing any fit of that name on the hull.

    The replacement is a delete followed by an insert rather than an UPSERT:
    the old fit's items have to go, and connect() sets
    `PRAGMA foreign_keys=ON`, so deleting the fits row cascades them away for
    free. Saving a second fit under an existing name is therefore an edit,
    not an IntegrityError surfacing in the card -- which is what a user
    pasting a revised rack under the same name means.

    A blank name falls back to the parsed one (never blank for a fit that
    parses), so the card cannot store an unaddressable fit by accident.
    """
    if not parsed.ok:
        raise ValueError("a fit with an unknown hull or unknown item names cannot be stored")
    name = (name or "").strip() or parsed.name
    conn.execute(
        "DELETE FROM fits WHERE hull_type_id = ? AND name = ? COLLATE NOCASE",
        (parsed.hull_type_id, name),
    )
    cur = conn.execute(
        "INSERT INTO fits (name, hull_type_id, eft_text, created_at) VALUES (?,?,?,?)",
        (
            name,
            parsed.hull_type_id,
            eft_text or "",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    fit_id = int(cur.lastrowid)
    conn.executemany(
        "INSERT INTO fit_items (fit_id, type_id, slot_kind, quantity) VALUES (?,?,?,?)",
        [(fit_id, i.type_id, i.slot_kind, i.quantity) for i in parsed.items],
    )
    return fit_id


def delete_fit(conn: sqlite3.Connection, fit_id: int) -> None:
    """Forget one fit. Its items go with it through the foreign key's cascade."""
    conn.execute("DELETE FROM fits WHERE fit_id = ?", (int(fit_id),))


_FIT_ROWS = """
SELECT f.fit_id,
       f.name,
       f.hull_type_id,
       t.name AS hull,
       f.eft_text,
       f.created_at,
       COALESCE((SELECT SUM(fi.quantity) FROM fit_items fi
                 WHERE fi.fit_id = f.fit_id
                   AND fi.slot_kind IN ('module','rig','subsystem')), 0) AS modules
FROM fits f
LEFT JOIN sde_types t ON t.type_id = f.hull_type_id
"""


def list_fits(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every stored fit for the card's list: hull first, then name.

    modules is the verdict multiset's size, the figure the card shows beside
    the name, so two fits of a hull can be told apart at a glance without
    opening either.
    """
    return list(conn.execute(
        _FIT_ROWS + " ORDER BY hull COLLATE NOCASE, f.name COLLATE NOCASE"
    ))


def fits_for_hull(conn: sqlite3.Connection, hull_type_id: int) -> list[sqlite3.Row]:
    """The stored fits of one hull, in the same shape and order as list_fits."""
    return list(conn.execute(
        _FIT_ROWS + " WHERE f.hull_type_id = ? ORDER BY f.name COLLATE NOCASE",
        (int(hull_type_id),),
    ))


def hull_has_fits(conn: sqlite3.Connection, hull_type_id: int) -> bool:
    """Whether any fit is stored for a hull -- the context menu's enable test.

    An EXISTS rather than len(fits_for_hull(...)): the menu is built on the
    GUI thread on every right-click, so the answer has to cost one index
    probe and no joins.
    """
    row = conn.execute(
        "SELECT 1 FROM fits WHERE hull_type_id = ? LIMIT 1", (int(hull_type_id),)
    ).fetchone()
    return row is not None


def fit_items(conn: sqlite3.Connection, fit_id: int) -> list[sqlite3.Row]:
    """One fit's stored lines, named, ordered by kind then name."""
    return list(conn.execute(
        """SELECT fi.type_id, t.name, fi.slot_kind, fi.quantity
           FROM fit_items fi
           LEFT JOIN sde_types t ON t.type_id = fi.type_id
           WHERE fi.fit_id = ?
           ORDER BY fi.slot_kind, t.name COLLATE NOCASE, fi.type_id""",
        (int(fit_id),),
    ))


# -------------------------------------------------------------------- diff
@dataclass
class FitDiff:
    """How one ship differs from one stored fit, as the inspector renders it.

    named says the fit was chosen by an active `fit:` chip rather than
    picked as the closest one, because "Deviates from Ratting" and "Closest
    stored fit: Ratting" are different claims and only the first is an
    answer to a question the user asked.
    """

    fit_id: int
    fit_name: str
    named: bool
    missing: list[tuple[str, int]]
    extra: list[tuple[str, int]]
    short: list[tuple[str, int, int]]

    @property
    def matches(self) -> bool:
        return not self.missing and not self.extra


def diff(
    ship_rows: list[sqlite3.Row], items: list[sqlite3.Row]
) -> tuple[list[tuple[str, int]], list[tuple[str, int]], list[tuple[str, int, int]]]:
    """(missing, extra, short) for a ship's contents against one fit's items.

    Pure, so the whole verdict can be checked against hand-computed
    expectations without a database. missing and extra are the two
    directions of the same multiset comparison queries.FIT_EQUAL runs in
    SQL, over fitting.fitted_modules' counts; short is the consumables
    line, over fitting.carried's: a drone line against the drone bays, a
    cargo line against every hold and the charges already loaded, because
    a pilot asking whether they have 2,000 rounds does not care which hold
    the rounds are in. The comparison window's Drones and Cargo sections
    read the same buckets, so a line short here is a red line there.
    """
    have = fitting.fitted_modules(ship_rows)
    aboard = fitting.carried(ship_rows)
    names: dict[int, str] = {}
    for r in ship_rows:
        names.setdefault(int(r["type_id"]), r["item"])

    want: dict[int, int] = {}
    for i in items:
        if i["slot_kind"] in VERDICT_KINDS:
            type_id = int(i["type_id"])
            want[type_id] = want.get(type_id, 0) + int(i["quantity"])
        names.setdefault(int(i["type_id"]), i["name"] or f"Type {i['type_id']}")

    def label(type_id: int) -> str:
        return names.get(type_id) or f"Type {type_id}"

    missing = [
        (label(t), n - have.get(t, 0)) for t, n in want.items() if have.get(t, 0) < n
    ]
    extra = [
        (label(t), n - want.get(t, 0)) for t, n in have.items() if n > want.get(t, 0)
    ]
    short = [
        (label(int(i["type_id"])), aboard.have(i["slot_kind"]).get(int(i["type_id"]), 0),
         int(i["quantity"]))
        for i in items
        if i["slot_kind"] not in VERDICT_KINDS
        and aboard.have(i["slot_kind"]).get(int(i["type_id"]), 0) < int(i["quantity"])
    ]
    missing.sort()
    extra.sort()
    short.sort()
    return missing, extra, short


@dataclass
class ChosenFit:
    """The stored fit a ship is held against, with the rows both sides read.

    One object rather than a bare fit_id because every caller that chooses a
    fit goes on to compare the same ship against it: the inspector's diff
    block and the comparison window must agree on which fit "the closest"
    is, and the only way to keep them agreeing is for one function to make
    the choice and hand back what it already computed on the way.
    """

    fit_id: int
    name: str
    hull: str
    eft_text: str
    named: bool
    ship_rows: list[sqlite3.Row]
    items: list[sqlite3.Row]
    missing: list[tuple[str, int]]
    extra: list[tuple[str, int]]
    short: list[tuple[str, int, int]]


def choose_fit(
    conn: sqlite3.Connection,
    ship_item_id: int,
    hull_type_id: int,
    fit_name: str | None = None,
) -> ChosenFit | None:
    """Which stored fit a ship is compared against, or None with none stored.

    A `fit:` chip naming a fit of this hull wins, because the user has just
    said which fit they are asking about. Otherwise the closest fit is
    chosen -- fewest missing plus extra, ties to the lowest fit_id so the
    answer does not shuffle between reloads -- since a ship that deviates
    from every stored fit is most usefully described by the one it is
    nearest to.
    """
    candidates = fits_for_hull(conn, hull_type_id)
    if not candidates:
        return None
    named_row = None
    if fit_name:
        wanted = fit_name.casefold()
        named_row = next((r for r in candidates if (r["name"] or "").casefold() == wanted), None)

    ship_rows = queries.fetch_fit(conn, ship_item_id)

    def against(row: sqlite3.Row, named: bool) -> ChosenFit:
        items = fit_items(conn, int(row["fit_id"]))
        missing, extra, short = diff(ship_rows, items)
        return ChosenFit(
            int(row["fit_id"]), row["name"], row["hull"] or "", row["eft_text"] or "",
            named, ship_rows, items, missing, extra, short,
        )

    if named_row is not None:
        return against(named_row, True)

    best: ChosenFit | None = None
    best_key: tuple[int, int] | None = None
    for row in sorted(candidates, key=lambda r: int(r["fit_id"])):
        chosen = against(row, False)
        key = (len(chosen.missing) + len(chosen.extra), chosen.fit_id)
        if best_key is None or key < best_key:
            best_key = key
            best = chosen
    return best


def diff_for_ship(
    conn: sqlite3.Connection,
    ship_item_id: int,
    hull_type_id: int,
    fit_name: str | None = None,
) -> FitDiff | None:
    """The fit-diff block for one ship, or None when its hull has no stored fit.

    The fit is choose_fit's; this only reshapes the answer into what the
    inspector renders.
    """
    chosen = choose_fit(conn, ship_item_id, hull_type_id, fit_name)
    if chosen is None:
        return None
    return FitDiff(
        chosen.fit_id, chosen.name, chosen.named, chosen.missing, chosen.extra, chosen.short
    )


# ----------------------------------------------------------------- compare
# What a comparison line can be. A line is on both sides ("match"), on the
# ship with no counterpart in the fit ("extra", left side only), in the fit
# with no counterpart on the ship ("missing", right side only), or an
# `[Empty X slot]` placeholder the EFT text carries ("empty"), kept so the
# fit's rack reads as it was written rather than closing up. "short" is the
# hold sections' own: the ship's side of a drone or cargo line it carries
# fewer of than the fit asks, opposite a "missing" line on the fit's side.
MATCH, EXTRA, MISSING, EMPTY, SHORT = "match", "extra", "missing", "empty", "short"

# The two sections drawn under the racks, in this order: what the fit's
# stacked lines (`Name xN`) are, split by classify's two consumable kinds.
# "extra" in these sections is surplus, not a deviation -- a ship carrying
# paste the fit never listed is not wrongly fitted -- so the window draws
# it in the ordinary text colour where a rack's extra is red.
DRONES_SECTION, CARGO_SECTION = "Drones", "Cargo"
HOLD_DISPLAY_ORDER = (DRONES_SECTION, CARGO_SECTION)
_SECTION_OF_KIND = {"drone": DRONES_SECTION, "cargo": CARGO_SECTION}

# The rack a placeholder names, by the word Pyfa writes between "Empty" and
# "slot" (service/port/eft.py: Low, Med, High, Rig, Subsystem), with the
# spellings a hand-typed fit is likely to use as well. Indexes into
# fitting.EFT_RACK_ORDER so the labels stay the export's own.
_EMPTY_SLOT_WORDS = {
    "low": 0, "lo": 0,
    "med": 1, "mid": 1, "medium": 1,
    "high": 2, "hi": 2,
    "rig": 3,
    "subsystem": 4, "sub": 4,
}
_EMPTY_SLOT_WORD_RE = re.compile(r"^\[\s*empty\s+(?P<word>[a-z]+)", re.IGNORECASE)

# Where a verdict item lands on the fit side when the stored text does not
# place it: no blank-line sections at all (a fit stored by a build before the
# text was kept, or a one-rack paste), or a line that no longer resolves.
# Rigs and subsystems still take the ship's own rack labels so they line up;
# plain modules have no rack of their own and go under a label the ship side
# never uses.
_FALLBACK_RACKS = {
    "module": "Modules",
    "rig": fitting.RACK_OF_FLAG["RigSlot0"],
    "subsystem": fitting.RACK_OF_FLAG["SubSystemSlot0"],
}
RACK_DISPLAY_ORDER = (*fitting.RACK_ORDER, _FALLBACK_RACKS["module"])


@dataclass
class CompareLine:
    """One line of one rack in the comparison window.

    quantity is the count the line stands for, never the whole type's --
    a type the ship carries three of where the fit asks for two renders as
    a "2 ×" match line and a "1 ×" extra line, so every line has one status
    and one colour. loaded is the greyed charge note a ship line carries
    ("loaded: 50 × Antimatter Charge M"), empty on the fit side and on an
    unloaded module; an EMPTY line's name is the placeholder as written.
    wanted is set on a SHORT line only: the fit's figure the ship's count
    falls short of, so the line can read "40 × Nanite Repair Paste (of 100)"
    without the reader looking across the column.
    """

    name: str
    quantity: int
    status: str
    type_id: int | None = None
    loaded: str = ""
    wanted: int = 0


@dataclass
class FitComparison:
    """Two rack listings ready to be rendered side by side, and the verdict.

    ship and fit are (rack label, lines) in fitting.RACK_ORDER, high to low,
    so the same rack sits at the same height on both sides; a rack is absent
    from a side that has nothing in it. Statuses are decided over the whole
    verdict multiset, not per rack: the pilot's question is "is this module
    on the ship", not "is it in the slot the text happened to list it in",
    which is also why a permuted rack matches. ship_holds and fit_holds are
    the Drones and Cargo sections drawn under the racks in HOLD_DISPLAY_ORDER,
    the fit's consumable lines on the right and what the ship carries on the
    left, built over fitting.carried's buckets. missing, extra and short are
    diff()'s lists, so the window's headline and the inspector's block can
    never disagree about the numbers.
    """

    fit_id: int
    fit_name: str
    hull: str
    named: bool
    ship: list[tuple[str, list[CompareLine]]]
    fit: list[tuple[str, list[CompareLine]]]
    missing: list[tuple[str, int]]
    extra: list[tuple[str, int]]
    short: list[tuple[str, int, int]]
    ship_holds: list[tuple[str, list[CompareLine]]] = field(default_factory=list)
    fit_holds: list[tuple[str, list[CompareLine]]] = field(default_factory=list)

    def shopping_list(self) -> str:
        """EVE multibuy text for everything this ship lacks against the fit.

        One "Name<tab>quantity" line per type, the format Copy list and the
        stockpile shopping list already write, so the game's multibuy window
        takes it as pasted. Missing modules come first in rack order, then
        each consumable's shortfall (want minus have, never the whole want:
        the ship already carries the rest). A type short in two places is
        one line, because the market does not care which bay it was for.
        Empty when the ship matches and lacks nothing.
        """
        wanted: dict[str, int] = {}
        for name, quantity in self.missing:
            wanted[name] = wanted.get(name, 0) + int(quantity)
        for name, have, want in self.short:
            gap = int(want) - int(have)
            if gap > 0:
                wanted[name] = wanted.get(name, 0) + gap
        return "\n".join(f"{name}\t{quantity}" for name, quantity in wanted.items())

    @property
    def matches(self) -> bool:
        return not self.missing and not self.extra

    @property
    def headline(self) -> str:
        return f"{self.fit_name} · {self.hull}" if self.hull else self.fit_name

    @property
    def summary(self) -> str:
        """The verdict line: "3 missing · 1 extra · 2 short", "Matches · 1 short" or
        "Matches".

        The short figure counts types, not units -- "825 short" of one
        missile type would read as a verdict on the whole fit -- and rides
        after the verdict rather than replacing "Matches", because a ship
        fitted right and low on rounds is both of those things.
        """
        parts = []
        missing = sum(n for _name, n in self.missing)
        extra = sum(n for _name, n in self.extra)
        if missing:
            parts.append(f"{missing:,} missing")
        if extra:
            parts.append(f"{extra:,} extra")
        verdict = " · ".join(parts) or "Matches"
        if self.short:
            verdict += f" · {len(self.short):,} short"
        return verdict

    def columns(self) -> list[tuple[str, list[CompareLine], list[CompareLine]]]:
        """The rows of the window top to bottom: the racks, then the hold sections.

        Each entry is (caption, ship lines, fit lines). A caption present on
        only one side is still emitted with an empty other side, so the rows
        it takes stay opposite each other; a caption empty on both sides is
        left out.
        """
        out = []
        for order, left_by, right_by in (
            (RACK_DISPLAY_ORDER, dict(self.ship), dict(self.fit)),
            (HOLD_DISPLAY_ORDER, dict(self.ship_holds), dict(self.fit_holds)),
        ):
            for caption in order:
                left, right = left_by.get(caption, []), right_by.get(caption, [])
                if left or right:
                    out.append((caption, left, right))
        return out


def compare(
    ship_rows: list[sqlite3.Row],
    items: list[sqlite3.Row],
    eft_text: str,
    *,
    fit_id: int = 0,
    fit_name: str = "",
    hull: str = "",
    named: bool = False,
) -> FitComparison:
    """Lay a ship's racks beside a stored fit's, every line given a status.

    Pure like diff(), and built on the same two multisets -- fitting's
    fitted_modules for the ship, the verdict items for the fit -- so a line
    is a match on one side exactly when it is a match on the other. The
    ship side groups by the slot flag's rack; the fit side reads the racks
    out of the stored EFT text's blank-line sections, because fit_items
    keeps only the kind (module, rig, subsystem) and the text is the one
    record of which of the three module racks a module was written in.
    """
    missing, extra, short = diff(ship_rows, items)
    have = fitting.fitted_modules(ship_rows)
    want: dict[int, int] = {}
    names: dict[int, str] = {}
    for i in items:
        if i["slot_kind"] in VERDICT_KINDS:
            type_id = int(i["type_id"])
            want[type_id] = want.get(type_id, 0) + int(i["quantity"])
            names.setdefault(type_id, i["name"] or f"Type {type_id}")
    ship_holds, fit_holds = _hold_sections(ship_rows, items)
    return FitComparison(
        fit_id=fit_id,
        fit_name=fit_name,
        hull=hull,
        named=named,
        ship=_ship_racks(ship_rows, want),
        fit=_fit_racks(items, eft_text, have, want, names),
        missing=missing,
        extra=extra,
        short=short,
        ship_holds=ship_holds,
        fit_holds=fit_holds,
    )


def compare_for_ship(
    conn: sqlite3.Connection,
    ship_item_id: int,
    hull_type_id: int,
    fit_name: str | None = None,
) -> FitComparison | None:
    """The comparison window's payload for one ship, or None with no stored fit.

    The fit is choose_fit's, so the window always shows the fit the
    inspector's block was talking about.
    """
    chosen = choose_fit(conn, ship_item_id, hull_type_id, fit_name)
    if chosen is None:
        return None
    return compare(
        chosen.ship_rows,
        chosen.items,
        chosen.eft_text,
        fit_id=chosen.fit_id,
        fit_name=chosen.name,
        hull=chosen.hull,
        named=chosen.named,
    )


# An entry of one rack before allocation: a module as (type_id, name,
# quantity, loaded note), a placeholder as (None, text as written, 0, "").
_Entry = tuple


def _allocate(
    rack_entries: dict[str, list[_Entry]],
    counterpart: dict[int, int],
    present: str,
    absent: str,
) -> list[tuple[str, list[CompareLine]]]:
    """Turn per-rack entries into lines, each matched against the other side.

    The racks are walked in display order and the counterpart dict is
    consumed as matches are handed out, so a type spread over two racks
    matches in the first rack the walk reaches and is present or absent in
    the second -- the multiset decides, the rack only decides where the
    line is drawn.
    """
    remaining = dict(counterpart)
    out: list[tuple[str, list[CompareLine]]] = []
    for rack in RACK_DISPLAY_ORDER:
        entries = rack_entries.get(rack)
        if not entries:
            continue
        lines: list[CompareLine] = []
        for type_id, name, quantity, loaded in entries:
            if type_id is None:
                lines.append(CompareLine(name, 0, EMPTY))
                continue
            matched = min(quantity, remaining.get(type_id, 0))
            if matched:
                remaining[type_id] -= matched
                lines.append(CompareLine(name, matched, present, type_id, loaded))
                loaded = ""
            if quantity - matched:
                lines.append(CompareLine(name, quantity - matched, absent, type_id, loaded))
        if lines:
            out.append((rack, lines))
    return out


def _ship_racks(
    ship_rows: list[sqlite3.Row], want: dict[int, int]
) -> list[tuple[str, list[CompareLine]]]:
    """The ship's fitted modules per rack, matched against the fit's counts.

    Modules of one type in one rack fold into one line (plus a split for
    the unmatched part), and the charges loaded in those slots fold into
    the line's greyed note, summed per charge type -- fetch_fit returns a
    module and its charge as two rows on the same flag, told apart by the
    Charge category exactly as fitting._render_slots does.
    """
    fitted = set(fitting.FITTED_FLAGS)
    # (rack, type_id) -> [name, quantity, flags occupied]; charges by flag.
    modules: dict[tuple[str, int], list] = {}
    charges: dict[str, dict[int, tuple[str, int]]] = {}
    order: dict[str, list[int]] = {}
    for r in ship_rows:
        flag = r["location_flag"] or ""
        if flag not in fitted:
            continue
        rack = fitting.RACK_OF_FLAG[flag]
        type_id = int(r["type_id"])
        quantity = int(r["quantity"] or 0)
        if (r["category"] or "") == fitting.CHARGE_CATEGORY:
            by_type = charges.setdefault(flag, {})
            name, had = by_type.get(type_id, (r["item"], 0))
            by_type[type_id] = (name, had + quantity)
            continue
        entry = modules.get((rack, type_id))
        if entry is None:
            entry = modules[(rack, type_id)] = [r["item"], 0, []]
            order.setdefault(rack, []).append(type_id)
        entry[1] += quantity
        entry[2].append(flag)

    rack_entries: dict[str, list[_Entry]] = {}
    for rack, type_ids in order.items():
        for type_id in type_ids:
            name, quantity, flags = modules[(rack, type_id)]
            loaded_by_type: dict[int, tuple[str, int]] = {}
            for flag in flags:
                for charge_id, (charge_name, count) in charges.get(flag, {}).items():
                    had = loaded_by_type.get(charge_id, (charge_name, 0))[1]
                    loaded_by_type[charge_id] = (charge_name, had + count)
            loaded = ""
            if loaded_by_type:
                loaded = "loaded: " + ", ".join(
                    f"{count:,} × {charge_name}" for charge_name, count in loaded_by_type.values()
                )
            rack_entries.setdefault(rack, []).append((type_id, name, quantity, loaded))
    return _allocate(rack_entries, want, MATCH, EXTRA)


def _fit_racks(
    items: list[sqlite3.Row],
    eft_text: str,
    have: dict[int, int],
    want: dict[int, int],
    names: dict[int, str],
) -> list[tuple[str, list[CompareLine]]]:
    """The fit's verdict items per rack, matched against the ship's counts.

    The text places a module; the items say how many of it the fit asks
    for. Every verdict item is drawn somewhere: whatever the text does not
    place -- no sections at all, a line that stopped resolving, a count the
    text no longer carries -- goes to the kind's fallback rack, so the right
    side always adds up to the verdict multiset diff() compared.
    """
    kind_of: dict[int, str] = {}
    by_name: dict[str, int] = {}
    for i in items:
        if i["slot_kind"] in VERDICT_KINDS:
            type_id = int(i["type_id"])
            kind_of[type_id] = i["slot_kind"]
            by_name.setdefault((i["name"] or "").casefold(), type_id)

    unplaced = dict(want)
    # Per rack, the order lines first appeared: a (rack, type_id) key for a
    # module, whose count accumulates in counts, or a placeholder entry.
    placed: dict[str, list] = {}
    counts: dict[tuple[str, int], int] = {}
    module_racks_left = list(fitting.EFT_RACK_ORDER[:3])
    rig_rack, subsystem_rack = fitting.EFT_RACK_ORDER[3], fitting.EFT_RACK_ORDER[4]

    def place(rack: str, type_id: int, quantity: int) -> None:
        key = (rack, type_id)
        if key not in counts:
            placed.setdefault(rack, []).append(key)
        counts[key] = counts.get(key, 0) + quantity

    for section in _eft_module_sections(eft_text):
        hint = None
        entries: list[tuple[int | None, str]] = []
        for line in section:
            if _EMPTY_SLOT_RE.match(line):
                hint = hint or _placeholder_rack(line)
                entries.append((None, line))
                continue
            type_id = by_name.get(_module_name(line).casefold())
            if type_id is not None:
                entries.append((type_id, ""))
        kinds = {kind_of[t] for t, _text in entries if t is not None}
        if hint is not None:
            rack = hint
        elif kinds == {"rig"}:
            rack = rig_rack
        elif kinds == {"subsystem"}:
            rack = subsystem_rack
        elif kinds:
            # A plain-module section is the next of low, mid, high the text
            # has not used; a fourth one (a hand-edited paste) piles onto
            # the last rather than vanishing.
            rack = module_racks_left.pop(0) if module_racks_left else fitting.EFT_RACK_ORDER[2]
        else:
            continue
        if rack in module_racks_left:
            module_racks_left.remove(rack)
        for type_id, text in entries:
            if type_id is None:
                placed.setdefault(rack, []).append((None, text, 0, ""))
            elif unplaced.get(type_id, 0) > 0:
                unplaced[type_id] -= 1
                place(rack, type_id, 1)

    for type_id, left in unplaced.items():
        if left > 0:
            place(_FALLBACK_RACKS[kind_of[type_id]], type_id, left)

    rack_entries: dict[str, list[_Entry]] = {}
    for rack, entries in placed.items():
        for entry in entries:
            if len(entry) == 2:
                type_id = entry[1]
                rack_entries.setdefault(rack, []).append(
                    (type_id, names[type_id], counts[entry], "")
                )
            else:
                rack_entries.setdefault(rack, []).append(entry)
    return _allocate(rack_entries, have, MATCH, MISSING)


def _hold_sections(
    ship_rows: list[sqlite3.Row], items: list[sqlite3.Row]
) -> tuple[list[tuple[str, list[CompareLine]]], list[tuple[str, list[CompareLine]]]]:
    """The Drones and Cargo sections for both sides of the window.

    Returns (ship side, fit side), each a list of (section, lines) in
    HOLD_DISPLAY_ORDER with both-empty sections left out. The fit's lines
    come first in fit_items' order, each answered on the
    ship's side by the count the ship carries: a match when it is enough,
    else a SHORT line carrying the fit's figure opposite a MISSING one.
    Whatever else the ship carries in that bucket follows, sorted by name,
    as EXTRA -- listed so the pilot can see what is aboard, not coloured,
    since surplus cargo deviates from nothing. Unlike the racks, a type the
    ship has more of than the fit asks is one match line with the ship's
    own count rather than a match and a split-off surplus: "8 × drones"
    opposite "5 × drones" says everything the split would.
    """
    aboard = fitting.carried(ship_rows)
    ship_side: dict[str, list[CompareLine]] = {}
    fit_side: dict[str, list[CompareLine]] = {}
    listed: dict[str, set[int]] = {}
    for i in items:
        section = _SECTION_OF_KIND.get(i["slot_kind"])
        if section is None:
            continue
        type_id, wanted = int(i["type_id"]), int(i["quantity"])
        name = i["name"] or aboard.names.get(type_id) or f"Type {type_id}"
        have = aboard.have(i["slot_kind"]).get(type_id, 0)
        listed.setdefault(section, set()).add(type_id)
        if have >= wanted:
            ship_side.setdefault(section, []).append(CompareLine(name, have, MATCH, type_id))
            fit_side.setdefault(section, []).append(CompareLine(name, wanted, MATCH, type_id))
        else:
            ship_side.setdefault(section, []).append(
                CompareLine(name, have, SHORT, type_id, wanted=wanted)
            )
            fit_side.setdefault(section, []).append(CompareLine(name, wanted, MISSING, type_id))
    for section, bucket in ((DRONES_SECTION, aboard.drones), (CARGO_SECTION, aboard.cargo)):
        surplus = [
            CompareLine(aboard.names.get(t) or f"Type {t}", n, EXTRA, t)
            for t, n in bucket.items()
            if n and t not in listed.get(section, set())
        ]
        surplus.sort(key=lambda line: (line.name.casefold(), line.type_id or 0))
        if surplus:
            ship_side.setdefault(section, []).extend(surplus)
    return (
        [(s, ship_side[s]) for s in HOLD_DISPLAY_ORDER if s in ship_side],
        [(s, fit_side[s]) for s in HOLD_DISPLAY_ORDER if s in fit_side],
    )


def _module_name(line: str) -> str:
    """The module half of an EFT module line.

    `/OFFLINE` and the charge after the first comma are dropped, the same
    two rules _read_body_line applies on the way in.
    """
    line = _OFFLINE_RE.sub("", line).strip()
    name, _sep, _charge = line.partition(",")
    return name.strip()


def _placeholder_rack(line: str) -> str | None:
    match = _EMPTY_SLOT_WORD_RE.match(line)
    if match is None:
        return None
    index = _EMPTY_SLOT_WORDS.get(match.group("word").lower())
    return None if index is None else fitting.EFT_RACK_ORDER[index]


def _eft_module_sections(text: str) -> list[list[str]]:
    """The module racks of an EFT paste as blank-line-separated sections.

    The header is dropped, comments are dropped, and reading stops at the
    first `Name xN` stack: that is where the drones and cargo begin, and
    Pyfa separates them from the racks with two blank lines that a
    hand-edited paste may not keep. Empty-slot placeholders stay, since
    they name their rack and are shown on the fit side.
    """
    sections: list[list[str]] = []
    current: list[str] = []
    seen_first = False
    for line in _clean_lines(text):
        if line.startswith("//") or line.startswith("#"):
            continue
        if not line:
            if current:
                sections.append(current)
                current = []
            continue
        if not seen_first:
            seen_first = True
            if _HEADER_RE.match(line) and not _EMPTY_SLOT_RE.match(line):
                continue
        if _STACK_RE.match(line):
            break
        current.append(line)
    if current:
        sections.append(current)
    return sections
