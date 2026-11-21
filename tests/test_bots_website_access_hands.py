"""The newer browser tools (fill, key chord, check, drag, answer a dialog) and elements inside
frames, under the website permission the person's own request gives (test_bots_website_access).

Every acting tool is website input like a click: in the person's own request it needs no card,
passes the action review with what the page's HTML says it does, and waits for the person when
it pays, signs in, uploads, hands over personal details or answers a dialog with an effect. An
element in another site's frame follows the same rule for that frame's site; its form is read in
the frame's own document when the browser's snapshot holds it, and when it does not (another
site's frame runs in its own process) the step waits for the person. Routine runs keep their
declared sites for every tool. Pages in Greek, Chinese and English; real store, runtime, adapter
and reviewer, with the fake browser of test_bots_browser_hands.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from test_bots_browser import ExecutorFixture, token
from test_bots_browser import task as browser_task
from test_bots_browser_hands import PAY, PAY_FRAME, HandsBrowser
from test_bots_browser_page_view import Page, ref
from test_bots_routine_access import allow, listed, routine, start_run
from test_bots_website_access import WORDS, Script, ask, at, runtime_for, tool, verdict

from alysis_code.bots.browser import BrowserComputers
from alysis_code.bots.browser_effects import EFFECT_REASONS, UNREADABLE_FRAME, origin
from alysis_code.bots.browser_tools import (
    DEFINITIONS,
    DIALOG_TOOLS,
    BrowserInputNotSent,
    BrowserObservationExpired,
    BrowserRefusal,
)
from alysis_code.bots.cloud_browser import OPERATIONS
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.models import BotInput
from alysis_code.bots.store import Store
from alysis_code.ide.managed_browser import BrowserNoDialogError

SHOP = "https://shop.example/checkout"
FIELD = {"editable": "plaintext", "focusable": True}
FRAME_WORDS = {
    "el": {"card": "Αριθμός κάρτας", "save": "Αποθήκευση κάρτας", "pay": "Πληρωμή"},
    "zh": {"card": "卡号", "save": "保存卡片", "pay": "支付"},
    "en": {"card": "Card number", "save": "Save this card", "pay": "Pay"},
}


# ---------------------------------------------------------------------------------------------
# Pages and the browser


def shop(words) -> Page:
    """A shop page: a search form, a sort list to reorder, and a frame for the payment."""
    page = Page(SHOP, title=words["results"])
    search = page.add(page.body, "FORM", "search", name="",
                      attrs={"role": "search", "action": "/search", "method": "get"})  # fmt: skip
    page.add(search, "INPUT", "searchbox", name=words["search"], value="",
             attrs={"type": "search", "name": "q"}, props=FIELD)  # fmt: skip
    first = page.add(page.body, "LI", "listitem", name=words["newest"])
    page.text(first, words["newest"])
    second = page.add(page.body, "LI", "listitem", name=words["cheapest"])
    page.text(second, words["cheapest"])
    page.add(page.body, "IFRAME", "Iframe", name="checkout", props={"focusable": True})
    return page


def payment_frame(labels) -> Page:
    """The payment form the frame shows: a card number, a save-card box and a pay button."""
    page = Page(PAY, title="Payment", frame=PAY_FRAME, first_backend=5000)
    form = page.add(page.body, "FORM", "form", name="", attrs={"method": "post"})
    page.add(form, "INPUT", "textbox", name=labels["card"], value="",
             attrs={"autocomplete": "cc-number"}, props=FIELD)  # fmt: skip
    page.add(form, "INPUT", "checkbox", name=labels["save"], attrs={"type": "checkbox"},
             props={"checked": "false", "focusable": True})  # fmt: skip
    pay = page.add(form, "BUTTON", "button", name=labels["pay"], props={"focusable": True})
    page.text(pay, labels["pay"])
    return page


def with_frame_document(data: dict, frame: dict) -> dict:
    """A DOM snapshot that also holds a frame's document, as the browser's snapshot holds a
    frame of the page's own process (its strings appended to the shared table)."""
    data = copy.deepcopy(data)
    base = len(data["strings"])
    data["strings"] += frame["strings"]
    document = copy.deepcopy(frame["documents"][0])

    def shift(index):
        return index if index < 0 else index + base

    for key in ("documentURL", "title", "frameId"):
        document[key] = shift(document[key])
    nodes = document["nodes"]
    for key in ("nodeName", "nodeValue"):
        nodes[key] = [shift(index) for index in nodes[key]]
    nodes["attributes"] = [[shift(index) for index in pairs] for pairs in nodes["attributes"]]
    for rare in ("inputValue", "shadowRootType"):
        nodes[rare]["value"] = [shift(index) for index in nodes[rare]["value"]]
    data["documents"].append(document)
    return data


