# AGENTS.md

Instructions for AI agents and contributors working in this repository.

This app reads a real EVE Online account: assets, wallet balances, journal
entries, market transactions, contracts, industry jobs and the stations and
structures all of it sits in. That is personal financial-shaped data about a
real person, and this repository is going public. The rules below exist
because that combination has already gone wrong once here, not as a
precaution against something hypothetical.

---

## 1. Never publish a screenshot of a live account

**Every screenshot committed, attached to a PR, pasted into an issue, or put
in a commit message must come from seeded demo data.**

Generate it:

```bash
EVEBOOTY_DATA_DIR=/tmp/demo uv run python scripts/seed_demo.py
EVEBOOTY_DATA_DIR=/tmp/demo uv run evebooty
```

`scripts/seed_demo.py` exists for exactly this. It creates two characters and
a corp with obviously fake names (`Vex Aldaran`, `Alt Trader`, `Test
Holdings`), NPC stations, and prices sampled once and hardcoded. Anything it
produces is safe to publish because anyone can reproduce it byte-for-byte from
the committed seeder.

`EVEBOOTY_DATA_DIR` is what makes this safe: it points the whole app at a
scratch database, so the real one is never opened and cannot leak into the
frame. Set it. Do not screenshot the app running against the default data
directory and then crop.

### What this covers

Not just `README.md`. A screenshot is exposed the moment it reaches GitHub, in
any of these:

- the README and anything under `docs/`
- **pull request descriptions and comments** — see §4, these are the worst case
- issue bodies and comments
- commit messages

### Why this rule is absolute

Five screenshots were committed to a PR branch to illustrate UI changes. They
showed a real character name, a real alt name, a real net worth figure, and
the specific stations across six regions where that account kept its assets.

Removing them cost: a `git-filter-repo` rewrite of all 27 commits, a
force-push across all six branches, and ultimately **migrating the entire
project to a new repository** — because the images survived in GitHub's
`refs/pull/*` namespace, which no git operation can reach (§4).

The fix for one careless screenshot was abandoning the repository. Generating
demo data takes about five seconds.

---

## 2. Never commit real account data in any form

No real values from a live account go into this repository, in **any** file
type. Specifically:

| Never commit | Use instead |
|---|---|
| Real character or corp names | `Main`, `Alt`, `Test Pilot`, `Test Corp` |
| Real wallet balances, journal rows, transactions | Round synthetic figures |
| Real asset quantities or ISK values | Values invented for the test |
| Real station or structure names revealing where someone keeps things | NPC stations (`Jita IV - Moon 4`), invented citadel names |
| Real net worth or estate totals | Invented totals |
| A real `client_id` or `client_secret` | Nothing — these come from Settings at runtime |

This applies to **test fixtures, planning and research documents, sample CSV
or JSON, docstrings, commit messages and issue text** — not only screenshots.
A station list in a markdown table leaks the same information as a screenshot
of one.

Two PRs in this project's history existed solely to undo violations of this
rule: one replaced real station names in a wrap test, another generalised real
estate figures in a planning document. Both were caught late. Write it
synthetic the first time.

If you need realistic-looking data, take it from `scripts/seed_demo.py` or
invent it. Never paste from a running instance.

---

## 3. Never commit credentials or the local database

These are already in `.gitignore`. **Do not weaken those rules**, and do not
`git add -f` past them:

```
settings.json      contains the user's ESI client_id (and secret, if set)
*.sqlite           the entire local database: assets, wallet, journal
tokens.json        SSO refresh tokens, when no OS credential store exists
.env
```

Additional rules:

