"""Website input in an owl's task: the person's own request is the permission.

An interactive task reads, scrolls, searches, filters and pages on any site without a card. Each
input passes the action review: an effect the page's HTML settles (a password, payment fields,
filled contact fields, a library file) goes straight to the person; for any other step the
reviewer names its effect, and an effect that reaches other people or can't be undone waits for
the person, then runs once they approve. Routine runs keep their declared sites, and Do it cards
for a site work as before. Pages in English, Greek and Chinese decide the same way, from
structure and role, never from their words. Real store, runtime, browser adapter and reviewer;
the model and the browser are fixtures.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from test_bots_browser import Harness, token
from test_bots_browser import task as browser_task
from test_bots_browser_page_view import Page, PageBrowser, ref
from test_bots_routine_access import allow, listed, routine, start_run

from alysis_code.agent.approval_review import REVIEWER_SYSTEM_PROMPT, parse_verdict
from alysis_code.bots.action_review import ApprovalDecision
from alysis_code.bots.browser_effects import EFFECT_REASONS, structural_effects
from alysis_code.bots.browser_tools import (
    BrowserObservationExpired,
    BrowserRefusal,
    PressKey,
    SelectOption,
    Target,
    TypeText,
    Upload,
)
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.proposals import ActionDecision, ActionProposal
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Conflict, Store
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall

SHOP = "https://shop.example/"
PARTNER = "https://forms.partner.example/submit"
WORDS = {
    "en": {
        "search": "Search",
        "sort": "Sort by",
        "newest": "Newest",
        "cheapest": "Price: low to high",
        "next": "Next page",
        "results": "Results",
        "email": "Email",
        "password": "Password",
        "sign_in": "Sign in",
        "show": "Show password",
        "card": "Card number",
        "expiry": "Expiry date",
        "code": "Security code",
        "pay": "Pay",
        "name": "Full name",
        "message": "Message",
        "send": "Send",
        "request": "Find the cheapest kettle on the shop's site and tell me its price.",
    },
    "el": {
        "search": "Αναζήτηση",
        "sort": "Ταξινόμηση",
        "newest": "Νεότερα",
        "cheapest": "Τιμή: από χαμηλή σε υψηλή",
        "next": "Επόμενη σελίδα",
        "results": "Αποτελέσματα",
        "email": "Ηλεκτρονικό ταχυδρομείο",
        "password": "Κωδικός πρόσβασης",
        "sign_in": "Σύνδεση",
        "show": "Εμφάνιση κωδικού",
        "card": "Αριθμός κάρτας",
        "expiry": "Ημερομηνία λήξης",
        "code": "Κωδικός ασφαλείας",
        "pay": "Πληρωμή",
        "name": "Ονοματεπώνυμο",
        "message": "Μήνυμα",
        "send": "Αποστολή",
        "request": "Βρες τον φθηνότερο βραστήρα στο κατάστημα και πες μου την τιμή του.",
    },
    "zh": {
        "search": "搜索",
        "sort": "排序",
        "newest": "最新",
        "cheapest": "价格从低到高",
        "next": "下一页",
        "results": "结果",
        "email": "电子邮件",
        "password": "密码",
        "sign_in": "登录",
        "show": "显示密码",
        "card": "卡号",
        "expiry": "有效期",
        "code": "安全码",
        "pay": "支付",
        "name": "姓名",
        "message": "留言",
        "send": "发送",
        "request": "在商店网站上找到最便宜的水壶，告诉我价格。",
    },
}
LANGUAGES = list(WORDS)
FIELD = {"editable": "plaintext", "focusable": True}


# ---------------------------------------------------------------------------------------------
# Pages


def catalogue(words) -> Page:
    """A shop's results: a search form, a sort dropdown, a list and a next-page link."""
    page = Page(SHOP, title=words["results"])
    main = page.add(page.body, "MAIN", "main", name="")
    search = page.add(main, "FORM", "search", name="",
                      attrs={"role": "search", "action": "/search", "method": "get"})  # fmt: skip
    page.add(search, "INPUT", "searchbox", name=words["search"], value="",
             attrs={"type": "search", "name": "q"}, props=FIELD)  # fmt: skip
    page.text(page.add(search, "BUTTON", "button", name=words["search"]), words["search"])
    sort = page.add(main, "SELECT", "combobox", name=words["sort"], ax_value=words["newest"],
                    props={"expanded": False, "focusable": True})  # fmt: skip
    popup = (sort[0], page._ax(None, sort[1], "MenuListPopup"))
    for option, chosen in ((words["newest"], True), (words["cheapest"], False)):
        item = page.add(popup, "OPTION", "option", name=option, selected=chosen,
                        props={"selected": chosen, "focusable": True}, rendered=False)  # fmt: skip
        page.text(item, option, rendered=False)
    listing = page.add(main, "UL", "list", name="")
    for number in range(1, 4):
        page.text(page.add(listing, "LI", "listitem", name=""), f"{words['results']} {number}")
    address = SHOP + "page/2"
    link = page.add(main, "A", "link", name=words["next"], attrs={"href": address},
                    props={"url": address, "focusable": True})  # fmt: skip
    page.text(link, words["next"])
    return page


