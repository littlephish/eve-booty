"""Icon download-and-cache behaviour, exercised entirely through
httpx.MockTransport -- no test here may ever touch the real image server.

The cache is a plain directory of {type_id}.png files that persists for the
whole session by design (icons barely ever change), so every test uses ids
from a private high range and clears them away afterwards; without that, a
"downloads on first fetch" test would silently pass against a file some
earlier test left behind.
"""

from __future__ import annotations

import httpx
import pytest

from evasset import icons

FAKE_PNG = b"\x89PNG fake"

# Far above any real EVE type id, so these files can never collide with
# anything another test legitimately cached.
CACHED_ID = 99900001
MISSING_ID = 99900002
EXTRA_ID = 99900003
NEVER_FETCHED_ID = 99900404

_ALL_TEST_IDS = (CACHED_ID, MISSING_ID, EXTRA_ID, NEVER_FETCHED_ID)


@pytest.fixture(autouse=True)
def _clean_icon_dir():
    """Remove this module's icon files before and after each test, so the
    session-long cache (a feature in production) cannot leak state between
    tests that are specifically about cache misses.

    Exact ids, not a glob -- a "999*.png" pattern would also match real
    three-to-six-digit type ids, quietly breaking the no-collision promise
    the constants above make."""

    def wipe():
        for tid in _ALL_TEST_IDS:
            (icons.ICON_DIR / f"{tid}.png").unlink(missing_ok=True)

    wipe()
    yield
    wipe()


class _CountingHandler:
    """Serves fake PNG bytes and counts requests, so a test can prove not just
    what came back but whether the network was touched at all."""

    def __init__(self, missing_ids=()):
        self.calls = 0
        self.missing_ids = set(missing_ids)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        # Pin the request shape, not just the count -- a wrong host, path or
        # size param would otherwise pass every test here while 404ing against
        # the real image server.
        assert request.url.host == "images.evetech.net"
        assert request.url.path.endswith("/icon")
        assert request.url.params["size"] == str(icons.ICON_SIZE)
        tid = int(request.url.path.split("/")[2])  # /types/{tid}/icon
        if tid in self.missing_ids:
            return httpx.Response(404)
        return httpx.Response(200, content=FAKE_PNG)


def _fetch(ids, handler):
    return icons.fetch_icons(ids, transport=httpx.MockTransport(handler))


def test_an_uncached_icon_is_downloaded_and_written_to_the_cache():
    handler = _CountingHandler()
    got = _fetch([CACHED_ID], handler)
    assert handler.calls == 1
    assert got[CACHED_ID] == icons.ICON_DIR / f"{CACHED_ID}.png"
    assert got[CACHED_ID].read_bytes() == FAKE_PNG


def test_second_call_hits_the_cache_without_touching_the_network():
    """The whole point of the disk cache: reopening a fit dialog must cost
    zero requests for icons already on disk."""
    handler = _CountingHandler()
    first = _fetch([CACHED_ID], handler)
    second = _fetch([CACHED_ID], handler)
    assert handler.calls == 1, "the second fetch must not go to the network"
    assert second == first


def test_a_404_id_is_omitted_not_cached_and_retried_next_time():
    """Some types genuinely have no icon, but a 404 can also be transient --
    so a miss must leave nothing on disk that would stop a later fetch from
    trying again."""
    handler = _CountingHandler(missing_ids={MISSING_ID})
    got = _fetch([MISSING_ID], handler)
    assert MISSING_ID not in got
    assert not (icons.ICON_DIR / f"{MISSING_ID}.png").exists()

    again = _fetch([MISSING_ID], handler)
    assert handler.calls == 2, "a failed id must be retried, not remembered as failed"
    assert MISSING_ID not in again


def test_icon_path_is_none_for_an_id_never_fetched():
    assert icons.icon_path(NEVER_FETCHED_ID) is None


def test_duplicate_ids_in_the_input_cost_one_request():
    """A fit full of the same faction gun asks for the same icon many times;
    the batch must collapse those to a single download."""
    handler = _CountingHandler()
    got = _fetch([EXTRA_ID, EXTRA_ID, EXTRA_ID], handler)
    assert handler.calls == 1
    assert got == {EXTRA_ID: icons.ICON_DIR / f"{EXTRA_ID}.png"}


def test_one_failing_id_does_not_take_the_rest_of_the_batch_with_it():
    """Failures are contained per id. This used to be false: an exception on
    one id escaped fetch_icons entirely, discarding every icon in the batch --
    including ones already sitting in the cache."""

    def handler(request: httpx.Request) -> httpx.Response:
        tid = int(request.url.path.split("/")[2])
        if tid == MISSING_ID:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, content=FAKE_PNG)

    got = _fetch([MISSING_ID, CACHED_ID], handler)
    assert MISSING_ID not in got
    assert got[CACHED_ID] == icons.ICON_DIR / f"{CACHED_ID}.png"
