# Abyssal module stats: show the rolls, make them searchable

## Context
Abyssal (mutated) modules show as "unpriced" because they have no market and
every item's attributes are rolled per item -- the SDE only knows the generic
"Abyssal X" type. The user wants to (a) see each abyssal item's actual rolled
stats and (b) filter/search on them. Research (primary sources, 2026-09-01;
full cited notes in `docs/research/abyssal-stats.md`) confirmed both are doable with
public ESI data plus four SDE files the app already downloads.

## Decisions (settled with the user)
- Surface: rolled stats in the row inspector AND a new omnibox token `stat:`,
  plus an `is:abyssal` flag.
- Show values with roll quality: position in the mutator's min..max range and
  a better/worse-than-source verdict.
- Fetch: **explicit optional action** (Update → Abyssal stats, and a per-item
  button in the inspector) -- research proved ESI has no batch route, so it
  is not part of routine sync. One call per item, fetched once, kept forever.
- No external appraisal (MutaMarket exists but has undocumented terms).
- (grilling) Inspector lists the mutator's ROLLED attributes only, each drawn
  as a thin range bar PLUS text (`61.2% · 88% of range · ▲ +11.2 vs 50`).
- (grilling) Steady state: a Settings switch "Fetch rolls for new abyssal
  items during sync", OFF by default; the first successful manual run asks
  once whether to turn it on. Corp-owned abyssals are included (public route).
- (grilling) The SDE tables re-import AUTOMATICALLY at startup when the
  tables version is stale (cached zip, ~3 s, normal status-bar progress).
- (grilling) Value cell shows an `abyssal` badge, but the item STILL counts as
  unpriced (`is:unpriced`, strip count, totals unchanged); its tooltip is the
  per-attribute quality one-liner once fetched (`Web 61% · Range 88% · CPU
  34%`), fetch state before.
- (grilling) `stat:` accepts curated aliases (cpu, pg/power, cap, range,
  falloff, rof, web, damage, hp, speed) besides display and internal names;
  the completer shows the canonical attribute when an alias is picked.
- (grilling) Sorting the table by a rolled stat is OUT of scope.

## Facts that constrain the design (all cited in the research notes)
- ESI `GET /dogma/dynamic/items/{type_id}/{item_id}`: public (no token), the
  ONLY dynamic-item route; `type_id` must be the abyssal type id; response is
  the item's FULL `dogma_attributes` list plus `source_type_id`,
  `mutator_type_id`, `created_by`; 4xx costs the 100/min error budget; rolls
  are permanent per CCP patch notes.
- **Gate on `types.isDynamicType`** (true on exactly the 89 mutated types),
  NOT `meta_group_id = 15` (260 types incl. 170 mutaplasmids + a blueprint).
- SDE JSONL (build 3487903): `dogmaAttributes` (`_key,name,displayName{en},
  unitID,highIsGood,attributeCategoryID,published,defaultValue`), `dogmaUnits`,
  `typeDogma` (`_key`=typeID, `dogmaAttributes[{attributeID,value}]` -- abyssal
  types carry ~none, so base values come from the SOURCE type),
  `dynamicItemAttributes` (`_key`=mutaplasmid, `attributeIDs[{_key,min,max,
  highIsGood?}]`, `inputOutputMapping[{applicableTypes,resultingType}]`) --
  ranges key on ESI's `mutator_type_id`; the optional per-mutaplasmid
  `highIsGood` OVERRIDES the attribute default (CCP's own polarity data for
  sign-inverted cases like webifier speedFactor).
- Roll position = `(value − lo) / (hi − lo)` with `lo/hi = min/max(base·min,
  base·max)` (handles negative bases); quality = position, inverted when
  low-is-good; NULL when range unknown/degenerate.
- Unit rendering (dogmaUnits): 101 ms→/1000 s; 108, 111 → (1−v)·100 %;
  109 → (v−1)·100 signed %; 127 → v·100 %; 124/205 shown as-is signed %;
  others raw with symbol. 52 mutable attributes, all with display names.
- Local: the live estate holds a few hundred abyssal items, all singletons.

## Implementation

