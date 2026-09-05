# Abyssal complex search: the Abyssal chip, its card, and roll columns

## Context
The abyssal-stats feature (uncommitted on `zero-dev-abyssal`, see
`docs/plans/abyssal-stats.md`) shows each mutated module's rolls in the
inspector and lets `stat:` chips filter on one attribute at a time. It does
not yet answer the question the user actually has: "find me the webifiers
among my abyssals whose strength and range both rolled well". Each item has
three to eight rolled stats, nothing about them is searchable in game, and
the estate spans a few dozen module types whose attribute sets differ. This plan adds
a purpose-built abyssal filter INSIDE the Assets omnibox: an `abyssal` chip
that carries module types, a popover card for building multi-stat searches
with sliders, `roll:` quality grammar with ranges, and per-type roll columns
in the table.

Grilled with the user on 2026-09-02; every decision below is settled.

## Execution mode: subagents, orchestrated
This plan is executed by subagents, not by the orchestrator writing code.
The orchestrator only: spawns packages, runs the merge gate, resolves
contract drift, routes review findings, and runs the final verification.

| Step | Agent | Runs | Owns (files) | Exit |
|---|---|---|---|---|
| 1 | A `abyssal-search-core` | parallel with B, background | omni.py, queries.py, abyssal.py, test_omni.py, test_abyssal.py | owned tests green, ruff clean, self adversarial pass done |
| 1 | B `abyssal-search-ui` | parallel with A, background | ui/omnibox.py, ui/grouped_model.py, ui/assets_view.py, ui/palette.py, ui/main_window.py, new ui/abyssal_card.py, new ui/range_slider.py, test_omnibox.py, test_grouped_model.py, test_contrast.py, new test_abyssal_card.py | offscreen MainWindow import ok, ruff clean, owned tests green where independent of A, self adversarial pass done |
| 2 | orchestrator merge gate | after A and B | -- | full pytest, ruff, sandboxed MainWindow import; drift fixed |
| 3 | C `abyssal-search-integration` | after gate | test_assets_integration.py, README.md, ROADMAP.md | full suite green, ruff clean |
| 4 | D adversarial reviewer | after C, read-only | scratchpad only | findings report with evidence |
| 5 | fixer agent (or orchestrator for small items) | after D | files named by findings | full suite green; gate re-run |
| 6 | orchestrator verification | last | -- | sandboxed render + live read-only checks; app relaunched |

Every agent prompt = Common briefing (verbatim) + the Pinned interface
contracts (verbatim) + its Package section + "Before reporting done, run an
adversarial pass over your own work; fix what breaks; report under 500 words:
what changed per file, contracts satisfied, deviations, tests, counts".
Packages A and B have DISJOINT files by construction; neither may touch the
other's. Nothing is committed by any agent.

## Settled decisions
- Lives inside the Assets tab, as part of the omnibox. No new tab, no window.
- New chip kind `abyssal`, NOT under `is:`. Typing `abyssal` mints a chip
  reading "Abyssal" (all dynamic types). The chip can hold module types
  inside it: `abyssal:"Abyssal Stasis Webifier, Abyssal Warp Disruptor"`
  renders "Abyssal · Stasis Webifier" / "Abyssal · 3 types"; types OR.
  `is:abyssal` stays as an alias that parses to the same chip.
- Tier, source and mutator are NOT filter axes. "It's what you got out as
  the end stat."
