"""Website steps whose page's own HTML proves nothing leaves the page need no reviewer.

Typing into a search field, filling, choosing in and sending a form that submits with GET to the
page's own origin (a search, filters, paging), and following a plain link run without a model
review: HTTP defines GET as safe, and browser_navigate already opens any address unreviewed. The
step records an action_review event that the page's structure settled it. Everything the HTML
can't prove (a POST form, GET to another origin, a rich-text editor, a password field, a link
that runs script, an inline handler or a script framework's attribute) is reviewed as before,
and the person's "ask first" rules and their choice to review every step still win.

Decided from HTML structure only, the same for pages in Greek, Chinese and English and for
models that declare different capabilities. Real store, runtime, browser adapter and reviewer;
the model and the browser are fixtures (test_bots_website_access).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from test_bots_browser import Harness
from test_bots_browser_page_view import Page, ref
from test_bots_website_access import (
    FIELD,
    SHOP,
    WORDS,
    Script,
    at,
    runtime_for,
    sent,
    serve,
    tool,
    verdict,
)

from alysis_code.bots.action_review import ReviewSettings, RuleInput
from alysis_code.bots.browser_effects import stays_on_page, structural_effects
from alysis_code.bots.browser_tools import DEFINITIONS
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.store import Store

OTHER_SITE = "https://search.partner.example/find"
MORE = {
    "en": {"stock": "In stock", "subject": "Subject", "user": "Member number", "notes": "Notes"},
    "el": {
        "stock": "Διαθέσιμα",
        "subject": "Θέμα",
        "user": "Αριθμός μέλους",
        "notes": "Σημειώσεις",
    },
    "zh": {"stock": "有货", "subject": "主题", "user": "会员编号", "notes": "备注"},
}


def words_for(language: str) -> dict:
    return {**WORDS[language], **MORE[language]}


# ---------------------------------------------------------------------------------------------
# Pages


def finder(
    words, *, form=None, submit=None, link=None, field=None, base=None, around=None
) -> tuple[Page, dict]:
    """A shop's search and filter form and a link to the next page. By default the form submits
    with GET to the shop's own site; ``form``, ``submit``, ``link``, ``field`` and ``around`` add
    attributes to the form, its button, the link, the search field and the main element around
    them all, ``base`` adds a base element."""
    page = Page(SHOP, title=words["results"])
    if base is not None:
        page.add(page.body, "BASE", attrs=base)
    main = page.add(page.body, "MAIN", "main", name="", attrs=around)
    search = page.add(main, "FORM", "search", name="",
                      attrs={"role": "search", "action": "/search", "method": "get",
                             **(form or {})})  # fmt: skip
    box = page.add(search, "INPUT", "searchbox", name=words["search"], value="",
                   attrs={"type": "search", "name": "q", **(field or {})}, props=FIELD)  # fmt: skip
    stock = page.add(search, "INPUT", "checkbox", name=words["stock"],
                     attrs={"type": "checkbox", "name": "stock"},
                     props={"checked": "false", "focusable": True})  # fmt: skip
    sort = page.add(search, "SELECT", "combobox", name=words["sort"], ax_value=words["newest"],
                    attrs={"name": "sort"}, props={"expanded": False, "focusable": True})  # fmt: skip
    popup = (sort[0], page._ax(None, sort[1], "MenuListPopup"))
    options = {}
    for option, chosen in ((words["newest"], True), (words["cheapest"], False)):
        item = page.add(popup, "OPTION", "option", name=option, selected=chosen,
                        props={"selected": chosen, "focusable": True}, rendered=False)  # fmt: skip
        page.text(item, option, rendered=False)
        options[option] = item
    button = page.add(search, "BUTTON", "button", name=words["search"], attrs=submit or {})
    page.text(button, words["search"])
    address = SHOP + "page/2"
    next_page = page.add(main, "A", "link", name=words["next"],
                         attrs={"href": address, **(link or {})},
                         props={"url": address, "focusable": True})  # fmt: skip
    page.text(next_page, words["next"])
    nodes = {"box": box, "stock": stock, "sort": sort, "submit": button, "link": next_page}
    return page, {**nodes, "cheapest": options[words["cheapest"]]}


def contact_form(words) -> tuple[Page, dict]:
    """A message form that posts to the shop's own site, with no field the HTML marks as personal:
    only the reviewer can tell what sending it does."""
    page = Page(SHOP + "contact", title=words["message"])
    form = page.add(page.body, "FORM", "form", name="",
                    attrs={"method": "post", "action": "/contact"})  # fmt: skip
    subject = page.add(form, "INPUT", "textbox", name=words["subject"], value="",
                       attrs={"type": "text", "name": "subject"}, props=FIELD)  # fmt: skip
    page.add(form, "TEXTAREA", "textbox", name=words["message"], value="", props=FIELD)
    send = page.add(form, "BUTTON", "button", name=words["send"])
    page.text(send, words["send"])
    return page, {"subject": subject, "send": send}


def editor(words) -> tuple[Page, dict]:
    """A rich-text editor (an editing host that calls itself a search box) inside a GET form."""
    page = Page(SHOP, title=words["results"])
    form = page.add(page.body, "FORM", "search", name="",
                    attrs={"role": "search", "action": "/search", "method": "get"})  # fmt: skip
    box = page.add(form, "DIV", "searchbox", name=words["search"],
                   attrs={"contenteditable": "true", "role": "searchbox"}, props=FIELD)  # fmt: skip
    return page, {"box": box}


def member_search(words) -> tuple[Page, dict]:
    """A GET form that also holds a password field: what it sends isn't only a query."""
    page = Page(SHOP, title=words["results"])
    form = page.add(page.body, "FORM", "form", name="",
                    attrs={"action": "/members", "method": "get"})  # fmt: skip
    user = page.add(form, "INPUT", "textbox", name=words["user"], value="",
                    attrs={"type": "text", "name": "member"}, props=FIELD)  # fmt: skip
    page.add(form, "INPUT", "textbox", name=words["password"], value="",
             attrs={"type": "password", "name": "pin"}, props=FIELD)  # fmt: skip
    return page, {"user": user}


