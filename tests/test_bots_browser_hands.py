"""The owl's newer browser tools (fill, key chords, hover, check, drag, wait, read a value,
answer a dialog) and elements inside frames, on the host: offered only while the running
browser declares them, addressed by page view refs with the same identity checks as a click, and
a page dialog read to the model and answered. The fake browser accepts exactly the managed
browser's own call signatures, so the host speaks the guest's language."""

from __future__ import annotations

import copy
import inspect

import pytest
from test_bots_browser import PREVIEW, task, token
from test_bots_browser_page_view import Page, PageBrowser, ref

from alysis_code.bots.browser import BrowserComputers
from alysis_code.bots.browser_tools import (
    FILE_TOOLS,
    TOOL_OPERATIONS,
    BrowserInputNotSent,
    BrowserObservationExpired,
    BrowserRefusal,
)
from alysis_code.bots.cloud_browser import OPERATIONS
from alysis_code.bots.computer import ComputerViews
from alysis_code.ide.managed_browser import (
    BrowserDialogOpenError,
    BrowserNoDialogError,
    BrowserWaitTimeoutError,
    ManagedBrowserService,
)

PREVIEW_ORIGIN = "http://127.0.0.1:8123"
PAY = "https://pay.example.net/checkout"
PAY_FRAME = "PAY-FRAME"
# The newer tools, but those that share library files (offered only with files).
NEW_TOOLS = set(TOOL_OPERATIONS) - FILE_TOOLS


