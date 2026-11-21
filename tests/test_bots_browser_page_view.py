"""Owls read a page as a page view: the accessibility tree as text, with element refs.

Each fixture is a pair, as one observation reads it: a DOMSnapshot and the full accessibility
tree of the same document, with matching backend node ids, shaped like Chromium's output.
"""

from __future__ import annotations

import copy
import json

import pytest
from test_bots_browser import PREVIEW, BrowserFixture, Harness

from alysis_code.bots.browser_tools import BrowserObservationExpired
from alysis_code.cancellation import CooperativeCancellationError
from alysis_code.ide.browser_observation import (
    PAGE_VIEW_CHARS,
    PageViewUnavailable,
    accessibility_page_view,
    compact_dom_snapshot,
    page_view_element,
    redact_form_secrets,
)
from alysis_code.ide.managed_browser import BrowserCancelledError, BrowserTimeoutError


class Page:
    """One page as Chromium reports it: DOM nodes and accessibility nodes built together."""

    def __init__(self, url=PREVIEW, title="Page", *, frame="FRAME-MAIN", first_backend=1):
        self.strings: list[str] = []
        self.url_string = self.string(url)
        self.title, self.frame = title, frame
        self.nodes: dict[str, list] = {
            key: []
            for key in (
                "parentIndex",
                "nodeType",
                "nodeName",
                "nodeValue",
                "backendNodeId",
                "attributes",
            )
        }
        self.values: dict[int, str] = {}
        self.checked: list[int] = []
        self.selected: list[int] = []
        self.layout: list[int] = []
        self.shadow: list[int] = []
        self.ax: list[dict] = []
        self.backend = first_backend
        document = self._dom(-1, 9, "#document", rendered=False)
        self.root = self._ax(document, None, "RootWebArea", name=title)
        self.root["frameId"] = frame
        html = self._dom(document, 1, "HTML")
        html_ax = self._ax(html, self.root, "none", ignored=True)
        body = self._dom(html, 1, "BODY")
        self.body = (body, self._ax(body, html_ax, "none", ignored=True))

    def string(self, value: str) -> int:
        self.strings.append(value)
        return len(self.strings) - 1

    def _dom(self, parent, kind, name, *, value=None, attrs=None, rendered=True) -> int:
        index = len(self.nodes["parentIndex"])
        self.nodes["parentIndex"].append(parent)
        self.nodes["nodeType"].append(kind)
        self.nodes["nodeName"].append(self.string(name))
        self.nodes["nodeValue"].append(-1 if value is None else self.string(value))
        self.nodes["backendNodeId"].append(self.backend)
        self.backend += 1
        self.nodes["attributes"].append(
            [
                i
                for key, item in (attrs or {}).items()
                for i in (self.string(key), self.string(item))
            ]
        )
        if rendered:
            self.layout.append(index)
        return index

    def _ax(self, index, parent, role, *, name=None, value=None, props=None, ignored=False):
        node = {
            "nodeId": str(len(self.ax) + 1),
            "ignored": ignored,
            "role": {"type": "role", "value": role},
            "childIds": [],
        }
        if index is not None:
            node["backendDOMNodeId"] = self.nodes["backendNodeId"][index]
        if name is not None:
            node["name"] = {"type": "computedString", "value": name, "sources": []}
        if value is not None:
            node["value"] = {"type": "string", "value": value}
        if props:
            node["properties"] = [
                {"name": key, "value": {"type": "token", "value": item}}
                for key, item in props.items()
            ]
        if parent is not None:
            node["parentId"] = parent["nodeId"]
            parent["childIds"].append(node["nodeId"])
        self.ax.append(node)
        return node

    def add(self, parent, tag, role=None, *, name=None, attrs=None, value=None, ax_value=None,
            props=None, checked=False, selected=False, rendered=True, shadow=False):  # fmt: skip
        """An element; with a role, also its accessibility node (else its children attach to
        the parent's). ``value`` is the form value; the accessibility value defaults to it."""
        dom_parent, ax_parent = parent
        index = self._dom(dom_parent, 1, tag, attrs=attrs, rendered=rendered)
        if value is not None:
            self.values[index] = value
        if checked:
            self.checked.append(index)
        if selected:
            self.selected.append(index)
        if shadow:
            self.shadow.append(index)
        node = ax_parent
        if role is not None:
            node = self._ax(
                index,
                ax_parent,
                role,
                name=name,
                value=ax_value if ax_value is not None else value,
                props=props,
            )
        return index, node

    def text(self, parent, text, *, rendered=True) -> int:
        dom_parent, ax_parent = parent
        index = self._dom(dom_parent, 3, "#text", value=text, rendered=rendered)
        node = self._ax(index, ax_parent, "StaticText", name=text)
        self._ax(None, node, "InlineTextBox", name=text)
        return index

    def build(self) -> tuple[dict, dict]:
        """The DOM snapshot as the browser service returns it (after its credential redaction)
        and the accessibility tree of the same moment."""
        strings = list(self.strings)

        def string(value):
            strings.append(value)
            return len(strings) - 1

        values = sorted(self.values.items())
        snapshot = {
            "strings": strings,
            "documents": [
                {
                    "documentURL": self.url_string,
                    "title": string(self.title),
                    "frameId": string(self.frame),
                    "nodes": {
                        **copy.deepcopy(self.nodes),
                        "inputValue": {
                            "index": [index for index, _ in values],
                            "value": [string(value) for _, value in values],
                        },
                        "inputChecked": {"index": list(self.checked)},
                        "optionSelected": {"index": list(self.selected)},
                        "shadowRootType": {
                            "index": list(self.shadow),
                            "value": [string("open") for _ in self.shadow],
                        },
                    },
                    "layout": {"nodeIndex": list(self.layout)},
                }
            ],
        }
        return redact_form_secrets(snapshot), {"nodes": copy.deepcopy(self.ax)}

    def backend_of(self, index: int) -> int:
        return self.nodes["backendNodeId"][index]