class FrameBrowser(HandsBrowser):
    """The hands browser with another site's frame: its elements in the accessibility snapshot,
    and its document in the page's DOM snapshot only when ``readable`` (a frame of the page's
    process). Otherwise it runs in its own process: a browser that declares frame snapshots
    returns its document with a DOM snapshot asked for frames, unless ``frame_left_out``."""

    def __init__(self, page: Page, frame: Page | None = None, *, readable: bool = True):
        super().__init__(page, {})
        self.frame, self.readable = frame, readable
        self.frame_left_out = False
        self.click_dialogs: list[dict] = []  # what the browser reports a click answered
        if frame is not None:
            nodes = frame.build()[1]["nodes"]

            def prefixed(node_id):
                return f"{PAY_FRAME}/{node_id}"

            for node in nodes:
                node["nodeId"] = prefixed(node["nodeId"])
                node["childIds"] = [prefixed(child) for child in node["childIds"]]
                if "parentId" in node:
                    node["parentId"] = prefixed(node["parentId"])
            self.frame_nodes = nodes

    def snapshot(self, owner, session_id, **kwargs):
        result = super().snapshot(owner, session_id, **kwargs)
        if kwargs.get("kind") == "dom" and self.frame is not None and self.readable:
            result["data"] = with_frame_document(result["data"], self.frame.build()[0])
        if kwargs.get("kind") == "accessibility" and self.frame is not None:
            # The frame is described with its own address and site.
            for frame in result["data"]["frames"]:
                if frame["id"] == PAY_FRAME:
                    frame["url"] = self.frame.strings[self.frame.url_string]
                    frame["origin"] = origin(frame["url"])
        if kwargs.get("frames"):
            result["frames"] = []
            result["omitted_frames"] = 0
            if self.frame is not None and not self.readable and not self.frame_left_out:
                address = self.frame.strings[self.frame.url_string]
                result["frames"].append(
                    {
                        "id": PAY_FRAME,
                        "url": address,
                        "origin": origin(address),
                        "data": self.frame.build()[0],
                    }
                )
            elif self.frame is not None and not self.readable:
                result["omitted_frames"] = 1
        return result

    def click(self, owner, session_id, selector, **kwargs):
        super().click(owner, session_id, selector, **kwargs)
        return {"clicked": True, **({"dialogs": self.click_dialogs} if self.click_dialogs else {})}

    def handle_dialog(self, owner, session_id, **kwargs):
        self._call("handle_dialog", owner, session_id, **kwargs)
        if self.dialog is None:
            raise BrowserNoDialogError("No dialog is open on this page. Nothing was answered.")
        kind, message, *_ = self.dialog
        self.dialog = None
        self._acted()
        return {"dialog": {"type": kind, "message": message, "accepted": kwargs["accept"]}}

    def set_checked(self, owner, session_id, selector, checked, **kwargs):
        if not kwargs.get("frame_id"):
            return super().set_checked(owner, session_id, selector, checked, **kwargs)
        self._call("set_checked", owner, session_id, selector, checked, **kwargs)
        self._acted()
        return {"changed": True, "checked": checked}


def computers(browser, features=OPERATIONS) -> BrowserComputers:
    """The owl's browser computers, running ``browser`` with ``features`` (by default today's
    newest operations; fewer for an older owl computer image)."""

    def factory(bot_id, cancel):
        executor = ExecutorFixture(bot_id, adapter.views)
        executor.browser = browser
        executor.browser_features = frozenset(features)
        return executor

    adapter = BrowserComputers("owner", ComputerViews(), factory)
    return adapter


def sent(browser, *names) -> list[str]:
    return [call[0] for call in browser.calls if call[0] in names]


INPUT = ("fill", "press_key", "set_checked", "drag", "click", "handle_dialog")
# An owl computer image from before its browser read other sites' frames, answered a step's own
# dialog or named a dialog's frame.
OLDER = tuple(
    operation
    for operation in OPERATIONS
    if operation not in {"frame_snapshots", "step_dialog", "dialog_frames"}
)


# ---------------------------------------------------------------------------------------------
# The adapter: every acting tool takes the request's permission and its review