- **The `client_id` is public; the `client_secret` is not.** The app ships a
  pre-configured `client_id` and uses PKCE, which is the SSO method CCP
  prefers for desktop apps. That is deliberate and safe: under PKCE
  ([RFC 7636](https://www.rfc-editor.org/rfc/rfc7636)) the client id is a
  public identifier, not a credential — the per-login code verifier is what
  proves the request is genuine, so knowing the id gets an attacker nothing.
  Users may replace it with their own application at any time in Settings, and
  nothing should ever require them to.

- **Never ship, hardcode or commit a `client_secret`.** Anything embedded in a
  distributed binary is extractable, and a leaked secret would let anyone
  impersonate the application. `client_secret` stays empty by default and PKCE
  stays the default flow. If a change would make a secret *required* rather
  than optional, it is the wrong change.

- **Treat the shared `client_id` as a shared resource.** All users of the
  default id appear to CCP as one application, so abusive traffic from this
  tool degrades every user at once. That is a reason to keep the error-limit
  handling honest (§6), and the reason swapping in a personal application must
  always remain possible.
- **Never log, print or write a token.** Refresh tokens live in the OS
  credential store (`esi/auth.py`). The `tokens.json` fallback is `0600` and
  the app warns when it is used — treat that file as a password.
- **Never widen the ESI scope list** in `config.py` without a reason recorded
  in the comment block there. Two corp scopes are deliberately excluded
  because EVE SSO rejects them, and one bad scope fails the *entire*
  authorization request.

---

## 4. If account data does reach the repository

**Deleting the file and committing does not remove it.** The blob stays in
history and remains reachable by SHA.

Do this instead:

1. **Stop. Tell the human immediately.** Do not quietly rewrite history.
2. Do not force-push anything unilaterally. History rewrites are the human's
   call — they break every clone and every open PR.
3. Establish the real scope before proposing a fix. Check **every branch**,
   including ones not checked out locally:
   ```bash
   git clone --mirror <repo> audit.git
   cd audit.git
   git rev-list --all --objects | grep -i '\.png'
   ```
   A leak found on `main` is not evidence that `main` is the only place it is.

### The thing that makes this expensive

`git-filter-repo` plus a force-push cleans **branches**. It cannot clean
`refs/pull/*`.

GitHub freezes a snapshot of every pull request under `refs/pull/N/head`.
That ref survives merging the PR, closing it, deleting the source branch, and
force-pushing rewritten history over the branch. Pushing to it fails:

```
! [remote rejected] refs/pull/4/head (deny updating a hidden ref)
```

Only GitHub Support can remove it — or deleting the repository, since the refs
die with it. This is why a screenshot in a **PR description or a PR branch** is
far more expensive than the same mistake on `main`, and why §1 treats PRs as
the primary risk rather than an afterthought.

---

## 5. Do not destroy the user's data

The local database holds things that **cannot be re-fetched**. Losing them is
not a sync away from being fixed.

### `wallet_journal` and `wallet_transactions` are append-only

ESI serves roughly the last 30 days of journal and at most 2500 transactions.
These two tables are the only ones the app appends to rather than replaces, so
a long-running install holds history **CCP will no longer give you**.

- Never add a `DELETE` against either table.
- Never "clean up" or dedupe them destructively. Re-syncing the same rows is
  already a no-op by design.
- A migration that rebuilds either table must copy every existing row.

Everything else (`assets`, `market_orders`, `contracts`, `industry_jobs`,
`blueprints`) is deliberately delete-then-replace per owner on each sync. That
asymmetry is intentional — do not "make it consistent" by converting the
append-only tables to replace.

### Migrations run against real databases

`db.init()` calls `migrate()` on the user's live database at startup, and
`SCHEMA_VERSION` is currently `4`. One migration path rebuilds a table via
`ALTER TABLE ... RENAME` + `CREATE` + copy + `DROP TABLE` (`db.py`).

If you touch schema:

- Bump `SCHEMA_VERSION` and add a forward migration. Never edit an existing
  migration that has shipped — someone has already run it.
- Test against a **copy** of a populated database, not just a fresh one. A
  migration that works on an empty schema and drops rows on a real one is the
  failure mode here.
- Preserve data. `DROP TABLE` is only acceptable after the rows are copied.

### Other things that are not yours to discard

- **Structures that disappear from ESI are marked, not deleted**
  (`structures.gone_at`). Assets still reference them for name resolution;
  deleting the row orphans real data. See `structures_view.py`.
- **Manually pinned prices are never overwritten by a reprice.** That is a
  user decision, not stale data.
- **Saved views and rail pins live in the database** next to the assets they
  describe. Schema changes must carry them.

---

## 6. ESI: you are a guest on CCP's API

This app talks to CCP's servers using a real player's token. Behave
accordingly.

- **Never remove or genericise the User-Agent.** `config.user_agent()` is how
  CCP identifies this tool and finds a contact before throttling or blocking
  it. It must carry a real version and a resolvable URL.
- **Never bypass the error-limit tracking** in `esi/client.py`. Do not add
  retry loops around it or raise concurrency to "speed up" a sync.
- **`COMPATIBILITY_DATE` in `config.py` pins the response shape** the parsers
  were written against. Bump it only after reading CCP's changelog, and update
  the parsers in the same change.
- **Do not add ESI scopes casually.** OAuth2 fails the *entire* authorization
  request over one bad scope, and EVE SSO rejects it before redirecting, so
  the login just hangs with no signal about which scope caused it. Two corp
  scopes are excluded for exactly this reason, with confirmation dates in the
  `SCOPES` comment. A new scope is unverified until someone completes a real
  login with it.
- **Tests must never hit the network.** The whole suite runs offline; keep it
  that way.

---

## 7. Project conventions worth not breaking

**The version comes from the git tag.** `src/evasset/_version.py` is rewritten
by `scripts/set_version.py` during the release build, and `pyproject.toml`,
the About box, the ESI User-Agent and the Windows exe resource all read from
it. Never hardcode a version anywhere, and never commit a release number into
`_version.py` — the committed value stays `0.0.0.dev0`, and a test enforces
that.

**`APP_NAME` moves three things at once.** It picks the data directory, the
cache directory and the keyring service holding every refresh token, so
renaming it strands the database and silently logs out every character. It was
changed from `evasset` to `eve-booty` only because both halves of the migration
exist: `config._adopt_legacy_dir` moves the folders, and
`auth._adopt_legacy_token` re-homes each token on first use. If you rename it
again, add the old name to `LEGACY_APP_NAMES` and check both still cover it.

**Before proposing anything is finished:**

```bash
uv run pytest                      # Qt tests run offscreen automatically
uv run ruff check src tests scripts
```

**Keep business logic Qt-free.** Every module in `src/evasset/` except
`__main__.py` imports no Qt; all widgets live in `src/evasset/ui/`. That is
what lets the value maths, the omnibox grammar, the treemap layout, the
pricing rules and the updater be tested without a display. Do not add a
module-level `PySide6` import outside `ui/`.

**`config.py` resolves paths at import time.** `tests/conftest.py` sets
`EVEBOOTY_DATA_DIR` *before* importing the package for that reason. Never
import `evasset` at the top of a test helper that runs before conftest, and
never make a test write to the default data directory.

**Packaging: `--standalone`, never `--onefile`.** A onefile build unpacks
itself into `%TEMP%` and executes from there, which Defender and CrowdStrike
both score as dropper behaviour. The program-folder layout is also what makes
the in-place updater work at all. The release zip filename must keep `win` in
it — `updater.pick_asset()` matches on that.

**Do not commit or push unless asked to.** Do not force-push to `main` without
explicit instruction.

---

## 8. Where the sensitive paths are

Useful when reasoning about what a change touches:

| Path | Holds |
|---|---|
| `src/evasset/config.py` | `client_id`, scopes, data directory locations |
| `src/evasset/esi/auth.py` | SSO flow, JWT validation, keyring token storage |
| `src/evasset/db.py` | schema for assets, wallet, journal, transactions |
| `<data dir>/evasset.sqlite` | the user's entire account history — never commit |
| `<data dir>/settings.json` | the user's ESI client credentials — never commit |
| `scripts/seed_demo.py` | the safe source of publishable data |
