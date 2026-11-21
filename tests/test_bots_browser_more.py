"""The owl's further browser tools on the host: back, forward and reload (reading), right, middle
and double clicks and choosing several options (input, reviewed like a click), images of one
element or the whole page (reading, for image models), and answering a file chooser a step opened
with a library file (sharing a file, like an upload). Each is offered, and runs, only while the
owl computer's browser declares it; the fake browser accepts only the managed browser's own call
signatures."""

from __future__ import annotations

import base64
import hashlib
import inspect
import struct
import time
from types import SimpleNamespace

import pytest
from test_bots_browser import PREVIEW, ExecutorFixture, token
from test_bots_browser_hands import HandsBrowser, form_page
from test_bots_browser_page_view import Page, ref

from alysis_code.bots import runtime
from alysis_code.bots.browser import BrowserComputers, BrowserImageResult
from alysis_code.bots.browser_tools import (
    DEFINITIONS,
    NO_WEBSITE_APPROVAL_TOOLS,
    PARAMETER_OPERATIONS,
    TOOL_OPERATIONS,
    BrowserObservationExpired,
    BrowserRefusal,
)
from alysis_code.bots.cloud_browser import OPERATIONS, RemoteBrowser
from alysis_code.bots.computer import ComputerViews
from alysis_code.ide.managed_browser import BrowserVisualFrame, ManagedBrowserService

FURTHER = ("history", "click_options", "select_options", "page_images", "file_chooser")
# An owl computer image from before these operations.
OLDER = tuple(operation for operation in OPERATIONS if operation not in FURTHER)
FURTHER_TOOLS = {"browser_history", "browser_select_many", "browser_choose_file"}
FILE = {
    "id": "f" * 32,
    "name": "Βεβαίωση εγγραφής.pdf",
    "size_bytes": 18,
    "sha256": hashlib.sha256(b"%PDF-1.7 synthetic").hexdigest(),
}


