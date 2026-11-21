"""The managed browser acting inside frames and shadow roots, waiting for ready elements, and its
newer operations (fill, key chords, typing key by key, hover, check, drag, wait, dialogs, reading
a value), against a small fake page that answers the browser commands the service sends."""

from __future__ import annotations

import copy
import queue
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import pytest
from test_ide_cdp_websocket_transport import FakeWebSocket
from test_ide_managed_browser import FakeTransport, make_service

from alysis_code.ide import managed_browser as mb
from alysis_code.ide.cdp_websocket_transport import CdpCommandError, WebSocketCdpTransport
from alysis_code.ide.managed_browser import (
    BrowserCancelledError,
    BrowserDialogOpenError,
    BrowserInputUnconfirmedError,
    BrowserNoDialogError,
    BrowserNotFoundError,
    BrowserTargetHiddenError,
    BrowserTargetObscuredError,
    BrowserTargetUnstableError,
    BrowserTimeoutError,
    BrowserValidationError,
    BrowserWaitTimeoutError,
)

PAGE = "cdp-session-1"  # the page's own CDP session (FakeTransport attaches it)
OOP = "oop-session"  # an out-of-process iframe's session
OOP_OWNER = 500  # the <iframe> element of the out-of-process frame, in the page


@dataclass
class Node:
    frame: str
    name: str = "BUTTON"
    box: list[tuple[float, float, float, float]] = field(default_factory=lambda: [(0, 0, 100, 20)])
    visible: bool = True
    attached: bool = True
    kind: str = "text"
    value: str = ""
    checked: Any = None
    secret: bool = False
    children: tuple[int, ...] = ()
    shadow: tuple[int, ...] = ()
    covered_by: int | None = None  # what a hit test finds over it, until uncovered
    uncover_after: int = 0  # hit tests before the cover goes (0: never)
    on_click: Any = None

    def quad(self) -> list[float]:
        # A box may move: each measurement takes the next position until the last one.
        x, y, width, height = self.box[0] if len(self.box) == 1 else self.box.pop(0)
        return [x, y, x + width, y, x + width, y + height, x, y + height]


class FakePage(FakeTransport):
    """A page with a main frame, a same-process child frame and an out-of-process frame."""

    HANDLED = {
        "Page.getFrameTree",
        "Page.createIsolatedWorld",
        "DOM.getDocument",
        "DOM.querySelector",
        "DOM.pushNodesByBackendIdsToFrontend",
        "DOM.resolveNode",
        "Runtime.callFunctionOn",
        "DOM.scrollIntoViewIfNeeded",
        "DOM.getBoxModel",
        "Page.getLayoutMetrics",
        "DOM.getNodeForLocation",
        "DOM.describeNode",
        "DOM.getFrameOwner",
        "DOM.focus",
        "Accessibility.getPartialAXTree",
        "Accessibility.getFullAXTree",
        "Input.dispatchMouseEvent",
        "Input.dispatchKeyEvent",
        "Input.insertText",
        "Runtime.evaluate",
        "Page.handleJavaScriptDialog",
        "DOM.setFileInputFiles",
        "DOMSnapshot.captureSnapshot",
    }

    def __init__(self, *, frames_reachable: bool = True) -> None:
        super().__init__()
        self.nodes: dict[tuple[str, int], Node] = {}
        self.selectors: dict[str, int] = {}
        self.worlds: dict[int, tuple[str, str]] = {}
        self.focused: tuple[str, int] | None = None
        self.selected_all = False
        self.texts: dict[str, str] = {}
        self.url = "https://example.test/start"
        self.visibility = "visible"
        self.ready_state = "complete"
        self.ax: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
        self.native_drag = False
        self.dialog_answer: dict[str, Any] | None = None
        self.dialog_answered = threading.Event()
        self.queue: list[tuple[str, dict[str, Any]]] = []
        self.changed = threading.Condition()
        self.hits = 0
        self.takes_files = True
        self.upload_allowances: list[tuple[str, int]] = []
        self.dom: dict[str, dict[str, Any]] = {}  # DOM snapshot per CDP session
        self.gone: set[str] = set()  # sessions whose target went away (commands refused)
        self.dialog_answers: list[tuple[dict[str, Any], str | None]] = []
        if frames_reachable:
            self.guarded_frame_sessions = lambda *, session_id: (
                {"oop-frame": OOP} if session_id == PAGE else {}
            )
        # The out-of-process frame's <iframe> element in the page.
        self.add(PAGE, OOP_OWNER, Node("main-frame", "IFRAME", [(0, 300, 400, 120)]))

    def add(self, session: str, backend: int, node: Node) -> Node:
        self.nodes[(session, backend)] = node
        return node

    def push(self, session: str, event: dict[str, Any]) -> None:
        with self.changed:
            self.queue.append((session, event))
            self.changed.notify_all()

    def drain_events(self, *, session_id, max_events, timeout, cancel=None):
        with self.changed:
            if not any(item[0] == session_id for item in self.queue):
                self.changed.wait(timeout)
            found = [event for session, event in self.queue if session == session_id]
            self.queue = [item for item in self.queue if item[0] != session_id]
            return found[:max_events]

    def call(self, method, params=None, *, session_id=None, timeout, cancel=None):
        if method not in self.HANDLED:
            return super().call(
                method, params, session_id=session_id, timeout=timeout, cancel=cancel
            )
        if cancel is not None and cancel():
            raise BrowserCancelledError("Browser operation was cancelled.")
        values = dict(params or {})
        self.calls.append((method, values, session_id))
        return getattr(self, "_" + method.replace(".", "_"))(values, session_id)

    def node(self, session: str, values: dict[str, Any]) -> Node:
        backend = values.get("backendNodeId", values.get("nodeId"))
        if (session, backend) not in self.nodes:
            raise mb.BrowserError("No node with given id found.")
        return self.nodes[(session, backend)]

    def _Page_getFrameTree(self, values, session):
        if session == OOP:
            return {
                "frameTree": {
                    "frame": {
                        "id": "oop-frame",
                        "parentId": "main-frame",
                        "url": "https://other.test/",
                        "securityOrigin": "https://other.test",
                        "loaderId": "oop-loader-1",
                    }
                }
            }
        return {
            "frameTree": {
                "frame": {
                    "id": "main-frame",
                    "url": self.url,
                    "securityOrigin": "https://example.test",
                    "loaderId": "loader-1",
                },
                "childFrames": [
                    {
                        "frame": {
                            "id": "same-frame",
                            "parentId": "main-frame",
                            "url": "https://example.test/form",
                            "securityOrigin": "https://example.test",
                        }
                    }
                ],
            }
        }

    def _Page_createIsolatedWorld(self, values, session):
        context = 100 + len(self.worlds)
        self.worlds[context] = (session, values["frameId"])
        return {"executionContextId": context}

    def _DOM_getDocument(self, values, session):
        return {"root": {"nodeId": 1}}

    def _DOM_querySelector(self, values, session):
        return {"nodeId": self.selectors.get(values["selector"], 0)}

    def _DOM_pushNodesByBackendIdsToFrontend(self, values, session):
        return {
            "nodeIds": [b if (session, b) in self.nodes else 0 for b in values["backendNodeIds"]]
        }

    def _DOM_resolveNode(self, values, session):
        self.node(session, values)
        world = self.worlds.get(values.get("executionContextId"), (session, None))[1]
        return {"object": {"objectId": f"{session}|{values['backendNodeId']}|{world}"}}

    def _Runtime_callFunctionOn(self, values, session):
        function = values["functionDeclaration"]
        if function == mb._DRAG_STARTED_FUNCTION:
            return {"result": {"value": self.native_drag}}
        if function == mb._SELECT_OPTION_FUNCTION:
            return {"result": {"value": {"dispatched": True}}}
        owner_session, backend, world = values["objectId"].split("|")
        node = self.nodes[(owner_session, int(backend))]
        if function == mb._DRAG_WATCH_FUNCTION:
            return {"result": {"objectId": "drag-watch"}}
        if function == mb._FILE_COUNT_FUNCTION:
            return {"result": {"value": len(node.value.split("|")) if node.value else 0}}
        if not node.attached or node.frame != world:
            return {"result": {"value": {"attached": False}}}
        if function == mb._PROBE_FUNCTION:
            return {
                "result": {"value": {"attached": True, "element": True, "visible": node.visible}}
            }
        assert function == mb._READ_FUNCTION
        state = {
            "attached": True,
            "element": True,
            "secret": node.secret,
            "kind": node.kind,
            "value": node.value,
        }
        if node.checked is not None:
            state["checked"] = node.checked
        return {"result": {"value": state}}

    def _DOM_scrollIntoViewIfNeeded(self, values, session):
        self.node(session, values)
        return {}

    def _DOM_getBoxModel(self, values, session):
        return {"model": {"content": self.node(session, values).quad()}}

    def _Page_getLayoutMetrics(self, values, session):
        return {
            "cssVisualViewport": {"pageX": 0, "pageY": 0},
            "cssLayoutViewport": {"clientWidth": 1280, "clientHeight": 800},
        }

    def _DOM_getNodeForLocation(self, values, session):
        self.hits += 1
        found = None
        for (owner, backend), node in self.nodes.items():
            x, y, width, height = node.box[0]
            if (
                owner == session
                and node.attached
                and node.visible
                and x <= values["x"] <= x + width
                and y <= values["y"] <= y + height
            ):
                found = backend
        node = self.nodes.get((session, found))
        if node is not None and node.covered_by is not None:
            if node.uncover_after and self.hits > node.uncover_after:
                node.covered_by = None
            else:
                found = node.covered_by
        return {"backendNodeId": found}

    def tree(self, session: str, backend: int) -> dict[str, Any]:
        node = self.nodes[(session, backend)]
        return {
            "backendNodeId": backend,
            "nodeName": node.name,
            "attributes": ["type", "file"] if node.kind == "file" else [],
            "children": [self.tree(session, child) for child in node.children],
            "shadowRoots": [self.tree(session, child) for child in node.shadow],
        }

    def _DOM_describeNode(self, values, session):
        backend = values.get("backendNodeId", values.get("nodeId"))
        self.node(session, values)
        return {"node": self.tree(session, backend)}

    def _DOM_getFrameOwner(self, values, session):
        assert (values["frameId"], session) == ("oop-frame", PAGE)
        return {"backendNodeId": OOP_OWNER}

    def _DOM_focus(self, values, session):
        self.node(session, values)
        self.focused = (session, values["backendNodeId"])
        return {}

    def _Accessibility_getPartialAXTree(self, values, session):
        backend = values["backendNodeId"]
        focused = self.focused == (session, backend)
        return {
            "nodes": [
                {
                    "backendDOMNodeId": backend,
                    "properties": [{"name": "focused", "value": {"value": focused}}],
                }
            ]
        }

    def _Accessibility_getFullAXTree(self, values, session):
        return {"nodes": self.ax.get((session, values.get("frameId")), [])}

    def _Input_dispatchMouseEvent(self, values, session):
        if values["type"] == "mouseReleased":
            for node in self.nodes.values():
                if node.on_click is not None:
                    node.on_click(values)
        return {}

    def field(self) -> Node:
        assert self.focused is not None
        return self.nodes[self.focused]

    def _Input_dispatchKeyEvent(self, values, session):
        if values["type"] == "keyDown":
            if values.get("commands") == ["selectAll"]:
                self.selected_all = True
            elif values["key"] == "Backspace" and self.selected_all:
                self.field().value, self.selected_all = "", False
            elif values.get("text") not in (None, "\r"):
                self.field().value += values["text"]
        return {}

    def _Input_insertText(self, values, session):
        # The page keeps text as browsers do after input: in composed (NFC) form.
        self.field().value += unicodedata.normalize("NFC", values["text"])
        return {}

    def _Runtime_evaluate(self, values, session):
        expression = values["expression"]
        if expression == "document.visibilityState":
            value: Any = self.visibility
        elif expression == "document.readyState":
            value = self.ready_state
        elif "requestAnimationFrame" in expression:
            value = True
        else:
            assert "innerText" in expression
            frame = self.worlds.get(values.get("contextId"), (session, "main-frame"))[1]
            value = self.texts.get(frame, "")
        return {"result": {"value": value}}

    def _DOM_setFileInputFiles(self, values, session):
        node = self.node(session, values)
        if self.takes_files:
            node.value = "|".join(values["files"])
        return {}

    def allow_file_upload(self, session_id: str, size: int) -> None:
        self.upload_allowances.append((session_id, size))

    def _DOMSnapshot_captureSnapshot(self, values, session):
        if session in self.gone:
            raise CdpCommandError("DOMSnapshot.captureSnapshot", -32001)
        return copy.deepcopy(self.dom.get(session, {"documents": [{"nodes": [1, 2, 3]}]}))

    def _Page_handleJavaScriptDialog(self, values, session):
        self.dialog_answer = values
        self.dialog_answers.append((values, session))
        self.push(session, {"method": "Page.javascriptDialogClosed", "params": {}})
        self.dialog_answered.set()
        return {}

    def opens_dialog(
        self,
        kind: str,
        message: str,
        default: str = "",
        *,
        session: str = PAGE,
        frame: str = "main-frame",
        url: str | None = None,
    ) -> Any:
        """A click handler that opens a JavaScript dialog, in the frame ``frame`` (shown on its
        CDP ``session``, at ``url``), as Chromium reports one: the page then answers nothing
        until the dialog is answered, and an unanswered command times out (ending the
        connection)."""

        def click(_values):
            self.push(
                session,
                {
                    "method": "Page.javascriptDialogOpening",
                    "params": {
                        "url": url or self.url,
                        "frameId": frame,
                        "type": kind,
                        "message": message,
                        "defaultPrompt": default,
                    },
                },
            )
            if not self.dialog_answered.wait(2):
                raise BrowserTimeoutError("Browser CDP command timed out.")

        return click

    def sent(self, prefix: str) -> list[tuple[str, dict[str, Any], str | None]]:
        return [call for call in self.calls if call[0].startswith(prefix)]