@pytest.mark.parametrize("language", ["el", "zh"])
def test_every_newer_acting_tool_works_by_the_persons_request_with_its_review(language):
    words = WORDS[language]
    browser = FrameBrowser(shop(words))
    adapter = computers(browser)
    reviewed = []

    def review(name, arguments, context, **options):
        reviewed.append((name, context, options))

    try:
        observation = adapter.call(browser_task(), "browser_observe", {}, token())
        view, source = observation["page_view"], observation["source_id"]
        box = ref(view, f'- searchbox "{words["search"]}"')
        steps = [
            ("browser_fill", {"node": box, "text": "βραστήρας 水壶"}),
            ("browser_key_chord", {"node": box, "key": "Enter", "modifiers": ["ControlOrMeta"]}),
            (
                "browser_drag",
                {
                    "node": ref(view, f'- listitem "{words["cheapest"]}"'),
                    "to_node": ref(view, f'- listitem "{words["newest"]}"'),
                },
            ),
        ]
        for name, arguments in steps:
            # Without the request's permission (or a card) nothing is sent.
            with pytest.raises(BrowserObservationExpired, match="website approval"):
                adapter.call(
                    browser_task(), name, {"source_id": source, **arguments}, token(),
                    review_action=review,
                )  # fmt: skip
            result = adapter.call(
                browser_task(), name, {"source_id": source, **arguments}, token(),
                review_action=review, input_by_request=True,
            )  # fmt: skip
            source = result["source_id"]
        assert sent(browser, *INPUT) == ["fill", "press_key", "drag"]
        assert [name for name, _, _ in reviewed] == [name for name, _ in steps]
        assert all(options == {"website_effects": ()} for _, _, options in reviewed)
        # A key chord with Enter in the search field submits its search form: nothing settled.
        chord = reviewed[1][1]["website_effects"]
        assert chord["effects"] == [] and chord["form"]["submits"] is True
    finally:
        adapter.close()


@pytest.mark.parametrize("language", ["el", "zh", "en"])
def test_a_payment_frame_form_is_read_in_the_frames_own_document(language):
    labels = FRAME_WORDS[language]
    browser = FrameBrowser(shop(WORDS[language]), payment_frame(labels))
    adapter = computers(browser)
    seen = []

    def review(name, arguments, context, **options):
        seen.append((name, context, options))

    try:
        observation = adapter.call(browser_task(), "browser_observe", {}, token())
        view, source = observation["page_view"], observation["source_id"]
        card = ref(view, f'- textbox "{labels["card"]}"')
        save = ref(view, f'- checkbox "{labels["save"]}"')
        pay = ref(view, f'- button "{labels["pay"]}"')
        for name, arguments in [
            ("browser_fill", {"node": card, "text": "4242 4242 4242 4242"}),
            ("browser_check", {"node": save, "checked": True}),
            ("browser_click", {"node": pay}),
        ]:
            result = adapter.call(
                browser_task(), name, {"source_id": source, **arguments}, token(),
                review_action=review, input_by_request=True,
            )  # fmt: skip
            source = result["source_id"]
        # Typing the card number and pressing pay are paying; ticking the box is left to the
        # reviewer. Each was read in the frame's own form, and reaches that frame by identity.
        assert [options for _, _, options in seen] == [
            {"website_effects": ("pay",)},
            {"website_effects": ()},
            {"website_effects": ("pay",)},
        ]
        form = seen[2][1]["website_effects"]["form"]
        assert form["submits"] is True and form["payment_fields"] == 1
        assert form["submits_to_origin"] == "https://pay.example.net"
        assert seen[2][1]["frame_url"] == PAY
        calls = [call for call in browser.calls if call[0] in INPUT]
        assert all(call[4]["frame_id"] == PAY_FRAME for call in calls)
    finally:
        adapter.close()


def test_a_frame_whose_form_the_snapshot_does_not_hold_waits_for_the_person():
    # An older owl computer reads no other process's frame.
    labels = FRAME_WORDS["zh"]
    browser = FrameBrowser(shop(WORDS["zh"]), payment_frame(labels), readable=False)
    adapter = computers(browser, OLDER)
    seen = []

    def review(name, arguments, context, **options):
        seen.append((context, options))
        raise BrowserRefusal("Waiting for the person.")

    try:
        observation = adapter.call(browser_task(), "browser_observe", {}, token())
        view = observation["page_view"]
        for name, arguments in [
            ("browser_fill", {"node": ref(view, f'- textbox "{labels["card"]}"'), "text": "1"}),
            (
                "browser_check",
                {"node": ref(view, f'- checkbox "{labels["save"]}"'), "checked": True},
            ),  # fmt: skip
            ("browser_click", {"node": ref(view, f'- button "{labels["pay"]}"')}),
        ]:
            with pytest.raises(BrowserRefusal, match="Waiting"):
                adapter.call(
                    browser_task(), name, {"source_id": observation["source_id"], **arguments},
                    token(), review_action=review, input_by_request=True,
                )  # fmt: skip
        assert [options for _, options in seen] == [{"website_effects": (UNREADABLE_FRAME,)}] * 3
        assert all(
            context["website_effects"]["frame_form_readable"] is False for context, _ in seen
        )
        assert sent(browser, *INPUT) == []
        # Nothing asked such a browser for frame documents it can't give.
        assert not any(call[4].get("frames") for call in browser.calls if call[0] == "snapshot")
        # Without the request's permission the frame's site needs its own approval, as before.
        with pytest.raises(BrowserRefusal, match="frame from another website"):
            adapter.call(
                browser_task(),
                "browser_fill",
                {
                    "source_id": observation["source_id"],
                    "node": ref(view, f'- textbox "{labels["card"]}"'),
                    "text": "1",
                },  # fmt: skip
                token(),
                approved_origins=("https://shop.example",),
                review_action=review,
            )
    finally:
        adapter.close()


