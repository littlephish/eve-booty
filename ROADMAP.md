# Roadmap

A burn-down. Anything not ticked is not built. Tick it in the commit that
builds it and move it to [Shipped](#shipped) when it goes out in a release.

Items say what they actually require, because several obvious-sounding features
are capped by what ESI exposes rather than by effort. Where that is true it is
recorded next to the item so nobody rediscovers it.

## Next

- [ ] **Asset safety wraps, searchable** (jEveAssets parity)

      Items in asset safety already resolve to an "Asset Safety" location and
      match `is:safety`, but they list flat. jEveAssets shows the wrap as a
      container you can open.

      ESI tags every asset with a `location_flag`, and `AssetSafety` is one
      value; `Deliveries` and `CapsuleerDeliveries` have the same shape of
      problem and should be solved once. Start with a spike: confirm whether
      wrap contents come back as children of the wrap's `item_id` or as flat
      rows. That answer decides whether this is a query change or a sync
      change, and it is cheap to establish before designing anything.

- [ ] **Citadel service view**

      What a structure is running, from `/corporations/{id}/structures`.
      `services` is already synced and stored as JSON on the `structures`
      table, so this is a UI job with no new sync work.

      Know the ceiling before designing it: that endpoint returns `services[]`
      as `{name, state}` and nothing more. ESI has no endpoint for a
      structure's rigs or modules, so this can show which services are online
      or offline and nothing about the fit. Naming it a "fitting view" would
      promise more than it can deliver.

- [ ] **Citadel profile assignment**

      `/corporations/{id}/structures` returns `profile_id`, the access
      profile applied to each structure. We do not store it, so this needs one
      column and one sync field.

      What it buys is grouping: "these six citadels share a profile and this
      one does not", which is how a misconfigured structure gets noticed.

      ESI exposes nothing else. Searched the whole 182-path spec: there is no
      `acl`, `access` or `profile` path, so the name of a profile and its
      contents are unavailable. This can show *which* profile is set and never
      *what is in it*. If the bare number is not useful enough, drop the item
      rather than build it badly.

- [ ] **MCP server**

      Expose the local database to an LLM over the Model Context Protocol, so
      questions like "what am I holding in Amarr that is worth more than 100m"
      or "which citadel runs out of fuel first" can be asked in words.

      The groundwork is already there. The database is opened
      `journal_mode=WAL`, so a second process can read it while the app has it
      open, and every query module (`queries`, `omni`, `stockpile`,
      `networth`, `treemap`, `fitting`) is Qt-free and importable headless.
      Best of all `omni.parse()` already turns a text query into SQL: the
      omnibox grammar was built for a human typing filters, which is exactly
      the shape a model emits. A tool that takes an omnibox string is most of
      the feature.

      This is the one item on this list that deliberately sends account data
      to a third party, so it needs deciding before it is built, not after:

      - **Read-only.** No tool writes to the database, changes settings, or
        starts a sync. A model should not be able to alter an estate it is
        describing.
      - **Never expose credentials.** Refresh tokens live in the OS credential
        store and must stay there. `settings.json` holds the client id and is
        not a data source. No tool returns either.
      - **stdio, not HTTP.** A local stdio server is a child process talking
        to one client. An HTTP or SSE transport is a network listener serving
        somebody's entire asset list and wallet history, and the moment it
        binds a port that is a decision about other people on the network too.
      - **Off by default,** and started explicitly. Someone who installed an
        asset tracker did not thereby agree to hand their wallet journal to a
        model.
      - **Optional dependency, separate entry point.** The MCP SDK must not
        land in the GUI build; the Nuitka artifact is already 138 MB.

## Later

- [ ] **Tree view** - assets as an expandable location → container → item tree instead
  of a flat table. The data model already carries the parent links.
- [ ] **Item database** - browse and search every published type in the SDE, not just
  what you own. Everything needed is already imported.
- [ ] **Realised profit per item** - the Transactions tab shows cash in against cash
  out, which is not profit. Matching a sale back to the lot it came from needs
  FIFO inventory accounting over the transaction history.
- [ ] **Reprocessing** - value items by their refined output instead of the item.
  Needs `typeMaterials.jsonl` from the SDE, which is not imported yet.
- [ ] **Materials** - flatten blueprint material requirements.
- [ ] **Slots** - how many manufacturing, research, order and contract slots are in
  use against the character's skill-derived maximum.
- [ ] **Mining ledger** - `/characters/{id}/mining` and the corp observers.
- [ ] **Routing** - jump planner between the systems holding your stuff.
- [ ] **Skills, standings, loyalty points, agents** - straightforward ESI pulls,
  low priority for an asset tool.

**Not planned**

## Not planned

- Price history charts per item. Adam4EVE and EVE Ref do this better than a
  desktop app can.
- In-game overlay, or anything that reads the client.
- Structure rigs and modules, and access list contents. Not a judgement call:
  ESI has no endpoint for either.

## Shipped

| Multi-character assets | One table, groupable with live rollup headers, filtered through the omnibox grammar (`loc:` `owner:` `is:unpriced` `val:>10m` …) |
| Corp assets | Per-character opt-in, needs the in-game role |
| Container flattening | Ship-in-ship-in-can resolves to the station |
| Player structures | Named via `/universe/structures`; no-access ones are marked and not re-asked |
| Rollup rail | Per-label stacks / m³ / ISK with value bars at six grouping levels; click-to-filter, star pins, "where is it" flip when hunting an item |
| Estate strip | Whole-estate net worth, assets, liquid ISK, volume, unpriced count and a value map of top locations, above the Assets table |
| Row inspector | Both price bases, source and quote age, where-else, single-type price refresh, manual price pinning |
| Saved views & pins | `Ctrl+1-9` saves filter + grouping + rail level to the database; rail pins persist per level |
| Manual prices | Pin a price per type; the repricer never overwrites a pin |
| Net worth tracker | Per character and corp, snapshot per sync, six-way split, charted at both Jita buy and Jita sell |
| Pricing | Jita 4-4 buy and sell; capitals from public contract average where the book is thin |
| Wallets, market orders, contracts, industry jobs, blueprints | Stored; orders/contracts/jobs feed net worth |
| Wallet journal | Append-only, filterable by owner, date and ref type, counterparties named |
| Transactions | Append-only, buy/sell filter, bought/sold/net over the current filter |
| CSV export | Whatever the current filter shows |
| Stockpile | Target quantities per item, scoped by owner and location; opt-in held sources; shortfall in units, ISK and m3; doctrine multiplier |
| Treemap | Value as area, grouped six ways, click through to a filter chip |
| Structures tab | Corp citadels with fuel, reinforcement timers and moon drills |
| Unanchor tracking | ESI never announces an unanchor, the structure just stops being reported, so sync records `gone_at` rather than deleting the row (assets still there need its name). A "Show unanchored" tick box brings them back |
| In-app updates | Help -> Check for updates; folder swap via a static Rust helper |
| Headless sync | `evebooty --sync` for schedulers |

## Known rough edges

- Contract pricing makes one ESI call per candidate contract. On a wide region
  scan that's slow. Worth caching contract IDs between runs and only fetching
  items for ones not seen before.
- Net worth counts in-progress manufacturing at output value. The items don't
  exist yet, so that number is optimistic by whatever your build failure rate is.
  Excluded jobs would make the chart dip every time you start a build, which is
  worse.
- Assets in a corp hangar are attributed to the corp, not to any character. If two
  of your characters are in the same corp, don't tick Corp data on both - you'll
  pull the same assets twice under one owner and the second pull just overwrites
  the first, which is harmless but wasteful.
- No incremental sync for assets, orders, contracts and jobs. Every run replaces
  all rows for that owner. Fine at a few thousand stacks; worth revisiting past
  ~100k. Journal and transactions are already incremental.
- Capitals with no market and no contract sighting fall back to the SDE base
  price, which is a CCP placeholder (60b for every titan, regardless of hull).
  Right order of magnitude, wrong number. Widening the contract scan to more
  regions helps; caching contract sightings across runs would help more.
- Old net worth snapshots taken before the buy/sell split keep their sell figures
  and show zero on the buy side. The migration will not invent a spread for
  history it cannot reconstruct, so the buy line starts at your first sync after
  upgrading.