def start(tmp_path, page: FakePage, **overrides):
    service, executable, *_ = make_service(tmp_path, transport=page, **overrides)
    session_id = service.start("owner", executable_path=executable).session_id
    page.calls.clear()  # start-up commands are not what these tests check
    return service, session_id


def mouse(page: FakePage) -> list[tuple[str, float, float, str | None]]:
    return [(v["type"], v["x"], v["y"], s) for _, v, s in page.sent("Input.dispatchMouseEvent")]


def test_a_button_in_an_open_shadow_root_is_clicked_by_its_identity(tmp_path):
    # <x-card> holds the button inside its shadow root: no selector reaches it, its identity
    # does. The hit test finds the button's inner label, inside the button: that is the button.
    page = FakePage()
    page.add(PAGE, 12, Node("main-frame", "X-CARD", [(0, 0, 300, 100)], shadow=(14,)))
    page.add(PAGE, 14, Node("main-frame", "BUTTON", [(10, 10, 80, 20)], children=(16,)))
    page.add(PAGE, 16, Node("main-frame", "SPAN", [(20, 12, 40, 16)]))
    service, sid = start(tmp_path, page)
    result = service.click("owner", sid, None, expected_backend_node_id=14)
    assert result["clicked"] is True
    assert mouse(page) == [("mousePressed", 50, 20, PAGE), ("mouseReleased", 50, 20, PAGE)]
    # The shadow host itself: a click lands inside its shadow tree, which belongs to it.
    service.click("owner", sid, None, expected_backend_node_id=12)
    assert len(mouse(page)) == 4


def test_a_form_inside_a_same_process_iframe_is_filled_and_read_by_identity(tmp_path):
    page = FakePage()
    page.add(PAGE, 21, Node("same-frame", "INPUT", [(40, 140, 160, 20)], kind="field"))
    # The same identity number in the main document is a different element: never typed into.
    page.add(PAGE, 22, Node("main-frame", "INPUT", [(40, 20, 160, 20)], kind="field"))
    service, sid = start(tmp_path, page)
    with pytest.raises(BrowserNotFoundError, match="fresh snapshot"):
        service.fill("owner", sid, None, "Ada", expected_backend_node_id=21)  # main frame
    assert page.sent("Input.") == []
    result = service.fill(
        "owner", sid, None, "Ada", expected_backend_node_id=21, frame_id="same-frame"
    )
    assert result["filled"] and result["value_matches"] and not result["submitted"]
    assert ("DOM.focus", {"backendNodeId": 21}, PAGE) in page.calls
    assert page.sent("Input.insertText") == [("Input.insertText", {"text": "Ada"}, PAGE)]
    # The probe and the read ran in that frame's isolated world.
    assert any(v["frameId"] == "same-frame" for _, v, _ in page.sent("Page.createIsolatedWorld"))
    read = service.read_value(
        "owner", sid, None, expected_backend_node_id=21, frame_id="same-frame"
    )
    assert (read["kind"], read["value"], read["truncated"]) == ("field", "Ada", False)
    with pytest.raises(BrowserNotFoundError, match="frame is no longer"):
        service.click("owner", sid, None, expected_backend_node_id=21, frame_id="gone-frame")


