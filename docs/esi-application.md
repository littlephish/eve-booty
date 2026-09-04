# ESI application registration

What to enter at <https://developers.eveonline.com/applications> for the
shared **EVE Booty** application, and the record of what it is registered
with. Anyone registering their own application (see the end) fills in the same
things.

---

## The form

| Field | Value |
| --- | --- |
| **Name** | `EVE Booty` |
| **Description** | see below |
| **Connection Type** | **Authentication & API Access** |
| **Callback URL** | `http://localhost:8629/callback` — but read [the callback section](#the-callback-url-is-the-decision-that-locks-in) first |
| **Permissions** | the 16 scopes below |

**Connection Type must be Authentication & API Access**, not "Authentication
Only". Authentication Only returns an identity and nothing else; every feature
in this app is an authenticated ESI call.

### Description

Suggested text — CCP reads these, so it says what data is used and why:

> Cross-character asset inventory and net worth tracking for EVE Online.
> Reads a player's own assets, wallet, market orders, contracts, industry jobs
> and blueprints across their characters and corporations, and presents them
> as one searchable inventory with valuations from Jita market data. Desktop
> application; data is stored locally on the player's own machine and is not
> uploaded anywhere.

### No client secret

Leave the secret unused. The app uses **PKCE**, which CCP describes as
"mostly aimed at mobile and desktop applications that cannot securely store
the client secret", and which needs only the client id at the token endpoint.
A secret embedded in a distributed binary would be extractable by anyone who
downloaded it — see `AGENTS.md` §3.

---

## Scopes to tick (16)

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

This list is generated from `SCOPES` in `src/evasset/config.py` and must match
it exactly. To re-derive it:

```bash
uv run python -c "from evasset.config import SCOPES; print('\n'.join(SCOPES))"
```

**The registration is the ceiling, not the request.** An application can only
ever request scopes it was registered with, so a scope added to `SCOPES` in
code without being ticked here fails at login for every user of this
application. Adding a scope means updating both.

What each one is for is tabulated in the README under "ESI scopes".

### Two scopes are deliberately NOT registered

Do **not** tick these, even though the form offers them:

```
esi-corporations.read_blueprints.v1      ← rejected by SSO
esi-corporations.read_divisions.v1       ← rejected by SSO
```

Both are declared valid by ESI's own OpenAPI spec and can be ticked in this
form, but EVE SSO's `/v2/oauth/authorize` rejects both with `invalid_scope`
regardless (confirmed against a live application on 2026-08-06 and
2026-08-07).

This matters more than a normal missing scope, because two things stack:
OAuth2 fails the **entire** authorization request over one bad scope
([RFC 6749 §4.1.2.1](https://www.rfc-editor.org/rfc/rfc6749#section-4.1.2.1)),
and `invalid_scope` is rejected *before* the redirect, so SSO shows its own
error page and never calls back to the app. The failure is therefore invisible
to us: the login just hangs until timeout with no signal about which scope
caused it.

Treat any corp-scoped addition as unverified until someone completes a real
login with it.

---

## The callback URL is the decision that locks in

EVE SSO redirects only to the exact URL registered here — no wildcards, no
path or port flexibility. Two consequences worth deciding deliberately, now,
before anyone is running a release.

### 1. `localhost` or `127.0.0.1`?

Right now the app builds `http://localhost:8629/callback`
(`Settings.redirect_uri`), but the callback server binds the IPv4 literal:

```python
http.server.HTTPServer(("127.0.0.1", settings.callback_port), _CallbackHandler)
```

On Windows, `localhost` resolves to **both** `::1` and `127.0.0.1`, usually
preferring IPv6. A browser that tries `[::1]:8629` first finds nothing
listening, because the server is IPv4-only. Browsers generally fall back to
IPv4 so this usually works, but it depends on browser behaviour rather than on
anything guaranteed.

[RFC 8252 §7.3](https://www.rfc-editor.org/rfc/rfc8252#section-7.3) — OAuth
for native apps — recommends the IP literal for exactly this reason.

**Recommendation: register `http://127.0.0.1:8629/callback`** and change
`Settings.redirect_uri` to match, so the redirect target and the listening
socket are the same thing by construction. Doing it now costs nothing; doing
it later invalidates every user's registration.

Whichever is chosen, the registered value and `redirect_uri` must be
byte-identical.

### 2. A shared client id pins the port for everyone

The registered callback contains a fixed port, so every user of the shared
application must be able to bind **8629** locally. 8629 was picked to stay
clear of other EVE tools that bind a loopback callback:

| Port | Tool |
| --- | --- |
| 2221 | jEveAssets |
| 8635 | littlephish/eve-strait |
| 8629 | **EVE Booty** |

A user who already has 8629 bound by something else cannot change the port
against the shared application, because they cannot edit its registration.
Their fallback is to register their own application (below) and set both the
port and the client id in Settings. The app already detects a busy port and
names the likely culprit rather than throwing.

If the registration form accepts more than one callback URL, registering a
couple of alternates (e.g. 8630, 8631) would let the app retry a second port
before pushing the user to their own application. Worth checking on the form —
CCP's public docs don't state whether multiple are supported.

---

## After registering

Copy the client id into `DEFAULT_CLIENT_ID` in `src/evasset/config.py`. It is
a public identifier under PKCE, so committing it is intended, not a leak
(`AGENTS.md` §3). The client secret is not used and must never be committed.

Users can override it in **Settings → ESI application** at any time; nothing
should ever require them to.

---

## Registering your own application instead

Optional. Worth doing if port 8629 is taken on your machine, if you want your
own traffic separated from other users of the tool, or if you simply prefer
not to use a shared id.

1. Go to <https://developers.eveonline.com/applications> and create an
   application.
2. Connection Type: **Authentication & API Access**.
3. Tick the 16 scopes above. Do not tick the two rejected ones.
4. Set the callback URL to `http://localhost:8629/callback`, or another port
   if 8629 is busy — then set the same port in Settings so the two match.
5. Copy the client id into **Settings → ESI application**. Leave the secret
   blank; the app uses PKCE.