### 1. Schema -- `src/evasset/db.py`
New tables (CREATE IF NOT EXISTS): `sde_dogma_attributes(attribute_id PK,
name, display_name, unit_id, high_is_good, default_value, published)` +
index on display_name; `sde_dogma_units(unit_id PK, name, display_name)`;
`sde_type_dogma(type_id, attribute_id, value, PK both)`;
`sde_mutator_ranges(mutator_type_id, attribute_id, min_mult, max_mult,
high_is_good NULL, resulting_type_id, PK(mutator,attribute))`;
`abyssal_items(item_id PK, type_id, source_type_id, mutator_type_id,
created_by, status 'ok'|'missing', fetched_at)`; `abyssal_attributes(item_id
FK cascade, attribute_id, value, PK both)`. Existing `sde_types` gains
`is_dynamic_type INTEGER NOT NULL DEFAULT 0` via db.py's rebuild-and-copy
migration idiom (trigger: column absent). `dogma_effects` not stored.

### 2. SDE import -- `src/evasset/sde.py`
Four new `import_zip` steps after types: dogma attributes (English
displayName), dogma units, mutator ranges (flattened, incl. the optional
highIsGood and `inputOutputMapping[0].resultingType`), type dogma RESTRICTED
to `applicableTypes ∪ resultingType` from dynamicItemAttributes (a few
hundred types, not 26.8k). `_import_types` reads `isDynamicType`. Add
`SDE_TABLES_VERSION` recorded in `meta`; `installed_build()` reports None
until it matches, so an install whose build is already current re-imports
(cached zip → no re-download).

### 3. Read side -- `src/evasset/queries.py` + new Qt-free `src/evasset/abyssal.py`
- `ASSET_ROWS` gains `t.is_dynamic_type`.
- `queries.display_value_sql(value_expr, unit_expr)`: the single CASE
  implementing the unit table above -- shared by the inspector query and the
  `stat:` filter so typed numbers equal displayed numbers.
- `queries.fetch_abyssal_rolls(conn, item_id) -> dict`: status
  (ok/missing/unfetched), source/mutator names, and per-attribute rows
  (label, unit, display value, display base, position, high_is_good) from a
  join of abyssal_attributes × sde_dogma_attributes × sde_dogma_units ×
  sde_type_dogma(source) × sde_mutator_ranges(mutator); rows = the mutator's
  attribute set, or (unknown mutator) attributes that differ from base.
- `abyssal.py`: polarity resolution (mutator override → attribute default →
  small verified `POLARITY_OVERRIDES` escape hatch, empty until a live check
  proves a case), `quality()`, `verdict()`, `format_value()` (2 dp <10, else
  0 dp; signed for modifier units), and the fetch/store functions below.

### 4. Fetch -- `abyssal.py`, `ui/workers.py`, `ui/main_window.py`
- `abyssal.pending(conn, item_ids=None, retry_missing=False)`: assets ⋈
  sde_types WHERE is_dynamic_type=1 and no `abyssal_items` row (or status
  'missing' when retrying); ordered by type then item for readable progress.
- `abyssal.fetch_rolls(conn, client, progress, should_stop, ...)`:
  sequential `client.get(f"/dogma/dynamic/items/{type_id}/{item_id}",
  allow_404=True)` with NO character_id (public); dict → store item + full
  attribute list (idempotent replace); None (404) → status 'missing' (never
  re-asked unless retry); ESIError → counted failed, nothing stored (retried
  next run); cooperative cancel between items; returns fetched/missing/
  failed/remaining. ErrorLimiter already throttles near the floor.
- `workers.AbyssalStatsJob` (SyncJob shape); MainWindow Update menu gains
  "A&byssal stats" after Game data, submitted as kind "abyssal" with
  `after=("sync",)` so it never reads a half-replaced assets table; log line
  "Abyssal stats: fetched N, M not known to ESI, K failed (will retry)".
- Inspector button → `AssetsView` runs the same fetch for one item via a
  small Job (the `_PriceRefreshJob` pattern, strong refs held) and then
  `refresh_all()`.

