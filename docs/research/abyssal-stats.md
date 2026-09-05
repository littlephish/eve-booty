# Abyssal (mutated) item stats: can we show them, and make them searchable?

Research findings, 2026-09-01. Primary sources only: the live ESI OpenAPI spec and live ESI
responses, CCP's developer documentation, the actual SDE zip (build 3487903, downloaded and
inspected), CCP patch notes, and MutaMarket's own OpenAPI spec and live API. Every claim carries
its source. No code was changed; the repo was read only for context.

Short answer: **yes on both counts, with nothing exotic.** ESI has a public, unauthenticated
route that returns the rolled attributes of any mutated item given the `(type_id, item_id)` pair
the assets sync already stores; the SDE we already download carries attribute names, units,
base values and the mutaplasmid roll ranges needed to render a roll as a human number and as a
"% of possible range". Pricing is the weak leg: CCP has no price for these, and the only live
third-party estimate is MutaMarket's ML model, which is public JSON but undocumented on terms.

---

## 1. ESI: `GET /dogma/dynamic/items/{type_id}/{item_id}`

### 1.1 Path, method, versioning

- The current ESI OpenAPI document (`https://esi.evetech.net/meta/openapi.json`, title
  "EVE SKINR Ingenuity (ESI) - tranquility", `info.version` "2020-01-01", server
  `https://esi.evetech.net`) lists exactly one dynamic-item path: `/dogma/dynamic/items/{type_id}/{item_id}`,
  method GET, `operationId` `GetDogmaDynamicItemsTypeIdItemId`, tag `Dogma`, summary
  "Get dynamic item information".
  Source: https://esi.evetech.net/meta/openapi.json (fetched 2026-09-01; parsed locally).
- Description on the operation, verbatim: *"Returns info about a dynamic item resulting from
  mutation with a mutaplasmid.\n\nThis route expires daily at 11:05"*. Source: same.
- The operation carries `"x-compatibility-date": "2020-01-01"`, i.e. it exists under every
  compatibility date. The app's pinned `COMPATIBILITY_DATE = "2025-08-26"`
  (`src/evasset/config.py:42`) was accepted live (200 below); the response echoed
  `X-Compatibility-Date: 2020-01-01`, which is the route's own date, not an error.
  Source: spec + live response headers (section 1.4).
- How compatibility dates work, verbatim from CCP: *"Every ESI request can include an
  `X-Compatibility-Date` header using the ISO format - `YYYY-MM-DD`."* ... *"If applications
  cannot set custom headers, the `compatibility_date` query parameter will do the same."* ...
  *"If a request does not set a compatibility date, the oldest available compatibility date is
  used."* Source: https://developers.eveonline.com/docs/services/esi/overview/
- Legacy `/v1/`, `/latest/` etc. paths "will work for the foreseeable future" but new routes
  are compatibility-date only; CCP "will aim to maintain at least one year's backwards
  compatibility". Source: https://developers.eveonline.com/blog/changing-versions-v42-was-getting-out-of-hand

### 1.2 Authentication

- The operation has **no `security` block**, and the document has no top-level `security`
  either (parsed: `None`). Compare `/characters/{character_id}/assets`, which has
  `security: [{"OAuth2": ["esi-assets.read_assets.v1"]}]`. Source: openapi.json.
- Confirmed live: an unauthenticated `curl` with only `User-Agent` and `X-Compatibility-Date`
  returned HTTP 200 (section 1.4). So this is a **public** route: it can be called without any
  character token, and the error/rate buckets are keyed by IP, not by character (see 1.6).

### 1.3 Parameters and response schema (verbatim from the spec, dereferenced)

Parameters:

