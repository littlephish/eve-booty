"""ESIClient response handling, in particular bodies that are not there.

/contracts/public/items/{contract_id} documents a 204 with the description
"Contract expired or recently accepted by player". The contract pricing path
lists public contracts for a region and then fetches the items of each
candidate one at a time, so a contract can perfectly well expire or be
accepted in the gap between those two calls. That is a race nothing can close
from this side -- 204 is the normal, documented answer, not an error -- and
before this the client called .json() on it and took the whole sync down with
a JSONDecodeError that named neither the contract nor the endpoint.
"""

from __future__ import annotations

import httpx
import pytest

from evasset.config import Settings
from evasset.esi.client import ESIClient


class _StubTokens:
    """Contract endpoints are public; nothing here should ask for a token."""

    def access_token(self, character_id):  # pragma: no cover - a call is a bug
        raise AssertionError("public endpoints must not request a token")


def make_client(handler) -> ESIClient:
    client = ESIClient(Settings(), _StubTokens())
    client._http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://esi.test"
    )
    return client


def test_get_returns_none_for_a_204_instead_of_raising():
    client = make_client(lambda request: httpx.Response(204))
    assert client.get("/contracts/public/items/12345") is None


def test_the_caller_can_treat_that_as_an_empty_contract():
    """fetch_contract_prices does `... or []`, which only works if a missing
    body arrives as None rather than an exception."""
    client = make_client(lambda request: httpx.Response(204))
    assert (client.get("/contracts/public/items/1", allow_404=True) or []) == []


def test_a_200_with_an_empty_body_is_also_survivable():
    """Belt and braces: the status code is not the only way to end up with
    nothing to parse."""
    client = make_client(lambda request: httpx.Response(200, content=b""))
    assert client.get("/contracts/public/items/1") is None


def test_post_returns_none_for_a_204():
    client = make_client(lambda request: httpx.Response(204))
    assert client.post("/ui/openwindow/information", {"target_id": 1}) is None


def test_paginated_stops_cleanly_on_a_204():
    client = make_client(lambda request: httpx.Response(204))
    assert list(client.paginated("/contracts/public/12000000")) == []


def test_a_real_body_still_comes_back():
    """Guard against a fix broad enough to swallow good responses too."""
    payload = [{"type_id": 34, "quantity": 100}]
    client = make_client(lambda request: httpx.Response(200, json=payload))
    assert client.get("/contracts/public/items/1") == payload


def test_paginated_still_walks_every_page():
    pages = {
        "1": [{"contract_id": 1}],
        "2": [{"contract_id": 2}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        page = dict(request.url.params).get("page", "1")
        return httpx.Response(200, json=pages[page], headers={"x-pages": "2"})

    client = make_client(handler)
    assert list(client.paginated("/contracts/public/12000000")) == [
        [{"contract_id": 1}],
        [{"contract_id": 2}],
    ]


def test_a_404_with_allow_404_is_still_none():
    client = make_client(lambda request: httpx.Response(404, json={"error": "nope"}))
    assert client.get("/contracts/public/items/1", allow_404=True) is None


def test_a_real_error_still_raises():
    """A 204 being benign must not make a 400 benign too."""
    from evasset.esi.client import ESIError

    client = make_client(lambda request: httpx.Response(400, json={"error": "bad"}))
    with pytest.raises(ESIError):
        client.get("/contracts/public/items/1")
