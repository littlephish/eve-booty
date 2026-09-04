"""In-place updates for the shipped Windows build.

Only the packaged app updates itself. Running from source, this is inert --
`can_update()` is False and nothing ever calls GitHub -- because a developer's
checkout is managed by git, not by an updater that would happily mirror a
release over the top of it.

The shipped artifact is a Nuitka --standalone *program folder*, not a onefile
exe, so an update is "download the new folder, mirror it over the old one,
relaunch" rather than "swap one file". That is not an accident of packaging:
a onefile build unpacks itself to %TEMP% and executes from there, which
Microsoft Defender and CrowdStrike both score as dropper behaviour, and the
folder layout is what makes the swap possible at all.

A running process cannot overwrite its own directory on Windows, so the swap
is done by updater/update.exe -- a small statically-linked Rust binary shipped
inside the program folder. The app copies *just that one file* to a temp
folder and runs it from there, which is what lets the new build replace the
entire install including the installed update.exe. It is deliberately not a
PowerShell script: a machine ExecutionPolicy of AllSigned or Restricted
overrides -ExecutionPolicy Bypass and silently refuses to run an unsigned
.ps1, so the update would simply never happen and leave no log behind.

Qt-free on purpose, the same way queries.py and treemap.py are: the parts
worth testing (which asset to pick, which version is newer, where the exe
lives inside the archive) are then testable without a widget or a network.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import USER_AGENT_CONTACT

# Where releases come from, and what the release workflow names things. The
# workflow builds "EVEBooty-<version>-win64.zip"; the "win" in that name is
# load-bearing here, and a comment says so at both ends.
UPDATE_REPO = os.environ.get("EVEBOOTY_UPDATE_REPO", "littlephish/eve-booty")
EXE_NAME = "evebooty.exe"
HELPER_NAME = "update.exe"

_API = "https://api.github.com/repos/{repo}/releases/latest"
# A program folder zip is tens of megabytes; anything tiny is an error page or
# a truncated transfer, not a build.
_MIN_ZIP_BYTES = 1_000_000


@dataclass(frozen=True)
class Release:
    """A newer release than the one running, and where to get it."""

    version: str
    url: str
    current: str
    # What changed, as published on the release. The workflow sets
    # generate_release_notes, so GitHub writes this from the commits in the
    # tag -- it was being fetched and thrown away, leaving the user asked to
    # approve an update whose contents they had no way to see. Optional
    # because a release can be published with an empty body.
    notes: str = ""
    page_url: str = ""


def parse_version(text: str) -> tuple[int, ...]:
    """"v1.2.3" and "1.2.3.0" both to comparable tuples.

    Digits are pulled out rather than the string split on dots, so a "v"
    prefix, a "-beta" suffix or a four-part Windows file version all compare
    without a special case each.
    """
    numbers = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in numbers[:4]) if numbers else (0,)


def is_frozen() -> bool:
    """True in the Nuitka build, False from a source checkout."""
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def install_dir() -> Path:
    """The folder holding the running executable -- what an update replaces."""
    return Path(sys.executable).resolve().parent


def can_write_install_dir() -> bool:
    """False when the program folder would need elevation we never ask for --
    a machine-wide Program Files install, say. An in-place update is then
    impossible, and saying so beats failing silently halfway through one."""
    try:
        probe = install_dir() / ".evebooty-write-test"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def current_version() -> str | None:
    """The version the release build stamped into the exe, or None from
    source. None is what disables the whole feature outside a real install."""
    if not (is_frozen() and sys.platform == "win32"):
        return None
    try:
        import ctypes

        path = sys.executable
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buffer)
        block = ctypes.c_void_p()
        length = ctypes.c_uint()
        if not ctypes.windll.version.VerQueryValueW(
            buffer, "\\", ctypes.byref(block), ctypes.byref(length)
        ):
            return None
        # VS_FIXEDFILEINFO: dwFileVersionMS at offset 8, dwFileVersionLS at 12
        raw = ctypes.string_at(block, length.value)
        ms = int.from_bytes(raw[8:12], "little")
        ls = int.from_bytes(raw[12:16], "little")
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:  # noqa: BLE001 - version resources are best effort
        return None


def version_string() -> str:
    """The version to show a user, and the one to quote in a bug report.

    Normally just __version__, which scripts/set_version.py stamped from the
    release tag before Nuitka compiled it. The exe's own version resource is
    consulted only to catch the case that actually matters: the two
    disagreeing, which means the stamping step did not run and the build is
    misreporting itself. Surfacing that in About beats discovering it from a
    user who says they are on a version that was never released.
    """
    from . import __version__

    stamped = current_version()
    if stamped and parse_version(stamped) != parse_version(__version__):
        return f"{__version__} (exe resource {stamped})"
    return __version__


def can_update() -> bool:
    """Every condition that has to hold before we even ask GitHub."""
    return (
        bool(UPDATE_REPO)
        and sys.platform == "win32"
        and is_frozen()
        and can_write_install_dir()
    )


def _user_agent() -> str:
    contact = USER_AGENT_CONTACT.strip()
    return f"EVE-Booty-updater ({contact})" if contact else "EVE-Booty-updater"


def pick_asset(assets: list[dict]) -> dict | None:
    """The portable program-folder zip out of a release's assets.

    Matched on "win" plus ".zip" rather than an exact filename so the version
    in the middle of it does not have to be reconstructed here, and so an
    installer .exe published alongside is never mistaken for the archive.
    """
    for asset in assets:
        name = str(asset.get("name", "")).lower()
        if name.endswith(".zip") and "win" in name:
            return asset
    return None


def check(timeout: float = 15.0) -> Release | None:
    """Ask GitHub for the newest release. None means "nothing newer"."""
    current = current_version() or "0"
    url = _API.format(repo=UPDATE_REPO)
    headers = {"User-Agent": _user_agent(), "Accept": "application/vnd.github+json"}
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()

    asset = pick_asset(data.get("assets") or [])
    tag = str(data.get("tag_name") or "")
    if asset is None or parse_version(tag) <= parse_version(current):
        return None
    return Release(
        version=tag,
        url=asset["browser_download_url"],
        current=current,
        notes=str(data.get("body") or "").strip(),
        page_url=str(data.get("html_url") or ""),
    )


def download(release: Release, progress=None, timeout: float = 300.0) -> Path:
    """Fetch the release zip to a temp folder and return its path."""
    base = Path(tempfile.mkdtemp(prefix="evebooty-update-"))
    dest = base / "update.zip"
    headers = {"User-Agent": _user_agent()}
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        with client.stream("GET", release.url, headers=headers) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length") or 0)
            written = 0
            with open(dest, "wb") as handle:
                for chunk in response.iter_bytes(1 << 20):
                    handle.write(chunk)
                    written += len(chunk)
                    if progress is not None and total:
                        progress(int(written * 100 / total))
    if dest.stat().st_size < _MIN_ZIP_BYTES:
        raise ValueError("the downloaded archive is too small to be a build")
    return dest


def extract(zip_path: Path) -> Path:
    """Unpack, and return the folder that actually holds the exe.

    Compress-Archive nests the program folder one level inside the zip, so the
    exe is looked for rather than assumed to be at the root.
    """
    out = zip_path.parent / "unpacked"
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(out)
    if (out / EXE_NAME).exists():
        return out
    for found in out.rglob(EXE_NAME):
        return found.parent
    raise RuntimeError(f"{EXE_NAME} is not in the downloaded archive")


def _spawn_detached(args: list[str]) -> bool:
    """Start the helper so it outlives us -- we are about to exit, and a child
    that dies with us would never finish the swap."""
    detached = getattr(subprocess, "DETACHED_PROCESS", 0x8)
    new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x1000000)
    for flags in (detached | new_group | breakaway, detached | new_group):
        try:
            subprocess.Popen(args, creationflags=flags, close_fds=True)
            return True
        except OSError:
            continue
    return False


def apply(new_dir: Path) -> bool:
    """Launch the detached swap helper. True means "now quit, it has this".

    The helper is taken from the *newly downloaded* build first and only then
    from the current install, so a fix to the updater itself ships and takes
    effect on the very update that carries it. Either way it runs from a temp
    copy, which is what lets it overwrite the installed update.exe too.
    """
    target = install_dir()
    staging = new_dir.parent
    for source in (new_dir / HELPER_NAME, target / HELPER_NAME):
        if not source.exists():
            continue
        try:
            helper = staging / HELPER_NAME
            if source != helper:
                shutil.copy2(source, helper)
            if _spawn_detached([str(helper), str(new_dir), str(target), EXE_NAME]):
                return True
        except OSError:
            continue
    return False
