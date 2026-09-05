"""Appraisals via Janice.

The button used to copy multibuy text and open the site, leaving the user to
paste. It now posts the list and opens the finished appraisal.

No test here touches the network. The request shape and the response reading
are separated from the call for exactly that reason, and the one test that
exercises create() stubs httpx.
"""

from __future__ import annotations

import httpx
import pytest

from evasset import janice

# A real reply, trimmed. Kept verbatim in shape because the fields read here
# are the ones the live API actually returns.
REPLY = {
    "id": 4096,
    "result": {
        "code": "r1LKbY",
        "failures": "",
        "items": [{"itemType_eid": 34}, {"itemType_eid": 645}],
        "effectivePrices": {
            "totalBuyPrice": 276003670.0,
            "totalSplitPrice": 289603775.0,
            "totalSellPrice": 303203880.0,
        },
    },
}


# --------------------------------------------------------------- the request
def test_the_request_carries_the_list_and_the_market():
    body = janice.build_request("Tritanium\t1000")

    assert body["method"] == "Appraisal.create"
    params = body["params"]
    assert params["input"] == "Tritanium\t1000"
    assert params["marketId"] == janice.MARKET_JITA_4_4
    assert params["compactize"] is True


def test_the_market_is_the_one_this_app_prices_against():
    """Janice's marketId 2 is Jita 4-4, which is the same market pricing.py
    uses. An appraisal against a different market would silently disagree with
    every value in the table."""
    assert janice.MARKET_JITA_4_4 == 2


# -------------------------------------------------------------- the response
def test_a_good_reply_becomes_an_appraisal():
    appraisal = janice.parse_response(REPLY)

    assert appraisal.code == "r1LKbY"
    assert appraisal.url == "https://janice.e-351.com/a/r1LKbY"
    assert appraisal.priced == 2
    assert appraisal.failed == 0
    assert appraisal.total_sell == 303203880.0


def test_unrecognised_lines_are_reported_but_do_not_fail_the_appraisal():
    """Janice prices what it knows and hands back the rest verbatim. Throwing
    the whole appraisal away over one bad line would be the wrong trade."""
    reply = {**REPLY, "result": {**REPLY["result"],
                                 "failures": "NotAnItem\t5\nAlsoNot\t1",
                                 "items": [{"itemType_eid": 34}]}}

    appraisal = janice.parse_response(reply)

    assert appraisal.code == "r1LKbY"
    assert appraisal.priced == 1
    assert appraisal.failed == 2
    assert appraisal.failed_lines == ("NotAnItem\t5", "AlsoNot\t1")


def test_a_jsonrpc_error_member_is_a_failure():
    """JSON-RPC reports application errors with HTTP 200, so the status code
    proves nothing on its own."""
    with pytest.raises(janice.JaniceError, match="returned an error"):
        janice.parse_response({"id": 1, "error": {"code": -32000, "message": "nope"}})


def test_a_reply_with_no_result_is_a_failure():
    with pytest.raises(janice.JaniceError):
        janice.parse_response({"id": 1})


def test_a_reply_with_no_code_is_a_failure():
    """Without a code there is no URL to open, so a 200 is still unusable."""
    with pytest.raises(janice.JaniceError, match="no code"):
        janice.parse_response({"id": 1, "result": {"items": []}})


def test_a_non_object_reply_is_a_failure():
    with pytest.raises(janice.JaniceError):
        janice.parse_response(["not", "an", "object"])


# ------------------------------------------------------------------- the call
def test_empty_input_never_reaches_the_network(monkeypatch):
    def explode(*a, **kw):
        raise AssertionError("must not post an empty list")

    monkeypatch.setattr(janice.httpx, "post", explode)
    with pytest.raises(janice.JaniceError, match="nothing to appraise"):
        janice.create("   ")


def test_a_network_failure_is_a_janice_error_not_an_httpx_one(monkeypatch):
    """The caller catches JaniceError to fall back to the clipboard. An httpx
    exception leaking through would bypass that."""
    def boom(*a, **kw):
        raise httpx.ConnectTimeout("too slow")

    monkeypatch.setattr(janice.httpx, "post", boom)
    with pytest.raises(janice.JaniceError, match="could not reach Janice"):
        janice.create("Tritanium\t1")


def test_a_successful_call_sends_the_list_and_identifies_the_app(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return REPLY

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return Response()

    monkeypatch.setattr(janice.httpx, "post", fake_post)
    appraisal = janice.create("Tritanium\t1000")

    assert appraisal.code == "r1LKbY"
    assert captured["url"] == janice.ENDPOINT
    assert captured["json"]["params"]["input"] == "Tritanium\t1000"
    # CCP asks tools to identify themselves, and so should anyone else's API.
    assert "EVEBooty/" in captured["headers"]["User-Agent"]


# ------------------------------------------------------- the button behaviour
@pytest.fixture()
def view(qapp_or_skip, monkeypatch):
    from evasset.ui.assets_view import AssetsView

    v = AssetsView(defer_load=True)
    v._multibuy_text = lambda: "Tritanium\t1000"
    yield v
    v.close()


def _capture_opened(monkeypatch):
    from PySide6.QtGui import QDesktopServices

    opened: list[str] = []
    monkeypatch.setattr(
        QDesktopServices, "openUrl", staticmethod(lambda u: opened.append(u.toString()))
    )
    return opened


def test_the_list_is_on_the_clipboard_before_anything_is_sent(view, monkeypatch):
    """The clipboard is the fallback, so it is filled unconditionally and
    first. Someone who only wanted the text still has it."""
    from PySide6.QtGui import QGuiApplication

    _capture_opened(monkeypatch)
    monkeypatch.setattr(
        "evasset.ui.assets_view.AppraiseJob",
        lambda text, settings: pytest.skip("job not started in this test"),
    )
    try:
        view.appraise()
    except BaseException:
        pass
    assert QGuiApplication.clipboard().text() == "Tritanium\t1000"


def test_a_failed_appraisal_falls_back_to_the_old_behaviour(view, monkeypatch):
    """Losing the appraisal must not lose the list. Before there was an API
    this button copied and opened the site, and that is exactly what a failure
    should leave the user with."""
    opened = _capture_opened(monkeypatch)

    view._on_appraise_failed("could not reach Janice")

    assert opened == ["https://janice.e-351.com/"]
    assert "Copied" in view.footer.text()


def test_a_successful_appraisal_opens_the_saved_one(view, monkeypatch):
    opened = _capture_opened(monkeypatch)
    appraisal = janice.Appraisal(
        code="abc123", priced=2, failed_lines=(), total_buy=1.0, total_sell=2.0
    )

    view._on_appraised({"appraisal": appraisal})

    assert opened == ["https://janice.e-351.com/a/abc123"]
    assert "Appraised 2 item(s)" in view.footer.text()


def test_unrecognised_lines_are_named_in_the_footer(view, monkeypatch):
    """A count sends somebody hunting; the names usually explain themselves."""
    _capture_opened(monkeypatch)
    appraisal = janice.Appraisal(
        code="abc123", priced=1,
        failed_lines=("Bogus Thing\t5",), total_buy=1.0, total_sell=2.0,
    )

    view._on_appraised({"appraisal": appraisal})

    assert "1 not recognised" in view.footer.text()
    assert "Bogus Thing" in view.footer.text()


def test_an_error_result_takes_the_fallback_path(view, monkeypatch):
    opened = _capture_opened(monkeypatch)

    view._on_appraised({"error": "could not reach Janice"})

    assert opened == ["https://janice.e-351.com/"]