def field_in_get_form(words, kind_attrs, tag="INPUT") -> tuple[Page, dict]:
    """A GET form to the page's own site with a text field and one more field of ``kind_attrs``."""
    page = Page(SHOP, title=words["results"])
    form = page.add(page.body, "FORM", "form", name="", attrs={"action": "/find", "method": "get"})
    text = page.add(form, "INPUT", "textbox", name=words["search"], value="",
                    attrs={"type": "text", "name": "q"}, props=FIELD)  # fmt: skip
    page.add(form, tag, "textbox", name=words["notes"], value="", attrs=kind_attrs, props=FIELD)
    send = page.add(form, "BUTTON", "button", name=words["search"])
    page.text(send, words["search"])
    return page, {"text": text, "submit": send}


def two_fields_no_button(words) -> tuple[Page, dict]:
    """A GET form with two text fields and no submit button: Enter submits nothing (HTML)."""
    page = Page(SHOP, title=words["results"])
    form = page.add(page.body, "FORM", "form", name="", attrs={"action": "/find", "method": "get"})
    first = page.add(form, "INPUT", "textbox", name=words["search"], value="",
                     attrs={"type": "text", "name": "q"}, props=FIELD)  # fmt: skip
    page.add(form, "INPUT", "textbox", name=words["notes"], value="",
             attrs={"type": "text", "name": "near"}, props=FIELD)  # fmt: skip
    return page, {"text": first}