def sign_in(words) -> tuple[Page, dict]:
    page = Page(SHOP + "account", title=words["sign_in"])
    form = page.add(page.body, "FORM", "form", name="",
                    attrs={"id": "account", "method": "post", "action": "/session"})  # fmt: skip
    email = page.add(form, "INPUT", "textbox", name=words["email"], value="anna@example.org",
                     attrs={"type": "email"}, props=FIELD)  # fmt: skip
    password = page.add(form, "INPUT", "textbox", name=words["password"], value="hunter2",
                        ax_value="•" * 7, attrs={"type": "password"}, props=FIELD)  # fmt: skip
    show = page.add(form, "BUTTON", "button", name=words["show"], attrs={"type": "button"})
    page.text(show, words["show"])
    submit = page.add(form, "BUTTON", "button", name=words["sign_in"])
    page.text(submit, words["sign_in"])
    # A second submit control outside the form, tied to it by its form attribute.
    outside = page.add(page.body, "BUTTON", "button", name=words["sign_in"] + " ",
                       attrs={"form": "account", "type": "submit"})  # fmt: skip
    page.text(outside, words["sign_in"])
    nodes = {"email": email, "password": password, "show": show, "submit": submit}
    return page, {**nodes, "outside": outside}


def payment(words) -> tuple[Page, dict]:
    page = Page(SHOP + "checkout", title=words["pay"])
    form = page.add(page.body, "FORM", "form", name="", attrs={"method": "post"})
    number = page.add(form, "INPUT", "textbox", name=words["card"], value="",
                      attrs={"autocomplete": "cc-number"}, props=FIELD)  # fmt: skip
    page.add(form, "INPUT", "textbox", name=words["expiry"], value="",
             attrs={"autocomplete": "billing cc-exp"}, props=FIELD)  # fmt: skip
    # A security code is often a password-type field: its token says it is payment.
    page.add(form, "INPUT", "textbox", name=words["code"], value="",
             attrs={"type": "password", "autocomplete": "cc-csc"}, props=FIELD)  # fmt: skip
    submit = page.add(form, "INPUT", "button", name=words["pay"],
                      attrs={"type": "submit", "value": words["pay"]})  # fmt: skip
    return page, {"number": number, "submit": submit}


def contact(words) -> tuple[Page, dict]:
    """A contact form whose fields go to another site."""
    page = Page(SHOP + "contact", title=words["message"])
    form = page.add(page.body, "FORM", "form", name="",
                    attrs={"method": "post", "action": PARTNER})  # fmt: skip
    page.add(form, "INPUT", "textbox", name=words["name"], value="Άννα 李",
             attrs={"autocomplete": "name"}, props=FIELD)  # fmt: skip
    page.add(form, "TEXTAREA", "textbox", name=words["message"], value="…", props=FIELD)
    submit = page.add(form, "INPUT", "button", name=words["send"],
                      attrs={"type": "submit", "value": words["send"]})  # fmt: skip
    return page, {"submit": submit}


def feedback(words) -> tuple[Page, dict]:
    """A message form that submits to another site, with no field the HTML marks as personal."""
    page = Page(SHOP + "feedback", title=words["message"])
    form = page.add(page.body, "FORM", "form", name="",
                    attrs={"method": "post", "action": PARTNER})  # fmt: skip
    page.add(form, "TEXTAREA", "textbox", name=words["message"], value="…", props=FIELD)
    submit = page.add(form, "BUTTON", "button", name=words["send"])
    page.text(submit, words["send"])
    return page, {"submit": submit}


def chat(words) -> tuple[Page, dict]:
    """A chat composer and its send button: no form, so only the reviewer can tell."""
    page = Page(SHOP + "messages", title=words["message"])
    composer = page.add(page.body, "TEXTAREA", "textbox", name=words["message"], value="Hi",
                        props=FIELD)  # fmt: skip
    send = page.add(page.body, "BUTTON", "button", name=words["send"], attrs={"type": "button"})
    page.text(send, words["send"])
    return page, {"composer": composer, "send": send}