class PageBrowser(BrowserFixture):
    """The lab browser, answering accessibility snapshots too (as the managed browser does)."""

    def __init__(self, page: Page):
        super().__init__()
        self.show(page)
        self.tree_truncated = False
        self.tree_failure = None

    def show(self, page: Page) -> None:
        self.data, self.tree = page.build()

    def snapshot(self, owner, session_id, **kwargs):
        if kwargs.get("kind") != "accessibility":
            return super().snapshot(owner, session_id, **kwargs)
        self._call("snapshot", owner, session_id, **kwargs)
        if self.tree_failure:
            raise self.tree_failure
        return {
            "data": copy.deepcopy(self.tree),
            "command_generation": self.generation,
            "truncated": self.tree_truncated,
        }

    def _acted(self):
        # The page answers input (so the post-action observation need not wait for a change).
        self.data["strings"].append("acted")

    def click(self, owner, session_id, selector, **kwargs):
        super().click(owner, session_id, selector, **kwargs)
        self._acted()

    def type_text(self, owner, session_id, selector, text, **kwargs):
        self._call("type", owner, session_id, selector, text, **kwargs)
        self._acted()

    def select_option(self, owner, session_id, selector, option, **kwargs):
        self._call("select", owner, session_id, selector, option, **kwargs)
        self._acted()


@pytest.fixture
def lab():
    harness = Harness()
    yield harness
    harness.adapter.close()


def show(lab, page: Page) -> PageBrowser:
    """Serve ``page`` from the owl's computer (which starts on the first observation)."""
    if lab.executors:
        lab.executors[0].browser.show(page)
        return lab.executors[0].browser
    browser = PageBrowser(page)
    lab.factory_hook = lambda executor, cancel: setattr(executor, "browser", browser)
    return browser


def ref(view: str, line_start: str) -> int:
    """The ref number on the page view line that starts (after indentation) with ``line_start``."""
    line = next(line for line in view.splitlines() if line.lstrip().startswith(line_start))
    return int(line.split("[ref=e", 1)[1].split("]", 1)[0])


def link_ref(view: str, number: int) -> int:
    """The ref of the results page link to result ``number``."""
    line = next(
        line
        for line in view.splitlines()
        if line.endswith(f" -> https://example.org/result/{number}")
    )
    return int(line.split("[ref=e", 1)[1].split("]", 1)[0])


def results_page(count: int) -> tuple[Page, list[int]]:
    """A search results list: per result a link (heading and address) and a snippet."""
    page = Page(title="Results")
    main = page.add(page.body, "MAIN", "main", name="")
    page.text(page.add(main, "H1", "heading", name="Results", props={"level": 1}), "Results")
    listing = page.add(main, "OL", "list", name="")
    links = []
    for number in range(1, count + 1):
        item = page.add(
            listing,
            "LI",
            "listitem",
            name="",
            attrs={"class": f"result-card result-card--{number} js-track", "data-pos": str(number)},
        )
        address = f"https://example.org/result/{number}"
        link = page.add(
            item,
            "A",
            "link",
            name=f"Result {number}",
            attrs={"href": address, "class": "result-link"},
            props={"url": address, "focusable": True},
        )
        page.text(page.add(link, "H3", "heading", name=f"Result {number}", props={"level": 3}),
                  f"Result {number}")  # fmt: skip
        links.append(link[0])
        snippet = page.add(item, "DIV", "generic", attrs={"class": "snippet"})
        page.text(snippet, f"Snippet for result {number}.")
    return page, links


