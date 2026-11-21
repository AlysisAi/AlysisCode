"""What a page reports about an acting step (ManagedBrowserService.page_report): its own requests
that failed or answered with an error status, and its uncaught script errors, from the browser's
own events (the fixtures are shaped as Chromium sends them), with nothing private in it."""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import quote

import pytest
from test_ide_managed_browser_hands import OOP, PAGE, FakePage, Node, start

from alysis_code.ide import managed_browser as mb

SITE = "https://shop.example.test"
DOCUMENT = SITE + "/checkout"


def request(
    request_id: str,
    url: str,
    *,
    method: str = "GET",
    kind: str = "Fetch",
    same_site: bool | None = True,
    at: float | None = None,
) -> dict[str, Any]:
    sent: dict[str, Any] = {"url": url, "method": method, "headers": {"Cookie": "sid=private"}}
    if same_site is not None:
        sent["isSameSite"] = same_site
    if method == "POST":
        sent["postData"] = '{"card": "4111 1111 1111 1111"}'
    return {
        "method": "Network.requestWillBeSent",
        "params": {
            "requestId": request_id,
            "loaderId": "loader-1",
            "documentURL": DOCUMENT,
            "request": sent,
            "timestamp": 1234.5,
            "wallTime": time.time() if at is None else at,
            "type": kind,
            "frameId": "main-frame",
        },
    }


def response(request_id: str, url: str, status: int, *, kind: str = "Fetch") -> dict[str, Any]:
    return {
        "method": "Network.responseReceived",
        "params": {
            "requestId": request_id,
            "loaderId": "loader-1",
            "timestamp": 1235.0,
            "type": kind,
            "response": {
                "url": url,
                "status": status,
                "statusText": "Internal Server Error",
                "headers": {"Set-Cookie": "sid=private"},
                "mimeType": "application/json",
            },
        },
    }


def failed(request_id: str, error: str, *, kind: str = "Fetch", canceled: bool = False):
    return {
        "method": "Network.loadingFailed",
        "params": {
            "requestId": request_id,
            "timestamp": 1235.0,
            "type": kind,
            "errorText": error,
            "canceled": canceled,
        },
    }


def finished(request_id: str) -> dict[str, Any]:
    return {
        "method": "Network.loadingFinished",
        "params": {"requestId": request_id, "timestamp": 1235.0, "encodedDataLength": 10},
    }


def script_error(at: float | None = None) -> dict[str, Any]:
    return {
        "method": "Runtime.exceptionThrown",
        "params": {
            "timestamp": (time.time() if at is None else at) * 1000,
            "exceptionDetails": {
                "exceptionId": 1,
                "text": "Uncaught TypeError: card 4111 1111 1111 1111 rejected",
                "lineNumber": 3,
                "columnNumber": 7,
            },
        },
    }


def page_with_button(on_click) -> FakePage:
    page = FakePage()
    page.add(PAGE, 14, Node("main-frame", "BUTTON", [(10, 10, 80, 20)], on_click=on_click))
    return page


def push_all(page: FakePage, session: str, *events: dict[str, Any]) -> None:
    for event in events:
        page.push(session, event)


ORDER = SITE + "/api/orders/104233/items?coupon=SECRET-COUPON&token=abc#receipt"
SESSION = SITE + "/api/session/5f8d0d55b54764421b7156c9/renew"
ADS = "https://ads.other-site.example/pixel/7"


def test_a_click_reports_the_sites_own_failed_requests_and_script_errors(tmp_path):
    # Before the step: a request that failed and a script error the step did not cause.
    earlier = time.time() - 60

    def submitted(_values):
        # The click starts the site's order request, a third-party pixel and a session renewal,
        # and its script throws once; another site's frame throws too.
        push_all(
            page,
            PAGE,
            request("order", ORDER, method="POST"),
            request("pixel", ADS, same_site=False),
            request("renew", SESSION, kind="XHR"),
            script_error(),
        )
        page.push(OOP, script_error())

    page = page_with_button(submitted)
    service, sid = start(tmp_path, page)
    push_all(
        page,
        PAGE,
        request("old", SITE + "/api/cart", at=earlier),
        response("old", SITE + "/api/cart", 503),
        script_error(at=earlier),
    )
    service.click("owner", sid, None, expected_backend_node_id=14)
    # The answers arrive after the click returned, while the page is being observed.
    push_all(
        page,
        PAGE,
        response("order", ORDER, 500),
        response("pixel", ADS, 404),
        failed("renew", "net::ERR_CONNECTION_RESET", kind="XHR"),
    )
    report = service.page_report("owner", sid)
    assert report == {
        "session_id": sid,
        "failed_requests": [
            {"method": "POST", "path": "/api/orders/:id/items", "status": 500},
            {"method": "GET", "path": "/api/session/:id/renew", "failure": "net::ERR_CONNECTION_RESET"},
        ],
        "more_failed_requests": 0,
        "uncaught_errors": 1,
    }  # fmt: skip
    # Nothing private: no query, fragment, body, cookie, header, console text or other site.
    written = json.dumps(report)
    for private in ("SECRET-COUPON", "token", "receipt", "4111", "sid=", "other-site", "TypeError"):
        assert private not in written
    # Read once: the next read (with no step since) is empty.
    assert service.page_report("owner", sid)["failed_requests"] == []


