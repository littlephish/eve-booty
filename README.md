# EVE Booty

Desktop asset manager for EVE Online — all your plunder in one place. Point it at
your characters, hit sync, and get one searchable table of everything you own
across every hangar, ship, can and corp division, plus a chart of what each
character is worth over time.

Python 3.11+, PySide6, SQLite. Runs from source with `uv`, ships as a single
Windows exe built with Nuitka.

![Assets tab](docs/screenshot-assets.png)

## What it does

- One asset table across all your characters and their corps, filtered through a
  single omnibox with a typed grammar (see below) and groupable by location,
  system, region, owner, category or group — collapsible headers carry live
  stacks / m³ / ISK rollups for whatever the filter leaves
- Container trees flattened, so a module in a can in a ship in a station still
  reports the station
- Items sitting in Asset Safety show up labelled as such rather than as an
  unresolvable location -- ESI's asset endpoint gives Asset Safety a fixed
  location id (2004) that is not a real station, system or structure, so it
  needs its own case rather than an ESI lookup that would just 404
- Right-click a ship and View fit to see everything on it: fitted modules and
  loaded charges (paired up even though ESI reports both under the same slot),
  drones, fighters, cargo, fleet hangar and every specialized hold
- Copy a fit straight into Pyfa as ESI fitting JSON, or as EFT text for a
  forum post or the in-game fitting window
- A rollup rail beside the table: every location (or system, region, owner,
  category, group) with its stacks, volume, value and a proportional value bar.
  One click adds the label as a filter chip; stars pin favourites to the top,
  and unresolved "Unknown location" entries fold into one row. When you search
  for an item by name the rail flips to "where is it" — per-location quantities
  of the matched items
- A Treemap tab showing the same rollups as area, so "most of my ISK is in
  implants" is a glance rather than a read. Group by item group, system,
  region, owner or category; size by Jita buy or sell; right-click a tile to
  send it to the Assets tab as a filter chip
- An estate strip above the table — net worth, assets, liquid ISK, volume,
  unpriced count and a one-row value map of your top locations — always
  whole-estate, never faceted by the filters below it
- A row inspector (`Enter`): both price bases, price source and quote age,
  volume, location and slot, plus Where else? / Refresh price / Pin price…
- Honest price flagging: unpriced and manually pinned stacks are badged in the
  value cells, quotes older than 48 h carry their age, and a manual price is
  never overwritten by a reprice until you unpin it
- Net worth per character, snapshotted on every sync and charted over time at
  both Jita buy and Jita sell, split into assets / wallet / sell orders / buy
  escrow / contracts / in-production
- Wallet journal and market transactions, filterable by owner, date range, ref
  type and buy/sell, with counterparty names resolved
- CSV export of whatever the current filter shows
- A `--sync` CLI mode so you can put it on a scheduler and just look at the chart

## The Assets tab

Everything that narrows the table is a token in one search field. Bare words
match item and custom names; everything else is a `prefix:value` chip:

```
loc:"Jita IV - Moon 4"     sys:Jita      region:"The Forge"
owner:Main                 cat:Ship      group:Battleship      meta:"Tech II"
is:fitted  is:safety  is:unpriced  is:bpc
val:>10m   val:<1b
-owner:Alt                 (a leading - negates any token)
```

Typed tokens become deletable chips as you commit them, each washed in its
kind's own colour (negations always in red); rail rows, value-map segments
and the cell context menu all add chips the same way, and `Ctrl+F` (or the +
button) opens a two-stage chip builder that first lists what can be filtered
and then completes real values. Multiple chips of one kind OR together,
different kinds AND. `Esc` peels the newest layer of state; Clear all drops
the lot.