def test_a_form_in_an_out_of_process_iframe_is_reached_through_its_frame(tmp_path):
    # The frame's own session holds the form; its <iframe> starts at (0, 300) in the page.
    page = FakePage()
    page.visibility = "hidden"  # a background page drops mouse input into such frames
    page.add(OOP, 8, Node("oop-frame", "BUTTON", [(10, 50, 70, 20)]))
    page.add(OOP, 9, Node("oop-frame", "INPUT", [(10, 80, 70, 20)], kind="field"))
    page.add(PAGE, 8, Node("main-frame", "A", [(500, 500, 10, 10)]))  # same number, other process
    service, sid = start(tmp_path, page)
    service.click("owner", sid, None, expected_backend_node_id=8, frame_id="oop-frame")
    # Each frame's hit test agrees: the page finds the iframe, the frame finds the button.
    hits = [(s, v["x"], v["y"]) for _, v, s in page.sent("DOM.getNodeForLocation")]
    assert hits[-2:] == [(PAGE, 45, 360), (OOP, 45, 60)]
    assert ("Page.bringToFront", {}, PAGE) in page.calls
    assert mouse(page) == [("mousePressed", 45, 360, PAGE), ("mouseReleased", 45, 360, PAGE)]
    assert ("DOM.getBoxModel", {"backendNodeId": 8}, OOP) in page.calls
    assert ("Page.enable", {}, OOP) in page.calls  # its dialogs are reported on its session
    result = service.fill(
        "owner", sid, None, "Ada", expected_backend_node_id=9, frame_id="oop-frame"
    )
    assert result["value_matches"] and ("DOM.focus", {"backendNodeId": 9}, OOP) in page.calls
    assert page.nodes[(OOP, 9)].value == "Ada"


def test_an_out_of_process_frame_is_refused_when_the_connection_cannot_reach_it(tmp_path):
    page = FakePage(frames_reachable=False)
    page.add(OOP, 8, Node("oop-frame"))
    service, sid = start(tmp_path, page)
    with pytest.raises(BrowserNotFoundError, match="separate browser process"):
        service.click("owner", sid, None, expected_backend_node_id=8, frame_id="oop-frame")
    assert page.sent("Input.") == []


def test_a_covered_button_is_clicked_once_its_cover_goes(tmp_path):
    page = FakePage()
    page.add(PAGE, 90, Node("main-frame", covered_by=99, uncover_after=3))
    service, sid = start(tmp_path, page, actionable_wait_seconds=2.0)
    service.click("owner", sid, None, expected_backend_node_id=90)
    assert page.hits > 3  # it waited for the cover to go instead of refusing at once
    assert [kind for kind, *_ in mouse(page)] == ["mousePressed", "mouseReleased"]


@pytest.mark.parametrize(
    "node,error",
    [
        (Node("main-frame", covered_by=99), BrowserTargetObscuredError),
        (Node("main-frame", visible=False), BrowserTargetHiddenError),
        (
            Node("main-frame", box=[(0, y, 100, 20) for y in range(0, 4000, 10)]),
            BrowserTargetUnstableError,
        ),
    ],
)
def test_input_is_refused_with_the_reason_when_the_element_never_gets_ready(tmp_path, node, error):
    page = FakePage()
    page.add(PAGE, 90, node)
    service, sid = start(tmp_path, page)
    for operation in (
        lambda: service.click("owner", sid, None, expected_backend_node_id=90),
        lambda: service.hover("owner", sid, None, expected_backend_node_id=90),
    ):
        with pytest.raises(error, match="No (click|input) was sent"):
            operation()
    assert page.sent("Input.") == []


@pytest.mark.parametrize("dialog", ["accept", "dismiss"])
def test_a_confirm_dialog_opened_by_a_click_is_answered_and_reported(tmp_path, dialog):
    page = FakePage()
    button = page.add(PAGE, 90, Node("main-frame"))
    button.on_click = page.opens_dialog("confirm", "Διαγραφή; Delete this item?")
    service, sid = start(tmp_path, page)
    result = service.click("owner", sid, None, expected_backend_node_id=90, dialog=dialog)
    assert page.dialog_answer == {"accept": dialog == "accept"}
    # Reported with the frame that showed it (its address and id).
    assert result["dialogs"] == [
        {
            "type": "confirm",
            "message": "Διαγραφή; Delete this item?",
            "accepted": dialog == "accept",
            "url": "https://example.test/start",
            "frame_id": "main-frame",
        }
    ]
    assert service.status("owner", sid).state == "running"


def test_a_prompt_is_answered_with_the_given_text_or_its_default(tmp_path):
    page = FakePage()
    button = page.add(PAGE, 90, Node("main-frame"))
    button.on_click = page.opens_dialog("prompt", "Όνομα;", default="—")
    service, sid = start(tmp_path, page)
    service.click(
        "owner", sid, None, expected_backend_node_id=90, dialog="accept", dialog_text="Νίκος"
    )
    assert page.dialog_answer == {"accept": True, "promptText": "Νίκος"}
    page.dialog_answered.clear()
    service.click("owner", sid, None, expected_backend_node_id=90, dialog="accept")
    assert page.dialog_answer == {"accept": True, "promptText": "—"}


def test_a_dialog_the_page_opened_by_itself_is_answered_on_request(tmp_path):
    page = FakePage()
    service, sid = start(tmp_path, page)
    with pytest.raises(BrowserNoDialogError):
        service.handle_dialog("owner", sid, accept=True)
    page.push(
        PAGE,
        {
            "method": "Page.javascriptDialogOpening",
            "params": {"type": "alert", "message": "Αποθηκεύτηκε"},
        },
    )
    result = service.handle_dialog("owner", sid, accept=True)
    assert result["dialog"] == {"type": "alert", "message": "Αποθηκεύτηκε", "accepted": True}
    assert page.dialog_answer == {"accept": True}
    with pytest.raises(BrowserNoDialogError):
        service.handle_dialog("owner", sid, accept=False)


def test_a_greek_labelled_form_is_filled_checked_and_its_confirmation_awaited(tmp_path):
    # Όνομα (name) and Συμφωνώ (I agree): typed text in decomposed form still matches the
    # page's composed value, and the confirmation is found without its accents in capitals.
    page = FakePage()
    page.add(PAGE, 31, Node("main-frame", "INPUT", [(100, 10, 200, 20)], kind="field"))
    agree = page.add(
        PAGE, 32, Node("main-frame", "INPUT", [(100, 40, 16, 16)], kind="checkbox", checked=False)
    )
    page.selectors = {"#onoma": 31}

    def agreed(_values):
        agree.checked = True

    agree.on_click = agreed
    service, sid = start(tmp_path, page)
    typed = unicodedata.normalize("NFD", "Αθηνά Παπαδοπούλου")
    result = service.fill("owner", sid, "#onoma", typed, expected_backend_node_id=31, submit=True)
    assert result["value_matches"] and result["submitted"]
    assert page.nodes[(PAGE, 31)].value == "Αθηνά Παπαδοπούλου"
    enter = [v for _, v, _ in page.sent("Input.dispatchKeyEvent") if v["key"] == "Enter"]
    assert [v["type"] for v in enter] == ["keyDown", "keyUp"] and enter[0]["text"] == "\r"
    assert service.set_checked("owner", sid, None, True, expected_backend_node_id=32)["changed"]
    page.texts["main-frame"] = "ΕΠΙΒΕΒΑΙΩΣΗ ΠΑΡΑΓΓΕΛΙΑΣ"
    page.url = (
        "https://example.test/%CF%80%CE%B1%CF%81%CE%B1%CE%B3%CE%B3%CE%B5%CE%BB%CE%AF%CE%B1/ok"
    )
    assert service.wait_for("owner", sid, "text", value="Επιβεβαίωση παραγγελίας")["satisfied"]
    assert service.wait_for("owner", sid, "address", value="/παραγγελία/")["satisfied"]


def test_fill_refuses_to_type_into_a_field_that_keeps_its_text(tmp_path):
    page = FakePage()
    page.add(PAGE, 31, Node("main-frame", "INPUT", kind="field", value="kept"))
    page._Input_dispatchKeyEvent = lambda values, session: {}  # the page ignores the clearing
    service, sid = start(tmp_path, page)
    with pytest.raises(BrowserInputUnconfirmedError, match="nothing was typed"):
        service.fill("owner", sid, None, "new", expected_backend_node_id=31)
    assert page.sent("Input.insertText") == []