def test_a_dialog_on_a_page_the_request_permits_says_how_to_answer_it():
    browser = FrameBrowser(shop(WORDS["el"]))
    adapter = computers(browser)
    try:
        adapter.call(browser_task(), "browser_observe", {}, token())
        browser.dialog = ("confirm", "Να διαγραφεί η παραγγελία; 删除订单吗？")
        report = adapter.call(
            browser_task(), "browser_observe", {}, token(), input_by_request=True,
            review_action=lambda *args, **options: None,
        )  # fmt: skip
        assert report["source_kind"] == "page_dialog"
        assert "Answer it with browser_handle_dialog" in report["message"]
        assert "request this task's website approval" not in report["message"]
    finally:
        adapter.close()


# ---------------------------------------------------------------------------------------------
# The whole task: runtime, adapter, action review and approval card


@pytest.fixture
def owl(tmp_path):
    store = Store(tmp_path / "website-hands.db")
    bot = store.save_bot("owner", BotInput(name="Γλαύκα", purpose="Αγορές"))
    chat_ = store.create_chat("owner", bot["id"], "Chat")
    return store, bot, chat_


def start(owl, browser, script, words, *, features=OPERATIONS):
    store, _, chat_ = owl
    adapter = computers(browser, features)
    ask(store, chat_, words["request"])
    task = store.claim("owner")
    runtime = runtime_for(store, SimpleNamespace(adapter=adapter), script)
    return runtime, task, adapter


@pytest.mark.parametrize("language", ["el", "zh", "en"])
def test_filling_a_search_box_and_pressing_enter_needs_no_card(owl, language):
    store, bot, _ = owl
    words = WORDS[language]
    browser = FrameBrowser(shop(words))
    box = f'- searchbox "{words["search"]}"'
    script = Script(
        [
            lambda _: ("browser_observe", {}),
            tool("browser_fill", at(box, text="βραστήρας 水壶 kettle", submit=True)),
            tool("browser_key_chord", at(box, key="Enter", modifiers=["Shift"])),
            lambda _: "Searched the shop.",
        ],
        [verdict(), verdict()],
    )
    runtime, task, adapter = start(owl, browser, script, words)
    try:
        runtime.run(task, runtime.task_token(task["id"]))
    finally:
        adapter.close()
    done = store.task("owner", task["id"])
    assert done["status"] == "completed", done.get("error")
    assert sent(browser, *INPUT) == ["fill", "press_key"]
    assert runtime.proposals.list(bot["id"]) == []
    assert runtime.action_reviews.list(bot["id"]) == []
    assert len(script.reviews) == 2
    for review in script.reviews:
        assert "WEBSITE INPUT" in review[0]["content"]
        effects = json.loads(review[1]["content"])["action"]["exact_action"]["observed_context"][
            "website_effects"
        ]
        assert effects["effects"] == [] and effects["form"]["submits"] is True


@pytest.mark.parametrize("readable", [True, False], ids=["frame-read", "frame-unreadable"])
@pytest.mark.parametrize("language", ["el", "zh"])
def test_paying_in_a_payment_frame_waits_for_the_person_every_time(owl, language, readable):
    # Unreadable: another process's frame, on an owl computer that reads none.
    store, bot, _ = owl
    labels = FRAME_WORDS[language]
    browser = FrameBrowser(shop(WORDS[language]), payment_frame(labels), readable=readable)
    script = Script(
        [
            lambda _: ("browser_observe", {}),
            tool("browser_fill", at(f'- textbox "{labels["card"]}"', text="4242 4242 4242 4242")),
        ]
    )
    runtime, task, adapter = start(
        owl, browser, script, WORDS[language], features=OPERATIONS if readable else OLDER
    )
    try:
        runtime.run(task, runtime.task_token(task["id"]))
    finally:
        adapter.close()
    assert store.task("owner", task["id"])["status"] == "waiting"
    assert sent(browser, *INPUT) == [] and script.reviews == []
    (card,) = runtime.action_reviews.list(bot["id"])
    effect = "pay" if readable else UNREADABLE_FRAME
    assert card["reason"] == EFFECT_REASONS[effect]
    assert card["action"]["observed_context"]["always_ask"] is True
    assert card["action"]["observed_context"]["frame_url"] == PAY