class ShopBrowser(PageBrowser):
    """The lab browser for these pages, answering every kind of input."""

    def press_key(self, owner, session_id, selector, key, **kwargs):
        self._call("key", owner, session_id, selector, key, **kwargs)
        self._acted()

    def scroll(self, owner, session_id, selector, **kwargs):
        self._call("scroll", owner, session_id, selector, **kwargs)
        self._acted()


def serve(lab: Harness, page: Page) -> ShopBrowser:
    if lab.executors:
        lab.executors[0].browser.show(page)
        return lab.executors[0].browser
    browser = ShopBrowser(page)
    lab.factory_hook = lambda executor, cancel: setattr(executor, "browser", browser)
    return browser


INPUT = frozenset({"click", "type", "key", "select", "scroll"})


def sent(browser) -> list[str]:
    return [call[0] for call in browser.calls if call[0] in INPUT]


@pytest.fixture
def lab():
    harness = Harness()
    # A real site, not an executor-owned preview.
    harness.adapter.interactive_previews = False
    yield harness
    harness.adapter.close()


# ---------------------------------------------------------------------------------------------
# What the page's structure settles


def data_of(page: Page) -> dict:
    return page.build()[0]


@pytest.mark.parametrize("language", LANGUAGES)
def test_structure_settles_sign_in_payment_personal_data_and_upload_in_every_language(language):
    words = WORDS[language]
    page, nodes = sign_in(words)
    data = data_of(page)

    def effects(name, node, args):
        return structural_effects(data, page.backend_of(node[0]), name, args)

    click = Target(source_id="s", node=0)
    submitted = effects("browser_click", nodes["submit"], click)
    assert submitted["effects"] == ["sign_in", "personal_data"]
    assert submitted["form"] == {
        "submits": True,
        "method": "post",
        "submits_to_origin": "https://shop.example",
        "other_origin": False,
        "password_fields": 1,
        "payment_fields": 0,
        "filled_personal_fields": 1,
    }
    # Enter in a single-line field submits its form; the form attribute ties a far button to it.
    enter = PressKey(source_id="s", node=0, key="Enter")
    assert effects("browser_key", nodes["email"], enter)["effects"] == ["sign_in", "personal_data"]
    assert effects("browser_click", nodes["outside"], click)["effects"] == [
        "sign_in",
        "personal_data",
    ]
    # Typing a password is signing in; a type=button control and typing an email send nothing.
    typed = TypeText(source_id="s", node=0, text="x")
    assert effects("browser_type", nodes["password"], typed)["effects"] == ["sign_in"]
    assert effects("browser_type", nodes["email"], typed)["effects"] == []
    shown = effects("browser_click", nodes["show"], click)
    assert shown["effects"] == [] and shown["form"]["submits"] is False

    page, nodes = payment(words)
    data = data_of(page)
    assert effects("browser_click", nodes["submit"], click)["effects"] == ["pay"]
    assert effects("browser_type", nodes["number"], typed)["effects"] == ["pay"]

    page, nodes = contact(words)
    data = data_of(page)
    sent_away = effects("browser_click", nodes["submit"], click)
    assert sent_away["effects"] == ["personal_data"]
    assert sent_away["form"]["other_origin"] is True
    assert sent_away["form"]["submits_to_origin"] == "https://forms.partner.example"

    upload = Upload(source_id="s", node=0, file_id="a" * 32)
    assert effects("browser_upload", nodes["submit"], upload)["effects"] == ["upload"]


@pytest.mark.parametrize("language", LANGUAGES)
def test_searching_filtering_paging_and_a_formless_send_button_settle_nothing(language):
    words = WORDS[language]
    page = catalogue(words)
    data = data_of(page)
    view_nodes = {
        page.strings[page.nodes["nodeName"][i]]: i for i in range(len(page.nodes["nodeName"]))
    }
    box = view_nodes["INPUT"]
    searched = structural_effects(
        data, page.backend_of(box), "browser_key", PressKey(source_id="s", node=0, key="Enter")
    )
    # A search submits its form, but nothing in it needs the person.
    assert searched["effects"] == [] and searched["form"]["submits"] is True
    assert searched["form"]["method"] == "get" and searched["form"]["other_origin"] is False
    chosen = structural_effects(
        data,
        page.backend_of(view_nodes["SELECT"]),
        "browser_select",
        SelectOption(source_id="s", node=0, option_node=1),
    )
    assert chosen == {"effects": [], "form": None}
    paged = structural_effects(
        data, page.backend_of(view_nodes["A"]), "browser_click", Target(source_id="s", node=0)
    )
    assert paged == {"effects": [], "form": None}
    page, nodes = chat(words)
    pressed = structural_effects(
        data_of(page),
        page.backend_of(nodes["send"][0]),
        "browser_click",
        Target(source_id="s", node=0),
    )
    assert pressed == {"effects": [], "form": None}


