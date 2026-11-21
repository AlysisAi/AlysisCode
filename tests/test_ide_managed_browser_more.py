"""The managed browser's further operations, against the fake page of the hands tests: going
back, forward and reloading without sending a form again, right/middle/double clicks, choosing
several options of a list, images of one element or the whole page, and a file chooser a click
opens, held and answered with a file."""

from __future__ import annotations

import hashlib
import struct
from typing import Any
from urllib.parse import quote

import pytest
from test_ide_managed_browser_hands import OOP, PAGE, FakePage, Node, mouse, start

from alysis_code.ide import managed_browser as mb
from alysis_code.ide.managed_browser import (
    BrowserLimitError,
    BrowserNotFoundError,
    BrowserSecurityError,
    BrowserTargetHiddenError,
    BrowserValidationError,
)

ORDERS = "https://example.test/" + quote("παραγγελίες")
DONE = "https://example.test/" + quote("ολοκληρώθηκε")


def png(width: int, height: int, size: int) -> bytes:
    """A PNG as far as the service reads one (signature and size), ``size`` bytes long."""
    head = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", width, height)
    return head + b"\x00" * max(0, size - len(head))


class MorePage(FakePage):
    """The fake page with a tab history, file choosers, images and multiple selects."""

    HANDLED = FakePage.HANDLED | {
        "Page.getNavigationHistory",
        "Page.navigateToHistoryEntry",
        "Page.reload",
        "Page.captureScreenshot",
    }

    def __init__(self, **options) -> None:
        super().__init__(**options)
        self.loader = "loader-1"
        self.history: list[dict[str, Any]] = [
            {"id": 1, "url": "about:blank", "transitionType": "auto_toplevel"},
            {"id": 2, "url": ORDERS, "transitionType": "typed"},
            {"id": 3, "url": DONE, "transitionType": "form_submit"},
        ]
        self.index = 2
        self.url = DONE
        self.loads = 0
        self.stays = False  # the page asks to stay when left (unsaved changes)
        self.page_menu = False  # the page handles a right click with its own menu
        self.content = (1280.0, 3000.0)
        self.scrolled = (0, 0)
        self.bytes_per_pixel = 0.05
        self.fixed_size: int | None = None  # an image that does not get smaller when scaled
        self.selected: dict[int, bool] = {}

    def _Page_getFrameTree(self, values, session):
        tree = super()._Page_getFrameTree(values, session)
        if session != OOP:
            tree["frameTree"]["frame"]["loaderId"] = self.loader
        return tree

    def _Page_getNavigationHistory(self, values, session):
        return {"currentIndex": self.index, "entries": [dict(item) for item in self.history]}

    def _go(self, index: int) -> None:
        if self.stays:
            self.opens_dialog("beforeunload", "")(None)
            return
        self.index = index
        self.url = self.history[index]["url"]
        self.loads += 1
        self.loader = f"loader-{self.loads + 1}"

    def _Page_navigateToHistoryEntry(self, values, session):
        self._go(next(i for i, item in enumerate(self.history) if item["id"] == values["entryId"]))
        return {}

    def _Page_reload(self, values, session):
        self.history[self.index]["transitionType"] = "reload"
        self._go(self.index)
        return {}

    def _Page_getLayoutMetrics(self, values, session):
        metrics = super()._Page_getLayoutMetrics(values, session)
        metrics["cssVisualViewport"] = {"pageX": self.scrolled[0], "pageY": self.scrolled[1]}
        metrics["cssContentSize"] = {"width": self.content[0], "height": self.content[1]}
        return metrics

    def _Page_captureScreenshot(self, values, session):
        clip = values["clip"]
        width = round(clip["width"] * clip["scale"])
        height = round(clip["height"] * clip["scale"])
        size = self.fixed_size or int(width * height * self.bytes_per_pixel)
        data = png(width, height, size)
        return {"data": mb.base64.b64encode(data).decode()}

    def _Runtime_evaluate(self, values, session):
        if values["expression"] == "document.documentElement":
            return {"result": {"objectId": f"{session}|root|main-frame"}}
        return super()._Runtime_evaluate(values, session)

    def _Runtime_callFunctionOn(self, values, session):
        function = values["functionDeclaration"]
        if function == mb._CONTEXT_MENU_WATCH_FUNCTION:
            return {"result": {"objectId": "menu-watch"}}
        if function == mb._CONTEXT_MENU_HANDLED_FUNCTION:
            return {"result": {"value": {"fired": True, "handled": self.page_menu}}}
        if function == mb._SELECT_OPTIONS_FUNCTION:
            select = self.nodes[self.object_node(values["objectId"])]
            chosen = [self.object_node(item["objectId"]) for item in values["arguments"]]
            if select.kind != "multiple":
                return {"result": {"value": {"dispatched": False, "multiple": False}}}
            if any(self.nodes[item].kind == "disabled" for item in chosen):
                return {"result": {"value": {"dispatched": False, "multiple": True}}}
            before = dict(self.selected)
            for child in select.children:
                self.selected[child] = (session, child) in chosen
            return {
                "result": {
                    "value": {
                        "dispatched": True,
                        "multiple": True,
                        "changed": before != self.selected,
                        "confirmed": True,
                        "selected": [
                            self.nodes[(session, child)].value
                            for child in select.children
                            if self.selected[child]
                        ],
                    }
                }
            }
        if function == mb._IN_LINK_FUNCTION:
            node = self.nodes[self.object_node(values["objectId"])]
            return {"result": {"value": node.name == "A"}}
        return super()._Runtime_callFunctionOn(values, session)

    @staticmethod
    def object_node(object_id: str) -> tuple[str, int]:
        session, backend, _world = object_id.split("|")
        return session, int(backend)

    def opens_chooser(self, *, session: str = PAGE, frame: str = "main-frame", backend: int = 70):
        """A click handler that opens a file chooser for file input ``backend``, as Chromium
        reports one while the page holds choosers."""

        def click(_values):
            if ("Page.setInterceptFileChooserDialog", {"enabled": True}, session) in self.calls:
                self.push(
                    session,
                    {
                        "method": "Page.fileChooserOpened",
                        "params": {
                            "frameId": frame,
                            "mode": "selectSingle",
                            "backendNodeId": backend,
                        },
                    },
                )

        return click


