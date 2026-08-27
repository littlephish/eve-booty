"""Type icon download and disk cache.

Icons are not in the SDE jsonl export at all -- the one public source is CCP's
image service, one small PNG per type id. Fetched on demand (a fit shows a few
dozen icons, so prefetching every owned type would pull thousands of files
nothing ever looks at) and cached on disk forever: type icons only change when
CCP redesigns them, which is rare enough that "re-download after a manual cache
clear" is the right invalidation policy.

Qt-free on purpose, like pricing.py and fitting.py -- the download-and-cache
logic is testable with httpx.MockTransport and no window. The fit dialog wraps
it in a QRunnable to keep the fetch off the GUI thread.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from .config import CACHE_DIR, Settings, user_agent

IMAGE_SERVER = "https://images.evetech.net"

# The image service serves types at fixed power-of-two sizes; 32 is the
# smallest it offers and plenty for a 24 px on-screen icon.
ICON_SIZE = 32

ICON_DIR = CACHE_DIR / "icons"

# Cold downloads run concurrently: measured sequentially they cost ~150ms
# each, so a 16-icon first open kept placeholders up for ~2.4s. Six workers
# bring that down to roughly the cost of the slowest three requests. Modest on
# purpose -- this is a burst against a CDN, not a scrape.
_MAX_PARALLEL = 6


def icon_path(type_id: int) -> Path | None:
    """The cached icon file, or None if it has never been fetched successfully."""
    path = ICON_DIR / f"{type_id}.png"
    return path if path.exists() else None


def fetch_icons(
    type_ids: list[int],
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[int, Path]:
    """Download whichever of these ids are not cached yet; return every id
    that now has a file, cached or fresh.

    A failed id -- 404 (a type with no icon), a transport error mid-batch --
    is simply absent from the result rather than raising: the dialog shows a
    placeholder for it and the next open retries. `transport` exists for
    tests (httpx.MockTransport); None means the real network.
    """
    out: dict[int, Path] = {}
    todo: list[int] = []
    for tid in dict.fromkeys(type_ids):  # de-dup, keep order
        cached = icon_path(tid)
        if cached is not None:
            out[tid] = cached
        else:
            todo.append(tid)
    if not todo:
        return out

    ICON_DIR.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": user_agent(settings)}
    with httpx.Client(
        base_url=IMAGE_SERVER, headers=headers, timeout=30,
        follow_redirects=True, transport=transport,
    ) as client:
        # httpx.Client is documented thread-safe, so the workers share one
        # connection pool rather than each paying a TLS handshake.

        def fetch_one(tid: int) -> tuple[int, Path | None]:
            # The whole body is guarded per id, disk writes included, so one
            # bad id costs one icon rather than the rest of the batch --
            # cached hits already in `out` must survive whatever happens here.
            try:
                r = client.get(f"/types/{tid}/icon", params={"size": ICON_SIZE})
                if r.status_code != 200 or not r.content:
                    return tid, None
                dest = ICON_DIR / f"{tid}.png"
                # The temp name carries pid and thread id because two writers
                # can fetch the same uncached type at once (two dialogs, or
                # close-and-reopen while the first job still runs). With a
                # shared "{tid}.part" the loser's replace() raises
                # FileNotFoundError; with a name per writer both replace()
                # calls succeed and the second just overwrites the first with
                # identical bytes.
                tmp = dest.with_name(f"{tid}.{os.getpid()}-{threading.get_ident()}.part")
                tmp.write_bytes(r.content)
                tmp.replace(dest)
                return tid, dest
            except (httpx.HTTPError, OSError):
                return tid, None

        if len(todo) == 1:
            results = [fetch_one(todo[0])]
        else:
            with ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL, len(todo))) as pool:
                results = list(pool.map(fetch_one, todo))
    for tid, dest in results:
        if dest is not None:
            out[tid] = dest
    return out