def test_results_list_shows_far_more_of_the_page_than_dom_nodes_and_refs_click_the_link(lab):
    page, links = results_page(90)
    browser = show(lab, page)
    observation = lab.call()
    view = observation["page_view"]
    assert "dom" not in observation and "page_view_unavailable" not in observation
    assert observation["url"] == PREVIEW and observation["title"] == "Results"
    assert view.startswith('- main [ref=e1]\n  - heading "Results" [level=1] [ref=e2]\n')
    # A link named by its content shows that content once, and where it goes.
    assert (
        "      - link [ref=e5] -> https://example.org/result/1\n"
        '        - heading "Result 1" [level=3] [ref=e6]\n'
        "      - text [ref=e7]: Snippet for result 1.\n"
    ) in view
    assert "result-card" not in view and "data-pos" not in view
    assert len(view) <= PAGE_VIEW_CHARS and view.endswith("Snippet for result 90.")
    # Today's element view of the same snapshot holds a small part of the list.
    compact = compact_dom_snapshot(browser.data)
    assert compact["documents"][0]["truncated"]
    assert "Result 20" not in json.dumps(compact, ensure_ascii=False)
    # A ref is a main-document element: the click keeps the selector and identity check.
    lab.call("browser_click", {"source_id": observation["source_id"], "node": link_ref(view, 77)})
    click = next(call for call in browser.calls if call[0] == "click")
    expected = compact_dom_snapshot(browser.data, around=links[76])
    record = next(node for node in expected["documents"][0]["nodes"] if node["node"] == links[76])
    assert click[3][0] == record["selector"]
    assert click[4]["expected_backend_node_id"] == page.backend_of(links[76])
    assert click[4]["expected_generation"] == 17


def article_page(paragraphs: int, *, words: int = 40) -> Page:
    page = Page(title="Article")
    article = page.add(page.body, "ARTICLE", "article", name="")
    page.text(page.add(article, "H1", "heading", name="A long read", props={"level": 1}),
              "A long read")  # fmt: skip
    for number in range(1, paragraphs + 1):
        paragraph = page.add(article, "P", "paragraph", name="")
        page.text(paragraph, f"Paragraph {number}: " + " ".join(["word"] * words))
    footer = page.add(page.body, "FOOTER", "contentinfo", name="")
    page.add(footer, "BUTTON", "button", name="Back to top", props={"focusable": True})
    return page


def test_long_article_is_cut_with_a_clear_line_and_browser_source_reads_on(lab):
    show(lab, article_page(200))
    observation = lab.call()
    view = observation["page_view"]
    shown, note = view.rsplit("\n", 1)
    assert len(shown) <= PAGE_VIEW_CHARS
    assert note.startswith("[Page view cut for length: ") and "browser_source" in note
    offset = int(note.split("from offset ", 1)[1].split(",", 1)[0])
    assert observation["source_chars"] > PAGE_VIEW_CHARS
    # The rest of the page, refs included, is in the retained source: nothing is lost.
    start = lab.call("browser_source", {"source_id": observation["source_id"]})
    # A part of a page view holds 20,000 characters (4,000 of DOM data): the shown lines first.
    assert len(start["content"]) == 20_000 and start["content"].startswith(shown[:-1])
    part = lab.call("browser_source", {"source_id": observation["source_id"], "offset": offset})
    assert part["offset"] == offset
    first_missing = part["content"].split("\n", 1)[0]
    assert first_missing.lstrip().startswith("- paragraph [ref=e") and first_missing not in shown
    end = lab.call(
        "browser_source",
        {"source_id": observation["source_id"], "find": '- button "Back to top"'},
    )
    button = ref(end["content"], '- button "Back to top"')
    # A ref read from the source acts like one shown in the observation.
    lab.call("browser_click", {"source_id": observation["source_id"], "node": button})
    browser = lab.executors[0].browser
    assert sum(call[0] == "click" for call in browser.calls) == 1