class HandsBrowser(PageBrowser):
    """A browser that does the newer operations on its page, with another site's frame."""

    def __init__(self, page: Page, fields: dict):
        self.fields = fields  # name -> DOM index of the page's form elements
        self.frame_nodes: list[dict] = []
        self.frame_loader = "pay-loader-1"
        self.frame_values: dict[int, str] = {}
        self.dialog: tuple[str, str] | None = None
        # What the website reports during the next acting step (page_report), read once.
        self.reported: dict | None = None
        super().__init__(page)
        self.page = page

    def show(self, page: Page) -> None:
        self.page = page
        super().show(page)

    def _call(self, name, owner, session_id, *args, **kwargs):
        # Only calls the managed browser itself accepts.
        method = getattr(ManagedBrowserService, name, None)
        if method is not None:
            inspect.signature(method).bind(None, owner, session_id, *args, **kwargs)
        super()._call(name, owner, session_id, *args, **kwargs)

    def snapshot(self, owner, session_id, **kwargs):
        if self.dialog is not None:
            self._call("snapshot", owner, session_id, **kwargs)
            raise BrowserDialogOpenError(*self.dialog)
        result = super().snapshot(owner, session_id, **kwargs)
        if kwargs.get("kind") == "accessibility":
            result["data"]["frames"] = [
                {"id": self.page.frame, "url": PREVIEW, "origin": PREVIEW_ORIGIN, "loader": "L"},
            ]
            if self.frame_nodes:
                result["data"]["frames"].append(
                    {
                        "id": PAY_FRAME,
                        "url": PAY,
                        "origin": "https://pay.example.net",
                        "loader": self.frame_loader,
                    }
                )
                result["data"]["nodes"] += copy.deepcopy(self.frame_nodes)
        return result

    # The page answers each operation.
    def _index(self, backend):
        return self.page.nodes["backendNodeId"].index(backend)

    def _ax(self, backend):
        return next(node for node in self.page.ax if node.get("backendDOMNodeId") == backend)

    def _set_value(self, kwargs, text):
        if kwargs.get("frame_id"):
            self.frame_values[kwargs["expected_backend_node_id"]] = text
            return
        backend = kwargs["expected_backend_node_id"]
        self.page.values[self._index(backend)] = text
        self._ax(backend)["value"] = {"type": "string", "value": text}
        self.show(self.page)

    def fill(self, owner, session_id, selector, text, **kwargs):
        self._call("fill", owner, session_id, selector, text, **kwargs)
        self._set_value(kwargs, text)
        self._acted()
        return {"filled": True, "value_matches": True, "submitted": kwargs.get("submit", False)}

    def set_checked(self, owner, session_id, selector, checked, **kwargs):
        self._call("set_checked", owner, session_id, selector, checked, **kwargs)
        backend = kwargs["expected_backend_node_id"]
        index = self._index(backend)
        changed = (index in self.page.checked) != checked
        if checked and changed:
            self.page.checked.append(index)
        elif changed:
            self.page.checked.remove(index)
        node = self._ax(backend)
        node["properties"] = [
            item for item in node.get("properties", []) if item["name"] != "checked"
        ] + [{"name": "checked", "value": {"type": "tristate", "value": str(checked).lower()}}]
        self.show(self.page)
        self._acted()
        return {"changed": changed, "checked": checked}

    def read_value(self, owner, session_id, selector, **kwargs):
        self._call("read_value", owner, session_id, selector, **kwargs)
        backend = kwargs["expected_backend_node_id"]
        if kwargs.get("frame_id"):
            return {
                "kind": "field",
                "value": self.frame_values.get(backend, ""),
                "truncated": False,
            }
        index = self._index(backend)
        if (
            index in self.page.checked
            or self.page.nodes["attributes"][index]
            and any(
                self.page.strings[item] == "checkbox"
                for item in self.page.nodes["attributes"][index]
            )
        ):
            return {
                "kind": "checkbox",
                "value": "on",
                "truncated": False,
                "checked": index in self.page.checked,
            }
        return {"kind": "field", "value": self.page.values.get(index, ""), "truncated": False}

    def press_key(self, owner, session_id, selector, key, **kwargs):
        self._call("press_key", owner, session_id, selector, key, **kwargs)
        self._acted()
        return {"dispatched": True}

    def hover(self, owner, session_id, selector, **kwargs):
        self._call("hover", owner, session_id, selector, **kwargs)
        return {"dispatched": True}

    def drag(self, owner, session_id, selector, target_selector, **kwargs):
        self._call("drag", owner, session_id, selector, target_selector, **kwargs)
        self._acted()
        return {"dispatched": True}

    def wait_for(self, owner, session_id, condition, **kwargs):
        self._call("wait_for", owner, session_id, condition, **kwargs)
        if kwargs.get("value") == "never shown":
            raise BrowserWaitTimeoutError("Synthetic wait timeout")
        return {"condition": condition, "satisfied": True, "waited_seconds": 0.1}

    def handle_dialog(self, owner, session_id, **kwargs):
        self._call("handle_dialog", owner, session_id, **kwargs)
        if self.dialog is None:
            raise BrowserNoDialogError("No dialog is open on this page. Nothing was answered.")
        kind, message = self.dialog
        self.dialog = None
        self._acted()
        return {"dialog": {"type": kind, "message": message, "accepted": kwargs["accept"]}}

    def page_report(self, owner, session_id, **kwargs):
        self._call("page_report", owner, session_id, **kwargs)
        reported, self.reported = self.reported, None
        return reported or {"failed_requests": [], "more_failed_requests": 0, "uncaught_errors": 0}


def form_page(name_label="Όνομα", consent_label="Συμφωνώ με τους όρους"):
    """A labelled form: a name field, a consent checkbox, a list to reorder and a button."""
    page = Page(title="Φόρμα")
    form = page.add(page.body, "FORM", "form", name="")
    name = page.add(form, "INPUT", "textbox", name=name_label, value="",
                    attrs={"type": "text"}, props={"editable": "plaintext", "focusable": True})  # fmt: skip
    consent = page.add(form, "INPUT", "checkbox", name=consent_label,
                       attrs={"type": "checkbox"}, props={"checked": "false", "focusable": True})  # fmt: skip
    first = page.add(form, "LI", "listitem", name="Πρώτο")
    page.text(first, "Πρώτο")
    second = page.add(form, "LI", "listitem", name="Δεύτερο")
    page.text(second, "Δεύτερο")
    send = page.add(form, "BUTTON", "button", name="Αποστολή", props={"focusable": True})
    page.text(send, "Αποστολή")
    fields = {"name": name[0], "consent": consent[0], "first": first[0], "second": second[0],
              "send": send[0]}  # fmt: skip
    return page, fields