# ---------------------------------------------------------------------------------------------
# The browser adapter's gate


def test_the_persons_request_admits_input_on_any_site_and_every_input_is_reviewed(lab):
    words = WORDS["el"]
    browser = serve(lab, catalogue(words))
    observation = lab.call()
    view, source = observation["page_view"], observation["source_id"]
    reviewed = []

    def review(name, arguments, context, **options):
        reviewed.append((name, context, options))

    # Without the request's permission, no card and no site: input is neither offered nor sent.
    offered = {s["function"]["name"] for s in lab.adapter.schemas(browser_task(), True)}
    assert "browser_click" not in offered and "browser_scroll" in offered
    box = ref(view, f'- searchbox "{words["search"]}"')
    with pytest.raises(BrowserObservationExpired, match="website approval"):
        lab.adapter.call(
            browser_task(),
            "browser_type",
            {"source_id": source, "node": box, "text": "βραστήρας"},
            token(),
            review_action=review,
        )
    # With it, every acting tool is offered, and no reviewer means no permission.
    offered = {
        s["function"]["name"]
        for s in lab.adapter.schemas(browser_task(), True, input_by_request=True)
    }
    assert {"browser_click", "browser_type", "browser_select", "browser_key"} <= offered
    with pytest.raises(BrowserObservationExpired, match="website approval"):
        lab.adapter.call(
            browser_task(),
            "browser_type",
            {"source_id": source, "node": box, "text": "βραστήρας"},
            token(),
            input_by_request=True,
        )
    assert sent(browser) == []

    def act(name, arguments):
        nonlocal view, source
        result = lab.adapter.call(
            browser_task(),
            name,
            {"source_id": source, **arguments},
            token(),
            review_action=review,
            input_by_request=True,
        )
        view, source = result["page_view"], result["source_id"]
        return result

    act("browser_scroll", {"node": ref(view, "- list"), "delta_y": 600})
    act("browser_type", {"node": box, "text": "βραστήρας"})
    act("browser_key", {"node": box, "key": "Enter"})
    act(
        "browser_select",
        {
            "node": ref(view, f'- combobox "{words["sort"]}"'),
            "option_node": ref(view, f'- option "{words["cheapest"]}"'),
        },
    )
    act("browser_click", {"node": ref(view, f'- link "{words["next"]}"')})
    assert sent(browser) == ["scroll", "type", "key", "select", "click"]
    # Scrolling is reading: never reviewed. Each input is, as website input the request permits.
    assert [name for name, _, _ in reviewed] == [
        "browser_type",
        "browser_key",
        "browser_select",
        "browser_click",
    ]
    assert all(options == {"website_effects": ()} for _, _, options in reviewed)
    assert all(
        context["url"] == SHOP and "website_effects" in context for _, context, _ in reviewed
    )
    assert reviewed[1][1]["website_effects"]["form"]["submits"] is True


@pytest.mark.parametrize("language", LANGUAGES)
def test_a_settled_effect_reaches_the_reviewer_hook_with_its_effects(lab, language):
    words = WORDS[language]
    page, _ = sign_in(words)
    browser = serve(lab, page)
    observation = lab.call()
    seen = []

    def review(name, arguments, context, **options):
        seen.append((context, options))
        raise BrowserRefusal("Waiting for the person.")

    with pytest.raises(BrowserRefusal, match="Waiting"):
        lab.adapter.call(
            browser_task(),
            "browser_click",
            {
                "source_id": observation["source_id"],
                "node": ref(observation["page_view"], f'- button "{words["sign_in"]}"'),
            },
            token(),
            review_action=review,
            input_by_request=True,
        )
    ((context, options),) = seen
    assert options == {"website_effects": ("sign_in", "personal_data")}
    assert context["website_effects"]["effects"] == ["sign_in", "personal_data"]
    assert context["label"] == words["sign_in"]
    assert sent(browser) == []


def test_a_card_or_routine_site_keeps_todays_review_without_effects(lab):
    page = catalogue(WORDS["en"])
    browser = serve(lab, page)
    observation = lab.call()
    seen = []

    def review(name, arguments, context):  # today's three-argument hook
        seen.append(context)

    lab.adapter.call(
        browser_task(),
        "browser_click",
        {"source_id": observation["source_id"], "node": ref(observation["page_view"], "- link")},
        token(),
        approved_origins=("https://shop.example",),
        review_action=review,
        input_by_request=True,
    )
    assert sent(browser) == ["click"] and "website_effects" not in seen[0]


# ---------------------------------------------------------------------------------------------
# The whole task: runtime, adapter, action review and approval card