def shadow_search(words) -> tuple[Page, dict]:
    """A search field inside a custom element's shadow root."""
    page = Page(SHOP, title=words["results"])
    host = page.add(page.body, "SHOP-SEARCH", "generic", name="")
    root = page.add(host, "#document-fragment", shadow=True)
    form = page.add(root, "FORM", "search", name="", attrs={"action": "/search", "method": "get"})
    box = page.add(form, "INPUT", "searchbox", name=words["search"], value="",
                   attrs={"type": "search", "name": "q"}, props=FIELD)  # fmt: skip
    return page, {"box": box}


# ---------------------------------------------------------------------------------------------
# What the page's structure proves


def step(name: str, **arguments):
    return name, DEFINITIONS[name][0](source_id="s", node=0, **arguments)


def stays(page: Page, node, name: str, args) -> bool:
    return stays_on_page(page.build()[0], page.backend_of(node[0]), name, args)


TYPE = step("browser_type", text="βραστήρας 水壶 kettle")
FILL = step("browser_fill", text="βραστήρας")
FILL_SUBMIT = step("browser_fill", text="βραστήρας", submit=True)
ENTER = step("browser_key", key="Enter")
CLICK = step("browser_click")
CHECK = step("browser_check", checked=True)

STAYS = [
    ("type-search", lambda w: finder(w), "box", TYPE),
    ("fill-search", lambda w: finder(w), "box", FILL),
    ("fill-and-send-search", lambda w: finder(w), "box", FILL_SUBMIT),
    ("enter-in-search", lambda w: finder(w), "box", ENTER),
    ("send-button", lambda w: finder(w), "submit", CLICK),
    ("enter-on-send-button", lambda w: finder(w), "submit", ENTER),
    ("filter-box", lambda w: finder(w), "stock", CHECK),
    ("filter-box-click", lambda w: finder(w), "stock", CLICK),
    ("plain-link", lambda w: finder(w), "link", CLICK),
    ("enter-on-link", lambda w: finder(w), "link", ENTER),
    ("link-to-another-site", lambda w: finder(w, link={"href": OTHER_SITE}), "link", CLICK),
    ("explicit-get-same-tab", lambda w: finder(w, form={"target": "_self"}), "submit", CLICK),
    (
        "searchbox-role",
        lambda w: finder(w, field={"type": "text", "role": "searchbox"}),
        "box",
        TYPE,
    ),  # fmt: skip
    ("search-with-suggestions", lambda w: finder(w, field={"role": "combobox"}), "box", TYPE),
    ("search-in-a-post-form", lambda w: finder(w, form={"method": "post"}), "box", TYPE),
    ("base-on-the-same-site", lambda w: finder(w, base={"href": "/shop/"}), "submit", CLICK),
    (
        "formmethod-get-in-post-form",
        lambda w: finder(w, form={"method": "post"}, submit={"formmethod": "get"}),
        "submit",
        CLICK,
    ),  # fmt: skip
    (
        "text-field-of-a-get-form",
        lambda w: field_in_get_form(w, {"type": "number"}),
        "text",
        TYPE,
    ),  # fmt: skip
    (
        "custom-data-around",
        lambda w: finder(w, around={"data-section": "results"}),
        "submit",
        CLICK,
    ),  # fmt: skip
    (
        "annotations-around",
        lambda w: finder(w, around={"itemscope": "", "lang": "el"}),
        "link",
        CLICK,
    ),  # fmt: skip
]