def scripts(page: FakePage, function: str) -> list[tuple[str, dict[str, Any], str | None]]:
    """The calls that ran this fixed function."""
    return [call for call in page.calls if call[1].get("functionDeclaration") == function]


def presses(page: FakePage) -> list[tuple[str, str, int, int | None]]:
    return [
        (v["type"], v["button"], v["clickCount"], v.get("buttons"))
        for _, v, _ in page.sent("Input.dispatchMouseEvent")
    ]


# ---------------------------------------------------------------------------------------------
# Back, forward and reload


def test_back_and_forward_move_through_the_tabs_history_and_wait_for_the_page(tmp_path):
    page = MorePage()
    service, sid = start(tmp_path, page)
    moved = service.history("owner", sid, "back")
    assert moved["moved"] is True and moved["url"] == ORDERS and "load_pending" not in moved
    assert ("Page.navigateToHistoryEntry", {"entryId": 2}, PAGE) in page.calls
    assert page.sent("Input.") == []  # moving sends no input to the page
    forward = service.history("owner", sid, "forward")
    assert (forward["moved"], forward["url"], page.index) == (True, DONE, 2)
    with pytest.raises(BrowserValidationError, match="no later page"):
        service.history("owner", sid, "forward")
    page.index = 0
    with pytest.raises(BrowserValidationError, match="no earlier page"):
        service.history("owner", sid, "back")
    with pytest.raises(BrowserValidationError):
        service.history("owner", sid, "sideways")


def test_a_page_that_came_from_sending_a_form_is_never_reloaded(tmp_path):
    # Chromium's DevTools reload sends a form's result page's form again without asking.
    page = MorePage()
    service, sid = start(tmp_path, page)
    with pytest.raises(BrowserValidationError, match="send that form again"):
        service.history("owner", sid, "reload")
    assert page.sent("Page.reload") == [] and page.loads == 0
    # A page reached otherwise reloads, and may reload again (its entry then says "reload").
    page.index, page.url = 1, ORDERS
    for _ in range(2):
        assert service.history("owner", sid, "reload")["moved"] is True
    assert len(page.sent("Page.reload")) == 2 and page.history[1]["transitionType"] == "reload"
    # An entry reloaded by someone else hides where it came from: not reloaded.
    page.history[2]["transitionType"] = "reload"
    page.index, page.url = 2, DONE
    with pytest.raises(BrowserValidationError, match="send that form again"):
        service.history("owner", sid, "reload")
    assert len(page.sent("Page.reload")) == 2


