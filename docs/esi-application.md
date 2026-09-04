# ESI application registration

What to put in the form at <https://developers.eveonline.com/applications>,
and the record of what the shared **EVE Booty** application is registered
with. Registering your own works the same way — see the end.

## The form

| Field | Value |
| --- | --- |
| **Name** | `EVE Booty` |
| **Connection Type** | Authentication & API Access |
| **Callback URL** | `http://localhost:8629/callback` — [read this first](#callback-url) |
| **Permissions** | [the 16 scopes](#scopes-to-tick) |
| **Description** | [suggested text](#description) |

Pick **Authentication & API Access**, not Authentication Only. Authentication
Only returns an identity and nothing else, and every feature here is an
authenticated ESI call.

Leave the client secret unused. This app authenticates with PKCE, which CCP
describes as "mostly aimed at mobile and desktop applications that cannot
securely store the client secret" and which needs only the client id at the
token endpoint. A secret shipped inside a downloadable binary can be extracted
from it, which is the whole problem PKCE removes. See `AGENTS.md` §3.

### Description

CCP staff read these, so say what the app touches and where the data goes:

> Cross-character asset inventory and net worth tracking for EVE Online. Reads
> a player's own assets, wallet, market orders, contracts, industry jobs and
> blueprints across their characters and corporations, and presents them as one
> searchable inventory valued against Jita market data. Desktop application;
> everything is stored locally on the player's own machine and nothing is
> uploaded.

## Scopes to tick

```
publicData
esi-assets.read_assets.v1
esi-assets.read_corporation_assets.v1
esi-wallet.read_character_wallet.v1
esi-wallet.read_corporation_wallets.v1
esi-markets.read_character_orders.v1
esi-markets.read_corporation_orders.v1
esi-contracts.read_character_contracts.v1
esi-contracts.read_corporation_contracts.v1
esi-industry.read_character_jobs.v1
esi-industry.read_corporation_jobs.v1
esi-characters.read_blueprints.v1
esi-corporations.read_structures.v1
esi-universe.read_structures.v1
esi-clones.read_clones.v1
esi-clones.read_implants.v1
```

Sixteen scopes, and they must match `SCOPES` in `src/evasset/config.py`
exactly. Re-derive the list rather than retyping it:

```bash
uv run python -c "from evasset.config import SCOPES; print('\n'.join(SCOPES))"
```

**The registration is a ceiling, not a request.** An application can only ask
for scopes it was registered with, so a scope added to `SCOPES` in code but
never ticked here fails at login for everyone using this application. Adding
one means changing both.

The README's ESI scopes table says what each scope buys.

### Two scopes must not be ticked

```
esi-corporations.read_blueprints.v1
esi-corporations.read_divisions.v1
```

The form offers both. ESI's own OpenAPI spec declares both. EVE SSO rejects
both with `invalid_scope` anyway — confirmed against a live application on
2026-08-06 and 2026-08-07.

Ticking either breaks every login, because two behaviours compound. OAuth2
fails the *entire* authorization request over a single bad scope
([RFC 6749 §4.1.2.1](https://www.rfc-editor.org/rfc/rfc6749#section-4.1.2.1)),
and `invalid_scope` is rejected before the redirect, so SSO renders its own
error page and never calls back. Nothing reaches the app, so the login simply
hangs until it times out, with no clue which scope caused it.

Treat any new corp-scoped entry as unverified until someone completes a real
login with it.

## Callback URL

SSO redirects only to the exact URL registered here — no wildcards, no
flexibility on port or path. It cannot be changed afterwards without
invalidating every login that depends on it, so both of the following are
worth settling before a release exists.

### `localhost` or `127.0.0.1`

The app currently builds `http://localhost:8629/callback`
(`Settings.redirect_uri`), while the callback server binds the IPv4 literal:

```python
http.server.HTTPServer(("127.0.0.1", settings.callback_port), _CallbackHandler)
```

On Windows, `localhost` resolves to both `::1` and `127.0.0.1`, and IPv6 wins.
A browser that tries `[::1]:8629` finds nothing listening there. It works today
only because browsers fall back to IPv4 — that is grace, not design.
[RFC 8252 §7.3](https://www.rfc-editor.org/rfc/rfc8252#section-7.3) recommends
the IP literal for native apps for exactly this reason.

**Register `http://127.0.0.1:8629/callback`** and point `Settings.redirect_uri`
at the same string, so the redirect target and the listening socket cannot
disagree. Changing this now costs nothing. Changing it later invalidates
everyone's registration.

Whichever you pick, the registered URL and `redirect_uri` must be identical
down to the character.

### A shared client id fixes the port for everyone

The registered callback names one port, so every user of the shared
application has to bind **8629** locally. It was chosen to stay clear of the
other EVE tools that run a loopback callback:

| Port | Tool |
| --- | --- |
| 2221 | jEveAssets |
| 8629 | **EVE Booty** |
| 8635 | littlephish/eve-strait |

Anyone who already has 8629 in use cannot move it, because they cannot edit an
application they do not own. Their way out is to register their own and set
both the port and the client id in Settings. The app detects a busy port at
login and names the likely culprit instead of throwing.

If the form accepts more than one callback URL, register a couple of
alternates — 8630 and 8631 — so the app can try a second port before sending
anyone off to make their own application. CCP's public documentation does not
say whether multiple are allowed, so check the form.

## After registering

Put the client id in `DEFAULT_CLIENT_ID` in `src/evasset/config.py`. Under
PKCE it is a public identifier, so committing it is the intent rather than a
leak (`AGENTS.md` §3). The secret stays unused and uncommitted.

Settings → ESI application overrides it at any time, and nothing should ever
force a user to.

## Registering your own instead

Worth doing if 8629 is taken on your machine, if you would rather your traffic
was not pooled with everyone else's, or simply if you prefer your own id.

1. Create an application at <https://developers.eveonline.com/applications>.
2. Set Connection Type to **Authentication & API Access**.
3. Tick the sixteen scopes above, and neither of the two rejected ones.
4. Set the callback to `http://localhost:8629/callback`, or another port if
   8629 is busy — then set the matching port in Settings.
5. Paste the client id into **Settings → ESI application**. Leave the secret
   blank; the app uses PKCE.