@pytest.mark.parametrize("language", ["el", "zh"])
def test_answering_a_dialog_shows_the_reviewer_its_text(owl, language):
    store, bot, _ = owl
    words = WORDS[language]
    browser = FrameBrowser(shop(words))
    message = "Να διαγραφεί η παραγγελία; 删除订单吗？"
    browser.dialog = ("confirm", message)
    script = Script(
        [
            lambda _: ("browser_navigate", {"url": SHOP}),
            lambda report: (
                "browser_handle_dialog",
                {"source_id": report["source_id"], "accept": True},
            ),
        ],
        [verdict("ask_person", "delete", "Accepting deletes the order.")],
    )
    runtime, task, adapter = start(owl, browser, script, words)
    try:
        runtime.run(task, runtime.task_token(task["id"]))
    finally:
        adapter.close()
    assert store.task("owner", task["id"])["status"] == "waiting"
    assert sent(browser, "handle_dialog") == []
    (review,) = script.reviews
    assert "WEBSITE INPUT" in review[0]["content"] and "page dialog" in review[0]["content"]
    observed = json.loads(review[1]["content"])["action"]["exact_action"]["observed_context"]
    assert observed["dialog"] == {"type": "confirm", "message": message}
    (card,) = runtime.action_reviews.list(bot["id"])
    assert card["reason"] == "Accepting deletes the order."
    assert card["action"]["tool"] == "browser_handle_dialog"


@pytest.mark.parametrize("name", ["browser_fill", "browser_check", "browser_key_chord"])
def test_a_routine_run_refuses_a_newer_tool_on_a_site_it_did_not_declare(tmp_path, name):
    store = Store(tmp_path / "routine-hands.db")
    bot = store.save_bot("owner", BotInput(name="Ήρα", purpose="Έλεγχος τιμών"))["id"]
    item = routine(store, bot, needs={"sites": ["https://news.example"]})
    allow(store, bot, item, turn_on=True)
    task = start_run(store, listed(store, bot, item))
    labels = FRAME_WORDS["el"]
    words = WORDS["el"]
    browser = FrameBrowser(shop(words), payment_frame(labels))
    arguments = {
        "browser_fill": at(f'- searchbox "{words["search"]}"', text="βραστήρας"),
        "browser_check": at(f'- checkbox "{labels["save"]}"', checked=True),
        "browser_key_chord": at(
            f'- searchbox "{words["search"]}"', key="a", modifiers=["ControlOrMeta"]
        ),  # fmt: skip
    }[name]
    script = Script(
        [
            lambda _: ("browser_navigate", {"url": SHOP}),
            tool(name, arguments),
            lambda _: "The shop is not one of this routine's sites.",
        ]
    )
    adapter = computers(browser)
    runtime = runtime_for(store, SimpleNamespace(adapter=adapter), script)
    try:
        assert runtime._website_input_by_request(task) is False
        runtime.run(task, runtime.task_token(task["id"]))
    finally:
        adapter.close()
    refused = next(m for m in reversed(script.turns[-1]["messages"]) if m["role"] == "tool")
    assert "outside the task's approval" in refused["content"]
    assert sent(browser, *INPUT) == [] and script.reviews == []


# ---------------------------------------------------------------------------------------------
# Another site's frame read in its own process; a step's own dialog; a dialog's frame

LOGIN = "https://login.example.org/signin"
# Labels in two languages, and labels that say nothing about paying: the decision is the
# fields' autocomplete tokens and types, never these words.
CARD_WORDS = {
    "el": {"card": "Αριθμός κάρτας", "expiry": "Λήξη", "pay": "Πληρωμή"},
    "zh": {"card": "卡号", "expiry": "有效期", "pay": "支付"},
    "unrelated": {"card": "Σχόλιο 备注", "expiry": "Ψευδώνυμο 昵称", "pay": "Συνέχεια 继续"},
}
LOGIN_WORDS = {
    "el": {"user": "Όνομα χρήστη", "password": "Κωδικός", "sign_in": "Σύνδεση"},
    "zh": {"user": "用户名", "password": "密码", "sign_in": "登录"},
}


def card_frame(labels) -> Page:
    """Another site's card form: a card number, its expiry and a pay button."""
    page = Page(PAY, title="Checkout", frame=PAY_FRAME, first_backend=5000)
    form = page.add(page.body, "FORM", "form", name="", attrs={"method": "post"})
    page.add(form, "INPUT", "textbox", name=labels["card"], value="",
             attrs={"autocomplete": "cc-number", "inputmode": "numeric"}, props=FIELD)  # fmt: skip
    page.add(form, "INPUT", "textbox", name=labels["expiry"], value="",
             attrs={"autocomplete": "cc-exp"}, props=FIELD)  # fmt: skip
    pay = page.add(form, "BUTTON", "button", name=labels["pay"], props={"focusable": True})
    page.text(pay, labels["pay"])
    return page


def login_frame(labels) -> Page:
    """Another site's sign-in form: a user name, a password and a sign-in button."""
    page = Page(LOGIN, title="Login", frame=PAY_FRAME, first_backend=7000)
    form = page.add(page.body, "FORM", "form", name="", attrs={"method": "post"})
    page.add(form, "INPUT", "textbox", name=labels["user"], value="",
             attrs={"autocomplete": "username"}, props=FIELD)  # fmt: skip
    page.add(form, "INPUT", "textbox", name=labels["password"], value="",
             attrs={"type": "password", "autocomplete": "current-password"}, props=FIELD)  # fmt: skip
    button = page.add(form, "BUTTON", "button", name=labels["sign_in"], props={"focusable": True})
    page.text(button, labels["sign_in"])
    return page