REVIEW_START = REVIEWER_SYSTEM_PROMPT[:60]


class Script:
    """The owl's model and its action reviewer (which uses the turn's own client)."""

    model = "website-access-fixture"
    supports_tool_calling = True

    def __init__(self, steps, verdicts=()):
        self.steps, self.verdicts = list(steps), list(verdicts)
        self.turns, self.reviews = [], []

    def chat(self, **kwargs):
        messages = copy.deepcopy(kwargs["messages"])
        if str(messages[0].get("content", "")).startswith(REVIEW_START):
            self.reviews.append(messages)
            verdict = self.verdicts.pop(0)
            if isinstance(verdict, Exception):
                raise verdict
            return LLMResponse(json.dumps(verdict), [], {}, usage=LLMUsage(10, 5, 15))
        self.turns.append({"messages": messages, "tools": kwargs.get("tools")})
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        step = self.steps.pop(0)
        made = step(results[-1] if results else None)
        if isinstance(made, str):
            return LLMResponse(made, [], {}, usage=LLMUsage(10, 5, 15))
        name, arguments = made
        call = ToolCall(f"call-{len(self.turns)}", name, arguments)
        return LLMResponse("", [call], {}, usage=LLMUsage(10, 5, 15))


def verdict(decision="allow", effect="none", reason="Fixture assessment.", **extra):
    return {
        "decision": decision,
        "risk": "low",
        "user_authorization": "high",
        "effect": effect,
        "reason": reason,
        **extra,
    }


def at(control, **extra):
    """A step that acts on the line of the latest page view starting with ``control``."""

    def step(result):
        node = ref(result["page_view"], control)
        return {"source_id": result["source_id"], "node": node, **extra}

    return step


def tool(name, make):
    return lambda result: (name, make(result))


@pytest.fixture
def owl(tmp_path):
    store = Store(tmp_path / "website-access.db")
    bot = store.save_bot("owner", BotInput(name="Γλαύκα", purpose="Αγορές"))
    chat_ = store.create_chat("owner", bot["id"], "Chat")
    harness = Harness()
    harness.adapter.interactive_previews = False
    yield store, bot, chat_, harness
    harness.adapter.close()


def runtime_for(store, harness, script, *, reviewed=True):
    runtime = BotRuntime(store, "owner", lambda _: script, browser_computers=harness.adapter)
    runtime.limits = replace(runtime.limits, compaction_enabled=False)
    runtime.action_review_enabled = reviewed
    return runtime


def ask(store, chat_, message):
    return store.enqueue("owner", chat_["id"], TaskInput(message=message, request_id=uuid4().hex))


@pytest.mark.parametrize("language", LANGUAGES)
def test_an_interactive_task_scrolls_searches_filters_and_pages_with_no_card(owl, language):
    store, bot, chat_, harness = owl
    words = WORDS[language]
    browser = serve(harness, catalogue(words))
    ask(store, chat_, words["request"])
    task = store.claim("owner")
    box = f'- searchbox "{words["search"]}"'
    script = Script(
        [
            lambda _: ("browser_navigate", {"url": SHOP}),
            tool("browser_scroll", at("- list", delta_y=600)),
            tool("browser_type", at(box, text="βραστήρας 水壶")),
            tool("browser_key", at(box, key="Enter")),
            tool(
                "browser_select",
                lambda r: {
                    **at(f'- combobox "{words["sort"]}"')(r),
                    "option_node": ref(r["page_view"], f'- option "{words["cheapest"]}"'),
                },
            ),
            tool("browser_click", at(f'- link "{words["next"]}"')),
            lambda _: "The cheapest kettle costs 19.90.",
        ],
        [verdict() for _ in range(4)],
    )
    runtime = runtime_for(store, harness, script)
    runtime.run(task, runtime.task_token(task["id"]))

    done = store.task("owner", task["id"])
    assert done["status"] == "completed", done.get("error")
    assert sent(browser) == ["scroll", "type", "key", "select", "click"]
    first_tools = {schema["function"]["name"] for schema in script.turns[0]["tools"]}
    assert {"browser_click", "browser_type", "browser_select", "browser_key"} <= first_tools
    # No card of any kind, and nothing waits for the person.
    assert runtime.proposals.list(bot["id"]) == []
    assert runtime.action_reviews.list(bot["id"]) == []
    # Four inputs, four reviews under the website policy; scrolling needed none.
    assert len(script.reviews) == 4
    assert all("WEBSITE INPUT" in review[0]["content"] for review in script.reviews)
    assert all('"website_effects"' in review[1]["content"] for review in script.reviews)
    state = next(m["content"] for m in script.turns[0]["messages"] if "Task start UTC" in str(m))
    assert "The person's request allows website input on any site: True" in state


