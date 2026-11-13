"""Owls can act on large real-world pages: focused observations, clear refusals, own frames."""

from __future__ import annotations

import json

import pytest

from alysis_code.agent.approval_context import safe_text
from alysis_code.bots.browser import (
    REVIEW_FORM_CHARS,
    _embedded_documents_allowed,
    _review_form,
    _review_page,
    _visible_links,
)
from alysis_code.ide.browser_observation import compact_dom_snapshot


def long_page(filler: int, labels: list[str]) -> dict:
    """A body with ``filler`` paragraphs, then one checkbox+label row per label (in any script)."""
    strings = [
        "https://example.org/",
        "Results",
        "#document",
        "HTML",
        "BODY",
        "P",
        "#text",
        "Filler text paragraph",
        "DIV",
        "INPUT",
        "LABEL",
        "type",
        "checkbox",
    ]
    strings += labels
    parents, types, names, values, attributes, layout = (
        [-1, 0, 1],
        [9, 1, 1],
        [2, 3, 4],
        [-1, -1, -1],
        [[], [], []],
        [1, 2],
    )
    for _ in range(filler):
        p = len(parents)
        parents += [2, p]
        types += [1, 3]
        names += [5, 6]
        values += [-1, 7]
        attributes += [[], []]
        layout += [p, p + 1]
    for offset, _label in enumerate(labels):
        row = len(parents)
        parents += [2, row, row, row + 2]
        types += [1, 1, 1, 3]
        names += [8, 9, 10, 6]
        values += [-1, -1, -1, 13 + offset]
        attributes += [[], [11, 12], [], []]
        layout += [row, row + 1, row + 2, row + 3]
    return {
        "strings": strings,
        "documents": [
            {
                "documentURL": 0,
                "title": 1,
                "nodes": {
                    "parentIndex": parents,
                    "nodeType": types,
                    "nodeName": names,
                    "nodeValue": values,
                    "attributes": attributes,
                },
                "layout": {"nodeIndex": layout},
            }
        ],
    }


def node_names(view):
    return [node["name"] for node in view["documents"][0]["nodes"]]


def test_a_control_far_down_a_long_page_is_reachable_with_find():
    page = long_page(2000, ["Apartments", "Hotels", "Ξενοδοχεία", "酒店"])
    plain = compact_dom_snapshot(page)
    assert "INPUT" not in node_names(plain) and plain["clipped"]  # the controls are past the budget

    for phrase in ("hotels", "HOTELS", "ξενοδοχεία", "酒店"):
        focused = compact_dom_snapshot(page, focus=phrase)
        assert focused["focus"] == {"find": phrase, "matches": 1}
        nodes = focused["documents"][0]["nodes"]
        checkbox = [node for node in nodes if node["name"] == "INPUT"]
        assert len(checkbox) == 1 and "selector" in checkbox[0]  # clickable like any observed node
        assert "P" not in node_names(focused)  # the filler is left out
        assert len(nodes) < 20


def test_find_matches_labels_too_and_reports_no_match_plainly():
    page = long_page(5, ["Hotels"])
    label_index = page["strings"].index("type")
    assert label_index
    page["strings"].append("aria-label")
    page["strings"].append("Sort by price")
    sort = len(page["documents"][0]["nodes"]["parentIndex"])
    nodes = page["documents"][0]["nodes"]
    nodes["parentIndex"].append(2)
    nodes["nodeType"].append(1)
    nodes["nodeName"].append(page["strings"].index("DIV"))
    nodes["nodeValue"].append(-1)
    nodes["attributes"].append([len(page["strings"]) - 2, len(page["strings"]) - 1])
    page["documents"][0]["layout"]["nodeIndex"].append(sort)
    focused = compact_dom_snapshot(page, focus="sort by")
    assert focused["focus"]["matches"] == 1
    assert any(node["node"] == sort for node in focused["documents"][0]["nodes"])
    missing = compact_dom_snapshot(page, focus="no such control")
    assert missing["focus"]["matches"] == 0
    with pytest.raises(ValueError):
        compact_dom_snapshot(page, focus="   ")


def frame_page(extra: list[str], *, frames: int | None = None, plugin: bool = False) -> dict:
    strings = ["https://example.org/", "#document", "HTML", "IFRAME", "OBJECT"] + extra
    names = [1, 2] + [3] * (len(extra) if frames is None else frames) + ([4] if plugin else [])
    documents = [{"documentURL": 0, "nodes": {"nodeName": names}}]
    documents += [{"documentURL": 5 + i, "nodes": {"nodeName": [1]}} for i in range(len(extra))]
    return {"strings": strings, "documents": documents}


