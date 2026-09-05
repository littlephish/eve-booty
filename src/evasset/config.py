"""Application paths, settings and constants."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from platformdirs import PlatformDirs

# APP_NAME picks the platform data directory, the cache directory *and* the
# keyring service holding every character's SSO refresh token (esi/auth.py's
# KEYRING_SERVICE). It was left as "evasset" long after the app was renamed
# precisely because changing it moves all three at once: the database and
# settings end up in a folder nothing looks in, and every saved login silently
# disappears because the credential store is keyed by service name.
#
# It is safe to change now only because both halves are carried across --
# _adopt_legacy_dir below moves the folders, and auth._adopt_legacy_token
# re-homes each refresh token the first time its character is used. Do not
# rename this again without checking both still cover it.
APP_NAME = "eve-booty"
APP_AUTHOR = "eve-booty"

# What the app used to be called on disk. The data directory, the cache
# directory and the keyring service holding every refresh token were all
# derived from APP_NAME, so renaming it strands an existing install unless all
# three are carried across -- see _adopt_legacy_dir below for the folders and
# esi/auth.py for the tokens. Keep this list; it is how upgrades from any
# previous name keep working, and it costs one stat() per launch.
LEGACY_APP_NAMES = ("evasset",)

# EVEBOOTY_* is the current spelling; EVASSET_* still works so that anyone with
# a scheduled task or a shell profile pointing at the old names does not have
# it break under them on upgrade.
def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"EVEBOOTY_{name}") or os.environ.get(f"EVASSET_{name}") or default


USER_AGENT_CONTACT = _env("CONTACT")

# Where this tool lives, as sent to ESI in the User-Agent and shown in About.
# Kept in step with updater.UPDATE_REPO, which pulls releases from the same
# repository; a test asserts they do not drift apart.
PROJECT_REPO = "littlephish/eve-booty"

# The ESI application this app ships with, so a fresh install can add a
# character without registering anything first. Public on purpose: the login
# uses PKCE, where the client id is an identifier rather than a credential and
# the per-login code verifier is what proves the request is genuine. There is
# no client secret and there must never be one -- anything embedded in a
# downloadable binary can be read straight back out of it.
#
# Users may substitute their own application in Settings at any time, and must
# be able to: the callback URL registered against this id fixes the loopback
# port at CALLBACK_PORT for everyone sharing it, so anyone who already has that
# port bound needs their own. See docs/esi-application.md.
DEFAULT_CLIENT_ID = "3cfb528fc0b448dc87808d1bcbfe6083"

PROJECT_URL = f"https://github.com/{PROJECT_REPO}"

_dirs = PlatformDirs(APP_NAME, APP_AUTHOR, roaming=True)


def _adopt_legacy_dir(current: Path, legacy: Path) -> bool:
    """Move a pre-rename directory to where this build now looks.

    Runs at import, which is deliberate: everything downstream -- the database,
    settings.json and the tokens.json fallback -- is addressed relative to
    DATA_DIR, so the move has to finish before any of them is opened or a fresh
    empty database gets created next to a perfectly good old one.

    Moved wholesale rather than file by file. The database may have `-wal` and
    `-shm` siblings holding committed transactions that are not in the main
    file yet, and separating them from it is how you turn a rename into data
    loss. Moving the directory keeps the set together, and is also why the
    database keeps its `evasset.sqlite` filename: renaming it would mean
    renaming the WAL set in step, for no gain a user can see.

    Returns True if something was actually moved.
    """
    if current.exists() and any(current.iterdir()):
        return False  # already migrated, or this install started here
    if not legacy.is_dir() or not any(legacy.iterdir()):
        return False  # nothing to bring across

    try:
        current.parent.mkdir(parents=True, exist_ok=True)
        if current.exists():
            # An empty directory a previous launch created before failing, or
            # one the OS pre-made. rmdir only succeeds while it is empty, which
            # is the check we want anyway.
            current.rmdir()
        # os.rename, not os.replace. os.replace passes MOVEFILE_REPLACE_EXISTING
        # to MoveFileExW, which Windows rejects outright for a directory --
        # WinError 5, "access is denied", which reads like a permissions
        # problem and is not one. shutil.move then covers the case os.rename
        # cannot do at all, a data directory and an app directory sitting on
        # different volumes, by falling back to copy-and-delete.
        try:
            os.rename(legacy, current)
        except OSError:
            shutil.move(str(legacy), str(current))
        # PlatformDirs nests <author>/<name>, so moving the inner directory
        # leaves an empty husk behind. rmdir refuses if anything is in it,
        # which is the guard we want -- a sibling directory from another tool
        # by the same author is not ours to remove.
        with contextlib.suppress(OSError):
            legacy.parent.rmdir()
        return True
    except OSError:
        # A locked file, a half-finished move, or a volume that will not take
        # it. Report failure so the caller keeps using the old location: an
        # upgrade that cannot rename its folder is a nuisance, one that walks
        # away from the folder and starts empty is data loss.
        #
        # Undo any half-copy first. shutil.move falls back to copy-then-delete,
        # so a failure part way through leaves the same data in both places --
        # and the copy is a snapshot of a database that was open at the time,
        # so it may be torn. Left there, the next launch would find a non-empty
        # new directory, take it as authoritative, and quietly start reading a
        # damaged copy while the real one carried on being written next door.
        # This is exactly what a running instance holding the database causes.
        if legacy.is_dir() and any(legacy.iterdir()) and current.exists():
            with contextlib.suppress(OSError):
                shutil.rmtree(current)
        return False


def _resolve_dir(env_name: str, current: str, legacy_attr: str) -> Path:
    override = _env(env_name)
    if override:
        # An explicit path (the test suite, or a portable install) is taken at
        # face value and never migrated into -- moving a real install's data
        # into a scratch directory would be the worst possible bug here.
        return Path(override)
    target = Path(current)
    if target.is_dir() and any(target.iterdir()):
        return target  # already living here; nothing to consider
    for name in LEGACY_APP_NAMES:
        old = Path(getattr(PlatformDirs(name, name, roaming=True), legacy_attr))
        if not old.is_dir() or not any(old.iterdir()):
            continue
        if _adopt_legacy_dir(target, old):
            return target
        # The move failed and the data is still in the old directory. Keep
        # using it rather than starting empty beside it, which would look
        # exactly like every character and every asset having vanished.
        return old
    return target


DATA_DIR = _resolve_dir("DATA_DIR", _dirs.user_data_dir, "user_data_dir")
CACHE_DIR = _resolve_dir("CACHE_DIR", _dirs.user_cache_dir, "user_cache_dir")
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
    client_id: str = DEFAULT_CLIENT_ID
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
    # Ask ESI for the rolled stats of newly seen abyssal items as part of a
    # routine sync. Off by default because it is one public GET per item and
    # ESI has no batch route -- the first successful manual run offers to
    # turn it on once the user has seen what it costs.
    abyssal_stats_on_sync: bool = False
    contact_email: str = ""
    # Off by default: the useful lines name someone's characters and
    # holdings, so they belong on disk only when a user asks for them.
    debug_logging: bool = False
    # Costs one 80-byte request at startup. On by default because the
    # alternative is a user whose Assets tab is silently empty.
    check_sde_on_startup: bool = True

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.callback_port}{self.callback_path}"

    @classmethod
    def load(cls) -> Settings:
        if SETTINGS_PATH.exists():
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            known = {f: raw[f] for f in cls.__dataclass_fields__ if f in raw}
            # An empty client id means "use the one we ship", not "use no
            # client id at all". A dataclass default only applies when the key
            # is absent, and clearing the box in Settings writes "" rather than
            # dropping the key -- so without this, emptying the field in the UI
            # sent client_id="" to SSO and came back as a 401 with a page of
            # HTML in it. Blank is also what every settings.json written before
            # DEFAULT_CLIENT_ID existed contains.
            if not str(known.get("client_id", "")).strip():
                known.pop("client_id", None)
            return cls(**known)
        s = cls()
        s.save()
        return s

    def save(self) -> None:
        SETTINGS_PATH.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


def user_agent(settings: Settings | None = None) -> str:
    """ESI asks every client to identify itself and give a contact address.

    The version comes from the package (stamped from the release tag by
    scripts/set_version.py) rather than a literal, so a build cannot report a
    version it is not. The URL has to be somewhere CCP can actually reach a
    human: this is the header they use to work out who to contact before they
    rate-limit or block a misbehaving third-party tool, so a placeholder is
    worse than useless.
    """
    from . import __version__

    contact = (settings.contact_email if settings else "") or USER_AGENT_CONTACT
    base = f"EVEBooty/{__version__} (+{PROJECT_URL})"
    return f"{base} contact:{contact}" if contact else base