def add_payment_frame(page: Page, browser: HandsBrowser) -> None:
    """Another site's frame in the page: its <iframe> in the main document, and its own tree
    from another process (node ids prefixed by its frame, backend ids its own: one repeats a
    main-document element's)."""
    page.add(page.body, "IFRAME", "Iframe", name="Πληρωμή", props={"focusable": True})
    browser.show(page)
    collision = page.backend_of(browser.fields["name"])

    def node(node_id, role, name=None, *, parent=None, children=(), backend=None, **extra):
        made = {
            "nodeId": f"{PAY_FRAME}/{node_id}",
            "ignored": False,
            "role": {"type": "role", "value": role},
            "childIds": [f"{PAY_FRAME}/{child}" for child in children],
            **extra,
        }
        if name is not None:
            made["name"] = {"type": "computedString", "value": name}
        if parent is not None:
            made["parentId"] = f"{PAY_FRAME}/{parent}"
        if backend is not None:
            made["backendDOMNodeId"] = backend
        return made

    browser.frame_nodes = [
        node("1", "RootWebArea", "Payment", children=["2", "3"], frameId=PAY_FRAME, backend=1),
        node(
            "2",
            "textbox",
            "Αριθμός κάρτας",
            parent="1",
            backend=collision,
            properties=[
                {"name": "editable", "value": {"type": "token", "value": "plaintext"}},
                {"name": "focusable", "value": {"type": "boolean", "value": True}},
            ],
        ),  # fmt: skip
        node(
            "3",
            "button",
            "Pay",
            parent="1",
            backend=collision + 1,
            properties=[{"name": "focusable", "value": {"type": "boolean", "value": True}}],
        ),  # fmt: skip
    ]


class Lab:
    """The owl's browser computers with one computer whose browser declares ``features``."""

    def __init__(self, page, fields, *, features=frozenset(OPERATIONS), previews=True):
        self.browser = HandsBrowser(page, fields)
        self.executors = []
        self.features = features
        self.adapter = BrowserComputers(
            "owner", ComputerViews(), self.factory, interactive_previews=previews
        )

    def factory(self, bot_id, cancel):
        from test_bots_browser import ExecutorFixture

        executor = ExecutorFixture(bot_id, self.adapter.views)
        executor.browser = self.browser
        if self.features is not None:
            executor.browser_features = self.features
        self.executors.append(executor)
        return executor

    def call(self, name="browser_observe", args=None, *, approved=(), review=None):
        return self.adapter.call(
            task(),
            name,
            args or {},
            token(),
            approved_origins=approved,
            review_action=review,
        )

    def offered(self, *, approved=()):
        schemas = self.adapter.schemas(task(), eligible=True, approved_origins=approved)
        return {schema["function"]["name"] for schema in self.adapter.offered(task(), schemas)}

    def sent(self, name):
        return [call for call in self.browser.calls if call[0] == name]


@pytest.fixture
def lab():
    page, fields = form_page()
    made = Lab(page, fields)
    yield made
    made.adapter.close()


def test_the_new_tools_are_offered_once_a_browser_that_declares_them_runs(lab):
    assert not NEW_TOOLS & lab.offered()  # nothing runs yet: today's tools only
    lab.call()
    assert lab.offered() >= NEW_TOOLS
    # Without the previews' or a site's approval only the ones that just read are offered.
    lab.adapter.interactive_previews = False
    assert NEW_TOOLS & lab.offered() == {
        "browser_hover",
        "browser_wait",
        "browser_read_value",
        "browser_history",
    }


TOOL_CASES = {
    "browser_fill": ("name", {"text": "Αθηνά"}, "fill", True),
    "browser_key_chord": ("name", {"key": "a", "modifiers": ["ControlOrMeta"]}, "press_key", True),
    "browser_hover": ("send", {}, "hover", True),
    "browser_check": ("consent", {"checked": True}, "set_checked", True),
    "browser_drag": ("first", {"to": "second"}, "drag", True),
    "browser_read_value": ("name", {}, "read_value", False),
    "browser_wait": ("send", {"condition": "element_present"}, "wait_for", False),
}