def framed_snapshots(browser) -> list[dict]:
    return [call[4] for call in browser.calls if call[0] == "snapshot" and call[4].get("frames")]


def steps_in_frame(browser, steps, *, review):
    """Run ``steps`` (tool, the line its element starts with, its arguments) by the person's
    request on ``browser``'s page; returns the effects each review was given."""
    adapter = computers(browser)
    seen = []

    def reviewing(name, arguments, context, **options):
        seen.append((context, options))
        review(name, arguments, context, **options)

    try:
        observation = adapter.call(browser_task(), "browser_observe", {}, token())
        source = observation["source_id"]
        for name, line, arguments in steps:
            result = adapter.call(
                browser_task(), name,
                {"source_id": source, "node": ref(observation["page_view"], line), **arguments},
                token(), review_action=reviewing, input_by_request=True,
            )  # fmt: skip
            source = result["source_id"]
    finally:
        adapter.close()
    return seen


@pytest.mark.parametrize("language", list(CARD_WORDS))
def test_a_card_form_in_another_sites_frame_is_read_in_its_own_process(language):
    labels = CARD_WORDS[language]
    browser = FrameBrowser(shop(WORDS["el"]), card_frame(labels), readable=False)
    seen = steps_in_frame(
        browser,
        [
            ("browser_fill", f'- textbox "{labels["card"]}"', {"text": "4242 4242 4242 4242"}),
            ("browser_fill", f'- textbox "{labels["expiry"]}"', {"text": "12/29"}),
            ("browser_click", f'- button "{labels["pay"]}"', {}),
        ],
        review=lambda *args, **options: None,
    )
    # Each step is paying, read in the frame's own document (not the fail-safe for a frame
    # that can't be read), whatever its labels say.
    assert [options for _, options in seen] == [{"website_effects": ("pay",)}] * 3
    form = seen[2][0]["website_effects"]["form"]
    assert form["submits"] is True and form["payment_fields"] == 2
    assert form["submits_to_origin"] == "https://pay.example.net"
    assert all("frame_form_readable" not in context["website_effects"] for context, _ in seen)
    # Asked of the browser only for these steps in the frame, and checked again after review.
    assert len(framed_snapshots(browser)) == 6
    assert sent(browser, *INPUT) == ["fill", "fill", "click"]


@pytest.mark.parametrize("language", list(LOGIN_WORDS))
def test_a_login_form_in_another_sites_frame_is_signing_in(language):
    labels = LOGIN_WORDS[language]
    browser = FrameBrowser(shop(WORDS[language]), login_frame(labels), readable=False)
    seen = steps_in_frame(
        browser,
        [
            ("browser_fill", f'- textbox "{labels["user"]}"', {"text": "ada"}),
            ("browser_fill", f'- textbox "{labels["password"]}"', {"text": "κωδικός 密码"}),
            ("browser_click", f'- button "{labels["sign_in"]}"', {}),
        ],
        review=lambda *args, **options: None,
    )
    assert [options for _, options in seen] == [{"website_effects": ("sign_in",)}] * 3
    assert seen[2][0]["website_effects"]["form"]["password_fields"] == 2
    assert all(context["frame_url"] == LOGIN for context, _ in seen)


def test_a_frame_left_out_of_the_snapshot_waits_for_the_person():
    labels = CARD_WORDS["zh"]
    browser = FrameBrowser(shop(WORDS["zh"]), card_frame(labels), readable=False)
    browser.frame_left_out = True  # over the snapshot's limits, or gone while it was read

    def review(name, arguments, context, **options):
        raise BrowserRefusal("Waiting for the person.")

    with pytest.raises(BrowserRefusal, match="Waiting"):
        steps_in_frame(
            browser,
            [("browser_fill", f'- textbox "{labels["card"]}"', {"text": "4242"})],
            review=review,
        )
    assert sent(browser, *INPUT) == []
    assert len(framed_snapshots(browser)) == 1


def test_a_frame_that_changes_during_review_gets_nothing():
    labels = CARD_WORDS["el"]
    browser = FrameBrowser(shop(WORDS["el"]), card_frame(labels), readable=False)

    def review(name, arguments, context, **options):
        # The frame's form changes while the step is reviewed (a field is added).
        browser.frame.add(browser.frame.body, "INPUT", "textbox", name="CVC",
                          attrs={"autocomplete": "cc-csc"}, props=FIELD)  # fmt: skip

    with pytest.raises(BrowserInputNotSent, match="changed during action review"):
        steps_in_frame(
            browser,
            [("browser_click", f'- button "{labels["pay"]}"', {})],
            review=review,
        )
    assert sent(browser, *INPUT) == []


def orders(words) -> Page:
    """A shop's order page with a button that deletes the order (it asks first)."""
    page = Page(SHOP, title=words["results"])
    delete = page.add(page.body, "BUTTON", "button", name=words["delete"],
                      props={"focusable": True})  # fmt: skip
    page.text(delete, words["delete"])
    return page