STAYS_NOT = [
    ("post-form-send", lambda w: finder(w, form={"method": "post"}), "submit", CLICK),
    ("post-form-enter", lambda w: finder(w, form={"method": "post"}), "box", ENTER),
    ("post-form-filter", lambda w: finder(w, form={"method": "post"}), "stock", CHECK),
    ("get-to-another-site", lambda w: finder(w, form={"action": OTHER_SITE}), "submit", CLICK),
    (
        "get-to-another-site-enter",
        lambda w: finder(w, form={"action": OTHER_SITE}),
        "box",
        ENTER,
    ),  # fmt: skip
    (
        "backslash-address",
        lambda w: finder(w, form={"action": "\\\\search.partner.example/x"}),
        "submit",
        CLICK,
    ),  # fmt: skip
    (
        "base-on-another-site",
        lambda w: finder(w, base={"href": "https://search.partner.example/"}),
        "submit",
        CLICK,
    ),  # fmt: skip
    ("button-posts", lambda w: finder(w, submit={"formmethod": "post"}), "submit", CLICK),
    ("default-button-posts", lambda w: finder(w, submit={"formmethod": "post"}), "box", ENTER),
    (
        "default-button-elsewhere",
        lambda w: finder(w, submit={"formaction": OTHER_SITE}),
        "box",
        FILL_SUBMIT,
    ),  # fmt: skip
    ("new-tab-form", lambda w: finder(w, form={"target": "_blank"}), "submit", CLICK),
    ("new-tab-button", lambda w: finder(w, submit={"formtarget": "_blank"}), "submit", CLICK),
    ("base-target", lambda w: finder(w, base={"target": "_blank"}), "link", CLICK),
    ("dialog-form", lambda w: finder(w, form={"method": "dialog"}), "submit", CLICK),
    ("inline-submit-handler", lambda w: finder(w, form={"onsubmit": "go()"}), "submit", CLICK),
    ("inline-input-handler", lambda w: finder(w, form={"oninput": "suggest()"}), "box", TYPE),
    ("framework-directive", lambda w: finder(w, form={"hx-post": "/search"}), "submit", CLICK),
    (
        "data-binding-on-the-form",
        lambda w: finder(w, form={"data-action": "submit->go"}),
        "submit",
        CLICK,
    ),  # fmt: skip
    (
        "data-binding-on-the-link",
        lambda w: finder(w, link={"data-method": "delete"}),
        "link",
        CLICK,
    ),  # fmt: skip
    ("inline-click-handler", lambda w: finder(w, link={"onclick": "go()"}), "link", CLICK),
    ("handler-around", lambda w: finder(w, around={"onclick": "route(event)"}), "link", CLICK),
    ("directive-around", lambda w: finder(w, around={"x-on:click": "go"}), "submit", CLICK),
    ("editing-host-around", lambda w: finder(w, around={"contenteditable": ""}), "link", CLICK),
    ("javascript-link", lambda w: finder(w, link={"href": "javascript:go()"}), "link", CLICK),
    (
        "hidden-javascript-link",
        lambda w: finder(w, link={"href": "\x01java\tscript:go()"}),
        "link",
        CLICK,
    ),  # fmt: skip
    ("mail-link", lambda w: finder(w, link={"href": "mailto:shop@example.org"}), "link", CLICK),
    ("same-document-link", lambda w: finder(w, link={"href": "#reviews"}), "link", CLICK),
    ("download-link", lambda w: finder(w, link={"download": ""}), "link", CLICK),
    ("ping-link", lambda w: finder(w, link={"ping": "/count"}), "link", CLICK),
    ("new-tab-link", lambda w: finder(w, link={"target": "_blank"}), "link", CLICK),
    ("link-as-button", lambda w: finder(w, link={"role": "button"}), "link", CLICK),
    ("right-click-link", lambda w: finder(w), "link", step("browser_click", button="right")),
    ("double-click-link", lambda w: finder(w), "link", step("browser_click", clicks=2)),
    ("accepts-its-question", lambda w: finder(w), "submit", step("browser_click", dialog="accept")),
    ("shift-enter", lambda w: finder(w), "box", step("browser_key", key="Enter", shift=True)),
    ("other-key", lambda w: finder(w), "box", step("browser_key", key="ArrowDown")),
    (
        "key-chord",
        lambda w: finder(w),
        "box",
        step("browser_key_chord", key="Enter", modifiers=["Control"]),
    ),  # fmt: skip
    ("rich-text-editor", editor, "box", TYPE),
    ("password-in-the-form", member_search, "user", TYPE),
    ("email-in-the-form", lambda w: field_in_get_form(w, {"type": "email"}), "text", TYPE),
    ("file-in-the-form", lambda w: field_in_get_form(w, {"type": "file"}), "submit", CLICK),
    ("free-text-in-the-form", lambda w: field_in_get_form(w, {}, "TEXTAREA"), "submit", CLICK),
    (
        "personal-token-in-the-form",
        lambda w: field_in_get_form(w, {"autocomplete": "street-address"}),
        "text",
        TYPE,
    ),  # fmt: skip
    ("enter-submits-nothing", two_fields_no_button, "text", ENTER),
    ("shadow-root", shadow_search, "box", TYPE),
]