@pytest.mark.parametrize(
    "page,allowed",
    [
        (frame_page([]), True),
        (frame_page(["about:blank", "about:srcdoc"]), True),
        (frame_page(["https://ads.example.net/frame"]), False),  # another site's content
        (frame_page([], frames=1), False),  # a frame whose document was not captured (cross-site)
        (frame_page([], plugin=True), False),
    ],
)
def test_only_a_pages_own_helper_frames_allow_input(page, allowed):
    assert _embedded_documents_allowed(page) is allowed


def test_acting_on_a_text_only_observation_is_explained_and_never_pauses_the_task(monkeypatch):
    from test_bots_browser import Harness, large_page

    lab = Harness()
    try:
        lab.factory_hook = large_page
        observation = lab.call()
        assert observation["read_only_text"]
        for _ in range(4):  # more than the three changed-page attempts that pause a task
            with pytest.raises(ValueError, match="too large to act on") as refusal:
                lab.call("browser_click", {"source_id": observation["source_id"], "node": 6})
            assert "No input was sent" in str(refusal.value)
        assert not any(call[0] == "click" for call in lab.executors[0].browser.calls)
    finally:
        lab.adapter.close()


def with_backend_ids(page: dict) -> dict:
    nodes = page["documents"][0]["nodes"]
    nodes["backendNodeId"] = [1000 + index for index in range(len(nodes["parentIndex"]))]
    return page


def test_a_review_sees_the_page_around_its_target_far_down_a_long_page():
    page = with_backend_ids(long_page(2000, ["Apartments", "Hotels", "Villas"]))
    names = page["documents"][0]["nodes"]["nodeName"]
    inputs = [i for i, name in enumerate(names) if page["strings"][name] == "INPUT"]
    target = inputs[1]  # the "Hotels" checkbox, past the normal view's budget
    view = _review_page(page, 1000 + target)
    assert view["around"] == target
    nodes = view["documents"][0]["nodes"]
    assert any(node["node"] == target and "selector" in node for node in nodes)
    texts = {node.get("text") for node in nodes}
    assert {"Apartments", "Hotels", "Villas"} <= texts  # its label and its neighbours
    assert len(json.dumps(view, ensure_ascii=False)) <= 12_000
    # A target the fresh page no longer has: the page's start, still bounded.
    fallback = _review_page(page, 1)
    assert "around" not in fallback and len(json.dumps(fallback, ensure_ascii=False)) <= 12_000


def test_around_is_one_main_document_element_and_not_combined_with_find():
    page = long_page(3, ["Hotels"])
    with pytest.raises(ValueError):
        compact_dom_snapshot(page, around=10_000)
    with pytest.raises(ValueError):
        compact_dom_snapshot(page, around=4, focus="Hotels")


def test_a_large_form_is_reviewed_within_budget_filled_fields_first():
    fields = [
        {
            "tag": "INPUT",
            "type": "checkbox",
            "identity": {"aria-label": f"Filter {i} " + "ξ" * 80},
            "checked": i in {150, 151},
            "value": "on",
        }
        for i in range(400)
    ] + [{"tag": "INPUT", "type": "text", "identity": {"name": "q"}, "value": "Ναύπλιο"}]
    form = {
        "url": "https://example.org/",
        "title": "Results",
        "fields": fields,
        "complete": True,
        "omitted_controls": 0,
        "partial_controls": 0,
        "embedded_documents": 0,
    }
    reviewed = _review_form(form)
    assert len(json.dumps(reviewed, ensure_ascii=False)) <= REVIEW_FORM_CHARS
    assert reviewed["complete"] is False and reviewed["fields_omitted"] == 401 - len(
        reviewed["fields"]
    )
    kept = reviewed["fields"]
    assert fields[150] in kept and fields[151] in kept and fields[-1] in kept
    small = {**form, "fields": fields[:3]}
    assert _review_form(small) is small


def test_a_review_of_a_big_page_with_a_big_form_fits_the_reviewer():
    page = with_backend_ids(long_page(3000, [f"Option {i} " + "ξ" * 60 for i in range(300)]))
    names = page["documents"][0]["nodes"]["nodeName"]
    target = [i for i, name in enumerate(names) if page["strings"][name] == "INPUT"][200]
    fields = [
        {
            "tag": "INPUT",
            "type": "checkbox",
            "identity": {"aria-label": f"Option {i} " + "ξ" * 60},
            "checked": False,
            "value": "on",
        }
        for i in range(300)
    ]
    form = {"url": "https://example.org/", "title": "Results", "fields": fields, "complete": True}
    action = {
        "tool": "browser_click",
        "arguments": {"node": target},
        "observed_context": {
            "url": "https://example.org/",
            "target": {"node": target, "name": "INPUT"},
            "label": "Option 200",
            "page": _review_page(page, 1000 + target),
            "form": _review_form(form),
        },
    }
    assert len(safe_text(action)) < 32_000  # the automatic reviewer's budget for a whole action


