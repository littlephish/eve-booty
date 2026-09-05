"""ESI HTTP client.

Handles the things that bite you on a long asset sync: the error-limit
headers, per-route token buckets, paged endpoints, and retries on 5xx. ESI
routes are versioned by X-Compatibility-Date now, not by /v4/ path segments.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx

from ..config import COMPATIBILITY_DATE, ESI_BASE, Settings, user_agent
from ..logsetup import LOGGER
from .auth import TokenCache


class ESIError(RuntimeError):
    def __init__(self, status: int, url: str, body: str):
        super().__init__(f"ESI {status} on {url}: {body[:300]}")
        self.status = status
        self.url = url
        self.body = body


class ErrorLimiter:
    """ESI hands back X-ESI-Error-Limit-Remain / -Reset. Getting to zero earns
    a temporary ban, so we stop well short of it."""

    def __init__(self, floor: int = 10):
        self.floor = floor
        self.remain = 100
        self.reset_at = 0.0
        self._lock = threading.Lock()

    def observe(self, headers: httpx.Headers) -> None:
        with self._lock:
            if "x-esi-error-limit-remain" in headers:
                self.remain = int(headers["x-esi-error-limit-remain"])
            if "x-esi-error-limit-reset" in headers:
                self.reset_at = time.time() + int(headers["x-esi-error-limit-reset"])

    def wait_if_needed(self) -> None:
        with self._lock:
            if self.remain > self.floor:
                return
            delay = max(0.0, self.reset_at - time.time()) + 1.0
        if delay > 0:
            time.sleep(min(delay, 60.0))


class ESIClient:
    def __init__(self, settings: Settings, tokens: TokenCache, max_retries: int = 4):
        self.settings = settings
        self.tokens = tokens
        self.max_retries = max_retries
        self.errors = ErrorLimiter()
        self._http = httpx.Client(
            base_url=ESI_BASE,
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={
                "User-Agent": user_agent(settings),
                "X-Compatibility-Date": COMPATIBILITY_DATE,
                "Accept": "application/json",
            },
            follow_redirects=True,
        )

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ core
    def request(
        self,
        method: str,
        path: str,
        *,
        character_id: int | None = None,
        params: dict | None = None,
        json_body: Any = None,
        allow_404: bool = False,
        allow_403: bool = False,
    ) -> httpx.Response | None:
        headers = {}
        if character_id is not None:
            headers["Authorization"] = f"Bearer {self.tokens.access_token(character_id)}"

        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            self.errors.wait_if_needed()
            try:
                resp = self._http.request(
                    method, path, params=params, json=json_body, headers=headers
                )
            except httpx.TransportError as exc:
                last_exc = exc
                LOGGER.warning(
                    "ESI %s %s: transport error on attempt %d/%d: %s",
                    method, path, attempt + 1, self.max_retries, exc,
                )
                time.sleep(2**attempt)
                continue

            self.errors.observe(resp.headers)
            # The single most useful line in the log. A 403 from one endpoint
            # and "this character simply owns nothing" are indistinguishable by
            # the time either reaches the table.
            LOGGER.debug(
                "ESI %s %s -> %s%s",
                method, path, resp.status_code,
                f" (character {character_id})" if character_id else "",
            )

            if resp.status_code == 404 and allow_404:
                return None
            if resp.status_code == 403 and allow_403:
                return None
            if resp.status_code == 420:  # error-limited
                time.sleep(float(resp.headers.get("x-esi-error-limit-reset", 60)) + 1)
                continue
            if resp.status_code in (502, 503, 504) or resp.status_code == 500:
                time.sleep(2**attempt)
                last_exc = ESIError(resp.status_code, path, resp.text)
                continue
            if resp.status_code >= 400:
                raise ESIError(resp.status_code, path, resp.text)
            return resp

        if isinstance(last_exc, ESIError):
            raise last_exc
        raise ESIError(0, path, f"gave up after {self.max_retries} attempts: {last_exc}")

    @staticmethod
    def _body(resp: httpx.Response | None) -> Any:
        """Parse a JSON body, or return None when there is not one.

        A response under 400 does not guarantee something to parse. ESI
        answers /contracts/public/items/{id} and /contracts/public/bids/{id}
        with 204 No Content and the description "Contract expired or recently
        accepted by player" -- which is a normal outcome, not an error: the
        contract pricing path lists a region's public contracts and then
        fetches each candidate's items one at a time, so a contract can expire
        or be bought in the gap between the two calls. Calling .json() on that
        empty body raised JSONDecodeError from inside the worker thread and
        failed the whole sync over one contract that someone else happened to
        buy, naming neither the contract nor the endpoint in the traceback.

        The content check is there as well as the status check because an
        empty body is an empty body however it is labelled.
        """
        if resp is None or resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def get(self, path: str, **kw) -> Any:
        return self._body(self.request("GET", path, **kw))

    def post(self, path: str, json_body: Any, **kw) -> Any:
        return self._body(self.request("POST", path, json_body=json_body, **kw))

    def paginated(self, path: str, **kw) -> Iterator[list]:
        """Yields each page of an X-Pages endpoint."""
        params = dict(kw.pop("params", None) or {})
        params["page"] = 1
        resp = self.request("GET", path, params=params, **kw)
        if resp is None:
            return
        first = self._body(resp)
        if first is None:
            return
        yield first
        pages = int(resp.headers.get("x-pages", 1))
        for page in range(2, pages + 1):
            params["page"] = page
            r = self.request("GET", path, params=params, **kw)
            body = self._body(r)
            if body is None:
                return
            yield body

    def all_pages(self, path: str, **kw) -> list:
        out: list = []
        for page in self.paginated(path, **kw):
            out.extend(page)
        return out

    def post_chunked(
        self, path: str, ids: list[int], chunk: int = 1000, **kw
    ) -> list:
        """Assets names/locations endpoints take at most 1000 ids per call."""
        out: list = []
        for i in range(0, len(ids), chunk):
            got = self.post(path, ids[i : i + chunk], **kw)
            if got:
                out.extend(got)
        return out