@pytest.mark.parametrize("language", ["el", "zh", "en"])
@pytest.mark.parametrize("case,build,node,named", STAYS, ids=[case[0] for case in STAYS])
def test_the_page_structure_proves_a_search_a_same_site_get_form_or_a_plain_link_stays(
    language, case, build, node, named
):
    page, nodes = build(words_for(language))
    name, args = named
    assert stays(page, nodes[node], name, args) is True
    # Nothing it does waits for the person either.
    assert (
        structural_effects(page.build()[0], page.backend_of(nodes[node][0]), name, args)["effects"]
        == []
    )


@pytest.mark.parametrize("language", ["el", "zh", "en"])
@pytest.mark.parametrize("case,build,node,named", STAYS_NOT, ids=[case[0] for case in STAYS_NOT])
def test_anything_the_structure_cannot_prove_is_left_to_the_reviewer(
    language, case, build, node, named
):
    page, nodes = build(words_for(language))
    name, args = named
    assert stays(page, nodes[node], name, args) is False


def test_choosing_in_a_get_forms_list_stays_but_a_list_outside_a_form_does_not():
    words = words_for("el")
    page, nodes = finder(words)
    choose = step("browser_select", option_node=0)
    assert stays(page, nodes["sort"], *choose) is True
    many = step("browser_select_many", option_nodes=[0])
    assert stays(page, nodes["sort"], *many) is True
    lone = Page(SHOP, title=words["results"])
    sort = lone.add(lone.body, "SELECT", "combobox", name=words["sort"], attrs={"name": "sort"})
    assert stays(lone, sort, *choose) is False


def test_the_form_reported_to_the_reviewer_follows_the_submitter_html_resolves():
    """Enter in a field submits through the form's default button: its formmethod and
    formaction are the ones the reviewer is told about."""
    page, nodes = finder(words_for("zh"), submit={"formmethod": "post", "formaction": OTHER_SITE})
    form = structural_effects(page.build()[0], page.backend_of(nodes["box"][0]), *ENTER)["form"]
    assert form["method"] == "post"
    assert form["submits_to_origin"] == "https://search.partner.example"
    assert form["other_origin"] is True


# ---------------------------------------------------------------------------------------------
# The whole task: two models with different declared capabilities, the same decisions


class TextModel(Script):
    """A text-only model: it declares no image input."""

    model = "text-only-fixture"


class VisionModel(Script):
    """A model that declares image input: it is offered screenshots and coordinate clicks."""

    model = "vision-fixture"
    _alysis_browser_image_input = True


MODELS = [TextModel, VisionModel]


@pytest.fixture
def owl(tmp_path):
    store = Store(tmp_path / "website-steps.db")
    bot = store.save_bot("owner", BotInput(name="Γλαύκα", purpose="Αγορές"))
    chat_ = store.create_chat("owner", bot["id"], "Chat")
    harness = Harness()
    harness.adapter.interactive_previews = False
    yield store, bot, chat_, harness
    harness.adapter.close()


def start(owl, page: Page, words: dict):
    store, _, chat_, harness = owl
    browser = serve(harness, page)
    store.enqueue("owner", chat_["id"], TaskInput(message=words["request"], request_id=uuid4().hex))
    return browser, store.claim("owner")