@pytest.mark.parametrize("tool", sorted(TOOL_CASES))
def test_each_new_tool_reaches_the_browser_with_the_observed_identity(lab, tool):
    target, extra, method, generation = TOOL_CASES[tool]
    observation = lab.call()
    view = observation["page_view"]
    names = {"name": "Όνομα", "consent": "Συμφωνώ", "first": "Πρώτο", "second": "Δεύτερο",
             "send": "Αποστολή"}  # fmt: skip

    def ref_of(field):
        line = next(line.strip() for line in view.splitlines() if f'"{names[field]}' in line)
        return ref(view, line)

    arguments = {"source_id": observation["source_id"], "node": ref_of(target), **extra}
    if "to" in arguments:
        arguments["to_node"] = ref_of(arguments.pop("to"))
    result = lab.call(tool, arguments)
    (call,) = lab.sent(method)
    page = lab.browser.page
    assert call[4]["expected_backend_node_id"] == page.backend_of(lab.browser.fields[target])
    if tool == "browser_wait":
        # Waited for by identity: a position-based selector could name another element later.
        assert call[3] == ("element_present",) and call[4]["selector"] is None
    else:
        assert isinstance(call[3][0], str)  # a main-document element keeps its selector
    assert ("expected_generation" in call[4]) is generation
    if tool == "browser_drag":
        assert call[4]["target_backend_node_id"] == page.backend_of(lab.browser.fields["second"])
    if tool == "browser_read_value":
        assert result["value"] == "" and result["untrusted_page_data"] is True
    elif tool in {"browser_hover", "browser_wait"}:
        assert "page_view" in result and "action" not in result
    else:
        assert result["action"]["name"] == tool and result["action"]["dispatched"] is True
        assert "page_view" in result


@pytest.mark.parametrize(
    "name_label,text,consent_label",
    [
        ("Όνομα", "Αθηνά Παπαδοπούλου", "Συμφωνώ με τους όρους"),
        ("姓名", "王小明", "我同意服务条款"),
    ],
)
def test_a_labelled_form_in_any_script_is_filled_checked_and_read_back(
    name_label, text, consent_label
):
    page, fields = form_page(name_label, consent_label)
    lab = Lab(page, fields)
    try:
        observation = lab.call("browser_observe", {"find": name_label})
        field = ref(observation["page_view"], f'- textbox "{name_label}"')
        filled = lab.call(
            "browser_fill", {"source_id": observation["source_id"], "node": field, "text": text}
        )
        assert filled["action"]["value_matches"] is True
        assert f'textbox "{name_label}" [ref=e{field}]: {text}' in filled["page_view"]
        read = lab.call("browser_read_value", {"source_id": filled["source_id"], "node": field})
        assert read["value"] == text
        consent = ref(filled["page_view"], f'- checkbox "{consent_label}"')
        checked = lab.call(
            "browser_check",
            {"source_id": filled["source_id"], "node": consent, "checked": True},
        )
        assert checked["action"]["checked"] is True and checked["action"]["dispatched"] is True
        assert f'checkbox "{consent_label}" [checked] [ref=e{consent}]' in checked["page_view"]
        state = lab.call("browser_read_value", {"source_id": checked["source_id"], "node": consent})
        assert state["checked"] is True
        # Checking it again sends nothing: the page already shows it.
        again = lab.call(
            "browser_check",
            {"source_id": checked["source_id"], "node": consent, "checked": True},
        )
        assert again["action"]["dispatched"] is False
    finally:
        lab.adapter.close()


