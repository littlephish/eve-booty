"""Appraisals via Janice.

The Appraise button used to copy multibuy text to the clipboard and open
janice.e-351.com, leaving the user to paste it themselves. Janice has an API
that will take the list and hand back a saved appraisal, so the button can
just open the finished thing.

Qt-free on purpose, the same way queries.py and updater.py are: the parts
worth testing (building the request, reading the response, deciding what
counts as a failure) are then testable without a widget.

The request is JSON-RPC. No API key is required for Appraisal.create --
verified against the live endpoint -- so nothing here needs a credential, and
that is deliberate: a key baked into a distributed binary is a key given away.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .config import Settings, user_agent

ENDPOINT = "https://janice.e-351.com/api/rpc/v1?m=Appraisal.create"
APPRAISAL_URL = "https://janice.e-351.com/a/{code}"

# Janice's own identifiers, echoed back in the response. marketId 2 is
# confirmed as "Jita 4-4" by pricerMarket.name in the reply, which is the same
# market this app prices everything against (see config.JITA_4_4_STATION_ID).
#
# The other three are Janice's internal enumerations for appraisal type,
# price basis and price variant. They are sent as the values its own web form
# sends by default, and are named here rather than left as bare numbers in a
# dict. They are not documented publicly, so do not "tidy" them into something
# that looks more meaningful without checking what the site actually sends.
MARKET_JITA_4_4 = 2
DESIGNATION_DEFAULT = 100
PRICING_DEFAULT = 200
PRICING_VARIANT_DEFAULT = 100

# 1.0 means "100% of the quoted price", not 1%.
PRICE_PERCENTAGE = 1


class JaniceError(RuntimeError):
    """The appraisal could not be created. The caller is expected to fall back
    to the clipboard rather than surface this as a failure: an appraisal is a
    convenience, and losing it should not lose the list."""


@dataclass(frozen=True)
class Appraisal:
    """A saved appraisal, and what Janice made of the input."""

    code: str
    priced: int
    failed_lines: tuple[str, ...]
    total_buy: float
    total_sell: float

    @property
    def url(self) -> str:
        return APPRAISAL_URL.format(code=self.code)

    @property
    def failed(self) -> int:
        return len(self.failed_lines)


def build_request(text: str) -> dict:
    """The JSON-RPC body. Separated out so a test can assert its shape without
    a network."""
    return {
        "id": 4096,
        "method": "Appraisal.create",
        "params": {
            "marketId": MARKET_JITA_4_4,
            "designation": DESIGNATION_DEFAULT,
            "pricing": PRICING_DEFAULT,
            "pricingVariant": PRICING_VARIANT_DEFAULT,
            "pricePercentage": PRICE_PERCENTAGE,
            "input": text,
            "comment": "",
            "compactize": True,
        },
    }


def parse_response(payload: dict) -> Appraisal:
    """Read a reply, or say why it cannot be used.

    JSON-RPC reports application errors in an "error" member with HTTP 200, so
    a 200 alone proves nothing and the body has to be looked at.
    """
    if not isinstance(payload, dict):
        raise JaniceError("Janice returned something that was not a JSON object")
    if "error" in payload:
        raise JaniceError(f"Janice returned an error: {payload['error']}")

    result = payload.get("result")
    if not isinstance(result, dict):
        raise JaniceError("Janice returned no appraisal")

    code = str(result.get("code") or "")
    if not code:
        raise JaniceError("Janice returned an appraisal with no code")

    # Lines it could not identify come back verbatim, newline separated, and
    # the appraisal still succeeds for everything it did recognise.
    failures = str(result.get("failures") or "")
    failed_lines = tuple(line for line in failures.splitlines() if line.strip())

    prices = result.get("effectivePrices") or {}
    return Appraisal(
        code=code,
        priced=len(result.get("items") or []),
        failed_lines=failed_lines,
        total_buy=float(prices.get("totalBuyPrice") or 0.0),
        total_sell=float(prices.get("totalSellPrice") or 0.0),
    )


def create(text: str, settings: Settings | None = None, timeout: float = 20.0) -> Appraisal:
    """Send a multibuy list to Janice and return the saved appraisal."""
    if not text.strip():
        raise JaniceError("nothing to appraise")

    try:
        response = httpx.post(
            ENDPOINT,
            json=build_request(text),
            headers={"User-Agent": user_agent(settings), "Content-Type": "application/json"},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise JaniceError(f"could not reach Janice: {exc}") from exc
    except ValueError as exc:
        raise JaniceError(f"Janice returned something that was not JSON: {exc}") from exc

    return parse_response(payload)