def test_set_checked_reads_the_state_and_confirms_the_click(tmp_path):
    page = FakePage()
    page.add(PAGE, 32, Node("main-frame", "INPUT", kind="checkbox", checked=True))
    page.add(PAGE, 33, Node("main-frame", "INPUT", [(0, 30, 16, 16)], kind="radio", checked=True))
    page.add(PAGE, 34, Node("main-frame", "DIV", [(0, 60, 40, 16)], kind="switch", checked=False))
    page.add(PAGE, 35, Node("main-frame", "DIV", [(0, 90, 40, 16)]))
    service, sid = start(tmp_path, page)
    assert (
        service.set_checked("owner", sid, None, True, expected_backend_node_id=32)["changed"]
        is False
    )
    with pytest.raises(BrowserValidationError, match="choosing another option"):
        service.set_checked("owner", sid, None, False, expected_backend_node_id=33)
    with pytest.raises(BrowserValidationError, match="no checked state"):
        service.set_checked("owner", sid, None, True, expected_backend_node_id=35)
    assert page.sent("Input.") == []
    # A switch whose click does nothing: the click was sent once and that is said plainly.
    with pytest.raises(BrowserInputUnconfirmedError, match="sent once"):
        service.set_checked("owner", sid, None, True, expected_backend_node_id=34)
    assert len(page.sent("Input.dispatchMouseEvent")) == 2


def test_key_chords_hold_modifiers_and_release_every_key(tmp_path):
    page = FakePage()
    page.add(PAGE, 31, Node("main-frame", "INPUT", kind="field"))
    service, sid = start(tmp_path, page)
    service.press_key(
        "owner", sid, None, "k", modifiers=["Control", "Shift"], expected_backend_node_id=31
    )
    keys = [
        (v["type"], v["key"], v["modifiers"], v.get("text"))
        for _, v, _ in page.sent("Input.dispatchKeyEvent")
    ]
    assert keys == [
        ("keyDown", "Control", 2, None),
        ("keyDown", "Shift", 10, None),
        ("keyDown", "k", 10, None),  # a chord types no text
        ("keyUp", "k", 10, None),
        ("keyUp", "Shift", 2, None),
        ("keyUp", "Control", 0, None),
    ]
    page.calls.clear()
    service.press_key("owner", sid, None, "λ", expected_backend_node_id=31)
    assert [v.get("text") for _, v, _ in page.sent("Input.dispatchKeyEvent")] == ["λ", None]
    # Cancelled between key events: every pressed key is still released.
    page.calls.clear()
    with pytest.raises(BrowserCancelledError):
        service.press_key(
            "owner",
            sid,
            None,
            "Enter",
            modifiers=["Alt"],
            expected_backend_node_id=31,
            cancel=lambda: bool(page.sent("Input.dispatchKeyEvent")),
        )
    events = [(v["type"], v["key"]) for _, v, _ in page.sent("Input.dispatchKeyEvent")]
    assert events[0] == ("keyDown", "Alt") and events[-1] == ("keyUp", "Alt")
    downs = {key for kind, key in events if kind == "keyDown"}
    assert downs <= {key for kind, key in events if kind == "keyUp"}


@pytest.mark.parametrize("key", ["Ctrl+L", "", "ab", "\x07", " "])
def test_key_chords_refuse_unknown_keys_and_modifiers_before_any_lookup(tmp_path, key):
    page = FakePage()
    service, sid = start(tmp_path, page)
    with pytest.raises(BrowserValidationError):
        service.press_key("owner", sid, "input", key)
    with pytest.raises(BrowserValidationError):
        service.press_key("owner", sid, "input", "a", modifiers=["Hyper"])
    assert page.sent("DOM.") == []


def test_type_keys_presses_each_character_of_any_script(tmp_path):
    page = FakePage()
    page.add(PAGE, 31, Node("main-frame", "INPUT", kind="field"))
    service, sid = start(tmp_path, page)
    # Persian with a zero-width non-joiner, Greek, a digit and a line break.
    typed = "می\u200cخواهم Γειά 7\n"
    result = service.type_keys("owner", sid, None, typed, expected_backend_node_id=31)
    assert result["typed"] and result["character_count"] == len(typed) and result["focus_kept"]
    downs = [v for _, v, _ in page.sent("Input.dispatchKeyEvent") if v["type"] == "keyDown"]
    assert [v["key"] for v in downs] == [*"می\u200cخواهم Γειά 7", "Enter"]
    assert (downs[-2]["code"], downs[-2]["windowsVirtualKeyCode"]) == ("Digit7", 55)
    assert downs[-1]["text"] == "\r"
    assert page.nodes[(PAGE, 31)].value == "می\u200cخواهم Γειά 7"
    with pytest.raises(BrowserValidationError):
        service.type_keys("owner", sid, None, "bell\x07", expected_backend_node_id=31)


def test_hover_moves_the_mouse_over_a_ready_element(tmp_path):
    page = FakePage()
    page.add(PAGE, 40, Node("main-frame", "A", [(10, 10, 100, 20)]))
    service, sid = start(tmp_path, page)
    assert service.hover("owner", sid, None, expected_backend_node_id=40)["dispatched"]
    assert mouse(page) == [("mouseMoved", 60, 20, PAGE)]


@pytest.mark.parametrize("native", [True, False])
def test_drag_drops_on_the_target_and_always_releases_the_mouse(tmp_path, native):
    page = FakePage()
    page.native_drag = native
    page.add(PAGE, 41, Node("main-frame", "LI", [(0, 0, 100, 20)]))
    page.add(PAGE, 42, Node("main-frame", "UL", [(0, 200, 100, 100)]))
    if native:
        page.push(
            PAGE,
            {
                "method": "Input.dragIntercepted",
                "params": {"data": {"items": [], "dragOperationsMask": 1}},
            },
        )
    service, sid = start(tmp_path, page)
    result = service.drag(
        "owner", sid, None, None, expected_backend_node_id=41, target_backend_node_id=42
    )
    assert result["native_drag"] is native
    drags = [(v["type"], v["x"], v["y"]) for _, v, _ in page.sent("Input.dispatchDragEvent")]
    assert drags == (
        [(kind, 50, 250) for kind in ("dragEnter", "dragOver", "drop")] if native else []
    )
    events = mouse(page)
    assert events[:2] == [("mouseMoved", 50, 10, PAGE), ("mousePressed", 50, 10, PAGE)]
    assert events[-1] == ("mouseReleased", 50, 250, PAGE)
    assert [v["enabled"] for _, v, _ in page.sent("Input.setInterceptDrags")] == [True, False]
    # Cancelled while the button is down: it is released anyway.
    page.calls.clear()
    with pytest.raises(BrowserCancelledError):
        service.drag(
            "owner",
            sid,
            None,
            None,
            expected_backend_node_id=41,
            target_backend_node_id=42,
            cancel=lambda: any(
                v["type"] == "mousePressed" for _, v, _ in page.sent("Input.dispatchMouseEvent")
            ),
        )
    assert mouse(page)[-1][0] == "mouseReleased"


def test_wait_for_elements_load_and_a_bounded_timeout(tmp_path):
    page = FakePage()
    spinner = page.add(PAGE, 50, Node("main-frame", "DIV"))
    page.selectors = {"#done": 51}
    service, sid = start(tmp_path, page)

    def finish():
        spinner.attached = False
        page.add(PAGE, 51, Node("main-frame", "P"))

    timer = threading.Timer(0.2, finish)
    timer.start()
    try:
        gone = service.wait_for(
            "owner", sid, "element_gone", expected_backend_node_id=50, timeout=2
        )
    finally:
        timer.cancel()
    assert gone["satisfied"] and gone["waited_seconds"] > 0
    assert service.wait_for("owner", sid, "element_present", selector="#done", timeout=1)[
        "satisfied"
    ]
    page.ready_state = "interactive"
    assert service.wait_for("owner", sid, "load", value="domcontentloaded")["satisfied"]
    with pytest.raises(BrowserWaitTimeoutError, match="Nothing was changed"):
        service.wait_for("owner", sid, "load", timeout=0.3)
    with pytest.raises(BrowserWaitTimeoutError):
        service.wait_for("owner", sid, "text", value="never shown", timeout=0.2)
    assert page.sent("Input.") == []


def test_wait_for_text_reads_every_frame(tmp_path):
    page = FakePage()
    page.texts = {"same-frame": "Πληρωμή ολοκληρώθηκε", "oop-frame": "Payment received"}
    service, sid = start(tmp_path, page)
    assert service.wait_for("owner", sid, "text", value="PAYMENT RECEIVED", timeout=1)["satisfied"]
    assert service.wait_for("owner", sid, "text", value="πληρωμη ολοκληρωθηκε", timeout=1)[
        "satisfied"
    ]