def test_an_element_inside_another_sites_frame_is_reached_with_that_sites_approval(lab):
    add_payment_frame(lab.browser.page, lab.browser)
    observation = lab.call()
    view = observation["page_view"]
    assert f'- frame "Payment" -> {PAY}' in view
    card = ref(view, '- textbox "Αριθμός κάρτας"')
    name = ref(view, '- textbox "Όνομα"')
    # Its backend id repeats the main document's name field's, yet it is its own element.
    assert card != name and f"[ref=e{card}]" in view.split("- frame")[1]
    args = {"source_id": observation["source_id"], "node": card, "text": "4242"}
    # The frame's site is not approved: nothing is sent to it.
    with pytest.raises(BrowserRefusal, match="frame from another website"):
        lab.call("browser_fill", args)
    assert not lab.sent("fill")
    # Reading it needs no approval.
    read = lab.call("browser_read_value", {"source_id": observation["source_id"], "node": card})
    assert read["value"] == "" and read["untrusted_page_data"] is True
    lab.call("browser_fill", args, approved=(PREVIEW, "https://pay.example.net"))
    (call,) = lab.sent("fill")
    assert call[3][0] is None  # reached by identity, not a selector
    assert call[4]["expected_backend_node_id"] == lab.browser.page.backend_of(
        lab.browser.fields["name"]
    )
    assert call[4]["frame_id"] == PAY_FRAME
    assert lab.browser.frame_values[call[4]["expected_backend_node_id"]] == "4242"
    assert lab.browser.page.values[lab.browser.fields["name"]] == ""  # the page's own field
    # Other frames on the page no longer block input to the page's own controls.
    send = ref(lab.call()["page_view"], '- button "Αποστολή"')
    lab.call("browser_click", {"source_id": observation["source_id"], "node": send})
    assert len(lab.sent("click")) == 1
    # The frame loads another document: the old ref names nothing there any more.
    lab.browser.frame_loader = "pay-loader-2"
    with pytest.raises(BrowserObservationExpired, match="no longer on the page"):
        lab.call("browser_fill", args, approved=(PREVIEW, "https://pay.example.net"))
    assert len(lab.sent("fill")) == 1


def test_an_older_browser_keeps_todays_tools_and_rules():
    page, fields = form_page()
    lab = Lab(page, fields, features=None)
    try:
        add_payment_frame(page, lab.browser)
        observation = lab.call()
        assert not NEW_TOOLS & lab.offered()
        view = observation["page_view"]
        # The other site's frame is not addressable: no frame section, no refs in it.
        assert "- frame" not in view and "Αριθμός κάρτας" not in view
        name = ref(view, '- textbox "Όνομα"')
        with pytest.raises(BrowserRefusal, match="needs a newer owl computer"):
            lab.call("browser_fill", {"source_id": observation["source_id"], "node": name,
                                      "text": "Αθηνά"})  # fmt: skip
        assert not lab.sent("fill")
        # Today's tools work as before, and a page with another site's frame still takes none.
        with pytest.raises(BrowserRefusal, match="embeds content from another site"):
            lab.call("browser_type", {"source_id": observation["source_id"], "node": name,
                                      "text": "Αθηνά"})  # fmt: skip
        lab.browser.frame_nodes = []
        page, fields = form_page()
        lab.browser.show(page)
        observation = lab.call()
        name = ref(observation["page_view"], '- textbox "Όνομα"')
        lab.call("browser_type", {"source_id": observation["source_id"], "node": name,
                                  "text": "Αθηνά"})  # fmt: skip
        assert len(lab.sent("type")) == 1
    finally:
        lab.adapter.close()


class Approval:
    def __init__(self):
        self.released = False

    def release(self):
        self.released = True


def test_a_dialog_while_observing_is_read_plainly_and_answered(lab):
    lab.call()
    lab.browser.dialog = ("confirm", "Να διαγραφεί το αρχείο; 删除文件吗？")
    report = lab.call()
    assert report["source_kind"] == "page_dialog" and report["untrusted_page_data"] is True
    assert report["dialog"] == {
        "type": "confirm",
        "message": "Να διαγραφεί το αρχείο; 删除文件吗？",
    }
    assert "browser_handle_dialog" in report["message"] and "Traceback" not in report["message"]
    assert "action" not in report
    reviewed = []

    def review(name, arguments, context):
        reviewed.append((name, arguments, context))
        return Approval()

    answered = lab.call(
        "browser_handle_dialog",
        {"source_id": report["source_id"], "accept": False},
        review=review,
    )
    ((name, arguments, context),) = reviewed
    assert name == "browser_handle_dialog" and arguments["accept"] is False
    assert context["dialog"] == report["dialog"] and context["url"] == PREVIEW
    (call,) = lab.sent("handle_dialog")
    assert call[4]["accept"] is False and call[4]["expected_generation"] == 17
    assert answered["action"]["dialog"]["accepted"] is False and "page_view" in answered
    # The page reads again; the answered report cannot answer another dialog.
    assert "page_view" in lab.call()
    with pytest.raises(BrowserObservationExpired, match="no longer current"):
        lab.call("browser_handle_dialog", {"source_id": report["source_id"], "accept": True})


