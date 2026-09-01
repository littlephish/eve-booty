"""Application paths, settings and constants."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from platformdirs import PlatformDirs

# The app is called EVE Booty; this is deliberately not that. APP_NAME picks
# the platform data directory *and* the keyring service holding every
# character's SSO refresh token (see esi/auth.py's KEYRING_SERVICE), so
# renaming it would strand an existing install's database, settings and saved
# logins in a folder nothing looks in any more, and silently ask the user to
# re-authenticate every character. The on-disk identity stays put; only what
# the user reads changed.
APP_NAME = "evasset"
APP_AUTHOR = "evasset"
USER_AGENT_CONTACT = os.environ.get("EVASSET_CONTACT", "")

_dirs = PlatformDirs(APP_NAME, APP_AUTHOR, roaming=True)

DATA_DIR = Path(os.environ.get("EVASSET_DATA_DIR") or _dirs.user_data_dir)
CACHE_DIR = Path(os.environ.get("EVASSET_CACHE_DIR") or _dirs.user_cache_dir)
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "evasset.sqlite"
SETTINGS_PATH = DATA_DIR / "settings.json"

# --- ESI ---------------------------------------------------------------
ESI_BASE = "https://esi.evetech.net"
SSO_METADATA_URL = "https://login.eveonline.com/.well-known/oauth-authorization-server"
SSO_ISSUERS = ("login.eveonline.com", "https://login.eveonline.com")
SSO_AUDIENCE = "EVE Online"

# ESI now versions by date rather than by /v4/ style path segments. Bump this
# only after reviewing the ESI changelog -- it pins the response shape the
# parsers below were written against.
COMPATIBILITY_DATE = "2025-08-26"

SCOPES = [
    "publicData",
    "esi-assets.read_assets.v1",
    "esi-assets.read_corporation_assets.v1",
    "esi-wallet.read_character_wallet.v1",
    "esi-wallet.read_corporation_wallets.v1",
    "esi-markets.read_character_orders.v1",
    "esi-markets.read_corporation_orders.v1",
    "esi-contracts.read_character_contracts.v1",
    "esi-contracts.read_corporation_contracts.v1",
    "esi-industry.read_character_jobs.v1",
    "esi-industry.read_corporation_jobs.v1",
    "esi-characters.read_blueprints.v1",
    # esi-corporations.read_blueprints.v1 and esi-corporations.read_divisions.v1
    # are both deliberately excluded. Both are declared in ESI's own OpenAPI
    # spec, generate into every SDK built from that spec, and could be checked
    # in CCP's application registration UI -- but EVE SSO's /v2/oauth/authorize
    # rejects both with invalid_scope regardless, confirmed against a live
    # application on 2026-08-06 and 2026-08-07 respectively. Two for two on
    # corp-specific scopes is a pattern, not a coincidence; treat any new
    # corp-scoped addition here as unverified until someone actually completes
    # a login with it.
    #
    # Because invalid_scope is a pre-redirect rejection, SSO shows its own
    # inline error page instead of calling back to us, and OAuth2 fails the
    # *entire* authorization request over a single bad scope (RFC 6749
    # 4.1.2.1) -- so one broken entry in this list does not degrade gracefully,
    # it hangs every character's login until our own timeout, with no signal
    # about which scope caused it. That is why a scope goes here the moment it
    # is confirmed bad, rather than staying in as "probably fine."
    #
    # Cost of leaving these two out: corp-owned blueprints (ME/TE/runs on BPOs
    # in the corp hangar) are never synced, and wallet division numbers show as
    # "Division 1" etc. rather than their in-game names -- neither is
    # currently rendered by the UI anyway, so there is no visible loss yet.
    # Corp assets, wallet balance, orders, contracts, jobs and structures do
    # not depend on either scope. See the esi-issues precedent for
    # read_structures.v1 (#1030, #1302) for what getting this fixed upstream
    # tends to look like.
    "esi-corporations.read_structures.v1",
    "esi-universe.read_structures.v1",
    "esi-clones.read_clones.v1",
    "esi-clones.read_implants.v1",
]

# --- Static data -------------------------------------------------------
SDE_LATEST_URL = "https://developers.eveonline.com/static-data/tranquility/latest.jsonl"
SDE_BUILD_URL = (
    "https://developers.eveonline.com/static-data/tranquility/"
    "eve-online-static-data-{build}-jsonl.zip"
)

# --- Market ------------------------------------------------------------
FUZZWORK_AGGREGATES = "https://market.fuzzwork.co.uk/aggregates/"
JITA_4_4_STATION_ID = 60003760
THE_FORGE_REGION_ID = 10000002

# ESI's asset location_id is a fixed constant for every item sitting in Asset
# Safety, for every character and every system -- it is not a per-system id.
# Confirmed against ESI's own docs (docs.esi.evetech.net/docs/
# asset_location_id.html, "Asset Safety (location_id == 2004)"), not assumed.
# That also means ESI genuinely does not tell you which system the loss
# happened in from the asset alone; do not invent one.
ASSET_SAFETY_LOCATION_ID = 2004

# Capital hulls are priced from public contracts rather than the Jita order
# book, because most of them are only ever sold via contract. Matched by SDE
# group name so no group IDs are baked in here.
DEFAULT_CONTRACT_PRICED_GROUPS = [
    "Titan",
    "Supercarrier",
    "Carrier",
    "Force Auxiliary",
    "Dreadnought",
    "Lancer Dreadnought",
    "Capital Industrial Ship",
    "Freighter",
    "Jump Freighter",
]

# Regions scanned for public contracts when pricing capitals.
DEFAULT_CONTRACT_SCAN_REGIONS = [
    10000002,  # The Forge (Jita)
    10000043,  # Domain (Amarr)
    10000032,  # Sinq Laison (Dodixie)
    10000030,  # Heimatar (Rens)
]


# Loopback ports other EVE tools bind for their SSO callback. The callback URL
# has to match what is registered at CCP exactly, so we cannot shuffle ports at
# runtime -- picking a free one up front is the only option. Verified by reading
# each project's source on 2026-08-06:
#   2221  jEveAssets            (io/esi/EsiCallbackURL.java: LOCALHOST)
#   8635  littlephish/eve-strait (config.py: CALLBACK_PORT)
# littlephish/ore-hold-watcher does not run a callback server; it reads the
# client log file instead.
KNOWN_TAKEN_PORTS = {
    2221: "jEveAssets",
    8635: "eve-strait",
}


@dataclass
class Settings:
    client_id: str = ""
    client_secret: str = ""  # optional; unset means PKCE (recommended for a desktop app)
    callback_port: int = 8629  # clear of KNOWN_TAKEN_PORTS above
    callback_path: str = "/callback"
    contract_priced_groups: list[str] = field(
        default_factory=lambda: list(DEFAULT_CONTRACT_PRICED_GROUPS)
    )
    contract_scan_regions: list[int] = field(
        default_factory=lambda: list(DEFAULT_CONTRACT_SCAN_REGIONS)
    )
    # Drop contract prices further than this many IQRs from the quartiles
    # before averaging. 0 disables outlier rejection.
    contract_outlier_iqr: float = 1.5
    # Prefilter for the contract scan. Public contract listings do not say what
    # is inside them, so every candidate costs one extra ESI call -- these two
    # keep that number small.
    #
    # Volume: a contracted capital is normally packaged, and a packaged capital
    # measures 1,300,000 m3 (verified against a live Chimera contract). Do NOT
    # derive this from the SDE volume column: that is the *assembled* figure,
    # 11,925,000 m3 for the same hull, which would reject every real contract.
    # 500,000 sits above any packaged subcapital and below any capital.
    contract_min_volume: float = 500_000.0
    # Price: the cheapest capital hull is comfortably over half a billion.
    contract_min_price: float = 500_000_000.0
    # False: use Jita for a capital when it has both a bid and an ask, and only
    # fall back to the contract average when the order book is one-sided or
    # empty. True: always prefer the contract average for these groups, which
    # is closer to what a hull really trades for when the only Jita order is a
    # token listing.
    contract_price_beats_market: bool = False
    snapshot_on_sync: bool = True
    contact_email: str = ""

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.callback_port}{self.callback_path}"

    @classmethod
    def load(cls) -> Settings:
        if SETTINGS_PATH.exists():
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            known = {f: raw[f] for f in cls.__dataclass_fields__ if f in raw}
            return cls(**known)
        s = cls()
        s.save()
        return s

    def save(self) -> None:
        SETTINGS_PATH.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


def user_agent(settings: Settings | None = None) -> str:
    """ESI asks every client to identify itself and give a contact address."""
    contact = (settings.contact_email if settings else "") or USER_AGENT_CONTACT
    base = "evasset/0.1.0 (+https://github.com/local/evasset)"
    return f"{base} contact:{contact}" if contact else base