def png(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", width, height) + b"\0" * 40


class MoreBrowser(HandsBrowser):
    """The hands browser, also moving through history, choosing several options, taking images
    of a part of the page, and opening a file chooser from a click."""

    def __init__(self, page, fields):
        super().__init__(page, fields)
        self.stays = False
        self.chooser = None  # what the next click reports opening
        self.page_menu = None

    def history(self, owner, session_id, move, **kwargs):
        self._call("history", owner, session_id, move, **kwargs)
        if self.stays:
            return {"moved": False, "dialogs": [{"type": "beforeunload", "accepted": False}]}
        self._acted()
        return {"moved": True, "url": PREVIEW}

    def click(self, owner, session_id, selector, **kwargs):
        super().click(owner, session_id, selector, **kwargs)
        return {
            "clicked": True,
            "command_generation": self.generation,
            **({"page_menu": self.page_menu} if self.page_menu is not None else {}),
            **({"file_chooser": self.chooser} if self.chooser else {}),
        }

    def select_options(self, owner, session_id, selector, **kwargs):
        self._call("select_options", owner, session_id, selector, **kwargs)
        self._acted()
        return {"dispatched": True, "changed": True, "confirmed": True, "selected": ["Αθήνα"]}

    def capture_image(self, owner, session_id, selector=None, **kwargs):
        self._call("capture_image", owner, session_id, selector, **kwargs)
        return {
            "kind": "full_page" if kwargs.get("full_page") else "element",
            "png": base64.b64encode(png(300, 120)).decode(),
            "width": 300,
            "height": 120,
            "scale": 1.0,
            "truncated": False,
        }

    def capture_visual_frame(self, owner, session_id, **kwargs):
        self._call("capture_visual_frame", owner, session_id, **kwargs)
        return BrowserVisualFrame(
            owner, session_id, self.generation, time.monotonic(), PREVIEW, 2, 2, 2, 2, png(2, 2)
        )

    def click_visual_frame(self, owner, session_id, frame, x, y, **kwargs):
        self._call("click_visual_frame", owner, session_id, frame, x, y, **kwargs)
        self._acted()
        return {"dispatched": True, "command_generation": self.generation}

    def upload_file(self, owner, session_id, selector, *, chunks, **kwargs):
        # Its own keyword "name" (the file's) is checked against the managed browser's call.
        inspect.signature(ManagedBrowserService.upload_file).bind(
            None, owner, session_id, selector, chunks=chunks, **kwargs
        )
        self.calls.append(
            ("upload_file", owner, session_id, (selector,), {**kwargs, "chunks": b"".join(chunks)})
        )
        self._acted()
        return {"selected": True, "command_generation": self.generation}


class Store:
    """The bot's library with one file and files enabled."""

    def bot(self, owner, bot_id):
        return {"files_enabled": True}

    def file(self, owner, bot_id, file_id):
        if file_id != FILE["id"]:
            raise KeyError(file_id)
        return dict(FILE)

    def file_chunks(self, owner, bot_id, file_id):
        yield b"%PDF-1.7 synthetic"


def browser_task():
    return {"id": "task-a", "bot_id": "bot-a", "config": {"files_enabled": True}}


class Lab:
    def __init__(self, page=None, fields=None, *, features=OPERATIONS, previews=False):
        if page is None:
            page, fields = form_page()
        self.browser = MoreBrowser(page, fields)
        self.features = features
        self.reviewed: list[tuple[str, dict, dict, dict]] = []
        self.adapter = BrowserComputers(
            "owner", ComputerViews(), self.factory, interactive_previews=previews, store=Store()
        )

    def factory(self, bot_id, cancel):
        executor = ExecutorFixture(bot_id, self.adapter.views)
        executor.browser = self.browser
        executor.browser_features = frozenset(self.features)
        return executor

    def review(self, name, arguments, context, **options):
        self.reviewed.append((name, arguments, context, options))

    def call(self, name="browser_observe", args=None, *, request=True, images=False):
        return self.adapter.call(
            browser_task(),
            name,
            args or {},
            token(),
            image_input=images,
            input_by_request=request,
            review_action=self.review,
        )

    def offered(self, *, images=True, request=True):
        schemas = self.adapter.schemas(
            browser_task(), True, image_input=images, input_by_request=request
        )
        return {
            item["function"]["name"]: item["function"]["parameters"]
            for item in self.adapter.offered(browser_task(), schemas)
        }

    def sent(self, name):
        return [call for call in self.browser.calls if call[0] == name]

    def close(self):
        self.adapter.close()


@pytest.fixture
def lab():
    made = Lab()
    yield made
    made.close()


def view_ref(observation, start):
    return ref(observation["page_view"], start)


# ---------------------------------------------------------------------------------------------
# Offered only where the owl computer's browser declares them


@pytest.mark.parametrize("features", [OLDER, OPERATIONS], ids=["older", "newer"])
def test_the_further_tools_and_options_are_offered_only_by_a_browser_that_declares_them(features):
    lab = Lab(features=features)
    try:
        lab.call()
        offered = lab.offered()
        newer = features == OPERATIONS
        assert (FURTHER_TOOLS <= set(offered)) is newer
        assert not FURTHER_TOOLS & set(offered) or newer
        for name, options in PARAMETER_OPERATIONS.items():
            assert (set(options) <= set(offered[name]["properties"])) is newer, name
        # Every schema is an object without a top-level combinator, in both.
        for parameters in offered.values():
            assert parameters["type"] == "object"
            assert not {"anyOf", "oneOf", "allOf"} & set(parameters)
    finally:
        lab.close()


def test_an_older_owl_computer_refuses_the_further_options_before_anything_is_sent():
    lab = Lab(features=OLDER)
    try:
        observation = lab.call()
        send = view_ref(observation, '- button "Αποστολή"')
        before = len(lab.browser.calls)
        attempts = [
            (
                "browser_click",
                {"source_id": observation["source_id"], "node": send, "button": "right"},
            ),  # fmt: skip
            ("browser_click", {"source_id": observation["source_id"], "node": send, "clicks": 2}),
            ("browser_history", {"move": "back"}),
        ]
        for name, arguments in attempts:
            with pytest.raises(BrowserRefusal, match="newer owl computer"):
                lab.call(name, arguments)
        with pytest.raises(BrowserRefusal, match="newer owl computer"):
            lab.call("browser_screenshot", {"full_page": True}, images=True)
        assert lab.browser.calls[before:] == [] and lab.reviewed == []
        # The same click without the options is today's click.
        lab.call("browser_click", {"source_id": observation["source_id"], "node": send})
        ((_, _, _, _, options),) = lab.sent("click")
        assert "button" not in options and "click_count" not in options
    finally:
        lab.close()


# ---------------------------------------------------------------------------------------------
# Approval classification: moving and images are reading; the new input is input


def test_back_forward_and_reload_are_reading_without_website_approval():
    lab = Lab()
    try:
        lab.call("browser_navigate", {"url": PREVIEW}, request=False)
        for move in ("back", "forward", "reload"):
            moved = lab.call("browser_history", {"move": move}, request=False)
            assert moved["history"] == {"move": move, "moved": True} and "page_view" in moved
        assert [call[3] for call in lab.sent("history")] == [("back",), ("forward",), ("reload",)]
        assert all(call[4]["expected_generation"] == 17 for call in lab.sent("history"))
        assert lab.reviewed == []  # never reviewed
        lab.browser.stays = True
        stayed = lab.call("browser_history", {"move": "back"}, request=False)
        assert stayed["history"]["moved"] is False and "asked to stay" in stayed["message"]
        assert stayed["history"]["dialogs_answered"][0]["accepted"] is False
    finally:
        lab.close()
    assert "browser_history" in NO_WEBSITE_APPROVAL_TOOLS
    assert not {"browser_select_many", "browser_choose_file"} & NO_WEBSITE_APPROVAL_TOOLS


@pytest.mark.parametrize(
    "extra,sent",
    [
        ({"button": "right"}, {"button": "right"}),
        ({"button": "middle"}, {"button": "middle"}),
        ({"clicks": 2}, {"click_count": 2}),
        ({"clicks": 3, "button": "right"}, {"click_count": 3, "button": "right"}),
    ],
)
def test_right_middle_and_double_clicks_are_input_reviewed_like_a_click(lab, extra, sent):
    observation = lab.call()
    send = view_ref(observation, '- button "Αποστολή"')
    arguments = {"source_id": observation["source_id"], "node": send, **extra}
    # Without the person's request or a site's approval nothing is sent.
    with pytest.raises(BrowserObservationExpired, match="requires website approval"):
        lab.call("browser_click", arguments, request=False)
    assert lab.sent("click") == []
    lab.browser.page_menu = True if extra.get("button") == "right" else None
    result = lab.call("browser_click", arguments)
    ((name, reviewed, context, options),) = lab.reviewed
    assert name == "browser_click" and "website_effects" in options
    assert {key: reviewed[key] for key in extra} == extra and context["label"] == "Αποστολή"
    ((_, _, _, _, call),) = lab.sent("click")
    assert {key: call[key] for key in sent} == sent
    # A right or middle click is never redirected to a label.
    assert "label_backend_node_ids" not in call or extra.get("button", "left") == "left"
    if extra.get("button") == "right":
        assert result["action"]["page_menu"] is True


def test_a_plain_click_is_reviewed_and_remembered_as_it_always_was(lab):
    observation = lab.call()
    send = view_ref(observation, '- button "Αποστολή"')
    lab.call("browser_click", {"source_id": observation["source_id"], "node": send})
    ((_, reviewed, _, _),) = lab.reviewed
    # Saved exact-action rules and pending approvals keep matching a click without options.
    assert set(reviewed) == {"source_id", "node"}


def multiple_page():
    """A form with a list where several cities can be chosen."""
    page = Page(title="Προτιμήσεις")
    form = page.add(page.body, "FORM", "form", name="")
    cities = page.add(form, "SELECT", "listbox", name="Πόλεις", attrs={"multiple": ""},
                      props={"multiselectable": True, "focusable": True})  # fmt: skip
    other = page.add(form, "SELECT", "listbox", name="Άλλη λίστα", attrs={"multiple": ""})
    fields = {"cities": cities[0]}
    for parent, names in ((cities, ("Αθήνα", "Πάτρα", "Βόλος")), (other, ("Ρόδος",))):
        for name in names:
            option = page.add(parent, "OPTION", "option", name=name, props={"focusable": True})
            page.text(option, name)
            fields[name] = option[0]
    send = page.add(form, "BUTTON", "button", name="Αποθήκευση", props={"focusable": True})
    page.text(send, "Αποθήκευση")
    return page, fields


def test_choosing_several_options_is_input_reviewed_like_a_select():
    page, fields = multiple_page()
    lab = Lab(page, fields)
    try:
        observation = lab.call()
        cities = view_ref(observation, '- listbox "Πόλεις"')
        athens = view_ref(observation, '- option "Αθήνα"')
        volos = view_ref(observation, '- option "Βόλος"')
        arguments = {"source_id": observation["source_id"], "node": cities,
                     "option_nodes": [volos, athens]}  # fmt: skip
        with pytest.raises(BrowserObservationExpired, match="requires website approval"):
            lab.call("browser_select_many", arguments, request=False)
        result = lab.call("browser_select_many", arguments)
        ((name, reviewed, context, options),) = lab.reviewed
        # Reviewed with the options it chooses, as the page names them, in their order.
        assert name == "browser_select_many" and len(reviewed["option_nodes"]) == 2
        assert context["options"] == ["Βόλος", "Αθήνα"]
        # It only enters a value: it never sends its form, whatever the form is.
        assert context["website_effects"]["form"]["submits"] is False
        assert options["website_effects"] == ()
        ((_, _, _, selector, call),) = lab.sent("select_options")
        assert call["option_backend_node_ids"] == [
            page.backend_of(fields["Βόλος"]),
            page.backend_of(fields["Αθήνα"]),
        ]
        assert call["expected_backend_node_id"] == page.backend_of(fields["cities"])
        assert result["action"]["selected"] == ["Αθήνα"] and result["action"]["confirmed"]
        # An option of another list is refused before anything is sent.
        observation = lab.call()
        rhodes = view_ref(observation, '- option "Ρόδος"')
        with pytest.raises(BrowserRefusal, match="does not belong"):
            lab.call(
                "browser_select_many",
                {"source_id": observation["source_id"],
                 "node": view_ref(observation, '- listbox "Πόλεις"'), "option_nodes": [rhodes]},
            )  # fmt: skip
        assert len(lab.sent("select_options")) == 1
    finally:
        lab.close()


@pytest.mark.parametrize("arguments", [{"option_nodes": []}, {"option_nodes": [3, 3]}])
def test_unclear_option_lists_are_refused_before_any_browser_step(lab, arguments):
    observation = lab.call()
    before = len(lab.browser.calls)
    with pytest.raises(ValueError):
        lab.call(
            "browser_select_many", {"source_id": observation["source_id"], "node": 1, **arguments}
        )
    assert len(lab.browser.calls) == before


# ---------------------------------------------------------------------------------------------
# Images of one element or of the whole page


def test_an_element_or_full_page_image_is_reading_for_image_models_only(lab):
    assert "browser_screenshot" not in lab.offered(images=False)
    observation = lab.call(images=True)
    name = view_ref(observation, '- textbox "Όνομα"')
    arguments = {"source_id": observation["source_id"], "node": name}
    with pytest.raises(BrowserRefusal, match="image-capable"):
        lab.call("browser_screenshot", arguments)
    # A screenshot of the visible page, which a visual click can use...
    shot = lab.call("browser_screenshot", {}, images=True)
    assert isinstance(shot, BrowserImageResult) and not shot.caption
    # ...then an image of one element: clipped to that ref's element, for looking only.
    image = lab.call("browser_screenshot", arguments, images=True, request=False)
    assert isinstance(image, BrowserImageResult)
    ((_, _, _, selector, call),) = lab.sent("capture_image")
    assert call["expected_backend_node_id"] == lab.browser.page.backend_of(
        lab.browser.fields["name"]
    )
    assert selector[0] is not None and "full_page" not in call
    assert image.observation["image"] == {
        "kind": "element", "width": 300, "height": 120, "scale": 1.0, "truncated": False,
    }  # fmt: skip
    assert "for looking only" in image.caption and "e" + str(name) in image.caption
    assert image.image_url.startswith("data:image/png;base64,")
    # Its pixels are no frame a visual click can use: the earlier screenshot no longer is either.
    with pytest.raises(BrowserObservationExpired, match="current browser_screenshot"):
        lab.call(
            "browser_visual_click", {"source_id": shot.observation["source_id"], "x": 1, "y": 1},
            images=True,
        )  # fmt: skip
    assert lab.sent("click_visual_frame") == []
    page = lab.call("browser_screenshot", {"full_page": True}, images=True, request=False)
    assert lab.sent("capture_image")[-1][4]["full_page"] is True
    assert page.observation["image"]["kind"] == "full_page"
    assert lab.reviewed == []  # images are never reviewed


def test_a_visual_right_or_double_click_is_reviewed_and_sends_its_options(lab):
    lab.call()
    shot = lab.call("browser_screenshot", {}, images=True)
    arguments = {"source_id": shot.observation["source_id"], "x": 1, "y": 1, "button": "right"}
    lab.call("browser_visual_click", arguments, images=True)
    ((name, reviewed, _, _),) = lab.reviewed
    assert name == "browser_visual_click" and reviewed["button"] == "right"
    assert "clicks" not in reviewed  # left at its default
    ((_, _, _, _, call),) = lab.sent("click_visual_frame")
    assert call["button"] == "right" and "click_count" not in call


# ---------------------------------------------------------------------------------------------
# A file chooser a step opened


def opened_chooser(lab, chooser):
    observation = lab.call()
    send = view_ref(observation, '- button "Αποστολή"')
    lab.browser.chooser = chooser
    clicked = lab.call("browser_click", {"source_id": observation["source_id"], "node": send})
    lab.browser.chooser = None
    lab.reviewed.clear()
    return clicked["action"]["file_chooser"]


CHOOSER = {
    "backend_node_id": 70,
    "frame_id": "FRAME-MAIN",
    "url": PREVIEW,
    "loader": "L-1",
    "multiple": False,
}


def test_a_file_chooser_is_answered_with_a_library_file_after_the_persons_approval(lab):
    reported = opened_chooser(lab, CHOOSER)
    assert set(reported) == {"source_id", "multiple", "message"}
    assert "browser_choose_file" in reported["message"]
    answer = {"source_id": reported["source_id"], "file_id": FILE["id"]}
    # Only the person's request permits it here, and sharing a file waits for them.
    with pytest.raises(BrowserObservationExpired, match="requires website approval"):
        lab.call("browser_choose_file", answer, request=False)
    result = lab.call("browser_choose_file", answer)
    ((name, _, context, options),) = lab.reviewed
    assert name == "browser_choose_file" and options == {"website_effects": ("upload",)}
    assert context["file_chooser"]["file"] == FILE["name"] and context["url"] == PREVIEW
    ((_, _, _, selector, call),) = lab.sent("upload_file")
    assert selector == (None,) and call["chunks"] == b"%PDF-1.7 synthetic"
    assert (call["name"], call["size"], call["expected_sha256"]) == (
        FILE["name"],
        FILE["size_bytes"],
        FILE["sha256"],
    )
    assert (call["expected_backend_node_id"], call["frame_id"]) == (70, "FRAME-MAIN")
    assert (call["expected_frame_loader"], call["expected_generation"]) == ("L-1", 17)
    assert result["action"]["name"] == "browser_choose_file"
    assert result["action"]["file"] == FILE["name"] and result["action"]["selected"] is True
    # Answered once: the same report answers nothing more.
    with pytest.raises(BrowserObservationExpired, match="no longer open"):
        lab.call("browser_choose_file", answer)
    assert len(lab.sent("upload_file")) == 1


def test_a_chooser_is_not_answered_after_another_step_or_on_an_unapproved_frame(lab):
    reported = opened_chooser(lab, CHOOSER)
    # Another step (it may change the page) ends it; reading the page does not.
    lab.call("browser_observe")
    observation = lab.call()
    lab.call("browser_click", {"source_id": observation["source_id"],
                               "node": view_ref(observation, '- button "Αποστολή"')})  # fmt: skip
    with pytest.raises(BrowserObservationExpired, match="no longer open"):
        lab.call("browser_choose_file", {"source_id": reported["source_id"], "file_id": FILE["id"]})
    # A chooser of another site's frame shares the file with that site: it needs its approval.
    reported = opened_chooser(lab, {**CHOOSER, "url": "https://pay.example.net/upload"})
    with pytest.raises(BrowserRefusal, match="outside the task's approval"):
        lab.adapter.call(
            browser_task(), "browser_choose_file",
            {"source_id": reported["source_id"], "file_id": FILE["id"]}, token(),
            approved_origins=(PREVIEW,), review_action=lab.review,
        )  # fmt: skip
    assert lab.sent("upload_file") == [] and lab.reviewed == []
    # A picker without a file input can't be answered by the owl, and says so.
    unanswerable = opened_chooser(lab, {"frame_id": "FRAME-MAIN", "multiple": True})
    assert unanswerable["answerable"] is False and "source_id" not in unanswerable


def test_choosing_a_file_needs_files_and_a_newer_computer():
    lab = Lab()
    lab.adapter.store = None
    try:
        lab.call()
        assert "browser_choose_file" not in lab.offered()
        with pytest.raises(BrowserRefusal, match="File access is unavailable"):
            lab.call("browser_choose_file", {"source_id": "x" * 32, "file_id": FILE["id"]})
    finally:
        lab.close()
    lab = Lab(features=OLDER)
    try:
        # An older computer reports no chooser: a click's report is ignored, nothing is kept.
        assert "file_chooser" not in opened_chooser_or_none(lab, CHOOSER)
    finally:
        lab.close()


def opened_chooser_or_none(lab, chooser):
    observation = lab.call()
    send = view_ref(observation, '- button "Αποστολή"')
    lab.browser.chooser = chooser
    return lab.call("browser_click", {"source_id": observation["source_id"], "node": send})[
        "action"
    ]


# ---------------------------------------------------------------------------------------------
# Live step lines, the cloud bridge and the tool table


def test_a_live_step_line_can_name_the_kind_of_browser_step():
    started = runtime.step_started
    assert started("browser_history", {"move": "back"})["variant"] == "back"
    assert started("browser_click", {"button": "right"})["variant"] == "right_click"
    assert started("browser_click", {"clicks": 2})["variant"] == "double_click"
    assert started("browser_visual_click", {"clicks": 3})["variant"] == "triple_click"
    assert started("browser_screenshot", {"full_page": True})["variant"] == "full_page"
    assert started("browser_screenshot", {"source_id": "s", "node": 4})["variant"] == "element"
    for name, arguments in (
        ("browser_click", {}),
        ("browser_click", {"clicks": [2]}),
        ("browser_history", {"move": "Πίσω"}),
        ("browser_screenshot", {}),
    ):
        assert "variant" not in started(name, arguments)


def test_the_cloud_bridge_sends_visual_click_options_only_when_used():
    sent = []
    surface = SimpleNamespace(_request=lambda operation, arguments, cancel: sent.append(arguments))
    remote = RemoteBrowser(SimpleNamespace(owner="o", session_id="s", _surface=surface))
    frame = SimpleNamespace(frame_id="f")
    remote.click_visual_frame("o", "s", frame, 1, 2)
    remote.click_visual_frame("o", "s", frame, 1, 2, button="right", click_count=2)
    assert sent[0] == {"method": "visual_click", "frame_id": "f", "x": 1, "y": 2}
    assert sent[1]["options"] == {"button": "right", "click_count": 2}
    for method in ("history", "select_options", "capture_image"):
        assert callable(getattr(remote, method))


def test_every_further_tool_names_an_operation_the_browser_declares():
    for name in FURTHER_TOOLS:
        assert TOOL_OPERATIONS[name] in OPERATIONS and name in DEFINITIONS
    for options in PARAMETER_OPERATIONS.values():
        assert set(options.values()) <= set(OPERATIONS)


# ---------------------------------------------------------------------------------------------
# What the website reported during a step (page_reported)


def test_the_further_acting_steps_say_what_the_website_reported_and_reading_ones_ask_nothing():
    from test_bots_browser_hands import FAILED_SEND

    page, fields = multiple_page()
    lab = Lab(page, fields)
    try:
        reported = {"failed_requests": FAILED_SEND["failed_requests"], "uncaught_errors": 1}

        def acted(name, operation, arguments, **options):
            lab.browser.reported = dict(FAILED_SEND)
            before = len(lab.sent("page_report"))
            result = lab.call(name, arguments, **options)
            action = (result.observation if isinstance(result, BrowserImageResult) else result)[
                "action"
            ]
            assert action["page_reported"] == reported, name
            # Read once, after the step and the observation that followed it.
            assert len(lab.sent("page_report")) == before + 1
            names = [call[0] for call in lab.browser.calls]
            last = len(names) - 1 - names[::-1].index(operation)
            assert "page_report" in names[last + 1 :] and "snapshot" in names[last + 1 :]
            return action

        observation = lab.call()
        save = view_ref(observation, '- button "Αποθήκευση"')
        arguments = {"source_id": observation["source_id"], "node": save, "clicks": 2}
        acted("browser_click", "click", arguments)
        observation = lab.call()
        acted(
            "browser_select_many",
            "select_options",
            {"source_id": observation["source_id"],
             "node": view_ref(observation, '- listbox "Πόλεις"'),
             "option_nodes": [view_ref(observation, '- option "Πάτρα"')]},
        )  # fmt: skip
        lab.call()
        shot = lab.call("browser_screenshot", {}, images=True)
        acted(
            "browser_visual_click",
            "click_visual_frame",
            {"source_id": shot.observation["source_id"], "x": 1, "y": 1, "button": "right"},
            images=True,
        )
        observation = lab.call()
        lab.browser.chooser = CHOOSER
        save = view_ref(observation, '- button "Αποθήκευση"')
        clicked = lab.call("browser_click", {"source_id": observation["source_id"], "node": save})
        lab.browser.chooser = None
        held = clicked["action"]["file_chooser"]["source_id"]
        acted("browser_choose_file", "upload_file", {"source_id": held, "file_id": FILE["id"]})
        # Moving through history and page images read the page: no report is asked for.
        asked = len(lab.sent("page_report"))
        lab.browser.reported = dict(FAILED_SEND)
        lab.call("browser_history", {"move": "back"}, request=False)
        lab.call("browser_screenshot", {"full_page": True}, images=True, request=False)
        assert len(lab.sent("page_report")) == asked
    finally:
        lab.close()