@pytest.mark.parametrize(
    "condition,kwargs",
    [
        ("text", {}),
        ("text", {"value": " "}),
        ("text", {"value": "x", "selector": "#a"}),
        ("element_gone", {}),
        ("element_gone", {"selector": "#a", "value": "x"}),
        ("load", {"value": "networkidle"}),
        ("sometime", {}),
    ],
)
def test_wait_for_refuses_unclear_conditions_before_reading_the_page(tmp_path, condition, kwargs):
    page = FakePage()
    service, sid = start(tmp_path, page)
    before = len(page.calls)
    with pytest.raises(BrowserValidationError):
        service.wait_for("owner", sid, condition, **kwargs)
    assert len(page.calls) == before


def ax_node(node_id, role, name="", *, parent=None, children=(), backend=None, **extra):
    """An accessibility node as Chromium reports it, with the fields a page view never reads."""
    node = {
        "nodeId": node_id,
        "ignored": False,
        "ignoredReasons": [{"name": "uninteresting", "value": {"type": "boolean", "value": True}}],
        "role": {"type": "role", "value": role},
        "chromeRole": {"type": "internalRole", "value": 7},
        "name": {
            "type": "computedString",
            "value": name,
            "sources": [{"type": "attribute", "attribute": "aria-label"}] * 4,
        },
        "properties": [
            {"name": "focusable", "value": {"type": "booleanOrUndefined", "value": True}}
        ],
        "childIds": list(children),
        **extra,
    }
    if parent is not None:
        node["parentId"] = parent
    if backend is not None:
        node["backendDOMNodeId"] = backend
    return node


def frame_tree(frame_id, button_name, backend):
    """A frame's tree: its root, a button and the button's text with an inline text box."""
    return [
        ax_node("1", "RootWebArea", frameId=frame_id, children=["2"], backend=1),
        ax_node("2", "button", button_name, parent="1", children=["3"], backend=backend),
        ax_node("3", "StaticText", button_name, parent="2", children=["-9"]),
        ax_node("-9", "InlineTextBox", button_name, parent="3"),
    ]


def test_accessibility_snapshot_is_one_list_over_every_frame_without_a_depth_cap(tmp_path):
    # Node ids "1".."3" repeat in every frame (out-of-process frames number their own nodes):
    # other frames' ids are prefixed by their frame, and each frame's root names its frame.
    page = FakePage()
    page.ax = {
        (PAGE, None): frame_tree("main-frame", "Shadow", 14),
        (PAGE, "same-frame"): frame_tree("same-frame", "Όνομα", 21),
        (OOP, "oop-frame"): frame_tree("oop-frame", "Pay", 8),
    }
    service, sid = start(tmp_path, page)
    snapshot = service.snapshot("owner", sid, kind="accessibility")
    nodes = snapshot["data"]["nodes"]
    assert snapshot["truncated"] is False and snapshot["data"]["omitted_frames"] == 0
    roots = [(node["nodeId"], node["frameId"]) for node in nodes if "parentId" not in node]
    assert roots == [
        ("1", "main-frame"),
        ("same-frame/1", "same-frame"),
        ("oop-frame/1", "oop-frame"),
    ]
    buttons = {
        node["nodeId"]: node["backendDOMNodeId"]
        for node in nodes
        if node["role"]["value"] == "button"
    }
    assert buttons == {"2": 14, "same-frame/2": 21, "oop-frame/2": 8}
    ids = [node["nodeId"] for node in nodes]
    assert len(ids) == len(set(ids))
    assert all("depth" not in v for _, v, _ in page.sent("Accessibility.getFullAXTree"))
    # Each frame is described: which site its document is from, and which document it shows.
    described = {frame["id"]: frame for frame in snapshot["data"]["frames"]}
    assert list(described) == ["main-frame", "same-frame", "oop-frame"]
    assert described["oop-frame"] == {
        "id": "oop-frame",
        "url": "https://other.test/",
        "origin": "https://other.test",
        "loader": "oop-loader-1",
    }
    assert described["main-frame"]["loader"] == "loader-1"


def test_accessibility_snapshot_keeps_what_a_page_view_reads_and_drops_the_rest(tmp_path):
    page = FakePage()
    page.ax[(PAGE, None)] = [
        ax_node("1", "RootWebArea", frameId="main-frame", children=["2", "5"], backend=1),
        {
            **ax_node("2", "textbox", "Όνομα", parent="1", children=["3"], backend=21),
            "value": {"type": "string", "value": "Αθηνά"},
            "properties": [{"name": "focused", "value": {"type": "boolean", "value": True}}],
        },
        ax_node("3", "StaticText", "Αθηνά", parent="2", children=["-1", "-2"]),
        ax_node("-1", "InlineTextBox", "Αθη", parent="3"),
        ax_node("-2", "InlineTextBox", "νά", parent="3"),
        {
            **ax_node("5", "combobox", "城市", parent="1", backend=22),
            "value": {"type": "string", "value": "北京"},
            "ignored": True,
        },
    ]
    service, sid = start(tmp_path, page)
    nodes = {
        node["nodeId"]: node
        for node in service.snapshot("owner", sid, kind="accessibility")["data"]["nodes"]
    }
    assert set(nodes) == {"1", "2", "3", "5"}  # the inline text boxes are gone
    assert nodes["3"]["childIds"] == [] and nodes["1"]["childIds"] == ["2", "5"]
    every_id = {*nodes, *(child for node in nodes.values() for child in node["childIds"])}
    assert every_id == set(nodes) and {n.get("parentId") for n in nodes.values()} <= {*nodes, None}
    field = nodes["2"]
    assert field["nodeId"] == "2" and field["parentId"] == "1" and field["childIds"] == ["3"]
    assert field["ignored"] is False and field["role"]["value"] == "textbox"
    assert (field["name"]["value"], field["value"]["value"], field["backendDOMNodeId"]) == (
        "Όνομα",
        "Αθηνά",
        21,
    )
    assert field["properties"] == [{"name": "focused", "value": {"type": "boolean", "value": True}}]
    assert nodes["1"]["frameId"] == "main-frame"
    assert (nodes["5"]["name"]["value"], nodes["5"]["value"]["value"], nodes["5"]["ignored"]) == (
        "城市",
        "北京",
        True,
    )
    for node in nodes.values():
        assert "ignoredReasons" not in node and "chromeRole" not in node
        assert "sources" not in node["name"]


def test_a_page_over_the_byte_limit_before_trimming_fits_after(tmp_path):
    page = FakePage()
    tree = [ax_node("0", "RootWebArea", frameId="main-frame", backend=1)]
    for index in range(1, 40):
        name = "Παράδειγμα κειμένου 文本示例"
        tree[0]["childIds"].append(str(index))
        tree.append(ax_node(str(index), "StaticText", name, parent="0", children=[f"-{index}"]))
        tree.append(ax_node(f"-{index}", "InlineTextBox", name, parent=str(index)))
    page.ax[(PAGE, None)] = tree
    untrimmed = len(mb.json.dumps({"nodes": tree}, ensure_ascii=False).encode())
    limit = untrimmed // 2
    service, sid = start(tmp_path, page, max_snapshot_bytes=limit)
    snapshot = service.snapshot("owner", sid, kind="accessibility")
    assert untrimmed > limit and snapshot["truncated"] is False and snapshot["size_bytes"] <= limit
    assert len(snapshot["data"]["nodes"]) == 40


def test_frames_that_do_not_fit_the_byte_limit_are_counted_as_omitted(tmp_path):
    page = FakePage()
    page.ax = {
        (PAGE, None): frame_tree("main-frame", "Main", 14),
        (PAGE, "same-frame"): frame_tree("same-frame", "Όνομα " * 40, 21),
        (OOP, "oop-frame"): frame_tree("oop-frame", "Pay", 8),
    }
    # Room for the main frame's tree and the description of the page's own frames (with their
    # security origins, as Chromium reports them), not more.
    service, sid = start(tmp_path, page, max_snapshot_bytes=1050)
    snapshot = service.snapshot("owner", sid, kind="semantic")
    assert snapshot["truncated"] is True and snapshot["data"]["omitted_frames"] == 2
    assert {node.get("frameId") for node in snapshot["data"]["nodes"]} == {"main-frame", None}
    # The out-of-process frame was not added, so it is not described either.
    assert [frame["id"] for frame in snapshot["data"]["frames"]] == ["main-frame", "same-frame"]