### 5. Grammar -- `src/evasset/omni.py` (+ omnibox/palette)
- `stat:"<display or internal name><op><number>"` (`stat:"CPU usage"<30`,
  `stat:duration<9` = nine seconds, `stat:speedFactor<-55`); `op ∈ {>=,<=,>,<}`
  whitelisted by regex, name and number bound as parameters; SQL is a
  correlated EXISTS over abyssal_attributes ⋈ sde_dogma_attributes matching
  `display_name` OR `name` (NOCASE) with `display_value_sql` applied.
  Negation wraps NOT(EXISTS) so items without stored stats survive `-stat:`.
  Malformed → bare text; round-trips via `_quote_value`.
- `is:abyssal` → `t.is_dynamic_type = 1` / `= 0`.
- Omnibox: `stat` joins `_ALL_KINDS` (draft builder), a value placeholder,
  and value completion offering the mutable attributes' display names;
  palette gains a `stat` chip wash under the contrast tests.
- `roll:` (quality-based filter) deferred to a follow-up once polarity is
  verified live.

### 6. Inspector -- `src/evasset/ui/inspector.py`, `ui/assets_view.py`
Rolls section shown only for `is_dynamic_type` rows: header, one row per
attribute -- `display name: **value unit** · NN% of range · ▲/▼/= Δ vs base`
coloured with the existing measured POSITIVE/CRITICAL pairs, tooltip with
range and mutator; note line "Source: … · Mutator: …" / "Stats not fetched
yet" / "ESI has no record of this item"; "Fetch abyssal stats" (or "Retry")
button when status ≠ ok. Loaded through a dedicated `AsyncQuery` guarded on
the inspected item_id. Panel unchanged for every other item.

### 6b. Grilling additions across the UI
- **Badge** (`grouped_model.py` PRICE_BADGE_ROLE): `abyssal` for
  `is_dynamic_type` rows regardless of fetch state; `price_source` stays
  'none' so unpriced semantics are untouched. Tooltip role: per-attribute
  quality summary from one batched query over the visible abyssal item_ids
  (loaded with the reload, cached in the model), or "rolls not fetched".
- **Inspector rows**: bar (rail `_ValueBar` idiom, RAIL_BAR pair, fraction =
  quality) + the text line; rolled attributes only.
- **Settings** (`config.Settings`, `settings_dialog.py` Behaviour group):
  `abyssal_stats_on_sync: bool = False`; `SyncJob` runs `abyssal.fetch_rolls`
  for pending items after assets when enabled; `AbyssalStatsJob` completion
  handler offers to enable it once (a `meta` flag remembers the offer).
- **Startup** (`main_window.py`): if `sde.tables_stale(conn)`, submit
  `SdeUpdateJob` automatically (it uses the cached zip when the build is
  current) before the first tab load.
- **Aliases** (`abyssal.py` `STAT_ALIASES` dict → attribute name), resolved in
  `omni` before the display/internal-name match.

### 7. Docs
Commit research notes as `docs/research/abyssal-stats.md`; README "Where
things live" gains `abyssal.py`; ROADMAP marks the feature.

## Subagent execution plan

### Orchestration
1. The research notes already sit at `docs/research/abyssal-stats.md` in this
   worktree; commit them with the first feature commit.
2. Spawn **A (`abyssal-core`)** and **B (`abyssal-ui`)** in parallel, in one
   message, both in background. Disjoint files; both code to the pinned
   contracts below, never to each other's in-progress files.
3. When both report: orchestrator runs the merge gate (full `uv run pytest`,
   `uv run ruff check .`, offscreen `MainWindow` import), resolves contract
   drift itself, then spawns **C (`abyssal-integration-tests`)**.
4. After C: spawn **D (adversarial reviewer)**. Findings route back to the
   owning package agent, or the orchestrator fixes small ones directly.
5. Orchestrator verification (below), then commit/PR only on the user's word.

### Common briefing (goes into every agent prompt verbatim)
- Workdir: the repository root, branch zero-dev-abyssal, PySide6
  app "EVE Booty" (package still `evasset`; launch `uv run evebooty`).
  Commands: `uv run pytest`, `uv run ruff check .` (E/F/I/UP/B, line 100,
  Python 3.10 floor).