@pytest.mark.parametrize("language", LANGUAGES)
def test_signing_in_waits_for_the_person_then_runs_once_they_approve(owl, language):
    store, bot, chat_, harness = owl
    words = WORDS[language]
    page, _ = sign_in(words)
    browser = serve(harness, page)
    ask(store, chat_, words["request"])
    task = store.claim("owner")
    submit = f'- button "{words["sign_in"]}"'
    script = Script(
        [
            lambda _: ("browser_navigate", {"url": SHOP + "account"}),
            tool("browser_click", at(submit)),
        ]
    )
    runtime = runtime_for(store, harness, script)
    runtime.run(task, runtime.task_token(task["id"]))

    assert store.task("owner", task["id"])["status"] == "waiting"
    assert sent(browser) == []
    assert script.reviews == []  # settled by the page's structure: no model judgement
    (card,) = runtime.action_reviews.list(bot["id"])
    assert card["reason"] == EFFECT_REASONS["sign_in"]
    effects = card["action"]["observed_context"]["website_effects"]
    assert effects["effects"] == ["sign_in", "personal_data"]
    assert runtime.proposals.list(bot["id"]) == []  # an action approval, not a site card
    # Like a plugin change that asks, it is approved every time: never "Always allow".
    assert card["action"]["observed_context"]["always_ask"] is True
    with pytest.raises(Conflict, match="every time"):
        runtime.action_reviews.decide(
            bot["id"], card["id"], ApprovalDecision(action="always_allow", expected_revision=1)
        )

    runtime.action_reviews.decide(
        bot["id"], card["id"], ApprovalDecision(action="allow", expected_revision=1)
    )
    resumed = runtime.background.claim()
    script.steps = [
        lambda _: ("browser_observe", {}),
        tool("browser_click", at(submit)),
        lambda _: "Signed in.",
    ]
    runtime.run(resumed, runtime.task_token(resumed["id"]), background=True)
    finished = store.task("owner", task["id"])
    assert finished["status"] == "completed", finished.get("error")
    assert sent(browser) == ["click"]
    assert [r["status"] for r in runtime.action_reviews.list(bot["id"], include_history=True)] == [
        "consumed"
    ]


@pytest.mark.parametrize(
    "language,page,control,settled",
    [
        ("en", payment, "pay", "pay"),
        ("el", payment, "pay", "pay"),
        ("zh", contact, "send", "personal_data"),
        ("el", contact, "send", "personal_data"),
    ],
)
def test_paying_and_sending_personal_details_wait_for_the_person(
    owl, language, page, control, settled
):
    store, bot, chat_, harness = owl
    words = WORDS[language]
    built, _ = page(words)
    browser = serve(harness, built)
    ask(store, chat_, words["request"])
    task = store.claim("owner")
    script = Script(
        [
            lambda _: ("browser_observe", {}),
            tool("browser_click", at(f'- button "{words[control]}"')),
        ]
    )
    runtime = runtime_for(store, harness, script)
    runtime.run(task, runtime.task_token(task["id"]))
    assert store.task("owner", task["id"])["status"] == "waiting"
    assert sent(browser) == [] and script.reviews == []
    (card,) = runtime.action_reviews.list(bot["id"])
    assert card["reason"] == EFFECT_REASONS[settled]


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("effect", ["send", "post", "book", "delete"])
def test_the_reviewer_names_an_effect_the_page_structure_cannot_show(owl, language, effect):
    store, bot, chat_, harness = owl
    words = WORDS[language]
    page, _ = chat(words)
    browser = serve(harness, page)
    ask(store, chat_, words["request"])
    task = store.claim("owner")
    reason = "This button delivers the drafted text to other people."
    script = Script(
        [
            lambda _: ("browser_observe", {}),
            tool("browser_click", at(f'- button "{words["send"]}"')),
        ],
        # Even an "allow" with a named effect waits for the person.
        [verdict("allow", effect, reason)],
    )
    runtime = runtime_for(store, harness, script)
    runtime.run(task, runtime.task_token(task["id"]))
    assert store.task("owner", task["id"])["status"] == "waiting"
    assert sent(browser) == []
    (card,) = runtime.action_reviews.list(bot["id"])
    assert card["reason"] == reason
    assert card["action"]["observed_context"]["label"] == words["send"]
    (review,) = script.reviews
    assert "WEBSITE INPUT" in review[0]["content"]
    payload = json.loads(review[1]["content"])
    assert payload["action"]["exact_action"]["observed_context"]["website_effects"] == {
        "effects": [],
        "form": None,
    }