- The chip carries a glyph button that opens a COMPLEX-SEARCH CARD: a
  popover anchored under the chip (~520 px), Apply-on-Done (Cancel, Esc and
  outside click discard). Contents: a type picker with the user's owned
  abyssal types and counts; stat rows ENABLED ONLY WHEN EXACTLY ONE TYPE is
  selected, each with an attribute (that type's ROLLED attributes only), a
  quality|value mode switch, a dual-handle range slider plus two editable
  number fields (percent in quality mode, display units in value mode),
  bounds scoped to the estate's min/max for that attribute within that type;
  a banner "N abyssal items not fetched — Fetch". Done writes chips into the
  omnibox: the abyssal chip plus one `roll:`/`stat:` chip per row, replacing
  the card's previous chips.
- Grammar: `roll:<name><op><pct>` = mirrored roll QUALITY in percent, rolled
  attributes only; ranges `roll:cpu=60..90`, `stat:cpu=18..22` (inclusive).
  `stat:` keeps stored-attribute semantics and display units. Direct typing
  works without the card (`abyssal roll:web>=70`).
- Table: with exactly ONE type selected in the chip, one column per rolled
  attribute of that type appears after Qty, showing the display value with a
  DIVERGING quality wash (below 50% toward CRITICAL, above toward POSITIVE,
  plain near the middle), sortable by value; plus a "Roll" column (mean
  quality, percent, same wash), also only with a single type. Group,
  Category, Meta and the value columns stay put.
- Combination: stat rows AND; types within the chip OR. Unfetched items are
  excluded by stat filters and surfaced by the banner. Saved views cover the
  new chips automatically.

## Estate facts (live DB, read-only, 2026-09-02)
A few hundred items across a few dozen owned types (of 89 dynamic),
thirty-odd rolled attributes in the estate, 3–8 rolled per item (median 4), all fetched. All five tiers are present. CPU spans an order of magnitude
ACROSS types, so slider bounds must be type-scoped. "Signature Radius
Modifier" is two attributes (554 unit 124 on MWDs, 983 unit 1 on shield
extenders): pickers key on `attribute_id` and disambiguate labels. Per-item mean quality spreads widely around the middle.

## Facts in the tree that shape the design
- `queries.ASSET_COLUMNS` is indexed positionally in `assets_view.py`
  (`_filter_current_cell`, `_build_context_menu`, `_TreeSortController`,
  `_size_columns`, `export_csv`, the badge-delegate loop) and in tests
  (`tests/test_assets_integration.py` `COLUMN = {key: i}`,
  `tests/test_grouped_model.py` comparison and header counts). It stays
  untouched; dynamic columns live in the model.
- `FilterSpec.where(exclude_level=kind)` already drops any chip kind, so the
  card's type counts can facet by "every filter except the abyssal chip".
- `to_text()` already quotes a bare word that would re-tokenise as a chip,
  so a bare-word `abyssal` token is safe.
- `tests/test_omni.py` pins `len(omni.IS_FLAGS) == 5`; `is:abyssal` becoming
  an alias changes what `parse("is:abyssal")` yields (Package A owns that).
- Qt has no dual-handle slider (`QSlider` is single-valued; `superqt` is not
  a dependency): a small custom widget is needed.
- Every abyssal join is a primary-key probe; no schema change.

## Pinned interface contracts (every package codes to these; deviations are reported, never improvised)

### omni.py
- `ABYSSAL_KIND = "abyssal"`, `ROLL_KIND = "roll"`. `Chip("abyssal", "")` =
  all dynamic types; value = type names joined by `", "`;
  `split_types(value) -> list[str]`, `join_types(names) -> str`.