- Read the worktree's CLAUDE.md first and follow it: why-comments, British
  prose, `--` dashes, sentence-style tests, Qt-free logic outside ui/, the
  AsyncQuery/Job lifetime rules (src/evasset/ui/async_query.py docstring),
  and the **mandatory database-safety section**: every throwaway script sets
  `EVASSET_DATA_DIR`/`EVASSET_CACHE_DIR` to a fresh temp dir BEFORE importing
  evasset, asserts isolation after `db.init()`, never runs destructive SQL
  outside its own temp dir; the live DB is opened read-only (`mode=ro`) only.
- Facts and decisions are in this plan; the cited primary-source research is
  `docs/research/abyssal-stats.md`. Do not re-decide settled items. Contract
  deviations are reported loudly, never improvised.
- Touch ONLY your package's files. The full suite may be red on other
  packages' files until integration -- chase only failures in yours.

### Pinned interface contracts (all agents build against these; do not drift)
- Tables exactly as in Implementation §1 (names `sde_dogma_attributes`,
  `sde_dogma_units`, `sde_type_dogma`, `sde_mutator_ranges`, `abyssal_items`,
  `abyssal_attributes`; `abyssal_items.status ∈ {'ok','missing'}`;
  `sde_types.is_dynamic_type INTEGER NOT NULL DEFAULT 0`).
- `queries.ASSET_ROWS` exposes `is_dynamic_type` (from `t.is_dynamic_type`).
- `queries.display_value_sql(value_expr: str, unit_expr: str) -> str` -- the
  unit CASE (101, 108, 111, 109, 127; else raw).
- `queries.fetch_abyssal_rolls(conn, item_id: int) -> dict` with keys
  `status` ('ok'|'missing'|'unfetched'), `source` (type name or None),
  `mutator` (type name or None), `created_by`, `rolls: list[dict]` each
  `{attribute_id, label, unit, value, base, position (0..1|None),
  quality (0..1|None), high_is_good (bool), better (True|False|None)}`,
  ordered by label; rolled attributes only.
- `queries.abyssal_summaries(conn, item_ids: list[int]) -> dict[int, str]`
  -- the badge tooltip one-liner per item (`"Web 61% · Range 88%"`), missing
  keys = not fetched.
- `abyssal.pending(conn, item_ids=None, retry_missing=False) -> list[tuple[int, int]]`
  (item_id, type_id), gated on `is_dynamic_type = 1`.
- `abyssal.fetch_rolls(conn, client, progress=None, should_stop=None,
  item_ids=None, retry_missing=False) -> dict(fetched, missing, failed, remaining)`.
- `abyssal.format_value(value: float, unit_id: int | None, unit_symbol: str | None) -> str`.
- `abyssal.STAT_ALIASES: dict[str, str]` alias → CCP internal attribute name.
- `omni`: kind `"stat"` (value `"<name><op><number>"`, prefix `stat`),
  `IS_FLAGS` gains `"abyssal"`; `omni.STAT_KIND = "stat"` exported.
- `config.Settings.abyssal_stats_on_sync: bool = False`.
- `sde.SDE_TABLES_VERSION: int`, `sde.tables_stale(conn) -> bool`; `import_zip`
  records the version in `meta`.
- UI: `grouped_model` badge string `"abyssal"` via PRICE_BADGE_ROLE; new
  `ABYSSAL_SUMMARY_ROLE = Qt.UserRole + 6` for the tooltip text;
  `Inspector.show_rolls(payload: dict | None)`, signal
  `fetch_abyssal_clicked()`; `workers.AbyssalStatsJob(settings, tokens,
  item_ids=None, retry_missing=False)`.

### Package A -- `abyssal-core` (Qt-free)
Files: `src/evasset/db.py`, `src/evasset/sde.py`, `src/evasset/queries.py`,
new `src/evasset/abyssal.py`, `src/evasset/omni.py`, `src/evasset/config.py`,
new `tests/test_abyssal.py`, `tests/test_omni.py`, `tests/test_core.py`.
Builds Implementation §1-§4 (non-UI), §5 grammar (incl. aliases, `is:abyssal`),
the `Settings` field, `tables_stale`. Tests per the Tests section (Qt-free
list). Exit: `uv run pytest tests/test_abyssal.py tests/test_omni.py
tests/test_core.py` green; ruff clean on owned files. May not touch ui/.