def test_a_page_asking_to_stay_is_answered_stay_and_nothing_moves(tmp_path):
    page = MorePage()
    page.stays = True
    service, sid = start(tmp_path, page)
    result = service.history("owner", sid, "back")
    assert result["moved"] is False and page.index == 2
    assert [(item["type"], item["accepted"]) for item in result["dialogs"]] == [
        ("beforeunload", False)
    ]
    assert page.dialog_answers == [({"accept": False}, PAGE)]


def test_a_history_entry_the_address_policy_refuses_is_never_opened(tmp_path):
    page = MorePage()
    page.history[1]["url"] = "http://10.0.0.7/admin"  # a private address
    service, sid = start(tmp_path, page)
    with pytest.raises(BrowserSecurityError):
        service.history("owner", sid, "back")
    assert page.sent("Page.navigateToHistoryEntry") == [] and page.index == 2


# ---------------------------------------------------------------------------------------------
# Right, middle, double and triple clicks


def test_a_right_click_opens_the_pages_own_menu_and_never_the_browsers(tmp_path):
    page = MorePage()
    page.page_menu = True
    page.add(PAGE, 90, Node("main-frame", "DIV", [(10, 10, 200, 40)]))
    service, sid = start(tmp_path, page)
    # The label rule is for left clicks only: a right click on a control stays on it.
    result = service.click("owner", sid, None, expected_backend_node_id=90, button="right")
    assert result["page_menu"] is True and result["button"] == "right"
    assert presses(page) == [("mousePressed", "right", 1, 2), ("mouseReleased", "right", 1, None)]
    # The watch that keeps the browser's menu closed ran on the element, before the click.
    watch = page.calls.index(scripts(page, mb._CONTEXT_MENU_WATCH_FUNCTION)[0])
    press = page.calls.index(next(c for c in page.calls if c[0] == "Input.dispatchMouseEvent"))
    assert watch < press
    page.page_menu = False
    assert (
        service.click("owner", sid, None, expected_backend_node_id=90, button="right")["page_menu"]
        is False
    )


@pytest.mark.parametrize("count", [2, 3])
def test_double_and_triple_clicks_count_each_press(tmp_path, count):
    page = MorePage()
    page.add(PAGE, 90, Node("main-frame", "DIV", [(10, 10, 200, 40)]))
    service, sid = start(tmp_path, page)
    result = service.click("owner", sid, None, expected_backend_node_id=90, click_count=count)
    assert result["click_count"] == count
    expected = [
        (kind, "left", number, None)
        for number in range(1, count + 1)
        for kind in ("mousePressed", "mouseReleased")
    ]
    assert presses(page) == expected
    assert {(x, y) for _, x, y, _ in mouse(page)} == {(110, 30)}


def test_a_double_click_inside_another_sites_frame_reaches_it_through_its_frame(tmp_path):
    page = MorePage()
    page.add(OOP, 8, Node("oop-frame", "SPAN", [(10, 50, 70, 20)]))
    service, sid = start(tmp_path, page)
    service.click(
        "owner", sid, None, expected_backend_node_id=8, frame_id="oop-frame", click_count=2
    )
    # The frame's <iframe> starts at (0, 300): input goes to the page at the element's point.
    assert mouse(page) == [(kind, 45, 360, PAGE) for kind in ("mousePressed", "mouseReleased")] * 2
    # Choosers were held in the page and in the frame while it ran, and released after.
    held = [(v["enabled"], s) for _, v, s in page.sent("Page.setInterceptFileChooserDialog")]
    assert held == [(True, PAGE), (True, OOP), (False, PAGE), (False, OOP)]