Selection maths live in the footer (units · m³ · ISK for the selection, or the
whole filtered set when nothing is selected), with Copy list (EVE multibuy
text) and Appraise (copies the same text and opens
[Janice](https://janice.e-351.com/)).

Keyboard: `/` omnibox · `Ctrl+F` build a filter chip · `j`/`k` rows ·
`space` select · `f` filter to the focused cell · `x` exclude it · `w` where
else is this item · `Enter` inspector · `g` cycle group-by · `1`–`9` recall
a saved view, `Ctrl+1`–`9` save one (filter, grouping and rail level
together) · `?` the full key map.

Pins and saved views persist in the database next to the assets they
describe, so a copied database carries them along.

## Pricing

Every item carries two prices, both from
[Fuzzwork's](https://market.fuzzwork.co.uk/api/) Jita 4-4 aggregates in a single
request:

- **Jita buy** — the highest bid. What you get dumping the lot into standing
  orders right now.
- **Jita sell** — the lowest ask. What you get if you list it and wait.

Net worth is tracked on both, so the gap between the two lines on the chart is
what being in a hurry costs you. Wallet balance, sell orders and buy escrow are
already ISK, so they do not move with the basis.

### Capitals

Titans, supers, carriers, FAXes, dreads, lancers, capital industrials, freighters
and jump freighters mostly change hands by contract. Where Jita has a real
two-sided book we use it. Where it does not, the app scans public item-exchange
contracts across The Forge, Domain, Sinq Laison and Heimatar, keeps the ones that
are a single hull at a fixed price, drops anything more than 1.5 IQRs outside the
quartiles, and averages the rest. A contract price is one number rather than a
bid and an ask, so it fills both columns.

One thing worth knowing, because it is the difference between a correct number
and a catastrophically wrong one. Sampled on 2026-08-06, 42 of the 60 capital
hulls had a **one-sided** Jita book, and every titan sat on a lowball bid with no
asks at all — an Avatar bid at 1,324,000 ISK against a contract average near 170
billion. Mirroring that bid across to the sell side, which is the sensible thing
to do for a thin T2 module, would price a titan like a shuttle. So for
contract-priced groups a one-sided book is discarded outright: contracts decide,
and failing that the SDE base price, flagged `base_price` so you know it is a
placeholder rather than a market.

The group list, the scanned regions and both prefilters are editable in Settings,
along with a switch to make contract averages beat the market unconditionally.

## Wallet history outlives ESI

ESI serves roughly the last 30 days of journal and at most 2500 transactions.
Those two tables are the only ones the app appends to rather than replaces, so a
few months in, the local copy holds history CCP will no longer give you. Nothing
is ever deleted on sync, and re-syncing the same rows is a no-op.

The transactions endpoint has no page parameter — you rewind through it with
`from_id`. A routine sync stops after one call, because everything in the first
batch is already stored.

## ESI scopes

The full list lives in `SCOPES` in `src/evasset/config.py`. What each one buys:

| Scope | Feeds |
| --- | --- |
| `publicData` | Character and corp public info (names, tickers) |
| `esi-assets.read_assets.v1` | Character assets, plus container names |
| `esi-assets.read_corporation_assets.v1` | Corp hangars — needs Director in-game |
| `esi-wallet.read_character_wallet.v1` | Balance, journal and transactions |
| `esi-wallet.read_corporation_wallets.v1` | Corp wallet divisions — needs Accountant or Director |
| `esi-markets.read_character_orders.v1` | Sell order value and buy escrow |
| `esi-markets.read_corporation_orders.v1` | The same for the corp |
| `esi-contracts.read_character_contracts.v1` | Items tied up in your contracts |
| `esi-contracts.read_corporation_contracts.v1` | The same for the corp |
| `esi-industry.read_character_jobs.v1` | In-production output value |
| `esi-industry.read_corporation_jobs.v1` | The same for the corp |
| `esi-characters.read_blueprints.v1` | Blueprint ME/TE/runs |
| ~~`esi-corporations.read_blueprints.v1`~~ | Not requested — CCP rejects it, see below |
| ~~`esi-corporations.read_divisions.v1`~~ | Not requested — CCP rejects it, see below |
| `esi-corporations.read_structures.v1` | Names of structures your corp owns |
| `esi-universe.read_structures.v1` | Names of player structures you can dock at |
| `esi-clones.read_clones.v1`, `esi-clones.read_implants.v1` | Reserved for jump clones and implants; requested but not used yet |

Public endpoints used with no token at all: `/contracts/public/{region}` and
`/contracts/public/items/{id}` for capital pricing, `/universe/names` for
counterparties, Fuzzwork for Jita aggregates, and CCP's image service
(`images.evetech.net`) for the type icons in the fit dialog, fetched on
demand and cached on disk.

Note that `esi-wallet.read_corporation_wallet.v1` (singular) is not a scope ESI
declares any more. The one you want is `esi-wallet.read_corporation_wallets.v1`
(plural).

### Two scopes are confirmed broken on CCP's side

`esi-corporations.read_blueprints.v1` and `esi-corporations.read_divisions.v1`
are declared valid by ESI's own OpenAPI spec, map to real endpoints, generate
into every third-party SDK built from that spec, and can be checked in CCP's
application registration UI — but EVE SSO's `/v2/oauth/authorize` rejects both
with `invalid_scope` regardless. Confirmed against a live application on
2026-08-06 and 2026-08-07. This app does not request either by default; both
are commented out of `SCOPES` in `src/evasset/config.py` with the confirmation
dates.

This matters more than a normal missing-scope situation because of two things
stacking together:

1. OAuth2 fails the *entire* authorization request over a single bad scope
   ([RFC 6749 §4.1.2.1](https://www.rfc-editor.org/rfc/rfc6749#section-4.1.2.1))
   — a bad scope does not get silently dropped, it blocks every other scope in
   the same request.
2. `invalid_scope` is a pre-redirect rejection, so EVE SSO shows its own inline
   error page instead of ever calling back to this app's loopback listener.
   That means the failure is invisible to us — the login just hangs until our
   own timeout, which now explains this possibility in its error message.

Two for two on corp-specific scopes so far is a pattern worth taking seriously,
not a pair of coincidences. If you hit `invalid_scope` on a corp-scoped entry
not listed above, that is a new data point, not a surprise — please remove it
from your application and let it be known so `SCOPES` and this list can be
updated.

Cost of leaving these two out: corp-owned blueprints (ME/TE/runs on BPOs sitting
in the corp hangar) are never synced, and wallet journal division numbers show
as `1`, `2` etc. rather than their in-game names — neither is currently rendered
by the UI regardless, so there is no visible loss today. Corp assets, wallet
balance, orders, contracts, jobs and structures do not depend on either scope.

The same thing happened to `esi-universe.read_structures.v1` for a while (see
[esi-issues #1030](https://github.com/esi/esi-issues/issues/1030) and
[#1302](https://github.com/esi/esi-issues/issues/1302), both eventually fixed
after being reported) — worth filing the same way if you want these two fixed
for good.

Everything degrades gracefully. A scope you did not grant, or a corp role you do
not hold, skips that one pull and writes the reason into the Last result column
in the Characters dialog instead of failing the sync.

## Setup

You need your own ESI application. Nobody's client ID is shared here, and CCP
would rather you didn't.

1. Go to https://developers.eveonline.com/applications, create an application,
   pick Authentication & API Access, and select the scopes listed in
   `src/evasset/config.py` (`SCOPES`).
2. Set the callback URL to `http://localhost:8629/callback`. If 8629 is taken on
   your machine, change the port in Settings and update it on CCP's side to match.
   8629 was picked to stay clear of other EVE tools that bind a loopback
   callback: jEveAssets uses 2221, littlephish/eve-strait uses 8635. If the port
   is busy at login time the app says so by name rather than throwing.
3. Copy the client ID. You do not need the secret; the app uses PKCE by default.

Then:

```bash
uv sync
uv run evebooty
```

First launch asks for the client ID, then downloads and imports the Static Data
Export (about 95 MB, roughly 3 seconds to import). Open Characters…, add a
character, and hit Sync all.

To pull a character's corp hangars and wallets, tick Corp data next to them in the
Characters dialog. That needs the matching in-game role — Director, or Accountant
for the wallet. Without it those calls come back 403 and the dialog says so in the
Last result column rather than failing the whole sync.

### Building the exe

```bash
uv run python build.py            # dist/evebooty.dist/
uv run python build.py --onefile  # one file, slower cold start
```

The SDE is not bundled. The app checks CCP's build number on demand and only
re-downloads when it moves, which beats shipping a 95 MB blob that goes stale on
the next patch day.

### Releases and in-app updates

CI runs ruff and the test suite on every push, and compile-checks the Rust
updater on Windows. Releases are cut by tag:

```bash
git tag v1.0.0
git push origin v1.0.0
```

That builds a Nuitka `--standalone` **program folder**, drops `update.exe` in
beside the app, zips it as `EVEBooty-<version>-win64.zip` and publishes a
GitHub release. Deliberately not a onefile exe: onefile unpacks itself to
`%TEMP%` and runs from there, which Defender and CrowdStrike both score as
dropper behaviour, and the folder layout is what makes an in-place update
possible at all.

Help → Check for updates asks GitHub for the newest release, downloads the zip
and hands the swap to `updater/` — a small statically-linked Rust binary. A
running process cannot overwrite its own directory on Windows, so the app
copies `update.exe` to a temp folder and runs it from there; that is what lets
the new build replace the whole install, including the installed `update.exe`.
It is not a PowerShell script because a machine ExecutionPolicy of `AllSigned`
or `Restricted` silently refuses to run an unsigned `.ps1`, and the update
would just never happen.

The menu item is disabled when running from source: there is nothing there an
updater should be mirroring over. `updater/` is MIT-licensed and shared across
projects; the rest of this repo is not.

### Headless

```bash
uv run evebooty --update-sde
uv run evebooty --sync
```

`--sync` refreshes every enabled character, reprices, writes a snapshot and prints
the per-owner totals. It needs tokens already stored, so add characters in the GUI
once first.

## Where things live

| Path | What |
| --- | --- |
| `src/evasset/sde.py` | SDE download and import; derives NPC station names from CCP's celestial naming rules |
| `src/evasset/esi/auth.py` | SSO PKCE flow, loopback callback, JWT validation, token storage |
| `src/evasset/esi/client.py` | ESI client: error-limit tracking, retries, pagination |
| `src/evasset/esi/sync.py` | Character and corp pulls, container-tree flattening |
| `src/evasset/pricing.py` | Jita sell + public-contract averaging |
| `src/evasset/networth.py` | Snapshot maths and history |
| `src/evasset/fitting.py` | Groups a ship's contents into slots/holds, and exports ESI-fitting JSON / EFT |
| `src/evasset/treemap.py` | Squarified treemap layout for the Treemap tab |
| `src/evasset/ui/` | PySide6 widgets |
| `scripts/seed_demo.py` | Fills a throwaway database with plausible data, no EVE account needed |
| `scripts/make_icon.py` | Draws the app icon and packs the multi-size `.ico` |

The on-disk name is still `evasset` — the data directory, the `EVASSET_*`
environment variables and the keyring entry holding each character's refresh
token all kept their original identifier, because renaming them would strand an
existing install's database and saved logins. Only the name you read changed.

Data lives in your platform's app data directory: `evasset.sqlite` for everything,
`settings.json` for config. Refresh tokens go to the OS credential store (Windows
Credential Manager, macOS Keychain, Secret Service on Linux). If there's no
credential store — a headless Linux box, usually — they fall back to a 0600
`tokens.json` and the app warns you. Treat that file like a password.

## Versioning against ESI

ESI moved from `/v4/`-style path segments to an `X-Compatibility-Date` header.
`COMPATIBILITY_DATE` in `config.py` pins the response shape the parsers were
written against. Bump it after reading CCP's changelog, not before.

## Tests

```bash
uv run pytest
```

No network needed, and Qt runs offscreen where a widget is involved. They cover
the value maths, the omnibox grammar and its SQL (quoting, negation, `val:`
comparisons, saved-view round-trips), the rail rollup and facet queries, the
grouped tree model, the Assets tab wired end to end, the treemap layout (that
it tiles exactly, without overlaps, in proportion to value), the container-tree
resolver (including a cyclic-parent case that would otherwise hang), the
contract outlier filter and its packaged-volume floor, the `from_id` walk-back
for transactions, append-only journal behaviour, the `/universe/names`
batch-splitting retry, and every text colour the UI draws, measured against
WCAG AA in both themes.

To poke at the UI without an EVE account:

```bash
EVASSET_DATA_DIR=/tmp/demo uv run python scripts/seed_demo.py
EVASSET_DATA_DIR=/tmp/demo uv run evebooty
```

## Relationship to jEveAssets

This was written because jEveAssets was believed to be gone. It isn't — the
[repo](https://github.com/GoldenGnu/jeveassets) had commits as recently as
2026-07-31 and is not archived. If you want the mature tool with fifteen years of
features, use that one. This exists for people who want a small Python codebase
they can read in an afternoon and a net worth chart that isn't an afterthought.
See [ROADMAP.md](ROADMAP.md) for what jEveAssets does that this doesn't.

## Licence and CCP's rules

MIT, see [LICENSE](LICENSE).

EVE Online and all related material are property of CCP hf. This is a third-party
tool built on CCP's public ESI API and Static Data Export under the
[Developer Licence Agreement](https://developers.eveonline.com/license-agreement).
It is not endorsed by CCP. Market aggregates come from Fuzzwork Enterprises.