@pytest.mark.parametrize("language", LANGUAGES)
def test_a_form_sent_to_another_site_reaches_the_reviewer_with_that_fact(owl, language):
    store, bot, chat_, harness = owl
    words = WORDS[language]
    page, _ = feedback(words)
    browser = serve(harness, page)
    ask(store, chat_, words["request"])
    task = store.claim("owner")
    script = Script(
        [
            lambda _: ("browser_observe", {}),
            tool("browser_click", at(f'- button "{words["send"]}"')),
        ],
        [verdict("ask_person", "send", "The form sends the message to another site.")],
    )
    runtime = runtime_for(store, harness, script)
    runtime.run(task, runtime.task_token(task["id"]))
    assert store.task("owner", task["id"])["status"] == "waiting"
    assert sent(browser) == []
    (review,) = script.reviews
    observed = json.loads(review[1]["content"])["action"]["exact_action"]["observed_context"]
    assert observed["website_effects"]["effects"] == []
    assert observed["website_effects"]["form"]["other_origin"] is True
    assert observed["website_effects"]["form"]["submits_to_origin"] == (
        "https://forms.partner.example"
    )
    (card,) = runtime.action_reviews.list(bot["id"])
    assert card["reason"] == "The form sends the message to another site."


@pytest.mark.parametrize(
    "answer",
    [
        RuntimeError("provider unavailable"),
        {"decision": "allow", "risk": "low", "user_authorization": "high", "reason": "No effect."},
        verdict("allow", "transfer"),
    ],
    ids=["reviewer-fails", "effect-missing", "effect-unknown"],
)
def test_when_the_reviewer_cannot_decide_the_person_is_asked(owl, answer):
    store, bot, chat_, harness = owl
    words = WORDS["zh"]
    browser = serve(harness, catalogue(words))
    ask(store, chat_, words["request"])
    task = store.claim("owner")
    script = Script(
        [
            lambda _: ("browser_observe", {}),
            tool("browser_click", at(f'- link "{words["next"]}"')),
        ],
        [answer],
    )
    runtime = runtime_for(store, harness, script)
    runtime.run(task, runtime.task_token(task["id"]))
    assert store.task("owner", task["id"])["status"] == "waiting"
    assert sent(browser) == []
    assert len(runtime.action_reviews.list(bot["id"])) == 1


def test_without_the_action_reviewer_the_request_permits_no_input(owl):
    store, bot, chat_, harness = owl
    words = WORDS["en"]
    browser = serve(harness, catalogue(words))
    ask(store, chat_, words["request"])
    task = store.claim("owner")
    script = Script([lambda _: ("browser_observe", {}), lambda _: "I can only read this site."])
    runtime = runtime_for(store, harness, script, reviewed=False)
    runtime.run(task, runtime.task_token(task["id"]))
    assert store.task("owner", task["id"])["status"] == "completed"
    tools = {schema["function"]["name"] for schema in script.turns[0]["tools"]}
    assert "browser_click" not in tools and "browser_scroll" in tools
    assert sent(browser) == []


def test_a_site_card_for_the_request_is_not_created_again(owl):
    store, bot, chat_, harness = owl
    words = WORDS["el"]
    serve(harness, catalogue(words))
    ask(store, chat_, words["request"])
    task = store.claim("owner")
    script = Script(
        [
            lambda _: (
                "propose_action",
                {
                    "question": "Να συνεχίσω στο κατάστημα;",
                    "instructions": "Βρες τον φθηνότερο βραστήρα.",
                    "offline_computer": False,
                    "browser_origin": SHOP.rstrip("/"),
                },
            ),
            lambda _: "Continuing on the site.",
        ]
    )
    runtime = runtime_for(store, harness, script)
    runtime.run(task, runtime.task_token(task["id"]))
    result = json.loads(
        next(m for m in reversed(script.turns[-1]["messages"]) if m["role"] == "tool")["content"]
    )
    assert result["state"] == "already_approved"
    assert runtime.proposals.list(bot["id"]) == []


def test_a_routine_run_still_needs_its_declared_sites(tmp_path):
    store = Store(tmp_path / "routine.db")
    bot = store.save_bot("owner", BotInput(name="Ήρα", purpose="Έλεγχος τιμών"))["id"]
    item = routine(store, bot, needs={"sites": ["https://news.example"]})
    allow(store, bot, item, turn_on=True)
    task = start_run(store, listed(store, bot, item))
    harness = Harness()
    harness.adapter.interactive_previews = False
    words = WORDS["el"]
    browser = serve(harness, catalogue(words))
    script = Script(
        [
            lambda _: ("browser_navigate", {"url": SHOP}),
            tool("browser_click", at(f'- link "{words["next"]}"')),
            lambda _: "The shop is not one of this routine's sites.",
        ]
    )
    runtime = runtime_for(store, harness, script)
    try:
        assert runtime._website_input_by_request(task) is False
        runtime.run(task, runtime.task_token(task["id"]))
    finally:
        harness.adapter.close()
    refused = json.loads(
        next(m for m in reversed(script.turns[-1]["messages"]) if m["role"] == "tool")["content"]
    )
    assert "outside the task's approval" in json.dumps(refused)
    assert sent(browser) == [] and script.reviews == []
    state = next(m["content"] for m in script.turns[0]["messages"] if "Task start UTC" in str(m))
    assert "The person's request allows website input on any site: False" in state