def test_a_middle_click_is_sent_only_to_a_link(tmp_path):
    page = MorePage()
    page.add(PAGE, 90, Node("main-frame", "INPUT", [(10, 10, 200, 20)], kind="field"))
    page.add(PAGE, 91, Node("main-frame", "A", [(10, 40, 200, 20)]))
    service, sid = start(tmp_path, page)
    # On a text field Linux would paste the computer's selected text into it.
    with pytest.raises(BrowserValidationError, match="not a link"):
        service.click("owner", sid, None, expected_backend_node_id=90, button="middle")
    assert page.sent("Input.") == []
    service.click("owner", sid, None, expected_backend_node_id=91, button="middle")
    assert presses(page) == [
        ("mousePressed", "middle", 1, 4),
        ("mouseReleased", "middle", 1, None),
    ]


@pytest.mark.parametrize(
    "options", [{"button": "back"}, {"click_count": 4}, {"click_count": 0}, {"click_count": True}]
)
def test_unclear_clicks_are_refused_before_any_lookup(tmp_path, options):
    page = MorePage()
    service, sid = start(tmp_path, page)
    with pytest.raises(BrowserValidationError):
        service.click("owner", sid, None, expected_backend_node_id=90, **options)
    assert page.calls == []


# ---------------------------------------------------------------------------------------------
# A file chooser a click opens


def test_a_file_chooser_a_click_opens_is_held_reported_and_answered_with_a_file(tmp_path):
    page = MorePage()
    button = page.add(PAGE, 90, Node("main-frame", "BUTTON", [(10, 10, 80, 20)]))
    page.add(PAGE, 70, Node("main-frame", "INPUT", [(0, 0, 0, 0)], visible=False, kind="file"))
    button.on_click = page.opens_chooser()
    service, sid = start(tmp_path, page)
    clicked = service.click("owner", sid, None, expected_backend_node_id=90)
    assert clicked["file_chooser"] == {
        "backend_node_id": 70,
        "frame_id": "main-frame",
        "url": DONE,
        "loader": "loader-1",
        "multiple": False,
    }
    held = [v["enabled"] for _, v, _ in page.sent("Page.setInterceptFileChooserDialog")]
    assert held == [True, False]  # held only while the click ran
    data = "Βεβαίωση εγγραφής".encode()
    answer = {
        "name": "βεβαίωση.pdf",
        "size": len(data),
        "chunks": [data],
        "expected_sha256": hashlib.sha256(data).hexdigest(),
        "expected_backend_node_id": 70,
        "frame_id": "main-frame",
    }
    # The input of a later document (the page loaded again) is never given the file.
    page.loader = "loader-9"
    with pytest.raises(BrowserNotFoundError):
        service.upload_file("owner", sid, None, **answer, expected_frame_loader="loader-1")
    assert page.sent("DOM.setFileInputFiles") == []
    page.loader = "loader-1"
    chosen = service.upload_file("owner", sid, None, **answer, expected_frame_loader="loader-1")
    assert chosen["selected"] is True
    assert page.nodes[(PAGE, 70)].value.endswith("βεβαίωση.pdf")


def test_a_click_that_opens_no_chooser_reports_none(tmp_path):
    page = MorePage()
    page.add(PAGE, 90, Node("main-frame", "BUTTON", [(10, 10, 80, 20)]))
    service, sid = start(tmp_path, page)
    assert "file_chooser" not in service.click("owner", sid, None, expected_backend_node_id=90)


def test_a_file_chooser_of_another_sites_frame_names_that_frame(tmp_path):
    page = MorePage()
    button = page.add(OOP, 8, Node("oop-frame", "BUTTON", [(10, 50, 70, 20)]))
    button.on_click = page.opens_chooser(session=OOP, frame="oop-frame", backend=12)
    service, sid = start(tmp_path, page)
    clicked = service.click("owner", sid, None, expected_backend_node_id=8, frame_id="oop-frame")
    assert clicked["file_chooser"] == {
        "backend_node_id": 12,
        "frame_id": "oop-frame",
        "url": "https://other.test/",
        "loader": "oop-loader-1",
        "multiple": False,
    }


# ---------------------------------------------------------------------------------------------
# Several options of a list