def test_one_very_long_paragraph_is_cut_inside_it_rather_than_hidden():
    page = article_page(1, words=6000)
    data, tree = page.build()
    result = accessibility_page_view(tree, data)
    shown, note = result["text"].rsplit("\n", 1)
    assert len(shown) == PAGE_VIEW_CHARS + 1 and shown.endswith("…")
    assert f"from offset {PAGE_VIEW_CHARS}," in note
    assert shown[:-1] == result["full_text"][:PAGE_VIEW_CHARS]
    assert not result["complete"]


def sign_in_page() -> tuple[Page, dict]:
    page = Page(title="Σύνδεση")
    form = page.add(page.body, "FORM", "form", name="")
    label = page.add(form, "LABEL", "LabelText")
    page.text(label, "Email ")
    email = page.add(label, "INPUT", "textbox", name="Email", value="anna@example.org",
                     attrs={"type": "email"}, props={"editable": "plaintext", "focusable": True})  # fmt: skip
    password = page.add(form, "INPUT", "textbox", name="Κωδικός", value="hunter2-secret",
                        ax_value="•" * 14, attrs={"type": "password"},
                        props={"editable": "plaintext", "focusable": True})  # fmt: skip
    code = page.add(form, "INPUT", "textbox", name="Code", value="481516",
                    attrs={"autocomplete": "one-time-code"},
                    props={"editable": "plaintext", "focusable": True})  # fmt: skip
    # Chromium's own editor nodes inside a text field repeat its value.
    editor = page.add(code, "DIV", "generic", rendered=False)
    page.text(editor, "481516", rendered=False)
    echo = page.add(form, "P", "paragraph", name="")
    page.text(echo, "481516")
    remember = page.add(form, "INPUT", "checkbox", name="Να με θυμάσαι",
                        attrs={"type": "checkbox"}, checked=True,
                        props={"checked": "true", "focusable": True})  # fmt: skip
    page.values[remember[0]] = "on"  # a form value the browser does not report as a value
    country = page.add(form, "SELECT", "combobox", name="Χώρα", ax_value="Κύπρος",
                       props={"expanded": False, "focusable": True})  # fmt: skip
    popup = (country[0], page._ax(None, country[1], "MenuListPopup"))
    options = {}
    for label_text, chosen in (("Ελλάδα", False), ("Κύπρος", True)):
        option = page.add(popup, "OPTION", "option", name=label_text, selected=chosen,
                          props={"selected": chosen, "focusable": True}, rendered=False)  # fmt: skip
        page.text(option, label_text, rendered=False)
        options[label_text] = option[0]
    submit = page.add(form, "BUTTON", "button", name="Είσοδος", props={"disabled": True})
    page.text(submit, "Είσοδος")
    return page, {"email": email[0], "password": password[0], "code": code[0], **options}


def test_form_values_show_and_credential_fields_are_masked_everywhere(lab):
    page, nodes = sign_in_page()
    browser = show(lab, page)
    observation = lab.call()
    view = observation["page_view"]
    assert '- textbox "Email" [ref=' in view and "]: anna@example.org" in view
    assert '- textbox "Κωδικός" [ref=' in view
    lines = {line.split(" [ref=")[0].strip(): line for line in view.splitlines()}
    assert lines['- textbox "Κωδικός"'].endswith(": [private field]")
    assert lines['- textbox "Code"'].endswith(": [private field]")
    assert lines["- paragraph"].endswith(": [private field]")  # the same code shown as text
    assert lines['- checkbox "Να με θυμάσαι" [checked]'].endswith("]")  # no form value "on"
    assert '- button "Είσοδος" [disabled]' in view
    assert '- option "Κύπρος" [selected]' in view and '- combobox "Χώρα" [ref=' in view
    assert "]: Κύπρος" in lines['- combobox "Χώρα"']
    retained = lab.call("browser_source", {"source_id": observation["source_id"]})["content"]
    for secret in ("hunter2-secret", "481516", "•"):
        assert secret not in json.dumps(observation, ensure_ascii=False)
        assert secret not in retained
    # Typing goes to the observed field through its ref, with today's identity check.
    email = ref(view, '- textbox "Email"')
    lab.call("browser_type", {"source_id": observation["source_id"], "node": email, "text": "Νέα"})
    typed = next(call for call in browser.calls if call[0] == "type")
    assert typed[4]["expected_backend_node_id"] == page.backend_of(nodes["email"])
    # A dropdown choice uses the select's and the option's refs.
    lab.call(
        "browser_select",
        {
            "source_id": observation["source_id"],
            "node": ref(view, '- combobox "Χώρα"'),
            "option_node": ref(view, '- option "Ελλάδα"'),
        },
    )
    chosen = next(call for call in browser.calls if call[0] == "select")
    assert chosen[4]["expected_option_backend_node_id"] == page.backend_of(nodes["Ελλάδα"])