class ChurningPage(FakePage):
    """A page whose frames change while it is read, as Chromium answers then: a frame listed a
    moment ago is refused with "invalid params" (-32602) once it left the process it was listed
    in (removed, or moved to another site's process), and an out-of-process frame's session can
    detach."""

    def __init__(self, *, departed=(), detached=(), slow=(), **kwargs):
        super().__init__(**kwargs)
        self.departed = set(departed)  # (session, frame id) the process no longer has
        self.detached = set(detached)  # sessions whose target went away
        self.slow = set(slow)  # (session, frame id) whose read times out

    def _Page_getFrameTree(self, values, session):
        if session in self.detached:
            raise mb.BrowserPageUnavailableError("The browser page has detached.")
        return super()._Page_getFrameTree(values, session)

    def _Accessibility_getFullAXTree(self, values, session):
        if (session, values.get("frameId")) in self.departed:
            raise CdpCommandError("Accessibility.getFullAXTree", -32602)
        if (session, values.get("frameId")) in self.slow:
            raise BrowserTimeoutError("Browser CDP command timed out.")
        return super()._Accessibility_getFullAXTree(values, session)


def greek_frames(page: FakePage) -> None:
    page.ax = {
        (PAGE, None): frame_tree("main-frame", "Αποστολή παραγγελίας", 14),
        (PAGE, "same-frame"): frame_tree("same-frame", "Διαφήμιση", 21),
        (OOP, "oop-frame"): frame_tree("oop-frame", "Πληρωμή με κάρτα", 8),
    }


def test_a_frame_gone_while_the_page_is_read_is_marked_unavailable_and_the_rest_comes_back(
    tmp_path,
):
    # The same-process frame left the page's process between listing the frames and reading
    # its tree. The main frame's tree (in Greek), and the other site's frame, still come back.
    page = ChurningPage(departed={(PAGE, "same-frame")})
    greek_frames(page)
    service, sid = start(tmp_path, page)
    snapshot = service.snapshot("owner", sid, kind="accessibility")
    data = snapshot["data"]
    assert snapshot["truncated"] is False and data["omitted_frames"] == 0
    names = {node["name"]["value"] for node in data["nodes"] if node["role"]["value"] == "button"}
    assert names == {"Αποστολή παραγγελίας", "Πληρωμή με κάρτα"}
    assert {node.get("frameId") for node in data["nodes"]} == {"main-frame", "oop-frame", None}
    described = {frame["id"]: frame for frame in data["frames"]}
    assert list(described) == ["main-frame", "same-frame", "oop-frame"]
    assert described["same-frame"] == {
        "id": "same-frame",
        "url": "https://example.test/form",
        "origin": "https://example.test",
        "loader": "",
        "unavailable": True,
        "error": "CdpCommandError",
        "code": -32602,
    }
    assert "unavailable" not in described["oop-frame"]
    assert "unavailable" not in described["main-frame"]


def test_an_out_of_process_frame_whose_session_detached_is_unavailable_not_a_failure(tmp_path):
    page = ChurningPage(detached={OOP})
    greek_frames(page)
    service, sid = start(tmp_path, page)
    snapshot = service.snapshot("owner", sid, kind="semantic")
    data = snapshot["data"]
    assert snapshot["truncated"] is False and data["omitted_frames"] == 0
    assert {node.get("frameId") for node in data["nodes"]} == {"main-frame", "same-frame", None}
    gone = [frame for frame in data["frames"] if frame.get("unavailable")]
    assert gone == [
        {
            "id": "oop-frame",
            "url": "",
            "origin": "",
            "loader": "",
            "unavailable": True,
            "error": "BrowserPageUnavailableError",
        }
    ]


def test_a_frame_that_moved_to_another_process_is_read_once_where_it_is_now(tmp_path):
    # Listed in the page's process, then refused there: it now runs in another site's process,
    # whose session names it. Read there, described as it is now, never also as gone.
    page = ChurningPage(departed={(PAGE, "oop-frame")})
    greek_frames(page)
    original = page._Page_getFrameTree

    def listed_in_both(values, session):
        tree = original(values, session)
        if session == PAGE:
            tree["frameTree"]["childFrames"].append(
                {
                    "frame": {
                        "id": "oop-frame",
                        "parentId": "main-frame",
                        "url": "about:blank",
                        "securityOrigin": "https://example.test",
                    }
                }
            )
        return tree

    page._Page_getFrameTree = listed_in_both
    service, sid = start(tmp_path, page)
    data = service.snapshot("owner", sid, kind="accessibility")["data"]
    roots = [node["nodeId"] for node in data["nodes"] if "parentId" not in node]
    assert roots == ["1", "same-frame/1", "oop-frame/1"]
    described = [frame for frame in data["frames"] if frame["id"] == "oop-frame"]
    assert described == [
        {
            "id": "oop-frame",
            "url": "https://other.test/",
            "origin": "https://other.test",
            "loader": "oop-loader-1",
        }
    ]


def test_a_frame_read_that_times_out_still_fails_the_observation(tmp_path):
    # Only a frame that is gone is skipped: a slow or broken connection is not about one frame.
    page = ChurningPage(slow={(PAGE, "same-frame")})
    greek_frames(page)
    service, sid = start(tmp_path, page)
    with pytest.raises(BrowserTimeoutError):
        service.snapshot("owner", sid, kind="accessibility")


def test_the_main_frames_tree_is_never_skipped(tmp_path):
    page = ChurningPage(departed={(PAGE, None)})
    greek_frames(page)
    service, sid = start(tmp_path, page)
    with pytest.raises(CdpCommandError):
        service.snapshot("owner", sid, kind="accessibility")


def test_read_value_never_returns_a_private_field(tmp_path):
    page = FakePage()
    page.add(PAGE, 60, Node("main-frame", "INPUT", kind="field", value="hunter2", secret=True))
    page.add(PAGE, 61, Node("main-frame", "INPUT", kind="checkbox", value="on", checked="mixed"))
    service, sid = start(tmp_path, page)
    assert (
        service.read_value("owner", sid, None, expected_backend_node_id=60)["value"]
        == "[private field]"
    )
    box = service.read_value("owner", sid, None, expected_backend_node_id=61)
    assert (box["kind"], box["checked"]) == ("checkbox", "mixed")
    assert page.sent("Input.") == []


@pytest.mark.parametrize(
    "args,kwargs,match",
    [
        ((None,), {}, "selector or by its observed node identity"),
        (("#a",), {"frame_id": "same-frame"}, "top document"),
        ((None,), {"expected_backend_node_id": 5, "frame_id": "bad frame!"}, "frame is invalid"),
        ((None,), {"expected_backend_node_id": 5, "dialog": "later"}, "accept"),
        ((None,), {"expected_backend_node_id": 5, "dialog_text": "x"}, "accepted"),
    ],
)
def test_element_addresses_and_dialog_answers_are_checked_before_any_lookup(
    tmp_path, args, kwargs, match
):
    page = FakePage()
    service, sid = start(tmp_path, page)
    for operation in (service.click, service.hover):
        with pytest.raises(BrowserValidationError, match=match):
            operation("owner", sid, *args, **kwargs)
    assert page.sent("DOM.") == [] and page.sent("Page.getFrameTree") == []


@pytest.mark.parametrize("taken", [True, False])
def test_an_upload_is_confirmed_in_the_file_field_of_its_frame(tmp_path, taken):
    import hashlib

    page = FakePage()
    page.takes_files = taken
    page.add(OOP, 70, Node("oop-frame", "INPUT", [(10, 10, 100, 20)], kind="file"))
    service, sid = start(tmp_path, page)
    data = "σημείωμα".encode()
    upload = lambda: service.upload_file(  # noqa: E731
        "owner",
        sid,
        None,
        name="σημείωμα.txt",
        size=len(data),
        chunks=[data],
        expected_sha256=hashlib.sha256(data).hexdigest(),
        expected_backend_node_id=70,
        frame_id="oop-frame",
    )
    if taken:
        assert upload()["selected"] is True
    else:
        # The browser declined the file without an error: that is reported, not "selected".
        with pytest.raises(BrowserInputUnconfirmedError, match="did not take the file"):
            upload()
    # The frame's own guarded session intercepts the form's upload request.
    assert page.upload_allowances == [(OOP, len(data))]
    assert service.status("owner", sid).artifact_count == (1 if taken else 0)


@pytest.mark.parametrize(
    "inherited,platform,expected",
    [
        ({}, "linux", "C.UTF-8"),
        ({"LANG": "C"}, "linux", "C.UTF-8"),
        ({"LANG": "el_GR.UTF-8"}, "linux", None),
        ({"LC_CTYPE": "zh_CN.utf8"}, "linux", "zh_CN.utf8"),
        ({"LC_ALL": "C"}, "linux", None),  # an explicit choice of the person is kept
        ({}, "darwin", None),
        ({}, "win32", None),
    ],
)
def test_chromium_gets_utf8_file_names_on_linux_without_a_utf8_locale(
    inherited, platform, expected
):
    environment = mb._sanitized_browser_environment(
        {**inherited, "SECRET_TOKEN": "x"}, platform=platform
    )
    assert environment.get("LC_CTYPE") == expected
    assert "SECRET_TOKEN" not in environment
    assert {key: environment[key] for key in inherited} == inherited  # language unchanged