- `parse`: bare token `abyssal` (case-insensitive, optional `-`) and
  `abyssal:<value>` mint the chip; `is:abyssal` parses to
  `Chip("abyssal", "")` (`"abyssal"` stays in `IS_FLAGS` for the draft
  builder's vocabulary). `to_text`: empty value emits `abyssal`, else
  `abyssal:<quoted>`.
- `@dataclass StatTerm(name: str, op: str, low: float, high: float | None)`,
  `op ∈ {">=", "<=", ">", "<", ".."}`. `parse_stat(value) -> StatTerm | None`
  replaces the current tuple (A updates callers and tests). Regex:
  `^\s*([^<>=]+?)\s*(>=|<=|>|<|=)\s*(-?[0-9]+(?:\.[0-9]+)?)(?:\.\.(-?[0-9]+(?:\.[0-9]+)?))?\s*$`;
  `=` without `..` is malformed (bare text); `lo > hi` is malformed.
- SQL: abyssal chip -> `t.is_dynamic_type = 1` plus `t.name IN (?, …)` when
  types are given; negated -> `NOT (...)`. `stat:` keeps `_STAT_EXISTS`; the
  comparison becomes `{expr} {op} ?` or `{expr} BETWEEN ? AND ?`. `roll:` ->
  `_ROLL_EXISTS` (below), `NOT EXISTS` when negated.

### queries.py
- `roll_quality_sql(value, base, min_mult, max_mult, attr_high, mutator_high) -> str`:
  percent 0..100 or NULL. `lo = MIN(base*min, base*max)`, `hi = MAX(...)`,
  `pos = MIN(1.0, MAX(0.0, (value-lo)/(hi-lo)))`, polarity
  `COALESCE(mutator_high, attr_high, 1)`, `CASE WHEN base IS NULL OR hi-lo <= 0
  THEN NULL WHEN polarity = 1 THEN pos*100 ELSE (1-pos)*100 END`. A Qt-free
  test pins parity with `abyssal.quality(abyssal.roll_position(...))` over
  seeded rows including the webifier speedFactor override. If
  `abyssal.POLARITY_OVERRIDES` ever fills, the SQL prepends a
  `CASE sd.attribute_id WHEN … END` term (documented; tested by
  monkeypatching one override).
- `_ROLL_EXISTS` (used by omni): `EXISTS (SELECT 1 FROM abyssal_items i JOIN
  abyssal_attributes sa ON sa.item_id = i.item_id JOIN sde_mutator_ranges mr
  ON mr.mutator_type_id = i.mutator_type_id AND mr.attribute_id = sa.attribute_id
  JOIN sde_dogma_attributes sd ON sd.attribute_id = sa.attribute_id LEFT JOIN
  sde_type_dogma td ON td.type_id = i.source_type_id AND td.attribute_id =
  sa.attribute_id WHERE i.item_id = a.item_id AND i.status = 'ok' AND <the same
  name-first/display-name match as _STAT_EXISTS> AND {roll_quality_sql(...)} {op} ?)`.
- `abyssal_type_counts(conn, where, params) -> list[Row(type_id, name, items, fetched)]`
  over `(ASSET_ROWS WHERE ...) LEFT JOIN abyssal_items`, `is_dynamic_type = 1`,
  grouped by type, ordered by items DESC.
- `abyssal_type_attributes(conn, type_name) -> list[dict(attribute_id, name, label, unit_id, unit)]`:
  DISTINCT over `sde_mutator_ranges JOIN sde_types ON type_id = resulting_type_id
  WHERE name = ?`, ordered by label; a shared display name is disambiguated as
  `"Signature Radius Modifier (%)"` vs `"(m)"` (same rule as the omnibox's
  `_stat_option`).
- `abyssal_attribute_bounds(conn, type_name) -> dict[int, tuple[float, float]]`:
  estate MIN/MAX of `display_value_sql` per rolled attribute over that type's
  fetched items (after conversion, because units 108/111 invert).
- `abyssal_cells(conn, item_ids) -> dict[int, dict[int, tuple[float, float | None]]]`:
  `{item_id: {attribute_id: (display_value, quality)}}`, rolled attributes
  only; `abyssal_summaries` is rebuilt on the same row stream so one query
  per reload serves both.
- `abyssal_pending_count(conn, type_names: list[str] | None) -> int` (wraps
  `abyssal.pending`).

### abyssal.py
- `mean_quality(cells: dict[int, tuple]) -> float | None`;
  `strip_type_prefix(name) -> str` (`"Abyssal Stasis Webifier"` ->
  `"Stasis Webifier"`, also the `50MN Abyssal …` shapes) for chip rendering.

### UI
- `grouped_model.py`: `ROLL_QUALITY_ROLE = Qt.UserRole + 7`;
  `ROLL_MEAN_KEY = "roll"`; attribute keys `f"roll:{attribute_id}"`;
  `set_rows(rows, group_key, extra_columns: list[tuple[str, str]] = ())`
  inserts the extras after `quantity`; `columns()` and `key_at(col)`
  accessors; `set_abyssal_cells(cells)`; roll keys sort numerically, Display
  via `abyssal.format_value`, UserRole raw float, `Qt.BackgroundRole` from
  `palette.quality_wash`.
- `palette.py`: `_quality_hex(q, dark) -> str | None` (None within ±0.05 of
  0.5; strength 0.08..0.26 by `|q - 0.5| * 2`; low accent = the CRITICAL
  pair, high accent = POSITIVE, same `_blend` idiom as the heat wash),
  `quality_wash(q, palette=None)`; `CHIP_KIND_TINTS["abyssal"]` (crimson
  family) and `["roll"]`, distinct from every existing wash, contrast-tested.
- `omnibox.py`: `_ChipWidget` grows `card_btn` (glyph "▾") only for
  `ABYSSAL_KIND`; the value renders "Abyssal", "Abyssal · Stasis Webifier"
  or "Abyssal · 3 types" with the prefix label hidden;
  `Omnibox.card_requested = Signal(object, QWidget)` (chip, anchor widget);
  `_ALL_KINDS` gains `abyssal` and `roll`; `roll` shares the stat completion.
- New `ui/range_slider.py`: `RangeSlider(QWidget)` with `set_bounds(lo, hi)`,
  `set_values(lo, hi)`, `values()`, `range_changed = Signal(float, float)`;
  handle positions kept as 0..1 fractions (testable offscreen like
  `_ValueBar`); nearest-handle drag; arrow keys move the focused handle.
- New `ui/abyssal_card.py`: `AbyssalCard(QFrame)` (flag `Qt.Popup`, 520 px),
  database-free: `set_types(rows, selected)`, `set_attributes(attrs, bounds)`,
  `set_pending(n)`, `seed(chips)`; signals `selection_changed(list[str])`,
  `fetch_requested(list[str])`, `done(list[omni.Chip])`, `cancelled()`.
  `_StatRow` = attribute combo (data = attribute_id), quality|value toggle,
  `RangeSlider`, two `QDoubleSpinBox`; `chip()` emits `roll:` for quality,
  `stat:` for value, `name=lo..hi` (or a one-sided op when a handle sits at
  its bound). Popup close = Cancel; Done applies.
- `assets_view.py`: owns `_card_query: AsyncQuery`; on `card_requested`
  builds the card, positions it under the anchor via
  `QTimer.singleShot(0, ...)` after `mapToGlobal` (the omnibox's own popup
  lesson); on `done` rebuilds the spec (keep other chips, replace every
  abyssal/roll/stat chip) through `omnibox.set_spec`; `reload()`'s fetch also
  computes `extra_columns` and `cells` when the abyssal chip names exactly
  one type; `abyssal_fetch_requested = Signal(list)` -> `main_window.py`
  submits `AbyssalStatsJob(settings, tokens, item_ids=ids)` via
  `_submit("abyssal", ..., after=("sync",))`. `_TreeSortController`
  remembers the sort KEY and re-resolves the column through `model.key_at`,
  clearing when the key vanishes; the badge delegate, cell actions, export
  and column sizing read `model.columns()`; new roll columns are sized when
  they appear (`_size_columns` is otherwise once-only).

## Subagent execution plan

### Orchestration
1. Spawn **A (`abyssal-search-core`)** and **B (`abyssal-search-ui`)** in
   parallel, in one message, both in background. Disjoint files; both code to
   the pinned contracts, never to each other's in-progress files.
2. When both report: orchestrator runs the merge gate (full `uv run pytest`,
   `uv run ruff check .`, sandboxed offscreen `MainWindow` import), resolves
   contract drift itself, then spawns **C (`abyssal-search-integration`)**.
3. After C: spawn **D (adversarial reviewer)**. Findings route to a fixer
   agent (or the orchestrator for small ones); re-run the merge gate.
4. Orchestrator verification (below); relaunch the app for the user.
   Commit/PR only on the user's word.

### Common briefing (verbatim in every agent prompt)
- Workdir: the repository root, branch
  zero-dev-abyssal, PySide6 app "EVE Booty" (package `evasset`; launch
  `uv run evebooty`). Commands: `uv run pytest`, `uv run ruff check .`
  (E/F/I/UP/B, line 100, Python 3.10 floor). Qt tests offscreen. Absolute paths.
- Read the worktree's CLAUDE.md first and follow it: why-comments, British
  prose, `--` dashes, sentence-style tests, Qt-free logic outside ui/, the
  AsyncQuery/Job lifetime rules (src/evasset/ui/async_query.py docstring),
  theme-aware colour pairs measured in tests/test_contrast.py, and the
  MANDATORY database-safety section: every throwaway script sets
  `EVASSET_DATA_DIR`/`EVASSET_CACHE_DIR` to a fresh temp dir BEFORE importing
  evasset, asserts isolation after `db.init()`, never runs destructive SQL
  outside its own temp dir; the live DB is opened read-only (`mode=ro`) only.
- The existing abyssal-stats feature is uncommitted in the tree; read
  docs/plans/abyssal-stats.md for what exists. Do not commit. Touch ONLY your
  package's files; the full suite may be red on the other package's files
  until integration. Contract deviations are reported loudly, never
  improvised. Before reporting done, run an adversarial pass over your own
  work and fix what breaks.

### Package A — `abyssal-search-core` (Qt-free)
Files: `src/evasset/omni.py`, `src/evasset/queries.py`,
`src/evasset/abyssal.py`, `tests/test_omni.py`, `tests/test_abyssal.py`.
Builds: the `abyssal` chip kind (bare token, typed value, `is:abyssal`
alias, `split_types`/`join_types`, SQL); `StatTerm`/`parse_stat` with `..`
ranges and the malformed rules; `roll:` with `_ROLL_EXISTS` and
`roll_quality_sql`; `abyssal_type_counts`, `abyssal_type_attributes`,
`abyssal_attribute_bounds`, `abyssal_cells` (+ `abyssal_summaries` rebuilt on
it), `abyssal_pending_count`; `mean_quality`, `strip_type_prefix`; palette
kind list consumers updated where they live in omni. Tests per the Tests
section (Qt-free list), including `EXPLAIN QUERY PLAN` showing no
`SCAN abyssal_attributes` and a 25k-row synthetic timing (< 100 ms for one
`roll:` clause). Exit: those test files green; ruff clean on owned files.

### Package B — `abyssal-search-ui`
Files: `src/evasset/ui/omnibox.py`, `ui/grouped_model.py`,
`ui/assets_view.py`, `ui/palette.py`, `ui/main_window.py`, new
`ui/abyssal_card.py`, new `ui/range_slider.py`, `tests/test_omnibox.py`,
`tests/test_grouped_model.py`, `tests/test_contrast.py`, new
`tests/test_abyssal_card.py`.
Builds: the chip's glyph button and label shapes; `card_requested`; the
card, its stat rows and the range slider; the popover placement and
Apply-on-Done/Cancel semantics; the dynamic roll columns (model, sort
controller by key, delegate, sizing, export, cell actions via
`model.columns()`); `quality_wash` and the two chip washes; the fetch
request path into `main_window.py`. Codes against A's contracts (stubs
allowed until A lands). Exit: sandboxed offscreen `MainWindow` import ok,
ruff clean, owned tests green where independent of A.