def test_greek_page_reads_in_greek_and_find_ignores_case_and_accents(lab):
    page = Page(title="Παραγγελία")
    main = page.add(page.body, "MAIN", "main", name="")
    page.text(page.add(main, "H1", "heading", name="Η παραγγελία σας", props={"level": 1}),
              "Η παραγγελία σας")  # fmt: skip
    for number in range(60):
        row = page.add(main, "P", "paragraph", name="")
        page.text(row, f"Προϊόν {number}: ")
        page.text(page.add(row, "B", None), "διαθέσιμο")
    cart = page.add(main, "BUTTON", "button", name="Προσθήκη στο καλάθι", props={"focusable": True})
    page.text(cart, "Προσθήκη στο καλάθι")
    browser = show(lab, page)
    view = lab.call()["page_view"]
    # Inline text joins as written: the bold word stays on its sentence's line.
    assert "- paragraph [ref=e3]: Προϊόν 0: διαθέσιμο" in view
    found = lab.call("browser_observe", {"find": "ΠΡΟΣΘΗΚΗ ΣΤΟ ΚΑΛΑΘΙ"})
    assert found["find"] == {"find": "ΠΡΟΣΘΗΚΗ ΣΤΟ ΚΑΛΑΘΙ", "matches": 1}
    assert '- button "Προσθήκη στο καλάθι" [ref=' in found["page_view"]
    assert "Προϊόν 3:" not in found["page_view"]  # far from the match
    button = ref(found["page_view"], '- button "Προσθήκη')
    lab.call("browser_click", {"source_id": found["source_id"], "node": button})
    assert sum(call[0] == "click" for call in browser.calls) == 1


def test_chinese_page_keeps_text_unspaced_and_finds_a_control_by_its_words(lab):
    page = Page(title="北京")
    main = page.add(page.body, "MAIN", "main", name="")
    paragraph = page.add(main, "P", "paragraph", name="")
    page.text(paragraph, "北京")
    page.text(page.add(paragraph, "STRONG", "strong", name=""), "天安门")
    page.text(paragraph, "广场很大。")
    search = page.add(main, "INPUT", "searchbox", name="搜索", value="故宫",
                      props={"editable": "plaintext", "focusable": True})  # fmt: skip
    show(lab, page)
    observation = lab.call()
    assert "- paragraph [ref=e2]: 北京天安门广场很大。" in observation["page_view"]
    assert '- searchbox "搜索" [ref=e3]: 故宫' in observation["page_view"]
    found = lab.call("browser_observe", {"find": "搜索"})
    assert found["find"]["matches"] == 1 and "故宫" in found["page_view"]
    none = lab.call("browser_observe", {"find": "购物车"})
    assert none["find"]["matches"] == 0
    assert none["page_view"].startswith('[Nothing on this page matches "购物车".')
    lab.call("browser_type", {"source_id": found["source_id"], "node": 3, "text": "长城"})
    typed = next(call for call in lab.executors[0].browser.calls if call[0] == "type")
    assert typed[4]["expected_backend_node_id"] == page.backend_of(search[0])


def test_refs_stay_valid_while_their_element_lives_and_are_not_consumed(lab):
    page, links = results_page(5)
    browser = show(lab, page)
    first = lab.call()
    view = first["page_view"]
    after = lab.call("browser_click", {"source_id": first["source_id"], "node": link_ref(view, 3)})
    assert after["action"]["dispatched"]
    # The same element keeps its ref in the next observation.
    assert after["page_view"] == view
    # The first observation was not used up: its other refs still act on the live page.
    lab.call("browser_click", {"source_id": first["source_id"], "node": link_ref(view, 4)})
    clicks = [call for call in browser.calls if call[0] == "click"]
    assert [call[4]["expected_backend_node_id"] for call in clicks] == [
        page.backend_of(links[2]),
        page.backend_of(links[3]),
    ]
    # New elements get new numbers; numbers are never reused for another element.
    grown, _ = results_page(6)
    show(lab, grown)
    refreshed = lab.call()["page_view"]
    assert ref(refreshed, '- heading "Results"') == 2
    assert link_ref(refreshed, 6) > max(
        int(part.split("]")[0]) for part in view.split("[ref=e")[1:]
    )