| name | in | required | notes |
| --- | --- | --- | --- |
| `type_id` | path | yes | integer int64 |
| `item_id` | path | yes | integer int64 |
| `X-Compatibility-Date` | header | yes (in spec) | "The compatibility date for the request." schema `format: date`, enum lists `2020-01-01` (the route's date) |
| `X-Tenant` | header | no | default `tranquility` |
| `Accept-Language` | header | no | default `en`; enum en/de/fr/ja/ru/zh/ko/es |
| `If-None-Match` | header | no | "The ETag of the previous request. A 304 will be returned if this matches the current ETag." |
| `If-Modified-Since` | header | no | 304 if unmodified since |

200 response body (`application/json`), all five properties **required**:

```
created_by       integer int64  "The ID of the character who created the item"
dogma_attributes [ { attribute_id: int64 (required), value: double (required) } ]
dogma_effects    [ { effect_id: int64 (required), is_default: boolean (required) } ]
mutator_type_id  integer int64  "The type ID of the mutator used to generate the dynamic item."
source_type_id   integer int64  "The type ID of the source item the mutator was applied to create the dynamic item."
```

200 response headers declared: `Cache-Control`, `ETag`, `Last-Modified`.

Error responses: the new spec declares only a single `default` error with schema
`{ error: string (required), status: int64, details: [ {location, message, value} ] }`. It does
**not** enumerate 304/400/404/420 per route any more; the legacy `/latest/swagger.json` that
did so now returns "404 page not found" (checked 2026-09-01). Real behaviour is therefore
recorded empirically in 1.5.

Source for everything in this subsection: https://esi.evetech.net/meta/openapi.json

### 1.4 Live 200 (real mutated item, obtained from a public MutaMarket listing)

Request: `GET https://esi.evetech.net/dogma/dynamic/items/49738/1000000000001`,
headers `X-Compatibility-Date: 2025-08-26`, `User-Agent: EveBooty-research (...)`, no auth.
49738 is the SDE type "Abyssal Ballistic Control System".

Response headers of note (2026-09-01 ~18:45 UTC):

```
HTTP/1.1 200 OK
Cache-Control: public
Expires: Wed, 02 Sep 2026 11:05:00 GMT
Last-Modified: Mon, 31 Aug 2026 11:03:28 GMT
Etag: W/"3e25307efd650eebf0cd6b419012ac84927dc275bba12219dcebc00e"
X-Compatibility-Date: 2020-01-01
X-Esi-Cache-Status: HIT
X-Esi-Error-Limit-Remain: 100
X-Esi-Error-Limit-Reset: 34
Vary: X-Tenant / Accept-Language / X-Compatibility-Date
```

Body (verbatim except the item id and `created_by`, which are replaced by synthetic ids so no pilot is named):

```json
{"created_by":90000001,
 "dogma_attributes":[{"attribute_id":4,"value":1.0},{"attribute_id":9,"value":40.0},
  {"attribute_id":204,"value":0.8828844567859173},{"attribute_id":213,"value":1.1077080251407625},
  {"attribute_id":1692,"value":4.0},{"attribute_id":30,"value":1.0},{"attribute_id":161,"value":5.0},
  {"attribute_id":162,"value":1.0},{"attribute_id":422,"value":1.0},{"attribute_id":38,"value":0.0},
  {"attribute_id":50,"value":25.799999713897705},{"attribute_id":182,"value":3318.0},
  {"attribute_id":633,"value":8.0},{"attribute_id":277,"value":1.0}],
 "dogma_effects":[{"effect_id":11,"is_default":false},{"effect_id":16,"is_default":false},
  {"effect_id":763,"is_default":false},{"effect_id":889,"is_default":false},{"effect_id":6556,"is_default":false}],
 "mutator_type_id":49740,"source_type_id":13935}
```

Observations:
- `Expires` is the next 11:05 UTC, matching the spec's "expires daily at 11:05". A conditional
  re-request with `If-None-Match: <that ETag>` returned **304**.
- `dogma_attributes` is the item's **full** attribute list (14 entries here), not only the
  mutated ones. The mutated ones are found by intersecting with the mutaplasmid's
  `dynamicItemAttributes` entry (section 2.4): here 50 cpu, 204 speedMultiplier,
  213 missileDamageMultiplierBonus (and 1255 droneDamageBonus, which is absent because it is 0
  on this source type).
- `source_type_id` 13935 is "Domination Ballistic Control System" (SDE), meta group 4 Faction;
  `mutator_type_id` 49740 is "Gravid Ballistic Control System Mutaplasmid" (SDE).
- Caution on ETags: the **same** weak ETag `W/"3e25...ebc00e"` came back on this 200, on a 400
  and on a 404 with different bodies (1.5). Treat the ETag as an opaque revalidation token
  only, not as a content hash. For this use case it does not matter: a mutated item's roll is
  permanent (1.7), so one successful fetch per `item_id` is all that is ever needed.

### 1.5 Live error behaviour (observed, one request each)

| request | status | body | error-limit effect |
| --- | --- | --- | --- |
| `/dogma/dynamic/items/13935/1000000000001` (real item, but the **source** type id instead of the abyssal type id) | 404 | `{"error":"Item not found"}` | Remain 100 -> 99 |
| `/dogma/dynamic/items/47408/1` (nonsense item id) | 404 | `{"error":"Item not found"}` | -1 |
| `/dogma/dynamic/items/47820/1000000000002` (the example ids in MutaMarket's API docs; item presumably no longer exists) | 400 | `{"error":"Invalid item ID"}` | -1 |
| same item id with type 47408 | 400 | `{"error":"Invalid item ID"}` | -1 |

Conclusions that follow directly: (a) `type_id` **must be the mutated ("Abyssal ...") type id**,
which is exactly what the assets route returns for the item, so no source-type lookup is
required before calling; (b) a non-mutated or unknown item yields a 4xx, and every 4xx consumes
one unit of the 100-per-minute error budget (1.6), so the sync must only call this route for
rows already known to be mutated types (section 3) rather than probing every singleton; (c) the
distinction between 400 "Invalid item ID" and 404 "Item not found" is **not documented**
anywhere I could find; the 400 case is recorded as observed, its cause is inferred (see
Unverified).

### 1.6 Error limiting and rate limiting

- Verbatim: *"This allows at most 100 non-2xx/3xx responses per minute. After that, it will
  return 420s on all ESI routes."* Headers: `X-ESI-Error-Limit-Remain` ("errors left in this
  time frame"), `X-ESI-Error-Limit-Reset` ("seconds left until next time frame and errors reset
  to zero"). *"Once you reach the error limit, all your request are automatically discarded
  until the end of the time frame."* Sources:
  https://developers.eveonline.com/docs/services/esi/rate-limiting/ and
  https://developers.eveonline.com/docs/services/esi/best-practices/
- Bucket rate limiting (newer): each "rate limit group and userID pair" gets a bucket; for
  unauthenticated routes the key is `<sourceIP>` or `<sourceIP>:<applicationID>`; token costs
  "2XX responses: 2 tokens, 3XX: 1, 4XX: 5 (excluding 429), 5XX: 0"; headers `X-Ratelimit-Group`,
  `X-Ratelimit-Limit` (format `150/15m`), `X-Ratelimit-Remaining`, `X-Ratelimit-Used`,
  `Retry-After` on 429. Source: https://developers.eveonline.com/docs/services/esi/rate-limiting/
- Observed: none of the dynamic-items responses above carried any `X-Ratelimit-*` header
  (they are only named in `Access-Control-Expose-Headers`). So as of 2026-09-01 this route does
  not appear to belong to a rate-limit group; that could change, so the client should honour
  `X-Ratelimit-*`/`Retry-After` if they ever appear. The app's `ESIClient` already tracks
  `X-ESI-Error-Limit-*` and sleeps on 420 (`src/evasset/esi/client.py:30-44, 109-110`).
- Sizing for ~500 owned abyssal items: 500 successful GETs, once ever (rolls are permanent),
  spread across syncs as new item_ids appear; zero error-budget cost if the call is gated to
  mutated types. Caching guidance: *"The `expires` header represents when the resource cache in
  ESI should expire"*; do not re-request before then. Source: best-practices page above.
- User-Agent: *"All ESI requests should contain User Agent information"* with contact
  details; the app already sends `user_agent(settings)`. Source: best-practices page.

### 1.7 Assets route already provides the inputs

- `/characters/{character_id}/assets` (and `/corporations/{corporation_id}/assets`) 200 item
  schema, required fields: `type_id, quantity, location_id, location_type, item_id,
  location_flag, is_singleton`; optional `is_blueprint_copy`. Scope
  `esi-assets.read_assets.v1`. Source: https://esi.evetech.net/meta/openapi.json
- The app stores all of these (`ASSET_COLS` in `src/evasset/esi/sync.py:173-176`; table
  `assets` in `src/evasset/db.py:197-214`). So `(type_id, item_id)` for every owned mutated
  module is already in SQLite; the new step is a per-item GET keyed on it.
- Permanence: *"Once an Abyssal Tech module is created, it cannot be reverted to its components
  or rerolled with additional Mutaplasmids"* and *"Mutaplasmids alter attributes unpredictably
  by a percent value that falls within a specific range described on the Mutaplasmid"*.
  Source: https://www.eveonline.com/news/view/patch-notes-for-eve-online-into-the-abyss (2018-05-25)

---

## 2. Static Data Export (JSONL)

### 2.1 Where it is, how it is shaped

- `https://developers.eveonline.com/static-data/tranquility/latest.jsonl` today contains exactly:
  `{"_key": "sde", "buildNumber": 3487903, "releaseDate": "2026-09-01T11:30:39Z"}`.
  Source: fetched 2026-09-01.
- CCP's documented URL pattern:
  `https://developers.eveonline.com/static-data/tranquility/eve-online-static-data-<build-number>-<variant>.zip`,
  plus always-latest shorthands `.../static-data/eve-online-static-data-latest-jsonl.zip` and
  `-yaml.zip`. Record format for JSONL: *"`_key`: The actual key value"* and *"`_value`: The value
  (when the value is not an object)"*. Change files:
  `.../static-data/tranquility/changes/<build-number>.jsonl` (record `_meta` carries
  `lastBuildNumber`). Schema changes: `.../static-data/tranquility/schema-changelog.yaml`.
  Source: https://developers.eveonline.com/docs/services/static-data/
- The app already follows this (`SDE_LATEST_URL`, `SDE_BUILD_URL` in `config.py:90-94`;
  `_records()` in `sde.py:94-98`).
- The zip `eve-online-static-data-3487903-jsonl.zip` is 99,069,098 bytes and contains (among
  ~100 files) all of: `dogmaAttributes.jsonl` (1,288,439 B), `dogmaUnits.jsonl` (26,047 B),
  `dogmaAttributeCategories.jsonl` (3,227 B), `dogmaEffects.jsonl` (2,085,181 B),
  `typeDogma.jsonl`, `dynamicItemAttributes.jsonl` (166,282 B), `metaGroups.jsonl` (5,591 B),
  `types.jsonl`. Source: zip listing, downloaded to scratchpad 2026-09-01.
- CCP on the rework: *"Added the JSON Lines format alongside YAML"*; newly added collections
  include `dogmaUnits` and `dynamicItemAttributes`; *"no more `nameID` field for strings; it is
  now just `name`"*. Source: https://developers.eveonline.com/blog/reworking-the-sde-a-fresh-start-for-static-data

### 2.2 `dogmaAttributes.jsonl` (attribute metadata)

- 2,867 records. Union of field names observed: `_key, attributeCategoryID,
  chargeRechargeTimeID, dataType, defaultValue, description, displayName, displayWhenZero,
  highIsGood, iconID, maxAttributeID, minAttributeID, name, published, stackable,
  tooltipDescription, tooltipTitle, unitID`. `displayName`, `tooltipTitle`,
  `tooltipDescription` are localisation dicts (`{"en": ..., "de": ...}`); `name` and
  `description` are plain strings. 1,353 records have `unitID`, 1,248 have `displayName`.
  Source: inspected zip.
- Example record (verbatim, trimmed to English):
  `{"_key": 30, "attributeCategoryID": 1, "dataType": 4, "defaultValue": 0.0, "description": "current power need", "displayName": {"en": "Powergrid Usage", ...}, "displayWhenZero": false, "highIsGood": false, "iconID": 70, "name": "power", "published": true, "stackable": true, "unitID": 107}`
- Schema changelog confirms the spelling differences from the old YAML: build 2960198:
  *"dogmaAttributes: categoryID: attributeCategoryID"* and *"dogmaAttributes: attributeID:
  removed (was identical to key)."* Source: https://developers.eveonline.com/static-data/tranquility/schema-changelog.yaml
- All 52 attributes that any mutaplasmid can mutate (2.4) have a `displayName`; 51 have a
  `unitID`; the exception is 160 `trackingSpeed` (no `unitID`). Source: inspected zip.

### 2.3 `typeDogma.jsonl` (base values per type)

- Shape: `{"_key": <typeID>, "dogmaAttributes": [{"attributeID": ..., "value": ...}], "dogmaEffects": [{"effectID": ..., "isDefault": ...}]}`. Note camelCase `attributeID`
  here versus ESI's `attribute_id`. Source: inspected zip.
- Important: the **abyssal type itself carries almost no dogma**. Type 47408 "50MN Abyssal
  Microwarpdrive" has only `[{182: 3454}, {277: 4}]` (required skill/level) while a real source
  type such as 5975 has the full list (6 capacitorNeed 130, 20 speedFactor 505, 30 power 150,
  50 cpu 50, 73 duration 10000, 554 signatureRadiusBonus 500, ...). So "compare the roll to the
  un-mutated module" must join on ESI's `source_type_id`, not on the asset's `type_id`.
  Source: inspected zip.

### 2.4 `dynamicItemAttributes.jsonl` (mutaplasmid roll ranges)

- 413 records, keyed by the **mutaplasmid** type id. Field names: `_key`, `attributeIDs`
  (list of `{"_key": <attributeID>, "min": <float>, "max": <float>, "highIsGood"?: bool}`),
  `inputOutputMapping` (list of `{"applicableTypes": [typeIDs], "resultingType": typeID}`).
  Every one of the 413 records has exactly one `inputOutputMapping` entry. Source: inspected zip.
- Example (verbatim): `{"_key": 47297, "attributeIDs": [{"_key": 6, "max": 1.4, "min": 0.6}, {"_key": 20, "max": 1.1, "min": 0.9}, {"_key": 30, "max": 1.5, "min": 0.8}, {"_key": 50, "max": 1.5, "min": 0.8}, {"_key": 554, "max": 1.3, "min": 0.7}], "inputOutputMapping": [{"applicableTypes": [5975, 12052, ...], "resultingType": 47408}]}`
- `min`/`max` are multipliers applied to the source type's base value (e.g. cpu 0.8x-1.5x),
  consistent with CCP's "by a percent value that falls within a specific range" (1.7). Roll
  quality for attribute A on item I is therefore
  `(value_I / base_source(A) - min) / (max - min)` (invert when the attribute is
  low-is-good). This is the same normalisation MutaMarket exposes as `fraction_type` (section 4).
- The 89 distinct `resultingType` values are exactly the 89 `isDynamicType` types (2.5); several
  mutaplasmids map to one abyssal type with different ranges (e.g. 47408 <- 47297, 47741,
  47742, 85473, 85474, 85475), so range lookup must key on ESI's `mutator_type_id`, not on the
  abyssal type. Source: inspected zip.
- `attributeIDs[].highIsGood` is a per-mutaplasmid override, present on only a few entries
  (observed: 20 speedFactor -> false on some, 73 duration -> true on some). Changelog: build
  3030547 *"dynamicItemAttributes: attributeIDs.[].highIsGood: added field."*, build 3031812
  *"type changed to a boolean."* Fall back to `dogmaAttributes.highIsGood` when absent.
  Sources: inspected zip; schema-changelog.yaml.
- Distinct mutable attributes (52): 6 capacitorNeed, 9 hp, 20 speedFactor, 30 power,
  37 maxVelocity, 50 cpu, 54 maxRange, 64 damageMultiplier, 67 capacitorBonus, 68 shieldBonus,
  72 capacityBonus, 73 duration, 77 miningAmount, 84 armorDamageAmount,
  90 powerTransferAmount, 97 energyNeutralizerAmount, 99 empFieldRange, 114/116/117/118
  em/explosive/kinetic/thermalDamage, 158 falloff, 160 trackingSpeed, 204 speedMultiplier,
  213 missileDamageMultiplierBonus, 263 shieldCapacity, 265 armorHP, 554 signatureRadiusBonus,
  796 massAddition, 974-977 hull*DamageResonance, 983 signatureRadiusAdd, 1159 armorHPBonusAdd,
  1255 droneDamageBonus, 1795 reloadTime, 2267 energyWarfareResistanceBonus,
  2306/2307 siege*DamageBonus, 2335-2338 fighterBonus*, 2346/2347 siegeLocalLogistics*,
  3153 miningWastedVolumeMultiplier, 3154 miningWasteProbability, 5967 miningCritChance,
  5969 miningCritBonusYield. Source: inspected zip.

### 2.5 `types.jsonl` and `metaGroups.jsonl`

- `metaGroups.jsonl` record 15 (verbatim, English): `{"_key": 15, "color": {"b": 0.12549, "g": 0.12549, "r": 0.529412}, "iconID": 24152, "iconSuffix": "abyssal", "name": {"en": "Abyssal", ...}}`. Source: inspected zip.
- **Meta group 15 is not only mutated modules.** 260 types have `metaGroupID: 15`: 170 are
  Mutaplasmids (group 1964), 1 is a Mutaplasmid Blueprint (group 4820), and 89 are the mutated
  module/drone types. Gating on `meta_group_id = 15` alone would send mutaplasmid stacks to the
  dynamic-items route and burn error budget. Source: inspected zip.
- `types.jsonl` now has a boolean `isDynamicType`; it is `true` on exactly those 89 types and on
  nothing else. Changelog build 3464040 (2026-08-12): *"types: isDynamicType: added field."*
  and *"isRepackable: added field."* Sources: inspected zip; schema-changelog.yaml.
  The app's `_import_types` (`sde.py:167-184`) does not import it yet.
- Example abyssal type (verbatim, minus description): `{"_key": 47408, "groupID": 46, "iconID": 10149, "isDynamicType": true, "mass": 1.0, "metaGroupID": 15, "metaLevel": 10, "name": {"en": "50MN Abyssal Microwarpdrive", ...}, "packagedVolume": 10.0, "portionSize": 1, "published": true, "techLevel": 1, "volume": 10.0}`

### 2.6 `dogmaUnits.jsonl` and `dogmaAttributeCategories.jsonl`

- `dogmaUnits.jsonl`: 60 records, fields `_key, name, displayName (loc dict), description (loc dict)`.
  Changelog build 2960198: *"dogmaUnits: added."* Sources: inspected zip; schema-changelog.yaml.
- `dogmaAttributeCategories.jsonl`: `{"_key": 1, "description": "Fitting capabilities of a ship", "name": "Fitting"}` etc. Useful for grouping the attribute list in the inspector. Source: inspected zip.

---

## 3. Identifying abyssal items in the local data

- ESI marks nothing on the asset row itself; the only per-row facts are `type_id` and
  `is_singleton` (1.7). The **type** is what identifies a mutated item: its `type_id` is one of
  the 89 "Abyssal ..." types (`isDynamicType: true`, `metaGroupID: 15`). Source: 2.5.
- Mutated items are unique and unstackable; CCP: *"Abyssal Tech modules cannot be traded on the
  market but can be traded in contracts or trade windows"* while *"Mutaplasmids can be traded on
  the market"*. Source: https://www.eveonline.com/news/view/patch-notes-for-eve-online-into-the-abyss
  (The support article https://support.eveonline.com/hc/en-us/articles/360000848365-Mutaplasmids
  is reported by search to add "cannot be repackaged", but it returned HTTP 403 to my fetch; see
  Unverified.)
- The un-mutated source type and the mutaplasmid used are **only** knowable from ESI's
  `source_type_id` / `mutator_type_id` (1.3). The SDE can only tell you the set of possible
  sources (`applicableTypes`) and possible mutaplasmids per abyssal type (2.4); the abyssal
  type's own `typeDogma` is nearly empty (2.3).
- The app currently colours meta group 15 as "Abyssal" in the inspector and fit dialog
  (`ui/palette.py:56-63`, `ui/inspector.py:50-55`) but stores no per-item stats.

---

## 4. Pricing / appraisal (secondary goal)

- **First-party:** none. CCP exposes no price for mutated items; they are not on the market by
  design (section 3). The app's `is:unpriced` flag (`omni.py:86-89`) is therefore the correct
  state for them under current sources.
- **mutaplasmid.space:** DNS lookup fails (`getaddrinfo ENOTFOUND mutaplasmid.space`,
  2026-09-01) and the search engine's cached title for the root is "This website is for sale!".
  Treat it as defunct; no API to evaluate.
- **MutaMarket (mutamarket.com):** listed by CCP as a community tool. CCP's page, verbatim:
  *"A free public JSON API exposes modules for sale (with the full filter syntax), single-module
  lookups, module imports, and per-type roll statistics. No authentication is required."*
  Source: https://developers.eveonline.com/docs/community/mutamarket/
- MutaMarket's OpenAPI 3.0.3 document (`https://mutamarket.com/api/documentation.openapi`;
  a Postman export is at `.../documentation.postman`; the human page is
  `https://mutamarket.com/api/documentation`). Note its `servers.url` is the placeholder
  `https://mutamarket.test`; the live host `https://mutamarket.com` answered 200. Endpoints:
  - `GET /api/modules/{module_slug}` "Get a single module": *"Returns a single module with all
    rolled attributes, roll-quality metrics, the estimated value, and its current sale listing
    (contract or MutaMarket sell listing), if any. The module must have been imported into
    MutaMarket before; unknown item ids return 404."*
  - `POST /api/modules` "Import a module from EVE": body `{type_id, item_id}` or
    `{message: "...showinfo:{type_id}//{item_id}..."}`; *"The module data is fetched live from the
    EVE ESI API and the value estimation runs synchronously, so expect this request to take a
    few seconds. Re-submitting an existing module refreshes its data instead of duplicating it."*
    Its `type_id` field is described as *"The EVE type id of the (unmutated) source module"*,
    which contradicts what ESI accepts (1.5); see Unverified.
  - `GET /api/modules/{query}` "List modules of a type" (`type/{id-or-slug}` plus chained
    filter segments such as `attributes/cpu/20-30`, `sort/value/desc`; cursor-paginated, 100
    per page; `region_id` query param).
  - `GET /api/abyssal-type-statistics`: *"the best and worst possible rolled value of every
    mutated attribute for every abyssal type ... the same data MutaMarket uses for the
    `fraction_type` roll-quality metric"*.
  - `GET /api/estimator-statistics`: per-type `data_count`, `r2`, `mae`, `nmae`.
  Every operation has `security: []`. Source: the spec file above, parsed 2026-09-01.
- Live sample (`GET https://mutamarket.com/api/modules/type/49738`, 200): module objects carry
  `id` (= ESI item_id), `type {id,name}`, `source_type {id,name,meta_group,meta_group_id}`,
  `mutaplasmid {id,name}`, `creator`, `mutated_attributes[] {id, name, display_name, value,
  base_value, fraction, fraction_type, fraction_absolute, bar, is_derived, is_virtual,
  unit {id,name,display_name}}`, `average_fraction`, `estimated_value`,
  `estimated_value_updated_at`, `contract {id,type,price,date_issued,date_expired,...}`,
  `public_asset`, `slug`. Source: live response, 2026-09-01.
- How the estimate is made, from MutaMarket's docs: *"a machine-learning model — a Random
  Forest Regression — trained separately for every abyssal module type on recorded trades"*;
  a type needs *"at least 50 recorded trades"* or shows "No AI prediction available";
  *"the appraisal tool provides estimates, not definitive prices"*.
  Source: https://raw.githubusercontent.com/MutaMarket/MutaMarket/main/docs/04-appraisal.md
- Terms: `docs/15-legal.md` has only the CCP trademark notice and contact details; **no API
  terms of use, rate limits or attribution requirements are published** in the docs repo
  (MIT-licensed docs) or in the OpenAPI spec. Source:
  https://raw.githubusercontent.com/MutaMarket/MutaMarket/main/docs/15-legal.md ;
  https://github.com/MutaMarket/MutaMarket
- Privacy note for the app: `POST /api/modules` publishes the user's item into a third-party
  database that anyone can then read via `GET /api/modules/{item_id}`; it must be opt-in.
- Other appraisal services accepting abyssal item ids: none found on first-party pages; the
  only other hits were forum threads. Not investigated further because nothing documented
  exists to cite.

---

## 5. Unit rendering

All from `dogmaUnits.jsonl` (build 3487903), English `displayName` / `description`:

| unitID | name | display | description / meaning | render rule |
| --- | --- | --- | --- | --- |
| 1 | Length | m | Meter | value m (km when large) |
| 2 | Mass | kg | Kilogram | value kg |
| 9 | Volume | m3 | Cubic meter | value m3 |
| 11 | Acceleration | m/sec (sic) | Meter per second squared | value m/s2 |
| 101 | Milliseconds | s | (none) | value / 1000, "s" |
| 104 | Multiplier | x | "Indicates that the unit is a multiplier." | value x |
| 105 | Percentage | % | (none) | value % |
| 106 | Teraflops | tf | | value tf |
| 107 | MegaWatts | MW | | value MW |
| 108 | Inverse Absolute Percent | % | "Used for resistance. 0.0 = 100% 1.0 = 0%" | (1 - value) * 100 % |
| 109 | Modifier Percent | % | "Used for multipliers displayed as %1.1 = +10%0.9 = -10%" | (value - 1) * 100, signed % |
| 111 | Inversed Modifier Percent | % | "Used to modify damage resistance. Damage resistance bonus. 0.1 = 90% 0.9 = 10%" | (1 - value) * 100 % |
| 113 | Hitpoints | HP | | value HP |
| 114 | capacitor units | GJ | Giga Joule | value GJ |
| 120 | attributePoints | points | | value |
| 121 | realPercent | % | "Used for real percentages, i.e. the number 5 is 5%" | value % |
| 124 | Modifier Relative Percent | % | "Used for relative percentages displayed as %" | value, signed % |
| 127 | Absolute Percent | % | "0.0 = 0% 1.0 = 100%" | value * 100 % |
| 123 | trueTime | sec | "Shows seconds directly" | value s |
| 139 | Bonus | + | "Forces a plus sign for positive values" | |
| 205 | modifier realPercent | % | "10 is +10% -10 is -10% 3.6 is +3.6%" | signed value % |

Units actually used by the 52 mutable attributes (count of mutaplasmid-attribute pairs):
106 Teraflops 371, 107 MegaWatts 297, 114 GJ 283, 113 HP 238, 1 m 233, 101 ms 165, 104 x 90,
124 Modifier Relative % 78, 127 Absolute % 54, 108 Inverse Absolute % 48, 105 % 44, 111
Inversed Modifier % 40, 9 m3 37, 11 m/s2 34, 121 realPercent 28, 120 points 24, 2 kg 18,
109 Modifier % 10, and 32 pairs with no unit (160 trackingSpeed). Source: inspected zip.

Which way is "good": `dogmaAttributes.highIsGood` (e.g. `power` false, `maxRange` true), with
the per-mutaplasmid override in `dynamicItemAttributes.attributeIDs[].highIsGood` taking
precedence when present (2.4). ESI itself carries no unit or direction information; everything
above comes from the SDE.

---

## 6. What this means for implementing it in this app (no code, just the shape)

Data model (SQLite, alongside the existing `assets` and `sde_*` tables in `db.py`):

- `dynamic_items (item_id INTEGER PRIMARY KEY, type_id, source_type_id, mutator_type_id,
  created_by, fetched_at)` -- one row per mutated item, keyed by `item_id` because that is
  ESI's key and rolls are permanent (1.7). Not owner-scoped: an item keeps its id when it
  changes hands, and the row is still correct.
- `dynamic_item_attributes (item_id, attribute_id, value, PRIMARY KEY(item_id, attribute_id))`
  -- straight from `dogma_attributes`. Store the full list (1.4); derive "mutated subset" at
  query time via the mutator's range table.
- `sde_dogma_attributes (attribute_id PK, name, display_name, unit_id, high_is_good,
  published, category_id)` from `dogmaAttributes.jsonl` (2.2); `sde_dogma_units (unit_id PK,
  name, display_name, description)` (2.6).
- `sde_type_dogma (type_id, attribute_id, value)` from `typeDogma.jsonl` -- can be restricted
  at import time to the union of all `applicableTypes` (a few hundred source types) to avoid
  importing every type's dogma (2.3, 2.4).
- `sde_mutator_ranges (mutator_type_id, attribute_id, min, max, high_is_good NULL,
  resulting_type_id)` from `dynamicItemAttributes.jsonl` (2.4).
- `sde_types` gains `is_dynamic_type INTEGER` from `types.isDynamicType` (2.5).

Sync step (`esi/sync.py`), after `_store_assets`: select `assets.item_id, type_id` where the
type is `is_dynamic_type = 1` and no `dynamic_items` row exists; GET
`/dogma/dynamic/items/{type_id}/{item_id}` unauthenticated (no `character_id`), one call per
item, honouring the existing error-limit tracker; treat 4xx as "record as unavailable, do not
retry this sync" so a stale or repackaged id cannot loop the error budget (1.5, 1.6). Gate on
`isDynamicType`, **not** `meta_group_id = 15`, or mutaplasmid stacks would be probed (2.5).
This is a public route, so it also works for corp assets synced via a character (1.2).

Inspector (`ui/inspector.py`): for a `dynamic_items` row, list the attributes whose id is in the
mutator's range table, each as `display_name: rendered(value, unit)`, the source type's base
value, the signed delta, and roll-in-range percent, plus "Source: <source type name>" and
"Mutaplasmid: <mutator name>" lines. Render rules per unit are in section 5.

Omnibox grammar (`omni.py`): a new chip kind, e.g. `attr:<name><op><number>` such as
`attr:cpu<24`, `attr:damageMultiplier>=1.2`, `attr:roll>=0.8` (average roll quality), resolved
against `dynamic_item_attributes` joined on `sde_dogma_attributes.name`; the completer can
offer the 52 mutable attribute names (2.4). Comparisons should be done in stored units, with the
display conversion (section 5) applied only when rendering, or the grammar must convert the
typed number per unit (e.g. `duration<9s` -> 9000). `is:abyssal` maps to `t.is_dynamic_type = 1`.

Pricing (optional, opt-in): `GET https://mutamarket.com/api/modules/{item_id}` returns
`estimated_value` for items MutaMarket already knows; `POST /api/modules` imports one and
publishes it. Undocumented terms and rate limits; the estimate is a per-type ML model with
stated error metrics, so any use should surface `nmae`/`r2` from `/api/estimator-statistics`
and label the source distinctly from `jita`/`contract_avg` in `prices.source` (4).

---

## 7. Unverified / could not confirm

- Whether the 400 `{"error":"Invalid item ID"}` case (1.5) means "item no longer exists", "item
  exists but is not dynamic", or an id-range check. No CCP documentation describes it; the
  legacy swagger that enumerated per-route error codes is gone (404). Recorded as observed only.
- Whether ESI's ETag on this route is stable across all responses by design (the same weak
  ETag appeared on a 200, a 400 and a 404). Observed once; no documentation.