def test_a_dialog_after_input_says_the_input_was_sent_and_keeps_its_approval(lab):
    observation = lab.call()
    send = ref(observation["page_view"], '- button "Αποστολή"')
    approvals = []

    def review(name, arguments, context):
        approvals.append(Approval())
        return approvals[-1]

    original_click = lab.browser.click

    def click_opens_dialog(*args, **kwargs):
        original_click(*args, **kwargs)
        lab.browser.dialog = ("alert", "Αποθηκεύτηκε 已保存")

    lab.browser.click = click_opens_dialog
    report = lab.call(
        "browser_click", {"source_id": observation["source_id"], "node": send}, review=review
    )
    assert report["action"]["dispatched"] is True and "sent once" in report["message"]
    assert not approvals[0].released  # the input it allowed was sent
    # A dialog before any input releases the approval: nothing was sent.
    lab.browser.dialog = None
    observation = lab.call()
    send = ref(observation["page_view"], '- button "Αποστολή"')

    def review_then_dialog(name, arguments, context):
        approvals.append(Approval())
        lab.browser.dialog = ("alert", "Συνεδρία 会话")
        return approvals[-1]

    report = lab.call(
        "browser_click",
        {"source_id": observation["source_id"], "node": send},
        review=review_then_dialog,
    )
    assert "action" not in report and approvals[1].released
    assert len(lab.sent("click")) == 1


def test_without_website_approval_the_dialog_report_says_how_to_answer():
    page, fields = form_page()
    lab = Lab(page, fields, previews=False)
    try:
        lab.call("browser_navigate", {"url": PREVIEW})
        lab.browser.dialog = ("alert", "Γεια 你好")
        report = lab.call()
        assert "request this task's website approval" in report["message"]
        assert "browser_handle_dialog" not in lab.offered()
        with pytest.raises(BrowserObservationExpired, match="requires website approval"):
            lab.call("browser_handle_dialog", {"source_id": report["source_id"], "accept": True})
        assert not lab.sent("handle_dialog")
    finally:
        lab.adapter.close()


def test_waiting_reports_a_timeout_and_answering_a_closed_dialog_sends_nothing(lab):
    lab.call()
    result = lab.call("browser_wait", {"condition": "text", "text": "never shown", "seconds": 2})
    assert result["wait"]["satisfied"] is False and "Nothing was changed" in result["message"]
    (call,) = lab.sent("wait_for")
    assert call[3] == ("text",) and call[4]["value"] == "never shown" and call[4]["timeout"] == 2
    shown = lab.call("browser_wait", {"condition": "text", "text": "Αποστολή"})
    assert shown["wait"]["satisfied"] is True and "page_view" in shown
    lab.browser.dialog = ("alert", "Τέλος")
    report = lab.call()
    lab.browser.dialog = None  # it closed by itself
    with pytest.raises(BrowserInputNotSent, match="nothing was answered"):
        lab.call("browser_handle_dialog", {"source_id": report["source_id"], "accept": True})


@pytest.mark.parametrize(
    "arguments",
    [
        {"condition": "text"},
        {"condition": "element_present"},
        {"condition": "load", "text": "Ναι"},
        {"condition": "element_gone", "node": 1},
        {"condition": "text", "text": "x", "seconds": 21},
    ],
)
def test_unclear_waits_are_refused_before_any_browser_step(lab, arguments):
    lab.call()
    before = len(lab.browser.calls)
    with pytest.raises(ValueError):
        lab.call("browser_wait", arguments)
    assert len(lab.browser.calls) == before