### Package C — `abyssal-search-integration` (after A and B merge)
Files: `tests/test_assets_integration.py`, `README.md`, `ROADMAP.md`.
Offscreen tests through the real pipeline (seed the research BCS and the
synthetic webifier already in that file): `abyssal roll:web>=70` returns the
webifier; the chip's three label shapes; card opens from the glyph, stat
rows enable only with one type, Done writes exactly the expected chips and
replaces prior roll/stat chips, Cancel/Esc/outside click change nothing;
roll columns appear after Qty with one type and vanish with two, cells show
value + wash, Roll column = mean quality, sort by a roll column survives a
reload; the banner's fetch request reaches the job path; saved view with a
vanished type renders and filters to empty. README: grammar (`abyssal`,
`roll:`, ranges), the card, the columns; ROADMAP: remove the "`roll:`
deferred" entry and record the feature. Exit: full suite green, ruff clean.

### Package D — adversarial review (after C)
Read-only, scratchpad only, database-safety rules apply. Refute: roll SQL vs
Python parity on sign-inverted and negative-base attributes and on units
108/111; `..` range edge inclusivity and lo > hi; injection through type
names in the `IN (...)` list and through `roll:` names; the `is:abyssal`
alias round-trip; card state machine (Done vs popup-close, Enter in a spin
box, focus stealing, anchoring after the omnibox wraps onto two lines);
dynamic columns vs every positional `ASSET_COLUMNS` consumer (context menu
on a roll cell, export CSV, keyboard `_KEY_MAP`, `_filter_current_cell`);
sort key persistence when the column set changes; wash contrast at the ends
and its None band; performance of `_ROLL_EXISTS` and `abyssal_cells` on 25k
rows; vacuous tests; README accuracy.