@pytest.mark.parametrize(
    "change,message",
    [
        ("element_replaced", "no longer on the page"),
        ("element_retagged", "no longer on the page"),
        ("new_document", "page changed since"),
        ("new_address", "page changed since"),
        ("generation", "page changed since"),
    ],
)
def test_a_gone_element_or_changed_page_refuses_with_observe_again(lab, change, message):
    page, links = results_page(3)
    browser = show(lab, page)
    observation = lab.call()
    second = link_ref(observation["page_view"], 2)
    nodes = browser.data["documents"][0]["nodes"]
    if change == "element_replaced":  # the list re-rendered: result 2 is another node now
        nodes["backendNodeId"][links[1]] = 10_000
    elif change == "element_retagged":
        nodes["nodeName"][links[1]] = nodes["nodeName"][2]
    elif change == "new_document":  # a navigation, even to the same address
        nodes["backendNodeId"][0] = 9_999
    elif change == "new_address":
        browser.data["strings"][0] = "http://127.0.0.1:8123/other"
    else:  # the browser was navigated, switched or taken over by command
        browser.generation += 1
    with pytest.raises(BrowserObservationExpired, match=message):
        lab.call("browser_click", {"source_id": observation["source_id"], "node": second})
    assert not any(call[0] == "click" for call in browser.calls)


def test_an_unknown_ref_is_refused_without_input(lab):
    page, _ = results_page(2)
    browser = show(lab, page)
    observation = lab.call()
    with pytest.raises(ValueError, match="e999 is not a ref"):
        lab.call("browser_click", {"source_id": observation["source_id"], "node": 999})
    assert not any(call[0] == "click" for call in browser.calls)


def test_refs_need_a_current_observation_after_the_task_let_go(lab):
    page, _ = results_page(2)
    browser = show(lab, page)
    observation = lab.call()
    lab.adapter.release("task-a", cancelled=False)
    link = link_ref(observation["page_view"], 1)
    with pytest.raises(BrowserObservationExpired, match="stale or already used"):
        lab.call("browser_click", {"source_id": observation["source_id"], "node": link})
    assert not any(call[0] == "click" for call in browser.calls)


@pytest.mark.parametrize(
    "breakage,reason",
    [
        ("missing", "did not return an accessibility tree"),
        ("truncated", "larger than the browser's snapshot limit"),
        ("cut", "cut short"),
        ("other_frame", "not of this page's main document"),
        ("timeout", "could not read this page's accessibility tree (BrowserTimeoutError)"),
    ],
)
def test_without_a_usable_accessibility_tree_the_element_view_is_shown_and_says_why(
    lab, breakage, reason
):
    page, _ = results_page(3)
    browser = show(lab, page)
    if breakage == "missing":
        browser.tree = {"nodes": []}
    elif breakage == "truncated":
        browser.tree_truncated = True
        browser.tree = {"preview": '{"nodes":[{"nodeId":"1"'}
    elif breakage == "cut":
        browser.tree["nodes"][4]["childIds"].append("not-sent")
    elif breakage == "other_frame":
        browser.tree["nodes"][0]["frameId"] = "FRAME-OTHER"
    else:
        browser.tree_failure = BrowserTimeoutError("Synthetic slow tree")
    observation = lab.call()
    assert reason in observation["page_view_unavailable"]
    assert "act with a node number from dom" in observation["page_view_unavailable"]
    assert "page_view" not in observation
    assert observation["dom"]["documents"][0]["nodes"]
    # Today's element view and its actions work unchanged.
    link = next(
        node["node"]
        for node in observation["dom"]["documents"][0]["nodes"]
        if node["name"] == "A" and "selector" in node
    )
    lab.call("browser_click", {"source_id": observation["source_id"], "node": link})
    assert sum(call[0] == "click" for call in browser.calls) == 1


def test_cancellation_while_reading_the_tree_is_not_a_fallback(lab):
    page, _ = results_page(2)
    browser = show(lab, page)
    browser.tree_failure = BrowserCancelledError("Synthetic cancellation")
    with pytest.raises(CooperativeCancellationError):
        lab.call()
    assert lab.executors[0].closed