def events(store, task, kind):
    return [e["data"] for e in store.task("owner", task["id"])["events"] if e["kind"] == kind]


@pytest.mark.parametrize("model", MODELS, ids=["text-only", "vision"])
@pytest.mark.parametrize("language", ["el", "zh"])
def test_a_get_search_filters_and_paging_never_call_the_reviewer(owl, language, model):
    store, bot, _, harness = owl
    words = words_for(language)
    page, _ = finder(words)
    browser, task = start(owl, page, words)
    box = f'- searchbox "{words["search"]}"'
    script = model(
        [
            lambda _: ("browser_observe", {}),
            tool("browser_type", at(box, text="βραστήρας 水壶")),
            tool(
                "browser_select",
                lambda r: {
                    **at(f'- combobox "{words["sort"]}"')(r),
                    "option_node": ref(r["page_view"], f'- option "{words["cheapest"]}"'),
                },
            ),
            tool("browser_key", at(box, key="Enter")),
            tool("browser_click", at(f'- button "{words["search"]}"')),
            tool("browser_click", at(f'- link "{words["next"]}"')),
            lambda _: "The cheapest kettle costs 19.90.",
        ]
    )
    runtime = runtime_for(store, harness, script)
    runtime.run(task, runtime.task_token(task["id"]))

    done = store.task("owner", task["id"])
    assert done["status"] == "completed", done.get("error")
    # The two models really differ: only the one declaring image input gets coordinate clicks.
    offered = {schema["function"]["name"] for schema in script.turns[0]["tools"]}
    assert ("browser_visual_click" in offered) is (model is VisionModel)
    assert sent(browser) == ["type", "select", "key", "click", "click"]
    assert script.reviews == []  # the reviewer model was never called
    assert events(store, task, "action_review_call") == []
    assert runtime.action_reviews.usage(bot["id"])["actions"]["calls"] == 0
    assert runtime.action_reviews.list(bot["id"]) == []
    # Each step says the page's structure settled it, in the shape of today's review events.
    reviews = events(store, task, "action_review")
    assert [review["name"] for review in reviews] == [
        "browser_type",
        "browser_select",
        "browser_key",
        "browser_click",
        "browser_click",
    ]
    for review in reviews:
        assert review["outcome"] == "allow" and review["effect"] == "none"
        assert review["model"] == "" and review["elapsed_ms"] == 0
        assert "page's own structure" in review["message"]


@pytest.mark.parametrize("model", MODELS, ids=["text-only", "vision"])
@pytest.mark.parametrize("language", ["el", "zh"])
def test_a_post_contact_form_always_calls_the_reviewer_on_the_turns_own_model(owl, language, model):
    store, bot, _, harness = owl
    words = words_for(language)
    page, _ = contact_form(words)
    browser, task = start(owl, page, words)
    reason = "Sending this form delivers the message to the shop."
    script = model(
        [
            lambda _: ("browser_observe", {}),
            tool("browser_type", at(f'- textbox "{words["subject"]}"', text="Βραστήρας 水壶")),
            tool("browser_click", at(f'- button "{words["send"]}"')),
        ],
        [verdict(), verdict("ask_person", "send", reason)],
    )
    runtime = runtime_for(store, harness, script)
    runtime.run(task, runtime.task_token(task["id"]))

    assert store.task("owner", task["id"])["status"] == "waiting"
    assert sent(browser) == ["type"]
    assert len(script.reviews) == 2
    assert all("WEBSITE INPUT" in review[0]["content"] for review in script.reviews)
    assert [call["model"] for call in events(store, task, "action_review_call")] == [
        script.model,
        script.model,
    ]
    (card,) = runtime.action_reviews.list(bot["id"])
    assert card["reason"] == reason and card["effect"] == "send"