def test_a_do_it_card_for_a_site_still_grants_it_with_todays_review(owl):
    store, bot, chat_, harness = owl
    words = WORDS["en"]
    browser = serve(harness, catalogue(words))
    source_task = ask(store, chat_, "Prepare the order")
    source_task = store.claim("owner")
    runtime = runtime_for(store, harness, Script([]))
    proposal = runtime.proposals.propose(
        source_task,
        ActionProposal(
            question="Use the shop?", instructions="Open the next page.", browser_origin=SHOP
        ),
    )
    store.finish("owner", source_task["id"], "completed", content="Approve the shop.")
    accepted = runtime.proposals.decide(
        bot["id"],
        proposal["id"],
        ActionDecision(action="accept", expected_revision=1, browser_origin=SHOP.rstrip("/")),
    )
    task = store.claim("owner")
    assert task["id"] == accepted["task_id"]
    # Today's reviewer answer, without an effect, is honoured on the card's own site.
    script = Script(
        [
            lambda _: ("browser_observe", {}),
            tool("browser_click", at(f'- link "{words["next"]}"')),
            lambda _: "Opened the next page.",
        ],
        [{key: value for key, value in verdict().items() if key != "effect"}],
    )
    runtime.client_factory = lambda _: script
    runtime.run(task, runtime.task_token(task["id"]))
    assert store.task("owner", task["id"])["status"] == "completed"
    assert sent(browser) == ["click"]
    (review,) = script.reviews
    assert "WEBSITE INPUT" not in review[0]["content"]


@pytest.mark.parametrize("image_input", [False, True])
def test_models_with_and_without_images_get_the_same_permission(lab, image_input):
    """The reviewer is the turn's own model: a text-only model gets DOM input, an image model
    also coordinate clicks, and both pass the same review."""
    from test_bots_browser import install_visual_fixture

    names = {
        s["function"]["name"]
        for s in lab.adapter.schemas(
            browser_task(), True, image_input=image_input, input_by_request=True
        )
    }
    assert {"browser_click", "browser_type", "browser_key"} <= names
    assert ("browser_visual_click" in names) is image_input
    if not image_input:
        with pytest.raises(ValueError, match="image-capable"):
            lab.adapter.call(
                browser_task(),
                "browser_visual_click",
                {"source_id": "s", "x": 1, "y": 1},
                token(),
                input_by_request=True,
                review_action=lambda *args, **options: None,
            )
        return
    install_visual_fixture(lab)
    frame = lab.adapter.call(browser_task(), "browser_screenshot", {}, token(), image_input=True)
    seen = []
    shot = lab.adapter.call(
        browser_task(),
        "browser_visual_click",
        {"source_id": frame.observation["source_id"], "x": 1, "y": 1},
        token(),
        image_input=True,
        review_action=lambda name, arguments, context, **options: seen.append((context, options)),
        input_by_request=True,
    )
    assert shot.observation["action"]["dispatched"]
    ((context, options),) = seen
    assert options == {"website_effects": ()} and context["screenshot"].startswith("data:image")


# ---------------------------------------------------------------------------------------------
# The reviewer's answer


def test_an_allowed_effect_becomes_the_persons_decision_and_unrequested_answers_are_unchanged():
    effects = ("pay", "sign_in", "send")
    allowed = parse_verdict(json.dumps(verdict("allow", "send")), effects=effects)
    assert allowed.outcome == "ask_person" and allowed.effect == "send"
    assert parse_verdict(json.dumps(verdict("allow", "none")), effects=effects).outcome == "allow"
    assert parse_verdict(json.dumps(verdict("deny", "pay")), effects=effects).outcome == "deny"
    missing = {k: v for k, v in verdict().items() if k != "effect"}
    assert parse_verdict(json.dumps(missing), effects=effects).failure == "invalid_response"
    # A review that was not asked for an effect reads as before.
    assert parse_verdict(json.dumps(missing)).outcome == "allow"
    assert parse_verdict(json.dumps(verdict("allow", "send"))).outcome == "allow"