DELETE_WORDS = {"el": {"results": "Παραγγελίες", "delete": "Διαγραφή παραγγελίας"},
                "zh": {"results": "订单", "delete": "删除订单"}}  # fmt: skip


@pytest.mark.parametrize("language", ["el", "zh"])
def test_a_step_that_accepts_its_own_dialog_is_reviewed_with_it(language):
    words = DELETE_WORDS[language]
    browser = FrameBrowser(orders(words))
    question = {"type": "confirm", "message": "Να διαγραφεί; 删除吗？", "accepted": True,
                "url": SHOP, "frame_id": "FRAME-MAIN"}  # fmt: skip
    browser.click_dialogs = [question]
    adapter = computers(browser)
    reviewed = []
    try:
        observation = adapter.call(browser_task(), "browser_observe", {}, token())
        delete = ref(observation["page_view"], f'- button "{words["delete"]}"')
        result = adapter.call(
            browser_task(), "browser_click",
            {"source_id": observation["source_id"], "node": delete, "dialog": "accept"},
            token(), input_by_request=True,
            review_action=lambda name, arguments, context, **options: reviewed.append(
                (arguments, context)
            ),
        )  # fmt: skip
        # The reviewer, and a card the step waits on, see that it also answers yes.
        [(arguments, context)] = reviewed
        assert arguments["dialog"] == "accept" and context["step_dialog"] == "accept"
        [click] = [call for call in browser.calls if call[0] == "click"]
        assert click[4]["dialog"] == "accept" and "dialog_text" not in click[4]
        assert result["action"]["dialogs_answered"] == [question]
        # Without it, the step is the action it always was: nothing extra reviewed or sent.
        browser.click_dialogs = []
        reviewed.clear()
        adapter.call(
            browser_task(), "browser_click",
            {"source_id": result["source_id"], "node": delete}, token(), input_by_request=True,
            review_action=lambda name, arguments, context, **options: reviewed.append(
                (arguments, context)
            ),
        )  # fmt: skip
        [(arguments, context)] = reviewed
        assert "dialog" not in arguments and "step_dialog" not in context
        assert "dialog" not in [call for call in browser.calls if call[0] == "click"][-1][4]
    finally:
        adapter.close()


def test_an_older_owl_computer_refuses_a_steps_dialog_answer_before_anything_is_sent():
    words = DELETE_WORDS["el"]
    browser = FrameBrowser(orders(words))
    adapter = computers(browser, OLDER)
    reviewed = []
    try:
        observation = adapter.call(browser_task(), "browser_observe", {}, token())
        delete = ref(observation["page_view"], f'- button "{words["delete"]}"')
        with pytest.raises(BrowserRefusal, match="newer owl computer"):
            adapter.call(
                browser_task(), "browser_click",
                {"source_id": observation["source_id"], "node": delete, "dialog": "accept"},
                token(), input_by_request=True,
                review_action=lambda *args, **options: reviewed.append(args),
            )  # fmt: skip
        assert reviewed == [] and sent(browser, *INPUT) == []
        # Its tools don't offer the option; a newer computer's do.
        schemas = adapter.schemas(browser_task(), True, input_by_request=True)
        offered = {
            item["function"]["name"]: item for item in adapter.offered(browser_task(), schemas)
        }
        assert DIALOG_TOOLS <= set(offered)
        for name in DIALOG_TOOLS:
            properties = offered[name]["function"]["parameters"]["properties"]
            assert "dialog" not in properties and "dialog_text" not in properties
            assert "source_id" in properties and "node" in properties
    finally:
        adapter.close()
    newer = computers(FrameBrowser(orders(words)))
    try:
        newer.call(browser_task(), "browser_observe", {}, token())
        schemas = newer.schemas(browser_task(), True, input_by_request=True)
        for item in newer.offered(browser_task(), schemas):
            if item["function"]["name"] in DIALOG_TOOLS:
                assert "dialog" in item["function"]["parameters"]["properties"]
    finally:
        newer.close()
    assert {"browser_hover", "browser_read_value", "browser_visual_click"}.isdisjoint(DIALOG_TOOLS)
    assert DIALOG_TOOLS <= set(DEFINITIONS)