def busy_page(sections: int, items: int) -> dict:
    """``sections`` blocks that each mention "Hotels" amid ``items`` spans, then the Hotels filter."""
    strings = [
        "https://example.org/",
        "Results",
        "#document",
        "HTML",
        "BODY",
        "SECTION",
        "DIV",
        "SPAN",
        "#text",
        "Hotels nearby",
        "item",
        "INPUT",
        "LABEL",
        "type",
        "checkbox",
        "Hotels",
    ]
    parents, types, names, values, attributes = [-1, 0, 1], [9, 1, 1], [2, 3, 4], [-1] * 3, [[]] * 3

    def add(parent, node_type, name, value=-1, attrs=()):
        parents.append(parent)
        types.append(node_type)
        names.append(name)
        values.append(value)
        attributes.append(list(attrs))
        return len(parents) - 1

    for _ in range(sections):
        section = add(2, 1, 5)
        mention = add(section, 1, 6)
        add(mention, 3, 8, 9)
        for _ in range(items):
            span = add(section, 1, 7)
            add(span, 3, 8, 10)
    row = add(add(2, 1, 5), 1, 6)
    add(row, 1, 11, attrs=(13, 14))
    label = add(row, 1, 12)
    add(label, 3, 8, 15)
    return {
        "strings": strings,
        "documents": [
            {
                "documentURL": 0,
                "title": 1,
                "nodes": {
                    "parentIndex": parents,
                    "nodeType": types,
                    "nodeName": names,
                    "nodeValue": values,
                    "attributes": attributes,
                },
                "layout": {"nodeIndex": list(range(1, len(parents)))},
            }
        ],
    }


def test_many_matches_keep_less_around_each_so_the_last_one_still_fits():
    page = busy_page(40, 30)
    fixed = compact_dom_snapshot(page, focus="hotels", focus_region=40)
    assert fixed["documents"][0]["truncated"] and "INPUT" not in node_names(fixed)
    view = compact_dom_snapshot(page, focus="hotels")
    assert view["focus"]["matches"] == 41
    assert not view["documents"][0]["truncated"]
    assert "INPUT" in node_names(view)  # the filter at the end is still reachable


def linked_page(filler: int) -> dict:
    """``filler`` paragraphs, then links as a results page has them: after everything else."""
    page = long_page(filler, [])
    strings = page["strings"]
    document = page["documents"][0]
    nodes, layout = document["nodes"], document["layout"]["nodeIndex"]

    def s(value):
        strings.append(value)
        return len(strings) - 1

    def add(parent, node_type, name, value=-1, attrs=(), shown=True):
        nodes["parentIndex"].append(parent)
        nodes["nodeType"].append(node_type)
        nodes["nodeName"].append(name)
        nodes["nodeValue"].append(value)
        nodes["attributes"].append(list(attrs))
        index = len(nodes["parentIndex"]) - 1
        if shown:
            layout.append(index)
        return index

    a, span, text = s("A"), s("SPAN"), strings.index("#text")
    href, label, body = s("href"), s("aria-label"), 2
    first = add(body, 1, a, attrs=(href, s("/funds/one")))
    add(add(first, 1, span), 3, text, s("Fund "))
    add(first, 3, text, s("One   Capital"))
    greek = add(body, 1, a, attrs=(href, s("https://example.gr/φόροι")))
    add(greek, 3, text, s("Προθεσμίες ΦΠΑ"))
    add(body, 1, a, attrs=(href, s("https://example.org/share"), label, s("Share")))
    script = add(body, 1, a, attrs=(href, s("javascript:void(0)")))
    add(script, 3, text, s("Menu"))
    again = add(body, 1, a, attrs=(href, s("/funds/one")))
    add(again, 3, text, s("Fund One again"))
    hidden = add(body, 1, a, attrs=(href, s("https://hidden.example/")), shown=False)
    add(hidden, 3, text, s("Hidden"), shown=False)
    top = add(body, 1, a, attrs=(href, s("#top")))
    add(top, 3, text, s("Back to top"))
    return page


LINKS = [
    {"text": "Fund One Capital", "url": "https://example.org/funds/one"},
    {"text": "Προθεσμίες ΦΠΑ", "url": "https://example.gr/φόροι"},
    {"text": "Share", "url": "https://example.org/share"},
]