### Orchestrator verification (final)
1. Full `uv run pytest` + `uv run ruff check .`.
2. Sandboxed offscreen render (temp `EVASSET_DATA_DIR`, isolation asserted)
   seeding the BCS/webifier sample: `abyssal roll:web>=70` returns the
   webifier (75%); with `abyssal:"Abyssal Stasis Webifier"` the columns show
   `-63%`, `27 tf`, Roll `80%` with washes; the card round-trips Done.
3. Live, read-only: `abyssal_type_counts` lists every owned type; CPU bounds differ
   between two types; `abyssal:"Abyssal Stasis Webifier" roll:web>=50` timed
   on a live estate. Then relaunch `uv run evebooty` and hand the user the
   three things to try: type `abyssal`, open the card from the chip, pick
   one type and drag a slider.

## Tests
- Qt-free: bare `abyssal`, `abyssal:"A, B"`, `is:abyssal` parse and
  round-trip (extend the property test with the new kinds); `roll:` and
  range parse; malformed (`=` without `..`, `lo > hi`, non-ASCII digits)
  degrade to bare text; roll SQL equals Python quality on seeded webifier
  and BCS rows; `roll:cpu=60..90` inclusive ends; `-roll:` keeps unfetched
  items; type counts facet excluding the abyssal chip; attribute list per
  type via `resulting_type_id` with disambiguated labels; bounds after unit
  conversion (111 inverts); cells and summaries agree; EXPLAIN has no scan;
  `mean_quality`, `strip_type_prefix`.