- Whether `/dogma/dynamic/items` will be placed in a bucket rate-limit group. No
  `X-Ratelimit-*` headers were present on 2026-09-01.
- The exact semantics of `dynamicItemAttributes.attributeIDs[].highIsGood` when it contradicts
  `dogmaAttributes.highIsGood` (e.g. `duration` true on some mutaplasmids). Assumed to be an
  override; the schema changelog only records its addition.
- The claim that mutated modules "cannot be repackaged" comes from a search-engine snippet of
  https://support.eveonline.com/hc/en-us/articles/360000848365-Mutaplasmids; the page itself
  returned HTTP 403 to my fetch. The `types.isRepackable` field added in build 3464040 is the
  likely first-party source but was not inspected for the 89 abyssal types.
- MutaMarket's `POST /api/modules` documents `type_id` as the un-mutated source type, whereas
  ESI rejected the source type id with 404 and accepted the abyssal type id. MutaMarket may
  resolve either internally; not tested (a POST would publish an item, which I did not do).
- MutaMarket API terms of use, rate limits, attribution requirements and SLA: not published
  anywhere I could find (spec, docs repo, CCP community page).
- Any other third-party abyssal appraisal API: none found with documentation; not proven
  absent.
- `dogmaEffects.jsonl` field names were not inspected beyond confirming the file exists;
  effects are not needed for stat display.

