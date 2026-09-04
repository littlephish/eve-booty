# Roadmap

## What's in v0.1

| Feature | Notes |
| --- | --- |
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
| Structures tab | Corp citadels with fuel, reinforcement timers and moon drills; unanchored ones detected and hidden |
| In-app updates | Help -> Check for updates; folder swap via a static Rust helper |
| Headless sync | `evebooty --sync` for schedulers |

## What jEveAssets has that this doesn't

Taken from the tab list in
[GoldenGnu/jeveassets](https://github.com/GoldenGnu/jeveassets/tree/main/src/main/java/net/nikr/eve/jeveasset/gui/tabs).
Roughly in the order I'd add them.

**Next up**

- **Tree view** - assets as an expandable location → container → item tree instead
  of a flat table. The data model already carries the parent links.
- **Item database** - browse and search every published type in the SDE, not just
  what you own. Everything needed is already imported.
- ~~**Stockpile**~~ - done. Target quantities per item, scoped to an owner and a
  station/system/region, with held / target / shortfall, the shortfall costed in
  ISK and m3, and a doctrine multiplier. Narrower than jEveAssets' arbitrary
  filter tree by choice; see the module docstring for why.
- ~~**Values / summary tab**~~ - done, as the estate strip on the Assets tab
  (net worth, assets, liquid ISK, volume, unpriced count, value map).
- **Realised profit per item** - the Transactions tab shows cash in against cash
  out, which is not profit. Matching a sale back to the lot it came from needs
  FIFO inventory accounting over the transaction history.

**Later**

- **Reprocessing** - value items by their refined output instead of the item.
  Needs `typeMaterials.jsonl` from the SDE, which is not imported yet.
- **Materials** - flatten blueprint material requirements.
- **Slots** - how many manufacturing, research, order and contract slots are in
  use against the character's skill-derived maximum.
- **Mining ledger** - `/characters/{id}/mining` and the corp observers.
- **Routing** - jump planner between the systems holding your stuff.
- **Skills, standings, loyalty points, agents** - straightforward ESI pulls,
  low priority for an asset tool.

**Not planned**

- Price history charts per item. Adam4EVE and EVE Ref already do this better than
  a desktop app can.
- In-game overlay or anything that reads the client.

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