### Package B -- `abyssal-ui`
Files: `src/evasset/ui/workers.py`, `ui/main_window.py`, `ui/inspector.py`,
`ui/assets_view.py`, `ui/grouped_model.py`, `ui/omnibox.py`, `ui/palette.py`,
`ui/settings_dialog.py`, `tests/test_contrast.py`, `tests/test_grouped_model.py`,
`tests/test_main_window_menus.py`.
Builds §4 job + menu, §6 inspector block (bars + text), §6b badge/tooltip
role, settings switch, startup auto-import, omnibox `stat` kind + placeholder
+ completion of attribute names, palette `stat` wash under the contrast
tests. Codes against A's contracts (may stub-call them; must compile once A
lands). Exit: offscreen `from evasset.ui.main_window import MainWindow` ok,
ruff clean; owned tests green where they don't depend on A.

### Package C -- `abyssal-integration-tests` (after A and B merge)
Files: `tests/test_assets_integration.py`, `README.md`, `ROADMAP.md`.
Offscreen tests: inspector rolls block with quality/verdict/bars, fetch button
only when unfetched, hidden for ordinary rows; badge + tooltip summary; stat
chip filters the table in display units via the real pipeline; `is:abyssal`;
settings switch gates the sync-time fetch; startup auto-import fires when
stale. README "Where things live" + Assets-tab grammar docs; ROADMAP entry.
Exit: full suite green, ruff clean.

### Package D -- adversarial review (after C)
Refute the combined diff: polarity on sign-inverted attributes (webifier
speedFactor, painter signatureRadiusBonus) with hand-computed cases; unit
CASE correctness incl. 124/205 pass-through; `stat:` SQL injection surface
and NULL semantics; alias collisions; the `is_dynamic_type` gate (a seeded
mutaplasmid stack must never be requested); 404/ESIError/cancel paths and the
error-limit budget; idempotent store; SDE importer restriction and the
tables-version re-import (fresh install, current-build install, old DB
migration of `sde_types`); AsyncQuery races between rolls/rows/summary
queries; badge/tooltip staleness after a fetch; vacuous tests; README
accuracy. No repo edits; scratchpad only; database-safety rules apply.

### Orchestrator verification (final)
1. Full `uv run pytest` + `uv run ruff check .`.
2. Sandboxed offscreen run seeding the research's live sample item (type
   49738, source 13935, mutator 49740, its 14 attributes) -- inspector rolls,
   badge tooltip and a `stat:` filter agree with hand computation.
3. Real: launch `uv run evebooty` -- startup auto-import fills the dogma
   tables; Update → Abyssal stats over a live estate (error-limit stays
   100; note wall time); inspect one webifier and one painter for polarity;
   `stat:web>55`, `is:abyssal`, hover a badge. Screenshots to the user.

## Tests
- Qt-free: unit table (every rule incl. inverse and signed), position/quality
  incl. negative base and mutator polarity override, missing base → default,
  unknown range → unranked; grammar parse/round-trip/SQL in display units,
  malformed degrade, negated keeps unfetched items, `is:abyssal`; fetch: only
  is_dynamic_type rows are requested (a seeded mutaplasmid stack is never
  called), 404 → missing and not re-asked, ESIError leaves item pending,
  cancel between items, store is idempotent; SDE importers from a synthetic
  mini-zip incl. isDynamicType, applicableTypes restriction and the
  tables-version re-import trigger; sde_types migration on an old DB.
- Offscreen Qt: inspector rolls block with quality/verdict, fetch button only
  when unfetched, section hidden for ordinary rows; menu order/reachability.

## Verification
1. `uv run pytest` / `uv run ruff check .` green.
2. Sandboxed run seeding the research's live sample (type 49738, source 13935,
   mutator 49740, its 14 attributes): inspector rolls and a `stat:` filter
   agree with hand computation.
3. Real: Update → Game data (new tables fill), Update → Abyssal stats on the
   live estate (every item once; error-limit counter stays at 100), inspect a
   webifier and a painter to confirm polarity, filter with `stat:` and
   `is:abyssal`.