STILL_REVIEWED = [
    (
        "rich-text-editor",
        "el",
        editor,
        lambda w: tool("browser_type", at(f'- searchbox "{w["search"]}"', text="βραστήρας")),
    ),  # fmt: skip
    (
        "get-to-another-site",
        "zh",
        lambda w: finder(w, form={"action": OTHER_SITE}),
        lambda w: tool("browser_click", at(f'- button "{w["search"]}"')),
    ),  # fmt: skip
    (
        "password-field",
        "el",
        member_search,
        lambda w: tool("browser_type", at(f'- textbox "{w["user"]}"', text="4711")),
    ),  # fmt: skip
    (
        "javascript-link",
        "zh",
        lambda w: finder(w, link={"href": "javascript:go()"}),
        lambda w: tool("browser_click", at(f'- link "{w["next"]}"')),
    ),  # fmt: skip
]


@pytest.mark.parametrize("model", MODELS, ids=["text-only", "vision"])
@pytest.mark.parametrize(
    "case,language,build,make", STILL_REVIEWED, ids=[case[0] for case in STILL_REVIEWED]
)
def test_steps_the_structure_cannot_prove_are_reviewed_as_before(
    owl, case, language, build, make, model
):
    store, bot, _, harness = owl
    words = words_for(language)
    page, _ = build(words)
    browser, task = start(owl, page, words)
    script = model([lambda _: ("browser_observe", {}), make(words), lambda _: "Done."], [verdict()])
    runtime = runtime_for(store, harness, script)
    runtime.run(task, runtime.task_token(task["id"]))

    done = store.task("owner", task["id"])
    assert done["status"] == "completed", done.get("error")
    assert len(sent(browser)) == 1
    (review,) = script.reviews
    assert "WEBSITE INPUT" in review[0]["content"]
    assert [call["model"] for call in events(store, task, "action_review_call")] == [script.model]
    (event,) = events(store, task, "action_review")
    assert event["model"] == script.model


@pytest.mark.parametrize("language", ["el", "zh"])
def test_a_saved_ask_first_rule_still_wins_over_the_page_structure(owl, language):
    store, bot, _, harness = owl
    words = words_for(language)
    page, _ = finder(words)
    browser, task = start(owl, page, words)
    runtime = runtime_for(store, harness, None)
    rule = runtime.action_reviews.save_rule(
        bot["id"],
        RuleInput(effect="ask", description="Ρώτα με πριν από κάθε αναζήτηση. 搜索前先问我。"),
    )
    script = TextModel(
        [
            lambda _: ("browser_observe", {}),
            tool("browser_type", at(f'- searchbox "{words["search"]}"', text="βραστήρας")),
        ],
        [
            verdict(
                "ask_person",
                "none",
                "Your rule asks first for searches.",
                rule_matches={rule["id"]: "match"},
            )
        ],
    )
    runtime.client_factory = lambda _: script
    runtime.run(task, runtime.task_token(task["id"]))

    assert store.task("owner", task["id"])["status"] == "waiting"
    assert sent(browser) == []
    assert len(script.reviews) == 1  # the rule needs the reviewer: the structure doesn't skip it
    (card,) = runtime.action_reviews.list(bot["id"])
    assert card["reason"] == "A matching Ask first rule requires your approval."


@pytest.mark.parametrize("language", ["el", "zh"])
def test_reviewing_every_step_yourself_still_asks_about_a_search(owl, language):
    store, bot, _, harness = owl
    words = words_for(language)
    page, _ = finder(words)
    browser, task = start(owl, page, words)
    script = VisionModel(
        [
            lambda _: ("browser_observe", {}),
            tool("browser_click", at(f'- link "{words["next"]}"')),
        ]
    )
    runtime = runtime_for(store, harness, script)
    runtime.action_reviews.settings(bot["id"], ReviewSettings(mode="review"))
    runtime.run(task, runtime.task_token(task["id"]))

    assert store.task("owner", task["id"])["status"] == "waiting"
    assert sent(browser) == [] and script.reviews == []
    (card,) = runtime.action_reviews.list(bot["id"])
    assert card["reason"] == "You chose to review computer actions yourself."
