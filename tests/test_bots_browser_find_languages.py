"""A focused observation (browser_observe find) matches page text the same way in every language."""

from __future__ import annotations

import time

import pytest
from test_bots_browser_large_pages import long_page, node_names

from alysis_code.ide.browser_observation import compact_dom_snapshot


@pytest.mark.parametrize(
    ("label", "phrase"),
    [
        ("ΞΕΝΟΔΟΧΕΙΑ", "ξενοδοχεία"),  # capitals written without their accents
        ("Ξενοδοχεία", "ξενοδοχεια"),  # a phrase typed without accents
        ("Hôtels à Paris", "hotels a paris"),
        ("Precio más bajo", "PRECIO MAS BAJO"),
        ("Café Müller", "cafe muller"),  # non-breaking space
        ("Prix : 1 200 €", "prix : 1 200"),  # narrow no-break spaces
        ("Preis-­vergleich", "preis-vergleich"),  # soft hyphen
        ("Sort\n      by   price", "sort by price"),  # source line breaks and indentation
        ("ＨＯＴＥＬ　ＳＥＡＲＣＨ", "hotel search"),  # full-width letters and space
        ("İstanbul otelleri", "istanbul otelleri"),
        ("Straße", "STRASSE"),
        ("مَدِينَة", "مدينة"),  # vowel marks the phrase leaves out
        ("Đà Nẵng", "da nang"),  # a letter with a stroke, not a separate accent
        ("Łódź", "lodz"),
        ("København", "kobenhavn"),
        ("酒店", "酒店"),
        ("Поиск отелей", "ПОИСК"),
    ],
)
def test_find_ignores_case_spacing_and_accents_in_any_script(label, phrase):
    page = long_page(2000, [label])
    focused = compact_dom_snapshot(page, focus=phrase)
    assert focused["focus"]["matches"] == 1, (label, phrase)
    assert "INPUT" in node_names(focused)  # the control next to it is reachable


def test_an_exact_spelling_on_the_page_wins_over_a_looser_one():
    page = long_page(5, ["año", "ano"])
    view = compact_dom_snapshot(page, focus="año")
    assert view["focus"]["matches"] == 1
    texts = [node.get("text") for node in view["documents"][0]["nodes"]]
    assert "año" in texts and "ano" not in texts
    assert compact_dom_snapshot(page, focus="ano")["focus"]["matches"] == 1
    # With no exact spelling on the page, the phrase still finds the accented one.
    assert compact_dom_snapshot(long_page(5, ["año"]), focus="ano")["focus"]["matches"] == 1


def add_node(page, parent, node_type, name, value=None):
    strings, document = page["strings"], page["documents"][0]
    nodes = document["nodes"]
    for text in (name, value):
        if text is not None and text not in strings:
            strings.append(text)
    nodes["parentIndex"].append(parent)
    nodes["nodeType"].append(node_type)
    nodes["nodeName"].append(strings.index(name))
    nodes["nodeValue"].append(-1 if value is None else strings.index(value))
    nodes["attributes"].append([])
    index = len(nodes["parentIndex"]) - 1
    document["layout"]["nodeIndex"].append(index)
    return index


@pytest.mark.parametrize(
    ("parts", "phrase"),
    [
        (("Sort ", "by", " price"), "sort by price"),
        (("Ταξινόμηση ", "κατά", " τιμή"), "ταξινομηση κατα τιμη"),
        (("価格", "の", "安い順"), "価格の安い順"),
    ],
)
def test_find_matches_a_label_split_by_inline_markup(parts, phrase):
    page = long_page(2000, ["Hotels"])
    label = add_node(page, 2, 1, "LABEL")
    add_node(page, label, 3, "#text", parts[0])
    bold = add_node(page, label, 1, "B")
    add_node(page, bold, 3, "#text", parts[1])
    add_node(page, label, 3, "#text", parts[2])
    focused = compact_dom_snapshot(page, focus=phrase)
    assert focused["focus"]["matches"] == 1  # the label, not also BODY and HTML around it
    assert any(node["node"] == label for node in focused["documents"][0]["nodes"])


def test_find_counts_only_what_the_page_shows():
    page = long_page(5, ["Hotels", "Hotels"])
    document = page["documents"][0]
    names = document["nodes"]["nodeName"]
    first = next(i for i, name in enumerate(names) if page["strings"][name] == "LABEL")
    hidden = {first, first + 1}  # a label and its text without layout (display: none)
    document["layout"]["nodeIndex"] = [
        i for i in document["layout"]["nodeIndex"] if i not in hidden
    ]
    assert compact_dom_snapshot(page, focus="hotels")["focus"]["matches"] == 1


def test_find_stays_quick_on_a_large_page_in_any_script():
    labels = [f"Επιλογή {i} ξενοδοχείο" for i in range(300)]
    page = long_page(20_000, labels)
    compact_dom_snapshot(page, focus="warm up")  # first use builds the Unicode tables
    started = time.perf_counter()
    view = compact_dom_snapshot(page, focus="ΞΕΝΟΔΟΧΕΙΟ")
    assert view["focus"]["matches"] == 300
    assert time.perf_counter() - started < 5