---

## 8. Where this file should live in the repo

The repo today has `README.md`, `ROADMAP.md`, `LICENSE`, and a `docs/` folder that holds
no images. `README.md` has a "Where things live" table and a "Versioning against ESI"
section; `ROADMAP.md` keeps a "Next up" list.

Recommendation: commit this as `docs/research/abyssal-stats.md` (creating `docs/research/`
so that future primary-source write-ups have a home), and add one "Next up" bullet in
`ROADMAP.md` -- "Abyssal roll stats and
`attr:` filters" -- linking to it. When the feature lands, the durable facts (route, gating on
`isDynamicType`, unit rules) belong as comments next to the code in `sde.py`, `esi/sync.py` and
`omni.py` in the repo's existing style, with the README "Where things live" table gaining the
new module row.

---

## Appendix: sources consulted

- https://esi.evetech.net/meta/openapi.json (ESI OpenAPI; parsed locally)
- Live ESI calls to `https://esi.evetech.net/dogma/dynamic/items/...` on 2026-09-01 (section 1.4-1.5)
- https://developers.eveonline.com/docs/services/esi/overview/
- https://developers.eveonline.com/docs/services/esi/rate-limiting/
- https://developers.eveonline.com/docs/services/esi/best-practices/
- https://developers.eveonline.com/blog/changing-versions-v42-was-getting-out-of-hand
- https://developers.eveonline.com/docs/services/static-data/
- https://developers.eveonline.com/static-data/tranquility/latest.jsonl
- https://developers.eveonline.com/static-data/tranquility/eve-online-static-data-3487903-jsonl.zip (inspected)
- https://developers.eveonline.com/static-data/tranquility/schema-changelog.yaml
- https://developers.eveonline.com/blog/reworking-the-sde-a-fresh-start-for-static-data
- https://www.eveonline.com/news/view/patch-notes-for-eve-online-into-the-abyss
- https://developers.eveonline.com/docs/community/mutamarket/
- https://mutamarket.com/api/documentation.openapi (parsed) and live `GET /api/modules/type/49738`
- https://raw.githubusercontent.com/MutaMarket/MutaMarket/main/docs/04-appraisal.md
- https://raw.githubusercontent.com/MutaMarket/MutaMarket/main/docs/15-legal.md
- Repo (read-only): `src/evasset/config.py`, `sde.py`, `db.py`, `omni.py`, `esi/sync.py`, `esi/client.py`, `ui/inspector.py`, `ui/palette.py`, `README.md`, `ROADMAP.md`