@pytest.mark.parametrize("newer", [True, False], ids=["dialog-frames", "older"])
def test_a_dialog_in_another_sites_frame_is_answered_for_that_site(newer):
    browser = FrameBrowser(shop(WORDS["zh"]), payment_frame(FRAME_WORDS["zh"]))
    message = "Πληρωμή 9,99 €; 支付 9.99 欧元？"
    # A newer owl computer names the frame that shows a dialog; an older one does not.
    assert ("dialog_frames" in OPERATIONS, "dialog_frames" in OLDER) == (True, False)
    adapter = computers(browser, OPERATIONS if newer else OLDER)
    try:
        adapter.call(browser_task(), "browser_observe", {}, token())
        # The payment frame asks; a newer computer names the frame that shows the dialog.
        browser.dialog = ("confirm", message, *((PAY, PAY_FRAME) if newer else ()))
        report = adapter.call(browser_task(), "browser_observe", {}, token())
        assert report["dialog"] == {
            "type": "confirm",
            "message": message,
            **({"frame_url": PAY} if newer else {}),
        }
        # Approved for the shop only: the payment site's question is not the shop's.
        attempt = {"source_id": report["source_id"], "accept": True}
        if newer:
            with pytest.raises(BrowserRefusal, match="frame of another website"):
                adapter.call(
                    browser_task(), "browser_handle_dialog", attempt, token(),
                    approved_origins=("https://shop.example",),
                    review_action=lambda *args, **options: None,
                )  # fmt: skip
            assert sent(browser, "handle_dialog") == []
        # By the person's request it is reviewed as the frame's site's.
        reviewed = []
        adapter.call(
            browser_task(), "browser_handle_dialog", attempt, token(), input_by_request=True,
            review_action=lambda name, arguments, context, **options: reviewed.append(
                (context, options)
            ),
        )  # fmt: skip
        [(context, options)] = reviewed
        assert context["url"] == SHOP and options == {"website_effects": ()}
        assert context.get("frame_url") == (PAY if newer else None)
        # The answer goes to that frame's dialog only.
        [answer] = [call for call in browser.calls if call[0] == "handle_dialog"]
        assert answer[4].get("frame_id") == (PAY_FRAME if newer else None)
    finally:
        adapter.close()


# ---------------------------------------------------------------------------------------------
# The pending review carries the effect that made the step wait


@pytest.mark.parametrize("language", ["el", "zh"])
def test_a_pending_review_carries_the_effect_the_page_settled(owl, language):
    store, bot, _ = owl
    labels = CARD_WORDS[language]
    browser = FrameBrowser(shop(WORDS[language]), card_frame(labels), readable=False)
    script = Script(
        [
            lambda _: ("browser_observe", {}),
            tool("browser_fill", at(f'- textbox "{labels["card"]}"', text="4242 4242 4242 4242")),
        ]
    )
    runtime, task, adapter = start(owl, browser, script, WORDS[language])
    try:
        runtime.run(task, runtime.task_token(task["id"]))
    finally:
        adapter.close()
    assert store.task("owner", task["id"])["status"] == "waiting"
    assert sent(browser, *INPUT) == [] and script.reviews == []
    (card,) = runtime.action_reviews.list(bot["id"])
    # Read in the payment site's own frame: paying, not the fail-safe for an unreadable frame.
    assert card["effect"] == "pay" and card["reason"] == EFFECT_REASONS["pay"]
    [item] = [item for item in store.needs_you("owner")["items"] if item["kind"] == "approval"]
    assert item["effect"] == "pay"


@pytest.mark.parametrize("language", ["el", "zh"])
def test_a_step_that_confirms_its_own_question_waits_with_the_reviewers_effect(owl, language):
    store, bot, _ = owl
    words = DELETE_WORDS[language]
    browser = FrameBrowser(orders(words))
    script = Script(
        [
            lambda _: ("browser_observe", {}),
            tool("browser_click", at(f'- button "{words["delete"]}"', dialog="accept")),
        ],
        [verdict("ask_person", "delete", "Clicking this and confirming deletes the order.")],
    )
    runtime, task, adapter = start(owl, browser, script, {"request": "Διάγραψε την παραγγελία."})
    try:
        runtime.run(task, runtime.task_token(task["id"]))
    finally:
        adapter.close()
    assert store.task("owner", task["id"])["status"] == "waiting"
    assert sent(browser, *INPUT) == []
    (review,) = script.reviews
    assert "step_dialog" in review[0]["content"]
    action = json.loads(review[1]["content"])["action"]["exact_action"]
    assert action["arguments"]["dialog"] == "accept"
    assert action["observed_context"]["step_dialog"] == "accept"
    (card,) = runtime.action_reviews.list(bot["id"])
    assert card["effect"] == "delete"
    assert card["action"]["arguments"]["dialog"] == "accept"
    assert card["action"]["observed_context"]["step_dialog"] == "accept"


def test_an_existing_database_gains_the_pending_reviews_effect(tmp_path):
    import sqlite3

    from alysis_code.bots.schema import SCHEMA_VERSION

    path = tmp_path / "earlier.db"
    store = Store(path)
    bot = store.save_bot("owner", BotInput(name="Ήρα", purpose="Αγορές"))
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE bot_action_reviews DROP COLUMN effect")
        db.execute("PRAGMA user_version=45")
    migrated = Store(path)
    with migrated.connect() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(bot_action_reviews)")}
        assert "effect" in columns
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 46
    assert migrated.bot("owner", bot["id"])["name"] == "Ήρα"