class WireBrowser:
    """A browser at the other end of the real CDP transport: a fake WebSocket whose commands
    are answered from a FakePage model, with the navigation and frame events Chromium sends."""

    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.connection = FakeWebSocket()
        self.transport = WebSocketCdpTransport(self.connection)
        self.commands: list[tuple[str, str | None]] = []
        self.after_load: list[dict[str, Any]] = []
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.answer, daemon=True)
        self.thread.start()

    def answer(self) -> None:
        while not self.stopped.is_set():
            try:
                command = self.connection.outbound.get(timeout=0.02)
            except queue.Empty:
                continue
            method, session = command["method"], command.get("sessionId")
            self.commands.append((method, session))
            if method == "Target.getTargets":
                infos = [{"targetId": "target-1", "type": "page", "url": "about:blank"}]
                self.connection.respond(command, {"targetInfos": infos})
            elif method == "Page.navigate":
                self.connection.respond(command, {"frameId": "main-frame", "loaderId": "loader-1"})
                self.event(
                    PAGE,
                    "Page.lifecycleEvent",
                    {"name": "load", "loaderId": "loader-1", "frameId": "main-frame"},
                )
                for event in self.after_load:
                    self.connection.receive_json(event)
            else:
                try:
                    result = self.page.call(
                        method, command.get("params"), session_id=session, timeout=1
                    )
                except Exception:  # noqa: BLE001 - the browser answers a failed command with an error
                    self.connection.receive_json(
                        {"id": command["id"], "error": {"code": -32000, "message": "failed"}}
                    )
                else:
                    self.connection.respond(command, dict(result))

    def event(self, session: str, method: str, params: dict[str, Any]) -> None:
        self.connection.receive_json({"method": method, "sessionId": session, "params": params})

    def reports(self, frame_session: str) -> bool:
        """Whether the transport names this guarded out-of-process frame of the page."""
        lister = getattr(self.transport, "guarded_frame_sessions", None)
        return callable(lister) and frame_session in lister(session_id=PAGE).values()

    def reports_dialog(self, session: str) -> bool:
        tracker = getattr(self.transport, "open_dialog", None)
        return callable(tracker) and tracker(session_id=session) is not None

    def sent_since(self, mark: int) -> list[tuple[str, str | None]]:
        return self.commands[mark:]

    def close(self) -> None:
        self.stopped.set()
        self.thread.join(timeout=1)


def wait_until(condition, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)


def open_wire(tmp_path, page: FakePage):
    wire = WireBrowser(page)
    service, executable, *_ = make_service(tmp_path, transport=wire.transport)
    session_id = service.start("owner", executable_path=executable).session_id
    service.navigate("owner", session_id, "https://example.test/")
    return wire, service, session_id


def test_an_out_of_process_frame_is_reached_through_the_browser_connection(tmp_path):
    # The real transport guards the frame Chromium attaches, and names it to the service.
    page = FakePage(frames_reachable=False)
    page.add(OOP, 8, Node("oop-frame", "BUTTON", [(10, 50, 70, 20)]))
    wire, service, sid = open_wire(tmp_path, page)
    try:
        wire.event(
            PAGE,
            "Target.attachedToTarget",
            {
                "sessionId": OOP,
                "waitingForDebugger": True,
                "targetInfo": {
                    "targetId": "oop-frame",
                    "type": "iframe",
                    "url": "https://other.test/",
                },
            },
        )
        wait_until(
            lambda: wire.reports(OOP) or ("Runtime.runIfWaitingForDebugger", OOP) in wire.commands
        )
        service.click("owner", sid, None, expected_backend_node_id=8, frame_id="oop-frame")
        assert ("DOM.getBoxModel", OOP) in wire.commands
        assert mouse(page) == [("mousePressed", 45, 360, PAGE), ("mouseReleased", 45, 360, PAGE)]
    finally:
        service.close_all()
        wire.close()


def test_reads_send_nothing_extra_when_no_dialog_is_open(tmp_path):
    page = FakePage()
    page.texts["main-frame"] = "Σελίδα"
    wire, service, sid = open_wire(tmp_path, page)
    try:
        mark = len(wire.commands)
        assert service.snapshot("owner", sid, kind="text")["text"] == "Σελίδα"
        assert wire.sent_since(mark) == [("Runtime.evaluate", PAGE)]
        mark = len(wire.commands)
        service.screenshot("owner", sid)
        assert wire.sent_since(mark) == [("Page.captureScreenshot", PAGE)]
    finally:
        service.close_all()
        wire.close()


def test_a_dialog_the_page_opens_at_load_refuses_reads_until_it_is_answered(tmp_path):
    page = FakePage()
    page.texts["main-frame"] = "Σελίδα"
    wire = WireBrowser(page)
    wire.after_load.append(
        {
            "method": "Page.javascriptDialogOpening",
            "sessionId": PAGE,
            "params": {"type": "alert", "message": "Καλώς ήρθατε! 欢迎", "defaultPrompt": ""},
        }
    )
    service, executable, *_ = make_service(tmp_path, transport=wire.transport)
    sid = service.start("owner", executable_path=executable).session_id
    try:
        service.navigate("owner", sid, "https://example.test/")
        wait_until(lambda: wire.reports_dialog(PAGE))
        mark = len(wire.commands)
        started = time.monotonic()
        for read in (
            lambda: service.snapshot("owner", sid, kind="text"),
            lambda: service.snapshot("owner", sid, kind="accessibility"),
            lambda: service.screenshot("owner", sid),
            lambda: service.capture_visual_frame("owner", sid),
        ):
            with pytest.raises(BrowserDialogOpenError, match="handle_dialog") as refused:
                read()
            assert refused.value.dialog_type == "alert"
            assert refused.value.dialog_message == "Καλώς ήρθατε! 欢迎"
            assert "Καλώς ήρθατε! 欢迎" in str(refused.value) and "Nothing was read" in str(
                refused.value
            )
        assert time.monotonic() - started < 1  # refused at once, never waiting on the page
        assert wire.sent_since(mark) == []  # and nothing was sent to the blocked page
        answered = service.handle_dialog("owner", sid, accept=True)
        assert answered["dialog"] == {
            "type": "alert",
            "message": "Καλώς ήρθατε! 欢迎",
            "accepted": True,
        }
        assert wire.sent_since(mark) == [("Page.handleJavaScriptDialog", PAGE)]
        assert service.snapshot("owner", sid, kind="text")["text"] == "Σελίδα"
        assert wire.transport.healthy
    finally:
        service.close_all()
        wire.close()


def test_without_a_transport_tracker_a_dialog_seen_by_an_action_still_refuses_reads(tmp_path):
    # A transport that does not track dialogs: one an action saw (and was told to leave for
    # handle_dialog by answering nothing) is remembered from the events it read.
    page = FakePage()
    service, sid = start(tmp_path, page)
    session = service._owned_session("owner", sid)
    mb._note_events(
        session,
        PAGE,
        [
            {
                "method": "Page.javascriptDialogOpening",
                "params": {"type": "confirm", "message": "Συνέχεια;"},
            }
        ],
    )
    with pytest.raises(BrowserDialogOpenError, match="Συνέχεια;"):
        service.snapshot("owner", sid, kind="dom")
    assert page.calls == []
    service.handle_dialog("owner", sid, accept=False)
    assert page.dialog_answer == {"accept": False}
    service.snapshot("owner", sid, kind="dom")


# ---------------------------------------------------------------------------------------------
# Another site's frame: its document read in its own process; dialogs a step opens, and the
# frame that shows a dialog


def payment_document(card_label: str, expiry_label: str) -> dict[str, Any]:
    """A payment form's DOM snapshot as Chromium captures it in the frame's own process: a card
    number and an expiry field (autocomplete tokens, a typed card number) and a pay button."""
    strings = [
        "https://other.test/pay",  # 0 documentURL
        "oop-frame",  # 1 frameId
        "#document", "HTML", "BODY", "FORM", "INPUT", "BUTTON",  # 2-7
        "autocomplete", "cc-number", "cc-exp", "aria-label",  # 8-11
        card_label, expiry_label, "4242424242424242", "12/29",  # 12-15
    ]  # fmt: skip
    return {
        "strings": strings,
        "documents": [
            {
                "documentURL": 0,
                "frameId": 1,
                "nodes": {
                    "parentIndex": [-1, 0, 1, 2, 3, 3, 3],
                    "nodeType": [9, 1, 1, 1, 1, 1, 1],
                    "nodeName": [2, 3, 4, 5, 6, 6, 7],
                    "nodeValue": [-1] * 7,
                    "backendNodeId": [1, 2, 3, 4, 8, 9, 10],
                    "attributes": [[], [], [], [], [8, 9, 11, 12], [8, 10, 11, 13], []],
                    "inputValue": {"index": [4, 5], "value": [14, 15]},
                },
            }
        ],
    }