def test_other_frames_and_shadow_content_are_readable_but_have_no_refs():
    page = Page()
    host = page.add(page.body, "DIV", "generic")
    page.add(host, "BUTTON", "button", name="Inside shadow", shadow=True, props={"focusable": True})
    frame = page.add(page.body, "IFRAME", "Iframe", name="Embedded")
    child = page._ax(None, frame[1], "RootWebArea", name="Embedded page")
    child["frameId"] = "FRAME-CHILD"
    # Another frame's node whose backend id also exists in the main document is never a ref.
    page.add((frame[0], child), "BUTTON", "button", name="In frame", props={"focusable": True})
    page.add(page.body, "BUTTON", "button", name="Main", props={"focusable": True})
    data, tree = page.build()
    view = accessibility_page_view(tree, data)["text"]
    assert view.splitlines() == [
        '- button "Inside shadow"',
        '- Iframe "Embedded" [ref=e1]',
        '  - button "In frame"',
        '- button "Main" [ref=e2]',
    ]


def test_a_browser_that_reaches_elements_by_identity_gets_refs_in_shadow_roots_and_frames():
    page = Page()
    host = page.add(page.body, "DIV", "generic")
    shadow = page.add(host, "BUTTON", "button", name="Μέσα", shadow=True, props={"focusable": True})
    frame = page.add(page.body, "IFRAME", "Iframe", name="Φόρμα")
    child = page._ax(None, frame[1], "RootWebArea", name="Embedded page")
    child["frameId"] = "FRAME-CHILD"
    in_frame = page.add((frame[0], child), "BUTTON", "button", name="在框架中",
                        props={"focusable": True})  # fmt: skip
    main = page.add(page.body, "BUTTON", "button", name="Main", props={"focusable": True})
    data, tree = page.build()
    described = [
        {"id": "FRAME-MAIN", "url": PREVIEW, "origin": "http://127.0.0.1:8123", "loader": "M"},
        {"id": "FRAME-CHILD", "url": "https://forms.example/", "origin": "https://forms.example",
         "loader": "C1"},
    ]  # fmt: skip
    # The same frame's tree again, as its own root (the browser reads each frame too): shown once.
    tree["nodes"].append(
        {
            "nodeId": "FRAME-CHILD/1",
            "frameId": "FRAME-CHILD",
            "role": {"value": "RootWebArea"},
            "childIds": ["FRAME-CHILD/2"],
        }
    )
    tree["nodes"].append(
        {
            "nodeId": "FRAME-CHILD/2",
            "parentId": "FRAME-CHILD/1",
            "role": {"value": "button"},
            "name": {"value": "在框架中"},
            "backendDOMNodeId": page.backend_of(in_frame[0]),
            "properties": [{"name": "focusable", "value": {"value": True}}],
        }
    )
    result = accessibility_page_view({**tree, "frames": described}, data, identity_refs=True)
    assert result["text"].splitlines() == [
        '- button "Μέσα" [ref=e1]',
        '- Iframe "Φόρμα" [ref=e2]',
        '  - button "在框架中" [ref=e3]',
        '- button "Main" [ref=e4]',
    ]
    assert result["refs"] == {
        2: (page.backend_of(frame[0]), "IFRAME"),
        4: (page.backend_of(main[0]), "BUTTON"),
    }
    assert result["frame_refs"] == {
        1: (None, None, page.backend_of(shadow[0]), "BUTTON"),
        3: ("FRAME-CHILD", "C1", page.backend_of(in_frame[0]), "button"),
    }
    # The refs stay while the frame shows the same document; another document numbers anew.
    again = accessibility_page_view(
        {**tree, "frames": described}, data, previous=result, identity_refs=True
    )
    assert again["frame_refs"] == result["frame_refs"]
    described[1]["loader"] = "C2"
    reloaded = accessibility_page_view(
        {**tree, "frames": described}, data, previous=result, identity_refs=True
    )
    assert reloaded["frame_refs"][1] == result["frame_refs"][1]
    assert 3 not in reloaded["frame_refs"] and reloaded["frame_refs"][5][1] == "C2"
    # A frame the snapshot does not describe stays readable without refs.
    unknown = accessibility_page_view({**tree, "frames": described[:1]}, data, identity_refs=True)
    assert '  - button "在框架中"' in unknown["text"].splitlines()