@pytest.mark.parametrize(
    "arguments",
    [
        {"key": "a", "modifiers": []},
        {"key": "Ctrl+a", "modifiers": ["Control"]},
        {"key": "a", "modifiers": ["Control", "Control"]},
        {"key": "a", "modifiers": ["Hyper"]},
        {"key": "\n", "modifiers": ["Shift"]},
    ],
)
def test_unclear_key_chords_are_refused_before_any_browser_step(lab, arguments):
    observation = lab.call()
    name = ref(observation["page_view"], '- textbox "Όνομα"')
    with pytest.raises(ValueError):
        lab.call(
            "browser_key_chord", {"source_id": observation["source_id"], "node": name, **arguments}
        )
    assert not lab.sent("press_key")


def test_a_control_inside_an_open_shadow_root_is_clicked_by_its_identity(lab):
    page = lab.browser.page
    host = page.add(page.body, "DIV", "generic")
    inside = page.add(host, "BUTTON", "button", name="Αποδοχή 接受", shadow=True,
                      props={"focusable": True})  # fmt: skip
    lab.browser.show(page)
    observation = lab.call()
    button = ref(observation["page_view"], '- button "Αποδοχή 接受"')
    lab.call("browser_click", {"source_id": observation["source_id"], "node": button})
    (call,) = lab.sent("click")
    assert call[3][0] is None and "frame_id" not in call[4]
    assert call[4]["expected_backend_node_id"] == page.backend_of(inside[0])


def test_input_sent_to_a_point_is_still_refused_on_a_page_with_another_sites_frame(lab):
    # A wheel lands on whatever is under its point, possibly the other site's frame.
    add_payment_frame(lab.browser.page, lab.browser)
    observation = lab.call()
    send = ref(observation["page_view"], '- button "Αποστολή"')
    with pytest.raises(BrowserRefusal, match="embeds content from another site"):
        lab.call(
            "browser_scroll", {"source_id": observation["source_id"], "node": send, "delta_y": 300}
        )
    assert not lab.sent("scroll")


def test_a_frame_that_changes_during_review_gets_no_input(lab):
    add_payment_frame(lab.browser.page, lab.browser)
    observation = lab.call()
    card = ref(observation["page_view"], '- textbox "Αριθμός κάρτας"')
    approvals = []

    def review(name, arguments, context):
        # The reviewer sees which frame's site the input goes to.
        assert context["frame_url"] == PAY and context["label"] == "Αριθμός κάρτας"
        approvals.append(Approval())
        lab.browser.frame_loader = "pay-loader-2"  # the frame loads another document meanwhile
        return approvals[-1]

    with pytest.raises(BrowserInputNotSent, match="changed during action review"):
        lab.call(
            "browser_fill",
            {"source_id": observation["source_id"], "node": card, "text": "4242"},
            approved=(PREVIEW, "https://pay.example.net"),
            review=review,
        )
    assert approvals[0].released and not lab.sent("fill")


def test_a_radio_button_is_never_unchecked(lab):
    page = lab.browser.page
    radio = page.add(page.body, "INPUT", "radio", name="Παράδοση 送货", attrs={"type": "radio"},
                     props={"checked": "true", "focusable": True})  # fmt: skip
    page.checked.append(radio[0])
    lab.browser.show(page)
    observation = lab.call()
    node = ref(observation["page_view"], '- radio "Παράδοση 送货"')
    with pytest.raises(BrowserRefusal, match="choosing another option of its group"):
        lab.call(
            "browser_check", {"source_id": observation["source_id"], "node": node, "checked": False}
        )
    assert not lab.sent("set_checked")