def test_links_are_read_the_way_a_person_sees_them():
    # Text split by markup joins, a label stands in for an icon, relative addresses resolve;
    # scripts, repeats, hidden links and links back to the same page are left out.
    assert _visible_links(linked_page(3)) == (LINKS, False)
    assert _visible_links(linked_page(3), limit=2) == (LINKS[:2], True)


def busy_results(executor, cancel):
    executor.browser.data = linked_page(2000)


def test_a_busy_page_still_shows_its_text_and_links():
    from test_bots_browser import Harness

    lab = Harness()
    try:
        lab.factory_hook = busy_results
        observation = lab.call()
        assert observation["dom"]["documents"][0]["truncated"]  # the elements did not fit
        assert observation["page_text"].startswith("Filler text paragraph\nFiller text paragraph")
        assert len(observation["page_text"]) <= 6000 < observation["page_text_chars"]
        assert observation["links"] == LINKS and "links_omitted" not in observation
        assert "browser_navigate" in observation["readable_note"]
        assert observation["untrusted_page_data"] and "read_only_text" not in observation
        # Reading comes from this very observation: the browser is asked nothing more.
        calls = lab.executors[0].browser.calls
        assert not any(c[0] == "snapshot" and c[4].get("kind") == "text" for c in calls)
        focused = lab.call("browser_observe", {"find": "Fund One"})
        assert "page_text" not in focused and "links" not in focused
    finally:
        lab.adapter.close()


def test_rendered_text_reads_lines_the_way_the_page_lays_them_out():
    from alysis_code.bots.browser import _rendered_text

    strings = [
        "https://example.org/",
        "#text",
        "P",
        "SCRIPT",
        "A",
        "Fund ",
        "One   Capital",
        "Προθεσμίες ΦΠΑ",
        "var x = 1",
        "Hidden",
        "  One Capital  ",
        "Second line",
    ]
    page = {
        "strings": strings,
        "documents": [
            {
                "documentURL": 0,
                "nodes": {
                    #             0   1  2  3  4  5  6  7  8  9
                    "parentIndex": [-1, 0, 1, 1, 3, 0, 5, 0, 7, 1],
                    "nodeType": [9, 1, 3, 1, 3, 1, 3, 1, 3, 3],
                    "nodeName": [-1, 2, 1, 4, 1, 3, 1, 2, 1, 1],
                    "nodeValue": [-1, -1, 5, -1, 6, -1, 8, -1, 9, 11],
                },
                # Text boxes in layout order with their rendered strings and positions; the
                # script and the hidden paragraph have no layout entry.
                "layout": {
                    "nodeIndex": [1, 2, 3, 4, 9],
                    "text": [-1, 5, -1, 10, -1],
                    "bounds": [
                        [0, 0, 300, 20],
                        [0, 0, 40, 20],
                        [40, 0, 90, 20],
                        [40, 0, 90, 20],
                        [0, 24, 80, 20],
                    ],
                },
            }
        ],
    }
    assert _rendered_text(page) == "Fund One Capital\nSecond line"
    # Without positions, a new parent element starts a new line.
    for key in ("bounds", "text"):
        page["documents"][0]["layout"].pop(key)
    assert _rendered_text(page) == "Fund\nOne Capital\nSecond line"
    page["documents"][0]["layout"]["nodeIndex"].append(8)  # once shown, it is read
    assert _rendered_text(page).endswith("Second line\nHidden")
    assert _rendered_text({}) == ""


def test_a_page_that_fits_gets_no_extra_reading():
    from test_bots_browser import Harness

    lab = Harness()
    try:
        observation = lab.call()
        assert "page_text" not in observation and "links" not in observation
        calls = lab.executors[0].browser.calls
        assert not any(c[0] == "snapshot" and c[4].get("kind") == "text" for c in calls)
    finally:
        lab.adapter.close()


def test_a_control_replaced_by_a_re_render_is_explained_and_nothing_is_sent():
    from test_bots_browser import Harness

    from alysis_code.ide.managed_browser import BrowserNotFoundError

    lab = Harness()
    try:
        lab.call()
        browser = lab.executors[0].browser

        def gone(*args, **kwargs):
            raise BrowserNotFoundError("Synthetic re-render")

        browser.click = gone
        messages = []
        for _ in range(3):
            source = lab.call()["source_id"]
            with pytest.raises(Exception) as caught:
                lab.call("browser_click", {"source_id": source, "node": 6})
            messages.append(str(caught.value))
        assert all("No input was sent" in m or "keeps changing" in m for m in messages)
        assert "page changed after it was observed" in messages[0]
        assert "keeps changing" in messages[2]  # the third time, the person is asked to look
        assert not lab.executors[0].closed
    finally:
        lab.adapter.close()