def test_page_view_element_records_match_the_element_view_exactly():
    page, nodes = sign_in_page()
    long_label = "Λ" * 400
    extra = page.add(page.body, "BUTTON", "button", name="Long", attrs={"aria-label": long_label})
    page.text(extra, "Long")
    data, tree = page.build()
    result = accessibility_page_view(tree, data)
    compact = compact_dom_snapshot(data, max_chars=200_000)
    expected = {node["node"]: node for node in compact["documents"][0]["nodes"]}
    for backend, name in result["refs"].values():
        index, records = page_view_element(data, result["document"], backend, name)
        assert records[0]["node"] == index
        for record in records:
            assert record == expected[record["node"]]
    assert expected[extra[0]]["clipped"] is True
    assert page_view_element(data, {**result["document"], "url": "https://other.example/"},
                             page.backend_of(nodes["email"]), "INPUT") is None  # fmt: skip


def test_page_view_refuses_trees_it_cannot_render_faithfully():
    page, _ = results_page(1)
    data, tree = page.build()
    with pytest.raises(PageViewUnavailable, match="did not return"):
        accessibility_page_view({}, data)
    deep = Page()
    parent = deep.body
    for _ in range(260):
        parent = deep.add(parent, "DIV", "generic")
    deep.text(parent, "Deep")
    deep_data, deep_tree = deep.build()
    with pytest.raises(PageViewUnavailable, match="nested too deeply"):
        accessibility_page_view(deep_tree, deep_data)
    assert accessibility_page_view(tree, data)["complete"]


def greek_page_with_frames(*, inline_frame=True) -> Page:
    page = Page(title="Κράτηση")
    page.text(page.add(page.body, "H1", "heading", name="Κράτηση θέσης", props={"level": 1}),
              "Κράτηση θέσης")  # fmt: skip
    button = page.add(page.body, "BUTTON", "button", name="Συνέχεια", props={"focusable": True})
    page.text(button, "Συνέχεια")
    if inline_frame:
        frame = page.add(page.body, "IFRAME", "Iframe", name="Χάρτης")
        child = page._ax(None, frame[1], "RootWebArea", name="Χάρτης")
        child["frameId"] = "FRAME-MAP"
        page.text((frame[0], child), "Αθήνα")
    return page


DEPARTED = {
    "id": "FRAME-AD",
    "url": "https://ads.example/slot?id=1",
    "origin": "https://ads.example",
    "loader": "",
    "unavailable": True,
    "error": "CdpCommandError",
    "code": -32602,
}


def test_a_frame_the_browser_could_no_longer_read_is_named_in_the_page_view():
    page = greek_page_with_frames()
    data, tree = page.build()
    described = [
        {"id": "FRAME-MAIN", "url": PREVIEW, "origin": "http://127.0.0.1:8123", "loader": "M"},
        DEPARTED,
        # Gone too, but its content is already in the main tree: not named twice.
        {**DEPARTED, "id": "FRAME-MAP", "url": "https://maps.example/"},
    ]
    for identity_refs in (False, True):
        view = accessibility_page_view(
            {**tree, "frames": described}, data, identity_refs=identity_refs
        )["text"].splitlines()
        assert view == [
            '- heading "Κράτηση θέσης" [level=1] [ref=e1]',
            '- button "Συνέχεια" [ref=e2]',
            '- Iframe "Χάρτης" [ref=e3]: Αθήνα',
            "- frame [unavailable] -> https://ads.example/slot?id=1",
        ]


def test_an_observation_with_a_departed_frame_keeps_its_page_view_and_says_so(
    lab, tmp_path, monkeypatch
):
    from alysis_code.bots import diagnostics

    monkeypatch.setattr(diagnostics, "_path", tmp_path / "diagnostics.jsonl")
    browser = show(lab, greek_page_with_frames(inline_frame=False))
    browser.tree["frames"] = [
        {"id": "FRAME-MAIN", "url": PREVIEW, "origin": "http://127.0.0.1:8123", "loader": "M"},
        DEPARTED,
    ]
    observation = lab.call()
    assert "page_view_unavailable" not in observation
    view = observation["page_view"]
    assert '- button "Συνέχεια" [ref=' in view
    assert "- frame [unavailable] -> https://ads.example/slot?id=1" in view
    button = ref(view, '- button "Συνέχεια"')
    lab.call("browser_click", {"source_id": observation["source_id"], "node": button})
    assert sum(call[0] == "click" for call in browser.calls) == 1
    entries = [
        json.loads(line) for line in (tmp_path / "diagnostics.jsonl").read_text().splitlines()
    ]
    departed = [entry for entry in entries if entry["event"] == "browser_frame_unavailable"]
    assert departed and departed[0]["frames"] == 1
    assert (departed[0]["error_type"], departed[0]["code"]) == ("CdpCommandError", -32602)