def test_waiting_for_an_element_that_is_already_gone_is_satisfied_at_once(lab):
    observation = lab.call()
    first = ref(observation["page_view"], '- listitem "Πρώτο"')
    page, fields = form_page()
    page.nodes["backendNodeId"][fields["first"]] = 9999  # replaced by a new element
    lab.browser.show(page)
    lab.browser.fields = fields
    result = lab.call(
        "browser_wait",
        {"condition": "element_gone", "source_id": observation["source_id"], "node": first},
    )
    assert result["wait"]["satisfied"] is True and not lab.sent("wait_for")
    with pytest.raises(BrowserObservationExpired):
        lab.call(
            "browser_wait",
            {"condition": "element_present", "source_id": observation["source_id"], "node": first},
        )


FAILED_SEND = {
    "session_id": "session-bot-a",
    "failed_requests": [
        {"method": "POST", "path": "/api/orders/:id", "status": 500},
        {"method": "GET", "path": "/api/session", "failure": "net::ERR_CONNECTION_RESET"},
    ],
    "more_failed_requests": 0,
    "uncaught_errors": 1,
}


def submit_step(lab: Lab, tool: str) -> dict:
    observation = lab.call()
    view = observation["page_view"]
    if tool == "browser_fill":
        node = ref(view, '- textbox "Όνομα"')
        return lab.call(
            tool,
            {"source_id": observation["source_id"], "node": node, "text": "Αθηνά", "submit": True},
        )
    node = ref(view, '- button "Αποστολή"')
    return lab.call(tool, {"source_id": observation["source_id"], "node": node})


@pytest.mark.parametrize("tool", ["browser_click", "browser_fill"])
def test_an_acting_step_carries_what_the_website_reported_during_it(lab, tool):
    lab.browser.reported = FAILED_SEND
    result = submit_step(lab, tool)
    action = result["action"]
    assert action["page_reported"] == {
        "failed_requests": FAILED_SEND["failed_requests"],
        "uncaught_errors": 1,
    }
    assert action["message"].endswith(
        "so it may not have worked: check the page before saying it did."
    )
    # Read once, after the page was observed following the step (its settle time).
    names = [call[0] for call in lab.browser.calls]
    assert names.count("page_report") == 1 and names[-1] == "page_report"
    assert names.index(tool.removeprefix("browser_")) < names.index("page_report")


def test_a_step_the_website_reported_nothing_about_keeps_its_receipt(lab):
    result = submit_step(lab, "browser_click")
    assert "page_reported" not in result["action"]
    assert result["action"]["message"].startswith("Input was sent once.")
    assert len(lab.sent("page_report")) == 1


def test_an_older_owl_computer_reports_nothing_and_is_never_asked():
    page, fields = form_page()
    older = frozenset(OPERATIONS) - {"page_reported"}
    lab = Lab(page, fields, features=older)
    try:
        lab.browser.reported = FAILED_SEND
        result = submit_step(lab, "browser_fill")
        assert result["action"]["dispatched"] is True
        assert "page_reported" not in result["action"] and not lab.sent("page_report")
    finally:
        lab.adapter.close()


def test_only_the_reports_fixed_fields_reach_the_owl(lab):
    # The guest's report is checked as data: fields of their types and sizes only, at most five
    # requests listed and the rest counted.
    lab.browser.reported = {
        "failed_requests": [
            {"method": "POST", "path": "/api/a", "status": 500, "body": "card 4111"},
            {"method": "post; drop", "path": "/api/b", "status": 404},
            {"method": "GET", "path": "https://other.example/x", "status": 500},
            {"method": "GET", "path": "/api/c", "status": 200},
            {"method": "GET", "path": "/api/d", "failure": "Uncaught card 4111"},
            *({"method": "GET", "path": f"/api/{n}", "status": 503} for n in "efghij"),
        ],
        "more_failed_requests": 2,
        "uncaught_errors": "many",
    }
    reported = submit_step(lab, "browser_click")["action"]["page_reported"]
    assert reported == {
        "failed_requests": [
            {"method": "POST", "path": "/api/a", "status": 500},
            {"path": "/api/b", "status": 404},
            {"method": "GET", "path": "/api/e", "status": 503},
            {"method": "GET", "path": "/api/f", "status": 503},
            {"method": "GET", "path": "/api/g", "status": 503},
        ],
        "more_failed_requests": 5,
    }