def multiple_select(page: MorePage, session: str = PAGE, frame: str = "main-frame") -> None:
    """A list where several can be chosen: Αθήνα, Πάτρα, Βόλος and a disabled option."""
    names = {41: "Αθήνα", 42: "Πάτρα", 43: "Βόλος", 44: "Λάρισα"}
    page.add(session, 40, Node(frame, "SELECT", [(10, 10, 200, 80)], kind="multiple",
                               children=tuple(names)))  # fmt: skip
    for backend, name in names.items():
        kind = "disabled" if backend == 44 else "option"
        page.add(
            session, backend, Node(frame, "OPTION", [(10, 10, 200, 20)], kind=kind, value=name)
        )
        page.selected[backend] = backend == 42


def test_several_options_of_a_list_are_chosen_in_an_isolated_world(tmp_path):
    page = MorePage()
    multiple_select(page)
    service, sid = start(tmp_path, page)
    result = service.select_options(
        "owner", sid, None, expected_backend_node_id=40, option_backend_node_ids=[41, 43]
    )
    assert (result["changed"], result["confirmed"], result["selected"]) == (
        True,
        True,
        ["Αθήνα", "Βόλος"],
    )
    (call,) = scripts(page, mb._SELECT_OPTIONS_FUNCTION)
    # The list and its options are objects of the frame's isolated world, never page script.
    contexts = {v["executionContextId"] for _, v, _ in page.sent("DOM.resolveNode")}
    assert contexts and all(context in page.worlds for context in contexts)
    assert [item["objectId"] for item in call[1]["arguments"]] == [
        f"{PAGE}|41|main-frame",
        f"{PAGE}|43|main-frame",
    ]
    again = service.select_options(
        "owner", sid, None, expected_backend_node_id=40, option_backend_node_ids=[41, 43]
    )
    assert again["changed"] is False
    with pytest.raises(BrowserValidationError, match="disabled"):
        service.select_options(
            "owner", sid, None, expected_backend_node_id=40, option_backend_node_ids=[44]
        )
    assert page.sent("Input.") == []


def test_choosing_several_options_refuses_a_dropdown_and_another_frames_option(tmp_path):
    page = MorePage()
    multiple_select(page)
    page.add(PAGE, 50, Node("main-frame", "SELECT", kind="single", children=(41,)))
    page.add(OOP, 43, Node("oop-frame", "OPTION", kind="option", value="Άλλο"))
    service, sid = start(tmp_path, page)
    with pytest.raises(BrowserValidationError, match="dropdown takes one option"):
        service.select_options(
            "owner", sid, None, expected_backend_node_id=50, option_backend_node_ids=[41]
        )
    for options in ([], [41, 41], list(range(1, 103))):
        with pytest.raises(BrowserValidationError):
            service.select_options(
                "owner", sid, None, expected_backend_node_id=40, option_backend_node_ids=options
            )
    # An option is found in the list's own frame only: the same number elsewhere is not it.
    before = len(scripts(page, mb._SELECT_OPTIONS_FUNCTION))
    page.nodes.pop((PAGE, 43))
    with pytest.raises(BrowserNotFoundError):
        service.select_options(
            "owner", sid, None, expected_backend_node_id=40, option_backend_node_ids=[43]
        )
    assert len(scripts(page, mb._SELECT_OPTIONS_FUNCTION)) == before


# ---------------------------------------------------------------------------------------------
# Images of one element or the whole page


def test_an_element_image_is_clipped_to_its_box(tmp_path):
    page = MorePage()
    page.scrolled = (0, 1200)
    page.add(PAGE, 90, Node("main-frame", "TABLE", [(10, 50, 300, 120)]))
    service, sid = start(tmp_path, page)
    image = service.capture_image("owner", sid, None, expected_backend_node_id=90)
    ((_, params, session),) = page.sent("Page.captureScreenshot")
    # Its box in the page (where the view is scrolled to), at full scale, within the view.
    assert session == PAGE and params["captureBeyondViewport"] is False
    assert params["clip"] == {"x": 10, "y": 1250, "width": 300, "height": 120, "scale": 1.0}
    assert (image["kind"], image["width"], image["height"], image["truncated"]) == (
        "element",
        300,
        120,
        False,
    )
    assert mb.base64.b64decode(image["png"]).startswith(b"\x89PNG")
    assert ("DOM.scrollIntoViewIfNeeded", {"backendNodeId": 90}, PAGE) in page.calls
    assert page.sent("Input.") == []