def test_a_clean_step_reports_nothing(tmp_path):
    def saved(_values):
        push_all(
            page,
            PAGE,
            request("save", SITE + "/api/profile/88", method="PUT"),
            response("save", SITE + "/api/profile/88", 204),
            finished("save"),
            # The page itself cancelled a request (a navigation ends them): not a failure.
            request("search", SITE + "/api/suggest", kind="XHR"),
            failed("search", "net::ERR_ABORTED", kind="XHR", canceled=True),
            # Images and scripts are not the page's work requests.
            request("logo", SITE + "/img/logo.png", kind="Image"),
            response("logo", SITE + "/img/logo.png", 404, kind="Image"),
        )

    page = page_with_button(saved)
    service, sid = start(tmp_path, page)
    service.click("owner", sid, None, expected_backend_node_id=14)
    assert service.page_report("owner", sid) == {
        "session_id": sid,
        "failed_requests": [],
        "more_failed_requests": 0,
        "uncaught_errors": 0,
    }


class EnterSubmits(FakePage):
    """A form that is sent (a document request) when Enter is pressed in its field."""

    def __init__(self, status: int) -> None:
        super().__init__()
        self.status = status

    def _Input_dispatchKeyEvent(self, values, session):
        if values["type"] == "keyDown" and values.get("key") == "Enter":
            address = SITE + quote("/λογαριασμός/2041") + "?q=" + quote("Αθηνά")
            push_all(
                self,
                PAGE,
                request("form", address, method="post", kind="Document", same_site=None),
                response("form", address, self.status, kind="Document"),
            )
        return super()._Input_dispatchKeyEvent(values, session)


def test_filling_and_submitting_a_form_reports_its_failed_send(tmp_path):
    # The browser does not say whether the request is the page's own site here, so it counts
    # only from the page's own origin. A path in another script stays as the browser sent it.
    page = EnterSubmits(502)
    page.add(PAGE, 22, Node("main-frame", "INPUT", [(40, 20, 160, 20)], kind="field"))
    service, sid = start(tmp_path, page)
    service.fill("owner", sid, None, "Αθηνά 雅典", expected_backend_node_id=22, submit=True)
    assert service.page_report("owner", sid)["failed_requests"] == [
        {"method": "POST", "path": quote("/λογαριασμός/") + ":id", "status": 502}
    ]


def test_a_burst_of_failures_lists_five_and_counts_the_rest(tmp_path):
    def burst(_values):
        for number in range(8):
            address = f"{SITE}/api/items/{number}"
            push_all(
                page, PAGE, request(f"r{number}", address), response(f"r{number}", address, 404)
            )

    page = page_with_button(burst)
    service, sid = start(tmp_path, page)
    service.click("owner", sid, None, expected_backend_node_id=14)
    report = service.page_report("owner", sid)
    assert len(report["failed_requests"]) == 5 and report["more_failed_requests"] == 3
    assert {item["path"] for item in report["failed_requests"]} == {"/api/items/:id"}


def test_a_reading_operation_opens_no_report_of_its_own(tmp_path):
    def submitted(_values):
        page.push(PAGE, request("order", ORDER, method="POST"))

    page = page_with_button(submitted)
    service, sid = start(tmp_path, page)
    service.click("owner", sid, None, expected_backend_node_id=14)
    service.hover("owner", sid, None, expected_backend_node_id=14)  # reading, not acting
    page.push(PAGE, response("order", ORDER, 500))
    assert service.page_report("owner", sid)["failed_requests"] == [
        {"method": "POST", "path": "/api/orders/:id/items", "status": 500}
    ]


@pytest.mark.parametrize(
    "url,path",
    [
        (SITE + "/api/orders/104233?x=1#y", "/api/orders/:id"),
        (SITE + "/v2/users/3f2504e0-4f89-11d3-9a0c-0305e82c3301/2fa", "/v2/users/:id/2fa"),
        (SITE + "/files/report-5f8d0d55b54764421b7156c9.pdf", "/files/report-:id.pdf"),
        (SITE + "/reset/Zm9vYmFyYmF6cXV4MTIzNDU2Nzg", "/reset/:id"),
        (SITE + quote("/el/καλάθι/2"), quote("/el/καλάθι/") + ":id"),
        (SITE + quote("/zh/购物车 1"), quote("/zh/购物车 ") + ":id"),
        ("https://user:pass@shop.example.test/", "/"),
        ("data:text/plain,hello", None),
    ],
)  # fmt: skip
def test_a_reported_path_keeps_no_query_credentials_or_ids(url, path):
    assert mb._reported_path(url) == path


def test_choosing_several_options_reports_what_the_site_answered(tmp_path):
    from test_ide_managed_browser_more import MorePage, multiple_select

    class SavesFilters(MorePage):
        """A list whose change saves the person's filter (the site's own request)."""

        def _Runtime_callFunctionOn(self, values, session):
            result = super()._Runtime_callFunctionOn(values, session)
            if values["functionDeclaration"] == mb._SELECT_OPTIONS_FUNCTION:
                self.push(PAGE, request("filter", SITE + "/api/filters/31", method="PUT"))
            return result

    page = SavesFilters()
    multiple_select(page)
    service, sid = start(tmp_path, page)
    service.select_options(
        "owner", sid, None, expected_backend_node_id=40, option_backend_node_ids=[41, 43]
    )
    page.push(PAGE, response("filter", SITE + "/api/filters/31", 500))
    assert service.page_report("owner", sid)["failed_requests"] == [
        {"method": "PUT", "path": "/api/filters/:id", "status": 500}
    ]