- Offscreen: chip renders the three label shapes and shows the glyph only
  for abyssal; card enables stat rows only with one type; slider fractions
  and bounds; Done emits exact chips and `set_spec` replaces prior
  roll/stat chips; Cancel/Esc/outside click change nothing; roll columns
  appear after Qty with one type and vanish with two; wash contrast at
  q ∈ {0, .25, .75, 1} in both themes and None near 0.5; sort by a roll
  column survives a reload; existing column-index tests unchanged.

## Risks
- Correlated roll SQL over 25k rows: all joins are PK probes and the
  display-name fallback subselect is uncorrelated; pinned by EXPLAIN and a
  timing test.
- Popover anchoring and focus: `Qt.Popup` steals focus and closes on an
  outside click, which must read as Cancel; `QDoubleSpinBox` Enter must not
  close the popup (event filter); place after layout via `singleShot(0)`.
- Variable column set: sort by key not index; re-apply delegate and widths
  when the set changes.
- Saved view with a vanished type filters to an empty table; the picker
  shows it with count 0 and Done drops it if unticked.
- `is:abyssal` alias changes `parse` output; every `IS_FLAGS` loop test is
  updated deliberately.

## Amendments (2026-09-03, after the user tried it)
- The card opens on its own when the abyssal chip is minted by typing (Enter
  in the omnibox, or the draft builder), one event turn after the chip widget
  lands; the glyph reopens it. A chip that arrives by `set_spec`, `add_chip`,
  a trailing space or negated opens nothing (`Omnibox._request_card_later`).
- The type picker is a single-select `QComboBox` -- "All abyssal modules · N"
  then one entry per owned type -- rather than a checkable list. Stat rows
  are enabled whenever a type (not All) is picked. A chip naming several
  types seeds the first and Done writes that one; several types stay
  expressible by typing only. `selection_changed` carries `[name]` or `[]`.
- The stat rows bound a stat in its display units only. The Roll % | Value
  toggle and the quality axis are gone from the card: every other surface
  (roll columns, inspector, chips) speaks in the stat's own units, so the
  percent was a figure to translate before trusting, and a toggle that
  re-scaled the slider under the handles confused the two axes. `roll:`
  stays a typed filter: `CARD_KINDS` is `(abyssal, stat)`, so Done leaves a
  typed `roll:` chip untouched, while the picker facet still excludes
  `roll:` alongside the card's kinds (`_fetch_card_data`). `seed` reads
  `stat:` chips only. Attributes without estate bounds are not offered, and
  a row is dropped when a type switch takes its attribute's bounds away.
  A fresh row opens on the full range -- in display units there is no
  "good" end to start from.
- The "All abyssal modules · N" entry is gone from the type picker; the
  dropdown lists module types alone (a seeded-but-unowned type at "· 0").
  The bare `abyssal` chip is now the picker's *no selection* state:
  `currentIndex() == -1`, the edit empty under its placeholder, stat rows
  disabled and "+ Add stat" hidden, the match count over every abyssal item,
  and Done re-emitting the bare chip. The entry restated the state the card
  opened in, took the top slot of a list read by count, and sat among the
  types as if it were one. The way back from a type to every item without
  leaving the card is to clear the edit: empty text on Enter or on blur
  deselects the type (announced through `selection_changed([])`, only when
  it is a change), while typed garbage still reverts to the current pick.
  `set_types(rows, [])` and `seed` with no type select nothing;
  `selected_types()`/`seeded_types()` keep returning `[]` for that state, so
  `_fetch_card_data` and `_on_card_fetch` are unchanged.