def test_an_element_image_in_another_sites_frame_adds_where_the_frame_starts(tmp_path):
    page = MorePage()
    page.add(OOP, 8, Node("oop-frame", "CANVAS", [(10, 50, 70, 20)]))
    service, sid = start(tmp_path, page)
    service.capture_image("owner", sid, None, expected_backend_node_id=8, frame_id="oop-frame")
    ((_, params, _),) = page.sent("Page.captureScreenshot")
    assert (params["clip"]["x"], params["clip"]["y"]) == (10, 350)


def test_a_hidden_element_has_no_image(tmp_path):
    page = MorePage()
    page.add(PAGE, 90, Node("main-frame", "DIV", visible=False))
    service, sid = start(tmp_path, page)
    with pytest.raises(BrowserTargetHiddenError):
        service.capture_image("owner", sid, None, expected_backend_node_id=90)
    assert page.sent("Page.captureScreenshot") == []


def test_a_full_page_image_is_bounded_by_size_and_says_when_it_is_cut(tmp_path):
    page = MorePage()
    page.content = (1280.0, 20_000.0)  # a long page
    page.bytes_per_pixel = 0.25  # at full scale the image is over its allowance
    service, sid = start(tmp_path, page)
    image = service.capture_image("owner", sid, full_page=True)
    shots = [v for _, v, _ in page.sent("Page.captureScreenshot")]
    # From the top, cut at its height limit, past the visible part, scaled down until it fits.
    assert shots[0]["clip"] == {
        "x": 0.0,
        "y": 0.0,
        "width": 1280.0,
        "height": mb._FULL_PAGE_MAX_CSS_HEIGHT,
        "scale": 1.0,
    }
    assert all(shot["captureBeyondViewport"] for shot in shots)
    assert len(shots) == 2 and shots[1]["clip"]["scale"] < 1
    assert len(mb.base64.b64decode(image["png"])) <= 1024 * 1024
    assert (image["kind"], image["truncated"], image["page_height"]) == ("full_page", True, 20000)
    assert image["scale"] == round(shots[1]["clip"]["scale"], 3)
    # An image that never fits is refused after a few tries.
    page.fixed_size = 2 * 1024 * 1024
    with pytest.raises(BrowserLimitError, match="smaller element"):
        service.capture_image("owner", sid, full_page=True)
    assert len(page.sent("Page.captureScreenshot")) == 2 + mb._PAGE_IMAGE_ATTEMPTS


@pytest.mark.parametrize(
    "arguments",
    [{}, {"full_page": True, "expected_backend_node_id": 90}, {"full_page": "yes"}],
)
def test_an_unclear_image_request_is_refused_before_any_lookup(tmp_path, arguments):
    page = MorePage()
    service, sid = start(tmp_path, page)
    with pytest.raises(BrowserValidationError):
        service.capture_image("owner", sid, None, **arguments)
    assert page.calls == []


# ---------------------------------------------------------------------------------------------
# A visual click's button and count


class VisualPage(MorePage):
    """A page whose screenshot never changes, for clicks at a point of a visual frame."""

    def _Page_captureScreenshot(self, values, session):
        if "clip" in values:
            return super()._Page_captureScreenshot(values, session)
        return {"data": mb.base64.b64encode(png(64, 64, 200)).decode()}


def test_a_visual_right_or_double_click_keeps_the_browsers_menu_closed(tmp_path):
    page = VisualPage()
    page.page_menu = True
    service, sid = start(tmp_path, page)
    frame = service.capture_visual_frame("owner", sid)
    result = service.click_visual_frame("owner", sid, frame, 10, 10, button="right")
    assert result["page_menu"] is True
    assert presses(page) == [("mousePressed", "right", 1, 2), ("mouseReleased", "right", 1, None)]
    page.calls.clear()
    frame = service.capture_visual_frame("owner", sid)
    service.click_visual_frame("owner", sid, frame, 10, 10, click_count=2)
    assert [count for _, _, count, _ in presses(page)] == [1, 1, 2, 2]
    frame = service.capture_visual_frame("owner", sid)
    with pytest.raises(BrowserValidationError, match="link"):
        service.click_visual_frame("owner", sid, frame, 10, 10, button="middle")