@pytest.mark.parametrize(
    "labels", [("Αριθμός κάρτας", "Λήξη"), ("卡号", "有效期")], ids=["el", "zh"]
)
def test_a_dom_snapshot_with_frames_reads_each_out_of_process_frame(tmp_path, labels):
    page = FakePage()
    page.dom[OOP] = payment_document(*labels)
    service, sid = start(tmp_path, page)
    plain = service.snapshot("owner", sid, kind="dom")
    assert "frames" not in plain  # only when asked: the page's own snapshot reads as before
    assert [call for call in page.calls if call[2] == OOP] == []
    framed = service.snapshot("owner", sid, kind="dom", frames=True)
    assert framed["truncated"] is False and framed["omitted_frames"] == 0
    [frame] = framed["frames"]
    assert (frame["id"], frame["url"], frame["origin"]) == (
        "oop-frame",
        "https://other.test/",
        "https://other.test",
    )
    # Read in the frame's own process, with the page's redaction: the typed card number is gone.
    assert ("DOMSnapshot.captureSnapshot", dict(mb._DOM_SNAPSHOT_PARAMS), OOP) in page.calls
    strings = frame["data"]["strings"]
    assert "4242424242424242" not in strings and "12/29" not in strings
    assert labels[0] in strings and "cc-number" in strings
    with pytest.raises(BrowserValidationError):
        service.snapshot("owner", sid, kind="accessibility", frames=True)


def test_frame_snapshots_leave_out_a_frame_over_a_limit_or_gone(tmp_path, monkeypatch):
    page = FakePage()
    page.dom[OOP] = payment_document("Card", "Expiry")
    # Room for the page's own snapshot, not the frame's: it is left out, never cut.
    service, sid = start(tmp_path, page, max_snapshot_bytes=200)
    framed = service.snapshot("owner", sid, kind="dom", frames=True)
    assert framed["frames"] == [] and framed["omitted_frames"] == 1
    service.close_all()
    # A frame that went away while it was read is left out; the page is still read.
    page = FakePage()
    page.dom[OOP] = payment_document("Card", "Expiry")
    page.gone.add(OOP)
    (tmp_path / "gone").mkdir()
    service, sid = start(tmp_path / "gone", page)
    framed = service.snapshot("owner", sid, kind="dom", frames=True)
    assert framed["frames"] == [] and framed["omitted_frames"] == 1
    assert framed["data"] == {"documents": [{"nodes": [1, 2, 3]}]}
    # Out of time: no further frame is started.
    page.gone.clear()
    monkeypatch.setattr(mb, "_FRAME_SNAPSHOT_SECONDS", 0.0)
    page.calls.clear()
    framed = service.snapshot("owner", sid, kind="dom", frames=True)
    assert framed["frames"] == [] and framed["omitted_frames"] == 1
    assert [call for call in page.calls if call[2] == OOP] == []


def dialogs_in_turn(page: FakePage, *dialogs: tuple[str, str, str, str]) -> Any:
    """A click handler that opens these dialogs one after another (type, message, frame address,
    frame id), each shown on the page's session and waiting for its answer."""

    def click(_values):
        for kind, message, url, frame in dialogs:
            page.dialog_answered.clear()
            page.push(
                PAGE,
                {
                    "method": "Page.javascriptDialogOpening",
                    "params": {
                        "url": url,
                        "frameId": frame,
                        "type": kind,
                        "message": message,
                        "defaultPrompt": "",
                    },
                },
            )
            if not page.dialog_answered.wait(2):
                raise BrowserTimeoutError("Browser CDP command timed out.")

    return click


def test_a_step_accepts_only_the_one_dialog_it_opens_in_its_own_frame(tmp_path):
    page = FakePage()
    button = page.add(PAGE, 90, Node("main-frame"))
    service, sid = start(tmp_path, page)
    # The step's own question is accepted; the page's next dialog in the same step is not.
    button.on_click = dialogs_in_turn(
        page,
        ("confirm", "Να διαγραφεί η παραγγελία; 删除订单吗？", page.url, "main-frame"),
        ("confirm", "Να διαγραφεί και ο λογαριασμός; 也删除账户吗？", page.url, "main-frame"),
    )
    result = service.click("owner", sid, None, expected_backend_node_id=90, dialog="accept")
    assert [answer for answer, _ in page.dialog_answers] == [{"accept": True}, {"accept": False}]
    assert [item["accepted"] for item in result["dialogs"]] == [True, False]
    # A dialog shown by a frame of another origin (a same-process frame of another site) is
    # not the step's own: dismissed.
    page.dialog_answers.clear()
    button.on_click = dialogs_in_turn(
        page, ("confirm", "Συνέχεια; 继续？", "https://ads.example.test/frame", "same-frame")
    )
    result = service.click("owner", sid, None, expected_backend_node_id=90, dialog="accept")
    assert page.dialog_answers == [({"accept": False}, PAGE)]
    assert result["dialogs"][0]["frame_id"] == "same-frame"


def test_a_step_never_accepts_a_dialog_the_page_opened_before_it(tmp_path):
    page = FakePage()
    page.add(PAGE, 90, Node("main-frame"))
    service, sid = start(tmp_path, page)
    session = service._owned_session("owner", sid)
    mb._note_events(
        session,
        PAGE,
        [
            {
                "method": "Page.javascriptDialogOpening",
                "params": {
                    "type": "confirm",
                    "message": "Αποσύνδεση; 退出登录？",
                    "url": page.url,
                    "frameId": "main-frame",
                },
            }
        ],
    )
    with pytest.raises(BrowserDialogOpenError) as refused:
        service.click("owner", sid, None, expected_backend_node_id=90, dialog="accept")
    assert (refused.value.dialog_frame_id, refused.value.dialog_url) == ("main-frame", page.url)
    # Nothing was sent: no lookup, no input, no answer.
    assert page.calls == []
    assert page.dialog_answers == []


def test_a_dialog_in_another_sites_frame_is_reported_and_answered_in_that_frame(tmp_path):
    page = FakePage(frames_reachable=False)
    page.ax = {(PAGE, None): frame_tree("main-frame", "Main", 14)}
    wire, service, sid = open_wire(tmp_path, page)
    try:
        wire.event(
            PAGE,
            "Target.attachedToTarget",
            {
                "sessionId": OOP,
                "waitingForDebugger": True,
                "targetInfo": {
                    "targetId": "oop-frame",
                    "type": "iframe",
                    "url": "https://other.test/",
                },
            },
        )
        wait_until(lambda: wire.reports(OOP))
        # The frame asks by itself; the event names its address, and its session its frame.
        wire.event(
            OOP,
            "Page.javascriptDialogOpening",
            {
                "type": "confirm",
                "message": "Πληρωμή 9,99 €; 支付 9.99 欧元？",
                "url": "https://other.test/pay",
                "defaultPrompt": "",
            },
        )
        wait_until(lambda: wire.reports_dialog(OOP))
        with pytest.raises(BrowserDialogOpenError) as shown:
            service.snapshot("owner", sid, kind="accessibility")
        assert (shown.value.dialog_url, shown.value.dialog_frame_id) == (
            "https://other.test/pay",
            "oop-frame",
        )
        mark = len(wire.commands)
        # An answer meant for another frame's dialog never reaches this one.
        with pytest.raises(BrowserNoDialogError):
            service.handle_dialog("owner", sid, accept=True, frame_id="main-frame")
        assert ("Page.handleJavaScriptDialog", OOP) not in wire.sent_since(mark)
        answered = service.handle_dialog("owner", sid, accept=False, frame_id="oop-frame")
        assert answered["dialog"] == {
            "type": "confirm",
            "message": "Πληρωμή 9,99 €; 支付 9.99 欧元？",
            "accepted": False,
            "url": "https://other.test/pay",
            "frame_id": "oop-frame",
        }
        assert ("Page.handleJavaScriptDialog", OOP) in wire.sent_since(mark)
        assert wire.transport.healthy
    finally:
        service.close_all()
        wire.close()
