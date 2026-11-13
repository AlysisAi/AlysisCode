from __future__ import annotations

import copy
import json

import pytest

from alysis_code.ide.browser_observation import compact_dom_snapshot


def snapshot():
    return {
        "strings": [
            "http://example.test/",
            "Δοκιμή 日本語",
            "#document",
            "HTML",
            "HEAD",
            "BODY",
            "INPUT",
            "P",
            "#text",
            "aria-label",
            "入力",
            "Hidden value",
            "Visible value",
            "   Ελληνικά 日本語\n",
        ],
        "documents": [
            {
                "documentURL": 0,
                "title": 1,
                "nodes": {
                    "parentIndex": [-1, 0, 1, 1, 3, 3, 3, 6],
                    "nodeType": [9, 1, 1, 1, 1, 1, 1, 3],
                    "nodeName": [2, 3, 4, 5, 6, 6, 7, 8],
                    "nodeValue": [-1, -1, -1, -1, -1, -1, -1, 13],
                    "attributes": [[], [], [], [], [], [9, 10], [], []],
                    "inputValue": {"index": [4, 5], "value": [11, 12]},
                    "inputChecked": {"index": [5]},
                },
                "layout": {"nodeIndex": [1, 3, 5, 6, 7]},
            }
        ],
    }


def test_structure_unicode_and_hidden_siblings_preserved_without_raw_layout():
    source = snapshot()
    original = copy.deepcopy(source)
    view = compact_dom_snapshot(source)
    doc = view["documents"][0]
    nodes = {node["node"]: node for node in doc["nodes"]}
    assert 4 not in nodes
    assert nodes[5]["selector"] == ":root > :nth-child(2) > :nth-child(2)"
    assert nodes[5]["attributes"] == {"aria-label": "入力"}
    assert nodes[5]["value"] == "Visible value"
    assert nodes[5]["checked"] is True
    assert nodes[7]["text"] == "   Ελληνικά 日本語\n"
    assert doc["omitted_nodes"] == 2
    assert "layout" not in doc
    assert source == original


def test_clipped_fields_have_explicit_marker_and_original_remains_readable():
    source = snapshot()
    source["strings"][13] = "Ω日本語" * 1000
    view = compact_dom_snapshot(source, field_chars=20)
    node = view["documents"][0]["nodes"][-1]
    assert node["clipped"] is True
    assert view["clipped"] is True
    assert node["text"] == source["strings"][13][:20]
    assert len(source["strings"][13]) == 4000


def test_no_action_selector_for_child_documents_shadow_or_pseudo_nodes():
    source = snapshot()
    frame = copy.deepcopy(source["documents"][0])
    source["documents"].append(frame)
    source["documents"][0]["nodes"]["shadowRootType"] = {"index": [3], "value": [1]}
    view = compact_dom_snapshot(source)
    assert all("selector" not in node for node in view["documents"][1]["nodes"])
    assert all(
        "selector" not in node for node in view["documents"][0]["nodes"] if node["node"] >= 3
    )
    source = snapshot()
    source["documents"][0]["nodes"]["pseudoType"] = {"index": [5], "value": [1]}
    view = compact_dom_snapshot(source)
    node = next(node for node in view["documents"][0]["nodes"] if node["node"] == 5)
    assert "selector" not in node


def test_large_record_list_reports_omission_and_invalid_source_is_not_success():
    source = snapshot()
    source["strings"][10] = "long label " * 300
    source["strings"][13] = "long text " * 300
    view = compact_dom_snapshot(source, max_chars=1000)
    assert view["clipped"] is True
    assert view["documents"][0]["truncated"] is True
    assert len(json.dumps(view, ensure_ascii=False)) <= 1000
    with pytest.raises(KeyError):
        compact_dom_snapshot({})


def test_document_metadata_and_frames_share_the_character_budget():
    source = snapshot()
    source["strings"][0] += "x" * 3000
    source["documents"] *= 40
    view = compact_dom_snapshot(source, max_chars=1000)
    assert view["omitted_documents"] > 0
    assert view["clipped"] is True
    assert len(json.dumps(view, ensure_ascii=False)) <= 1000


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("type", "password"),
        ("autocomplete", "one-time-code"),
        ("autocomplete", "section-login current-password"),
    ],
)
def test_private_field_values_are_removed_from_raw_and_truncated_observations(attribute, value):
    from alysis_code.ide.managed_browser import _bounded_payload

    data = snapshot()
    data["strings"] += [attribute, value, "value", "arbitrary-秘密-α", "arbitrary-秘密-α"]
    data["documents"][0]["nodes"]["attributes"][5] = [14, 15, 16, 17]
    data["documents"][0]["nodes"]["inputValue"]["value"][1] = 17
    for limit in (100, 100000):
        result = _bounded_payload(data, limit)
        assert "arbitrary-秘密-α" not in json.dumps(result, ensure_ascii=False)
    assert data["strings"][17] == "arbitrary-秘密-α"  # No mutation of the source capture.
